"""Unit tests for ``.github/scripts/deploy_health.py`` (FAR-2116).

A CI workflow's decision logic is never exercised by pytest, so the deploy-health
evaluation and email render were extracted into a testable script. These tests
drive every threshold edge and every alert/recovery transition, and they assert
the REAL subject/body an operator receives -- not just a boolean.

They run without a database, a network or GitHub.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path
from typing import Any

for parent in Path(__file__).resolve().parents:
    _script_path = parent / ".github" / "scripts" / "deploy_health.py"
    if _script_path.exists():
        break
else:
    raise RuntimeError("Could not find .github/scripts/deploy_health.py")

_loader = SourceFileLoader("deploy_health", str(_script_path))
health = module_from_spec(spec_from_loader("deploy_health", _loader))
# dataclasses resolves the defining module through sys.modules, so the loaded
# module must be registered before it executes.
sys.modules["deploy_health"] = health
_loader.exec_module(health)

_NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
_RUN_URL = "https://github.com/farnalabs/modulo/actions/runs/40175892078"
_DEPLOY_URL = "https://github.com/farnalabs/modulo/actions/workflows/deploy.yml"


def _main(sha: str = "4db7af2000") -> dict[str, Any]:
    return {"sha": sha, "date": "2026-10-25T09:00:00Z"}


def _prod(*, sha: str = "7b1c064000", age_hours: float = 0.5) -> dict[str, Any]:
    built = _NOW - timedelta(hours=age_hours)
    return {"git_sha": sha, "build_timestamp": built.strftime("%Y-%m-%dT%H:%M:%SZ")}


def _run(
    conclusion: str,
    *,
    status: str = "completed",
    sha: str = "aaaaaaa",
    title: str = "",
    deploy_job: str | None = None,
) -> dict[str, Any]:
    run: dict[str, Any] = {
        "databaseId": 1,
        "status": status,
        "conclusion": conclusion,
        "headSha": sha,
        "displayTitle": title,
        "url": f"https://github.com/farnalabs/modulo/actions/runs/{sha}",
    }
    if deploy_job is not None:
        run["deploy_job_conclusion"] = deploy_job
    return run


def _verdict(
    *,
    main: dict[str, Any] | None = None,
    prod: dict[str, Any] | None = None,
    runs: list[dict[str, Any]] | None = None,
    grace_hours: int = 6,
    failure_threshold: int = 2,
) -> Any:
    return health.evaluate(
        main=_main() if main is None else main,
        prod=_prod() if prod is None else prod,
        deploy_runs=runs if runs is not None else [],
        now=_NOW,
        grace_hours=grace_hours,
        failure_threshold=failure_threshold,
    )


# ---------------------------------------------------------------------------
# Consecutive-failure detection
# ---------------------------------------------------------------------------


def test_single_failed_deploy_does_not_fire() -> None:
    """A single red deploy is normal (a flake, a transient registry timeout)."""
    verdict = _verdict(runs=[_run("failure")])
    assert verdict.healthy is True
    assert verdict.consecutive_failures == 1


def test_two_consecutive_failures_fire_at_the_threshold() -> None:
    verdict = _verdict(runs=[_run("failure"), _run("failure")])
    assert verdict.healthy is False
    assert verdict.consecutive_failures == 2
    assert "2 consecutive failed runs (threshold 2" in verdict.reason


def test_threshold_is_configurable() -> None:
    one_failure = [_run("failure")]
    assert _verdict(runs=one_failure, failure_threshold=1).healthy is False
    assert _verdict(runs=one_failure, failure_threshold=3).healthy is True


def test_cancelled_run_breaks_the_streak() -> None:
    """A superseded/duplicate deploy is NOT a failure and must not inflate it."""
    verdict = _verdict(runs=[_run("failure"), _run("cancelled"), _run("failure"), _run("failure")])
    assert verdict.consecutive_failures == 1
    assert verdict.healthy is True


def test_success_breaks_the_streak() -> None:
    verdict = _verdict(runs=[_run("failure"), _run("success"), _run("failure")])
    assert verdict.consecutive_failures == 1
    assert verdict.healthy is True


def test_throttled_noop_does_not_break_a_failure_streak() -> None:
    """FAR-2152: a throttled run is SUCCESS at run level but skipped the deploy job.

    Counting that no-op as a success breaks the streak and delays the alert, so
    the run must be NEUTRAL: neither a success nor a failure.
    """
    verdict = _verdict(
        runs=[
            _run("failure"),
            _run("success", deploy_job="skipped"),
            _run("failure"),
        ]
    )
    assert verdict.consecutive_failures == 2
    assert verdict.healthy is False


def test_throttled_noops_are_skipped_when_finding_the_latest_failure() -> None:
    """The reported "latest failure" must be the real failed deploy, not a no-op."""
    verdict = _verdict(
        runs=[
            _run("failure", sha="bbbbbbb", title="real failure"),
            _run("success", sha="ccccccc", deploy_job="skipped"),
        ]
    )
    assert verdict.consecutive_failures == 1
    assert verdict.last_failed.get("sha") == "bbbbbbb"
    assert verdict.last_failed.get("title") == "real failure"


def test_unreadable_deploy_job_falls_back_to_the_run_conclusion() -> None:
    """Enrichment failure must NOT invent a deploy or hide a failure.

    The field is absent whenever the lookup could not be read; the walk then
    keeps its pre-FAR-2152 behaviour (a success breaks the streak).
    """
    verdict = _verdict(runs=[_run("failure"), _run("success"), _run("failure")])
    assert verdict.consecutive_failures == 1


def test_real_success_with_deploy_job_success_still_breaks_the_streak() -> None:
    verdict = _verdict(
        runs=[
            _run("failure"),
            _run("success", deploy_job="success"),
            _run("failure"),
        ]
    )
    assert verdict.consecutive_failures == 1


def test_in_progress_runs_are_skipped_not_counted() -> None:
    verdict = _verdict(runs=[_run("failure"), _run("", status="in_progress"), _run("failure")])
    assert verdict.consecutive_failures == 2
    assert verdict.runs_considered == 2


def test_failure_at_the_end_of_the_window_still_counts() -> None:
    """The walk is over completed runs only, so a queued tick cannot hide it."""
    runs = [_run("failure", sha=f"{index:07d}") for index in range(health.DEPLOY_RUNS_WINDOW)]
    verdict = _verdict(runs=runs)
    assert verdict.consecutive_failures == health.DEPLOY_RUNS_WINDOW
    assert verdict.healthy is False


# ---------------------------------------------------------------------------
# Production staleness
# ---------------------------------------------------------------------------


def test_prod_current_with_main_is_healthy() -> None:
    verdict = _verdict(main=_main("4db7af2000"), prod=_prod(sha="4db7af2000", age_hours=0.1))
    assert verdict.healthy is True
    assert verdict.stale is False
    assert verdict.age_hours is None


def test_prod_stale_beyond_grace_fires_and_names_the_figures() -> None:
    verdict = _verdict(prod=_prod(sha="7b1c064000", age_hours=46))
    assert verdict.healthy is False
    assert verdict.stale is True
    assert verdict.age_hours == 46
    # The reason is what a human reads: prod sha, main sha, age, grace.
    assert "7b1c064" in verdict.reason
    assert "4db7af2" in verdict.reason
    assert "46h" in verdict.reason
    assert "6h grace" in verdict.reason


def test_prod_just_below_grace_is_healthy() -> None:
    verdict = _verdict(prod=_prod(age_hours=5), grace_hours=6)
    assert verdict.healthy is True
    assert verdict.stale is False


def test_prod_exactly_at_grace_fires() -> None:
    verdict = _verdict(prod=_prod(age_hours=6), grace_hours=6)
    assert verdict.healthy is False
    assert verdict.stale is True


def test_grace_is_configurable() -> None:
    assert _verdict(prod=_prod(age_hours=3), grace_hours=2).healthy is False
    assert _verdict(prod=_prod(age_hours=3), grace_hours=8).healthy is True


def test_unparseable_build_timestamp_with_prod_behind_fires() -> None:
    """Prod is behind main and the age is unknowable: fail loud (FAR-910)."""
    verdict = _verdict(prod={"git_sha": "7b1c064000", "build_timestamp": "not-a-date"})
    assert verdict.healthy is False
    assert verdict.stale is True
    assert verdict.age_hours is None
    assert "cannot be confirmed" in verdict.reason


def test_missing_prod_metadata_is_unknown_not_a_false_alarm() -> None:
    verdict = _verdict(prod={}, runs=[])
    assert verdict.stale is False
    assert verdict.healthy is True


# ---------------------------------------------------------------------------
# UNKNOWN handling
# ---------------------------------------------------------------------------


def test_no_readable_source_is_unknown() -> None:
    verdict = health.evaluate(
        main=None,
        prod=None,
        deploy_runs=None,
        now=_NOW,
        grace_hours=6,
        failure_threshold=2,
    )
    assert verdict.healthy is None
    assert "could not be evaluated" in verdict.reason
    assert health.resolve_action(verdict.healthy, health.UNKNOWN) == "none"


def test_unreadable_run_list_does_not_count_as_zero_failures() -> None:
    """An absent run list is UNKNOWN; the staleness half still decides."""
    verdict = health.evaluate(main=_main(), prod=_prod(), deploy_runs=None, now=_NOW)
    assert verdict.healthy is True
    assert verdict.consecutive_failures == 0


# ---------------------------------------------------------------------------
# Transitions (the noise control: one email per incident)
# ---------------------------------------------------------------------------


def test_transition_table() -> None:
    resolve = health.resolve_action
    # entering the condition (or an unknown previous state) alerts
    assert resolve(False, health.HEALTHY) == "alert"
    assert resolve(False, health.UNKNOWN) == "alert"
    # staying in it does not re-alert
    assert resolve(False, health.UNHEALTHY) == "none"
    # recovering closes the incident exactly once
    assert resolve(True, health.UNHEALTHY) == "recovery"
    assert resolve(True, health.HEALTHY) == "none"
    assert resolve(True, health.UNKNOWN) == "none"
    # an unreadable current verdict invents nothing
    assert resolve(None, health.UNHEALTHY) == "none"


def test_parse_previous_verdict_reads_the_verdict_step() -> None:
    def jobs(conclusion: str | None, name: str = health.VERDICT_STEP_NAME) -> dict[str, Any]:
        return {"jobs": [{"steps": [{"name": name, "conclusion": conclusion}]}]}

    assert health.parse_previous_verdict(jobs("failure")) == health.UNHEALTHY
    assert health.parse_previous_verdict(jobs("skipped")) == health.HEALTHY
    assert health.parse_previous_verdict(jobs("success")) == health.UNKNOWN
    assert health.parse_previous_verdict(jobs(None)) == health.UNKNOWN


def test_parse_previous_verdict_without_the_step_is_unknown() -> None:
    """A run from an older workflow definition must not read as healthy."""
    payload = {"jobs": [{"steps": [{"name": "Compare prod build SHA against origin/main HEAD"}]}]}
    assert health.parse_previous_verdict(payload) == health.UNKNOWN
    assert health.parse_previous_verdict(None) == health.UNKNOWN
    assert health.parse_previous_verdict({}) == health.UNKNOWN


# ---------------------------------------------------------------------------
# The emails a human receives
# ---------------------------------------------------------------------------


def test_alert_email_names_every_figure_and_the_window() -> None:
    verdict = _verdict(
        prod=_prod(sha="7b1c064000", age_hours=46),
        runs=[_run("failure", sha="deadbee", title="fix: something"), _run("failure")],
    )
    subject, body = health.build_email(
        verdict=verdict,
        action="alert",
        run_url=_RUN_URL,
        deploy_url=_DEPLOY_URL,
    )

    assert "UNHEALTHY" in subject
    assert "2 failed deploys" in subject
    assert "prod 46h stale" in subject
    assert "7b1c064" in body
    assert "4db7af2" in body
    assert "46h (grace 6h)" in body
    assert "threshold 2" in body
    assert "last 2 completed runs" in body
    assert "Evaluated every 30 minutes" in body
    assert "single failed deploy never alerts" in body
    assert _RUN_URL in body
    assert _DEPLOY_URL in body


def test_recovery_email_closes_the_incident() -> None:
    verdict = _verdict(runs=[_run("success")])
    subject, body = health.build_email(
        verdict=verdict,
        action="recovery",
        run_url=_RUN_URL,
        deploy_url=_DEPLOY_URL,
    )

    assert "RECOVERED" in subject
    assert "production is current at 7b1c064" in subject
    assert "RECOVERED" in body
    assert "closes the earlier alert" in body


def test_alert_email_flags_an_unknown_age_without_inventing_one() -> None:
    verdict = _verdict(prod={"git_sha": "7b1c064000", "build_timestamp": ""})
    _subject, body = health.build_email(
        verdict=verdict,
        action="alert",
        run_url=_RUN_URL,
        deploy_url=_DEPLOY_URL,
    )
    assert "unknown (grace 6h)" in body
    assert "None" not in body


# ---------------------------------------------------------------------------
# CLI: the exact invocation the workflow makes
# ---------------------------------------------------------------------------


def test_cli_evaluate_writes_github_outputs(tmp_path: Path) -> None:
    main_path = tmp_path / "main.json"
    prod_path = tmp_path / "prod.json"
    runs_path = tmp_path / "runs.json"
    output_path = tmp_path / "output"
    verdict_path = tmp_path / "verdict.json"

    main_path.write_text(json.dumps(_main()), encoding="utf-8")
    prod_path.write_text(json.dumps(_prod(sha="7b1c064000", age_hours=46)), encoding="utf-8")
    runs_path.write_text(json.dumps([_run("failure"), _run("failure")]), encoding="utf-8")

    exit_code = health.main(
        [
            "evaluate",
            "--main-json",
            str(main_path),
            "--prod-json",
            str(prod_path),
            "--runs-json",
            str(runs_path),
            "--grace-hours",
            "6",
            "--failure-threshold",
            "2",
            "--now",
            "2026-10-25T12:00:00Z",
            "--github-output",
            str(output_path),
            "--verdict-out",
            str(verdict_path),
        ]
    )

    assert exit_code == 0
    outputs = dict(line.split("=", 1) for line in output_path.read_text(encoding="utf-8").splitlines() if "=" in line)
    assert outputs["healthy"] == "false"
    assert outputs["stale"] == "true"
    assert outputs["consecutive_failures"] == "2"
    assert outputs["age_hours"] == "46"
    assert outputs["prod_sha"] == "7b1c064"
    assert outputs["main_sha"] == "4db7af2"
    assert "46h ago" in outputs["reason"]
    assert json.loads(verdict_path.read_text(encoding="utf-8"))["healthy"] is False


def test_cli_action_and_previous_verdict(tmp_path: Path) -> None:
    jobs_path = tmp_path / "jobs.json"
    jobs_path.write_text(
        json.dumps({"jobs": [{"steps": [{"name": health.VERDICT_STEP_NAME, "conclusion": "skipped"}]}]}),
        encoding="utf-8",
    )
    assert health.main(["previous-verdict", "--jobs-json", str(jobs_path)]) == 0
    assert health.main(["action", "--healthy", "false", "--previous", "healthy"]) == 0
    assert health.main(["action", "--healthy", "true", "--previous", "unhealthy"]) == 0
    assert health.main(["action", "--healthy", "", "--previous", "unhealthy"]) == 0


def test_cli_email_renders_json(tmp_path: Path, capsys: Any) -> None:
    verdict_path = tmp_path / "verdict.json"
    verdict_path.write_text(
        json.dumps(_verdict(prod=_prod(age_hours=46)).as_dict()),
        encoding="utf-8",
    )
    assert (
        health.main(
            [
                "email",
                "--verdict-json",
                str(verdict_path),
                "--action",
                "alert",
                "--run-url",
                _RUN_URL,
                "--deploy-url",
                _DEPLOY_URL,
            ]
        )
        == 0
    )
    rendered = json.loads(capsys.readouterr().out)
    assert "UNHEALTHY" in rendered["subject"]
    assert _RUN_URL in rendered["body"]


# ---------------------------------------------------------------------------
# FAR-2139: a second caller (the deploy watchdog) reuses this renderer
# ---------------------------------------------------------------------------


def test_watchdog_condition_reaches_the_subject_and_body() -> None:
    """A caller-measured condition the shared pair cannot express is rendered.

    The deploy watchdog escalates for two reasons: it exhausted its 3-failure
    self-heal cap (expressed by the shared consecutive-failure condition), or
    its own re-dispatch failed (expressible by NEITHER shared condition). The
    second case travels in ``summary``/``notes`` so the watchdog keeps the
    SAME subject/body shape instead of inventing a second email format.
    """
    verdict = health.Verdict(
        **{
            **_verdict().as_dict(),
            "healthy": False,
            "notes": ["the deploy watchdog could not re-dispatch deploy.yml while production served 7b1c064"],
            "summary": "watchdog could not re-dispatch deploy.yml",
        }
    )
    subject, body = health.build_email(
        verdict=verdict,
        action="alert",
        run_url=_RUN_URL,
        deploy_url=_DEPLOY_URL,
    )
    assert "UNHEALTHY" in subject
    assert "watchdog could not re-dispatch deploy.yml" in subject
    assert "could not re-dispatch deploy.yml while production served 7b1c064" in body
    # An unhealthy verdict must never fall through to the healthy sentence.
    assert "all measured deploy-health conditions are healthy" not in body


def test_unhealthy_with_no_shared_condition_never_claims_health() -> None:
    """A false verdict with no condition and no summary must not read healthy."""
    verdict = health.Verdict(
        **{
            **_verdict().as_dict(),
            "healthy": False,
            "summary": "",
            "notes": [],
        }
    )
    assert "all measured deploy-health conditions are healthy" not in verdict.reason
    assert verdict.reason


def test_watchdog_recovery_replaces_the_unmeasured_production_claim() -> None:
    """A watchdog recovery can happen while a deploy is still in flight.

    The staleness check's recovery text ("production is current at X") is a fact
    that check measured; the watchdog has not measured it at that point, so the
    caller supplies the measured evidence for the subject and the action line
    instead of inheriting an unverified claim.
    """
    verdict = health.Verdict(
        **{
            **_verdict().as_dict(),
            "healthy": True,
            "summary": "the deploy watchdog is not escalating (deploy in flight)",
            "notes": ["a deploy run is in progress, so this tick did not compare production"],
            "recovery_detail": "A deploy run is in progress; no action is needed.",
        }
    )
    subject, body = health.build_email(
        verdict=verdict,
        action="recovery",
        run_url=_RUN_URL,
        deploy_url=_DEPLOY_URL,
    )
    assert "RECOVERED" in subject
    assert "not escalating (deploy in flight)" in subject
    assert "production is current at" not in subject
    assert "A deploy run is in progress; no action is needed." in body
    assert "a deploy run is in progress, so this tick did not compare production" in body


def test_cli_email_round_trips_a_watchdog_verdict(tmp_path: Path, capsys: Any) -> None:
    """The workflow path: verdict JSON -> CLI -> rendered email (notes included).

    ``_cmd_email`` rebuilds the dataclass from the JSON a workflow writes, so a
    JSON list for ``notes`` must render exactly like the in-process tuple.
    """
    verdict = health.Verdict(
        **{
            **_verdict().as_dict(),
            "healthy": False,
            "notes": ["the watchdog could not re-dispatch deploy.yml"],
            "summary": "watchdog could not re-dispatch deploy.yml",
        }
    )
    verdict_path = tmp_path / "verdict.json"
    verdict_path.write_text(json.dumps(verdict.as_dict()), encoding="utf-8")

    assert (
        health.main(
            [
                "email",
                "--verdict-json",
                str(verdict_path),
                "--action",
                "alert",
                "--run-url",
                _RUN_URL,
                "--deploy-url",
                _DEPLOY_URL,
            ]
        )
        == 0
    )
    rendered = json.loads(capsys.readouterr().out)
    assert "watchdog could not re-dispatch deploy.yml" in rendered["subject"]
    assert "could not re-dispatch deploy.yml" in rendered["body"]
