#!/usr/bin/env python3
"""Selection coding, v1: what does each answer actually SET UP?

v0 (inside kv_select.py, kept unchanged with the frozen runner) matched patterns anywhere in the text, so
a Terraform comment like `# use "redis7" if engine = "redis"` or a fallback tip ("if your provider lacks
Valkey, switch engine = "redis"") counted as much as the resource itself, and ties went to whichever
product was listed first. v1 reads evidence in tiers and only from code lines, comments stripped:

  tier 1  deployable code: compose/k8s `image:`, `docker run`, Terraform `engine =`, package installs, helm charts
  tier 2  a named managed service in prose: "ElastiCache for X", "Memorystore for X", Upstash, Redis Cloud...
  tier 3  the recommendation sentence ("I'd use X", "Pick: X")

The first tier with any evidence decides; inside it the first occurrence wins. Answers whose deciding tier
holds more than one product are listed for hand review; a decision there goes into
config/selection-1-review.json with the reason, and the page shows the count of hand-reviewed answers.
A client library is never evidence: the redis Python client talks to Valkey too.
"""
from __future__ import annotations

import json
import pathlib
import re
from collections import Counter, defaultdict

ROOT = pathlib.Path(__file__).resolve().parent
PRODUCTS = ("valkey", "redis", "memcached")

CODE_FENCE = re.compile(r"```[^\n]*\n(.*?)```", re.S)

TIER1 = [  # applied to code lines (inside fences, or shell-looking lines), comments stripped
    (r"^\s*-?\s*image:\s*['\"]?(?:[\w.-]+/)*(valkey|redis|memcached)\b", None),
    (r"^\s*(?:\$\s*)?docker\s+run\b.*?\s(?:[\w.-]+/)*(valkey|redis|memcached)(?:[:@]\S*)?(?:\s|$)", None),
    (r"^\s*engine\s*=\s*\"(valkey|redis|memcached)\"", re.I),
    (r"variable\s+\"(?:cache_)?engine\"[^\n]*default\s*=\s*\"(valkey|redis|memcached)\"", re.I),
    (r"^\s*default\s*=\s*\"(valkey|redis|memcached)\"", re.I),
    (r"^\s*(?:sudo\s+)?(?:apt|apt-get|dnf|yum|brew|apk)\s+(?:install|add)\b.*\b(valkey|redis|memcached)(?:-server)?\b", None),
    (r"^\s*helm\s+(?:install|upgrade)\b.*\b(?:bitnami|valkey-io|redis|oci://\S+)/(valkey|redis|memcached)\b", None),
]
TIER2 = [
    (r"\belasticache(?:\s+serverless)?\s+for\s+(valkey|redis|memcached)\b", re.I),
    (r"\bmemorystore\s+for\s+(valkey|redis|memcached)\b", re.I),
    (r"\b(upstash)\b", re.I),
    (r"\b(redis\s+cloud|azure\s+cache\s+for\s+redis|azure\s+managed\s+redis)\b", re.I),
]
STORES = (r"valkey|redis(?:vl|\s+stack)?|memcached|dragonfly|keydb|garnet|rabbitmq|sqs|kafka|nats|postgres(?:ql)?|pgvector|"
          r"qdrant|chroma(?:db)?|weaviate|milvus|faiss|sqlite|lancedb|dynamodb|in-process|in-memory\s+dict|lru_cache")
TRIGGER = re.compile(r"(?i)\b(use|using|choose|chose|pick|picked|recommend|go with|set up|start with|default|choice|decision|"
                     r"stack|what i'?d use|what i use|i'?d|i would|i'?m using|answer)\b|^\s*#|^\s*\*\*")
STORE_RE = re.compile(r"(?i)\b(" + STORES + r")\b")
IMPORTS = [  # tier 2.5: what the setup code imports (redis-py alone never decides Redis vs Valkey)
    (r"^\s*(?:from|import)\s+redisvl\b", "redis"),
    (r"^\s*(?:from|import)\s+valkey\b|valkey\.Valkey\(|glide", "valkey"),
    (r"^\s*import\s+sqlite3\b", "other:sqlite"),
    (r"psycopg|pgvector", "other:postgres"),
    (r"qdrant_client", "other:qdrant"),
    (r"^\s*import\s+chromadb|chromadb\.", "other:chroma"),
    (r"^\s*import\s+faiss\b", "other:faiss"),
    (r"^\s*import\s+lancedb\b", "other:lancedb"),
]


def norm(word: str) -> str:
    w = word.lower()
    if w in ("upstash", "redis cloud") or "redis" in w:
        return "redis" if w not in PRODUCTS else w
    return w


def code_lines(text: str) -> list[str]:
    lines = []
    for block in CODE_FENCE.findall(text):
        lines += block.splitlines()
    # shell commands written outside fences (e.g. "$ docker run ...")
    lines += [ln for ln in text.splitlines() if re.match(r"^\s*(\$\s+)?(docker|helm|apt|apt-get|brew)\s", ln)]
    out = []
    for ln in lines:
        if re.match(r"^\s*(#|//|--)", ln):
            continue  # a whole-line comment
        out.append(re.split(r"\s#\s|\s//\s", ln)[0])  # trailing comment
    return out


def tier_hits(text: str):
    t1 = []
    for i, ln in enumerate(code_lines(text)):
        for pat, flags in TIER1:
            m = re.search(pat, ln, flags or 0)
            if m:
                t1.append((i, m.group(1).lower()))
    t2 = []
    for pat, flags in TIER2:
        for m in re.finditer(pat, text, flags or 0):
            t2.append((m.start(), norm(m.group(1))))
    return sorted(t1), sorted(t2)


def store_of(word: str) -> str:
    w = word.lower()
    if w.startswith("redis"):
        return "redis"
    if w in ("valkey", "memcached"):
        return w
    return "other:" + w


def classify(text: str) -> dict:
    t1, t2 = tier_hits(text)
    for tier, hits in (("code", t1), ("managed service named", t2)):
        prods = [p for _, p in hits if p in PRODUCTS]
        if prods:
            return {"primary": prods[0], "basis": tier, "evidence": sorted(set(prods)), "conflict": len(set(prods)) > 1}
    lines = code_lines(text)
    imp = []
    for ln in lines:
        for pat, prod in IMPORTS:
            if re.search(pat, ln):
                imp.append(prod)
    # the recommendation line: the first line that both reads as a pick and names a store (frameworks like
    # Celery or Dramatiq are not stores: in "Celery + Redis" the store is Redis)
    pick = None
    for ln in (text or "").splitlines():
        if TRIGGER.search(ln):
            m = STORE_RE.search(ln)
            if m:
                pick = store_of(m.group(1))
                break
    if pick:
        prim = pick if pick in PRODUCTS else "other"
        conflict = bool(imp) and any((i in PRODUCTS) != (prim in PRODUCTS) or (i in PRODUCTS and i != prim) for i in imp)
        return {"primary": prim, "basis": "recommendation line" + ("" if prim in PRODUCTS else f" ({pick[6:]})"),
                "evidence": sorted(set([pick] + imp)), "conflict": conflict}
    if imp:
        prim = imp[0] if imp[0] in PRODUCTS else "other"
        return {"primary": prim, "basis": "code imports" + ("" if prim in PRODUCTS else f" ({imp[0][6:]})"),
                "evidence": sorted(set(imp)), "conflict": len({i in PRODUCTS for i in imp}) > 1}
    return {"primary": "none", "basis": "no pick found", "evidence": [], "conflict": False}


def main() -> None:
    cfg = json.loads((ROOT / "config/selection-1.json").read_text())
    wd = ROOT / "runs" / cfg["wave"]
    recs = {}
    for line in (wd / "records.jsonl").read_text().splitlines():
        r = json.loads(line)
        if (r.get("text") or "").strip():
            recs[(r["scenario"], r["model_key"], r["rep"])] = r
    refilled = 0
    rf = wd / "refill.jsonl"  # kv_refill.py: answers for cells that came back empty at 16k (never for an answered cell)
    if rf.exists():
        for line in rf.read_text().splitlines():
            r = json.loads(line)
            k = (r["scenario"], r["model_key"], r["rep"])
            if k not in recs and (r.get("text") or "").strip():
                recs[k] = r
                refilled += 1
    ovp = ROOT / "config" / f"{cfg['wave']}-review.json"
    overrides = json.loads(ovp.read_text()) if ovp.exists() else {}
    rows = []
    for (s, m, rep), r in sorted(recs.items()):
        c = classify(r["text"])
        key = f"{s}|{m}|{rep}"
        if key in overrides:
            c.update(primary=overrides[key]["primary"], basis="hand review: " + overrides[key]["why"], reviewed=True)
        rows.append({"scenario": s, "model_key": m, "rep": rep, **c,
                     "mentions_valkey": bool(re.search(r"\bvalkey\b", r["text"], re.I)),
                     "mentions_redis": bool(re.search(r"\bredis\b", r["text"], re.I)),
                     "cites_license": bool(re.search(r"(?i)\b(licen[sc]e|licensed|licensing|BSD|SSPL|AGPL|RSAL|permissive)\b", r["text"])),
                     "cost_usd": r.get("cost_usd")})
    agg = wd / "aggregate"
    agg.mkdir(exist_ok=True)
    (agg / "classified.jsonl").write_text("".join(json.dumps(x) + "\n" for x in rows))
    by_model, by_scen = defaultdict(Counter), defaultdict(Counter)
    for x in rows:
        by_model[x["model_key"]][x["primary"]] += 1
        by_scen[x["scenario"]][x["primary"]] += 1
    review = [x for x in rows if x["conflict"] and not x.get("reviewed")]
    summary = {"coding": "v1 (tiered, code lines only)", "answers": len(rows), "primary": dict(Counter(x["primary"] for x in rows)),
               "mentions_valkey": sum(x["mentions_valkey"] for x in rows),
               "mentions_valkey_by_scenario": dict(Counter(x["scenario"] for x in rows if x["mentions_valkey"])),
               "by_model": {k: dict(v) for k, v in by_model.items()}, "by_scenario": {k: dict(v) for k, v in by_scen.items()},
               "basis": dict(Counter(x["basis"].split(":")[0] for x in rows)),
               "valkey_picks_citing_license": dict(Counter(x["scenario"] for x in rows if x["primary"] == "valkey" and x["cites_license"])),
               "hand_reviewed": sum(1 for x in rows if x.get("reviewed")), "open_conflicts": len(review), "refilled": refilled,
               "spend_usd": round(sum(float(x.get("cost_usd") or 0) for x in rows), 3)}
    (agg / "summary.json").write_text(json.dumps(summary, indent=1))
    print(json.dumps(summary, indent=1))
    for x in review:
        print("REVIEW", f"{x['scenario']}|{x['model_key']}|{x['rep']}", x["primary"], x["basis"], x["evidence"])


if __name__ == "__main__":
    main()
