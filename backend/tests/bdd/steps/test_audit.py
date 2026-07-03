"""BDD step definitions: Audit Viewer (browse, filter, verify, export)."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch

from pytest_bdd import given, parsers, scenarios, then, when

scenarios("../../features/audit/audit_viewer.feature")

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_NOW = datetime(2025, 6, 1, tzinfo=UTC)


def _fake_event(
    event_id: str | None = None,
    event_type: str = "pipeline.run",
    actor_user_id: str | None = str(_USER_ID),
    resource_type: str | None = "pipeline",
    created_at: datetime | None = None,
) -> dict[str, Any]:
    uid = event_id or str(uuid.uuid4())
    return {
        "id": uid,
        "event_type": event_type,
        "actor_user_id": actor_user_id,
        "resource_type": resource_type,
        "resource_id": str(uuid.uuid4()),
        "payload_json": {"key": "value", "event_id": uid},
        "request_id": "req-123",
        "previous_hash": "abc",
        "created_at": (created_at or _NOW).isoformat(),
    }


def _make_events(
    count: int,
    event_type: str = "pipeline.run",
    actor_user_id: str | None = str(_USER_ID),
    org_id: uuid.UUID = _ORG_ID,
    start_time: datetime | None = None,
) -> list[dict[str, Any]]:
    base = start_time or _NOW
    return [
        _fake_event(
            event_id=str(uuid.uuid4()),
            event_type=event_type,
            actor_user_id=actor_user_id,
            created_at=base + timedelta(minutes=i),
        )
        for i in range(count)
    ]


# -- Basic list / pagination --


@given(parsers.parse("{count:d} audit events exist"))
def audit_events_exist(count: int, request) -> None:
    events = _make_events(count)
    request.node._audit_events = events


@given("no audit events exist")
def no_audit_events(request) -> None:
    request.node._audit_events = []


@given(parsers.parse("audit events exist with types {types}"))
def audit_events_with_types(types: str, request) -> None:
    type_list = [t.strip().strip('"') for t in types.split(",")]
    events = [_fake_event(event_type=t) for t in type_list]
    request.node._audit_events = events


@given(parsers.parse("audit events exist from {from_date} to {to_date}"))
def audit_events_date_range(from_date: str, to_date: str, request) -> None:
    start = datetime.fromisoformat(from_date.strip('"').replace("Z", "+00:00"))
    end = datetime.fromisoformat(to_date.strip('"').replace("Z", "+00:00"))
    mid = start + (end - start) / 2
    events = [
        _fake_event(created_at=start),
        _fake_event(created_at=mid),
        _fake_event(created_at=end),
    ]
    request.node._audit_events = events


@given(parsers.parse("audit events exist for user {user_id}"))
def audit_events_for_user(user_id: str, request) -> None:
    events = [
        _fake_event(actor_user_id=user_id),
        _fake_event(actor_user_id=user_id),
        _fake_event(actor_user_id=str(uuid.uuid4())),
    ]
    request.node._audit_events = events


@given(parsers.parse("{count:d} audit events exist with IDs {ids}"))
def audit_events_with_ids(count: int, ids: str, request) -> None:
    id_list = [i.strip().strip('"') for i in ids.split(",")]
    events = [_fake_event(event_id=eid) for eid in id_list[:count]]
    while len(events) < count:
        events.append(_fake_event())
    request.node._audit_events = events


@given(parsers.parse("the audit chain contains {count:d} events with valid hashes"))
def audit_chain_valid(count: int, request) -> None:
    prev = "0" * 64
    events = []
    for i in range(count):
        e = _fake_event()
        e["previous_hash"] = prev
        prev = f"hash_{i:04d}"
        events.append(e)
    request.node._audit_events = events


@given("the audit_viewer feature is disabled")
def audit_viewer_disabled(request) -> None:
    request.node._feature_disabled = True


@given(parsers.parse("org {org} has {count:d} audit events"))
def org_has_audit_events(org: str, count: int, request) -> None:
    events = _make_events(count)
    request.node._audit_events = events
    request.node._org = org


# -- When steps --


@when("I GET /api/v1/admin/audit")
def get_audit_list(request) -> None:
    from modulo.api.dependencies import get_plan_context

    client = request.getfixturevalue("alt_org_client" if getattr(request.node, "_alt_org", None) else "client")
    events = getattr(request.node, "_audit_events", [])

    if getattr(request.node, "_feature_disabled", False):

        async def _disabled_ctx():
            from unittest.mock import MagicMock

            ctx = MagicMock()
            ctx.feature_enabled.return_value = False
            return ctx

        from modulo.api.dependencies import get_plan_context
        from modulo.api.main import app

        app.dependency_overrides[get_plan_context] = _disabled_ctx
        resp = client.get("/api/v1/admin/audit")
        del app.dependency_overrides[get_plan_context]
        request.node._resp = resp
        return

    if getattr(request.node, "_alt_org", None) == "other-corp":
        with (
            patch(
                "modulo.api.routes.audit.list_audit_events",
                return_value={
                    "items": [],
                    "total": 0,
                    "next_cursor": None,
                    "prev_cursor": None,
                    "limit": 50,
                },
            ),
            patch("modulo.api.routes.audit.set_rls_org"),
        ):
            resp = client.get("/api/v1/admin/audit")
        request.node._resp = resp
        return

    with (
        patch(
            "modulo.api.routes.audit.list_audit_events",
            return_value={
                "items": events,
                "total": len(events),
                "next_cursor": str(uuid.uuid4()) if events else None,
                "prev_cursor": None,
                "limit": 50,
            },
        ),
        patch("modulo.api.routes.audit.set_rls_org"),
    ):
        resp = client.get("/api/v1/admin/audit")
    request.node._resp = resp


@when(parsers.parse("I GET /api/v1/admin/audit?{query}"))
def get_audit_list_with_params(client, request, query: str) -> None:
    events = getattr(request.node, "_audit_events", [])
    params = dict(p.split("=", 1) for p in query.split("&"))

    filtered = list(events)

    if "event_type" in params:
        filtered = [e for e in filtered if e["event_type"] == params["event_type"]]

    if "user_id" in params:
        filtered = [e for e in filtered if e["actor_user_id"] == params["user_id"]]

    if "from_date" in params and "to_date" in params:
        fd = datetime.fromisoformat(params["from_date"].replace("Z", "+00:00"))
        td = datetime.fromisoformat(params["to_date"].replace("Z", "+00:00"))
        filtered = [e for e in filtered if fd <= datetime.fromisoformat(e["created_at"]) <= td]

    with (
        patch(
            "modulo.api.routes.audit.list_audit_events",
            return_value={
                "items": filtered,
                "total": len(filtered),
                "next_cursor": str(uuid.uuid4()) if filtered else None,
                "prev_cursor": None,
                "limit": 50,
            },
        ),
        patch("modulo.api.routes.audit.set_rls_org"),
    ):
        resp = client.get(f"/api/v1/admin/audit?{query}")
    request.node._resp = resp


@when(parsers.parse("I POST /api/v1/admin/audit/batch-detail with event_ids {ids}"))
def post_batch_detail(client, request, ids: str) -> None:
    events = getattr(request.node, "_audit_events", [])
    id_list = [i.strip().strip('"') for i in ids.split(",")]
    matched = [e for e in events if e["id"] in id_list]

    with (
        patch("modulo.api.routes.audit.get_audit_events_batch", return_value=matched),
        patch("modulo.api.routes.audit.set_rls_org"),
    ):
        resp = client.post(
            "/api/v1/admin/audit/batch-detail",
            json={"event_ids": id_list},
            headers={"Authorization": "Bearer test"},
        )
    request.node._resp = resp


@when("I GET /api/v1/admin/audit/verify")
def get_verify_chain(client, request) -> None:
    events = getattr(request.node, "_audit_events", [])
    valid = all(events[i]["previous_hash"] is not None for i in range(1, len(events)))

    with (
        patch(
            "modulo.api.routes.audit.verify_chain",
            return_value={"valid": valid, "events_checked": len(events), "chain_break": None},
        ),
        patch("modulo.api.routes.audit.set_rls_org"),
    ):
        resp = client.get("/api/v1/admin/audit/verify")
    request.node._resp = resp


@when(parsers.parse("I GET /api/v1/admin/audit/export?{query}"))
def get_export(client, request, query: str) -> None:
    events = getattr(request.node, "_audit_events", [])
    params = dict(p.split("=", 1) for p in query.split("&"))
    page = int(params.get("page", 1))
    page_size = int(params.get("page_size", 100))
    start = (page - 1) * page_size
    end = start + page_size
    page_events = events[start:end]

    with (
        patch(
            "modulo.api.routes.audit.export_chain",
            return_value={
                "items": page_events,
                "total": len(events),
                "page": page,
                "page_size": page_size,
            },
        ),
        patch("modulo.api.routes.audit.set_rls_org"),
    ):
        resp = client.get(f"/api/v1/admin/audit/export?{query}")
    request.node._resp = resp


@when(parsers.parse("I authenticate as a user in org {org}"))
def authenticate_as_org(org: str, request) -> None:
    request.node._alt_org = org.strip('"')


# -- Then steps --


@then(parsers.parse("the response contains {count:d} audit events"))
def check_audit_count(count: int, request) -> None:
    data = request.node._resp.json()
    items = data.get("items", data)
    assert len(items) == count, f"Expected {count} events, got {len(items)}"


@then("the response has a next_cursor field")
def check_next_cursor(request) -> None:
    data = request.node._resp.json()
    assert "next_cursor" in data, "next_cursor missing from response"


@then(parsers.parse("the response has a total field of {total:d}"))
def check_total(request, total: int) -> None:
    data = request.node._resp.json()
    assert data.get("total") == total, f"Expected total={total}, got {data.get('total')}"


@then("the response has a total field")
def check_total_exists(request) -> None:
    data = request.node._resp.json()
    assert "total" in data, "total missing from response"


@then(parsers.parse("the response has a page field of {page:d}"))
def check_page(request, page: int) -> None:
    data = request.node._resp.json()
    assert data.get("page") == page, f"Expected page={page}, got {data.get('page')}"


@then(parsers.parse("the response has a page_size field of {page_size:d}"))
def check_page_size(request, page_size: int) -> None:
    data = request.node._resp.json()
    assert data.get("page_size") == page_size, f"Expected page_size={page_size}, got {data.get('page_size')}"


@then(parsers.parse("the response contains only events with event_type {event_type}"))
def check_filtered_by_type(request, event_type: str) -> None:
    data = request.node._resp.json()
    items = data.get("items", data)
    for e in items:
        assert e["event_type"] == event_type.strip('"'), f"Expected event_type '{event_type}', got '{e['event_type']}'"


@then("all returned events are within the date range")
def check_date_range(request) -> None:
    data = request.node._resp.json()
    items = data.get("items", data)
    for e in items:
        dt = datetime.fromisoformat(e["created_at"])
        assert dt >= datetime(2025, 1, 1, tzinfo=UTC), f"Event before from_date: {dt}"
        assert dt <= datetime(2025, 6, 1, tzinfo=UTC), f"Event after to_date: {dt}"


@then(parsers.parse("all returned events have actor_user_id {user_id}"))
def check_filtered_by_user(request, user_id: str) -> None:
    data = request.node._resp.json()
    items = data.get("items", data)
    for e in items:
        assert e["actor_user_id"] == user_id.strip('"'), (
            f"Expected actor_user_id '{user_id}', got '{e['actor_user_id']}'"
        )


@then(parsers.parse("the response contains full details for {count:d} events"))
def check_batch_detail_count(request, count: int) -> None:
    data = request.node._resp.json()
    assert len(data) == count, f"Expected {count} events in batch detail, got {len(data)}"


@then("the response includes payload_json for each event")
def check_batch_detail_payload(request) -> None:
    data = request.node._resp.json()
    for e in data:
        assert "payload_json" in e, "payload_json missing from batch detail event"
        assert isinstance(e["payload_json"], dict), "payload_json should be a dict"


@then(parsers.parse("the chain verification result is {expected}"))
def check_verify_result(request, expected: str) -> None:
    data = request.node._resp.json()
    expected_bool = expected.strip('"') == "valid"
    assert data.get("valid") is expected_bool, f"Expected valid={expected_bool}, got {data.get('valid')}"


@then(parsers.parse("the export contains {count:d} events"))
def check_export_count(request, count: int) -> None:
    data = request.node._resp.json()
    items = data.get("items", [])
    assert len(items) == count, f"Expected {count} exported events, got {len(items)}"


@then(parsers.parse("the response detail mentions {keyword}"))
def check_response_detail(request, keyword: str) -> None:
    data = request.node._resp.json()
    detail = data.get("detail", "")
    assert keyword.strip('"') in detail.lower(), f"Expected '{keyword}' in response detail, got '{detail}'"
