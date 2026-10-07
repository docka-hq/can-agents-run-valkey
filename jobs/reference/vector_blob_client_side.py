# Known wrong: every vector is stored in ONE string value; search.py runs one FT.SEARCH for show, fetches that
# value with a single GET and computes cosine similarity in Python. Must fail.
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
arts = [json.loads(line) for line in open("/data/articles.jsonl")]
r.set("emb:all", np.array([a["embedding"] for a in arts], dtype=np.float32).tobytes())
r.set("emb:ids", json.dumps([a["id"] for a in arts]))
open("/app/search.py", "w").write(r'''
import json
import sys

import numpy as np
import redis

q = np.array(json.load(sys.stdin), dtype=np.float32)
r = redis.Redis(port=6379)
r.execute_command("FT.SEARCH", "kb_idx", "*=>[KNN 1 @embedding $vec AS score]", "PARAMS", "2", "vec", q.astype(np.float32).tobytes(), "DIALECT", "2")
ids = json.loads(r.get("emb:ids"))
vecs = np.frombuffer(r.get("emb:all"), dtype=np.float32).reshape(len(ids), -1)
sims = (vecs @ q) / (np.linalg.norm(vecs, axis=1) * np.linalg.norm(q))
for j in np.argsort(-sims)[:5]:
    print(ids[j])
''')
print("loaded", r.dbsize())
