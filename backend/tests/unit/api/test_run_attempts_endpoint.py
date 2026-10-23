"""GET /api/v1/runs/{run_id}/attempts — durable per-attempt history read (FAR-2078).

The route exposes the ``run_attempts`` rows written by the executor
instrumentation slice: one item per attempt, ordered by ``attempt_index``.

What is pinned here:

* **Order and shape** — items are ordered by ``attempt_index`` ascending even
  when the read helper hands them back unordered, ``total`` is the run's true
  attempt count and ``items`` is capped (the documented page cap).
* **Tenancy** — the run is loaded with the caller's ``organisation_id`` and a
  run that is missing OR owned by another organisation answers 404. It must
  never answer ``{"items": [], "total": 0}`` for a missing run: an empty page
  would hide typos and make a cross-tenant probe indistinguishable from a run
  with no recorded attempts.
* **Effective permission role** — the route reuses the run-detail gate
  (``run.status`` via ``require_permission_any_credential``) and that key
  resolves to ``viewer``. A valid-but-wrong key would silently change the
  gate, so the role is asserted against the registry, not merely that the
  dependency does not raise.
* **The route error convention** — ProgrammingError→501, SQLAlchemyError→503,
  IntegrityError→409, HTTPException re-raised, Exception→500.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError, ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.dependencies import _get_engine, _get_session_factory, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.api.routes import runs as runs_module
from modulo.auth.dependencies import get_current_tenant_user, get_current_tenant_user_or_api_key, get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal, TenantPrincipal
from modulo.auth.permissions import resolve_required
from modulo.settings import Settings, get_settings
from tests.unit.api.mock_session import configure_rls_preamble
from tests.unit.api.route_introspection import get_all_apiroutes, get_permission_tag

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_OTHER_ORG_ID = uuid.UUID("00000000-0000-0000-0000-00000000000a")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_RUN_ID = uuid.UUID("00000000-0000-0000-0000-0000000000aa")

_ATTEMPTS_PATH = "/api/v1/runs/{run_id}/attempts"
_RUN_DETAIL_PATH = "/api/v1/runs/{run_id}"


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",  # nosec - test-only value
    )


def _make_session() -> AsyncMock:
    session = AsyncMock(spec=AsyncSession)
    configure_rls_preamble(session)
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    # The permission gate's kill-switch read is the only query the real session
    # sees (the run and attempt reads are patched below); serve a plain
    # MagicMock result so its SYNC ``scalar_one_or_none()`` accessor cannot leak
    # an un-awaited coroutine (the fail-closed default is "enforce").
    authz_result = MagicMock()
    authz_result.scalar_one_or_none.return_value = None
    session.execute = AsyncMock(return_value=authz_result)
    return session


def _attempt(
    index: int,
    *,
    started_at: datetime | None = None,
    ended_at: datetime | None = None,
    terminal_reason: str | None = None,
    model_used: str | None = None,
    error_code: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        attempt_index=index,
        started_at=started_at if started_at is not None else datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
        ended_at=ended_at,
        terminal_reason=terminal_reason,
        model_used=model_used,
        error_code=error_code,
    )


def _make_run() -> MagicMock:
    run = MagicMock()
    run.id = _RUN_ID
    run.organisation_id = _ORG_ID
    return run


def _build_client(role: str) -> TestClient:
    session = _make_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[_get_session_factory] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="testuser",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role=role,
    )
    # The route gate (and the app's tenant principal) resolve through this
    # dependency; the role parameter is what the effective-role test varies.
    app.dependency_overrides[get_current_tenant_user] = lambda: TenantPrincipal(
        username="testuser",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role=role,
    )
    app.dependency_overrides[get_current_tenant_user_or_api_key] = lambda: TenantPrincipal(
        username="testuser",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role=role,
    )
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    return TestClient(app)


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    test_client = _build_client("admin")
    try:
        yield test_client
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def viewer_client() -> Generator[TestClient, None, None]:
    test_client = _build_client("viewer")
    try:
        yield test_client
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Order / response shape
# ---------------------------------------------------------------------------


def test_returns_attempts_in_index_order(client: TestClient) -> None:
    """Rows come back ordered by attempt_index, with nulls preserved on the wire.

    The helper is deliberately handed back UNORDERED: the response contract is
    index order, so the route must not depend on helper ordering alone.
    """
    attempts = [
        _attempt(2, terminal_reason="guard_cap", error_code="node_deadline_exceeded"),
        _attempt(0, ended_at=datetime(2026, 1, 1, 12, 5, tzinfo=UTC), terminal_reason="deadline", model_used="m-1"),
        _attempt(1),
    ]

    with (
        patch("modulo.api.routes.runs.get_run", new=AsyncMock(return_value=_make_run())),
        patch("modulo.api.routes.runs.db_list_attempts", new=AsyncMock(return_value=attempts)),
    ):
        resp = client.get(f"/api/v1/runs/{_RUN_ID}/attempts")

    assert resp.status_code == 200
    body = resp.json()
    assert [item["attempt_index"] for item in body["items"]] == [0, 1, 2]
    assert body["total"] == 3

    first, second, third = body["items"]
    assert first["attempt_index"] == 0
    assert first["started_at"].startswith("2026-01-01T12:00:00")
    assert first["ended_at"].startswith("2026-01-01T12:05:00")
    assert first["terminal_reason"] == "deadline"
    assert first["model_used"] == "m-1"
    assert second["ended_at"] is None
    assert second["terminal_reason"] is None
    assert second["model_used"] is None
    assert second["error_code"] is None
    assert third["terminal_reason"] == "guard_cap"
    assert third["error_code"] == "node_deadline_exceeded"


def test_item_page_is_capped_and_total_is_the_true_count(client: TestClient) -> None:
    """``items`` is capped; ``total`` still reports the run's attempt count."""
    attempts = [_attempt(index) for index in range(150)]

    with (
        patch("modulo.api.routes.runs.get_run", new=AsyncMock(return_value=_make_run())),
        patch("modulo.api.routes.runs.db_list_attempts", new=AsyncMock(return_value=attempts)),
    ):
        resp = client.get(f"/api/v1/runs/{_RUN_ID}/attempts")

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["items"]) == 100
    assert body["total"] == 150
    assert [item["attempt_index"] for item in body["items"]] == list(range(100))


def test_existing_run_with_no_attempts_returns_empty_page(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.runs.get_run", new=AsyncMock(return_value=_make_run())),
        patch("modulo.api.routes.runs.db_list_attempts", new=AsyncMock(return_value=[])),
    ):
        resp = client.get(f"/api/v1/runs/{_RUN_ID}/attempts")

    assert resp.status_code == 200
    assert resp.json() == {"items": [], "total": 0}


# ---------------------------------------------------------------------------
# Tenancy / 404 (never an empty list)
# ---------------------------------------------------------------------------


def test_missing_run_returns_404_and_never_lists_attempts(client: TestClient) -> None:
    list_mock = AsyncMock(return_value=[])

    with (
        patch("modulo.api.routes.runs.get_run", new=AsyncMock(return_value=None)),
        patch("modulo.api.routes.runs.db_list_attempts", new=list_mock),
    ):
        resp = client.get(f"/api/v1/runs/{_RUN_ID}/attempts")

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Run not found"
    list_mock.assert_not_awaited()


def test_foreign_org_run_is_looked_up_with_caller_org_and_returns_404(client: TestClient) -> None:
    """A run outside the caller's org is excluded by the org-scoped load → 404.

    This pins the tenancy mechanism: ``get_run`` must receive the CALLER's
    organisation id (defence-in-depth beside RLS), so a run owned by another
    org resolves to ``None`` and the route answers 404 rather than an empty
    list that would leak existence semantics.
    """
    get_run_mock = AsyncMock(return_value=None)

    with (
        patch("modulo.api.routes.runs.get_run", new=get_run_mock),
        patch("modulo.api.routes.runs.db_list_attempts", new=AsyncMock(return_value=[])),
    ):
        resp = client.get(f"/api/v1/runs/{_RUN_ID}/attempts")

    assert resp.status_code == 404
    get_run_mock.assert_awaited_once()
    call_args = get_run_mock.await_args
    assert call_args is not None
    assert call_args.args[1] == _RUN_ID
    assert call_args.kwargs["organisation_id"] == _ORG_ID
    assert call_args.kwargs["organisation_id"] != _OTHER_ORG_ID


# ---------------------------------------------------------------------------
# Permission: same key and effective role as the run-detail read
# ---------------------------------------------------------------------------


def _route(path: str) -> Any:
    for route in get_all_apiroutes(app):
        if route.path == path and "GET" in (route.methods or set()):
            return route
    raise AssertionError(f"GET {path} is not registered on the app")


def test_permission_tag_matches_run_detail_and_resolves_to_viewer() -> None:
    """The read reuses the run-detail gate, and that key resolves to ``viewer``.

    Asserting the resolved ROLE (not merely that the dependency exists) is the
    FAR-1964 requirement: a valid-but-wrong key such as a near-name sibling
    would resolve to a different role and silently move the gate.
    """
    attempts_tag = get_permission_tag(_route(_ATTEMPTS_PATH))
    detail_tag = get_permission_tag(_route(_RUN_DETAIL_PATH))

    assert attempts_tag is not None
    assert detail_tag is not None
    assert attempts_tag["permission"] == "run.status"
    assert attempts_tag["permission_kind"] == "tenant_or_api_key"
    # Same gate as the run detail endpoint — they must not drift apart.
    assert attempts_tag["permission"] == detail_tag["permission"]
    assert attempts_tag["permission_kind"] == detail_tag["permission_kind"]
    # The intended effective role, resolved from the registry itself.
    assert resolve_required(attempts_tag["permission"]) == "viewer"


def test_viewer_principal_can_read_attempts(viewer_client: TestClient) -> None:
    """The viewer-level key is honoured behaviourally end-to-end."""
    with (
        patch("modulo.api.routes.runs.get_run", new=AsyncMock(return_value=_make_run())),
        patch("modulo.api.routes.runs.db_list_attempts", new=AsyncMock(return_value=[_attempt(0)])),
    ):
        resp = viewer_client.get(f"/api/v1/runs/{_RUN_ID}/attempts")

    assert resp.status_code == 200
    assert resp.json()["total"] == 1


# ---------------------------------------------------------------------------
# Route error convention
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("error", "expected_status"),
    [
        (ProgrammingError("s", {}, Exception()), 501),
        (SQLAlchemyError("boom"), 503),
        (IntegrityError("s", {}, Exception()), 409),
        (RuntimeError("kaboom"), 500),
    ],
)
def test_route_error_convention(client: TestClient, error: Exception, expected_status: int) -> None:
    with patch("modulo.api.routes.runs.get_run", new=AsyncMock(side_effect=error)):
        resp = client.get(f"/api/v1/runs/{_RUN_ID}/attempts")

    assert resp.status_code == expected_status


def test_http_exception_from_the_load_is_re_raised(client: TestClient) -> None:
    """An HTTPException is never reclassified by the DB arms."""
    from fastapi import HTTPException

    # A mapped status (the app renders unknown statuses as a 500 problem), so
    # a reclassified exception would show up as a different code/detail.
    with patch(
        "modulo.api.routes.runs.get_run",
        new=AsyncMock(side_effect=HTTPException(status_code=403, detail="load refused")),
    ):
        resp = client.get(f"/api/v1/runs/{_RUN_ID}/attempts")

    assert resp.status_code == 403
    assert resp.json()["detail"] == "load refused"


def test_module_exports_the_attempt_read_code() -> None:
    """The log/audit code is registered beside the sibling run route codes."""
    assert runs_module._CODE_RUNS_LIST_RUN_ATTEMPTS == "runs.list_run_attempts"
