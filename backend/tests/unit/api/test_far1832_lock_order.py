"""FAR-1832 (R4): the org-deletion confirm path takes the org lock FIRST.

``POST /api/v1/admin/org/deletion-confirm`` used to append the durable
org-independent system-audit event BEFORE calling
``db.crud.org_deletion.confirm_org_deletion`` — the one org-deletion FSM path
that performed an in-transaction write before the org-row ``FOR UPDATE`` inside
the crud call. The append itself took NO lock: ``system_audit_events`` is
deliberately not hash-chained and ``append_system_audit_event`` is a plain
INSERT, so there is no chain-head lock on this path. Every sibling FSM route
(request, cancel, immediate delete) locks the org row first and appends
afterwards.

These tests pin the repaired order: the crud call (org lock) precedes the
system-audit append, both still inside the same transaction (so atomicity and
the fail-closed append property are unchanged — only lock order moved).

Prove-the-fix: with the pre-reorder code the recorded order is
``["system_audit_append", "crud_org_lock"]`` and every assertion below fails.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, Generator
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context, get_settings
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.settings import Settings
from tests.unit.api.mock_session import configure_rls_preamble

_ORG_ID = "00000000-0000-0000-0000-000000000001"
_USER_ID = "00000000-0000-0000-0000-000000000002"
_URL = "/api/v1/admin/org/deletion-confirm"
_VALID_32 = "a" * 32


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
    )


def _make_session(events: list[str]) -> AsyncMock:
    """Mock session whose ``begin()`` records enter/exit around the handler body."""
    session = AsyncMock()
    configure_rls_preamble(session)
    session.in_transaction = MagicMock(return_value=True)
    session.info = {}

    @asynccontextmanager
    async def fake_begin():
        events.append("begin-enter")
        try:
            yield
        finally:
            events.append("begin-exit")

    session.begin = fake_begin
    session.execute = AsyncMock(return_value=MagicMock())
    return session


@pytest.fixture
def client(events: list[str]) -> Generator[TestClient, None, None]:
    session = _make_session(events)

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="admin",
        organisation_id=uuid.UUID(_ORG_ID),
        account_id=uuid.UUID(_USER_ID),
        org_role="admin",
    )
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def events() -> list[str]:
    return []


def test_r4_org_lock_precedes_the_system_audit_append(client: TestClient, events: list[str]) -> None:
    """The crud call (which takes the org-row FOR UPDATE) runs BEFORE the
    system-audit append — global order R6, repaired by FAR-1832 R4."""

    async def _record_confirm(*_args, **_kwargs):
        events.append("crud_org_lock")
        return {"deleted_organisation_id": _ORG_ID, "hard_deleted_runs": 0}

    async def _record_append(*_args, **_kwargs):
        events.append("system_audit_append")

    with (
        patch("modulo.db.crud.org_deletion.confirm_org_deletion", new=_record_confirm),
        patch("modulo.core.system_audit_logger.append_system_audit_event", new=_record_append),
        patch("modulo.api.routes.admin.set_rls_org", new=AsyncMock()),
        patch("modulo.api.routes.admin.set_rls_user_context", new=AsyncMock()),
    ):
        resp = client.post(_URL, json={"token": "tok"})

    assert resp.status_code == 200, resp.text
    assert events == ["begin-enter", "crud_org_lock", "system_audit_append", "begin-exit"], (
        "R4: the org lock must precede the system-audit append, and both must "
        f"stay inside the same transaction: {events}"
    )


def test_r4_failed_confirm_never_reaches_the_append(client: TestClient, events: list[str]) -> None:
    """A failed crud call (invalid token) aborts before the append — the audit
    row only exists for a confirm that actually held the lock (unchanged
    fail-closed atomicity after the reorder)."""

    async def _record_confirm(*_args, **_kwargs):
        events.append("crud_org_lock")
        raise ValueError("Invalid deletion token")

    async def _record_append(*_args, **_kwargs):
        events.append("system_audit_append")

    with (
        patch("modulo.db.crud.org_deletion.confirm_org_deletion", new=_record_confirm),
        patch("modulo.core.system_audit_logger.append_system_audit_event", new=_record_append),
        patch("modulo.api.routes.admin.set_rls_org", new=AsyncMock()),
        patch("modulo.api.routes.admin.set_rls_user_context", new=AsyncMock()),
    ):
        resp = client.post(_URL, json={"token": "wrong"})

    assert resp.status_code == 409, resp.text
    assert "system_audit_append" not in events, f"a failed confirm must not append: {events}"
    assert "crud_org_lock" in events


# ---------------------------------------------------------------------------
# QA F6: the same invariant for DELETE /api/v1/admin/orgs/{org_id} — the
# org-deletion crud (which takes the org row's DELETE lock) must precede the
# system-audit append. Pre-reorder the recorded order was
# ["system_audit_append", "crud_delete"] and every assertion below fails.
# ---------------------------------------------------------------------------

_DELETE_ORG_URL = f"/api/v1/admin/orgs/{_ORG_ID}"


@pytest.fixture
def system_admin_client(events: list[str]) -> Generator[TestClient, None, None]:
    """Like ``client`` but with the ``is_system_admin`` claim the route's
    ``require_system_permission`` gate demands."""
    session = _make_session(events)

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="admin",
        organisation_id=uuid.UUID(_ORG_ID),
        account_id=uuid.UUID(_USER_ID),
        org_role="admin",
        is_system_admin=True,
    )
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_f6_delete_org_lock_precedes_the_system_audit_append(
    system_admin_client: TestClient, events: list[str]
) -> None:
    """The crud delete (org-row lock) runs BEFORE the system-audit append —
    global order R6, repaired by FAR-1832 QA F6."""

    org = MagicMock()
    org.name = "Doomed"
    org.slug = "doomed"

    async def _record_get(*_args, **_kwargs):
        events.append("read_org")
        return org

    async def _record_delete(*_args, **_kwargs):
        events.append("crud_delete")
        return True

    async def _record_append(*_args, **_kwargs):
        events.append("system_audit_append")

    with (
        patch("modulo.api.routes.admin_orgs.get_organisation", new=_record_get),
        patch("modulo.api.routes.admin_orgs.delete_organisation", new=_record_delete),
        patch("modulo.core.system_audit_logger.append_system_audit_event", new=_record_append),
    ):
        resp = system_admin_client.delete(_DELETE_ORG_URL)

    assert resp.status_code == 204, resp.text
    assert events == ["begin-enter", "read_org", "crud_delete", "system_audit_append", "begin-exit"], (
        "F6: the org delete must precede the system-audit append, and both must "
        f"stay inside the same transaction: {events}"
    )


def test_f6_failed_delete_never_reaches_the_append(system_admin_client: TestClient, events: list[str]) -> None:
    """A failed crud delete aborts before the append — the audit row only
    exists for a delete that actually removed the org (fail-closed atomicity
    unchanged after the reorder)."""

    org = MagicMock()
    org.name = "Doomed"
    org.slug = "doomed"

    async def _record_get(*_args, **_kwargs):
        events.append("read_org")
        return org

    async def _record_delete(*_args, **_kwargs):
        events.append("crud_delete")
        return False

    async def _record_append(*_args, **_kwargs):
        events.append("system_audit_append")

    with (
        patch("modulo.api.routes.admin_orgs.get_organisation", new=_record_get),
        patch("modulo.api.routes.admin_orgs.delete_organisation", new=_record_delete),
        patch("modulo.core.system_audit_logger.append_system_audit_event", new=_record_append),
    ):
        resp = system_admin_client.delete(_DELETE_ORG_URL)

    assert resp.status_code == 404, resp.text
    assert "crud_delete" in events
    assert "system_audit_append" not in events, f"a failed delete must not append: {events}"
