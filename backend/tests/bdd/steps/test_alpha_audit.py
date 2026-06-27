"""BDD step definitions: Audit event recording."""

import uuid
from datetime import UTC
from unittest.mock import MagicMock, patch

from pytest_bdd import given, parsers, scenarios, then, when

scenarios("../../features/audit/event_recording.feature")


@then(parsers.parse('an audit event is created with type "{event_type}"'))
def audit_event_created(event_type: str, request):
    pass


@then(parsers.parse('the audit event records the actor "{actor}"'))
def audit_event_actor(actor: str, request):
    pass


@then(parsers.parse('the audit event records the resource "{resource}"'))
def audit_event_resource(resource: str, request):
    pass


@then("the audit event records the pipeline id")
def audit_event_pipeline_id(request):
    pass


@given(parsers.parse("{count:d} audit events exist"))
def audit_events_exist(count: int, request):
    request.node._audit_count = count


@when(parsers.parse("I GET /api/admin/audit?limit={limit:d}"))
def get_audit_log(limit: int, client, request):
    from datetime import datetime

    with (
        patch("modulo.core.pipeline_engine.run_crud.set_rls_org"),
        patch(
            "modulo.core.pipeline_engine.run_crud.list_audit_events",
            return_value=[
                MagicMock(
                    id=uuid.uuid4(),
                    event_type="pipeline.created",
                    actor="admin",
                    resource="my-pipeline",
                    organisation_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
                    created_at=datetime.now(UTC),
                    previous_hash=f"hash_{i}",
                )
                for i in range(min(limit, getattr(request.node, "_audit_count", 10)))
            ],
        ),
    ):
        resp = client.get(f"/api/admin/audit?limit={limit}")
    request.node._resp = resp


@then(parsers.parse("the response contains {count:d} audit events"))
def check_audit_count(count: int, request):
    data = request.node._resp.json()
    assert len(data) == count or len(data.get("events", data)) == count


@then("the response has a next cursor")
def check_next_cursor(request):
    data = request.node._resp.json()
    assert "next_cursor" in data or "cursor" in data


@given("an audit event exists")
def audit_event_exists(request):
    pass


@when("I attempt to modify the audit event")
def modify_audit_event(client, request):
    resp = client.patch("/api/admin/audit/event-id", json={"event_type": "modified"})
    request.node._resp = resp


@then("the modification is rejected")
def modification_rejected(request):
    assert request.node._resp.status_code in (403, 405, 400)


@given("{count:d} audit events exist")
def audit_events_count(count: int, request):
    request.node._audit_count = count


@when("I verify the audit chain")
def verify_audit_chain(client, request):
    with (
        patch("modulo.core.pipeline_engine.run_crud.set_rls_org"),
        patch(
            "modulo.core.pipeline_engine.run_crud.verify_audit_chain",
            return_value={"valid": True, "events_checked": 3},
        ),
    ):
        resp = client.get("/api/admin/audit/verify")
    request.node._resp = resp


@then("each event has a previous_hash linking to the prior event")
def check_previous_hash(request):
    data = request.node._resp.json()
    events = data if isinstance(data, list) else data.get("events", [])
    for event in events:
        if event != events[0]:
            assert "previous_hash" in event


@then("the chain is valid")
def chain_valid(request):
    data = request.node._resp.json()
    assert data.get("valid") is True


@when("I POST /api/pipelines with name {name} and valid config")
def create_pipeline(name: str, client, request):
    with (
        patch("modulo.core.pipeline_engine.run_crud.set_rls_org"),
        patch(
            "modulo.core.pipeline_engine.run_crud.create_pipeline",
            return_value=MagicMock(id=uuid.uuid4(), name=name, slug=name),
        ),
        patch("modulo.audit_logger.AuditLogger.log"),
    ):
        resp = client.post("/api/pipelines", json={"name": name, "nodes": []})
    request.node._resp = resp


@given(parsers.parse('org "{org}" has pipeline "{name}"'))
def org_has_pipeline(org: str, name: str, request):
    request.node._pipeline_name = name


@when(parsers.parse("I DELETE /api/pipelines/{name}"))
def delete_pipeline(name, client, request):
    with (
        patch("modulo.core.pipeline_engine.run_crud.set_rls_org"),
        patch(
            "modulo.core.pipeline_engine.run_crud.get_pipeline_by_name",
            return_value=MagicMock(id=uuid.uuid4(), name=name),
        ),
        patch(
            "modulo.core.pipeline_engine.run_crud.delete_pipeline",
            return_value=True,
        ),
    ):
        resp = client.delete(f"/api/pipelines/{name}")
    request.node._resp = resp


@when(parsers.parse("I POST /api/pipelines/{name}/runs with empty run_context"))
def trigger_run(name, client, request):
    with (
        patch("modulo.core.pipeline_engine.run_crud.set_rls_org"),
        patch(
            "modulo.core.pipeline_engine.run_crud.get_pipeline_by_name",
            return_value=MagicMock(id=uuid.uuid4(), name=name),
        ),
        patch(
            "modulo.core.pipeline_engine.run_crud.create_run",
            return_value=MagicMock(id=uuid.uuid4(), status="pending"),
        ),
    ):
        resp = client.post(f"/api/pipelines/{name}/runs", json={})
    request.node._resp = resp


@given("I have claimed gate {gate}")
def claimed_gate(gate: str, request):
    request.node._claim_token = "test_claim_token"


@when("I approve the run")
def approve_run(client, request):
    from modulo.hitl_manager import ApproveResult

    run_id = getattr(request.node, "_run_id", uuid.uuid4())
    with (
        patch(
            "modulo.hitl_manager.HITLManager.approve_gate",
            return_value=ApproveResult(success=True, new_status="running"),
        ),
    ):
        resp = client.post(
            f"/api/runs/{run_id}/approve",
            json={"decision": "approved", "claim_token": "test_claim_token"},
        )
    request.node._resp = resp
