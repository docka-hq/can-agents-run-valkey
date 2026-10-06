# Selection: prompts and coding rules

`config.json` holds the five published prompts (`scenarios`: `id`, `turf`, `prompt`) and the models they were asked of (`park`). The protocol: each prompt as the only message, no system prompt, one turn, the provider's default sampling, five answers per model and prompt.

## How an answer is counted

`kv_classify.py` reads each answer and records what it **sets up**, in this order:

1. **Code first**: a container image (`image: valkey/valkey`, `docker run redis`), a Terraform engine or variable default, an install line, a Helm chart. Comments and fallback tips are ignored.
2. **Then a named managed service** (for example ElastiCache for Valkey).
3. **Then the line that states the choice** ("I'd use Redis"), read in context.
4. A client library never counts: the redis Python client talks to Valkey too.

The published counts also include 8 answers coded by hand where the evidence conflicted; they are in [cari-valkey-redis](https://github.com/docka-hq/cari-valkey-redis/blob/main/selection/hand-review.json), each with its reason. Run alone, the script may code those 8 differently.
