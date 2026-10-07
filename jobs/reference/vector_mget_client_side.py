# Known wrong: search.py runs one FT.SEARCH for show, then fetches every vector with MGET in bulk and computes
# cosine similarity in Python. Few commands, but the server sends hundreds of KB per query. Must fail.
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
ids_all = []
for line in open("/data/articles.jsonl"):
    a = json.loads(line)
    r.set("emb:" + a["id"], np.array(a["embedding"], dtype=np.float32).tobytes())
    ids_all.append(a["id"])
r.set("emb:ids", json.dumps(ids_all))
open("/app/search.py", "w").write(r'''
import json
import sys

import numpy as np
import redis

q = np.array(json.load(sys.stdin), dtype=np.float32)
r = redis.Redis(port=6379)
r.execute_command("FT.SEARCH", "kb_idx", "*=>[KNN 1 @embedding $vec AS score]", "PARAMS", "2", "vec", q.astype(np.float32).tobytes(), "DIALECT", "2")
ids = json.loads(r.get("emb:ids"))
vecs = []
for s in range(0, len(ids), 500):
    vecs += [np.frombuffer(b, dtype=np.float32) for b in r.mget(["emb:" + i for i in ids[s:s + 500]])]
vecs = np.array(vecs)
sims = (vecs @ q) / (np.linalg.norm(vecs, axis=1) * np.linalg.norm(q))
for j in np.argsort(-sims)[:5]:
    print(ids[j])
''')
print("loaded", r.dbsize())
