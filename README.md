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

The selection prompts in `selection/config.json` ask a model what it would use for a cache, a job queue, a semantic cache, a managed store on AWS, and a cache an agent runs itself. Published: Valkey in 0 of 75 answers to the first three (two of those prompts name Python), 16 of 25 on AWS, 19 of 25 for the agent. The prompts differ in more than who is asking.

## What is where

| Path | Contents |
|---|---|
| `inspect_task.py` | The runner: `cari_job` for a job, `cari_selection` for the selection prompts. |
| `prove_all.py` | Proves every grader with no model calls and no API key: 35 checks. |
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
python3 prove_all.py        # 35 checks, no API key, no model calls
```

`build.sh` pins base images by digest and Python packages by version, so server versions and packages match the published build; Debian packages are not pinned, so image ids differ. `prove_all.py` must report 35 of 35 before you trust a run.

## Run an agent

```bash
export OPENROUTER_API_KEY=...      # or ANTHROPIC_API_KEY for Claude
inspect eval inspect_task.py@cari_job -T job=jobs/cache_valkey.yaml \
    --model openrouter/deepseek/deepseek-v4.1-flash --epochs 3
inspect view                       # every attempt: the messages, each command, the grader's verdict
```

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

1. **The graders are v2.** They close four gaps an outside review found in v1: the cache TTL is checked on the keys that hold the product, search wants the full top 5 in order and all 1,000 articles searchable and fails a `search.py` that reads the data back, and the old server's expiry deadlines must not move. Re-checked from what the published runs recorded, every published pass also meets the cache and search-order rules; the other checks need a running container and cannot be re-run on old attempts. Details: `jobs/README.md` and the review note in cari-valkey-redis.
2. **Model settings.** The published runs gave Claude adaptive thinking at effort medium and left the others at provider defaults. This runner leaves every model at Inspect's defaults; pass reasoning options yourself to match.
3. **No browser fallback** for pages that render only in JavaScript, and **no per-host rate limit** for `fetch_doc`.
4. **The runaway guard counts messages**, not tool calls: 3M tokens, 60 minutes, about 500 tool calls per attempt.

**Checked on 2026-10-06:** 35 of 35 grader proofs on the images the published runs used, and 35 of 35 on images freshly built with `build.sh`. One live check: DeepSeek V4.1 Flash on the cache job gave 3 of 3, as in the published run (on 2026-10-05, before the nudge and the per-turn limit were added). This is not yet validated across all models and jobs: expect close numbers, not identical ones.

## Add a job or a prompt

See [CONTRIBUTING.md](CONTRIBUTING.md). A job counts once its grader passes a correct solution and fails known wrong ones in `prove_all.py`.

## Licence

Code (`*.py`, `*.sh`, Dockerfiles): MIT, see [LICENSE](LICENSE). Prompts and data: CC BY 4.0, see [LICENSE-DATA.md](LICENSE-DATA.md).

Questions: eugene@docka.ai
