#!/usr/bin/env python3
"""Rebuild the grader inside every job YAML from jobs/graders/*.py.

Each YAML carries its grader inline, so a job file is self-contained. After changing a grader source, run this,
then prove the result: python3 prove_all.py. The payload layout is the one the published runs used: the dump helper
the migration graders share with the image, then the job's `CFG = {...}` line, then the grader.

    python3 jobs/build_jobs.py          # rewrite jobs/*.yaml
    python3 jobs/build_jobs.py --check  # exit 1 if any YAML is out of date
"""
from __future__ import annotations

import json
import pathlib
import re
import sys

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
GRADER = {"cache": "grade_cache.py", "vector": "grade_vector.py", "migrate": "grade_migrate.py", "migrate8": "grade_migrate8.py"}
PRELUDE = {"migrate": ROOT / "images/assets/mig/kvdump.py", "migrate8": ROOT / "images/assets/mig8/kvdump2.py"}


def payload(grader: str, cfg: dict, prelude: str = "") -> str:
    code = (prelude + "\n" if prelude else "") + f"CFG = {json.dumps(cfg, sort_keys=True)}\n\n" + grader
    assert "KVGRADER_EOF" not in code
    return f"/opt/kv-grader/bin/python - <<'KVGRADER_EOF'\n{code}\nKVGRADER_EOF\n"


def rebuilt(path: pathlib.Path) -> str:
    text = path.read_text()
    head, sep, tail = text.partition("success:\n")
    assert sep, f"{path.name}: no success block"
    cfg = json.loads(re.search(r"^\s*CFG = (\{.*\})$", tail, re.M).group(1))
    job = path.stem.rsplit("_", 1)[0]
    pl = payload((HERE / "graders" / GRADER[job]).read_text(), cfg, PRELUDE[job].read_text() if job in PRELUDE else "")
    body = "".join(("    " + ln) if ln.strip() else "\n" for ln in pl.splitlines(True))
    return head + "success:\n  kind: shell_command\n  payload: |\n" + body


def main() -> None:
    check = "--check" in sys.argv
    stale = []
    for y in sorted(HERE.glob("*.yaml")):
        new = rebuilt(y)
        if new != y.read_text():
            stale.append(y.name)
            if not check:
                y.write_text(new)
    if check and stale:
        raise SystemExit(f"out of date: {', '.join(stale)} (run python3 jobs/build_jobs.py)")
    print(("rebuilt: " if not check else "up to date") + (", ".join(stale) if stale and not check else ""))


if __name__ == "__main__":
    main()
