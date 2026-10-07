# Selection: prompts and coding rules

`config.json` holds the five published prompts (`scenarios`: `id`, `turf`, `prompt`) and the models they were asked of (`park`). The protocol: each prompt as the only message, no system prompt, one turn, the provider's default sampling, five answers per model and prompt.

## How an answer is counted

`kv_classify.py` reads each answer and records what it **sets up**, in this order:

1. **Code first**: a container image (`image: valkey/valkey`, `docker run redis`), a Terraform engine or variable default, an install line, a Helm chart. Comments and fallback tips are ignored.
2. **Then a named managed service** (for example ElastiCache for Valkey).
3. **Then the line that states the choice** ("I'd use Redis"), read in context.
4. A client library never counts: the redis Python client talks to Valkey too.

v2 (2026-10-06) adds two rules. A managed service named only as a fallback ("If unavailable, use ElastiCache for Redis") is not the pick. And an answer whose deciding evidence and stated choice name different stores is marked `conflict` for a second look, instead of being coded silently. On the 125 published answers, v2 gives every answer the script coded the same code as v1, and flags two for a second look; both keep their published code (see the review note in cari-valkey-redis).

    python3 selection/kv_classify.py answer.md           # code one answer
    python3 selection/test_classify.py ../cari-valkey-redis   # regression cases + every published answer

The published counts also include 8 answers coded by hand where the evidence conflicted; they are in [cari-valkey-redis](https://github.com/docka-hq/cari-valkey-redis/blob/main/selection/hand-review.json), each with its reason. Run alone, the script may code those 8 differently.
