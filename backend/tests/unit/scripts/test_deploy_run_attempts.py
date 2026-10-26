"""Unit tests for ``.github/scripts/deploy_run_attempts.py`` (FAR-2152).

A throttled ``deploy.yml`` run concludes SUCCESS while shipping nothing, so the
deploy JOB's conclusion is the only run-level fact that says a deploy was
attempted. These tests drive the enrichment that attaches it - the input the
deploy-health evaluator and the deploy watchdog use to classify an attempt by
the artefact rather than by the run's colour.

No network, no GitHub: ``gh`` is a fake runner.
"""

from __future__ import annotations

import json
import subprocess
import sys
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path
from typing import Any

for parent in Path(__file__).resolve().parents:
    _script_path = parent / ".github" / "scripts" / "deploy_run_attempts.py"
    if _script_path.exists():
        break
else:
    raise RuntimeError("Could not find .github/scripts/deploy_run_attempts.py")

_loader = SourceFileLoader("deploy_run_attempts", str(_script_path))
attempts = module_from_spec(spec_from_loader("deploy_run_attempts", _loader))
sys.modules["deploy_run_attempts"] = attempts
_loader.exec_module(attempts)

_REPO = "farnalabs/modulo"


def _run(
    run_id: int,
    *,
    status: str = "completed",
    conclusion: str = "success",
) -> dict[str, Any]:
    return {
        "databaseId": run_id,
        "status": status,
        "conclusion": conclusion,
        "headSha": f"{run_id:08d}",
        "displayTitle": f"run {run_id}",
    }


class _FakeGh:
    """A fake ``gh api`` that returns a per-run deploy-job conclusion."""

    def __init__(self, conclusions: dict[int, str], *, returncode: int = 0) -> None:
        self.conclusions = conclusions
        self.returncode = returncode
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        run_id = 0
        for part in argv:
            if "/actions/runs/" in part:
                run_id = int(part.split("/actions/runs/")[1].split("/")[0])
        stdout = self.conclusions.get(run_id, "") + "\n"
        return subprocess.CompletedProcess(argv, self.returncode, stdout, "")


def test_enrich_attaches_the_deploy_job_conclusion_per_run() -> None:
    gh = _FakeGh({111: "skipped", 222: "success", 333: "failure"})
    result = attempts.enrich(
        [_run(111), _run(222, conclusion="failure"), _run(333, conclusion="failure")],
        repo=_REPO,
        runner=gh,
    )
    assert [r.get("deploy_job_conclusion") for r in result] == ["skipped", "success", "failure"]


def test_enrich_queries_the_production_job_with_a_full_job_page() -> None:
    gh = _FakeGh({111: "skipped"})
    attempts.enrich([_run(111)], repo=_REPO, runner=gh)
    assert len(gh.calls) == 1
    argv = gh.calls[0]
    assert argv[0] == "gh"
    assert f"repos/{_REPO}/actions/runs/111/jobs?per_page=100" in argv
    jq_program = argv[argv.index("--jq") + 1]
    assert attempts.DEPLOY_JOB_NAME in jq_program
    assert "select(.name ==" in jq_program


def test_enrich_ignores_in_progress_runs() -> None:
    """Only a COMPLETED run has a deploy-job conclusion worth reading."""
    gh = _FakeGh({111: "success"})
    result = attempts.enrich(
        [_run(111, status="in_progress", conclusion=""), _run(222, status="queued", conclusion="")],
        repo=_REPO,
        runner=gh,
    )
    assert not gh.calls
    assert "deploy_job_conclusion" not in result[0]
    assert "deploy_job_conclusion" not in result[1]


def test_enrich_is_bounded_by_the_limit() -> None:
    runs = [_run(index) for index in range(1, 8)]
    gh = _FakeGh(dict.fromkeys(range(1, 8), "success"))
    result = attempts.enrich(runs, repo=_REPO, limit=3, runner=gh)
    assert len(gh.calls) == 3
    assert result[3].get("deploy_job_conclusion") is None


def test_enrich_fails_open_when_gh_errors() -> None:
    """An unreadable lookup leaves the field ABSENT (unknown), never invented."""
    gh = _FakeGh({111: "skipped"}, returncode=1)
    result = attempts.enrich([_run(111)], repo=_REPO, runner=gh)
    assert "deploy_job_conclusion" not in result[0]
    assert result[0]["databaseId"] == 111


def test_enrich_fails_open_when_gh_is_missing() -> None:
    def missing(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("gh")

    result = attempts.enrich([_run(111)], repo=_REPO, runner=missing)
    assert "deploy_job_conclusion" not in result[0]


def test_enrich_handles_a_missing_job_name_as_unknown() -> None:
    """An empty jq result (the job was renamed) must not read as "skipped"."""
    gh = _FakeGh({})
    result = attempts.enrich([_run(111)], repo=_REPO, runner=gh)
    assert "deploy_job_conclusion" not in result[0]


def test_enrich_preserves_order_and_every_other_field() -> None:
    gh = _FakeGh({111: "skipped"})
    runs = [_run(111), _run(222, status="in_progress", conclusion="")]
    result = attempts.enrich(runs, repo=_REPO, runner=gh)
    assert [r["databaseId"] for r in result] == [111, 222]
    assert result[0]["headSha"] == "00000111"
    assert result[1]["status"] == "in_progress"


def test_cli_writes_the_enriched_payload(tmp_path: Path) -> None:
    runs_path = tmp_path / "runs.json"
    out_path = tmp_path / "out.json"
    runs_path.write_text(json.dumps([_run(111)]), encoding="utf-8")
    original = attempts._deploy_job_conclusion
    attempts._deploy_job_conclusion = lambda repo, run_id, **kwargs: "skipped"  # type: ignore[assignment]
    try:
        exit_code = attempts.main(
            [
                "enrich",
                "--runs-json",
                str(runs_path),
                "--out",
                str(out_path),
                "--repo",
                _REPO,
            ]
        )
    finally:
        attempts._deploy_job_conclusion = original  # type: ignore[assignment]
    assert exit_code == 0
    written = json.loads(out_path.read_text(encoding="utf-8"))
    assert written[0]["deploy_job_conclusion"] == "skipped"


def test_cli_rejects_a_non_list_payload(tmp_path: Path) -> None:
    runs_path = tmp_path / "runs.json"
    runs_path.write_text(json.dumps({"not": "a list"}), encoding="utf-8")
    exit_code = attempts.main(
        [
            "enrich",
            "--runs-json",
            str(runs_path),
            "--out",
            str(tmp_path / "out.json"),
            "--repo",
            _REPO,
        ]
    )
    assert exit_code == 2
