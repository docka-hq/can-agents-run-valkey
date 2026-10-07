#!/usr/bin/env python3
"""Prove every grader, with no model calls and no API key.

A check counts only if the grader returns a valid verdict and, when a solution plays the agent, the solution itself
runs without error: a crash is reported as an error, never as an expected fail.

For each job, three kinds of check:
  - the untouched environment (an agent that does nothing) must FAIL,
  - every correct solution must PASS: the reference one, and other valid shapes the grader must not reject
    (a cache in base64 or in hash fields with their own expiry, search over two indexes, SCAN to find an index),
  - every known-wrong solution in jobs/reference/ must FAIL, including the ones that exploit gaps found by outside
    reviews (cache_decoy_ttl, vector_shuffled_tail, vector_*client_side, vector_local_copy, *_moved_deadlines).

    python3 prove_all.py            # all 49 checks, 4 at a time
    python3 prove_all.py cache      # only jobs whose name starts with "cache"

Needs the job images (bash images/build.sh), a running Docker daemon with Compose v2, and the packages in
requirements.txt. Exits 1 if any verdict differs from the expected one.
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys
import tempfile

from inspect_ai import eval as inspect_eval

HERE = pathlib.Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("inspect_task", HERE / "inspect_task.py")
it = importlib.util.module_from_spec(spec)
spec.loader.exec_module(it)

WRONG = {
    "cache": ["cache_no_invalidate.sh", "cache_inprocess.sh", "cache_decoy_ttl.sh"],
    "vector": ["vector_bruteforce.py", "vector_shuffled_tail.py", "vector_client_side.py",
               "vector_mget_client_side.py", "vector_blob_client_side.py", "vector_local_copy.py"],
    "migrate": ["migrate_no_ttl.py", "migrate_left_replica.sh", "migrate_moved_deadlines.sh"],
    "migrate8": ["migrate8_no_field_ttl.py", "migrate8_replicaof.sh", "migrate8_moved_deadlines.py"],
}
# correct solutions, including other valid shapes the grader must not reject
OK = {"cache": ["cache_ok.sh", "cache_base64_ok.sh", "cache_hash_field_ttl_ok.sh"],
      "vector": ["vector_ok.py", "vector_two_indexes_ok.py", "vector_scan_discovery_ok.py"],
      "migrate": ["migrate_ok.sh"], "migrate8": ["migrate8_ok.py"]}


def cases(prefix: str = ""):
    for y in sorted((HERE / "jobs").glob("*.yaml")):
        if not y.stem.startswith(prefix):
            continue
        kind = y.stem.rsplit("_", 1)[0]
        yield y.stem, None, "fail"
        for good in OK[kind]:
            yield y.stem, good, "pass"
        for w in WRONG[kind]:
            yield y.stem, w, "fail"


def main() -> None:
    todo = list(cases(sys.argv[1] if len(sys.argv) > 1 else ""))
    tasks = [it.cari_job(job=f"jobs/{job}.yaml", solution=f"jobs/reference/{sol}" if sol else None) for job, sol, _ in todo]
    logs = inspect_eval(tasks, model="mockllm/model", log_dir=tempfile.mkdtemp(prefix="prove-"), max_tasks=4,
                        display="none")
    bad = 0
    print(f"{'job':16} {'solution':30} {'expected':8} {'got':6}")
    for (job, sol, want), log in zip(todo, logs):
        if log.status != "success" or not log.samples:
            got, why = "error", (log.error.message if log.error else log.status)[:160]
        else:
            score = next(iter(log.samples[0].scores.values()))
            meta = score.metadata or {}
            run = meta.get("solution_run") or {}
            if meta.get("grader_error", True):
                got, why = "error", f"the grader gave no valid verdict: {(score.explanation or '')[:140]}"
            elif sol and run.get("exit") != 0:
                got, why = "error", f"the solution itself failed (exit {run.get('exit')}): {(run.get('stderr') or '')[-140:]}"
            else:
                got, why = ("pass" if score.value == "C" else "fail"), (score.explanation or "")[:160]
        ok = got == want
        bad += not ok
        print(f"{job:16} {sol or '(agent does nothing)':30} {want:8} {got:6} {'' if ok else 'MISMATCH: ' + why}")
    print(f"\n{len(todo) - bad} of {len(todo)} checks gave the expected verdict")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
