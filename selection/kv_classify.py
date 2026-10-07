#!/usr/bin/env python3
"""Selection coding, v2 (2026-10-06): what does each answer actually SET UP?

v2 changes, after an outside review. The published counts were coded with v1 (cari-valkey-redis), and v2 codes
every published answer the same way:
  - tier 2 skips a managed service named in a fallback sentence ("Use Valkey. If unavailable, use ElastiCache
    for Redis" was coded Redis by v1);
  - an answer whose deciding evidence and stated choice name different stores is flagged as a conflict for hand
    review, instead of being coded silently (an optional Redis next to a recommended RabbitMQ);
  - run as a script, it codes text from files or stdin.

v1 notes follow.

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


FALLBACK = re.compile(r"(?i)\b(if (?:it'?s |that'?s |\w+ is )?(?:unavailable|not available|not supported)|"
                      r"if you (?:can'?t|cannot|prefer|need|want)|otherwise|alternatively|as a fallback|fall ?back|"
                      r"or use|if your (?:provider|region|cloud|platform))\b")


def sentence_at(text: str, start: int, end: int) -> str:
    """The sentence (or line) around a match, so a cue in one sentence does not reach into the next."""
    a = max(text.rfind(c, 0, start) for c in ".!?;\n") + 1
    after = [i for i in (text.find(c, end) for c in ".!?;\n") if i != -1]
    return text[a:min(after) if after else len(text)]


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
            if FALLBACK.search(sentence_at(text, m.start(), m.end())):
                continue  # v2: a service named only as the fallback is not the pick
            t2.append((m.start(), norm(m.group(1))))
    return sorted(t1), sorted(t2)


def store_of(word: str) -> str:
    w = word.lower()
    if w.startswith("redis"):
        return "redis"
    if w in ("valkey", "memcached"):
        return w
    return "other:" + w


ADJECTIVE = re.compile(r"(?i)^[-\s]*(?:protocol|compatible|wire|like|style|api|clients?)\b")


def stated_choice(text: str):
    """The store named in the first sentence that reads as a pick, or None. Checked sentence by sentence, so a
    fallback later in a paragraph does not hide the pick; "Redis-compatible" and "Redis-protocol" are not picks."""
    for ln in (text or "").splitlines():
        if not TRIGGER.search(ln):
            continue
        for sent in re.split(r"(?<=[.!?;])\s+", ln):
            if FALLBACK.search(sent):
                continue
            for m in STORE_RE.finditer(sent):
                if not ADJECTIVE.match(sent[m.end():]):
                    return store_of(m.group(1))
    return None


def classify(text: str) -> dict:
    t1, t2 = tier_hits(text)
    said = stated_choice(text)
    for tier, hits in (("code", t1), ("managed service named", t2)):
        prods = [p for _, p in hits if p in PRODUCTS]
        if prods:
            # v2: deciding evidence and the stated choice disagree -> hand review, not a silent code
            disagree = said is not None and said != prods[0] and not (said in PRODUCTS and said in prods)
            return {"primary": prods[0], "basis": tier, "evidence": sorted(set(prods)),
                    "conflict": len(set(prods)) > 1 or disagree, "stated_choice": said}
    lines = code_lines(text)
    imp = []
    for ln in lines:
        for pat, prod in IMPORTS:
            if re.search(pat, ln):
                imp.append(prod)
    # the recommendation line: the first line that both reads as a pick and names a store (frameworks like
    # Celery or Dramatiq are not stores: in "Celery + Redis" the store is Redis)
    pick = said  # v2: the same sentence-level, fallback-aware reading used for the conflict check
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
    """Code text from files given as arguments, or from stdin: python3 kv_classify.py answer.md"""
    import sys
    paths = sys.argv[1:]
    texts = [(pth, pathlib.Path(pth).read_text()) for pth in paths] if paths else [("stdin", sys.stdin.read())]
    for name, text in texts:
        print(json.dumps({"file": name, **classify(text)}))


if __name__ == "__main__":
    main()
