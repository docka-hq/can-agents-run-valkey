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
