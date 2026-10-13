"""GET /runs/{run_id}/logs — run-keyed, provenance-typed log projection (FAR-1690 A4).

Covers the pure projection (``core.run_log_correlation``) and the route wiring:
each entry carries its governed provenance class (FAR-1693): a retained
workspace log tail is ``observed``, a dispatched node's folded-back telemetry is
``reported`` (with its ``witnessed_via`` stamp), a Modulo-native audit event is
``attested``. The route is org-scoped: a foreign-org run id answers 404 before
any source is read.
"""

import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.dependencies import _get_engine, _get_session_factory, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_tenant_user, get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal, TenantPrincipal
from modulo.core.provenance import PROVENANCE_ATTESTED, PROVENANCE_OBSERVED, PROVENANCE_REPORTED
from modulo.core.run_log_correlation import (
    LOG_TAIL_EXCERPT_CHARS,
    SOURCE_NODE_TELEMETRY,
    SOURCE_STRUCTURED_EVENT,
    SOURCE_WORKSPACE_LOG_TAIL,
    correlate_run_logs,
)
from modulo.settings import Settings, get_settings

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_RUN_ID = uuid.UUID("00000000-0000-0000-0000-0000000000aa")
_NODE_ID = "node-1"


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",  # nosec - test-only value
    )


@pytest.fixture
def mock_session() -> AsyncMock:
    session = AsyncMock(spec=AsyncSession)
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


@pytest.fixture
def client(mock_session: AsyncMock) -> Generator[TestClient, None, None]:
    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[_get_session_factory] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="testuser",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
    )
    app.dependency_overrides[get_current_tenant_user] = lambda: TenantPrincipal(
        username="testuser",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
    )
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    yield TestClient(app)

    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Pure projection
# ---------------------------------------------------------------------------


def test_marker_tail_is_observed_workspace_log_tail() -> None:
    markers = {
        f"run:{_RUN_ID}:node:{_NODE_ID}:1": {
            "node_id": _NODE_ID,
            "attempt_key": f"run:{_RUN_ID}:node:{_NODE_ID}:1",
            "deadline_triage": True,
            "sandbox_log_tail": "tail line",
        }
    }
    entries = correlate_run_logs(markers=markers)
    assert len(entries) == 1
    assert entries[0].source == SOURCE_WORKSPACE_LOG_TAIL
    assert entries[0].provenance == PROVENANCE_OBSERVED
    assert entries[0].excerpt == "tail line"
    assert entries[0].node_id == _NODE_ID


def test_marker_without_tail_is_skipped() -> None:
    markers = {f"run:{_RUN_ID}:node:{_NODE_ID}:1": {"node_id": _NODE_ID, "delivery_done": True}}
    entries = correlate_run_logs(markers=markers)
    assert not entries


def test_dispatch_telemetry_is_reported_with_witnessed_via() -> None:
    telemetry = {
        _NODE_ID: {
            "status": "success",
            "summary": "substrate job done",
            "provenance": PROVENANCE_REPORTED,
            "witnessed_via": "github-conn-7",
            "provider": "e2b",
            "sandbox_id": "sbx-9",
        }
    }
    entries = correlate_run_logs(telemetry=telemetry)
    assert len(entries) == 1
    entry = entries[0]
    assert entry.source == SOURCE_NODE_TELEMETRY
    assert entry.provenance == PROVENANCE_REPORTED
    assert entry.witnessed_via == "github-conn-7"
    assert entry.provider == "e2b"
    assert entry.provider_ref == "sbx-9"


def test_sandbox_telemetry_tail_is_observed() -> None:
    telemetry = {_NODE_ID: {"status": "success", "sandbox_log_tail": "live tail", "provider": "e2b"}}
    entries = correlate_run_logs(telemetry=telemetry)
    assert len(entries) == 1
    assert entries[0].provenance == PROVENANCE_OBSERVED
    assert entries[0].source == SOURCE_WORKSPACE_LOG_TAIL
    assert entries[0].excerpt == "live tail"


def test_structured_event_is_attested() -> None:
    when = datetime(2026, 1, 1, tzinfo=UTC)
    events = [{"event_type": "run.started", "detail": "run started", "recorded_at": when}]
    entries = correlate_run_logs(events=events)
    assert len(entries) == 1
    assert entries[0].source == SOURCE_STRUCTURED_EVENT
    assert entries[0].provenance == PROVENANCE_ATTESTED
    assert entries[0].recorded_at == when


def test_duplicate_tail_across_sources_collapses() -> None:
    tail = "shared tail"
    markers = {f"run:{_RUN_ID}:node:{_NODE_ID}:1": {"node_id": _NODE_ID, "sandbox_log_tail": tail}}
    telemetry = {_NODE_ID: {"status": "success", "sandbox_log_tail": tail}}
    entries = correlate_run_logs(markers=markers, telemetry=telemetry)
    assert len(entries) == 1


def test_timestamped_entries_are_ordered_first() -> None:
    events = [
        {"event_type": "run.started", "detail": "first", "recorded_at": datetime(2026, 1, 1, tzinfo=UTC)},
        {"event_type": "run.done", "detail": "second", "recorded_at": datetime(2026, 1, 2, tzinfo=UTC)},
    ]
    markers = {f"run:{_RUN_ID}:node:{_NODE_ID}:1": {"node_id": _NODE_ID, "sandbox_log_tail": "tail"}}
    entries = correlate_run_logs(events=events, markers=markers)
    excerpts = [entry.excerpt for entry in entries]
    assert excerpts == ["first", "second", "tail"]


def test_tail_excerpt_is_bounded() -> None:
    long_tail = "x" * (LOG_TAIL_EXCERPT_CHARS + 500)
    markers = {f"run:{_RUN_ID}:node:{_NODE_ID}:1": {"node_id": _NODE_ID, "sandbox_log_tail": long_tail}}
    entries = correlate_run_logs(markers=markers)
    assert len(entries[0].excerpt) == LOG_TAIL_EXCERPT_CHARS


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------


def _audit_result(events: list[Any]) -> MagicMock:
    result = MagicMock()
    result.scalars.return_value.all.return_value = events
    return result


def test_logs_404_on_foreign_org_run(client: TestClient, mock_session: AsyncMock) -> None:
    """A run id belonging to another organisation answers 404, not 200 []."""
    mock_session.execute = AsyncMock(return_value=_audit_result([]))
    with (
        patch("modulo.api.routes.runs.get_run", new_callable=AsyncMock) as mock_get_run,
        patch("modulo.api.routes.runs.read_run_markers", new_callable=AsyncMock) as mock_markers,
        patch("modulo.api.routes.runs.read_run_telemetry", new_callable=AsyncMock) as mock_telemetry,
    ):
        mock_get_run.return_value = None
        response = client.get(f"/api/v1/runs/{_RUN_ID}/logs")

    assert response.status_code == 404
    body: dict[str, Any] = response.json()
    assert body["detail"] == "Run not found"
    assert mock_get_run.call_args.kwargs.get("organisation_id") == _ORG_ID
    # Without the run, no source read is attempted.
    assert not mock_markers.await_count
    assert not mock_telemetry.await_count


def test_logs_returns_provenance_typed_entries(client: TestClient, mock_session: AsyncMock) -> None:
    """A same-org run returns its correlated, provenance-typed entries."""
    event = MagicMock()
    event.event_type = "workspace_started"
    event.payload_json = {"detail": "workspace prepared"}
    event.created_at = datetime(2026, 1, 1, tzinfo=UTC)
    mock_session.execute = AsyncMock(return_value=_audit_result([event]))

    markers = {f"run:{_RUN_ID}:node:{_NODE_ID}:1": {"node_id": _NODE_ID, "sandbox_log_tail": "tail line"}}
    telemetry = {_NODE_ID: {"status": "success", "sandbox_log_tail": "tail line"}}
    with (
        patch("modulo.api.routes.runs.get_run", new_callable=AsyncMock) as mock_get_run,
        patch("modulo.api.routes.runs.read_run_markers", new_callable=AsyncMock) as mock_markers,
        patch("modulo.api.routes.runs.read_run_telemetry", new_callable=AsyncMock) as mock_telemetry,
    ):
        mock_get_run.return_value = MagicMock()
        mock_markers.return_value = markers
        mock_telemetry.return_value = telemetry
        response = client.get(f"/api/v1/runs/{_RUN_ID}/logs")

    assert response.status_code == 200
    body = response.json()
    assert body["run_id"] == str(_RUN_ID)
    entries = body["entries"]
    assert entries
    classes = {entry["provenance"] for entry in entries}
    assert classes == {PROVENANCE_OBSERVED, PROVENANCE_ATTESTED}


def test_logs_audit_query_carries_org_predicate(client: TestClient, mock_session: AsyncMock) -> None:
    """The audit-event select is pinned to the caller's organisation_id."""
    mock_session.execute = AsyncMock(return_value=_audit_result([]))
    with (
        patch("modulo.api.routes.runs.get_run", new_callable=AsyncMock) as mock_get_run,
        patch("modulo.api.routes.runs.read_run_markers", new_callable=AsyncMock) as mock_markers,
        patch("modulo.api.routes.runs.read_run_telemetry", new_callable=AsyncMock) as mock_telemetry,
    ):
        mock_get_run.return_value = MagicMock()
        mock_markers.return_value = {}
        mock_telemetry.return_value = {}
        response = client.get(f"/api/v1/runs/{_RUN_ID}/logs")

    assert response.status_code == 200
    executed_stmts = [call.args[0] for call in mock_session.execute.await_args_list]
    audit_stmts = [stmt for stmt in executed_stmts if "audit_events" in str(stmt.compile())]
    assert audit_stmts
    sql = str(audit_stmts[0].compile())
    assert "audit_events.organisation_id" in sql
