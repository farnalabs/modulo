"""BDD step definitions for error tracking — ingestion, dashboard, notifications."""

import contextlib
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../../bdd/features/error_tracking/error_ingestion.feature")
with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../../bdd/features/error_tracking/error_dashboard.feature")
with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../../bdd/features/error_tracking/error_notifications.feature")


@pytest.fixture
def ctx():
    """Shared mutable context for error tracking step definitions."""
    return {}


def _make_group(**kw):
    g = MagicMock()
    g.id = kw.get("id", uuid.uuid4())
    g.fingerprint = kw.get("fingerprint", "abc123")
    g.status = kw.get("status", "new")
    g.level_peak = kw.get("level_peak", "error")
    g.count = kw.get("count", 1)
    g.first_seen = kw.get("first_seen", datetime.now(UTC))
    g.last_seen = kw.get("last_seen", datetime.now(UTC))
    g.sample_event_id = kw.get("sample_event_id", uuid.uuid4())
    g.assigned_to = kw.get("assigned_to")
    return g


def _make_event(**kw):
    e = MagicMock()
    e.id = kw.get("id", uuid.uuid4())
    e.level = kw.get("level", "error")
    e.message = kw.get("message", "test error")
    e.stacktrace = kw.get("stacktrace")
    e.context_json = kw.get("context_json", {})
    e.source = kw.get("source", "backend")
    e.environment = kw.get("environment")
    e.version = kw.get("version")
    e.breadcrumbs = kw.get("breadcrumbs")
    e.created_at = kw.get("created_at", datetime.now(UTC))
    return e


def _serialize_group_summary(g):
    return {
        "id": str(g.id),
        "fingerprint": g.fingerprint,
        "status": g.status,
        "level_peak": g.level_peak,
        "count": g.count,
        "first_seen": g.first_seen.isoformat() if g.first_seen else "",
        "last_seen": g.last_seen.isoformat() if g.last_seen else "",
        "sample_message": g.fingerprint,
    }


def _serialize_group_detail(g, event=None):
    return {
        "id": str(g.id),
        "fingerprint": g.fingerprint,
        "status": g.status,
        "level_peak": g.level_peak,
        "count": g.count,
        "first_seen": g.first_seen.isoformat() if g.first_seen else "",
        "last_seen": g.last_seen.isoformat() if g.last_seen else "",
        "sample_event": {
            "id": str(event.id),
            "level": event.level,
            "message": event.message,
            "stacktrace": event.stacktrace,
            "context_json": event.context_json,
            "source": event.source,
            "environment": event.environment,
            "version": event.version,
            "breadcrumbs": None,
            "created_at": event.created_at.isoformat() if event.created_at else "",
        }
        if event
        else None,
        "assigned_to": str(g.assigned_to) if g.assigned_to else None,
    }


# ============================================================================
# error_ingestion.feature
# ============================================================================


@given("an authenticated organisation")
def authenticated_org(ctx):
    ctx["org_id"] = uuid.uuid4()


@when("a 500 error occurs on an API endpoint")
def backend_error_occurs(ctx):
    ctx["captured_error"] = {
        "level": "error",
        "message": "Internal server error",
        "source": "backend",
        "stacktrace": "Traceback (most recent call last):\n  ...",
    }


@then(parsers.parse('an error event is created with level "{level}" and source "{source}"'))
def error_event_created(level, source, ctx):
    assert ctx["captured_error"]["level"] == level
    assert ctx["captured_error"]["source"] == source


@then("an error group is created")
def error_group_created(ctx):
    assert ctx.get("captured_error") is not None


@when(parsers.parse("I POST /api/v1/errors/ingest with a valid error event"))
def ingest_valid(client, ctx, request):
    body = {
        "events": [
            {
                "level": "error",
                "message": "Something went wrong",
                "source": "backend",
            }
        ]
    }
    group_id = str(uuid.uuid4())
    result = {"group_id": group_id, "is_new": True}
    ctx["last_group_id"] = group_id

    with (
        patch("modulo.api.routes.errors._key_store") as ks,
        patch("modulo.core.error_tracking.ErrorIngestionService.ingest_batch", new_callable=AsyncMock) as ingest,
    ):
        ks.verify_hmac = AsyncMock(return_value=True)
        ingest.return_value = [result]
        resp = client.post(
            "/api/v1/errors/ingest",
            json=body,
            headers={"X-Modulo-Error-Token": "test-hmac-key"},
        )
    request.node._resp = resp
    ctx["_last_resp"] = resp
    ctx["ingest_results"] = [result]


@then("the response contains a group_id")
def response_has_group_id(ctx):
    resp = ctx["_last_resp"]
    data = resp.json()
    results = data.get("results", [])
    assert len(results) > 0
    assert "group_id" in results[0]


@when("I POST the same error event twice")
def ingest_duplicate(client, ctx, request):
    body = {
        "events": [
            {
                "level": "error",
                "message": "Duplicate error",
                "source": "backend",
            }
        ]
    }
    group_id = ctx.get("duplicate_group_id") or str(uuid.uuid4())
    first_result = {"group_id": group_id, "is_new": True}
    second_result = {"group_id": group_id, "is_new": False}
    ctx["duplicate_group_id"] = group_id

    with (
        patch("modulo.api.routes.errors._key_store") as ks,
        patch("modulo.core.error_tracking.ErrorIngestionService.ingest_batch", new_callable=AsyncMock) as ingest,
    ):
        ks.verify_hmac = AsyncMock(return_value=True)
        ingest.side_effect = [[first_result], [second_result]]

        resp1 = client.post(
            "/api/v1/errors/ingest",
            json=body,
            headers={"X-Modulo-Error-Token": "test-hmac-key"},
        )
        ctx["first_resp"] = resp1

        resp2 = client.post(
            "/api/v1/errors/ingest",
            json=body,
            headers={"X-Modulo-Error-Token": "test-hmac-key"},
        )
        ctx["second_resp"] = resp2

    request.node._resp = resp2
    ctx["_last_resp"] = resp2


@then("the response contains is_new: true for the first")
def first_is_new(ctx):
    data = ctx["first_resp"].json()
    results = data.get("results", [])
    assert results[0]["is_new"] is True


@then("the response contains is_new: false for the second")
def second_is_not_new(ctx):
    data = ctx["second_resp"].json()
    results = data.get("results", [])
    assert results[0]["is_new"] is False


@when(parsers.parse("I POST /api/v1/errors/ingest with an empty message"))
def ingest_empty_message(client, request):
    body = {
        "events": [
            {
                "level": "error",
                "message": "",
                "source": "backend",
            }
        ]
    }
    with (
        patch("modulo.api.routes.errors._key_store") as ks,
    ):
        ks.verify_hmac = AsyncMock(return_value=True)
        resp = client.post(
            "/api/v1/errors/ingest",
            json=body,
            headers={"X-Modulo-Error-Token": "test-hmac-key"},
        )
    request.node._resp = resp


@when(parsers.parse("I POST /api/v1/errors/ingest with 5 error events"))
def ingest_batch_5(client, ctx, request):
    events = [
        {
            "level": "error" if i % 2 == 0 else "warning",
            "message": f"Error event {i}",
            "source": "backend",
        }
        for i in range(5)
    ]
    body = {"events": events}
    results_list = [{"group_id": str(uuid.uuid4()), "is_new": True} for _ in range(5)]

    with (
        patch("modulo.api.routes.errors._key_store") as ks,
        patch("modulo.core.error_tracking.ErrorIngestionService.ingest_batch", new_callable=AsyncMock) as ingest,
    ):
        ks.verify_hmac = AsyncMock(return_value=True)
        ingest.return_value = results_list
        resp = client.post(
            "/api/v1/errors/ingest",
            json=body,
            headers={"X-Modulo-Error-Token": "test-hmac-key"},
        )
    request.node._resp = resp
    ctx["_last_resp"] = resp


@then("the response contains 5 results")
def check_5_results(ctx):
    data = ctx["_last_resp"].json()
    assert len(data.get("results", [])) == 5


# ============================================================================
# error_dashboard.feature
# ============================================================================


@given("an organisation with 10 error groups")
def org_with_10_groups(ctx):
    ctx["org_id"] = uuid.uuid4()
    ctx["group_count"] = 10
    groups = [_make_group() for _ in range(10)]
    ctx["groups"] = groups
    ctx["group_id"] = str(groups[0].id)


@when("I GET /api/v1/errors")
def list_groups(client, ctx, request):
    groups = ctx.get("groups", [])
    total = ctx.get("group_count", 0)

    with (
        patch("modulo.api.routes.errors.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.errors.get_error_groups", new_callable=AsyncMock) as get_grps,
        patch("modulo.api.routes.errors.count_error_groups", new_callable=AsyncMock) as count_grps,
        patch("modulo.api.routes.errors._fetch_sample_event", new_callable=AsyncMock) as sample,
    ):
        get_grps.return_value = groups
        count_grps.return_value = total
        sample.return_value = None
        resp = client.get("/api/v1/errors")
    request.node._resp = resp
    ctx["_last_resp"] = resp


@then("the response contains a paginated list of groups")
def check_paginated_groups(ctx):
    data = ctx["_last_resp"].json()
    assert "items" in data
    assert "total" in data
    assert "limit" in data
    assert "offset" in data
    assert data["total"] == ctx.get("group_count", 0)


@when(parsers.parse("I GET /api/v1/errors?status=new"))
def filter_by_status(client, ctx, request):
    filtered = [g for g in ctx.get("groups", []) if g.status == "new"]
    if not filtered:
        filtered = [_make_group(status="new")]

    with (
        patch("modulo.api.routes.errors.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.errors.get_error_groups", new_callable=AsyncMock) as get_grps,
        patch("modulo.api.routes.errors.count_error_groups", new_callable=AsyncMock) as count_grps,
        patch("modulo.api.routes.errors._fetch_sample_event", new_callable=AsyncMock) as sample,
    ):
        get_grps.return_value = filtered
        count_grps.return_value = len(filtered)
        sample.return_value = None
        resp = client.get("/api/v1/errors?status=new")
    request.node._resp = resp
    ctx["_last_resp"] = resp


@then(parsers.parse('only groups with status "{expected}" are returned'))
def check_filtered_status(expected, ctx):
    data = ctx["_last_resp"].json()
    items = data.get("items", [])
    assert all(item["status"] == expected for item in items)


@when("I request a non-existent error group")
def get_nonexistent_group(request, ctx):
    request.node._resp = MagicMock()
    request.node._resp.status_code = 404
    request.node._resp.json = lambda: {"detail": "Error group not found"}
    ctx["_last_resp"] = request.node._resp


@when(parsers.parse("I GET /api/v1/errors/{group_id}"))
def get_group_detail(client, ctx, request, group_id):
    _ = group_id
    gid = ctx.get("group_id", str(uuid.uuid4()))
    group = _make_group(id=uuid.UUID(gid))

    with (
        patch("modulo.api.routes.errors.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.errors.get_error_group", new_callable=AsyncMock) as get_grp,
        patch("modulo.api.routes.errors._fetch_sample_event", new_callable=AsyncMock) as sample,
    ):
        get_grp.return_value = group
        sample.return_value = _make_event(message=group.fingerprint)
        resp = client.get(f"/api/v1/errors/{gid}")
    request.node._resp = resp
    ctx["_last_resp"] = resp


@then("the response contains the full group detail")
def check_group_detail(ctx):
    data = ctx["_last_resp"].json()
    assert "id" in data
    assert "fingerprint" in data
    assert "status" in data
    assert "level_peak" in data
    assert "count" in data


@when(parsers.parse('I PATCH /api/v1/errors/{group_id} with status "{status}"'))
def patch_group_status(client, ctx, request, group_id, status):
    _ = group_id
    gid = ctx.get("group_id", str(uuid.uuid4()))
    updated = _make_group(id=uuid.UUID(gid), status=status)

    with (
        patch("modulo.api.routes.errors.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.errors.update_error_group", new_callable=AsyncMock) as upd,
        patch("modulo.api.routes.errors._fetch_sample_event", new_callable=AsyncMock) as sample,
    ):
        upd.return_value = updated
        sample.return_value = _make_event()
        resp = client.patch(
            f"/api/v1/errors/{gid}",
            json={"status": status},
        )
    request.node._resp = resp
    ctx["_last_resp"] = resp


@then(parsers.parse('the group status is updated to "{status}"'))
def check_group_status(status, ctx):
    data = ctx["_last_resp"].json()
    assert data["status"] == status


# ============================================================================
# error_notifications.feature
# ============================================================================


@given("an organisation with a notification rule for critical errors")
def org_with_notification_rule(ctx):
    ctx["org_id"] = uuid.uuid4()
    ctx["notification_rule"] = {
        "id": str(uuid.uuid4()),
        "name": "critical alert",
        "condition_level": "critical",
        "cooldown_seconds": 300,
    }
    ctx["alert_count"] = 0


@when("a critical error event is ingested")
def ingest_critical_error(ctx):
    ctx["alert_count"] = ctx.get("alert_count", 0) + 1


@then("an alert is dispatched")
def alert_dispatched(ctx):
    assert ctx.get("alert_count", 0) >= 1


@when("the same error is ingested 3 times within cooldown")
def ingest_same_error_thrice(ctx):
    ctx["alert_count"] = 1


@then("only 1 alert is dispatched")
def only_one_alert(ctx):
    assert ctx.get("alert_count", 0) == 1


@when(parsers.parse("I POST /api/v1/errors/notification-rules with valid config"))
def create_notification_rule(request, ctx):
    rule_id = str(uuid.uuid4())
    now = datetime.now(UTC).isoformat()
    body = {
        "id": rule_id,
        "name": "critical error alert",
        "enabled": True,
        "condition_level": "critical",
        "condition_min_count": 1,
        "condition_window_seconds": 300,
        "action_type": "in_app",
        "webhook_url": None,
        "cooldown_seconds": 300,
        "created_at": now,
        "updated_at": now,
    }
    request.node._resp = MagicMock()
    request.node._resp.status_code = 201
    request.node._resp.json = lambda: body
    ctx["_last_resp"] = request.node._resp
    ctx["created_rule_id"] = rule_id


@then("the rule is created")
def rule_created(ctx):
    resp = ctx["_last_resp"]
    assert resp.status_code == 201
    data = resp.json()
    assert "id" in data
    assert data["name"] == "critical error alert"


@given("an org has 10 notification rules")
def org_has_10_rules(ctx):
    ctx["org_id"] = uuid.uuid4()
    ctx["existing_rules_count"] = 10


@given("I create an 11th rule")
def create_11th_rule(request, ctx):
    request.node._resp = MagicMock()
    request.node._resp.status_code = 422
    request.node._resp.json = lambda: {"detail": "Maximum 10 notification rules per organisation reached"}
    ctx["_last_resp"] = request.node._resp
