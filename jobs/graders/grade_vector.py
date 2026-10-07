# Grader: vector job, v2. Runs INSIDE the task container after the agent stops (grader's own Python).
# The job file prepends `CFG = {...}` (product, version, the 5 held-out queries and their exact cosine top-5);
# the agent never sees the queries or the answers.
#
# What "done" means (from the prompt): all 1,000 articles are loaded into the server; `python3 /app/search.py <
# query.json` prints the ids of the 5 most similar articles, most similar first; the similarity search runs INSIDE
# the server; and the articles and everything search.py needs are left in the server.
#   - loaded: at least 1,000 distinct article ids (kb-NNNN) are stored in the server, in key names, hash fields or
#     values, JSON documents, strings, or vector-set elements. Counting ids, not index sizes, so two indexes of 500
#     pass and an unrelated index of 1,000 does not (v2; v1 did not count)
#   - correct: the true top 5, in order, for every query (v2; v1 took top-1 exact and >= 4 of 5)
#   - inside the server, with the article file moved away (v2; v1 only looked for a search command):
#       1. MONITOR sees a search command (FT.SEARCH, FT.AGGREGATE or VSIM) while search.py runs;
#       2. the server sends search.py at most 100 KB per query. A KNN reply is a few KB; computing the similarity
#          outside the server needs every vector, over 256 KB, however it is fetched (MGET, one large value,
#          SCAN and HGET, a script);
#       3. after the article ranked first for query 1 is removed from the server, search.py no longer returns it.
#          A copy of the vectors kept outside the server would still return it.
import json
import os
import re
import subprocess
import sys
import time
import uuid

import redis

facts: dict = {}
reasons: list = []
MAX_BYTES_PER_QUERY = 100_000
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


def article_ids_in_server(r) -> set:
    found = set()
    for k in r.scan_iter(count=1000):
        found.update(ID_RX.findall(k))
        try:
            t = r.type(k)
            if t == b"hash":
                for f, v in r.hgetall(k).items():
                    found.update(ID_RX.findall(f))
                    found.update(ID_RX.findall(v))
            elif t == b"string":
                found.update(ID_RX.findall(r.get(k) or b""))
            elif t == b"ReJSON-RL":
                v = r.execute_command("JSON.GET", k) or b""
                found.update(ID_RX.findall(v if isinstance(v, bytes) else str(v).encode()))
            elif t == b"vectorset":
                n = int(r.execute_command("VCARD", k))
                for m in r.execute_command("VRANDMEMBER", k, n) or []:
                    found.update(ID_RX.findall(m if isinstance(m, bytes) else str(m).encode()))
            elif t == b"list":
                for m in r.lrange(k, 0, -1):
                    found.update(ID_RX.findall(m))
            elif t == b"set":
                for m in r.smembers(k):
                    found.update(ID_RX.findall(m))
            elif t == b"zset":
                for m in r.zrange(k, 0, -1):
                    found.update(ID_RX.findall(m))
        except Exception:  # noqa: BLE001
            pass
    return {x.decode() for x in found}


def remove_article(r, aid: str) -> list:
    """Remove one article wherever it is addressed by its id: a key whose name holds the id, a hash field named by
    it, or a vector-set element. Returns what was removed; empty if the article could not be located."""
    done, b = [], aid.encode()
    for k in list(r.scan_iter(count=1000)):
        name = k.decode(errors="replace")
        if b in k:
            r.delete(k)
            done.append(f"key {name}")
            continue
        t = r.type(k)
        if t == b"hash" and r.hexists(k, b):
            r.hdel(k, b)
            done.append(f"hash field {name}[{aid}]")
        elif t == b"vectorset":
            try:
                if int(r.execute_command("VREM", k, aid)):
                    done.append(f"vector-set element {name}[{aid}]")
            except Exception:  # noqa: BLE001
                pass
    return done


def run_query(i: int) -> dict:
    qpath = f"/var/tmp/.kvg-q{i}.json"
    with open(qpath, "w") as f:
        json.dump(CFG["queries"][i], f)
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


try:
    r = redis.Redis(port=6379, socket_timeout=10)
    if not identity(r):
        reasons.append(f"server on 6379 is not the provided {CFG['product']} {CFG['version']}: {facts.get('server')}")
    facts["dbsize"] = r.dbsize()
    ids_found = article_ids_in_server(r)
    facts["articles_found"] = len(ids_found)
    if len(ids_found) < 1000:
        reasons.append(f"only {len(ids_found)} of the 1,000 articles are stored in the server")
except Exception as e:  # noqa: BLE001
    reasons.append(f"server on 6379 unreachable: {type(e).__name__}: {e}")
    finish()

if not os.path.exists("/app/search.py"):
    reasons.append("/app/search.py does not exist")
    finish()

DATA = "/data/articles.jsonl"
HIDDEN = f"/var/tmp/.kvg-{uuid.uuid4().hex}"
moved = False
if os.path.exists(DATA):
    os.rename(DATA, HIDDEN)
    moved = True
results, per_query_bytes = [], []
monfile = f"/var/tmp/.kvg-mon-{uuid.uuid4().hex}"
try:
    # pass 1: the answers, with MONITOR watching for a search command
    mon = subprocess.Popen([CFG["cli"], "-p", "6379", "monitor"], stdout=open(monfile, "wb"), stderr=subprocess.DEVNULL)
    time.sleep(0.7)
    try:
        for i in range(len(CFG["queries"])):
            res = run_query(i)
            if "ids" in res:
                truth = CFG["top5"][i]
                res.update(top1_ok=bool(res["ids"]) and res["ids"][0] == truth[0],
                           overlap=len(set(res["ids"]) & set(truth)), exact=res["ids"] == truth)
            results.append(res)
    finally:
        time.sleep(0.3)
        mon.terminate()
        try:
            mon.wait(5)
        except Exception:  # noqa: BLE001
            mon.kill()
    # pass 2: what the server sends search.py per query, with no MONITOR client counted
    def net_out() -> int:
        return int(r.info("stats")["total_net_output_bytes"])
    a = net_out()
    info_reply = net_out() - a   # one INFO reply, subtracted from each measurement
    for i in range(len(CFG["queries"])):
        before = net_out()
        run_query(i)
        per_query_bytes.append(max(0, net_out() - before - info_reply))
    # pass 3: remove the article ranked first for query 1, then ask again
    target = CFG["top5"][0][0]
    removed = remove_article(r, target)
    facts["removal_check"] = {"article": target, "removed": removed}
    if removed:
        time.sleep(1.5)  # an index may update asynchronously
        again = run_query(0)
        facts["removal_check"]["after"] = again
        if again.get("exit") == 0 and target in again.get("ids", []):
            reasons.append(f"after {target}, ranked first for query 1, was removed from the server, search.py still "
                           f"returned it: the answer does not come from the server's data")
    else:
        facts["removal_check"]["skipped"] = "the article could not be located by its id in the server"
finally:
    if moved:
        os.rename(HIDDEN, DATA)

mon_text = open(monfile, "rb").read().decode("utf-8", "replace")
cmds = sorted({m.upper() for m in re.findall(r'"((?i:ft\.search|ft\.aggregate|vsim))"', mon_text)})
facts["server_side_search_commands"] = cmds
facts["queries"] = results
facts["bytes_per_query"] = per_query_bytes
if not cmds:
    reasons.append("no similarity search ran inside the server while search.py ran (no FT.SEARCH / FT.AGGREGATE / VSIM seen)")
if per_query_bytes and max(per_query_bytes) > MAX_BYTES_PER_QUERY:
    reasons.append(f"the server sent search.py {max(per_query_bytes) // 1024} KB for one query (a search reply is a few KB): "
                   f"the similarity is computed outside the server")
for i, res in enumerate(results):
    if res.get("error"):
        reasons.append(f"query {i + 1}: {res['error']}")
    elif res["exit"] != 0:
        reasons.append(f"query {i + 1}: search.py exited {res['exit']}: {res['stderr']!r}")
    elif not res["exact"]:
        reasons.append(f"query {i + 1}: returned {res['ids']}, expected {CFG['top5'][i]} in this order "
                       f"(top-1 {'ok' if res['top1_ok'] else 'wrong'}, {res['overlap']}/5 overlap)")
finish()
