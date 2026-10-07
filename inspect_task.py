"""Run the CARI Issue 2 jobs with Inspect AI (https://inspect.aisi.org.uk), on your own machine and your own API keys.

    pip install inspect_ai pyyaml httpx openai    # openai: Inspect's OpenRouter provider; add anthropic for Claude
    # build the job images first: see images/ (cari-kv-app, cari-kv-mig, cari-kv-mig8)

    # an agent run, 3 attempts
    inspect eval inspect_task.py@cari_job -T job=jobs/cache_valkey.yaml \\
        --model openrouter/deepseek/deepseek-v4.1-flash --epochs 3

    # a grader proof, no model calls: run a reference solution instead of an agent
    inspect eval inspect_task.py@cari_job -T job=jobs/cache_valkey.yaml \\
        -T solution=jobs/reference/cache_ok.sh --model mockllm/model

    # which store a model picks: the five published prompts, or your own file in the same format
    inspect eval inspect_task.py@cari_selection --model openrouter/openai/gpt-6-sol --epochs 5
    inspect eval inspect_task.py@cari_selection -T prompts=my_prompts.json --model ... --epochs 5

The job YAML is used as it is: its prompt is the user message, its image runs the server under test, and its grader
runs inside the container after the agent stops. The agent gets the system prompt from the published runs and the
same two tools, with the same descriptions and the same output limits: exec(cmd) in the container and fetch_doc(url)
for web pages. Differences from the published harness: no browser fallback for pages that render only in
JavaScript, no per-host rate limit, and the runaway guard counts messages rather than tool calls. Expect close
numbers, not identical ones.
"""
from __future__ import annotations

import html as _html
import importlib.util
import json
import os
import pathlib
import tempfile
from html.parser import HTMLParser

import httpx
import yaml
from inspect_ai import Task, task
from inspect_ai.dataset import Sample
from inspect_ai.model import ChatMessageUser, GenerateConfig
from inspect_ai.scorer import CORRECT, INCORRECT, Score, Target, accuracy, mean, scorer
from inspect_ai.solver import Generate, TaskState, generate, solver, system_message, use_tools
from inspect_ai.tool import tool
from inspect_ai.util import sandbox

HERE = pathlib.Path(__file__).resolve().parent
SYSTEM_PROMPT = (HERE / "jobs" / "system_prompt.txt").read_text().strip()
PER_TURN_MAX_TOKENS = 64000          # as in the published runs (per_turn_max_tokens)
NUDGE = ("[Your previous turn contained no tool call and ended at the per-turn output limit or empty. "
         "Continue the job with the next tool call, or state clearly that the task is complete.]")
GRADER_PY = "/opt/kv-grader/bin/python"
IMAGE_PREFIX = os.environ.get("CARI_IMAGE_PREFIX", "")   # e.g. "test-" for images built with TAG_PREFIX=test-
DEADLINES = "/var/tmp/.kvg-old-deadlines.json"

# Old-server expiry deadlines, taken before the agent starts (migration jobs). Kept in the runner, outside the
# container, and written for the grader only after the agent has stopped.
SNAPSHOT = r"""
import json, redis
out = {"keys": {}, "fields": {}}
r0 = redis.Redis(port=6379, socket_timeout=10)
info = r0.info("server")
ver = tuple(int(x) for x in str(info.get("valkey_version") or info.get("redis_version")).split(".")[:2])
# hash-field expiry exists from Redis 7.4 and Valkey 9.0; on those servers a failure is an error, never "unsupported"
fields_supported = ver >= ((9, 0) if "valkey_version" in info else (7, 4))
out["server"] = {"name": "valkey" if "valkey_version" in info else "redis", "version": ".".join(map(str, ver)),
                 "field_expiry": fields_supported}
for dbname in (r0.info("keyspace") or {}):
    db = int(dbname[2:]); rd = redis.Redis(port=6379, db=db, socket_timeout=10)
    keys = list(rd.scan_iter(count=1000))
    p = rd.pipeline(transaction=False)
    for k in keys:
        p.execute_command("EXPIRETIME", k)
    for k, t in zip(keys, p.execute()):
        if int(t) > 0:
            out["keys"].setdefault(str(db), {})[k.hex()] = int(t)
    for k in keys:
        if not fields_supported or rd.type(k) != b"hash":
            continue
        fs = rd.hkeys(k)
        ts = rd.execute_command("HEXPIRETIME", k, "FIELDS", len(fs), *fs)
        fx = {f.hex(): int(t) for f, t in zip(fs, ts) if int(t) > 0}
        if fx:
            out["fields"].setdefault(str(db), {})[k.hex()] = fx
print(json.dumps(out))
"""

# The image's own CMD starts the servers and then sleeps, so no `command` here: Inspect's default
# (`tail -f /dev/null`) would leave them stopped. Open network, as in the published runs.
COMPOSE = """services:
  default:
    image: "{image}"
    x-default: true
    init: true
    mem_limit: {mem}
    cpus: {cpus}
    stop_grace_period: 1s
"""

# --- page text, as the published harness extracts it (docka/docfetch.py) -------------------------
_SKIP_TAGS = {"script", "style", "noscript", "nav", "header", "footer", "svg", "form"}
_BLOCK_TAGS = {"p", "div", "li", "h1", "h2", "h3", "h4", "h5", "h6", "br", "tr", "pre", "section", "td", "th"}
_CONTENT_TAGS = {"article", "main"}


class _TextExtractor(HTMLParser):
    def __init__(self, capture_all: bool = False) -> None:
        super().__init__()
        self._skip = 0
        self._in_content = 0
        self.capture_all = capture_all
        self.saw_container = False
        self.parts: list[str] = []

    def _capturing(self) -> bool:
        return self._skip == 0 and (self.capture_all or self._in_content > 0)

    def handle_starttag(self, tag, attrs) -> None:
        if tag in _CONTENT_TAGS:
            self._in_content += 1
            self.saw_container = True
        if tag in _SKIP_TAGS:
            self._skip += 1
        elif tag in _BLOCK_TAGS and self._capturing():
            self.parts.append("\n")

    def handle_endtag(self, tag) -> None:
        if tag in _SKIP_TAGS and self._skip:
            self._skip -= 1
        if tag in _CONTENT_TAGS and self._in_content:
            self._in_content -= 1

    def handle_data(self, data) -> None:
        if self._capturing() and data.strip():
            self.parts.append(data.strip())

    def text(self) -> str:
        out, blank = [], False
        for ln in (x.strip() for x in "\n".join(self.parts).split("\n")):
            if ln:
                out.append(ln)
                blank = False
            elif not blank:
                out.append("")
                blank = True
        return "\n".join(out).strip()


def html_to_text(page: str) -> str:
    p = _TextExtractor()
    p.feed(page)
    if not p.saw_container:
        p = _TextExtractor(capture_all=True)
        p.feed(page)
    return _html.unescape(p.text())


# --- the agent's two tools ---------------------------------------------------------------------------
@tool(name="exec")
def exec_tool():
    async def execute(cmd: str) -> str:
        """Run a shell command in your Linux environment; returns stdout, stderr, exit code.

        Args:
            cmd: The shell command to run.
        """
        r = await sandbox().exec(["bash", "-lc", cmd], timeout=600)
        return f"exit={r.returncode}\nstdout:\n{r.stdout[:4000]}\nstderr:\n{r.stderr[:1500]}"
    return execute


@tool(name="fetch_doc")
def fetch_doc_tool():
    async def execute(url: str) -> str:
        """Fetch a documentation page by URL and return its readable text.

        Args:
            url: Full https URL of the doc page.
        """
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=20.0) as client:
                r = await client.get(url, headers={"User-Agent": "docka/0.1 (+doc-eval harness)"})
        except httpx.HTTPError as e:
            return f"[fetch failed: {type(e).__name__}]"
        if r.status_code >= 400:
            return f"[fetch failed: {r.status_code}]"
        body = html_to_text(r.text) if "html" in r.headers.get("content-type", "") else r.text
        return body[:6000]
    return execute


# --- the agent loop, as in the published harness ----------------------------------------------------------
@solver
def cari_agent():
    """One model turn at a time. A turn without a tool call that came back empty or cut at the per-turn limit gets
    one nudge; a second such turn in a row ends the attempt. Any other turn without a tool call ends it."""
    async def solve(state: TaskState, generate: Generate) -> TaskState:
        nudged = False
        while True:
            state = await generate(state, tool_calls="single")
            out = state.output
            if out.message.tool_calls:
                nudged = False
                continue
            cut = out.stop_reason in ("max_tokens", "model_length") or not (out.completion or "").strip()
            if cut and not nudged:
                nudged = True
                state.messages.append(ChatMessageUser(content=NUDGE))
                continue
            return state
    return solve


@solver
def snapshot_old_server():
    async def solve(state: TaskState, generate: Generate) -> TaskState:
        r = await sandbox().exec([GRADER_PY, "-c", SNAPSHOT], timeout=300)
        if r.returncode != 0:
            raise RuntimeError(f"could not snapshot the current server's deadlines: {r.stderr[-300:]}")
        state.metadata["old_deadlines"] = r.stdout.strip()
        return state
    return solve


# --- the grader, inside the container ------------------------------------------------------
@scorer(metrics=[accuracy()])
def cari_grader():
    async def score(state: TaskState, target: Target) -> Score:
        if state.metadata.get("old_deadlines"):
            await sandbox().write_file(DEADLINES, state.metadata["old_deadlines"])
        r = await sandbox().exec(["bash", "-c", state.metadata["grader"]], timeout=1800)
        line = next((ln for ln in reversed(r.stdout.splitlines()) if ln.startswith("{")), "")
        try:
            verdict = json.loads(line)
        except json.JSONDecodeError:
            verdict = None
        if not isinstance(verdict, dict) or not isinstance(verdict.get("pass"), bool) \
                or verdict["pass"] != (r.returncode == 0):
            # no structured verdict, or one that contradicts the exit code: the grader broke, nothing was graded
            return Score(value=INCORRECT, explanation=f"GRADER ERROR (exit {r.returncode}): {r.stderr.strip()[-300:]}",
                         metadata={"grader_error": True, "solution_run": state.metadata.get("solution_run")})
        ok = verdict["pass"]
        return Score(value=CORRECT if ok else INCORRECT, explanation="; ".join(verdict.get("reasons") or []) or "pass",
                     metadata={"grader_error": False, "facts": verdict.get("facts"),
                               "solution_run": state.metadata.get("solution_run")})
    return score


@solver
def reference_solution(path: str):
    """Instead of an agent: copy a solution from jobs/reference into the container and run it (a grader proof)."""
    async def solve(state: TaskState, generate: Generate) -> TaskState:
        src = HERE / path
        dst = f"/tmp/kv_solution{src.suffix}"
        await sandbox().write_file(dst, src.read_text())
        r = await sandbox().exec(["python3" if src.suffix == ".py" else "bash", dst], timeout=600)
        state.metadata["solution_run"] = {"exit": r.returncode, "stdout": r.stdout[-1500:], "stderr": r.stderr[-1500:]}
        return state
    return solve


@task
def cari_job(job: str, solution: str | None = None) -> Task:
    spec = yaml.safe_load((HERE / job).read_text())
    box = spec["sandbox"]
    compose = pathlib.Path(tempfile.mkdtemp(prefix="cari-")) / "compose.yaml"
    compose.write_text(COMPOSE.format(image=IMAGE_PREFIX + box["base_image"], mem=box.get("memory", "1g"), cpus=box.get("cpus", 1.0)))
    ready = box.get("ready_cmd", "true")
    sample = Sample(id=spec["id"], input=spec["prompt"], metadata={"grader": spec["success"]["payload"]},
                    setup=f"for i in $(seq 600); do {ready} && exit 0; sleep 0.2; done; echo 'server not ready' >&2; exit 1")
    agent = [system_message(SYSTEM_PROMPT), use_tools([exec_tool(), fetch_doc_tool()]), cari_agent()]
    return Task(dataset=[sample], setup=[snapshot_old_server()] if spec["id"].startswith("migrate") else None,
                solver=reference_solution(solution) if solution else agent, scorer=cari_grader(),
                sandbox=("docker", str(compose)), config=GenerateConfig(max_tokens=PER_TURN_MAX_TOKENS),
                token_limit=3_000_000, message_limit=1002, time_limit=3600)


# --- selection: which store a model picks when nobody names one -------------------------------------------
def _load_classify():
    spec = importlib.util.spec_from_file_location("kv_classify", HERE / "selection" / "kv_classify.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.classify


@scorer(metrics=[mean()])
def store_pick():
    """Codes the answer with selection/kv_classify.py. The score is 1 when the answer sets up Valkey, so the mean is
    the Valkey share; the store each answer set up is in `answer`, the reason in `explanation`."""
    classify = _load_classify()

    async def score(state: TaskState, target: Target) -> Score:
        c = classify(state.output.completion or "")
        return Score(value=1.0 if c["primary"] == "valkey" else 0.0, answer=c["primary"],
                     explanation=f"{c['primary']} ({c['basis']})", metadata=c)
    return score


@task
def cari_selection(prompts: str = "selection/config.json") -> Task:
    """Each prompt as the only message, no system prompt, one turn, the provider's default sampling.
    `prompts` is selection/config.json or a JSON list of entries in its format: {"id", "turf", "prompt"}."""
    data = json.loads((HERE / prompts).read_text())
    items = data["scenarios"] if isinstance(data, dict) else data
    return Task(dataset=[Sample(id=x["id"], input=x["prompt"], metadata={"turf": x.get("turf", "")}) for x in items],
                solver=generate(), scorer=store_pick(), config=GenerateConfig(max_tokens=16000))

