# Contributing

Two kinds of contribution are useful: a **selection prompt** (what does a model pick when nobody names a product) and a **job** (can an agent finish this on a Valkey server). Open an issue or a pull request with either.

## A selection prompt

Add an entry in the format of `selection/config.json`:

```json
{"id": "node_job_queue", "turf": "job queues for Node.js", "prompt": "I need a background job queue for my Node.js app: send emails, resize images, retry jobs that fail. What do you set up?"}
```

- The prompt must not name the products being compared, or anything that implies one of them.
- Write it the way a developer, or an agent, would actually ask.
- Say in `turf` what it tests.
- A variant of an existing prompt (another language, the same need asked by a person and by an agent) is the most useful kind: it isolates one difference.

Try it before sending: `inspect eval inspect_task.py@cari_selection -T prompts=my_prompts.json --model ... --epochs 5`. Answers are coded by the rules in `selection/README.md`; if you change the classifier, `python3 selection/test_classify.py ../cari-valkey-redis` must still pass. If a prompt needs a different rule, say which and why.

## A job

A job is one YAML file in `jobs/`, in the format described in `jobs/README.md`, plus what it needs:

1. **The prompt.** If the job has a Redis twin, the two prompts differ only in the product's name, version and config path.
2. **A container image** with the starting state: a Dockerfile and assets in `images/`, added to `images/build.sh`.
3. **A grader** that runs inside the container after the agent stops, checks the end state, prints one JSON line `{"pass": ..., "reasons": [...], "facts": {...}}` and exits 0 on a pass. Keep its source in `jobs/graders/` and run `python3 jobs/build_jobs.py` to inline it.
4. **Proofs**: one correct solution that passes and at least one known wrong solution that fails, in `jobs/reference/`. Add them to `prove_all.py`, run it (no model calls), and include the output. A grader that is not proven both ways is not used. A wrong solution that exploits a specific weakness of the grader is the most useful kind.

The grader checks what the prompt asks for, nothing more, and never trusts what the agent says it did. No job may need a secret or an outside account.

## What happens next

We check the proofs, run the job on the same models with the same protocol, and publish the result, failures included. When a model or Valkey changes something these jobs depend on, we rerun them and publish before and after.
