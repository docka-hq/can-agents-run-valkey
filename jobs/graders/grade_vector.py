# Grader: vector job, v2 (sixth revision, 2026-10-07). Runs INSIDE the task container after the agent stops, with
# the grader's own Python. The job file prepends `CFG = {...}` (product, version, the 5 held-out queries and their
# exact cosine top-5); the agent never sees the queries or the answers.
#
# What "done" means (from the prompt): all 1,000 articles are loaded into the server; `python3 /app/search.py <
# query.json` prints the ids of the 5 most similar articles, one per line, most similar first; the similarity search
# runs INSIDE the server; and the articles and everything search.py needs are left in the server.
#   - loaded: every one of the 1,000 articles in the article file is stored in the server WITH ITS OWN EMBEDDING: a
#     record that holds the article's id and its embedding (FLOAT32, FLOAT64, FLOAT16, BFLOAT16, INT8 or UINT8 bytes,
#     a JSON array, a list of numbers, or one hash field per coordinate; plain, compressed or base64), in any database,
#     or a vector-set element whose vector is the article's or that is named by its id. A record is a hash, JSON document or string holding one article's embedding, whatever other ids
#     it names; a hash holding the embeddings of several articles is shared, and read entry by entry. An id stored
#     without its embedding does not count, nor does an id not in the file. Two indexes of 500 pass.
#   - correct: exactly 5 ids, the true top 5 in order, for every query; and every article can be found: eleven
#     articles spread over the set (the first, the last, every hundredth), queried with their own embedding, come
#     back first (an index whose prefix leaves some articles out fails here).
#   - inside the server, with the article file moved away, measured on the same run that is scored:
#       1. the server's own command counters show a search command (FT.SEARCH, FT.AGGREGATE, FT.HYBRID or VSIM) or a
#          server-side script (EVAL, FCALL: similarity computed in Lua also runs inside the server) for every query;
#       2. the server sends search.py at most 100 KB per query. A heuristic: a search reply is a few KB, while
#          computing the similarity outside the server from vectors read back needs all of them, over 256 KB;
#       3. the article ranked first for query 1 is removed from the server, as deleting it would: the record holding
#          its embedding (the whole record, whatever other ids it mentions), its entries in a shared hash and in lists
#          or sets, its vector-set element, and any record holding its id and no other. search.py must then run cleanly and print the true
#          top 5 of the remaining articles, computed here. An answer computed from a local copy keeps returning the
#          removed article. If nothing in the server holds the article, the check cannot run and the job fails.
# Not detectable here, and documented as limits in the README: a search.py that fetches a few hundred candidates
# from a server-side search and re-ranks them itself, or that computes the answer from a local copy and asks the
# server only which articles still exist.
import base64
import binascii
import bz2
import gzip
import json
import lzma
import math
import os
import re
import struct
import subprocess
import sys
import time
import uuid
import zlib

import redis

facts: dict = {}
reasons: list = []
MAX_BYTES_PER_QUERY = 100_000
SEARCH_COMMANDS = ("ft.search", "ft.aggregate", "ft.hybrid", "ft.profile", "vsim",
                   "eval", "evalsha", "eval_ro", "evalsha_ro", "fcall", "fcall_ro")  # Lua runs in the server too
ID_RX = re.compile(rb"kb-\d{4}")
SAME_VECTOR = 0.99      # cosine at or above which a stored vector is taken to be an article's embedding
RECORD_MAX_IDS = 50     # a value naming more articles than this is an index or a list, not one article's record


def finish():
    ok = not reasons
    print(json.dumps({"pass": ok, "reasons": reasons, "facts": facts}, sort_keys=True, default=str))
    sys.exit(0 if ok else 1)


def identity(r) -> bool:
    info = r.info("server")
    facts["server"] = {k: info.get(k) for k in ("server_name", "redis_version", "valkey_version")}
    if CFG["product"] == "valkey":
        return info.get("server_name") == "valkey" and str(info.get("valkey_version")) == CFG["version"]
    return "valkey_version" not in info and info.get("server_name") in (None, "redis") \
        and str(info.get("redis_version")) == CFG["version"]


def layers(b) -> list:
    """The value and whatever it unwraps to through zlib, gzip, bzip2, xz/lzma and base64 (up to three layers)."""
    if not isinstance(b, (bytes, bytearray)):
        b = str(b).encode()
    seen = [bytes(b)]
    for _ in range(3):
        cur = seen[-1]
        for unpack in (zlib.decompress, gzip.decompress, bz2.decompress, lzma.decompress,
                       lambda x: base64.b64decode(x, validate=True)):
            try:
                out = unpack(cur)
            except (zlib.error, lzma.LZMAError, OSError, EOFError, binascii.Error, ValueError):
                continue
            if out and out != cur:
                seen.append(out)
                break
        else:
            break
    return seen


def ids_in(*blobs) -> set:
    """Article ids in the given values, after unwrapping compression and base64."""
    out = set()
    for bl in blobs:
        for layer in layers(bl):
            out.update(x.decode() for x in ID_RX.findall(layer))
    return out & ARTICLE_IDS


def unpack_vectors(raw: bytes) -> list:
    n = len(raw)
    if n == DIM * 4:
        return [struct.unpack(f"<{DIM}f", raw)]
    if n == DIM * 8:
        return [struct.unpack(f"<{DIM}d", raw)]
    if n == DIM * 2:  # FLOAT16, or BFLOAT16 (the upper half of a FLOAT32)
        return [struct.unpack(f"<{DIM}e", raw),
                struct.unpack(f"<{DIM}f", b"".join(b"\0\0" + raw[i:i + 2] for i in range(0, n, 2)))]
    if n == DIM:      # INT8 or UINT8
        return [struct.unpack(f"<{DIM}b", raw), struct.unpack(f"<{DIM}B", raw)]
    return []


def json_arrays(o) -> list:
    if isinstance(o, list):
        if len(o) == DIM and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in o):
            return [o]
        return [a for x in o for a in json_arrays(x)]
    if isinstance(o, dict):
        return [a for x in o.values() for a in json_arrays(x)]
    return []


def vectors_in(*blobs) -> list:
    """Vectors of the articles' dimension held in the given values: raw bytes or JSON arrays."""
    out = []
    for bl in blobs:
        for layer in layers(bl):
            out += unpack_vectors(layer)
            if layer[:1] in (b"[", b"{"):
                try:
                    out += json_arrays(json.loads(layer))
                except ValueError:
                    pass
    return out


def cos(a, b) -> float:
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(x * x for x in b))
    if not (na > 0 and nb > 0 and math.isfinite(na) and math.isfinite(nb)):
        return 0.0
    return sum(x * y for x, y in zip(a, b)) / (na * nb)


def closest(v) -> str:
    return max(ARTICLES, key=lambda aid: cos(v, ARTICLES[aid]))


def matched(ids, vecs) -> set:
    """The articles, among the ids given, whose own embedding is among the vectors given."""
    if len(ids) > RECORD_MAX_IDS:
        return set()
    return {aid for aid in ids if any(cos(v, ARTICLES[aid]) >= SAME_VECTOR for v in vecs)}


def holds_embedding_of(vecs, aid) -> bool:
    """One of the vectors is this article's embedding: close to it, and closer to it than to any other article."""
    return any(cos(v, ARTICLES[aid]) >= SAME_VECTOR and closest(v) == aid for v in vecs)


def vemb(r, k, m) -> list:
    try:
        v = [float(x) for x in (r.execute_command("VEMB", k, m) or [])]
    except Exception:  # noqa: BLE001
        return []
    return [v] if len(v) == DIM else []


def keys(r):
    """Every key in every database: (client for its database, key, type, label)."""
    for dbname in (r.info("keyspace") or {}):
        db = int(dbname[2:])
        rd = r if db == 0 else redis.Redis(port=6379, db=db, socket_timeout=10)
        for k in list(rd.scan_iter(count=1000)):
            try:
                yield rd, k, rd.type(k), ("" if db == 0 else f"db{db}:") + k.decode(errors="replace")
            except Exception:  # noqa: BLE001
                continue


def other_blobs(r, k, t) -> list:
    if t == b"string":
        return [r.get(k) or b""]
    if t == b"list":
        return r.lrange(k, 0, -1)
    if t == b"set":
        return list(r.smembers(k))
    if t == b"zset":
        return r.zrange(k, 0, -1)
    return [r.execute_command("JSON.GET", k) or b""]  # JSON documents, under whatever type name the module uses


def numbers(values) -> list:
    """The values as numbers, or None if any is not one."""
    try:
        return [float(v) for v in values]
    except (TypeError, ValueError):
        return None


def coordinate_fields(h: dict) -> list:
    """An embedding stored one coordinate per hash field: fields named <prefix><index> (e0 to e63, or 1 to 64)."""
    groups = {}
    for f, v in h.items():
        m = re.fullmatch(rb"(.*?)(\d+)", f)
        x = numbers([v]) if m else None
        if x:
            groups.setdefault(m.group(1), {})[int(m.group(2))] = x[0]
    return [[c[i] for i in range(first, first + DIM)] for c in groups.values() for first in (0, 1)
            if all(i in c for i in range(first, first + DIM))]


def hash_view(r, k):
    """A hash's ids, vectors, and whether it is shared: it names more than RECORD_MAX_IDS articles, or holds the
    embeddings of several (a shard of articles); otherwise it is one article's record, whatever other ids it names."""
    h = r.hgetall(k)
    ids, vecs = ids_in(k, *h.keys(), *h.values()), vectors_in(*h.values()) + coordinate_fields(h)
    return h, ids, vecs, len(ids) > RECORD_MAX_IDS or len(matched(ids, vecs)) > 1


def other_view(k, t, blobs):
    """Ids and vectors of a key that is not a hash or a vector set; a list of numbers is one embedding."""
    vecs = vectors_in(*blobs)
    if t == b"list" and len(blobs) == DIM and numbers(blobs):
        vecs.append(numbers(blobs))
    return ids_in(k, *blobs), vecs


def element_view(r, k, m):
    """A vector-set element's ids, vector, and the article it is: the one whose embedding it holds, else the one id
    its name (or else its attributes) carries, for vectors the grader cannot read back (REDUCE, BIN)."""
    attrs = r.execute_command("VGETATTR", k, m) or b""
    ids, vecs = ids_in(m, attrs), vemb(r, k, m)
    own = matched(ids, vecs)
    if len(own) != 1:
        named = ids_in(m) or ids_in(attrs)
        own = named if len(named) == 1 else set()
    return ids, vecs, own


def stored_articles(r):
    """(articles stored with their own embedding, every article id seen anywhere), over every database."""
    stored, seen = set(), set()
    for rd, k, t, _ in keys(r):
        try:
            if t == b"hash":
                h, ids, vecs, shared = hash_view(rd, k)
                seen |= ids
                if shared:
                    for f, v in h.items():
                        stored |= matched(ids_in(k, f, v), vectors_in(v))
                else:
                    stored |= matched(ids, vecs)
            elif t == b"vectorset":
                for m in rd.execute_command("VRANDMEMBER", k, int(rd.execute_command("VCARD", k))) or []:
                    ids, _, own = element_view(rd, k, m)
                    seen |= ids
                    stored |= own
            else:
                blobs = other_blobs(rd, k, t)
                ids, vecs = other_view(k, t, blobs)
                seen |= ids
                found = matched(ids, vecs)
                if len(found) == 1:      # one article's record; one document holding several is not read
                    stored |= found
                if t in (b"list", b"set", b"zset"):   # a list or set of records, read member by member
                    for mem in blobs:
                        stored |= matched(ids_in(mem), vectors_in(mem))
        except Exception:  # noqa: BLE001
            continue
    return stored, seen


def remove_article(r, aid: str) -> list:
    """Remove one article from the server, as deleting it would, in every database: the record that holds its
    embedding, whatever other ids that record names; its entries in a hash shared by several articles and in lists or
    sets (of records, or of keys and ids that an index of the solution's own may walk); its vector-set element; and
    records that hold its id and no other article, or are named by it. Returns what was removed."""
    done = []

    def drop(what, label):
        try:
            what()
            done.append(label)
        except Exception:  # noqa: BLE001
            pass

    for rd, k, t, name in keys(r):
        try:
            if t == b"hash":
                h, ids, vecs, shared = hash_view(rd, k)
                if not shared and (holds_embedding_of(vecs, aid) or
                                   (not matched(ids, vecs) and (ids == {aid} or aid in ids_in(k)))):
                    drop(lambda: rd.delete(k), name)
                elif shared or (aid in ids and not matched(ids, vecs)):
                    # a hash shared by several articles, or a map of ids holding no embedding: only this article's
                    # entries (another article's record that merely names this one is left alone)
                    for f, v in h.items():
                        fv = vectors_in(v)
                        if holds_embedding_of(fv, aid) or (not matched(ids_in(k, f, v), fv) and ids_in(f, v) == {aid}):
                            drop(lambda f=f: rd.hdel(k, f), f"{name}[{f.decode(errors='replace')}]")
            elif t == b"vectorset":
                for m in rd.execute_command("VRANDMEMBER", k, int(rd.execute_command("VCARD", k))) or []:
                    _, vecs, own = element_view(rd, k, m)
                    if own == {aid} or holds_embedding_of(vecs, aid):
                        drop(lambda m=m: rd.execute_command("VREM", k, m), f"{name}[{m.decode(errors='replace')}]")
            else:
                blobs = other_blobs(rd, k, t)
                ids, vecs = other_view(k, t, blobs)
                found = matched(ids, vecs)
                single = len(ids) <= RECORD_MAX_IDS and len(found) <= 1
                if single and (holds_embedding_of(vecs, aid) or (not found and (ids == {aid} or aid in ids_in(k)))):
                    drop(lambda: rd.delete(k), name)
                elif t in (b"list", b"set", b"zset"):   # a list or set of articles or of their keys: its members
                    rem = {b"list": lambda x: rd.lrem(k, 0, x), b"set": lambda x: rd.srem(k, x),
                           b"zset": lambda x: rd.zrem(k, x)}[t]
                    for mem in blobs:
                        if ids_in(mem) == {aid} or holds_embedding_of(vectors_in(mem), aid):
                            drop(lambda mem=mem: rem(mem), f"{name}[{mem.decode(errors='replace')[:40]}]")
        except Exception:  # noqa: BLE001
            continue
    return done


def run_query(qvec, i: int) -> dict:
    qpath = f"/var/tmp/.kvg-q{i}.json"
    with open(qpath, "w") as f:
        json.dump(qvec, f)
    try:
        with open(qpath) as fin:
            p = subprocess.run(["python3", "/app/search.py"], stdin=fin, capture_output=True, text=True,
                               timeout=120, cwd="/root")
    except subprocess.TimeoutExpired:
        return {"error": "timeout"}
    ids = []
    for line in p.stdout.splitlines():
        found = re.findall(r"kb-\d{4}", line)
        if len(found) == 1:
            ids.append(found[0])
    return {"ids": ids[:5], "printed": len(ids), "exit": p.returncode, "stderr": p.stderr.strip()[-200:]}


def counters(r):
    calls = sum(v.get("calls", 0) for k, v in r.info("commandstats").items()
                if k.lower().split("cmdstat_", 1)[-1] in SEARCH_COMMANDS)
    return calls, int(r.info("stats")["total_net_output_bytes"])


def top5_without(qv: list, skip: str) -> list:
    return sorted((aid for aid in ARTICLES if aid != skip), key=lambda aid: -cos(qv, ARTICLES[aid]))[:5]


DATA = "/data/articles.jsonl"
ARTICLES = {}
for line in open(DATA):
    a = json.loads(line)
    ARTICLES[a["id"]] = a["embedding"]
ARTICLE_IDS = set(ARTICLES)
DIM = len(next(iter(ARTICLES.values())))

try:
    r = redis.Redis(port=6379, socket_timeout=10)
    if not identity(r):
        reasons.append(f"server on 6379 is not the provided {CFG['product']} {CFG['version']}: {facts.get('server')}")
    facts["dbsize"] = r.dbsize()
    stored, seen = stored_articles(r)
    facts["articles_found"] = len(stored)
    if len(stored) < len(ARTICLES):
        only_id = sorted(seen - stored)
        reasons.append(f"only {len(stored)} of the {len(ARTICLES)} supplied articles are stored in the server with "
                       f"their own embedding" + (f"; {len(only_id)} more appear only as an id ({', '.join(only_id[:3])})"
                                                 if only_id else ""))
except Exception as e:  # noqa: BLE001
    reasons.append(f"server on 6379 unreachable: {type(e).__name__}: {e}")
    finish()

if not os.path.exists("/app/search.py"):
    reasons.append("/app/search.py does not exist")
    finish()

HIDDEN = f"/var/tmp/.kvg-{uuid.uuid4().hex}"
os.rename(DATA, HIDDEN)
results = []
try:
    _, b0 = counters(r)
    _, b1 = counters(r)
    overhead = b1 - b0          # the counters' own replies, subtracted from each measurement
    for i, q in enumerate(CFG["queries"]):
        cb, bb = counters(r)
        res = run_query(q, i)
        ca, ba = counters(r)
        res["search_calls"] = ca - cb
        res["bytes"] = max(0, ba - bb - overhead)
        if "ids" in res:
            truth = CFG["top5"][i]
            res.update(top1_ok=bool(res["ids"]) and res["ids"][0] == truth[0],
                       overlap=len(set(res["ids"]) & set(truth)), exact=res["ids"] == truth)
        results.append(res)
    # every article is searchable, not only stored: probe articles spread over the whole set (the first, the last, every
    # hundredth), each queried with its own embedding, must come back first (no two articles are closer than 0.78)
    probes = [aid for aid in ["kb-0001"] + [f"kb-{n:04d}" for n in range(100, 1001, 100)] if aid in ARTICLES]
    missed = []
    for j, aid in enumerate(probes):
        res = run_query(ARTICLES[aid], f"p{j}")
        if res.get("error") or res.get("exit") != 0 or not res.get("ids") or res["ids"][0] != aid:
            missed.append(aid)
    facts["coverage_probes"] = {"probed": probes, "not_first": missed}
    if missed:
        reasons.append(f"{len(missed)} of {len(probes)} articles queried with their own embedding did not come back "
                       f"first ({', '.join(missed[:3])}): stored, but not all of them can be found by the search")
    # the removal check
    target = CFG["top5"][0][0]
    expected_after = top5_without(CFG["queries"][0], target)
    facts["removal_check"] = {"article": target, "expected_after": expected_after}
    removed = remove_article(r, target)
    facts["removal_check"]["removed"] = removed
    if not removed:
        reasons.append(f"could not find {target} in the server (no record holds its embedding or only its id, no "
                       f"vector-set element is named by it), so it cannot be checked that the answers come from the server")
    else:
        time.sleep(1.5)  # an index may update asynchronously
        again = run_query(CFG["queries"][0], 0)
        facts["removal_check"]["after"] = again
        got = again.get("ids", [])
        if again.get("error") or again.get("exit") != 0:
            reasons.append(f"after {target} was removed from the server, search.py failed "
                           f"({again.get('error') or 'exit ' + str(again.get('exit'))})")
        elif target in got:
            reasons.append(f"after {target}, ranked first for query 1, was removed from the server, search.py still "
                           f"returned it: the answer does not come from the server's data")
        elif again.get("printed") != 5:
            reasons.append(f"after {target} was removed from the server, query 1 printed {again.get('printed')} "
                           f"article ids, expected 5")
        elif not got or got[0] != expected_after[0] or len(set(got) & set(expected_after)) < 4:
            reasons.append(f"after {target} was removed from the server, query 1 returned {got}, expected the new "
                           f"top 5 {expected_after}")
finally:
    os.rename(HIDDEN, DATA)

facts["queries"] = results
for i, res in enumerate(results):
    if res.get("error"):
        reasons.append(f"query {i + 1}: {res['error']}")
        continue
    if res["exit"] != 0:
        reasons.append(f"query {i + 1}: search.py exited {res['exit']}: {res['stderr']!r}")
    elif res["printed"] != 5:
        reasons.append(f"query {i + 1}: printed {res['printed']} article ids, one per line; the job asks for 5")
    elif not res["exact"]:
        reasons.append(f"query {i + 1}: returned {res['ids']}, expected {CFG['top5'][i]} in this order "
                       f"(top-1 {'ok' if res['top1_ok'] else 'wrong'}, {res['overlap']}/5 overlap)")
    if res["search_calls"] < 1:
        reasons.append(f"query {i + 1}: no search command (FT.SEARCH, FT.AGGREGATE, FT.HYBRID, VSIM) or server-side "
                       f"script (EVAL, FCALL) ran in the server")
    if res["bytes"] > MAX_BYTES_PER_QUERY:
        reasons.append(f"query {i + 1}: the server sent search.py {res['bytes'] // 1024} KB (a search reply is a few KB): "
                       f"the similarity is computed outside the server")
finish()
