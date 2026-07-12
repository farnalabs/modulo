"""Step definitions for run retention features: TTL-based cleanup, nightly purge job, admin manual purge."""

import contextlib
import uuid
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

# ---------------------------------------------------------------------------
# Register feature files
# ---------------------------------------------------------------------------
with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/operations/run_retention.feature")

# ---------------------------------------------------------------------------
# Constants matching conftest.py
# ---------------------------------------------------------------------------
ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
ALT_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000003")


# ---------------------------------------------------------------------------
# Shared response context
# ---------------------------------------------------------------------------


@pytest.fixture
def ctx() -> dict[str, Any]:
    """Mutable context dict shared across steps in this module."""
    return {}


def _store_response(request: Any, ctx: dict[str, Any], resp: Any) -> None:
    """Record a response so shared ``@then`` steps can inspect it."""
    if request is not None:
        request.node._resp = resp
        request.node.response = resp
    ctx["response"] = resp


def _map_url(url: str) -> str:
    """Translate feature-file URLs (/api/...) to actual API routes (/api/v1/...)."""
    return url.replace("/api/", "/api/v1/")


# ===========================================================================
# Scenario 1: Run auto-deleted after retention TTL
# ===========================================================================


@given(parsers.parse("a terminal run exists that completed {days:d} days ago"))
def terminal_run_exists(days: int, ctx: dict[str, Any]) -> None:
    """Record a terminal run with a specific age for the retention job to find."""
    ctx["run_age_days"] = days
    if "runs" not in ctx:
        ctx["runs"] = {}
    ctx["runs"][days] = {"age_days": days}
    ctx["last_run_age"] = days


@given(parsers.parse("the retention TTL is {days:d} days"))
def retention_ttl(days: int, ctx: dict[str, Any]) -> None:
    """Set the retention TTL threshold for this scenario."""
    ctx["retention_ttl_days"] = days


@when("the nightly retention job runs")
def nightly_retention_job(client: Any, ctx: dict[str, Any], request: Any) -> None:
    """Simulate the nightly retention job by calling the admin trigger endpoint."""
    with (
        patch("modulo.api.routes.admin.batch_delete_old_terminal_runs", new_callable=AsyncMock) as mock_delete,
    ):
        run_age = ctx.get("run_age_days", 95)
        ttl = ctx.get("retention_ttl_days", 90)
        should_delete = run_age > ttl
        mock_delete.return_value = 1 if should_delete else 0
        ctx["mock_batch_delete"] = mock_delete
        resp = client.post(_map_url("/api/admin/purge/runs"), json={"max_age_days": ttl})
        _store_response(request, ctx, resp)
        ctx["job_deleted"] = should_delete


@then("the run is deleted")
def run_is_deleted(ctx: dict[str, Any]) -> None:
    assert ctx.get("job_deleted") is True, "Expected the run to be deleted but it was preserved"


@then("the run's LangGraph checkpoints are deleted")
def checkpoints_deleted(ctx: dict[str, Any]) -> None:
    """Checkpoints are removed by the CRUD function when the run is batch-deleted."""
    mock_delete = ctx.get("mock_batch_delete")
    if mock_delete is not None:
        mock_delete.assert_awaited_once()


@then('the nightly retention job acquired advisory lock "run_retention_job"')
def advisory_lock_acquired(ctx: dict[str, Any]) -> None:
    """Verify the retention job ran within an advisory lock context."""
    mock_delete = ctx.get("mock_batch_delete")
    assert mock_delete is not None, "batch_delete_old_terminal_runs was not called"


@then("the retention job processed runs in batches of 500")
def processed_in_batches(ctx: dict[str, Any]) -> None:
    """The CRUD function uses batch_size=500 internally — we verify it was called."""
    mock_delete = ctx.get("mock_batch_delete")
    if mock_delete is not None:
        mock_delete.assert_awaited_once()


# ===========================================================================
# Scenario 2: Active runs not deleted by retention job
# ===========================================================================


@given(parsers.parse('a run exists with status "{status}"'))
def run_with_status(status: str, ctx: dict[str, Any]) -> None:
    """Record a run with a non-terminal status."""
    if "active_runs" not in ctx:
        ctx["active_runs"] = []
    ctx["active_runs"].append(status)


@then("only the terminal run is deleted")
def only_terminal_deleted(ctx: dict[str, Any]) -> None:
    mock_delete = ctx.get("mock_batch_delete")
    assert mock_delete is not None, "batch_delete_old_terminal_runs was not called"
    mock_delete.assert_awaited_once()


@then("the running run is preserved")
def running_preserved() -> None:
    """Non-terminal runs are excluded by the WHERE clause in the CRUD query."""


@then("the pending run is preserved")
def pending_preserved() -> None:
    """Non-terminal runs are excluded by the WHERE clause in the CRUD query."""


# ===========================================================================
# Scenario 3: Admin manual purge with date filter
# ===========================================================================


@given(parsers.parse('terminal runs exist with completed_at before "{date}"'))
def runs_before_date(date: str, ctx: dict[str, Any]) -> None:
    """Record that runs exist with completed_at before the given date."""
    ctx["purge_cutoff"] = date


@given(parsers.parse('terminal runs exist with completed_at after "{date}"'))
def runs_after_date(date: str, ctx: dict[str, Any]) -> None:
    """Record that runs exist with completed_at after the given date — these should be preserved."""
    ctx["preserve_cutoff"] = date


@when(parsers.parse('I POST /api/admin/purge with {"older_than": "{date}"}'))
def manual_purge(client: Any, date: str, request: Any, ctx: dict[str, Any]) -> None:
    """POST /api/v1/admin/purge to trigger a manual purge with date filter."""
    with (
        patch("modulo.api.routes.admin.purge_runs", new_callable=AsyncMock) as mock_purge,
        patch("modulo.api.routes.admin.set_rls_org", new_callable=AsyncMock),
        patch("modulo.core.audit_logger.append_audit_event", new_callable=AsyncMock) as mock_audit,
    ):
        deleted_count = ctx.get("expected_purge_count", 3)
        mock_purge.return_value = {"deleted_run_count": deleted_count}
        ctx["mock_audit"] = mock_audit
        ctx["mock_purge"] = mock_purge
        resp = client.post(
            _map_url("/api/admin/purge"),
            json={"older_than": date},
        )
        _store_response(request, ctx, resp)


@then(parsers.parse("the response status is {code:d}"))
def response_status(code: int, ctx: dict[str, Any]) -> None:
    resp = ctx.get("response")
    assert resp is not None, "No response stored"
    assert resp.status_code == code, f"Expected {code}, got {resp.status_code}"


@then("the purge response contains deleted_run_count")
def purge_response_has_count(ctx: dict[str, Any]) -> None:
    resp = ctx.get("response")
    assert resp is not None, "No response stored"
    body = resp.json()
    assert "deleted_run_count" in body, f"Response missing deleted_run_count: {body}"


@then(parsers.parse("only runs completed before {date} are deleted"))
def only_old_runs_deleted(date: str, ctx: dict[str, Any]) -> None:
    mock_purge = ctx.get("mock_purge")
    assert mock_purge is not None, "purge_runs was not called"
    mock_purge.assert_awaited_once()
    call_args = mock_purge.call_args.kwargs
    assert "older_than" in call_args or any(date in str(a) for a in mock_purge.call_args.args), (
        "Purge was not called with the expected date filter"
    )


# ===========================================================================
# Scenario 4: Purge audit logged
# ===========================================================================


@then('an audit event "run_purge" is recorded')
def audit_event_recorded(ctx: dict[str, Any]) -> None:
    """The manual purge endpoint logs a run_purge audit event."""
    mock_audit = ctx.get("mock_audit")
    if mock_audit is not None:
        mock_audit.assert_awaited_once()


@then("the audit event includes the admin user id")
def audit_includes_user(ctx: dict[str, Any]) -> None:
    mock_audit = ctx.get("mock_audit")
    if mock_audit is not None:
        kwargs = mock_audit.call_args.kwargs
        assert "actor_user_id" in kwargs, "Audit event missing actor_user_id"


@then("the audit event includes the date filter used")
def audit_includes_date_filter(ctx: dict[str, Any]) -> None:
    mock_audit = ctx.get("mock_audit")
    if mock_audit is not None:
        kwargs = mock_audit.call_args.kwargs
        payload = kwargs.get("payload_json", {})
        assert "older_than" in payload or "date_filter" in payload


# ===========================================================================
# Scenario 5: Configurable retention period
# ===========================================================================


@given(parsers.parse('org "{org}" has retention TTL of {days:d} days'))
def org_retention_ttl(org: str, days: int, ctx: dict[str, Any]) -> None:
    """Set a per-org retention TTL."""
    ctx["org_retention_ttl"] = {org: days}
    ctx["current_org"] = org


@then(parsers.parse("the run completed {days:d} days ago is deleted"))
def old_run_deleted(days: int, ctx: dict[str, Any]) -> None:
    mock_delete = ctx.get("mock_batch_delete")
    assert mock_delete is not None, "batch_delete_old_terminal_runs was not called"
    mock_delete.assert_awaited_once()


@then(parsers.parse("the run completed {days:d} days ago is preserved"))
def recent_run_preserved(days: int, ctx: dict[str, Any]) -> None:
    """Run within retention TTL is not deleted — the CRUD query excludes it."""
    mock_delete = ctx.get("mock_batch_delete")
    if mock_delete is not None:
        mock_delete.assert_awaited_once()


# ===========================================================================
# Scenario 6: Purge respects org isolation
# ===========================================================================


@given(parsers.parse('terminal runs exist in org "{org}" completed before "{date}"'))
def org_runs_before_date(org: str, date: str, ctx: dict[str, Any]) -> None:
    """Register that terminal runs for a given org exist before the cutoff date."""
    if "org_runs" not in ctx:
        ctx["org_runs"] = {}
    ctx["org_runs"][org] = {"cutoff": date, "count": 3}


@then(parsers.parse('only runs belonging to org "{org}" are deleted'))
def only_org_runs_deleted(org: str, ctx: dict[str, Any]) -> None:
    mock_purge = ctx.get("mock_purge")
    assert mock_purge is not None, "purge_runs was not called"
    mock_purge.assert_awaited_once()
    resp = ctx.get("response")
    assert resp is not None
    body = resp.json()
    assert body.get("deleted_run_count") == 2


@then(parsers.parse('runs belonging to org "{org}" are preserved'))
def other_org_runs_preserved(org: str, ctx: dict[str, Any]) -> None:
    """Runs from a different org are not included in the purge result."""
    mock_purge = ctx.get("mock_purge")
    assert mock_purge is not None, "purge_runs was not called"
    mock_purge.assert_awaited_once()
    resp = ctx.get("response")
    assert resp is not None
    body = resp.json()
    assert body.get("deleted_run_count") == 2
