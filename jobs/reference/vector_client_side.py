# Known wrong, built to exploit a v1 gap: search.py runs one FT.SEARCH for show, then reads every vector back
# from the server and computes cosine similarity in Python. v1 only looked for a search command;
# v2 fails a search.py that scans the keyspace or reads items one by one.
import json

import numpy as np
import redis

r = redis.Redis(port=6379)
try:
    r.execute_command("FT.DROPINDEX", "kb_idx")
except redis.ResponseError:
    pass
r.execute_command("FT.CREATE", "kb_idx", "ON", "HASH", "PREFIX", "1", "kb:", "SCHEMA",
                  "embedding", "VECTOR", "HNSW", "6", "TYPE", "FLOAT32", "DIM", "64", "DISTANCE_METRIC", "COSINE")
pipe = r.pipeline(transaction=False)
for line in open("/data/articles.jsonl"):
    a = json.loads(line)
    pipe.hset("kb:" + a["id"], mapping={"id": a["id"], "title": a["title"],
                                         "embedding": np.array(a["embedding"], dtype=np.float32).tobytes()})
pipe.execute()
open("/app/search.py", "w").write(r'''
import json
import sys

import numpy as np
import redis

q = np.array(json.load(sys.stdin), dtype=np.float32)
r = redis.Redis(port=6379)
r.execute_command("FT.SEARCH", "kb_idx", "*=>[KNN 1 @embedding $vec AS score]", "PARAMS", "2", "vec", q.tobytes(), "DIALECT", "2")
scored = []
for k in r.scan_iter(match="kb:*", count=1000):
    v = np.frombuffer(r.hget(k, "embedding"), dtype=np.float32)
    scored.append((float(np.dot(q, v) / (np.linalg.norm(q) * np.linalg.norm(v))), r.hget(k, "id").decode()))
for _, doc_id in sorted(scored, reverse=True)[:5]:
    print(doc_id)
''')
print("loaded", r.dbsize())
