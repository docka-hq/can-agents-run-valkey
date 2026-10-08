# Correct, another place: as vector_lua_ok.py, in logical database 1 instead of 0. Must pass: the job names the server
# and its port, not a database. Round five read database 0 only and found no articles.
import json

import redis

r = redis.Redis(port=6379, db=1)
pipe = r.pipeline(transaction=False)
for line in open("/data/articles.jsonl"):
    a = json.loads(line)
    pipe.set("art:" + a["id"], json.dumps({"id": a["id"], "title": a["title"], "embedding": a["embedding"]}))
    pipe.sadd("art:index", "art:" + a["id"])
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
for _, key in ipairs(redis.call('SMEMBERS', 'art:index')) do
  local a = cjson.decode(redis.call('GET', key))
  local e, dot, n = a['embedding'], 0, 0
  for i = 1, #e do dot = dot + e[i] * q[i]; n = n + e[i] * e[i] end
  table.insert(scored, {dot / math.sqrt(n * qn), a['id']})
end
table.sort(scored, function(x, y) return x[1] > y[1] end)
local top = {}
for i = 1, math.min(5, #scored) do top[i] = scored[i][2] end
return top
"""
r = redis.Redis(port=6379, db=1)
for doc_id in r.eval(SCRIPT, 0, json.dumps(json.load(sys.stdin))):
    print(doc_id.decode() if isinstance(doc_id, bytes) else doc_id)
''')
print("loaded", r.scard("art:index"))
