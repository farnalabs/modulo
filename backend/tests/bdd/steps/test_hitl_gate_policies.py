"""BDD step definitions: HITL gate policies (modify-then-approve, human_only,
overdue warnings) — the executing surface for the behaviours that were
previously unit-tested only (docs/product-map/hitl/hitl-gates.md Known Gaps).

The scenarios drive the real HITL routes through the authenticated TestClient
and the real ``overdue_warning.get_overdue_claims`` aggregation, patching only
the HITLManager (modify/expired branches) and the session reads, so the router
contract (200/403/410/422, resume payload carrying ``modified_output``) is
asserted end to end.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from tests.bdd.conftest import ORG_ID, USER_ID, _make_test_client

scenarios("../features/hitl/gate_policies.feature")


@pytest.fixture
def ctx() -> dict[str, Any]:
    """Shared mutable context dict for the gate-policy scenarios."""
    return {}


@pytest.fixture
def api_key_client(mock_session):
    """Authenticated principal carrying a non-browser credential (mk_ API key).

    ``via_api_key`` + ``client_kind="api_key"`` is exactly the credential class
    the human_only gate denies (cf. ``_enforce_human_only_gate``), so the
    human_only refusal scenarios exercise the real REST verdict without a
    browser JWT.
    """
    yield from _make_test_client(
        mock_session,
        username="agent",
        organisation_id=ORG_ID,
        account_id=USER_ID,
        org_role="admin",
        via_api_key=True,
        client_kind="api_key",
    )


@given("a browser reviewer is signed in")
def browser_reviewer_signed_in(ctx: dict[str, Any]) -> None:
    ctx["claim_token"] = "valid_token_" + uuid.uuid4().hex


@given(parsers.parse('a HITL gate "{gate_id}" is awaiting review'))
def hitl_gate_awaiting_review(ctx: dict[str, Any], gate_id: str) -> None:
    ctx["run_id"] = uuid.uuid4()
    ctx["gate_id"] = gate_id
    ctx["claim_token"] = ctx.get("claim_token") or ("valid_token_" + uuid.uuid4().hex)


@given("the browser reviewer holds the claim")
def browser_reviewer_holds_claim(ctx: dict[str, Any]) -> None:
    ctx["claim_token"] = "valid_token_" + uuid.uuid4().hex


@given("the claim token has expired")
def claim_token_expired(ctx: dict[str, Any]) -> None:
    ctx["claim_token_expired"] = True


@given("the gate is a human_only gate")
def gate_is_human_only(ctx: dict[str, Any]) -> None:
    ctx["human_only"] = True


_MODIFIED_OUTPUT = {"summary": "corrected by reviewer"}


@when("the reviewer approves with a modified output")
def reviewer_approves_with_modification(ctx: dict[str, Any], client, request) -> None:
    from modulo.core.hitl_manager import ClaimTokenExpiredError
    from modulo.core.pipeline_engine.executor import PipelineExecutor as RealExecutor

    body: dict[str, Any] = {"modified_output": _MODIFIED_OUTPUT}
    if ctx.get("claim_token_expired"):
        mgr = MagicMock()
        mgr.approve_with_modification = AsyncMock(side_effect=ClaimTokenExpiredError())
        with patch("modulo.api.routes.hitl.HITLManager", return_value=mgr):
            resp = client.post(
                f"/api/v1/runs/{ctx['run_id']}/hitl/{ctx['gate_id']}/approve-with-modification",
                json={**body, "claim_token": ctx["claim_token"]},
            )
    else:
        body["claim_token"] = ctx["claim_token"]
        mock_gate = MagicMock()
        mock_gate.run_id = ctx["run_id"]
        mock_gate.gate_id = ctx["gate_id"]
        mgr = MagicMock()
        mgr.approve_with_modification = AsyncMock(return_value=mock_gate)
        with (
            patch("modulo.api.routes.hitl.HITLManager", return_value=mgr),
            patch("modulo.api.routes.hitl.PipelineExecutor", spec=RealExecutor) as exec_cls,
        ):
            exec_cls.return_value.resume = AsyncMock()
            resp = client.post(
                f"/api/v1/runs/{ctx['run_id']}/hitl/{ctx['gate_id']}/approve-with-modification",
                json=body,
            )
        ctx["_resume"] = exec_cls.return_value.resume
    request.node._resp = resp
    ctx["modified_output"] = _MODIFIED_OUTPUT


@when("the reviewer approves with a modified output without a claim token")
def reviewer_approves_with_modification_no_claim_token(ctx: dict[str, Any], client, request) -> None:
    """Body validation rejects the missing required ``claim_token`` with a real 422."""
    resp = client.post(
        f"/api/v1/runs/{ctx['run_id']}/hitl/{ctx['gate_id']}/approve-with-modification",
        json={"modified_output": _MODIFIED_OUTPUT},
    )
    request.node._resp = resp


@then("the resume decision carries the modified output")
def resume_carries_modified_output(ctx: dict[str, Any]) -> None:
    resume = ctx.get("_resume")
    assert resume is not None, "Pipeline execution was not resumed"
    resume_data = resume.await_args.kwargs.get("resume_data", {}) if resume.await_args else {}
    assert resume_data.get("action") == "approved", f"Expected approved action, got {resume_data}"
    assert resume_data.get("modified_output") == _MODIFIED_OUTPUT, (
        f"Expected modified output in resume data, got {resume_data}"
    )


@when("an API-key credential approves the gate")
def api_key_credential_approves(ctx: dict[str, Any], api_key_client, request) -> None:
    with (
        patch(
            "modulo.api.routes.hitl.resolve_hitl_gate_config",
            new=AsyncMock(return_value={"human_only": True}),
        ),
        patch("modulo.api.routes.hitl._emit_human_only_denial_audit", new=AsyncMock()),
    ):
        resp = api_key_client.post(
            f"/api/v1/runs/{ctx['run_id']}/hitl/{ctx['gate_id']}/approve",
            json={"claim_token": ctx["claim_token"], "notes": None},
        )
    request.node._resp = resp


def _claim_mock(age_hours: int, gate_id: str) -> MagicMock:
    claim = MagicMock()
    claim.id = uuid.uuid4()
    claim.run_id = uuid.uuid4()
    claim.gate_id = gate_id
    claim.claimed_at = datetime.now(UTC) - timedelta(hours=age_hours)
    return claim


@given(parsers.parse("a claimed HITL gate has been held for {hours:d} hours"))
def claimed_gate_held_for(ctx: dict[str, Any], hours: int) -> None:
    ctx["held_hours"] = hours
    ctx["overdue_gate_id"] = "pre-deploy"


@when("the overdue claims are queried")
def overdue_claims_queried(ctx: dict[str, Any]) -> None:
    from modulo.core.hitl_manager.overdue_warning import get_overdue_claims

    result = MagicMock()
    result.scalars.return_value.all.return_value = [_claim_mock(ctx["held_hours"], ctx["overdue_gate_id"])]
    session = MagicMock()
    session.execute = AsyncMock(return_value=result)

    ctx["overdue_entries"] = asyncio.run(get_overdue_claims(session, ORG_ID, warning_hours=4, escalation_hours=24))


@then(parsers.parse('the claim is reported as "{status}" about {hours:d} hours old'))
def overdue_reported_with_age(ctx: dict[str, Any], status: str, hours: int) -> None:
    entries = ctx["overdue_entries"]
    assert entries, "no overdue claims were reported"
    entry = entries[0]
    assert entry["status"] == status, f"Expected status {status!r}, got {entry['status']!r}"
    assert abs(entry["age_hours"] - hours) < 1.0, f"Expected ~{hours}h old, got {entry['age_hours']}h"


@then(parsers.parse('the claim is reported as "{status}"'))
def overdue_reported(ctx: dict[str, Any], status: str) -> None:
    entries = ctx["overdue_entries"]
    assert entries, "no overdue claims were reported"
    assert entries[0]["status"] == status, f"Expected status {status!r}, got {entries[0]['status']!r}"
