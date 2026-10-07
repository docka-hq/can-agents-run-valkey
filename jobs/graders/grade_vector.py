# Grader: vector job, v2 (fourth revision, 2026-10-07). Runs INSIDE the task container after the agent stops, with
# the grader's own Python. The job file prepends `CFG = {...}` (product, version, the 5 held-out queries and their
# exact cosine top-5); the agent never sees the queries or the answers.
#
# What "done" means (from the prompt): all 1,000 articles are loaded into the server; `python3 /app/search.py <
# query.json` prints the ids of the 5 most similar articles, one per line, most similar first; the similarity search
# runs INSIDE the server; and the articles and everything search.py needs are left in the server.
#   - loaded: every one of the 1,000 articles in the article file is stored in the server WITH ITS OWN EMBEDDING: a
#     record (a hash, JSON document or string, or one entry of a hash shared by many articles) that holds the
#     article's id and its embedding (FLOAT32, FLOAT64, FLOAT16, BFLOAT16, INT8 or UINT8 bytes, or a JSON array;
#     plain, compressed or base64), or a vector-set element named by the id or carrying it in its attributes. An id
#     stored without its embedding does not count, nor does an id not in the file. Two indexes of 500 pass.
#   - correct: exactly 5 ids, the true top 5 in order, for every query.
#   - inside the server, with the article file moved away, measured on the same run that is scored:
#       1. the server's own command counters show a search command (FT.SEARCH, FT.AGGREGATE, FT.HYBRID or VSIM)
#          for every query;
#       2. the server sends search.py at most 100 KB per query. A heuristic: a search reply is a few KB, while
#          computing the similarity outside the server from vectors read back needs all of them, over 256 KB;
#       3. the article ranked first for query 1 is removed from the server: the record holding its embedding (the
#          whole record, whatever other ids it mentions), the entry holding it in a shared hash, its vector-set
#          element, and any record holding its id and no other. search.py must then run cleanly and print the true
#          top 5 of the remaining articles, computed here. An answer computed from a local copy keeps returning the
#          removed article. If nothing in the server holds the article, the check cannot run and the job fails.
# Not detectable here, and documented as limits in the README: a search.py that fetches a few hundred candidates
# from a server-side search and re-ranks them itself, or that computes the answer from a local copy and asks the
# server only which articles still exist.
import base64
import binascii
import gzip
import json
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
SEARCH_COMMANDS = ("ft.search", "ft.aggregate", "ft.hybrid", "ft.profile", "vsim")
ID_RX = re.compile(rb"kb-\d{4}")
SAME_VECTOR = 0.99      # cosine at or above which a stored vector is taken to be an article's embedding
RECORD_MAX_FIELDS = 50  # a hash with more fields is a map shared by many articles, read field by field
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
    """The value and whatever it unwraps to through zlib, gzip and base64 (up to three layers)."""
    if not isinstance(b, (bytes, bytearray)):
        b = str(b).encode()
    seen = [bytes(b)]
    for _ in range(3):
        cur = seen[-1]
        for unpack in (zlib.decompress, gzip.decompress, lambda x: base64.b64decode(x, validate=True)):
            try:
                out = unpack(cur)
            except (zlib.error, OSError, EOFError, binascii.Error, ValueError):
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


def units(r):
    """The server's contents in units of one article at most. Each unit is (label, ids, vectors, own, remove): the
    article ids it mentions, the vectors it holds, the article it is when that is known from its form (a vector-set
    element), and how to remove it. A hash of up to RECORD_MAX_FIELDS fields is one record; a bigger hash is a map
    shared by many articles, one unit per field; a vector set is one unit per element; any other key is one unit."""
    for k in list(r.scan_iter(count=1000)):
        name = k.decode(errors="replace")
        try:
            t = r.type(k)
            if t == b"hash":
                h = r.hgetall(k)
                if len(h) <= RECORD_MAX_FIELDS:
                    yield name, ids_in(k, *h.keys(), *h.values()), vectors_in(*h.values()), None, \
                        (lambda k=k: r.delete(k))
                else:
                    for f, v in h.items():
                        yield f"{name}[{f.decode(errors='replace')}]", ids_in(k, f, v), vectors_in(v), None, \
                            (lambda k=k, f=f: r.hdel(k, f))
            elif t == b"vectorset":
                for m in r.execute_command("VRANDMEMBER", k, int(r.execute_command("VCARD", k))) or []:
                    attrs = r.execute_command("VGETATTR", k, m) or b""
                    own = ids_in(m) or ids_in(attrs)
                    yield f"{name}[{m.decode(errors='replace')}]", ids_in(m, attrs), [], \
                        (next(iter(own)) if len(own) == 1 else None), \
                        (lambda k=k, m=m: r.execute_command("VREM", k, m))
            else:
                if t == b"string":
                    blobs = [r.get(k) or b""]
                elif t == b"list":
                    blobs = r.lrange(k, 0, -1)
                elif t == b"set":
                    blobs = list(r.smembers(k))
                elif t == b"zset":
                    blobs = r.zrange(k, 0, -1)
                else:  # JSON documents, under whatever type name the module uses
                    blobs = [r.execute_command("JSON.GET", k) or b""]
                yield name, ids_in(k, *blobs), vectors_in(*blobs), None, (lambda k=k: r.delete(k))
        except Exception:  # noqa: BLE001
            continue


def own_articles(ids, vecs, own) -> set:
    """The articles a unit stores with their own embedding."""
    if own:
        return {own}
    if len(ids) > RECORD_MAX_IDS:
        return set()
    return {aid for aid in ids if any(cos(v, ARTICLES[aid]) >= SAME_VECTOR for v in vecs)}


def stored_articles(r):
    """(articles stored with their own embedding, every article id seen anywhere)."""
    stored, seen = set(), set()
    for _, ids, vecs, own, _ in units(r):
        seen |= ids
        stored |= own_articles(ids, vecs, own)
    return stored, seen


def remove_article(r, aid: str) -> list:
    """Remove one article from the server. Returns what was removed."""
    done, target = [], ARTICLES[aid]
    for label, ids, vecs, own, remove in units(r):
        if own is not None:
            hit = own == aid
        else:
            hit = ids == {aid} or (len(ids) <= RECORD_MAX_IDS and any(
                cos(v, target) >= SAME_VECTOR and closest(v) == aid for v in vecs))
        if hit:
            try:
                remove()
                done.append(label)
            except Exception:  # noqa: BLE001
                pass
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
        reasons.append(f"query {i + 1}: no search command (FT.SEARCH, FT.AGGREGATE, FT.HYBRID, VSIM) ran in the server")
    if res["bytes"] > MAX_BYTES_PER_QUERY:
        reasons.append(f"query {i + 1}: the server sent search.py {res['bytes'] // 1024} KB (a search reply is a few KB): "
                       f"the similarity is computed outside the server")
finish()
