"""Unit tests for the artifact listing endpoint (FAR-582).

``GET /api/v1/runs/{run_id}/nodes/{node_id}/artifacts`` returns a flat list
of artifact pointers across all attempts for a given (run, node) pair.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError

from modulo.api.dependencies import get_db_session
from modulo.api.main import app
from modulo.auth.dependencies import (
    get_current_tenant_user,
    get_current_tenant_user_or_api_key,
    get_current_user,
)
from modulo.auth.jwt import TenantPrincipal
from modulo.db.models.run_node_outputs import RunNodeOutput
from modulo.settings import Settings, get_settings

_RUN_ID = uuid.uuid4()
_NODE_ID = "sandbox_1"
_ORG_ID = uuid.uuid4()
_USER_ID = uuid.uuid4()

_SETTINGS = Settings(
    database_url="postgresql+asyncpg://localhost/test",
    secret_key="a" * 32,
    fernet_key="a" * 32,
    modulo_admin_password="testpass",
    redis_url="redis://localhost:6379/0",
)

_PRINCIPAL = TenantPrincipal(
    username="ci@example.com",
    organisation_id=_ORG_ID,
    account_id=_USER_ID,
    org_role="operator",
)


class _BeginRaiser:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def __aenter__(self) -> None:
        raise self._exc

    async def __aexit__(self, *args: object) -> None:
        return None


def _make_session(*, begin_exc: Exception | None = None) -> AsyncMock:
    """Build a mock session with proper begin() async context manager."""
    session = AsyncMock()
    if begin_exc is not None:
        session.begin = MagicMock(return_value=_BeginRaiser(begin_exc))
    else:
        begin_cm = AsyncMock()
        begin_cm.__aenter__ = AsyncMock(return_value=None)
        begin_cm.__aexit__ = AsyncMock(return_value=False)
        session.begin = MagicMock(return_value=begin_cm)

    # Default result: empty scalars
    exec_result = MagicMock()
    exec_result.scalar_one_or_none.return_value = None
    exec_result.scalar.return_value = 0
    exec_result.scalars.return_value.all.return_value = []
    exec_result.scalars.return_value.first.return_value = None
    exec_result.all.return_value = []
    exec_result.fetchone.return_value = None
    session.execute = AsyncMock(return_value=exec_result)
    session.refresh = AsyncMock(return_value=None)
    session.add = MagicMock(return_value=None)
    return session


def _queue_execute(session: AsyncMock, results: list[MagicMock]) -> None:
    """Route session.execute through a result queue, handling auth noise."""
    authz_result = MagicMock()
    authz_result.scalar_one_or_none.return_value = None

    async def _execute(stmt: object, *_args: object, **_kwargs: object) -> MagicMock:
        if "authz_enforce" in str(stmt):
            return authz_result
        if not results:
            raise AssertionError(
                "Unexpected session.execute(): the result queue is exhausted",
            )
        return results.pop(0)

    session.execute = AsyncMock(side_effect=_execute)


def _make_artifact_row(
    *,
    attempt_key: str = "run:abc:node:sandbox_1:0",
    artifacts: list[dict] | None = None,
) -> MagicMock:
    """Create a mock RunNodeOutput row."""
    row = MagicMock(spec=RunNodeOutput)
    row.attempt_key = attempt_key
    row.artifacts_json = artifacts
    return row


def _result(rows: list | None = None) -> MagicMock:
    """Build a result mock with .scalars().all()."""
    r = MagicMock()
    r.scalar_one_or_none.return_value = None
    r.scalar.return_value = 0
    r.scalars.return_value.all.return_value = rows if rows is not None else []
    r.all.return_value = rows if rows is not None else []
    return r


def _override_deps(session: AsyncMock) -> None:
    """Wire up FastAPI dependency overrides for a test."""
    app.dependency_overrides[get_db_session] = lambda: session
    app.dependency_overrides[get_settings] = lambda: _SETTINGS

    async def _fake_user():
        return _PRINCIPAL

    app.dependency_overrides[get_current_user] = _fake_user
    app.dependency_overrides[get_current_tenant_user] = _fake_user
    app.dependency_overrides[get_current_tenant_user_or_api_key] = _fake_user


def _clear_deps() -> None:
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    _clear_deps()


# ── Happy path ────────────────────────────────────────────────────────


class TestArtifactListingHappy:
    """Happy-path tests for the listing endpoint."""

    def test_list_with_multiple_attempts_and_streams(self) -> None:
        """Two attempt rows, each with stdout+stderr -> 4 artifacts."""
        row_a = _make_artifact_row(
            attempt_key="run:abc:node:sandbox_1:1",
            artifacts=[
                {
                    "stream": "stdout",
                    "rel_path": "s1/stdout.zst",
                    "size_bytes": 1024,
                    "sha256": "aa",
                    "compression": "zstd",
                },
                {
                    "stream": "stderr",
                    "rel_path": "s1/stderr.zst",
                    "size_bytes": 512,
                    "sha256": "bb",
                    "compression": "zstd",
                },
            ],
        )
        row_b = _make_artifact_row(
            attempt_key="run:abc:node:sandbox_1:0",
            artifacts=[
                {
                    "stream": "stdout",
                    "rel_path": "s0/stdout.zst",
                    "size_bytes": 2048,
                    "sha256": "cc",
                    "compression": "zstd",
                },
                {
                    "stream": "stderr",
                    "rel_path": "s0/stderr.zst",
                    "size_bytes": 256,
                    "sha256": "dd",
                    "compression": "none",
                },
            ],
        )

        session = _make_session()
        _queue_execute(session, [_result(rows=[row_a, row_b])])
        _override_deps(session)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get(
            f"/api/v1/runs/{_RUN_ID}/nodes/{_NODE_ID}/artifacts",
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["run_id"] == str(_RUN_ID)
        assert body["node_id"] == _NODE_ID
        assert len(body["artifacts"]) == 4

        # Newest first: attempt 1 before attempt 0
        assert body["artifacts"][0]["attempt_key"] == "run:abc:node:sandbox_1:1"
        assert body["artifacts"][0]["stream"] == "stdout"
        assert body["artifacts"][0]["size_bytes"] == 1024
        assert body["artifacts"][0]["compression"] == "zstd"

        # No rel_path leaked
        for art in body["artifacts"]:
            assert "rel_path" not in art

    def test_list_empty_when_no_artifacts(self) -> None:
        """Node has output rows but no artifacts -> empty list."""
        row = _make_artifact_row(
            attempt_key="run:abc:node:sandbox_1:0",
            artifacts=None,
        )
        session = _make_session()
        _queue_execute(session, [_result(rows=[row])])
        _override_deps(session)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get(
            f"/api/v1/runs/{_RUN_ID}/nodes/{_NODE_ID}/artifacts",
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["artifacts"] == []

    def test_list_skips_empty_artifacts_json(self) -> None:
        """Row with artifacts_json=None is silently skipped."""
        row_none = _make_artifact_row(
            attempt_key="run:abc:node:sandbox_1:0",
            artifacts=None,
        )
        row_has = _make_artifact_row(
            attempt_key="run:abc:node:sandbox_1:1",
            artifacts=[
                {
                    "stream": "stdout",
                    "rel_path": "x",
                    "size_bytes": 100,
                    "sha256": "ee",
                    "compression": "none",
                },
            ],
        )
        session = _make_session()
        _queue_execute(session, [_result(rows=[row_none, row_has])])
        _override_deps(session)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get(
            f"/api/v1/runs/{_RUN_ID}/nodes/{_NODE_ID}/artifacts",
        )

        assert resp.status_code == 200
        assert len(resp.json()["artifacts"]) == 1


# ── Error paths ───────────────────────────────────────────────────────


class TestArtifactListingErrors:
    """Error-path tests for the listing endpoint."""

    def test_404_when_node_not_found(self) -> None:
        """No rows for (run, node) -> 404."""
        session = _make_session()
        _queue_execute(session, [_result(rows=[])])
        _override_deps(session)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get(
            f"/api/v1/runs/{_RUN_ID}/nodes/{_NODE_ID}/artifacts",
        )

        assert resp.status_code == 404

    def test_501_on_programming_error(self) -> None:
        """ProgrammingError -> 501."""
        session = _make_session(
            begin_exc=ProgrammingError("s", {}, Exception()),
        )
        _override_deps(session)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get(
            f"/api/v1/runs/{_RUN_ID}/nodes/{_NODE_ID}/artifacts",
        )

        assert resp.status_code == 501

    def test_503_on_sqlalchemy_error(self) -> None:
        """SQLAlchemyError -> 503."""
        session = _make_session(
            begin_exc=SQLAlchemyError("boom"),
        )
        _override_deps(session)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get(
            f"/api/v1/runs/{_RUN_ID}/nodes/{_NODE_ID}/artifacts",
        )

        assert resp.status_code == 503
