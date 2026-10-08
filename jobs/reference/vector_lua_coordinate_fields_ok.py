# Correct, another shape: one hash per article (kb:<id>) holding its id, title and one field per coordinate (e0 to
# e63), listed in a set of keys; a Lua script computes the cosine similarities inside the server. Must pass. Round
# five read embeddings packed into one value or a JSON array, not one coordinate per field.
import json

import redis

r = redis.Redis(port=6379)
pipe = r.pipeline(transaction=False)
for line in open("/data/articles.jsonl"):
    a = json.loads(line)
    pipe.hset("kb:" + a["id"], mapping={"id": a["id"], "title": a["title"],
                                         **{f"e{n}": x for n, x in enumerate(a["embedding"])}})
    pipe.sadd("kb:index", "kb:" + a["id"])
pipe.execute()
open("/app/search.py", "w").write(r'''
import json
import sys

import redis

SCRIPT = """
local q = cjson.decode(ARGV[1])
local qn = 0
for i = 1, #q do qn = qn + q[i] * q[i] end
local fields = {}
for i = 0, #q - 1 do fields[#fields + 1] = 'e' .. i end
local scored = {}
for _, key in ipairs(redis.call('SMEMBERS', 'kb:index')) do
  local e = redis.call('HMGET', key, unpack(fields))
  local dot, n = 0, 0
  for i = 1, #q do
    local x = tonumber(e[i])
    dot = dot + x * q[i]
    n = n + x * x
  end
  table.insert(scored, {dot / math.sqrt(n * qn), redis.call('HGET', key, 'id')})
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
print("loaded", r.scard("kb:index"))
