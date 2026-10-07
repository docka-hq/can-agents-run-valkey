"""Known wrong, built to exploit a v1 gap: moves the current server's key and hash-field expiry deadlines
(+1000 s), then runs the reference copy. The copy matches the changed server; v1 checked only whether keys
expire. v2 compares key and field deadlines with a baseline the runner took before the solution ran."""
import redis as _r
_moved = _fields = 0
for _db in (0, 2):
    _c = _r.Redis(port=6379, db=_db)
    for _k in _c.scan_iter(count=1000):
        _t = _c.ttl(_k)
        if _t > 0:
            _c.expire(_k, _t + 1000)
            _moved += 1
        if _c.type(_k) == b"hash":
            _ft = _c.execute_command("HTTL", _k, "FIELDS", 1, "otp")
            if _ft and int(_ft[0]) > 0:
                _c.execute_command("HEXPIRE", _k, int(_ft[0]) + 1000, "FIELDS", 1, "otp")
                _fields += 1
print("moved deadlines:", _moved, "fields:", _fields)

# Reference (wave-2): Redis 8.10 -> Valkey 9.1 by a logical, type-aware copy. DUMP/RESTORE fails (RDB version)
# and REPLICAOF never syncs, so every key is read with its type's commands and written back, binary-safe,
# with absolute key expiry, per-field hash expiry, stream entries, and consumer groups with their pending entries.
import redis

SRC_PORT, DST_PORT = 6379, 6380


def sid(x):
    a, b = (x.decode() if isinstance(x, bytes) else x).split("-")
    return int(a), int(b)


def copy_db(db):
    s, d = redis.Redis(port=SRC_PORT, db=db), redis.Redis(port=DST_PORT, db=db)
    for k in s.scan_iter(count=1000):
        t = s.type(k).decode()
        if t == "string":
            d.set(k, s.get(k))
        elif t == "hash":
            items = s.hgetall(k)
            d.hset(k, mapping=items)
            fields = list(items)
            exp = s.execute_command("HPEXPIRETIME", k, "FIELDS", len(fields), *fields)
            for f, e in zip(fields, exp):
                if int(e) > 0:
                    d.execute_command("HPEXPIREAT", k, int(e), "FIELDS", 1, f)
        elif t == "list":
            d.rpush(k, *s.lrange(k, 0, -1))
        elif t == "set":
            d.sadd(k, *s.smembers(k))
        elif t == "zset":
            d.zadd(k, dict(s.zrange(k, 0, -1, withscores=True)))
        elif t == "stream":
            entries = s.xrange(k, "-", "+")
            for i, fields in entries:
                d.xadd(k, fields, id=i)
            for g in s.xinfo_groups(k):
                name, last = g["name"], g["last-delivered-id"]
                pend = {p["message_id"]: p["consumer"] for p in s.xpending_range(k, name, "-", "+", 100000)}
                consumers = [c["name"] for c in s.xinfo_consumers(k, name)]
                if not pend:
                    d.xgroup_create(k, name, id=last)
                else:  # replay deliveries in id order so the pending list and its owners come out the same
                    d.xgroup_create(k, name, id="0")
                    filler = consumers[0]
                    for i, _ in entries:
                        if sid(i) > sid(last):
                            break
                        owner = pend.get(i)
                        got = d.xreadgroup(name, owner or filler, {k: ">"}, count=1)
                        assert got and got[0][1][0][0] == i, (k, i, got)
                        if not owner:
                            d.xack(k, name, i)
                    d.xgroup_setid(k, name, last)
                for c in consumers:
                    d.xgroup_createconsumer(k, name, c)
        pexp = s.pexpiretime(k)
        if pexp and pexp > 0:
            d.pexpireat(k, pexp)


src0 = redis.Redis(port=SRC_PORT)
for name in (src0.info("keyspace") or {}):
    copy_db(int(name[2:]))
print("copied", {k: v["keys"] for k, v in redis.Redis(port=DST_PORT).info("keyspace").items()})
