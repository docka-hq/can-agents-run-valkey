# Can agents run Valkey? Test kit

A public kit for rerunning the tasks behind [Can agents run Valkey?](https://lab.docka.ai/can-agents-run-it/valkey/) with [Inspect AI](https://inspect.aisi.org.uk), on any model with your own API key, and for adding your own. By [Docka](https://docka.ai).

- Published results, October 2026: https://lab.docka.ai/can-agents-run-it/valkey/
- Every answer and attempt from those runs: https://lab.docka.ai/can-agents-run-it/valkey/explore/
- The frozen data, prompts and graders behind them: https://github.com/docka-hq/cari-valkey-redis

## The jobs

Real jobs an AI agent is asked to do on a server, graded on the server's end state after the agent stops. Each Valkey job has a Redis twin as a baseline; the two prompts differ only in the product's name, version and config path.

| Job | Valkey | Redis baseline | Published, 5 models × 3 attempts |
|---|---|---|---|
| Add a shared cache with a 5-minute expiry in front of a slow service | `jobs/cache_valkey.yaml` | `jobs/cache_redis.yaml` | Valkey 15/15, Redis 15/15 |
| Build semantic search over 1,000 articles, running inside the server | `jobs/vector_valkey.yaml` | `jobs/vector_redis.yaml` | Valkey 15/15, Redis 15/15 |
| Copy production data off Redis 7.2, exactly | `jobs/migrate_valkey.yaml` | `jobs/migrate_redis.yaml` | Valkey 15/15, Redis 15/15 |
| Copy production data off Redis 8.10, where replication and DUMP/RESTORE do not work | `jobs/migrate8_valkey.yaml` | none | 11/15 |

The selection prompts in `selection/config.json` ask a model what it would use for a cache, a job queue, a semantic cache, a managed store on AWS, and a cache an agent runs itself. Published: Valkey in 0 of 75 answers to the first three (two of those prompts name Python), 17 of 25 on AWS (corrected from 16 after an outside review), 19 of 25 for the agent. The prompts differ in more than who is asking.

## What is where

| Path | Contents |
|---|---|
| `inspect_task.py` | The runner: `cari_job` for a job, `cari_selection` for the selection prompts. |
| `prove_all.py` | Proves every grader with no model calls and no API key: 96 checks. |
| `jobs/*.yaml` | One file per job: prompt, container, starting state, grader. Format and grader rules in `jobs/README.md`. |
| `jobs/graders/`, `jobs/build_jobs.py` | Grader sources, and the script that inlines them into the YAML files. |
| `jobs/reference/` | A correct solution per job and known wrong ones. |
| `selection/` | The selection prompts, the classifier and its regression tests. |
| `images/` | Dockerfiles, assets, `build.sh`, and `images.json`, the record of the published build. |
| `requirements.txt` | The Python packages, at the versions tested. |

## Set up

You need Linux or macOS, Docker with Compose v2 and the daemon running, Python 3.10 or newer (tested on 3.12), about 3 GB of disk for the images, and 1 CPU and 1 GB of memory for each attempt running at once (`prove_all.py` runs 4 at a time).

```bash
git clone https://github.com/docka-hq/can-agents-run-valkey && cd can-agents-run-valkey
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
bash images/build.sh        # builds the 5 images, starts each one, compares versions with the published build
python3 prove_all.py        # 96 checks, no API key, no model calls
```

`build.sh` pins base images by digest and Python packages by version, so server versions and packages match the published build; Debian packages are not pinned, so image ids differ. `prove_all.py` must report 96 of 96 before you trust a run.

## Run an agent

```bash
export OPENROUTER_API_KEY=...      # or ANTHROPIC_API_KEY for Claude
inspect eval inspect_task.py@cari_job -T job=jobs/cache_valkey.yaml \
    --model openrouter/deepseek/deepseek-v4.1-flash --epochs 3
inspect view                       # every attempt: the messages, each command, the grader's verdict
```

An attempt whose grader breaks, with no valid verdict, is recorded as an error and left out of the score, never counted as the model's failure.

The models of the published runs, as Inspect model ids:

| Model | `--model` | Published settings |
|---|---|---|
| Claude Opus 5.5 | `anthropic/claude-opus-5-5` | Anthropic API, adaptive thinking, effort medium |
| GPT-6 Sol | `openrouter/openai/gpt-6-sol` | OpenRouter, provider defaults |
| DeepSeek V4.1 Flash | `openrouter/deepseek/deepseek-v4.1-flash` | OpenRouter, provider defaults |
| GLM-5.3-Flash | `openrouter/z-ai/glm-5.3-flash` | OpenRouter, provider defaults |
| MiMo-V2.6-Flash | `openrouter/xiaomi/mimo-v2.6-flash` | OpenRouter, provider defaults |

All jobs for one model, three attempts each:

```bash
for job in jobs/*.yaml; do
  inspect eval inspect_task.py@cari_job -T job=$job --model openrouter/z-ai/glm-5.3-flash --epochs 3
done
```

## Ask the selection prompts, or your own

```bash
inspect eval inspect_task.py@cari_selection --model openrouter/openai/gpt-6-sol --epochs 5
inspect eval inspect_task.py@cari_selection -T prompts=my_prompts.json --model openrouter/openai/gpt-6-sol --epochs 5
```

`my_prompts.json` is a list of entries in the format of `selection/config.json`, for example the job-queue prompt with Python changed to Java:

```json
[{"id": "java_job_queue", "turf": "job queues for Java",
  "prompt": "I need a background job queue for my Java web app: send emails, resize images, retry jobs that fail. What do you set up?"}]
```

The score is the share of answers that set up Valkey; the store each answer set up, and why, is in the log. Answers whose code and stated choice disagree are marked `conflict` for a second look.

## How close this is to the published runs

The published runs used Docka's own harness, which is not public.

**The same:** the prompts, the images (same server versions and Python packages), the system prompt, the two tools with the same descriptions and output limits (`exec`: 4,000 characters of stdout, 1,500 of stderr; `fetch_doc`: 6,000 characters of page text, extracted the same way), 64,000 output tokens per turn, one nudge after a turn that comes back empty or cut off, and the runaway guard's limits.

**Different:**

1. **The graders are v2.** They close the gaps seven rounds of outside review found: the cache's expiry is checked on each product read (whole key or hash field, plain, compressed or base64, the sooner expiry counting), before and after a price update, a price update must reach the cache for both products, `update_price` must still return None, and every product of the catalog must come back as itself; search wants exactly five ids, the true top 5 in order, all 1,000 supplied articles stored, each with its own embedding (in any database), and 98 sampled articles findable by their own embedding, and it checks behaviour on the runs it scores (a search command or a Lua script in the server for every query, at most 100 KB sent to `search.py` per query, and a correct answer once the top article is removed from the server); and the old server's expiry deadlines must not move. Re-checked from what the published runs recorded, every published pass also meets the cache and search-order rules; every published search pass stored each article's embedding with its id, in a format the grader reads (FLOAT32 or FLOAT64 bytes, a JSON array, a vector set), and its `search.py` asks the server for 5 results and prints one id for each. The other checks need a running container and cannot be re-run on old attempts. Details: `jobs/README.md` and the review note in cari-valkey-redis.
2. **Model settings.** The published runs gave Claude adaptive thinking at effort medium and left the others at provider defaults. This runner leaves every model at Inspect's defaults; pass reasoning options yourself to match.
3. **No browser fallback** for pages that render only in JavaScript, and **no per-host rate limit** for `fetch_doc`.
4. **The runaway guard counts messages**, not tool calls: 3M tokens, 60 minutes, about 500 tool calls per attempt.

**Checked on 2026-10-07:** 96 of 96 grader proofs on the images the published runs used, every one with a valid grader verdict and a cleanly running solution; the earlier 35-check set also passed on images freshly built with `build.sh` (the images have not changed since). One live check: DeepSeek V4.1 Flash on the cache job gave 3 of 3, as in the published run (on 2026-10-05, before the nudge and the per-turn limit were added). This is not yet validated across all models and jobs: expect close numbers, not identical ones.

## What the graders cannot see

The graders check the end state and how `search.py` behaves, not how an answer was meant to work, so an answer built to pass the checks without doing the job can still pass. The cases we know of:

- **Search: where the similarity is computed.** The three behaviour checks catch every way of computing outside the server we have tried (`prove_all.py` holds them), not every way there is. A `search.py` that takes a few hundred candidates from a server-side search and re-ranks them itself passes, and so does one that answers from a local copy and asks the server only which articles still exist.
- **Search: the 100 KB limit is a heuristic.** It separates a search reply (2 to 5 KB here) from reading every vector back (over 256 KB). It counts everything the server sends during the query, so another process still using the server at grading time adds to it.
- **Search: each article's id must be stored with its embedding.** The count of articles reads both from the same record: a hash, JSON document or string, one entry of a hash shared by many articles, or a vector-set element named by the id or carrying it in its attributes. An answer that stores vectors under numbers and keeps the ids elsewhere, in a separate list for example, fails the count, although its search may be correct. Every published search pass stored each article's embedding with its id.
- **Search: whether every article can be found is sampled.** 98 articles are asked for by their own embedding: where loaders slip by one (the last of every batch of 50, 64, 100, 128, 256 or 512 and the first of the next), the first and the last, and 30 at random. An index that leaves out other articles passes.
- **Search: several articles held in one string or JSON document are not counted.** Articles are read from records, from hashes and lists that hold several records, and from vector sets; one document holding many articles fails the count, although a Lua script could search it inside the server.
- **Cache: the product is found by its name.** In a product split across several keys, only the key holding the name has its expiry checked. A product compressed with something outside Python's standard library (zstd, lz4, snappy, brotli) is not recognised, and fails.

If you find another, open an issue, or add a solution that demonstrates it to `jobs/reference/`, with or without the fix (see CONTRIBUTING.md).

## Add a job or a prompt

See [CONTRIBUTING.md](CONTRIBUTING.md). A job counts once its grader passes a correct solution and fails known wrong ones in `prove_all.py`.

## Licence

Code (`*.py`, `*.sh`, Dockerfiles): MIT, see [LICENSE](LICENSE). Prompts and data: CC BY 4.0, see [LICENSE-DATA.md](LICENSE-DATA.md).

Questions: eugene@docka.ai
