# Job format

One YAML file per job and product. Each file carries its grader inline, so a job file is self-contained. The readable sources are in `graders/`; after changing one, run `python3 jobs/build_jobs.py` to rebuild every YAML that uses it, then `python3 prove_all.py`. `build_jobs.py --check` fails if a YAML is out of date.

```yaml
id: cache_valkey                 # job_product
difficulty: medium
surface: valkey                  # the product under test
task_version: 1
prompt: |                        # exactly what the agent receives as the user message
  The storefront code in /app reads products through ...
sandbox:
  base_image: cari-kv-app:valkey-9.1   # built by images/build.sh
  preconditions:                       # the starting state, in words
  - Valkey 9.1.2 on 127.0.0.1:6379, empty
  - inventory service on 127.0.0.1:8000 (300 ms per GET)
  network: bridge
  entrypoint_mode: image
  ready_cmd: test -f /run/kv-ready     # the run starts only once this succeeds
  memory: 1g
success:
  kind: shell_command
  payload: |                           # the grader, run inside the container after the agent stops
    /opt/kv-grader/bin/python - <<'KVGRADER_EOF'
    CFG = {...}                        # per-product settings
    ...                                # contents of graders/grade_*.py
    KVGRADER_EOF
```

The grader runs with its own Python (`/opt/kv-grader`), so nothing the agent installs or removes changes how it checks. It prints one JSON line, `{"pass": true|false, "reasons": [...], "facts": {...}}`, and exits 0 on a pass. `facts` records what it measured, so a reader can see why a verdict came out the way it did.

Every attempt also gets the same system prompt (`system_prompt.txt`) and two tools: `exec(cmd)` runs a shell command in the container, `fetch_doc(url)` reads a web page.

## What each grader checks

These are graders v2 (2026-10-06). The published runs used v1, kept in [cari-valkey-redis](https://github.com/docka-hq/cari-valkey-redis); v2 closes four gaps an outside review found in v1, marked below.

- **Cache** (`grade_cache.py`): two reads of a product in two processes make one call to the inventory service; **every key that holds the product has a TTL between 240 and 300 seconds** (v1: any key with such a TTL); after `update_price` a new process reads the new price and the inventory holds it; return values are unchanged; the inventory service files are untouched.
- **Semantic search** (`grade_vector.py`): with the article file moved away, `search.py` returns **the true top 5 in order** for five held-out queries (v1: top 1 exact and 4 of 5); **an index with at least 1,000 documents or a vector set with at least 1,000 elements exists** (v1: not counted); the server's MONITOR shows a search command (FT.SEARCH, FT.AGGREGATE or VSIM) while `search.py` runs, and **`search.py` neither scans the keyspace nor reads more than 50 items one by one** (v1: a search command was enough).
- **Migration** (`grade_migrate.py`, `grade_migrate8.py`): every key in every database on the new server, with the same type, value and remaining TTL, stream entries, consumer groups and pending entries; the new server is a writable primary; the old server is unchanged, **including the deadline of every expiring key and, in wave 2, of every expiring hash field** (v1: only whether a key expires). The runner takes the deadline baseline before the agent starts and hands it to the grader after the agent stops.

## Proofs

`reference/` holds one correct solution per job (`*_ok`) and known wrong ones. `python3 prove_all.py` runs every job against the untouched environment, the correct solution and each wrong one, with no model calls, and checks the verdicts: 35 checks.

| Job | Known wrong solutions |
|---|---|
| Cache | no invalidation, a per-process cache, **a product cached without expiry next to a decoy key with a 300 s TTL** |
| Semantic search | brute-force search in Python, **the right top 1 with the rest reversed**, **one search command for show and the similarity computed in Python from vectors read back** |
| Migration from 7.2 | a copy that drops expiries, a new server left as a replica, **deadlines on the old server moved before copying** |
| Migration from 8.10 | a copy without field expiry, REPLICAOF only, **key and field deadlines on the old server moved before copying** |

The solutions in bold were written for v2: each one passes v1 and fails v2.
