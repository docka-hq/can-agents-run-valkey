"""Canonical, server-version-independent dump of a Redis/Valkey keyspace, for exact-copy grading.
Version 2 (wave-2, Redis 8 -> Valkey): hashes also carry each field's absolute expiry (HPEXPIRETIME, -1 = none).

Shared by the seeding script (which records the old server's digest at container start) and the
migration grader (which inlines this same file). Values are read with type-specific commands, never
DUMP: a DUMP payload carries the RDB version, so it differs across server versions even when the
data is identical. TTLs are kept beside the value (pttl) and compared with a tolerance by the grader.
"""
from __future__ import annotations

import hashlib
import json

import redis


def _h(b) -> str:
    if isinstance(b, str):
        b = b.encode()
    return b.hex()


def _s(b) -> str:
    return b.decode() if isinstance(b, bytes) else str(b)


def dbs_with_keys(r: redis.Redis) -> list:
    ks = r.info("keyspace") or {}
    return sorted(int(k[2:]) for k in ks if k.startswith("db") and (ks[k] or {}).get("keys", 0) > 0)


def dump_value(r: redis.Redis, key: bytes, typ: str):
    if typ == "string":
        return _h(r.get(key))
    if typ == "hash":
        items = sorted(r.hgetall(key).items())
        fields = [f for f, _ in items]
        exp = list(r.execute_command("HPEXPIRETIME", key, "FIELDS", len(fields), *fields)) if fields else []
        return sorted([_h(f), _h(v), int(e)] for (f, v), e in zip(items, exp))
    if typ == "list":
        return [_h(x) for x in r.lrange(key, 0, -1)]
    if typ == "set":
        return sorted(_h(x) for x in r.smembers(key))
    if typ == "zset":
        return [[_h(m), repr(float(s))] for m, s in r.zrange(key, 0, -1, withscores=True)]
    if typ == "stream":
        entries = [[_s(i), sorted([_h(f), _h(v)] for f, v in fields.items())] for i, fields in r.xrange(key, "-", "+")]
        groups = []
        for g in r.xinfo_groups(key):
            name = g.get("name")
            consumers = sorted([_h(c.get("name")), int(c.get("pending", 0))] for c in r.xinfo_consumers(key, name))
            pel = sorted([_s(p["message_id"]), _h(p["consumer"])]
                         for p in r.xpending_range(key, name, min="-", max="+", count=100000))
            groups.append([_h(name), _s(g.get("last-delivered-id")), int(g.get("pending", 0)), consumers, pel])
        return {"entries": entries, "groups": sorted(groups)}
    return f"<unsupported type {typ}>"


def dump_server(port: int, host: str = "127.0.0.1") -> dict:
    """{db: {key_hex: [type, value, pttl]}} for every database that holds keys."""
    out: dict = {}
    base = redis.Redis(host=host, port=port, socket_timeout=30)
    for db in dbs_with_keys(base):
        r = redis.Redis(host=host, port=port, db=db, socket_timeout=30)
        keys = list(r.scan_iter(count=1000))
        pipe = r.pipeline(transaction=False)
        for k in keys:
            pipe.type(k)
            pipe.pttl(k)
        meta = pipe.execute()
        dbd = {}
        for i, k in enumerate(keys):
            typ = _s(meta[2 * i])
            if typ == "none":  # expired or deleted between SCAN and TYPE
                continue
            dbd[_h(k)] = [typ, dump_value(r, k, typ), int(meta[2 * i + 1])]
        out[db] = dbd
    return out


def _strip_field_expiry(t, v):
    # hash rows are [field, value, abs_expiry_ms]: keep only whether the field expires (stable over time)
    return [[f, val, e > 0] for f, val, e in v] if t == "hash" else v


def digest(dump: dict) -> str:
    """Content digest: types, values and WHETHER a key (or hash field) expires; not the remaining ms."""
    canon = {str(db): {k: [t, _strip_field_expiry(t, v), p > 0] for k, (t, v, p) in sorted(d.items())}
             for db, d in sorted(dump.items())}
    return hashlib.sha256(json.dumps(canon, sort_keys=True).encode()).hexdigest()
