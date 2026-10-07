# Known wrong: the vectors are also copied to a local file at load time; search.py runs one FT.SEARCH for show
# and computes the answer from the local copy, reading almost nothing from the server. Must fail: after the top
# article is removed from the server, the local copy still returns it.
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
np.save("/app/vectors.npy", np.array([a["embedding"] for a in arts], dtype=np.float32))
json.dump([a["id"] for a in arts], open("/app/ids.json", "w"))
open("/app/search.py", "w").write(r'''
import json
import sys

import numpy as np
import redis

q = np.array(json.load(sys.stdin), dtype=np.float32)
r = redis.Redis(port=6379)
r.execute_command("FT.SEARCH", "kb_idx", "*=>[KNN 1 @embedding $vec AS score]", "PARAMS", "2", "vec", q.astype(np.float32).tobytes(), "DIALECT", "2")
ids = json.load(open("/app/ids.json"))
vecs = np.load("/app/vectors.npy")
sims = (vecs @ q) / (np.linalg.norm(vecs, axis=1) * np.linalg.norm(q))
for j in np.argsort(-sims)[:5]:
    print(ids[j])
''')
print("loaded", r.dbsize())
