#!/usr/bin/env python3
"""Attach the production-deploy job's conclusion to ``deploy.yml`` run records.

FAR-2152: a throttled ``deploy.yml`` run concludes **SUCCESS** while shipping
nothing - every downstream job, including ``Deploy to app.modulo.run``, is
skipped. Any consumer that reads the RUN conclusion therefore cannot tell a
real deploy from a no-op, and (2026-10-25) four "successful" deploy runs
between 12:03Z and 12:44Z shipped nothing while a regression stayed invisible.

The deploy JOB conclusion can tell them apart: ``success`` means the production
deploy ran and passed its SHA verification, ``skipped`` means nothing was
deployed. This script enriches a ``gh run list --json ...`` payload with that
fact so the deploy-escalation consumers classify an *attempt* by the artefact
rather than by the run's colour:

- ``.github/scripts/deploy_health.py`` (deploy-staleness-check.yml)
- ``deploy-watchdog.yml``'s inline failure-streak walk

It shells out to ``gh`` (``GH_TOKEN`` from the environment) and FAILS OPEN: a
lookup failure leaves ``deploy_job_conclusion`` absent, which the consumers read
as UNKNOWN and handle by falling back to the run conclusion - never by
inventing a deploy. Enrichment is bounded (``--limit``, default 5 completed
runs) because a consumer only ever needs enough history to find the streak it
is measuring.

Tested by ``backend/tests/unit/scripts/test_deploy_run_attempts.py``.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

#: The production-deploy job. Its conclusion IS the deploy-attempt signal.
DEPLOY_JOB_NAME = "Deploy to app.modulo.run"

#: Enough completed runs to find a 2-failure streak even when throttled no-ops
#: are interleaved (a throttled run is neutral, so the walk skips past it).
DEFAULT_LIMIT = 5

_JQ_PROGRAM = '[.jobs[] | select(.name == "' + DEPLOY_JOB_NAME + '") | .conclusion] | .[0] // ""'


def _deploy_job_conclusion(
    repo: str,
    run_id: object,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> str | None:
    """The deploy job's conclusion, or ``None`` when it cannot be read.

    ``None`` is deliberately distinct from ``""``/``"skipped"``: the caller
    must fall back to the run conclusion on UNKNOWN, never treat an unreadable
    lookup as "nothing was deployed" (which would hide a real failure).
    """
    endpoint = f"repos/{repo}/actions/runs/{run_id}/jobs?per_page=100"
    try:
        completed = runner(
            ["gh", "api", endpoint, "--jq", _JQ_PROGRAM],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if completed.returncode != 0:
        return None
    conclusion = (completed.stdout or "").strip().lower()
    if not conclusion:
        return None
    return conclusion


def enrich(
    runs: Sequence[dict[str, Any]],
    *,
    repo: str,
    limit: int = DEFAULT_LIMIT,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> list[dict[str, Any]]:
    """Return ``runs`` with ``deploy_job_conclusion`` attached where readable.

    Ordering is preserved (the consumers walk newest-first) and unrecognised
    fields are untouched. Only completed runs are looked up, and at most
    ``limit`` of them.
    """
    enriched = [dict(run) for run in runs]
    looked_up = 0
    for run in enriched:
        if str(run.get("status") or "").lower() != "completed":
            continue
        if looked_up >= limit:
            break
        run_id = run.get("databaseId")
        if run_id in (None, ""):
            continue
        looked_up += 1
        conclusion = _deploy_job_conclusion(repo, run_id, runner=runner)
        if conclusion is None:
            continue
        run["deploy_job_conclusion"] = conclusion
    return enriched


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    enrich_cmd = sub.add_parser("enrich", help="attach deploy_job_conclusion to a run list")
    enrich_cmd.add_argument("--runs-json", required=True, help="input gh run list JSON")
    enrich_cmd.add_argument("--out", required=True, help="output path for enriched JSON")
    enrich_cmd.add_argument("--repo", required=True, help="owner/repo")
    enrich_cmd.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    args = parser.parse_args(argv)

    if args.command != "enrich":  # pragma: no cover - argparse enforces the one command
        parser.error(f"unknown command {args.command!r}")

    runs = json.loads(Path(args.runs_json).read_text(encoding="utf-8"))
    if not isinstance(runs, list):
        print(f"deploy_run_attempts: {args.runs_json} is not a JSON list", file=sys.stderr)
        return 2
    result = enrich(runs, repo=args.repo, limit=args.limit)
    Path(args.out).write_text(json.dumps(result), encoding="utf-8")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by the workflows
    raise SystemExit(main())
