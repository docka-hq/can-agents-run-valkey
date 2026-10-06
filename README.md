# Can agents run Valkey? Test kit

Agent tests for [Valkey](https://valkey.io), by [Docka](https://docka.ai). Real jobs an AI agent is asked to do on a Valkey server, graded on the server's end state, and the prompts that ask a model which store it would pick. Run them on any model with your own API key, add your own, and rerun when a model or Valkey changes.

Published results, October 2026: https://lab.docka.ai/can-agents-run-it/valkey/
Every answer and attempt from those runs: https://lab.docka.ai/can-agents-run-it/valkey/explore/
The frozen data behind them: https://github.com/docka-hq/cari-valkey-redis

## The jobs

Each Valkey job has a Redis twin as a baseline. The two prompts differ only in the product's name, version and config path.

| Job | Valkey | Redis baseline | Published, 5 models × 3 attempts |
|---|---|---|---|
| Add a shared cache with a 5-minute expiry in front of a slow service | `jobs/cache_valkey.yaml` | `jobs/cache_redis.yaml` | Valkey 15/15, Redis 15/15 |
| Build semantic search over 1,000 articles, running inside the server | `jobs/vector_valkey.yaml` | `jobs/vector_redis.yaml` | Valkey 15/15, Redis 15/15 |
| Copy production data off Redis 7.2, exactly | `jobs/migrate_valkey.yaml` | `jobs/migrate_redis.yaml` | Valkey 15/15, Redis 15/15 |
| Copy production data off Redis 8.10, where replication and DUMP/RESTORE do not work | `jobs/migrate8_valkey.yaml` | none | 11/15 |

The selection prompts in `selection/config.json` ask a model what it would use for a cache, a job queue, a semantic cache, a managed store on AWS, and a cache an agent runs itself. Published: Valkey in 0 of 75 answers to the first three, 16 of 25 on AWS, 19 of 25 for the agent.

## What is where

| Path | Contents |
|---|---|
| `inspect_task.py` | The runner, on Inspect AI. |
| `jobs/*.yaml` | One file per job: the prompt, the container, the starting state and the grader. Format in `jobs/README.md`. |
| `jobs/graders/` | The grader sources; each YAML carries its grader inline. |
| `jobs/reference/` | A correct solution per job and known wrong ones, to prove a grader. |
| `jobs/system_prompt.txt` | The system prompt every agent gets. |
| `selection/` | The selection prompts and the classifier that codes answers. |
| `images/` | Dockerfiles and assets for the job containers. |

## Run it

The jobs and the selection prompts run unchanged on [Inspect AI](https://inspect.aisi.org.uk), the open evaluation framework from the UK AI Security Institute. For a job, `inspect_task.py` starts the job's container, gives the agent our system prompt and two tools (`exec` runs a shell command in the container, `fetch_doc` reads a web page), and uses the job's own grader as the scorer. For selection, it sends each prompt as the only message and codes the answer with `selection/kv_classify.py`.

**1. Build the images** (Docker):

```bash
bash images/build.sh
```

Base images are pulled by tag. The digests of the published runs are in the frozen configs of [cari-valkey-redis](https://github.com/docka-hq/cari-valkey-redis/tree/main/jobs/config).

**2. Install and set a key:**

```bash
pip install inspect_ai pyyaml httpx openai    # add anthropic to run Claude
export OPENROUTER_API_KEY=...
```

**3. Prove a grader, with no model calls.** A reference solution plays the agent:

```bash
inspect eval inspect_task.py@cari_job -T job=jobs/cache_valkey.yaml \
    -T solution=jobs/reference/cache_ok.sh --model mockllm/model          # expect 1.0
inspect eval inspect_task.py@cari_job -T job=jobs/cache_valkey.yaml \
    -T solution=jobs/reference/cache_no_invalidate.sh --model mockllm/model   # expect 0.0
```

**4. Run an agent, three attempts:**

```bash
inspect eval inspect_task.py@cari_job -T job=jobs/cache_valkey.yaml \
    --model openrouter/deepseek/deepseek-v4.1-flash --epochs 3
inspect view     # every attempt: the messages, each command, the grader's verdict
```

**5. Ask the selection prompts, or your own:**

```bash
inspect eval inspect_task.py@cari_selection --model openrouter/openai/gpt-6-sol --epochs 5
inspect eval inspect_task.py@cari_selection -T prompts=my_prompts.json --model openrouter/openai/gpt-6-sol --epochs 5
```

`my_prompts.json` is a list of entries in the format of `selection/config.json`, for example the job-queue prompt with Python changed to Java:

```json
[{"id": "java_job_queue", "turf": "job queues for Java",
  "prompt": "I need a background job queue for my Java web app: send emails, resize images, retry jobs that fail. What do you set up?"}]
```

The score is the share of answers that set up Valkey. The store each answer set up, and why, is in the log.

## How close this is to the published runs

The published runs used Docka's own harness, which is not public. This runner keeps what matters for the verdict: the same prompts, images, graders, system prompt, tools, tool descriptions and output limits. It differs in three places: no browser fallback for pages that render only in JavaScript, no per-host rate limit, and the runaway guard counts messages rather than tool calls (3M tokens, 60 minutes and about 500 tool calls per attempt).

Checked on 2026-10-05: nine grader proofs across six job files gave the expected verdicts under this runner, and DeepSeek V4.1 Flash on the cache job gave 3 of 3, as in the published run, with longer paths (a median of 15 tool calls against 8). Expect the same verdicts, not identical numbers.

## Add a job or a prompt

See [CONTRIBUTING.md](CONTRIBUTING.md). A job counts once its grader passes a correct solution and fails known wrong ones, and step 3 above runs exactly that check.

## Licence

Code (`*.py`, `*.sh`, Dockerfiles): MIT, see [LICENSE](LICENSE). Prompts and data: CC BY 4.0, see [LICENSE-DATA.md](LICENSE-DATA.md).

Questions: eugene@docka.ai
