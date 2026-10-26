#!/usr/bin/env python3
"""Deploy-health evaluation for the deploy escalation workflows.

Used by ``deploy-staleness-check.yml`` (FAR-2116) and ``deploy-watchdog.yml``
(FAR-2139). One evaluator and one email renderer for both: the two workflows
measure different things (the staleness check reads prod's build age and the
deploy run history; the watchdog reads the fly image tag and its own self-heal
outcome) but they must not drift into two thresholds or two email formats. The
watchdog supplies its own measured condition through :attr:`Verdict.notes` /
:attr:`Verdict.summary` when neither of the shared conditions below applies.

CI never exercises a workflow's decision logic, so the logic lives here where
``backend/tests/unit/scripts/test_deploy_health.py`` can drive it: every
threshold, every transition and the exact email a human receives are proved by
a unit test instead of by hoping a cron tick behaves.

Two measured conditions, either of which makes deploy health UNHEALTHY:

1. **Production is stale** -- the production build's age (from its
   ``BUILD_TIMESTAMP``, NOT from main's commit date; FAR-910) is at or beyond
   the grace window while prod's SHA differs from ``origin/main`` HEAD.
2. **The deploy workflow is failing repeatedly** -- at least
   ``failure_threshold`` (default 2) consecutive *failed* completed runs of
   ``deploy.yml``. ``cancelled`` runs are NOT failures (a superseded deploy is
   normal and must not inflate the streak), so the walk stops at any
   non-failure outcome. A THROTTLED run (``deploy_job_conclusion ==
   "skipped"``) deployed nothing, so it is neither: it is neutral and the walk
   continues past it (FAR-2152) - otherwise a throttled no-op sitting between
   two real failures would break the streak and suppress the alert.

A single flaky deploy therefore never alerts, and neither does a prod that is
merely inside the grace window (the throttle batches up to 3h, staging E2E
adds ~2h).

A tick that could read only SOME of its inputs is **UNKNOWN, never healthy**
(FAR-2159). An absent input is not evidence of health: scoring a partial read
as healthy let the transition email send a RECOVERY - closing a live alert -
on figures the tick never measured. Every input the verdict depends on is
classified REQUIRED or ADVISORY in :data:`INPUT_CLASSIFICATION`, and a tick
missing any REQUIRED input can never be HEALTHY. A positively measured
unhealthy condition still wins, because downgrading a confirmed failure streak
to UNKNOWN would itself suppress an alert.

The email is sent on a **transition**, and the previous verdict comes from the
workflow's own run history (the verdict step in the previous run is
``failure`` when it was unhealthy and ``skipped`` when it was healthy) -- no
state store is introduced. An UNKNOWN previous verdict (a crashed tick, a
cancelled run, or the first run after this workflow changed) alerts rather than
suppressing, so the first alert can never be swallowed.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

#: Production may lag main by this long before it is declared stale. Covers the
#: deploy throttle (DEPLOY_INTERVAL_HOURS, default 3h) + pre-deploy CI +
#: staging E2E (~2h) + retry headroom.
GRACE_HOURS_DEFAULT = 6

#: Consecutive FAILED deploy runs before the deploy itself is unhealthy.
FAILURE_THRESHOLD_DEFAULT = 2

#: How many completed deploy runs the consecutive-failure walk considers.
DEPLOY_RUNS_WINDOW = 15

#: The step whose conclusion is the durable previous-verdict state vector.
VERDICT_STEP_NAME = "Fail the run when deploy health is unhealthy"

#: ``deploy_job_conclusion`` values meaning "the lookup could not tell".
_ATTEMPT_UNKNOWN = frozenset({"", "unknown", "missing", "none", "null"})

HEALTHY = "healthy"
UNHEALTHY = "unhealthy"
UNKNOWN = "unknown"

#: A tick cannot reach a verdict at all without this input, so a tick missing
#: one is UNKNOWN - never healthy (FAR-2159).
REQUIRED = "required"

#: A tick can still reach a verdict without this input; it only enriches the
#: verdict (a missing timestamp fails loud, a missing job map drops a detail).
ADVISORY = "advisory"


@dataclass(frozen=True)
class InputClassification:
    """One measured input, and how the verdict depends on it.

    ``input_name`` is the source the evaluator reads (the field name on
    :class:`AvailableInputs`); ``verdict_field`` is the :class:`Verdict` field
    that input feeds. Every Verdict field must be either a classified input or
    listed in :data:`DERIVED_VERDICT_FIELDS` - a test pins that, so adding a
    new figure to the verdict forces the author to decide whether a tick may be
    scored healthy without reading it.
    """

    input_name: str
    verdict_field: str
    classification: str
    why: str


#: The single place the verdict's inputs are classified. The required-input
#: policy consults this list (:data:`REQUIRED_INPUTS`), so it cannot drift into
#: ad-hoc ``if x is None`` checks scattered through the evaluator.
INPUT_CLASSIFICATION: tuple[InputClassification, ...] = (
    InputClassification(
        "main_sha",
        "main_sha",
        REQUIRED,
        "the prod-vs-main comparison cannot run without origin/main's HEAD",
    ),
    InputClassification(
        "prod_sha",
        "prod_sha",
        REQUIRED,
        "the prod-vs-main comparison cannot run without production's build SHA",
    ),
    InputClassification(
        "deploy_runs",
        "consecutive_failures",
        REQUIRED,
        "without the run history a failure streak is unattempted, not absent",
    ),
    InputClassification(
        "prod_build_ts",
        "prod_build_ts",
        ADVISORY,
        "only the age figure needs it; a prod behind main with no timestamp fails loud as stale",
    ),
    InputClassification(
        "deploy_jobs",
        "failure_stages",
        ADVISORY,
        "only enriches the failure-stage breakdown; the streak itself does not need it",
    ),
)

#: Inputs a tick may not be scored HEALTHY without.
REQUIRED_INPUTS: frozenset[str] = frozenset(
    spec.input_name for spec in INPUT_CLASSIFICATION if spec.classification == REQUIRED
)

#: Verdict fields that are NOT a directly-measured input: derived from the
#: classified inputs, configuration, or supplied by the caller. A field that is
#: in neither this set nor :data:`INPUT_CLASSIFICATION` has no health policy -
#: a structural test fails on it rather than letting a figure slip in.
DERIVED_VERDICT_FIELDS: frozenset[str] = frozenset(
    {
        "healthy",  # computed from every required input plus the two conditions
        "stale",  # computed from the two SHAs + the build timestamp
        "age_hours",  # computed from the build timestamp
        "runs_considered",  # computed from the readable run list
        "last_failed",  # computed from the readable run list
        "failure_threshold",  # configuration
        "grace_hours",  # configuration
        "summary",  # caller-supplied condition label
        "notes",  # caller-supplied measured detail
        "recovery_detail",  # caller-supplied recovery wording
        "missing_inputs",  # computed from input availability
    }
)


@dataclass(frozen=True)
class AvailableInputs:
    """Which classified inputs one tick actually read.

    Built once by :func:`_read_sources`; :func:`_overall_healthy` consults
    :meth:`missing_required`, which in turn consults the registry - never a
    hand-written list at the call site.
    """

    main_sha: str = ""
    prod_sha: str = ""
    prod_build_ts: str = ""
    deploy_runs: bool = False
    deploy_jobs: bool = False

    def present(self, input_name: str) -> bool:
        return bool(getattr(self, input_name))

    def missing_required(self) -> tuple[str, ...]:
        """Required inputs this tick could not read, in a stable order."""
        return tuple(sorted(name for name in REQUIRED_INPUTS if not self.present(name)))


@dataclass(frozen=True)
class Verdict:
    """The evaluated deploy health for one tick."""

    healthy: bool | None
    stale: bool
    consecutive_failures: int
    failure_threshold: int
    age_hours: int | None
    grace_hours: int
    main_sha: str = "unknown"
    prod_sha: str = "unknown"
    prod_build_ts: str = "unknown"
    runs_considered: int = 0
    last_failed: dict[str, str] = field(default_factory=dict)
    #: Failure stage classifications for the failed runs in the streak, in run
    #: order (newest first). Each entry is ``{"sha": <short sha>, "stage":
    #: <category>}``. A list rather than a dict keyed by SHA so two failed
    #: reruns of the same commit contribute two entries instead of collapsing.
    failure_stages: list[dict[str, str]] = field(default_factory=list)
    #: A short condition label for the subject line when neither of the two
    #: shared conditions applies (e.g. the watchdog could not re-dispatch).
    summary: str = ""
    #: Caller-measured detail lines folded into :attr:`reason`. Both callers
    #: share this renderer; a caller with a condition the shared pair cannot
    #: express names it here instead of inventing a second email format.
    notes: tuple[str, ...] = ()
    #: Caller-measured sentence replacing the recovery email's default
    #: "production now serves the latest build" claim when the caller's
    #: recovery is real but not "production is current" (the deploy watchdog
    #: recovers into "no longer escalating", which can happen while a deploy is
    #: still in flight).
    recovery_detail: str = ""
    #: Required inputs this tick could not read (FAR-2159). Non-empty means the
    #: tick is a PARTIAL READ, so its verdict can never be HEALTHY; a positively
    #: measured unhealthy condition still reports ``healthy is False``.
    missing_inputs: tuple[str, ...] = ()

    def _stale_condition(self) -> str:
        if not self.stale:
            return ""
        if self.age_hours is None:
            return (
                f"production is serving {self.prod_sha} (behind origin/main {self.main_sha}) "
                "but its build timestamp is missing or unparseable, so staleness cannot be "
                f"confirmed against the {self.grace_hours}h grace window"
            )
        return (
            f"production is serving {self.prod_sha}, built {self.age_hours}h ago "
            f"(>= {self.grace_hours}h grace), while origin/main HEAD is {self.main_sha}"
        )

    def _failure_condition(self) -> str:
        if self.consecutive_failures < self.failure_threshold:
            return ""
        latest = self.last_failed.get("sha") or "unknown"
        title = self.last_failed.get("title") or ""
        return (
            f"the Deploy: Staging + Prod workflow has {self.consecutive_failures} consecutive "
            f"failed runs (threshold {self.failure_threshold}, over the last "
            f"{self.runs_considered} completed runs); latest failure {latest} {title}".strip()
        )

    def _failure_stages_condition(self) -> str:
        if not self.failure_stages:
            return ""
        return f"failure stages: {_summarize_failure_stages(self.failure_stages)}"

    def _partial_read_condition(self) -> str:
        """Name a partial read that stands alongside a confirmed bad condition."""
        if self.healthy is not False or not self.missing_inputs:
            return ""
        return (
            "this tick is a PARTIAL READ: required input(s) missing ("
            + ", ".join(self.missing_inputs)
            + "), so no all-clear follows from it"
        )

    def _default_reason(self) -> str:
        if self.healthy is None:
            if self.missing_inputs:
                return (
                    "deploy health could not be evaluated this cycle: required input(s) missing ("
                    + ", ".join(self.missing_inputs)
                    + "), so this partially readable tick is UNKNOWN rather than healthy"
                )
            return "deploy health could not be evaluated this cycle (no deploy-run or production metadata was readable)"
        if self.healthy is False:
            # A caller-measured unhealthy condition that the shared pair
            # cannot express must never fall through to the healthy
            # sentence below.
            return self.summary or "an unnamed deploy-health condition is unmet"
        return (
            "all measured deploy-health conditions are healthy: production is within the "
            f"{self.grace_hours}h grace window and fewer than {self.failure_threshold} consecutive "
            "deploy runs have failed"
        )

    @property
    def reason(self) -> str:
        """Every failing condition, naming its figures and the window."""
        parts: list[str] = list(self.notes)
        parts.extend(
            condition
            for condition in (
                self._stale_condition(),
                self._failure_condition(),
                self._failure_stages_condition(),
                self._partial_read_condition(),
            )
            if condition
        )
        return "; ".join(parts) if parts else self._default_reason()

    def as_dict(self) -> dict[str, Any]:
        """The verdict as JSON-serialisable data (handed to the email renderer)."""
        return {
            "healthy": self.healthy,
            "stale": self.stale,
            "consecutive_failures": self.consecutive_failures,
            "failure_threshold": self.failure_threshold,
            "age_hours": self.age_hours,
            "grace_hours": self.grace_hours,
            "main_sha": self.main_sha,
            "prod_sha": self.prod_sha,
            "prod_build_ts": self.prod_build_ts,
            "runs_considered": self.runs_considered,
            "last_failed": self.last_failed,
            "failure_stages": self.failure_stages,
            "summary": self.summary,
            "notes": list(self.notes),
            "recovery_detail": self.recovery_detail,
            "missing_inputs": list(self.missing_inputs),
        }

    @property
    def as_outputs(self) -> dict[str, str]:
        """Plain string outputs, as a GitHub Actions step would consume them."""
        if self.healthy is None:
            healthy = ""
        elif self.healthy:
            healthy = "true"
        else:
            healthy = "false"
        return {
            "healthy": healthy,
            "stale": "true" if self.stale else "false",
            "consecutive_failures": str(self.consecutive_failures),
            "failure_threshold": str(self.failure_threshold),
            "age_hours": "" if self.age_hours is None else str(self.age_hours),
            "grace_hours": str(self.grace_hours),
            "main_sha": self.main_sha,
            "prod_sha": self.prod_sha,
            "prod_build_ts": self.prod_build_ts,
            "runs_considered": str(self.runs_considered),
            "reason": self.reason,
            "summary": self.summary,
            "failure_stages": json.dumps(self.failure_stages),
            "missing_inputs": ",".join(self.missing_inputs),
        }


# ---------------------------------------------------------------------------
# Measurement
# ---------------------------------------------------------------------------


def _short(sha: str) -> str:
    return (sha or "").strip()[:7]


# Map deploy.yml jobs to failure stage categories.
#
# The keys are GitHub's ``jobs[].name`` DISPLAY names, not the YAML job IDs:
# the staleness workflow builds its job map from the API's jobs payload, which
# only carries the display name. Keying this map by YAML IDs made the
# classifier a silent no-op in production - no key ever matched, every failure
# classified ``unknown`` and was dropped, so ``failure_stages`` was always
# empty. ``test_deploy_health.py`` parses deploy.yml and asserts these exact
# display names, so a rename in the workflow fails CI instead of silently
# disabling classification again.
JOB_TO_STAGE: dict[str, str] = {
    # Job id: deploy-throttle (skip-vs-deploy decision; blocks the run)
    "Deploy throttle (batch deploys)": "deploy_throttle",
    # Job ids: backend-lint / backend-unit / frontend-checks (pre-deploy checks)
    "Pre-deploy: Backend lint": "pre_deploy_lint",
    "Pre-deploy: Backend unit tests": "pre_deploy_lint",
    "Pre-deploy: Frontend checks": "pre_deploy_lint",
    # Job id: integration-tests
    "Pre-deploy: Integration tests": "integration_test",
    # Job id: security-audit (semgrep, pip-audit)
    "Pre-deploy: Security audit": "pre_deploy_lint",
    # Job id: pre-deploy-gate (aggregate gate failing when a child failed)
    "Pre-deploy gate": "pre_deploy_gate",
    # Job id: deploy-staging (includes migration rehearsal and pre-migrate)
    "Deploy to staging": "migration",
    # Job id: staging-e2e
    "Staging E2E (smoke + WCAG + regression)": "staging_e2e",
    # Job id: deploy-production
    "Deploy to app.modulo.run": "migration",
    # Job id: prod-smoke, a SEPARATE job (needs deploy-production) - the one
    # stage where prod is deployed but broken.
    "Prod smoke tests": "prod_smoke",
}

# The order deploy.yml runs its jobs in, as the same ``jobs[].name`` display
# names. Earlier stages block later ones, so the first failure in this order is
# the one that stopped the run.
DEPLOYMENT_ORDER: tuple[str, ...] = (
    "Deploy throttle (batch deploys)",
    "Pre-deploy: Backend lint",
    "Pre-deploy: Backend unit tests",
    "Pre-deploy: Frontend checks",
    "Pre-deploy: Integration tests",
    "Pre-deploy: Security audit",
    "Pre-deploy gate",
    "Deploy to staging",
    "Staging E2E (smoke + WCAG + regression)",
    "Deploy to app.modulo.run",
    "Prod smoke tests",
)


def _failed(conclusion: str | None) -> bool:
    return str(conclusion or "").lower() in ("failure", "cancelled")


def _classify_failure_stage(job_conclusions: dict[str, str]) -> str:
    """Classify a failed run by the first failed job in deployment order.

    We return the stage category of the first failed job in
    :data:`DEPLOYMENT_ORDER`, since that is the failure that blocked the run.
    Any failed job outside the known order is then considered, so an unmapped
    job cannot mask a later mappable failure.

    ``job_conclusions`` is the wire shape produced by the staleness workflow:
    GitHub job DISPLAY names (``jobs[].name``) mapped to conclusions.
    """
    for job_name in DEPLOYMENT_ORDER:
        stage = JOB_TO_STAGE.get(job_name)
        if stage and _failed(job_conclusions.get(job_name)):
            return stage
    for job_name, conclusion in job_conclusions.items():
        stage = JOB_TO_STAGE.get(job_name)
        if stage and _failed(conclusion):
            return stage
    return "unknown"


def _summarize_failure_stages(failure_stages: Sequence[dict[str, str]]) -> str:
    """Render per-run failure stages as a comma-separated category count."""
    stage_counts: dict[str, int] = {}
    for entry in failure_stages:
        stage = entry.get("stage", "")
        if not stage:
            continue
        stage_counts[stage] = stage_counts.get(stage, 0) + 1
    return ", ".join(f"{count} {stage}" for stage, count in sorted(stage_counts.items()))


def _streak_failure_runs(
    completed: Sequence[dict[str, Any]],
    streak: int,
) -> Iterator[dict[str, Any]]:
    """The first ``streak`` failed runs, newest first, skipping neutral no-ops.

    Mirrors :func:`consecutive_failures`: a throttled no-op is neutral (skip
    it), and any other non-failure ends the streak.
    """
    counted = 0
    for run in completed:
        if counted >= streak:
            break
        if deploy_attempted(run) is False:
            continue
        if str(run.get("conclusion") or "").lower() != "failure":
            break
        counted += 1
        yield run


def _run_failure_stage(
    run: dict[str, Any],
    deploy_jobs: dict[str, dict[str, str]],
) -> str:
    run_id = str(run.get("databaseId") or "")
    jobs = deploy_jobs.get(run_id) if run_id else None
    return _classify_failure_stage(jobs) if jobs else "unknown"


def _classify_failure_stages(
    completed: Sequence[dict[str, Any]],
    streak: int,
    deploy_jobs: dict[str, dict[str, str]] | None,
) -> list[dict[str, str]]:
    """Classify each failed run in the streak by its blocking failure stage.

    ``deploy_jobs`` maps a run's ``databaseId`` (as a string) to that run's
    job name -> conclusion. Keying by run id rather than head SHA keeps reruns
    of the same commit distinct, so two failed reruns produce two entries.
    """
    if not deploy_jobs or streak <= 0:
        return []
    stages: list[dict[str, str]] = []
    for run in _streak_failure_runs(completed, streak):
        stage = _run_failure_stage(run, deploy_jobs)
        if stage != "unknown":
            stages.append({"sha": _short(str(run.get("headSha") or "")), "stage": stage})
    return stages


def _parse_timestamp(raw: str) -> datetime | None:
    if not raw:
        return None
    text = raw.strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def deploy_attempted(run: dict[str, Any]) -> bool | None:
    """``True``/``False`` when the run's deploy attempt is known, else ``None``.

    FAR-2152: a throttled run concludes SUCCESS but every job (including
    ``Deploy to app.modulo.run``) is skipped - it deployed nothing. Reading the
    run conclusion alone counts that no-op as a success, which breaks a streak
    of real deploy failures and delays the alert.

    ``deploy_job_conclusion`` is attached by
    ``.github/scripts/deploy_run_attempts.py``. When it is absent or
    unreadable the answer is ``None`` and the caller falls back to the run
    conclusion (never to "not attempted" - an unreadable lookup must not hide a
    real failure).
    """
    raw = run.get("deploy_job_conclusion")
    if raw is None:
        return None
    conclusion = str(raw).strip().lower()
    if conclusion in _ATTEMPT_UNKNOWN:
        return None
    return conclusion != "skipped"


def consecutive_failures(runs: Sequence[dict[str, Any]]) -> int:
    """Leading *failed* completed runs, newest first.

    ``cancelled`` (a superseded/duplicate deploy), ``success`` and any other
    outcome end the streak without contributing to it. A run that deployed
    NOTHING (a throttled no-op, ``deploy_job_conclusion == "skipped"``) is
    NEUTRAL: it neither feeds the streak nor breaks it, so a throttled run
    between two real failures cannot suppress the alert (FAR-2152).
    """
    streak = 0
    for run in runs:
        if str(run.get("status") or "").lower() != "completed":
            continue
        if deploy_attempted(run) is False:
            continue
        if str(run.get("conclusion") or "").lower() == "failure":
            streak += 1
            continue
        break
    return streak


def _completed_runs(runs: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    return [run for run in runs if str(run.get("status") or "").lower() == "completed"]


def _latest_failure(runs: Sequence[dict[str, Any]]) -> dict[str, str]:
    for run in runs:
        if str(run.get("status") or "").lower() != "completed":
            continue
        # FAR-2152: a throttled no-op is neutral here too - it must not stop
        # the walk before the real failed deploy it sits above.
        if deploy_attempted(run) is False:
            continue
        if str(run.get("conclusion") or "").lower() != "failure":
            break
        return {
            "sha": _short(str(run.get("headSha") or "")),
            "title": str(run.get("displayTitle") or ""),
            "url": str(run.get("url") or ""),
        }
    return {}


def _staleness(
    main_sha: str,
    prod_sha: str,
    prod_ts: str,
    *,
    now: datetime,
    grace_hours: int,
) -> tuple[bool, int | None]:
    """``(stale, age_hours)`` for the prod-vs-main comparison.

    An incomparable pair (either SHA missing) is ``(False, None)``: the tick
    measured nothing about staleness, and :func:`_overall_healthy` scores that
    UNKNOWN rather than healthy.
    """
    if not (main_sha and prod_sha) or main_sha == prod_sha:
        return False, None
    built = _parse_timestamp(prod_ts)
    if built is None:
        # Prod IS behind main and we cannot say by how much: fail loud
        # rather than silently skip (the FAR-910 precedent).
        return True, None
    age_hours = int((now - built) / timedelta(hours=1))
    return age_hours >= grace_hours, age_hours


def _overall_healthy(*, inputs: AvailableInputs, stale: bool, failing: bool) -> bool | None:
    """The verdict, under the required-input policy.

    POLICY (FAR-2159): a tick missing a REQUIRED input is UNKNOWN (``None``),
    NEVER healthy. An absent input is not evidence of health - scoring a
    partial read healthy let the transition email send a RECOVERY (closing a
    live alert) on figures the tick never measured. UNKNOWN sends nothing and
    keeps an alert open (:func:`resolve_action`), so it can neither close an
    incident nor suppress the first alert.

    A positively measured unhealthy condition still wins: a confirmed failure
    streak or staleness is evidence in its own right, and returning UNKNOWN for
    it would suppress an alert the tick did measure.
    """
    if inputs.missing_required():
        return False if (stale or failing) else None
    return not (stale or failing)


def _read_sources(
    main: dict[str, Any] | None,
    prod: dict[str, Any] | None,
    deploy_runs: Sequence[dict[str, Any]] | None,
    deploy_jobs: dict[str, dict[str, str]] | None,
) -> tuple[AvailableInputs, list[dict[str, Any]]]:
    """Normalise the raw sources into input availability and completed runs.

    Availability is recorded per classified input; the required-input policy
    is applied in :func:`_overall_healthy`, never by ad-hoc ``is None`` tests at
    the call sites.
    """
    inputs = AvailableInputs(
        main_sha=_short(str((main or {}).get("sha") or "")),
        prod_sha=_short(str((prod or {}).get("git_sha") or "")),
        prod_build_ts=str((prod or {}).get("build_timestamp") or ""),
        deploy_runs=deploy_runs is not None,
        deploy_jobs=bool(deploy_jobs),
    )
    return inputs, _completed_runs(list(deploy_runs or []))


def evaluate(
    *,
    main: dict[str, Any] | None,
    prod: dict[str, Any] | None,
    deploy_runs: Sequence[dict[str, Any]] | None,
    now: datetime,
    grace_hours: int = GRACE_HOURS_DEFAULT,
    failure_threshold: int = FAILURE_THRESHOLD_DEFAULT,
    deploy_jobs: dict[str, dict[str, str]] | None = None,
) -> Verdict:
    """Evaluate one tick's deploy health.

    ``deploy_runs`` of ``None`` means the run list was unreadable. A tick that
    can read only SOME of its inputs is UNKNOWN, never healthy (FAR-2159): the
    verdict is HEALTHY only when every REQUIRED input (see
    :data:`INPUT_CLASSIFICATION`) was read and no condition fires. A positively
    measured unhealthy condition still reports UNHEALTHY with the missing
    inputs named in :attr:`Verdict.missing_inputs`.

    ``deploy_jobs`` maps a failed run's ``databaseId`` (as a string) to that
    run's job name -> conclusion, used to classify the failure stage. If
    omitted or None, failure stages will be empty.
    """
    inputs, completed = _read_sources(main, prod, deploy_runs, deploy_jobs)
    stale, age_hours = _staleness(
        inputs.main_sha,
        inputs.prod_sha,
        inputs.prod_build_ts,
        now=now,
        grace_hours=grace_hours,
    )
    streak = consecutive_failures(completed)
    failing = inputs.deploy_runs and streak >= failure_threshold
    healthy = _overall_healthy(inputs=inputs, stale=stale, failing=failing)
    failure_stages = _classify_failure_stages(completed, streak, deploy_jobs)

    return Verdict(
        healthy=healthy,
        stale=stale,
        consecutive_failures=streak,
        failure_threshold=failure_threshold,
        age_hours=age_hours,
        grace_hours=grace_hours,
        main_sha=inputs.main_sha or "unknown",
        prod_sha=inputs.prod_sha or "unknown",
        prod_build_ts=inputs.prod_build_ts or "unknown",
        runs_considered=len(completed),
        last_failed=_latest_failure(completed),
        failure_stages=failure_stages,
        missing_inputs=inputs.missing_required(),
    )


# ---------------------------------------------------------------------------
# Transition + rendering
# ---------------------------------------------------------------------------


#: The step each escalation workflow runs ONLY when the tick produced no verdict
#: (``healthy`` is empty): it measured nothing, or it could read only some of its
#: required inputs (FAR-2159). Its presence is the durable machine-readable
#: "UNKNOWN" marker: without it, an unmeasured tick skips the verdict step and
#: its run is recorded as HEALTHY by ``_verdict_from_step`` (FAR-2157). Both
#: workflows must use this exact name - an architecture test pins it.
UNKNOWN_VERDICT_STEP_NAME = "Record the unmeasured tick (FAR-2157)"


def _verdict_from_step(step: dict[str, Any]) -> str:
    """Map one verdict step's conclusion onto a previous-verdict value."""
    conclusion = str(step.get("conclusion") or "").lower()
    if conclusion == "failure":
        return UNHEALTHY
    if conclusion == "skipped":
        return HEALTHY
    return UNKNOWN


def _has_unknown_marker(jobs_payload: dict[str, Any] | None) -> bool:
    """True when the run carries the UNKNOWN-tick marker step (FAR-2157).

    ``True`` is authoritative: the workflow ran that step, which it does only
    when the tick's ``healthy`` output was empty, i.e. the tick reached no
    verdict (nothing was measured, or only some required inputs were - FAR-2159).
    A tick that reached no verdict must never be read back as a healthy verdict
    - the transition email would then silently swallow the recovery.
    """
    for job in (jobs_payload or {}).get("jobs") or []:
        for step in job.get("steps") or []:
            if step.get("name") == UNKNOWN_VERDICT_STEP_NAME:
                return True
    return False


def parse_previous_verdict(jobs_payload: dict[str, Any] | None) -> str:
    """The previous run's verdict, from its steps.

    A run that carries the UNKNOWN-tick marker reached no verdict - it measured
    nothing, or only some of its required inputs (FAR-2159) - so its actual
    state is unknown (FAR-2157). That check comes first, because such a run
    SKIPS the verdict step and would otherwise read as healthy. Otherwise
    ``failure`` -> the previous run evaluated unhealthy; ``skipped`` -> it
    evaluated healthy (the step's condition was false); anything else, or no
    such step (a run from an older workflow definition), -> UNKNOWN.
    """
    if _has_unknown_marker(jobs_payload):
        return UNKNOWN
    for job in (jobs_payload or {}).get("jobs") or []:
        for step in job.get("steps") or []:
            if step.get("name") == VERDICT_STEP_NAME:
                return _verdict_from_step(step)
    return UNKNOWN


def resolve_action(current_healthy: bool | None, previous_verdict: str) -> str:
    """``alert`` / ``recovery`` / ``none`` for this tick.

    UNKNOWN never suppresses the signal: an unreadable previous verdict alerts
    the current unhealthy state, and an unreadable current verdict sends
    nothing (the workflow warns loudly instead of inventing a transition).
    That includes a PARTIAL read (FAR-2159): no recovery is ever inferred from
    a tick that could not read all of its required inputs, so a live alert
    stays open.
    """
    if current_healthy is None:
        return "none"
    if current_healthy:
        return "recovery" if previous_verdict == UNHEALTHY else "none"
    return "none" if previous_verdict == UNHEALTHY else "alert"


def build_email(
    *,
    verdict: Verdict,
    action: str,
    run_url: str,
    deploy_url: str,
) -> tuple[str, str]:
    """The (subject, body) an operator receives. Named figures, no lookup."""
    if action == "recovery":
        detail = verdict.summary or f"production is current at {verdict.prod_sha}"
        subject = f"[Modulo] DEPLOY HEALTH RECOVERED: {detail}"
        headline = "Deploy health has RECOVERED."
        action_line = verdict.recovery_detail or (
            "Production now serves the latest evaluated build and the deploy workflow is not in a "
            "consecutive-failure streak. No action needed; this email closes the earlier alert."
        )
    else:
        bits: list[str] = []
        if verdict.consecutive_failures:
            bits.append(f"{verdict.consecutive_failures} failed deploys")
        if verdict.stale and verdict.age_hours is not None:
            bits.append(f"prod {verdict.age_hours}h stale")
        detail = ", ".join(bits) if bits else (verdict.summary or "condition met")
        subject = f"[Modulo] DEPLOY HEALTH UNHEALTHY: {detail}"
        headline = "Deploy health is UNHEALTHY."
        action_line = (
            f"Investigate the deploy pipeline: {deploy_url}\n"
            'If no deploy is in flight, force one: gh workflow run "Deploy: Staging + Prod" '
            "--ref main -f force_deploy=true"
        )

    lines = [
        headline,
        "",
        "Measured condition(s):",
        f"  {verdict.reason}",
        "",
        "Figures:",
        (
            f"  Consecutive failed deploy runs : {verdict.consecutive_failures} "
            f"(threshold {verdict.failure_threshold}, over the last {verdict.runs_considered} completed runs)"
        ),
        f"  Production build SHA          : {verdict.prod_sha} (build timestamp {verdict.prod_build_ts})",
        f"  origin/main HEAD              : {verdict.main_sha}",
        (
            f"  Production build age          : {verdict.age_hours}h (grace {verdict.grace_hours}h)"
            if verdict.age_hours is not None
            else f"  Production build age          : unknown (grace {verdict.grace_hours}h)"
        ),
        "",
        "Evaluated every 30 minutes; a single failed deploy never alerts.",
        "",
        action_line,
        "",
        f"Workflow run: {run_url}",
    ]
    return subject, "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI (used by deploy-staleness-check.yml)
# ---------------------------------------------------------------------------


def _load_json(path: str | None) -> Any:
    """Load a JSON file, or ``None`` when the file is absent or unparseable.

    A failed API read is represented as an absent file by the workflow, which
    is exactly the UNKNOWN signal ``evaluate`` needs.
    """
    if not path:
        return None
    candidate = Path(path)
    if not candidate.is_file():
        return None
    try:
        raw = candidate.read_text(encoding="utf-8")
    except OSError:
        return None
    if not raw.strip():
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _write_outputs(outputs: dict[str, str], github_output: str | None) -> None:
    rendered = "\n".join(f"{key}={value}" for key, value in outputs.items())
    if github_output:
        with Path(github_output).open("a", encoding="utf-8") as handle:
            handle.write(rendered + "\n")
    else:
        print(rendered)


def _cmd_evaluate(args: argparse.Namespace) -> int:
    now = _parse_timestamp(args.now) if args.now else datetime.now(UTC)
    if now is None:
        print(f"invalid --now value: {args.now}", file=sys.stderr)
        return 2
    runs_raw = _load_json(args.runs_json)
    deploy_runs = runs_raw if isinstance(runs_raw, list) else None
    main_raw = _load_json(args.main_json)
    prod_raw = _load_json(args.prod_json)
    jobs_raw = _load_json(args.jobs_json) if args.jobs_json else None
    deploy_jobs = jobs_raw if isinstance(jobs_raw, dict) else None
    verdict = evaluate(
        main=main_raw if isinstance(main_raw, dict) else None,
        prod=prod_raw if isinstance(prod_raw, dict) else None,
        deploy_runs=deploy_runs,
        now=now,
        grace_hours=args.grace_hours,
        failure_threshold=args.failure_threshold,
        deploy_jobs=deploy_jobs,
    )
    if args.verdict_out:
        Path(args.verdict_out).write_text(json.dumps(verdict.as_dict()), encoding="utf-8")
    _write_outputs(verdict.as_outputs, args.github_output)
    if verdict.healthy is False:
        print(f"::error::DEPLOY HEALTH UNHEALTHY: {verdict.reason}")
    elif verdict.healthy is None:
        missing = ", ".join(verdict.missing_inputs) or "none readable"
        print(
            "::warning::Deploy health could not be evaluated this cycle - required input(s) missing: "
            f"{missing}. A partially readable tick is UNKNOWN, never healthy (FAR-2159)."
        )
    else:
        print(f"::notice::Deploy health OK: {verdict.reason}")
    return 0


def _cmd_action(args: argparse.Namespace) -> int:
    healthy: bool | None
    if args.healthy == "true":
        healthy = True
    elif args.healthy == "false":
        healthy = False
    else:
        healthy = None
    print(resolve_action(healthy, args.previous or UNKNOWN))
    return 0


def _cmd_previous_verdict(args: argparse.Namespace) -> int:
    payload = _load_json(args.jobs_json)
    print(parse_previous_verdict(payload if isinstance(payload, dict) else None))
    return 0


def _cmd_email(args: argparse.Namespace) -> int:
    raw = _load_json(args.verdict_json)
    if not isinstance(raw, dict):
        print("verdict JSON missing or unreadable", file=sys.stderr)
        return 2
    verdict = Verdict(**{key: raw[key] for key in raw if key in Verdict.__dataclass_fields__})
    subject, body = build_email(
        verdict=verdict,
        action=args.action,
        run_url=args.run_url,
        deploy_url=args.deploy_url,
    )
    print(json.dumps({"subject": subject, "body": body}))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    evaluate_parser = sub.add_parser("evaluate", help="evaluate deploy health and print step outputs")
    evaluate_parser.add_argument("--main-json")
    evaluate_parser.add_argument("--prod-json")
    evaluate_parser.add_argument("--runs-json")
    evaluate_parser.add_argument("--jobs-json", help="JSON file with job conclusions per run databaseId")
    evaluate_parser.add_argument("--grace-hours", type=int, default=GRACE_HOURS_DEFAULT)
    evaluate_parser.add_argument("--failure-threshold", type=int, default=FAILURE_THRESHOLD_DEFAULT)
    evaluate_parser.add_argument("--now", help="ISO timestamp (tests); defaults to now")
    evaluate_parser.add_argument("--github-output", help="append outputs here instead of stdout")
    evaluate_parser.add_argument("--verdict-out", help="write the verdict as JSON here")
    evaluate_parser.set_defaults(func=_cmd_evaluate)

    action_parser = sub.add_parser("action", help="alert / recovery / none for this tick")
    action_parser.add_argument("--healthy", default="", choices=("", "true", "false", "unknown"))
    action_parser.add_argument("--previous", default="", choices=("", "healthy", "unhealthy", "unknown"))
    action_parser.set_defaults(func=_cmd_action)

    previous_parser = sub.add_parser("previous-verdict", help="parse the previous run's verdict")
    previous_parser.add_argument("--jobs-json")
    previous_parser.set_defaults(func=_cmd_previous_verdict)

    email_parser = sub.add_parser("email", help="render the alert/recovery email as JSON")
    email_parser.add_argument("--verdict-json", required=True, help="file holding the Verdict fields")
    email_parser.add_argument("--action", required=True, choices=("alert", "recovery"))
    email_parser.add_argument("--run-url", default="")
    email_parser.add_argument("--deploy-url", default="")
    email_parser.set_defaults(func=_cmd_email)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
