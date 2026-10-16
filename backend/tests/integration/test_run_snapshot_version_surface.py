"""FAR-1838: ``snapshot_version`` end-to-end on the run list / detail / trigger routes.

The run payload carried ``snapshot_id`` but not the version number, so no run
surface could render a ``v{n}`` chip without a second GET against the snapshot.
This module is the REAL-stack proof (real ASGI app, real Postgres via
testcontainers, real RLS): the unit-level proof lives in
``tests/unit/api/test_run_snapshot_version_surface.py``.

Pinned here:

* ``POST /api/v1/runs`` (manual trigger) and ``POST /api/v1/runs/{id}/rerun``
  surface the version of the snapshot the response already holds — the rerun
  REUSES the source snapshot it loaded for validation (exactly ONE full-row
  ``pipeline_snapshots`` SELECT during a rerun; a response-side re-fetch would
  be two);
* ``GET /api/v1/runs`` surfaces every item's version, loaded for the whole
  page in EXACTLY ONE snapshot statement (an N+1 regression makes that count
  grow with the page);
* ``GET /api/v1/runs/{run_id}`` surfaces the version loaded in the run-load
  transaction (``selectinload`` + ``load_only(snapshot_version)`` — never the
  snapshot's megabyte-scale ``graph_json``).
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import AsyncGenerator, Generator
from typing import Any, Self
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from modulo.api.dependencies import _get_engine, _get_session_factory, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.jwt import create_access_token
from modulo.settings import Settings, get_settings

os.environ.setdefault("MODULO_AUTH_RATE_LIMIT_ENABLED", "false")
os.environ.setdefault("REDIS_URL", "")

pytestmark = pytest.mark.integration

_VALID_32 = "a" * 32

# A single flat ``manual`` node (no agent_id) — enough for
# ``create_snapshot_from_live_graph`` and the POST /runs graph validation.
_MINIMAL_GRAPH_NODES = [
    {
        "id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "node_type": "manual",
        "position": {"x": 0, "y": 0},
    }
]


class _AllFeatures:
    """Plan-context stub that reports every feature as enabled."""

    def feature_enabled(self, name: str) -> bool:
        return True

    def list_enabled_features(self) -> list[str]:
        return []

    def tier(self) -> str:
        return "enterprise"

    def has_license_key(self) -> bool:
        return True


def _admin_token(org_id: uuid.UUID, user_id: uuid.UUID) -> str:
    return create_access_token(
        subject=f"user-{user_id.hex[:8]}",
        secret_key=_VALID_32,
        organisation_id=str(org_id),
        account_id=str(user_id),
        org_role="admin",
        client_kind="browser",
    )


async def _seed_pipeline(db_engine: AsyncEngine, org_id: uuid.UUID, user_id: uuid.UUID) -> uuid.UUID:
    """Minimal committed, org-visible pipeline with a runnable one-node graph."""
    pipeline_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO pipelines (id, organisation_id, name, account_id, "
                "max_concurrent_runs, lock_wait_timeout_seconds, node_timeout_seconds, "
                "run_context_defaults, graph_nodes_json, default_autonomy_level, visibility) "
                "VALUES (:id, :oid, :name, :uid, 50, 120, 300, "
                "'{}'::json, (:graph)::json, 'manual_approval', 'org')"
            ),
            {
                "id": str(pipeline_id),
                "oid": str(org_id),
                "name": f"far1838-{pipeline_id.hex[:8]}",
                "uid": str(user_id),
                "graph": json.dumps(_MINIMAL_GRAPH_NODES),
            },
        )
    return pipeline_id


async def _cleanup(db_engine: AsyncEngine, pipeline_id: uuid.UUID) -> None:
    """Remove the test's rows (the DB is ephemeral, but keep it tidy)."""
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(text("DELETE FROM runs WHERE pipeline_id = :pid"), {"pid": str(pipeline_id)})
        await conn.execute(text("DELETE FROM pipeline_snapshots WHERE pipeline_id = :pid"), {"pid": str(pipeline_id)})
        await conn.execute(text("DELETE FROM pipelines WHERE id = :id"), {"id": str(pipeline_id)})


async def _snapshot_version_by_id(db_engine: AsyncEngine, pipeline_id: uuid.UUID) -> dict[uuid.UUID, int]:
    async with db_engine.connect() as conn:
        rows = (
            await conn.execute(
                text("SELECT id, snapshot_version FROM pipeline_snapshots WHERE pipeline_id = :pid"),
                {"pid": str(pipeline_id)},
            )
        ).all()
    return {uuid.UUID(str(row[0])): int(row[1]) for row in rows}


async def _mark_run_terminal(db_engine: AsyncEngine, run_id: uuid.UUID) -> None:
    """Force a pending (dispatch-stubbed) run terminal so it can be re-run."""
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text("UPDATE runs SET status = 'complete', completed_at = now() WHERE id = :id"),
            {"id": str(run_id)},
        )


class _SnapshotStatementCounter:
    """Count SQL statements touching ``pipeline_snapshots`` in a window.

    ``full_row_only`` keeps only the FULL-ROW snapshot load — the shape
    ``_create_rerun_run`` issues to re-validate the pinned graph — identified
    by ``connector_bindings_json``, a column no narrow projection selects.
    """

    def __init__(self, engine: AsyncEngine, *, full_row_only: bool = False) -> None:
        self.statements: list[str] = []
        self._engine = engine
        self._full_row_only = full_row_only

    def _on_execute(
        self,
        conn: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        if "pipeline_snapshots" not in statement:
            return
        if self._full_row_only and "connector_bindings_json" not in statement:
            return
        self.statements.append(statement)

    def __enter__(self) -> Self:
        event.listen(self._engine.sync_engine, "before_cursor_execute", self._on_execute)
        return self

    def __exit__(self, *exc: object) -> bool:
        event.remove(self._engine.sync_engine, "before_cursor_execute", self._on_execute)
        return False


@pytest_asyncio.fixture
async def runs_client(db_url: str, app_engine: AsyncEngine) -> AsyncGenerator[AsyncClient, None]:
    """ASGI client wired to the real app with the container DB (RLS applies)."""

    async def override_session() -> AsyncGenerator[AsyncSession, None]:
        factory = async_sessionmaker(app_engine, expire_on_commit=False)
        async with factory() as session:
            yield session

    # The detail/list routes resolve their own factory via
    # ``_get_session_factory`` (not ``get_db_session``), and the process-global
    # factory singleton in ``api.dependencies`` would otherwise bind them to
    # whichever engine was first seen in this pytest process. Override it so
    # every route in this module runs against ``app_engine`` (RLS live).
    app.dependency_overrides[_get_session_factory] = lambda: async_sessionmaker(app_engine, expire_on_commit=False)

    settings = Settings(
        database_url=db_url,
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_csrf_enabled=False,
        modulo_auth_rate_limit_enabled=False,
        redis_url="",
        modulo_admin_password="",
    )

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[_get_engine] = lambda: app_engine
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[get_plan_context] = lambda: _AllFeatures()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", timeout=120.0) as client:
        yield client

    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def _stub_run_dispatch() -> Generator[None, None, None]:
    """Stub the background dispatch so a 202 does not require Redis/SAQ."""
    with patch("modulo.api.routes.runs.dispatch_run", new_callable=AsyncMock):
        yield


async def _trigger_run(client: AsyncClient, headers: dict[str, str], pipeline_id: uuid.UUID) -> dict[str, Any]:
    resp = await client.post("/api/v1/runs", json={"pipeline_id": str(pipeline_id)}, headers=headers)
    assert resp.status_code == 202, resp.text
    return resp.json()


async def test_trigger_list_and_detail_surface_snapshot_version(
    runs_client: AsyncClient,
    db_engine: AsyncEngine,
    app_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    """Every run surface carries the version of the snapshot it executes."""
    pipeline_id = await _seed_pipeline(db_engine, test_org, test_user)
    headers = {"Authorization": f"Bearer {_admin_token(test_org, test_user)}"}
    try:
        first = await _trigger_run(runs_client, headers, pipeline_id)
        second = await _trigger_run(runs_client, headers, pipeline_id)

        # The trigger response itself carries the version of the snapshot the
        # route just created and attached (versions allocate 1, 2 per pipeline).
        assert first["snapshot_id"] is not None
        assert second["snapshot_id"] is not None
        assert first["snapshot_id"] != second["snapshot_id"]
        assert first["snapshot_version"] == 1
        assert second["snapshot_version"] == 2

        persisted = await _snapshot_version_by_id(db_engine, pipeline_id)
        assert persisted == {
            uuid.UUID(str(first["snapshot_id"])): 1,
            uuid.UUID(str(second["snapshot_id"])): 2,
        }

        # LIST: both items carry their own snapshot's version.
        list_resp = await runs_client.get("/api/v1/runs", params={"pipeline_id": str(pipeline_id)}, headers=headers)
        assert list_resp.status_code == 200, list_resp.text
        items = list_resp.json()["items"]
        by_snapshot = {uuid.UUID(str(item["snapshot_id"])): item["snapshot_version"] for item in items}
        assert by_snapshot == persisted

        # DETAIL: the run-load transaction's eager version read surfaces too.
        detail_resp = await runs_client.get(f"/api/v1/runs/{first['run_id']}", headers=headers)
        assert detail_resp.status_code == 200, detail_resp.text
        detail = detail_resp.json()
        assert detail["snapshot_id"] == first["snapshot_id"]
        assert detail["snapshot_version"] == 1
    finally:
        await _cleanup(db_engine, pipeline_id)


async def test_list_loads_snapshot_versions_in_one_statement(
    runs_client: AsyncClient,
    db_engine: AsyncEngine,
    app_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    """A multi-run page costs ONE snapshot statement — not one per row (no N+1)."""
    pipeline_id = await _seed_pipeline(db_engine, test_org, test_user)
    headers = {"Authorization": f"Bearer {_admin_token(test_org, test_user)}"}
    try:
        for _ in range(3):
            await _trigger_run(runs_client, headers, pipeline_id)
        persisted = await _snapshot_version_by_id(db_engine, pipeline_id)
        assert len(persisted) == 3

        with _SnapshotStatementCounter(app_engine) as counter:
            list_resp = await runs_client.get("/api/v1/runs", params={"pipeline_id": str(pipeline_id)}, headers=headers)

        assert list_resp.status_code == 200, list_resp.text
        items = list_resp.json()["items"]
        assert len(items) == 3
        for item in items:
            assert item["snapshot_version"] == persisted[uuid.UUID(str(item["snapshot_id"]))]

        assert len(counter.statements) == 1, (
            "the runs list must resolve every item's snapshot_version with ONE "
            f"batched statement; got {len(counter.statements)}: {counter.statements}"
        )
    finally:
        await _cleanup(db_engine, pipeline_id)


async def test_rerun_reuses_the_source_snapshot_already_loaded(
    runs_client: AsyncClient,
    db_engine: AsyncEngine,
    app_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    """A rerun response surfaces the version WITHOUT re-fetching the snapshot.

    ``_create_rerun_run`` already loads the source snapshot to re-validate the
    pinned graph — the response must reuse that object (the attach pattern),
    so exactly ONE FULL-ROW snapshot SELECT happens per rerun request. (The
    run-start path also issues narrow column projections — the dispatch-node
    graph read and the policy-gate pin read — which are excluded here; they
    pre-date FAR-1838 and are not response-side fetches.)
    """
    pipeline_id = await _seed_pipeline(db_engine, test_org, test_user)
    headers = {"Authorization": f"Bearer {_admin_token(test_org, test_user)}"}
    try:
        source = await _trigger_run(runs_client, headers, pipeline_id)
        await _mark_run_terminal(db_engine, uuid.UUID(str(source["run_id"])))

        with _SnapshotStatementCounter(app_engine, full_row_only=True) as counter:
            rerun_resp = await runs_client.post(f"/api/v1/runs/{source['run_id']}/rerun", headers=headers)

        assert rerun_resp.status_code == 202, rerun_resp.text
        rerun = rerun_resp.json()
        # A rerun is pinned to the SOURCE snapshot — same id, same version.
        assert rerun["snapshot_id"] == source["snapshot_id"]
        assert source["snapshot_version"] == 1
        assert rerun["snapshot_version"] == source["snapshot_version"]
        assert len(counter.statements) == 1, (
            "the rerun must reuse the snapshot it already loaded; a second "
            f"full-row fetch for the response would show up here: {counter.statements}"
        )
    finally:
        await _cleanup(db_engine, pipeline_id)
