# Grader: vector job. Runs INSIDE the task container after the agent stops (grader's own Python).
# make_tasks.py prepends `CFG = {...}` (product, version, the 5 held-out queries and their exact
# cosine top-5) and inlines this file; the agent never sees the queries or the answers.
#
# What "done" means (from the prompt): `python3 /app/search.py < query.json` prints the 5 most similar
# article ids, most similar first, and the similarity search runs INSIDE the server.
#   - correctness: top-1 exact and >= 4 of the true top-5 for every query (an approximate index may
#     swap the tail; queries were generated with a >= 0.10 top-1 margin and >= 0.02 at rank 5/6)
#   - inside the server: MONITOR sees FT.SEARCH / FT.AGGREGATE / VSIM while search.py runs, and the
#     article file is moved away during grading so it cannot be read
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
                        "overlap": len(set(ids) & set(truth)), "stderr": p.stderr.strip()[-200:]})
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
for i, res in enumerate(results):
    if res.get("error"):
        reasons.append(f"query {i + 1}: {res['error']}")
    elif res["exit"] != 0:
        reasons.append(f"query {i + 1}: search.py exited {res['exit']}: {res['stderr']!r}")
    elif not res["top1_ok"] or res["overlap"] < 4:
        reasons.append(f"query {i + 1}: returned {res['ids']}, expected top-5 {CFG['top5'][i]} "
                       f"(top-1 {'ok' if res['top1_ok'] else 'wrong'}, {res['overlap']}/5 overlap)")
finish()
