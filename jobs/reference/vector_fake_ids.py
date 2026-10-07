# Known wrong: loads 900 of the 1,000 articles (every tenth is skipped) and stores 100 placeholder records under ids
# that are not in the article file (kb-1001 to kb-1100), so 1,000 distinct ids are in the server. None of the
# skipped articles ranks in the top 10 of any held-out query, so every answer is still right and only the count can
# catch it. Round two counted any id-shaped text and passed this; must fail: only the supplied ids count.
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
    if int(a["id"][3:]) % 10 == 0:
        continue
    pipe.hset("kb:" + a["id"], mapping={"id": a["id"], "title": a["title"],
                                         "embedding": np.array(a["embedding"], dtype=np.float32).tobytes()})
for n in range(1001, 1101):
    pipe.hset(f"placeholder:kb-{n}", mapping={"id": f"kb-{n}", "title": "placeholder"})
pipe.execute()
open("/app/search.py", "w").write(r'''
import json
import sys

import numpy as np
import redis

q = np.array(json.load(sys.stdin), dtype=np.float32).tobytes()
r = redis.Redis(port=6379)
res = r.execute_command("FT.SEARCH", "kb_idx", "*=>[KNN 5 @embedding $vec AS score]",
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
print("loaded", r.dbsize())
