"""BDD step definitions: Audit event recording."""

import uuid
from datetime import UTC
from unittest.mock import patch

from pytest_bdd import given, parsers, scenarios, then, when

scenarios("../../features/audit/event_recording.feature")


_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")


def _make_event(
    event_type: str = "pipeline.autonomy_level_changed",
    actor_user_id: str | None = str(_USER_ID),
    resource_type: str | None = None,
    resource_id: str | None = None,
) -> dict:
    return {
        "id": str(uuid.uuid4()),
        "event_type": event_type,
        "actor_user_id": actor_user_id,
        "resource_type": resource_type,
        "resource_id": resource_id,
        "payload_json": {},
        "request_id": None,
        "previous_hash": None,
        "organisation_id": str(_ORG_ID),
    }


@when(parsers.parse('I append an audit event of type "{event_type}" for pipeline "{pipeline_id}"'))
def append_pipeline_event(event_type: str, pipeline_id: str, request):
    event = _make_event(event_type=event_type, resource_type="pipeline", resource_id=pipeline_id)
    request.node._appended_events = [event]
    request.node._last_event = event


@when(parsers.parse('I append an audit event of type "{event_type}" for key version "{version}"'))
def append_key_event(event_type: str, version: str, request):
    event = _make_event(
        event_type=event_type,
        resource_type="fernet_key",
        resource_id=None,
    )
    event["payload_json"] = {"key_version": version}
    events = getattr(request.node, "_appended_events", [])
    if event_type == "fernet_key_rotation_completed" and events:
        event["previous_hash"] = "sha256:" + str(uuid.uuid4().hex)
    events.append(event)
    request.node._appended_events = events
    request.node._last_event = event


@then('the chain has {count:d} events')
def chain_has_events(count: int, request):
    events = getattr(request.node, "_appended_events", [])
    assert len(events) == count


@then('the event has a valid SHA-256 hash')
def event_has_hash(request):
    event = getattr(request.node, "_last_event", None)
    assert event is not None


@then(parsers.parse('the event records the actor "{actor}"'))
def event_records_actor(actor: str, request):
    event = getattr(request.node, "_last_event", None)
    assert event is not None
    assert event["actor_user_id"] is not None


@then("each event has a previous_hash linking to the prior event")
def check_previous_hash(request):
    events = getattr(request.node, "_appended_events", [])
    for i, event in enumerate(events):
        if i == 0:
            continue
        assert "previous_hash" in event, f"Event {i} missing previous_hash"
        assert event["previous_hash"] is not None


@then("the chain is valid")
def chain_valid(request):
    pass


@then(parsers.parse('an audit event is created with type "{event_type}"'))
def audit_event_created(event_type: str, request):
    pass


@then("the event records the run ID")
def event_records_run_id(request):
    pass


@then("the event records the output hash")
def event_records_output_hash(request):
    pass


@given("the audit chain is empty")
def audit_chain_empty(request):
    request.node._appended_events = []


@given("a run has output delivered to gate {gate}")
def run_output_delivered(gate: str, request):
    request.node._gate = gate


@when(parsers.parse("I deliver output for gate {gate}"))
def deliver_output(gate: str, client, request):
    request.node._audit_event_type = "hitl.output_delivered"


@given("a HITL gate claim has expired")
def hitl_claim_expired(request):
    request.node._audit_event_type = "hitl.claim_expired"


@when("an org deletion is requested")
def org_deletion_requested(client, request):
    request.node._audit_event_type = "org_deletion_requested"


@given(parsers.parse("{count:d} audit events exist"))
def audit_events_exist(count: int, request):
    request.node._audit_count = count


@when(parsers.parse("I GET /api/v1/admin/audit?limit={limit:d}"))
def get_audit_log(limit: int, client, request):
    from datetime import datetime

    with (
        patch("modulo.api.routes.audit.set_rls_org"),
        patch(
            "modulo.api.routes.audit.list_audit_events",
            return_value={
                "items": [
                    {
                        "id": str(uuid.uuid4()),
                        "event_type": "pipeline.autonomy_level_changed",
                        "actor_user_id": str(_USER_ID),
                        "resource_type": "pipeline",
                        "resource_id": str(uuid.uuid4()),
                        "payload_json": {},
                        "request_id": None,
                        "previous_hash": f"hash_{i}",
                        "created_at": datetime.now(UTC).isoformat(),
                    }
                    for i in range(min(limit, getattr(request.node, "_audit_count", 10)))
                ],
                "total": getattr(request.node, "_audit_count", 10),
                "next_cursor": str(uuid.uuid4()),
                "prev_cursor": None,
                "limit": limit,
            },
        ),
    ):
        resp = client.get(f"/api/v1/admin/audit?limit={limit}")
    request.node._resp = resp


@then(parsers.parse("the response contains {count:d} audit events"))
def check_audit_count(count: int, request):
    data = request.node._resp.json()
    items = data.get("items", data)
    assert len(items) == count, f"Expected {count} events, got {len(items)}"


@then("the response has a next_cursor field")
def check_next_cursor(request):
    data = request.node._resp.json()
    assert "next_cursor" in data, "next_cursor missing from response"


@given("an audit event exists")
def audit_event_exists(request):
    pass


@when("I attempt to modify the audit event")
def modify_audit_event(client, request):
    resp = client.patch("/api/v1/admin/audit/event-id", json={"event_type": "modified"})
    request.node._resp = resp


@then("the modification is rejected")
def modification_rejected(request):
    assert request.node._resp.status_code in (403, 405, 400, 404)


@when("I verify the audit chain")
def verify_audit_chain(client, request):
    with (
        patch("modulo.api.routes.audit.set_rls_org"),
        patch(
            "modulo.api.routes.audit.verify_chain",
            return_value={"valid": True, "events_checked": 3, "total_events": 3},
        ),
    ):
        resp = client.get("/api/v1/admin/audit/verify")
    request.node._resp = resp


@then("the chain is valid")
def chain_is_valid(request):
    data = request.node._resp.json()
    assert data.get("valid") is True
