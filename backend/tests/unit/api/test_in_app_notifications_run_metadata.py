"""FAR-1234 â€” run-linked notifications: point-in-time metadata + dismissal.

Drives the REAL in-app notification endpoints against a real SQLite session
(no CRUD mocking) so the two defects in the ticket are reproduced end to end:

1. **Point-in-time metadata.** A ``hitl.awaiting`` notification is written when
   the gate fires and never re-stamped, so its body still claims the run is
   waiting for review long after the run was cancelled. ``GET /dashboard`` and
   ``GET /api/v1/notifications/in-app`` must therefore resolve the linked
   run's CURRENT state and expose ``run_id`` / ``run_status`` / ``run_terminal``
   / ``run_cancel_reason`` (PROVE-THE-FIX: these keys do not exist without the
   change).

2. **Dismissal of run-linked / stopped-run notifications.** ``POST .../dismiss``
   must succeed for a notification whose linked run is terminal, and the
   notification must then leave the ``status=active`` read path and appear
   under ``status=dismissed_self``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from modulo.api.dependencies import get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_tenant_user, get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal, TenantPrincipal
from modulo.db.models.account import Account
from modulo.db.models.base import Base
from modulo.db.models.notification import Dismissal, Notification, NotificationPreference
from modulo.db.models.run import Run
from modulo.settings import Settings, get_settings

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_RUN_ID = uuid.UUID("00000000-0000-0000-0000-0000000000a1")

_BASE = "/api/v1/notifications/in-app"
_DASHBOARD_PATH = f"{_BASE}/dashboard"

_TABLES = [
    Account.__table__,
    Notification.__table__,
    NotificationPreference.__table__,
    Dismissal.__table__,
    Run.__table__,
]


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key="a" * 32,
        fernet_key="a" * 32,
        modulo_admin_password="testpass",
    )


@pytest.fixture
def client(tmp_path: Path) -> Generator[TestClient, None, None]:
    """Per-test temp-file SQLite DB seeded with a CANCELLED run plus a
    run-linked ``hitl.awaiting`` notification (the FAR-1234 reproduction)."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'notif_run_meta.db'}", echo=False)
    seeded = False

    async def override_session() -> AsyncGenerator[AsyncSession, None]:
        nonlocal seeded
        async with engine.begin() as conn:
            await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=_TABLES))
        maker = async_sessionmaker(engine, expire_on_commit=False, autobegin=False)
        async with maker() as session:
            if not seeded:
                async with session.begin():
                    session.add(
                        Account(
                            id=_USER_ID,
                            email="admin@example.com",
                            display_name="Admin",
                            auth_provider="local",
                            preferences={},
                            active=True,
                            is_system_admin=False,
                            is_break_glass=False,
                            created_at=datetime.now(UTC),
                            updated_at=datetime.now(UTC),
                        )
                    )
                    session.add(
                        Run(
                            id=_RUN_ID,
                            organisation_id=_ORG_ID,
                            pipeline_id=uuid.uuid4(),
                            snapshot_id=uuid.uuid4(),
                            trigger_type="manual",
                            status="cancelled",
                            run_number=1,
                            input_hash="a" * 64,
                            langgraph_thread_id=f"thread-{_RUN_ID}",
                            cancel_reason="user_requested",
                            cancelled_by=str(_USER_ID),
                            started_at=datetime.now(UTC) - timedelta(hours=2),
                            completed_at=datetime.now(UTC) - timedelta(hours=1),
                        )
                    )
                    session.add(
                        Notification(
                            organisation_id=_ORG_ID,
                            scope="org",
                            level="info",
                            category="hitl.awaiting",
                            title="HITL review needed â€” Improve Security",
                            body='Pipeline "Improve Security" is waiting for human review.',
                            action_url=f"/runs/{_RUN_ID}",
                            expires_at=datetime.now(UTC) + timedelta(days=3),
                            created_at=datetime.now(UTC) - timedelta(days=1),
                            updated_at=datetime.now(UTC) - timedelta(days=1),
                        )
                    )
                seeded = True
            yield session

    def _current_user() -> AuthenticatedPrincipal:
        return AuthenticatedPrincipal(
            username="admin",
            organisation_id=_ORG_ID,
            account_id=_USER_ID,
            org_role="admin",
        )

    def _current_tenant_user() -> TenantPrincipal:
        return TenantPrincipal(
            username="admin",
            organisation_id=_ORG_ID,
            account_id=_USER_ID,
            org_role="admin",
        )

    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[get_current_user] = _current_user
    app.dependency_overrides[get_current_tenant_user] = _current_tenant_user
    app.dependency_overrides[get_plan_context] = lambda: mock_plan

    with (
        patch("modulo.api.routes.in_app_notifications.set_rls_org", new=AsyncMock()),
        patch("modulo.api.routes.in_app_notifications.set_rls_user_context", new=AsyncMock()),
    ):
        yield TestClient(app)

    app.dependency_overrides.clear()


def _single_item(resp: object) -> dict[str, object]:
    assert resp.status_code == 200, resp.text  # type: ignore[attr-defined]
    payload = resp.json()  # type: ignore[attr-defined]
    items = payload["notifications"] if "notifications" in payload else payload["items"]
    assert len(items) == 1, payload
    return items[0]


def test_dashboard_reports_current_run_state(client: TestClient) -> None:
    """PROVE-THE-FIX (metadata): the dashboard resolves the linked run NOW.

    The notification body still says the pipeline "is waiting for human
    review"; without read-time enrichment that stale claim is all the client
    has. The response must carry the run's current status and terminal flag.
    """
    item = _single_item(client.get(_DASHBOARD_PATH))

    assert item["run_id"] == str(_RUN_ID)
    assert item["run_status"] == "cancelled"
    assert item["run_terminal"] is True
    assert item["run_cancel_reason"] == "user_requested"


def test_notification_list_reports_current_run_state(client: TestClient) -> None:
    """The full inbox (the /notifications page) enriches identically."""
    resp = client.get(f"{_BASE}?page=1&page_size=20&status=active")
    item = _single_item(resp)

    assert item["run_id"] == str(_RUN_ID)
    assert item["run_status"] == "cancelled"
    assert item["run_terminal"] is True
    assert item["run_cancel_reason"] == "user_requested"


def test_notification_detail_reports_current_run_state(client: TestClient) -> None:
    """The detail read resolves the same metadata as the list reads."""
    listed = _single_item(client.get(f"{_BASE}?page=1&page_size=20&status=active"))
    detail = client.get(f"{_BASE}/{listed['id']}")

    assert detail.status_code == 200, detail.text
    item = detail.json()
    assert item["run_id"] == str(_RUN_ID)
    assert item["run_status"] == "cancelled"
    assert item["run_terminal"] is True
    assert item["run_cancel_reason"] == "user_requested"


def test_run_metadata_fields_have_safe_defaults(client: TestClient) -> None:
    """Contract shape: every enrichment field is always present, never absent.

    Guards the wire contract against a backend that only sets the fields for
    run-linked rows (a partially-populated payload breaks the generated
    OpenAPI types on the client).
    """
    item = _single_item(client.get(_DASHBOARD_PATH))
    for field in ("run_id", "run_status", "run_terminal", "run_cancel_reason"):
        assert field in item, f"{field} missing from NotificationResponse"


def test_stopped_run_notification_is_dismissible_end_to_end(client: TestClient) -> None:
    """PROVE-THE-FIX (dismissal): a run-linked notification for a CANCELLED run
    can be dismissed, leaves the active read path, and lands in the user's
    dismissed list."""
    active = _single_item(client.get(f"{_BASE}?page=1&page_size=20&status=active"))
    notification_id = active["id"]

    dismiss = client.post(f"{_BASE}/{notification_id}/dismiss", json={"dismiss_scope": "self"})
    assert dismiss.status_code == 200, dismiss.text
    assert dismiss.json() == {"status": "dismissed_for_self"}

    still_active = client.get(f"{_BASE}?page=1&page_size=20&status=active")
    assert still_active.status_code == 200
    assert not still_active.json()["items"], "a dismissed notification must leave the active list"

    dismissed = client.get(f"{_BASE}?page=1&page_size=20&status=dismissed_self")
    assert dismissed.status_code == 200
    assert [row["id"] for row in dismissed.json()["items"]] == [str(notification_id)]

    dashboard = client.get(_DASHBOARD_PATH)
    assert dashboard.status_code == 200
    assert not dashboard.json()["notifications"], "a dismissed notification must leave the dashboard"


def test_review_later_also_clears_a_stopped_run_notification(client: TestClient) -> None:
    """The dashboard's other clear affordance ("Review Later") clears it too."""
    active = _single_item(client.get(f"{_BASE}?page=1&page_size=20&status=active"))
    notification_id = active["id"]

    review = client.post(f"{_BASE}/{notification_id}/review-later")
    assert review.status_code == 200, review.text

    still_active = client.get(f"{_BASE}?page=1&page_size=20&status=active")
    assert not still_active.json()["items"]
