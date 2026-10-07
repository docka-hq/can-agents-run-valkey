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

These are graders v2 (2026-10-06, revised 2026-10-07). The published runs used v1, kept in [cari-valkey-redis](https://github.com/docka-hq/cari-valkey-redis); v2 closes the gaps three rounds of outside review found, marked below. What they still cannot see is listed in the main README.

- **Cache** (`grade_cache.py`): two reads of a product in two processes make one call to the inventory service; **the cached product itself expires in 240 to 300 seconds**, found by content wherever it is stored: a whole key (plain, JSON, pickle, zlib, gzip or base64) or one field of a hash, where the sooner of the key's and the field's expiry counts (v1: any key with such a TTL); after `update_price` a new process reads the new price and the inventory holds it; return values are unchanged; the inventory service files are untouched.
- **Semantic search** (`grade_vector.py`): **all 1,000 ids in the article file are stored in the server** (in key names, hash fields or values, JSON, strings, vector-set elements or their attributes; plain, compressed or base64; ids not in the file do not count; v1: not counted); with the article file moved away, `search.py` returns **the true top 5 in order** for five held-out queries (v1: top 1 exact and 4 of 5); and the search runs inside the server, checked by behaviour on the same runs that are scored: **the server's command counters show a search command (FT.SEARCH, FT.AGGREGATE, FT.HYBRID or VSIM) for every query**, **the server sends `search.py` at most 100 KB per query** (a search reply is a few KB; computing the similarity outside the server needs every vector, over 256 KB, however it is fetched), and **once the top article for query 1 is removed from the server, `search.py` runs cleanly and returns the new top 5** (a local copy of the vectors keeps returning the removed article). The grader finds that article by its id: a key named by it, a record that holds that id and no other, a vector-set element named by it or carrying it in its attributes, or its entries in a hash shared by many articles. **If it cannot find the article, the job fails**, so keep each article's id with its record. v1 only looked for a search command.
- **Migration** (`grade_migrate.py`, `grade_migrate8.py`): every key in every database on the new server, with the same type, value and remaining TTL, stream entries, consumer groups and pending entries; the new server is a writable primary; the old server is unchanged, **including the deadline of every expiring key and, on servers with field expiry, of every expiring hash field** (v1: only whether a key expires). The runner takes the deadline baseline before the agent starts and hands it to the grader after the agent stops; **a missing baseline fails**.

A grader that crashes, or prints no valid verdict, is reported as an error, never as a fail.

## Proofs

`reference/` holds correct solutions and known wrong ones. `python3 prove_all.py` runs every job against the untouched environment, each correct solution and each wrong one, with no model calls, and checks the verdicts: 61 checks. A check counts only if the grader returns a valid verdict and the solution itself ran without error.

| Job | Correct solutions (must pass) | Known wrong solutions (must fail) |
|---|---|---|
| Cache | the reference; **base64 JSON**; **one shared hash with an expiry per field**; **the same hash also kept for an hour** | no invalidation; a per-process cache; **a product cached without expiry next to a decoy key with a 300 s TTL**; **an expiry of 300 s per field inside a hash that expires after 30 s** |
| Semantic search | the reference; **two indexes of 500**; **SCAN to find the index name**; **opaque keys, with the id only in a base64 field** | brute-force search in Python; **the right top 1 with the rest reversed**; **one search command for show, then the similarity computed in Python** from vectors read with HGET, **with bulk MGET**, or **from one large value**; **a local copy of the vectors**, **the same under opaque keys**, and **one that exits with an error once an article is missing**; **900 articles plus 100 made-up ids** |
| Migration from 7.2 | the reference | a copy that drops expiries; a new server left as a replica; **deadlines on the old server moved before copying** |
| Migration from 8.10 | the reference | a copy without field expiry; REPLICAOF only; **key and field deadlines on the old server moved before copying** |

The solutions in bold were written after the outside reviews: each wrong one exposes a gap the earlier grader had, each correct one a shape it wrongly rejected.
