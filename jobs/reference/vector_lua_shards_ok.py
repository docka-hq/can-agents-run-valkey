# Correct, another shape: the articles in 20 hashes of 50 (shard:0 to shard:19), one field per article holding its
# JSON; a Lua script computes the cosine similarities inside the server. Must pass. Round four took a hash of 50
# fields for one article's record and deleted the whole shard, 50 articles, to remove one.
import json

import redis

r = redis.Redis(port=6379)
arts = [json.loads(line) for line in open("/data/articles.jsonl")]
pipe = r.pipeline(transaction=False)
for n, a in enumerate(arts):
    pipe.hset(f"shard:{n // 50}", a["id"], json.dumps({"id": a["id"], "title": a["title"], "embedding": a["embedding"]}))
pipe.execute()
open("/app/search.py", "w").write(r'''
import json
import sys

import redis

SCRIPT = """
local q = cjson.decode(ARGV[1])
local qn = 0
for i = 1, #q do qn = qn + q[i] * q[i] end
local scored = {}
for s = 0, 19 do
  for _, doc in ipairs(redis.call('HVALS', 'shard:' .. s)) do
    local a = cjson.decode(doc)
    local e, dot, n = a['embedding'], 0, 0
    for i = 1, #e do dot = dot + e[i] * q[i]; n = n + e[i] * e[i] end
    table.insert(scored, {dot / math.sqrt(n * qn), a['id']})
  end
end
table.sort(scored, function(x, y) return x[1] > y[1] end)
local top = {}
for i = 1, math.min(5, #scored) do top[i] = scored[i][2] end
return top
"""
r = redis.Redis(port=6379)
for doc_id in r.eval(SCRIPT, 0, json.dumps(json.load(sys.stdin))):
    print(doc_id.decode() if isinstance(doc_id, bytes) else doc_id)
''')
print("loaded", sum(r.hlen(f"shard:{s}") for s in range(20)))
