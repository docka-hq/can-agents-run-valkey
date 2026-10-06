# Job format

One YAML file per job and product. Each file carries its grader inline; the readable sources are in `graders/`, and a change to one must be copied into every YAML that uses it.

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

- **Cache** (`grade_cache.py`): two reads of a product in two processes make one call to the inventory service; a cache key lives in the provided server with a TTL between 240 and 300 seconds; after `update_price` a new process reads the new price and the inventory holds it; return values are unchanged; the inventory service files are untouched.
- **Semantic search** (`grade_vector.py`): with the article file moved away, `search.py` returns the right top 5 for held-out queries (exact top 1, at least 4 of 5), and the server's MONITOR shows the search ran inside it (FT.SEARCH, FT.AGGREGATE or VSIM).
- **Migration** (`grade_migrate.py`, `grade_migrate8.py`): every key in every database on the new server, with the same type, value and remaining TTL, stream entries, consumer groups and pending entries; the new server is a writable primary; the old server is unchanged. Wave 2 also checks per-field expiry on 40 hashes.

## Proofs

`reference/` holds one correct solution per job (`*_ok`) and known wrong ones: no invalidation and a per-process cache (cache), brute-force search in Python (search), a copy that drops expiries and a new server left as a replica (migration), a copy without field expiry and a REPLICAOF-only attempt (wave 2). Before the runs, every grader failed on the untouched environment and on each wrong solution and passed on the correct one, for both products.
