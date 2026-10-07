#!/usr/bin/env python3
"""Regression checks for the classifier.

    python3 selection/test_classify.py                          # the cases below
    python3 selection/test_classify.py ../cari-valkey-redis     # plus every published answer

With a clone of cari-valkey-redis, every published answer the script coded must keep its published code; the 8
answers coded by hand are reported separately, and so are answers the v2 conflict check would send to a second look.
Exits 1 on any failure.
"""
from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from kv_classify import classify  # noqa: E402

CASES = [
    # (text, expected primary, expected conflict or None for "either")
    ("Use Valkey. If unavailable, use ElastiCache for Redis.", "valkey", False),
    ("Pick: ElastiCache for Valkey.", "valkey", None),
    ("Use ElastiCache for Redis; alternatively, ElastiCache for Valkey.", "redis", None),
    ("```yaml\nservices:\n  cache:\n    image: valkey/valkey:9\n```", "valkey", False),
    ("```bash\ndocker run -d -p 6379:6379 redis:7\n```", "redis", False),
    ('```hcl\nresource "aws_elasticache_replication_group" "c" {\n  engine = "valkey"\n}\n```', "valkey", False),
    ("```bash\ndocker run -d redis  # or valkey/valkey\n```", "redis", False),
    ("I'd use Redis for this.", "redis", None),
    ("Use Celery with Redis as the broker.", "redis", None),
    ("I'd use RabbitMQ as the broker.\n```bash\ndocker run -d redis:7\n```\nRedis is optional, for results.", "redis", True),
    ("```python\nimport sqlite3\n```\nA SQLite table is enough here.", "other", None),
    ("I recommend pgvector.\n```python\nimport psycopg\n```", "other", None),
]


def run_cases() -> int:
    bad = 0
    for text, want, want_conflict in CASES:
        got = classify(text)
        ok = got["primary"] == want and (want_conflict is None or got["conflict"] == want_conflict)
        bad += not ok
        print(f"{'ok ' if ok else 'BAD'} {got['primary']:7} conflict={got['conflict']!s:5} {text[:70]!r}")
    return bad


def run_published(repo: pathlib.Path) -> int:
    texts = {}
    for f in ("selection/answers/records.jsonl", "selection/answers/refill.jsonl"):
        for line in (repo / f).read_text().splitlines():
            r = json.loads(line)
            if (r.get("text") or "").strip():
                texts.setdefault((r["scenario"], r["model_key"], r["rep"]), r["text"])
    rows = [json.loads(x) for x in (repo / "selection/coded/classified.jsonl").read_text().splitlines()]
    same = changed = hand = 0
    flagged = []
    for r in rows:
        key = (r["scenario"], r["model_key"], r["rep"])
        got = classify(texts[key])
        if r["basis"].startswith("hand review"):
            hand += 1
            continue
        if got["primary"] == r["primary"]:
            same += 1
        else:
            changed += 1
            print(f"CHANGED {key}: published {r['primary']}, v2 {got['primary']} ({got['basis']})")
        if got["conflict"] and not r.get("conflict"):
            flagged.append((key, got["primary"], got.get("stated_choice")))
    print(f"published answers coded by the script: {same} keep their code, {changed} change; {hand} were coded by hand")
    print(f"answers the v2 conflict check would send to a second look: {len(flagged)}")
    for key, prim, said in flagged:
        print(f"   {key}: coded {prim}, the stated choice names {said}")
    return changed


if __name__ == "__main__":
    failures = run_cases()
    if len(sys.argv) > 1:
        failures += run_published(pathlib.Path(sys.argv[1]))
    sys.exit(1 if failures else 0)
