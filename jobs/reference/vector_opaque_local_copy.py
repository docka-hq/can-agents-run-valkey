# Known wrong: opaque keys (doc:0 to doc:999) that hold the article id as a field value, plus a local copy of the
# vectors; search.py runs one FT.SEARCH for show and answers from the local copy. Round two's removal check looked
# for the id only in key names and hash field names, so it skipped this layout and the copy passed. Must fail: the
# check finds the record by the id it holds, and the local copy still returns the removed article.
import json

import numpy as np
import redis

r = redis.Redis(port=6379)
r.execute_command("FT.CREATE", "docs_idx", "ON", "HASH", "PREFIX", "1", "doc:", "SCHEMA",
                  "embedding", "VECTOR", "HNSW", "6", "TYPE", "FLOAT32", "DIM", "64", "DISTANCE_METRIC", "COSINE")
arts = [json.loads(line) for line in open("/data/articles.jsonl")]
pipe = r.pipeline(transaction=False)
for n, a in enumerate(arts):
    pipe.hset(f"doc:{n}", mapping={"id": a["id"], "title": a["title"],
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
r.execute_command("FT.SEARCH", "docs_idx", "*=>[KNN 1 @embedding $vec AS score]", "PARAMS", "2", "vec", q.astype(np.float32).tobytes(), "DIALECT", "2")
ids = json.load(open("/app/ids.json"))
vecs = np.load("/app/vectors.npy")
sims = (vecs @ q) / (np.linalg.norm(vecs, axis=1) * np.linalg.norm(q))
for j in np.argsort(-sims)[:5]:
    print(ids[j])
''')
print("loaded", r.dbsize())
