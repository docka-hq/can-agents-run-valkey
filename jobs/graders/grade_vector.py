# Grader: vector job. Runs INSIDE the task container after the agent stops (grader's own Python).
# make_tasks.py prepends `CFG = {...}` (product, version, the 5 held-out queries and their exact
# cosine top-5) and inlines this file; the agent never sees the queries or the answers.
#
# What "done" means (from the prompt): `python3 /app/search.py < query.json` prints the 5 most similar
# article ids, most similar first, and the similarity search runs INSIDE the server.
#   - correctness: the true top-5, in order, for every query (v2; v1 took top-1 exact and >= 4 of 5.
#     Queries were generated with a >= 0.10 top-1 margin and >= 0.02 at rank 5/6)
#   - all 1,000 articles are searchable in the server: an index with >= 1,000 documents or a vector set
#     with >= 1,000 elements (v2; v1 did not count)
#   - inside the server: MONITOR sees FT.SEARCH / FT.AGGREGATE / VSIM while search.py runs, search.py
#     does not scan the keyspace or read more than 50 items one by one (v2: so the answer cannot be
#     computed outside the server from data read back), and the article file is moved away during
#     grading so it cannot be read
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


try:
    r = redis.Redis(port=6379, socket_timeout=10)
    if not identity(r):
        reasons.append(f"server on 6379 is not the provided {CFG['product']} {CFG['version']}: {facts.get('server')}")
    facts["dbsize"] = r.dbsize()
    searchable = {}
    try:
        for name in r.execute_command("FT._LIST") or []:
            name = name.decode() if isinstance(name, bytes) else str(name)
            info = r.execute_command("FT.INFO", name)
            if isinstance(info, dict):
                d = {(k.decode() if isinstance(k, bytes) else str(k)): v for k, v in info.items()}
            else:
                d = {(info[j].decode() if isinstance(info[j], bytes) else str(info[j])): info[j + 1]
                     for j in range(0, len(info) - 1, 2)}
            nd = d.get("num_docs", 0)
            searchable[f"index {name}"] = int(float(nd.decode() if isinstance(nd, bytes) else nd or 0))
    except Exception as e:  # noqa: BLE001
        searchable["FT._LIST"] = f"{type(e).__name__}"
    for k in r.scan_iter(count=1000):
        try:
            if r.type(k) == b"vectorset":
                searchable[f"vector set {k.decode(errors='replace')}"] = int(r.execute_command("VCARD", k))
        except Exception:  # noqa: BLE001
            pass
    facts["searchable"] = searchable
    if max([v for v in searchable.values() if isinstance(v, int)] or [0]) < 1000:
        reasons.append(f"not all 1,000 articles are searchable in the server ({searchable or 'no index or vector set'})")
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
monfile = f"/var/tmp/.kvg-mon-{uuid.uuid4().hex}"
mon = subprocess.Popen([CFG["cli"], "-p", "6379", "monitor"], stdout=open(monfile, "wb"), stderr=subprocess.DEVNULL)
time.sleep(0.7)
results = []
try:
    for i, q in enumerate(CFG["queries"]):
        qpath = f"/var/tmp/.kvg-q{i}.json"
        with open(qpath, "w") as f:
            json.dump(q, f)
        try:
            with open(qpath) as fin:
                p = subprocess.run(["python3", "/app/search.py"], stdin=fin, capture_output=True, text=True,
                                   timeout=120, cwd="/root")
        except subprocess.TimeoutExpired:
            results.append({"error": "timeout"})
            continue
        ids = []
        for line in p.stdout.splitlines():
            found = re.findall(r"kb-\d{4}", line)
            if len(found) == 1:
                ids.append(found[0])
        ids = ids[:5]
        truth = CFG["top5"][i]
        results.append({"ids": ids, "exit": p.returncode, "top1_ok": bool(ids) and ids[0] == truth[0],
                        "overlap": len(set(ids) & set(truth)), "exact": ids == truth, "stderr": p.stderr.strip()[-200:]})
finally:
    time.sleep(0.3)
    mon.terminate()
    try:
        mon.wait(5)
    except Exception:  # noqa: BLE001
        mon.kill()
    if moved:
        os.rename(HIDDEN, DATA)

mon_text = open(monfile, "rb").read().decode("utf-8", "replace")
cmds = sorted({m.upper() for m in re.findall(r'"((?i:ft\.search|ft\.aggregate|vsim))"', mon_text)})
facts["server_side_search_commands"] = cmds
facts["queries"] = results
if not cmds:
    reasons.append("no similarity search ran inside the server while search.py ran (no FT.SEARCH / FT.AGGREGATE / VSIM seen)")
called = [c.upper() for c in re.findall(r'\] "([A-Za-z_.]+)"', mon_text)]
scans = sorted({c for c in called if c in ("SCAN", "KEYS", "HSCAN", "SSCAN", "ZSCAN")})
item_reads = sum(1 for c in called if c in ("GET", "MGET", "HGET", "HGETALL", "HMGET", "JSON.GET", "JSON.MGET",
                                             "VEMB", "VGETATTR", "LRANGE", "SMEMBERS", "ZRANGE", "DUMP"))
facts["search_py_reads"] = {"scans": scans, "item_reads": item_reads}
if scans:
    reasons.append(f"search.py scanned the keyspace while answering ({', '.join(scans)}): the answer may not come from the server's search")
if item_reads > 50:
    reasons.append(f"search.py read {item_reads} items one by one while answering 5 queries: the similarity may be computed outside the server")
for i, res in enumerate(results):
    if res.get("error"):
        reasons.append(f"query {i + 1}: {res['error']}")
    elif res["exit"] != 0:
        reasons.append(f"query {i + 1}: search.py exited {res['exit']}: {res['stderr']!r}")
    elif not res["exact"]:
        reasons.append(f"query {i + 1}: returned {res['ids']}, expected {CFG['top5'][i]} in this order "
                       f"(top-1 {'ok' if res['top1_ok'] else 'wrong'}, {res['overlap']}/5 overlap)")
finish()
