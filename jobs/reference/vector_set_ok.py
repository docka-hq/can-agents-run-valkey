# Correct, another shape (Redis 8 only: Valkey has no vector sets): one vector set, one element per article named by
# its id, unquantized, searched exactly with VSIM ... TRUTH. Must pass: the count reads the ids from element names.
import json

import redis

r = redis.Redis(port=6379)
pipe = r.pipeline(transaction=False)
for line in open("/data/articles.jsonl"):
    a = json.loads(line)
    pipe.execute_command("VADD", "articles", "VALUES", len(a["embedding"]), *a["embedding"], a["id"], "NOQUANT",
                         "SETATTR", json.dumps({"title": a["title"]}))
pipe.execute()
open("/app/search.py", "w").write(r'''
import json
import sys

import redis

q = json.load(sys.stdin)
r = redis.Redis(port=6379)
for element in r.execute_command("VSIM", "articles", "VALUES", len(q), *q, "COUNT", 5, "TRUTH"):
    print(element.decode() if isinstance(element, bytes) else element)
''')
print("loaded", r.execute_command("VCARD", "articles"))
