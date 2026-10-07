# Correct, another shape: JSON documents (art:<id>) holding the embedding as a JSON array, indexed ON JSON; search.py
# takes each hit's article id from its key. Must pass: the count reads the embedding from the JSON array.
import json

import redis

r = redis.Redis(port=6379)
r.execute_command("FT.CREATE", "art_idx", "ON", "JSON", "PREFIX", "1", "art:", "SCHEMA",
                  "$.embedding", "AS", "embedding", "VECTOR", "HNSW", "6", "TYPE", "FLOAT32", "DIM", "64",
                  "DISTANCE_METRIC", "COSINE")
pipe = r.pipeline(transaction=False)
for line in open("/data/articles.jsonl"):
    a = json.loads(line)
    pipe.execute_command("JSON.SET", "art:" + a["id"], "$", json.dumps({"title": a["title"], "embedding": a["embedding"]}))
pipe.execute()
open("/app/search.py", "w").write(r'''
import json
import sys

import numpy as np
import redis

q = np.array(json.load(sys.stdin), dtype=np.float32).tobytes()
r = redis.Redis(port=6379)
res = r.execute_command("FT.SEARCH", "art_idx", "*=>[KNN 5 @embedding $vec AS score]",
                        "PARAMS", "2", "vec", q, "DIALECT", "2")
rows = []
if isinstance(res, dict):  # RESP3 map reply (Redis 8 answers FT.SEARCH this way on a RESP3 connection)
    for n, doc in enumerate(res[b"results"]):
        score = doc.get(b"extra_attributes", {}).get(b"score")
        rows.append((float(score) if score is not None else n, doc[b"id"].decode()))
else:  # RESP2-shaped array (valkey-search answers this way even on a RESP3 connection)
    for n, i in enumerate(range(1, len(res), 2)):
        f = res[i + 1] or []
        d = {f[j].decode(): f[j + 1] for j in range(0, len(f), 2)}
        score = next((float(v) for k, v in d.items() if k.endswith("score")), None)
        rows.append((score if score is not None else n, res[i].decode()))
for _, key in sorted(rows):
    print(key[len("art:"):])
''')
print("loaded", r.dbsize())
