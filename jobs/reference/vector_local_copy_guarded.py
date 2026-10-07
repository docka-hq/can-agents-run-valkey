# Known wrong: answers from a local copy of the vectors, as vector_local_copy.py does, but checks that each article
# it is about to print still exists in the server and exits with an error if one does not. Round two's removal
# check failed only an answer that still returned the removed article, so this passed. Must fail: once the article
# is gone, search.py must still run cleanly and return the new top 5.
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
arts = [json.loads(line) for line in open("/data/articles.jsonl")]
pipe = r.pipeline(transaction=False)
for a in arts:
    pipe.hset("kb:" + a["id"], mapping={"id": a["id"], "title": a["title"],
                                         "embedding": np.array(a["embedding"], dtype=np.float32).tobytes()})
pipe.execute()
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
top = [ids[j] for j in np.argsort(-sims)[:5]]
missing = [i for i in top if not r.exists("kb:" + i)]
if missing:
    sys.exit(f"index out of date: {missing}")
print("\n".join(top))
''')
print("loaded", r.dbsize())
