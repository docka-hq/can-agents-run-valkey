#!/usr/bin/env python3
"""Seed the 'current production' Redis 8.10 with the wave-1 dataset PLUS 40 hashes whose fields expire
on their own (a Redis 7.4+ feature), then record its digest with kvdump2.

Every common type, keys in two databases, expiring and persistent keys, binary values, key names
with spaces and non-ASCII characters, and streams with consumer groups and pending entries.
Expiries are 2-20 hours, so nothing expires during an attempt.
"""
import argparse
import json
import pathlib
import random
import sys

import redis

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import kvdump2 as kvdump  # noqa: E402

SEED = 20261002


def seed(port: int) -> None:
    rng = random.Random(SEED)
    r0 = redis.Redis(port=port, db=0)
    r2 = redis.Redis(port=port, db=2)
    r0.flushall()
    p = r0.pipeline(transaction=False)

    for i in range(2000):
        k = f"session:{i:05d}"
        p.set(k, json.dumps({"user": i, "token": "%032x" % rng.getrandbits(128), "scopes": ["read", "write"][: 1 + i % 2]}))
        if i % 5 < 2:
            p.expire(k, rng.randint(7200, 72000))
    plans = ["free", "team", "business", "enterprise"]
    for i in range(400):
        k = f"user:{i:04d}"
        fields = {"name": f"User {i}", "email": f"user{i}@example.com", "plan": plans[i % 4],
                  "created": str(1700000000 + i * 3600), "visits": str(rng.randint(0, 5000))}
        for j in range(rng.randint(0, 5)):
            fields[f"pref:{j}"] = rng.choice(["on", "off", "weekly", "daily"])
        p.hset(k, mapping=fields)
        if i % 4 == 0:
            p.expire(k, rng.randint(7200, 72000))
    for i in range(150):
        p.rpush(f"queue:{i:03d}", *[f"job-{i}-{j}" for j in range(rng.randint(5, 40))])
        p.sadd(f"tags:{i:03d}", *{rng.choice(["red", "green", "blue", "new", "sale", "vip", "beta", "eu", "us", "apac",
                                             "mobile", "web", "trial", "paid", "churned", "lead", "partner"])
                                  for _ in range(rng.randint(3, 20))})
        p.zadd(f"leaderboard:{i:03d}", {f"player-{j}": round(rng.uniform(-50, 5000), rng.choice([0, 1, 2, 3]))
                                        for j in range(rng.randint(10, 60))})
    for i in range(30):
        p.pfadd(f"visitors:{i:02d}", *[f"visitor-{rng.randint(0, 100000)}" for _ in range(rng.randint(100, 500))])
    for i in range(10):
        for _ in range(rng.randint(20, 200)):
            p.setbit(f"active:{i:02d}", rng.randint(0, 4000), 1)
    for i in range(5):
        p.geoadd(f"stores:{i}", [v for j in range(20)
                                 for v in (round(rng.uniform(-122.5, -121.8), 6), round(rng.uniform(37.2, 37.9), 6), f"store-{i}-{j}")])
    for i in range(3):
        p.set(f"blob:{i}", bytes([0, 255, 16, 0]) + bytes(rng.getrandbits(8) for _ in range(64)) + b"\x00end")
    p.set("user:name with space", "a key name with spaces")
    p.set("ключ:юникод".encode(), "значение в UTF-8".encode())
    p.execute()

    for i in range(20):
        k = f"events:{i:02d}"
        base = 1767225600000 + i * 100000
        p = r0.pipeline(transaction=False)
        for j in range(50):
            p.xadd(k, {"type": rng.choice(["click", "view", "buy", "signup"]), "user": str(rng.randint(1, 400)),
                       "n": str(j)}, id=f"{base + j}-0")
        p.execute()
        if i < 10:
            r0.xgroup_create(k, "workers", id="0")
            got = r0.xreadgroup("workers", "w1", {k: ">"}, count=20)
            ids = [mid for mid, _ in got[0][1]]
            r0.xack(k, "workers", *ids[:10])
            r0.xreadgroup("workers", "w2", {k: ">"}, count=5)
            if i % 2 == 0:
                r0.xgroup_create(k, "audit", id="$")

    p = r2.pipeline(transaction=False)
    for i in range(300):
        k = f"cache:page:{i:03d}"
        p.set(k, f"<html>page {i} rendered {rng.randint(0, 10**9)}</html>")
        if i % 2 == 0:
            p.expire(k, rng.randint(7200, 72000))
    for i in range(50):
        p.hset(f"cache:fragment:{i:02d}", mapping={"html": f"<div>{i}</div>", "etag": "%08x" % rng.getrandbits(32)})
    p.execute()


def seed_field_ttls(port: int) -> None:
    rng = random.Random(SEED + 8)
    r = redis.Redis(port=port, db=0)
    for i in range(40):
        k = f"profile:{i:02d}"
        r.hset(k, mapping={"name": f"Profile {i}", "tier": rng.choice(["free", "pro"]), "otp": "%06d" % rng.randint(0, 999999),
                           "reset_token": "%016x" % rng.getrandbits(64), "theme": rng.choice(["dark", "light"]), "lang": "en"})
        r.execute_command("HEXPIRE", k, rng.randint(7200, 72000), "FIELDS", 2, "otp", "reset_token")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=6379)
    ap.add_argument("--digest", required=True)
    a = ap.parse_args()
    seed(a.port)
    seed_field_ttls(a.port)
    dump = kvdump.dump_server(a.port)
    pathlib.Path(a.digest).write_text(kvdump.digest(dump) + "\n")
    counts = {db: len(d) for db, d in dump.items()}
    print("seeded", counts, "digest", kvdump.digest(dump))


if __name__ == "__main__":
    main()
