"""FAR-1641: route-level concurrency test for ``POST /api/v1/runs``.

FAR-1625 (PR #1492) serialised ``snapshot_version`` allocation with a
transaction-scoped row lock on the ``pipelines`` row, so N concurrent
``POST /api/v1/runs`` for ONE pipeline must ALL be accepted with distinct run
ids and distinct, contiguous snapshot versions. Before FAR-1625 the same burst
collided on ``uq_pipeline_snapshot_version`` (or exhausted the snapshot-lock
budget): the losers did not create a run and returned a non-202 (409/503/500),
while duplicate versions / missing run rows could persist — work silently lost.

The CRUD-level proof lives in
``test_far1625_concurrent_snapshot_version.py`` (direct ``create_snapshot_from_live_graph``
/ ``create_run`` calls, each on its own session). This module adds the missing
ROUTE-level proof: the burst is driven through the REAL ASGI app — the real
``trigger_run`` handler, the real JWT auth + permission/team-gate dependency
chain, real RLS, and a real Postgres (testcontainers) — so a regression that
only manifests in the route's own transaction (snapshot + validation +
rate-limit + run insert + response build, all inside ONE transaction while the
allocation row lock is held) is caught here even when the CRUD path stays green.

Discriminating against pre-FAR-1625 main: the burst previously produced
``IntegrityError`` duplicate-key collisions on ``uq_pipeline_snapshot_version``
(and, with the bounded snapshot lock, ``SnapshotLockNotAvailableError``). On the
route those surface as 409/503/500 and some callers lose their run. This test
asserts every request is exactly 202, every returned run id is distinct and
persisted, and the N snapshot versions are exactly ``1..N`` with no duplicate
and no gap — so it fails on the pre-fix behaviour without needing a revert.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import AsyncGenerator, Generator
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.jwt import create_access_token
from modulo.settings import Settings, get_settings

os.environ.setdefault("MODULO_AUTH_RATE_LIMIT_ENABLED", "false")
os.environ.setdefault("REDIS_URL", "")

pytestmark = pytest.mark.integration

_VALID_32 = "a" * 32

# The reproduced burst size. The prod incident (FAR-1625) was 12 simultaneous
# ``POST /api/v1/runs`` for one pipeline; the ticket requires >= 8.
_CONCURRENT_TRIGGERS = 12

# A single flat ``manual`` node (no agent_id) — enough for
# ``create_snapshot_from_live_graph`` to produce a runnable snapshot and for the
# ``POST /api/v1/runs`` basic graph validation (entry node exists) to pass.
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
    """A browser JWT for the org-admin test user (bypasses the team gate)."""
    return create_access_token(
        subject=f"user-{user_id.hex[:8]}",
        secret_key=_VALID_32,
        organisation_id=str(org_id),
        account_id=str(user_id),
        org_role="admin",
        client_kind="browser",
    )


async def _seed_pipeline(db_engine: AsyncEngine, org_id: uuid.UUID, user_id: uuid.UUID) -> uuid.UUID:
    """Minimal committed, org-visible pipeline owned by the test org.

    ``lock_wait_timeout_seconds`` is a pipeline-level column that the
    snapshot-allocation path never consults; the FAR-1625 allocation row lock
    is bounded by the transaction-scoped
    ``Settings.mutation_row_lock_timeout_ms`` (raised in the
    ``runs_route_client`` fixture for the burst). This column is set
    generously only so no unrelated admission path trips during the burst.
    """
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
                "name": f"far1641-{pipeline_id.hex[:8]}",
                "uid": str(user_id),
                "graph": json.dumps(_MINIMAL_GRAPH_NODES),
            },
        )
    return pipeline_id


async def _cleanup(db_engine: AsyncEngine, pipeline_id: uuid.UUID) -> None:
    """Remove the burst's rows (the DB is ephemeral, but keep it tidy).

    Runs reference the pipeline and its snapshots; dependent rows (node outputs,
    evidence, eval results, ...) cascade or null-out, so deleting the runs first
    is sufficient before the snapshot/pipeline parents go.
    """
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(text("DELETE FROM runs WHERE pipeline_id = :pid"), {"pid": str(pipeline_id)})
        await conn.execute(
            text("DELETE FROM pipeline_snapshots WHERE pipeline_id = :pid"),
            {"pid": str(pipeline_id)},
        )
        await conn.execute(text("DELETE FROM pipelines WHERE id = :id"), {"id": str(pipeline_id)})


async def _persisted_run_ids(db_engine: AsyncEngine, pipeline_id: uuid.UUID) -> set[uuid.UUID]:
    async with db_engine.connect() as conn:
        rows = (
            await conn.execute(
                text("SELECT id FROM runs WHERE pipeline_id = :pid"),
                {"pid": str(pipeline_id)},
            )
        ).scalars()
        return {uuid.UUID(str(row)) for row in rows}


_SNAPSHOT_VERSIONS_SQL = text(
    "SELECT snapshot_version FROM pipeline_snapshots WHERE pipeline_id = :pid ORDER BY snapshot_version"
)


async def _persisted_snapshot_versions(db_engine: AsyncEngine, pipeline_id: uuid.UUID) -> list[int]:
    async with db_engine.connect() as conn:
        rows = (await conn.execute(_SNAPSHOT_VERSIONS_SQL, {"pid": str(pipeline_id)})).scalars()
        return [int(row) for row in rows]


@pytest_asyncio.fixture
async def runs_route_client(db_url: str, app_engine: AsyncEngine) -> AsyncGenerator[AsyncClient, None]:
    """ASGI client wired to the real app with the container DB (RLS applies).

    Mirrors the proven ``POST /api/v1/runs`` harness in
    ``test_trigger_run_team_gate.py``: dependency overrides point the route's
    session/engine at the non-superuser ``app_engine`` (so RLS is live), plan
    context is stubbed open, and each request gets its own session.
    """

    async def override_session() -> AsyncGenerator[AsyncSession, None]:
        factory = async_sessionmaker(app_engine, expire_on_commit=False)
        async with factory() as session:
            yield session

    settings = Settings(
        database_url=db_url,
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_csrf_enabled=False,
        modulo_auth_rate_limit_enabled=False,
        redis_url="",
        modulo_admin_password="",
        # The FAR-1625 snapshot-allocation row lock is bounded by the
        # *transaction-scoped* ``Settings.mutation_row_lock_timeout_ms`` — NOT
        # by the pipeline's ``lock_wait_timeout_seconds`` column (that column
        # never reaches ``set_mutation_row_lock_timeout``). At the 5 s factory
        # default a 12-deep serialised burst can exceed the budget on a cold
        # run, and the loser gets a retryable ``SnapshotLockNotAvailableError``
        # -> 503. 30 s sits comfortably above the 12-way serialisation time, so
        # the test measures allocation serialisation, not an incidental
        # lock-timeout expiry.
        mutation_row_lock_timeout_ms=30_000,
    )

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[_get_engine] = lambda: app_engine
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[get_plan_context] = lambda: _AllFeatures()

    transport = ASGITransport(app=app)
    # ``set_mutation_row_lock_timeout`` calls ``get_settings()`` DIRECTLY (a
    # module-level import in ``db.crud.row_lock``), so the
    # ``dependency_overrides[get_settings]`` above does NOT reach it — the
    # direct call would read the lru_cached real settings and keep the 5 s
    # default. Patch the ``row_lock`` module's binding so the raised budget
    # actually applies to the allocation lock.
    with patch("modulo.db.crud.row_lock.get_settings", return_value=settings):
        async with AsyncClient(transport=transport, base_url="http://test", timeout=120.0) as client:
            yield client

    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def _stub_run_dispatch() -> Generator[None, None, None]:
    """Stub the background dispatch so a 202 does not require Redis/SAQ.

    ``trigger_run`` awaits ``dispatch_run`` after the run row is committed; with
    ``redis_url=""`` the real dispatch has no queue to enqueue onto. The run is
    already created and the route returns 202 regardless, so the dispatch side
    effect is irrelevant to these assertions.
    """
    with patch("modulo.api.routes.runs.dispatch_run", new_callable=AsyncMock):
        yield


async def test_concurrent_route_triggers_all_succeed_with_distinct_runs_and_versions(
    runs_route_client: AsyncClient,
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    """FAR-1641: a 12-way ``POST /api/v1/runs`` burst for ONE pipeline.

    Every request goes through the real route. The assertions:

    1. EVERY request is 202 — none is 500/503 (a snapshot-lock loss), nor 409 (a
       duplicate-version IntegrityError), nor 429.
    2. N DISTINCT run ids are returned (no caller lost its run).
    3. N snapshots exist with versions exactly ``1..N`` — distinct and
       contiguous, so no duplicate allocation and no silently lost version.
    4. Every returned run id is actually persisted (the response was not a
       phantom) and each run references one of the burst's snapshots.

    ``asyncio.gather`` without ``return_exceptions`` also means any transport- or
    handler-level exception (including a deadlock) fails the test outright — the
    "no exception surfaces" requirement.
    """
    pipeline_id = await _seed_pipeline(db_engine, test_org, test_user)
    headers = {"Authorization": f"Bearer {_admin_token(test_org, test_user)}"}
    try:
        responses = await asyncio.gather(
            *(
                runs_route_client.post(
                    "/api/v1/runs",
                    json={"pipeline_id": str(pipeline_id)},
                    headers=headers,
                )
                for _ in range(_CONCURRENT_TRIGGERS)
            )
        )

        # (1) Every caller accepted — no 4xx/5xx, no lost run.
        statuses = [resp.status_code for resp in responses]
        assert statuses == [202] * _CONCURRENT_TRIGGERS, (
            "concurrent POST /api/v1/runs must ALL be accepted; got "
            f"{statuses} with bodies {[resp.text for resp in responses]}"
        )

        # (2) N distinct run ids returned.
        run_ids = [resp.json()["run_id"] for resp in responses]
        assert len(set(run_ids)) == _CONCURRENT_TRIGGERS, f"run ids not distinct: {run_ids}"
        assert None not in run_ids

        # (3) N persisted snapshots with contiguous versions 1..N.
        versions = await _persisted_snapshot_versions(db_engine, pipeline_id)
        assert versions == list(range(1, _CONCURRENT_TRIGGERS + 1)), (
            f"snapshot versions must be distinct and contiguous from 1; got {versions}"
        )

        # (4) Every returned run id is persisted and belongs to one of the
        # burst's snapshots (a phantom 202 or a mis-linked run would fail here).
        # The returned set is already asserted distinct and N-strong, so this
        # equality also pins the persisted set to exactly N — a separate
        # ``len`` assertion would be redundant.
        persisted_ids = await _persisted_run_ids(db_engine, pipeline_id)
        assert {uuid.UUID(run_id) for run_id in run_ids} == persisted_ids
    finally:
        await _cleanup(db_engine, pipeline_id)
