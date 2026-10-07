# Grader: vector job, v2 (third revision, 2026-10-07). Runs INSIDE the task container after the agent stops, with
# the grader's own Python. The job file prepends `CFG = {...}` (product, version, the 5 held-out queries and their
# exact cosine top-5); the agent never sees the queries or the answers.
#
# What "done" means (from the prompt): all 1,000 articles are loaded into the server; `python3 /app/search.py <
# query.json` prints the ids of the 5 most similar articles, most similar first; the similarity search runs INSIDE
# the server; and the articles and everything search.py needs are left in the server.
#   - loaded: every one of the 1,000 supplied article ids (read from the article file, not guessed from a pattern)
#     is stored in the server: in key names, hash fields or values, JSON documents, strings, vector-set elements or
#     their attributes, plain or compressed or base64. Two indexes of 500 pass; ids not in the file do not count.
#   - correct: the true top 5, in order, for every query.
#   - inside the server, with the article file moved away, measured on the same run that is scored:
#       1. the server's own command counters show a search command (FT.SEARCH, FT.AGGREGATE, FT.HYBRID or VSIM)
#          for every query;
#       2. the server sends search.py at most 100 KB per query. A heuristic: a search reply is a few KB, while
#          computing the similarity outside the server from vectors read back needs all of them, over 256 KB;
#       3. the article ranked first for query 1 is removed from the server, found by its id: a key named by it, a
#          record that holds that id and no other, a vector-set element named by it or carrying it in its
#          attributes, or its entries in a hash shared by many articles. search.py must then run cleanly and return
#          the true top 5 of the remaining articles, computed here. An answer computed from a local copy keeps
#          returning the removed article. If the article cannot be found by its id, the check cannot run and the
#          job fails with that reason.
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


def layers(b) -> bytes:
    """The value and whatever it unwraps to through zlib, gzip and base64 (up to three layers), joined."""
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
    return b"\n".join(seen)


def ids_in(*blobs) -> set:
    """Article ids in the given values, after unwrapping compression and base64."""
    out = set()
    for bl in blobs:
        out.update(x.decode() for x in ID_RX.findall(layers(bl)))
    return out


def blobs_of(r, k, t) -> list:
    """Everything one key holds, as raw values."""
    if t == b"hash":
        h = r.hgetall(k)
        return [*h.keys(), *h.values()]
    if t == b"string":
        return [r.get(k) or b""]
    if t == b"vectorset":
        members = r.execute_command("VRANDMEMBER", k, int(r.execute_command("VCARD", k))) or []
        return [*members, *(r.execute_command("VGETATTR", k, m) or b"" for m in members)]
    if t == b"list":
        return r.lrange(k, 0, -1)
    if t == b"set":
        return list(r.smembers(k))
    if t == b"zset":
        return r.zrange(k, 0, -1)
    return [r.execute_command("JSON.GET", k) or b""]  # JSON documents, under whatever type name the module uses


def stored_ids(r) -> set:
    found = set()
    for k in r.scan_iter(count=1000):
        found |= ids_in(k)
        try:
            found |= ids_in(*blobs_of(r, k, r.type(k)))
        except Exception:  # noqa: BLE001
            pass
    return found


def remove_article(r, aid: str) -> list:
    """Remove one article from the server: keys named by its id, records that hold its id and no other, vector-set
    elements named by it or carrying it in their attributes, and its entries in hashes shared by many articles.
    Returns what was removed."""
    done = []
    for k in list(r.scan_iter(count=1000)):
        name = k.decode(errors="replace")
        try:
            t = r.type(k)
            if aid in ids_in(k):
                r.delete(k)
                done.append(f"key {name}")
            elif t == b"vectorset":
                for m in r.execute_command("VRANDMEMBER", k, int(r.execute_command("VCARD", k))) or []:
                    if ids_in(m, r.execute_command("VGETATTR", k, m) or b"") == {aid}:
                        r.execute_command("VREM", k, m)
                        done.append(f"vector-set element {name}[{m.decode(errors='replace')}]")
            else:
                held = ids_in(*blobs_of(r, k, t))
                if held == {aid}:                       # the article's own record
                    r.delete(k)
                    done.append(f"key {name}, which holds {aid}")
                elif aid in held and t == b"hash":      # a hash shared by many articles: only its entries
                    for f, v in r.hgetall(k).items():
                        if aid in ids_in(f, v):
                            r.hdel(k, f)
                            done.append(f"hash field {name}[{f.decode(errors='replace')}]")
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
    return {"ids": ids[:5], "exit": p.returncode, "stderr": p.stderr.strip()[-200:]}


def counters(r):
    calls = sum(v.get("calls", 0) for k, v in r.info("commandstats").items()
                if k.lower().split("cmdstat_", 1)[-1] in SEARCH_COMMANDS)
    return calls, int(r.info("stats")["total_net_output_bytes"])


def top5_without(articles: dict, qv: list, skip: str) -> list:
    qn = math.sqrt(sum(x * x for x in qv))
    scored = []
    for aid, (vec, norm) in articles.items():
        if aid != skip:
            scored.append((sum(a * b for a, b in zip(qv, vec)) / (qn * norm), aid))
    scored.sort(reverse=True)
    return [aid for _, aid in scored[:5]]


DATA = "/data/articles.jsonl"
articles = {}
for line in open(DATA):
    a = json.loads(line)
    articles[a["id"]] = (a["embedding"], math.sqrt(sum(x * x for x in a["embedding"])))

try:
    r = redis.Redis(port=6379, socket_timeout=10)
    if not identity(r):
        reasons.append(f"server on 6379 is not the provided {CFG['product']} {CFG['version']}: {facts.get('server')}")
    facts["dbsize"] = r.dbsize()
    present = stored_ids(r) & set(articles)
    facts["articles_found"] = len(present)
    if len(present) < len(articles):
        reasons.append(f"only {len(present)} of the {len(articles)} supplied articles are stored in the server")
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
    expected_after = top5_without(articles, CFG["queries"][0], target)
    facts["removal_check"] = {"article": target, "expected_after": expected_after}
    removed = remove_article(r, target)
    facts["removal_check"]["removed"] = removed
    if not removed:
        reasons.append(f"could not find {target} in the server by its id (key names, records holding the id, "
                       f"vector-set elements, shared hashes), so it cannot be checked that the answers come from the server")
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
    elif not res["exact"]:
        reasons.append(f"query {i + 1}: returned {res['ids']}, expected {CFG['top5'][i]} in this order "
                       f"(top-1 {'ok' if res['top1_ok'] else 'wrong'}, {res['overlap']}/5 overlap)")
    if res["search_calls"] < 1:
        reasons.append(f"query {i + 1}: no search command (FT.SEARCH, FT.AGGREGATE, FT.HYBRID, VSIM) ran in the server")
    if res["bytes"] > MAX_BYTES_PER_QUERY:
        reasons.append(f"query {i + 1}: the server sent search.py {res['bytes'] // 1024} KB (a search reply is a few KB): "
                       f"the similarity is computed outside the server")
finish()
