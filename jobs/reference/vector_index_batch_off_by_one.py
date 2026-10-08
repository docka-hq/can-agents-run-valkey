# Known wrong: every article is stored complete as JSON (art:<id>), but the indexed copies (doc:<id>) are written in
# batches with an off-by-one: the first article alone, then slices of 99 with a stride of 100, so kb-0101, kb-0201, ...
# kb-0901 never reach the index. No held-out query needs them, so every answer is still right. Round six probed
# eleven articles and passed this; must fail: the first article of every batch of 100 is among the probes.
import json

import numpy as np
import redis

r = redis.Redis(port=6379)
r.execute_command("FT.CREATE", "doc_idx", "ON", "HASH", "PREFIX", "1", "doc:", "SCHEMA",
                  "embedding", "VECTOR", "HNSW", "6", "TYPE", "FLOAT32", "DIM", "64", "DISTANCE_METRIC", "COSINE")
arts = [json.loads(line) for line in open("/data/articles.jsonl")]
pipe = r.pipeline(transaction=False)
for a in arts:
    pipe.set("art:" + a["id"], json.dumps(a))


def index(batch):
    for a in batch:
        pipe.hset("doc:" + a["id"], mapping={"id": a["id"],
                                              "embedding": np.array(a["embedding"], dtype=np.float32).tobytes()})


index(arts[:1])
for start in range(1, len(arts), 100):
    index(arts[start:start + 99])      # should be 100
pipe.execute()
open("/app/search.py", "w").write(r'''
import json
import sys

import numpy as np
import redis

q = np.array(json.load(sys.stdin), dtype=np.float32).tobytes()
r = redis.Redis(port=6379)
res = r.execute_command("FT.SEARCH", "doc_idx", "*=>[KNN 5 @embedding $vec AS score]",
                        "PARAMS", "2", "vec", q, "DIALECT", "2")
rows = []
if isinstance(res, dict):  # RESP3 map reply (Redis 8 answers FT.SEARCH this way on a RESP3 connection)
    for doc in res[b"results"]:
        ea = doc.get(b"extra_attributes", {})
        rows.append((float(ea.get(b"score", 0)), ea.get(b"id", doc[b"id"]).decode()))
else:  # RESP2-shaped array (valkey-search answers this way even on a RESP3 connection)
    for i in range(1, len(res), 2):
        f = res[i + 1]
        d = {f[j].decode(): f[j + 1] for j in range(0, len(f), 2)}
        score = next((float(v) for k, v in d.items() if k.endswith("score")), 0.0)
        rows.append((score, d["id"].decode()))
for _, doc_id in sorted(rows):
    print(doc_id)
''')
print("indexed", len(r.keys("doc:*")), "of", len(arts))
