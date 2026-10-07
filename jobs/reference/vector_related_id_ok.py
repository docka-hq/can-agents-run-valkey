# Correct, another shape: as vector_opaque_ok.py (opaque keys, the id only in a base64 field), and each record also
# names a related article in a plain field. Must pass. Round three read a record that mentions two ids as a map shared
# by many articles, removed only its metadata field and left the article in the index, so search.py then failed.
import base64
import json

import numpy as np
import redis

r = redis.Redis(port=6379)
r.execute_command("FT.CREATE", "docs_idx", "ON", "HASH", "PREFIX", "1", "doc:", "SCHEMA",
                  "embedding", "VECTOR", "HNSW", "6", "TYPE", "FLOAT32", "DIM", "64", "DISTANCE_METRIC", "COSINE")
pipe = r.pipeline(transaction=False)
arts = [json.loads(line) for line in open("/data/articles.jsonl")]
for n, a in enumerate(arts):
    meta = base64.b64encode(json.dumps({"id": a["id"], "title": a["title"]}).encode())
    related = arts[(n + 1) % len(arts)]["id"]  # a "see also" link to another article
    pipe.hset(f"doc:{n}", mapping={"meta": meta, "related": related,
                                   "embedding": np.array(a["embedding"], dtype=np.float32).tobytes()})
pipe.execute()
open("/app/search.py", "w").write(r'''
import base64
import json
import sys

import numpy as np
import redis

q = np.array(json.load(sys.stdin), dtype=np.float32).tobytes()
r = redis.Redis(port=6379)
res = r.execute_command("FT.SEARCH", "docs_idx", "*=>[KNN 5 @embedding $vec AS score]",
                        "PARAMS", "2", "vec", q, "DIALECT", "2")
rows = []
if isinstance(res, dict):  # RESP3 map reply (Redis 8 answers FT.SEARCH this way on a RESP3 connection)
    for doc in res[b"results"]:
        ea = doc.get(b"extra_attributes", {})
        rows.append((float(ea.get(b"score", 0)), ea[b"meta"]))
else:  # RESP2-shaped array (valkey-search answers this way even on a RESP3 connection)
    for i in range(1, len(res), 2):
        f = res[i + 1]
        d = {f[j].decode(): f[j + 1] for j in range(0, len(f), 2)}
        score = next((float(v) for k, v in d.items() if k.endswith("score")), 0.0)
        rows.append((score, d["meta"]))
for _, meta in sorted(rows):
    print(json.loads(base64.b64decode(meta))["id"])
''')
print("loaded", r.dbsize())
