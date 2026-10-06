# WRONG PATH: vectors are in the server, but search.py pulls them all and ranks in Python.
import json

import numpy as np
import redis

r = redis.Redis(port=6379)
pipe = r.pipeline(transaction=False)
for line in open("/data/articles.jsonl"):
    a = json.loads(line)
    pipe.hset("kb:" + a["id"], mapping={"id": a["id"], "embedding": np.array(a["embedding"], dtype=np.float32).tobytes()})
pipe.execute()
open("/app/search.py", "w").write(r'''
import json
import sys

import numpy as np
import redis

q = np.array(json.load(sys.stdin), dtype=np.float32)
q /= np.linalg.norm(q)
r = redis.Redis(port=6379)
scored = []
for k in r.scan_iter("kb:*", count=500):
    v = np.frombuffer(r.hget(k, "embedding"), dtype=np.float32)
    scored.append((float(v @ q / np.linalg.norm(v)), r.hget(k, "id").decode()))
for _, doc_id in sorted(scored, reverse=True)[:5]:
    print(doc_id)
''')
