# Correct, another shape: the articles split across two indexes of 500, search.py queries both and merges by
# distance. Must pass: all 1,000 articles are loaded, just not in one index.
import json

import numpy as np
import redis

r = redis.Redis(port=6379)
for name, prefix in (("kb_a", "kba:"), ("kb_b", "kbb:")):
    r.execute_command("FT.CREATE", name, "ON", "HASH", "PREFIX", "1", prefix, "SCHEMA",
                      "embedding", "VECTOR", "HNSW", "6", "TYPE", "FLOAT32", "DIM", "64", "DISTANCE_METRIC", "COSINE")
pipe = r.pipeline(transaction=False)
for line in open("/data/articles.jsonl"):
    a = json.loads(line)
    pipe.hset(("kba:" if int(a["id"][3:]) % 2 else "kbb:") + a["id"], mapping={"id": a["id"], "title": a["title"],
                                         "embedding": np.array(a["embedding"], dtype=np.float32).tobytes()})
pipe.execute()
open("/app/search.py", "w").write(r'''
import json
import sys

import numpy as np
import redis

q = np.array(json.load(sys.stdin), dtype=np.float32).tobytes()
r = redis.Redis(port=6379)
rows = []
for index in ("kb_a", "kb_b"):
    res = r.execute_command("FT.SEARCH", index, "*=>[KNN 5 @embedding $vec AS score]", "PARAMS", "2", "vec", q, "DIALECT", "2")
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
for _, doc_id in sorted(rows)[:5]:
    print(doc_id)
''')
print("loaded", r.dbsize())
