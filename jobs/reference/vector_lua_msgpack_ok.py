# Correct, another format: each article as one MessagePack record (id, title, embedding) in a string (art:<id>),
# listed in a set of keys; a Lua script unpacks the records with cmsgpack and computes the cosine similarities inside
# the server. Must pass. Round six read embeddings from bytes, JSON arrays and fields, not from MessagePack.
import json
import struct

import redis


def msgpack(o) -> bytes:
    """A small MessagePack encoder (maps, arrays, strings, floats), enough for these records."""
    if isinstance(o, dict):
        return bytes([0x80 | len(o)]) + b"".join(msgpack(k) + msgpack(v) for k, v in o.items())
    if isinstance(o, list):
        return b"\xdc" + struct.pack(">H", len(o)) + b"".join(msgpack(x) for x in o)
    if isinstance(o, str):
        b = o.encode()
        return (bytes([0xa0 | len(b)]) if len(b) < 32 else b"\xd9" + bytes([len(b)])) + b
    return b"\xcb" + struct.pack(">d", float(o))


r = redis.Redis(port=6379)
pipe = r.pipeline(transaction=False)
for line in open("/data/articles.jsonl"):
    a = json.loads(line)
    pipe.set("art:" + a["id"], msgpack({"id": a["id"], "title": a["title"], "embedding": a["embedding"]}))
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
  local a = cmsgpack.unpack(redis.call('GET', key))
  local e, dot, n = a['embedding'], 0, 0
  for i = 1, #e do dot = dot + e[i] * q[i]; n = n + e[i] * e[i] end
  table.insert(scored, {dot / math.sqrt(n * qn), a['id']})
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
print("loaded", r.scard("art:index"))
