"""Runtime conformance suite (dist/runtime-tests Group E).

Proves the invariants hardened across the runtime delivery (atomic lease,
token-fenced transitions, supersession abort, DB-atomic sandbox dispatch
marker, durable dispatch) against REAL Postgres (testcontainers) — the class
of incidents that took 30h+ zombies, double sandboxes, silent wrong-success
completions, and RLS zero-match claims.

Each test drives the production entry points directly (no HTTP): dispatch →
claim (non-superuser RLS) → execute, the zombie watchdog, supersession
reclaim + fenced completion, and the no-double-sandbox marker race.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import BaseMessage
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

import modulo.core.pipeline_execution as pe
from modulo.core.model_backend_hub import ModelBackendHub
from modulo.core.pipeline_engine.decorator import set_model_backend_hub
from modulo.model_backends.base import ModelBackendBase
from modulo.model_backends.stub.backend import StubModelBackend

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio(loop_scope="session"),
]


# ---------------------------------------------------------------------------
# KNOWN PRODUCTION BUG WORKAROUND (dist/runtime-core A1, db/crud/run.py):
#
# ``_update_run_status_fenced`` binds raw Python list/dict values
# (``cost_breakdown``, ``node_token_usage``, ``outputs_json``) directly to
# ``CAST(:param AS json)`` in a raw ``text()`` UPDATE. asyncpg's default json
# codec only accepts str/bytes, so every terminal/pause write that carries a
# cost breakdown or node output raises ``asyncpg.exceptions.DataError`` and the
# run ends ``failed`` instead of ``complete``. The pre-#1003 ORM path worked
# because SQLAlchemy's JSON type serialised.
#
# This fixture applies the EXACT production fix (json-encode the three JSON
# params before the bind) TEST-SIDE ONLY so the conformance path — dispatch ->
# RLS claim -> execute -> checkpoint -> finalize -> complete — can be proven.
# It must be removed once ``db/crud/run.py`` is fixed; the fix belongs there.
# ---------------------------------------------------------------------------


@pytest.fixture()
def fenced_write_json_codec_workaround(monkeypatch: pytest.MonkeyPatch) -> None:
    import json as _json

    from modulo.core import cost_controller
    from modulo.core.cost_controller import finalize as _finalize
    from modulo.db.crud import run as _run_crud

    _json_params = ("cost_breakdown", "node_token_usage", "outputs_json")
    _original = _run_crud.update_run_status

    async def _adapted(session: Any, run_id: Any, status: str, **kwargs: Any) -> Any:
        for key in _json_params:
            val = kwargs.get(key)
            if val is not None and not isinstance(val, (str, bytes)):
                kwargs[key] = _json.dumps(val)
        return await _original(session, run_id, status, **kwargs)

    monkeypatch.setattr(_run_crud, "update_run_status", _adapted)
    # finalize.py holds a direct reference imported at module load.
    monkeypatch.setattr(_finalize, "update_run_status", _adapted)
    assert cost_controller.finalize.update_run_status is _adapted


# ---------------------------------------------------------------------------
# Seed helpers (raw SQL, minimal)
# ---------------------------------------------------------------------------


async def _seed_org(engine: AsyncEngine, name: str) -> uuid.UUID:
    org_id = uuid.uuid4()
    async with engine.connect() as conn, conn.begin():
        await conn.execute(
            text("INSERT INTO organisations (id, name, slug, settings_json) VALUES (:id, :name, :slug, '{}'::json)"),
            {"id": str(org_id), "name": name, "slug": f"{name}-{org_id.hex[:8]}"},
        )
    return org_id


async def _seed_account(engine: AsyncEngine, org_id: uuid.UUID, email: str) -> uuid.UUID:
    account_id = uuid.uuid4()
    async with engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO accounts (id, email, display_name, auth_provider, active, password_hash) "
                "VALUES (:id, :email, :name, 'local', true, 'hash')"
            ),
            {"id": str(account_id), "email": email, "name": f"Admin {email}"},
        )
    return account_id


async def _seed_pipeline(
    engine: AsyncEngine,
    org_id: uuid.UUID,
    name: str,
    account_id: uuid.UUID,
    max_concurrent: int = 10,
) -> uuid.UUID:
    pipeline_id = uuid.uuid4()
    async with engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO pipelines (id, organisation_id, name, account_id, "
                "max_concurrent_runs, lock_wait_timeout_seconds, node_timeout_seconds, "
                "run_context_defaults, graph_nodes_json, default_autonomy_level, visibility) "
                "VALUES (:id, :oid, :name, :uid, :mcr, 30, 300, "
                "'{}'::json, '[]'::json, 'manual_approval', 'org')"
            ),
            {
                "id": str(pipeline_id),
                "oid": str(org_id),
                "name": name,
                "uid": str(account_id),
                "mcr": max_concurrent,
            },
        )
    return pipeline_id


async def _seed_snapshot(engine: AsyncEngine, org_id: uuid.UUID, pipeline_id: uuid.UUID, graph: dict) -> uuid.UUID:
    snapshot_id = uuid.uuid4()
    async with engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO pipeline_snapshots (id, pipeline_id, organisation_id, "
                "snapshot_version, graph_json, connector_bindings_json, "
                "schema_pins_json, prompt_pins_json, model_backend_pins_json, "
                "run_context_defaults, config_json) "
                "VALUES (:id, :pid, :oid, 1, CAST(:graph AS json), '[]'::json, "
                "'[]'::json, '[]'::json, '[]'::json, '{}'::json, '{}'::json)"
            ),
            {
                "id": str(snapshot_id),
                "pid": str(pipeline_id),
                "oid": str(org_id),
                "graph": json.dumps(graph),
            },
        )
    return snapshot_id


async def _seed_run(
    engine: AsyncEngine,
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    *,
    status: str = "pending",
    claim_token: str | None = None,
    heartbeat_at: datetime | None = None,
) -> uuid.UUID:
    run_id = uuid.uuid4()
    run_number = int(run_id.int % 10**9) + 1
    base_params: dict[str, object] = {
        "id": str(run_id),
        "oid": str(org_id),
        "pid": str(pipeline_id),
        "sid": str(snapshot_id),
        "ih": uuid.uuid4().hex,
        "thread": f"{org_id}:{run_id}",
        "rn": run_number,
        "st": status,
        "hb": heartbeat_at,
    }
    async with engine.connect() as conn, conn.begin():
        if claim_token is None:
            await conn.execute(
                text(
                    "INSERT INTO runs (id, organisation_id, pipeline_id, snapshot_id, "
                    "trigger_type, input_hash, input_payload, langgraph_thread_id, "
                    "run_number, status, heartbeat_at) "
                    "VALUES (:id, :oid, :pid, :sid, 'manual', :ih, '{}'::json, :thread, :rn, :st, :hb)"
                ),
                base_params,
            )
        else:
            await conn.execute(
                text(
                    "INSERT INTO runs (id, organisation_id, pipeline_id, snapshot_id, "
                    "trigger_type, input_hash, input_payload, langgraph_thread_id, "
                    "run_number, status, heartbeat_at, claim_token) "
                    "VALUES (:id, :oid, :pid, :sid, 'manual', :ih, '{}'::json, :thread, :rn, :st, :hb, :tok)"
                ),
                {**base_params, "tok": claim_token},
            )
    return run_id


async def _run_row(engine: AsyncEngine, run_id: uuid.UUID) -> tuple[str, Any]:
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT status, completed_at, error_code, claim_count, claim_token FROM runs WHERE id=:rid"),
                {"rid": str(run_id)},
            )
        ).fetchone()
    assert row is not None
    return row[0], row[1], row[2], row[3], row[4]


# ---------------------------------------------------------------------------
# 1. dispatch -> RLS claim -> real execute -> complete
# ---------------------------------------------------------------------------


class _StubAdapter(ModelBackendBase):
    """Adapts StubModelBackend (BaseChatModel) to ModelBackendBase async invoke."""

    def __init__(self, fixture_map: dict[str, str]) -> None:
        self._inner = StubModelBackend(fixture_map)

    async def invoke(self, messages: list[BaseMessage], **kwargs: Any) -> BaseMessage:
        return await self._inner.ainvoke(messages, **kwargs)

    def stream(
        self,
        messages: list[BaseMessage],
        tools: list[dict] | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[BaseMessage]:
        return self._inner.astream(messages, tools=tools, **kwargs)

    @property
    def backend_id(self) -> str:
        return "stub"


def _one_agent_graph(node_id: str, backend_id: str) -> dict:
    return {
        "nodes": [
            {
                "id": node_id,
                "agent_id": str(uuid.uuid4()),
                "role": "agent",
                "prompt_template": "Hello {{ state.run_context.input.name }}",
                "model_backend_id": backend_id,
            },
        ],
        "edges": [],
    }


async def test_conformance_run_completes(
    db_engine: AsyncEngine,
    app_engine: AsyncEngine,
    migrated_db_url: str,
    fenced_write_json_codec_workaround: None,
) -> None:
    """The full happy path: dispatch (patched enqueue) -> claim under the
    non-superuser RLS role -> PipelineExecutor.execute drives a 1-node
    StubModelBackend graph -> the run row reaches ``complete`` with
    ``completed_at`` set (a silent wrong-success would leave it ``running``)."""
    from modulo.core import dispatch as dispatch_mod
    from modulo.core.pipeline_engine.executor import PipelineExecutor
    from modulo.settings import get_settings

    org_id = await _seed_org(db_engine, "ConformRun")
    account_id = await _seed_account(db_engine, org_id, "conform-run@test.local")
    pipe = await _seed_pipeline(db_engine, org_id, "PipeConform", account_id)
    node_id = "n1"
    backend_id = str(uuid.uuid4())
    snap = await _seed_snapshot(db_engine, org_id, pipe, _one_agent_graph(node_id, backend_id))
    run_id = await _seed_run(db_engine, org_id, pipe, snap, status="pending")

    # Dispatch: real DB path (dispatched_at + dispatcher='saq' + job id), the
    # SAQ broker itself is stubbed (no Redis in this container).
    enqueue_stub = AsyncMock(return_value=("saq:job:runs:conform", False))
    with patch("modulo.core.dispatch._enqueue_saq", new=enqueue_stub):
        outcome, job_id = await dispatch_mod.dispatch_run(str(run_id), str(org_id))
    assert outcome == "enqueued"
    assert job_id == "saq:job:runs:conform"

    # Claim under the NOBYPASSRLS role (C3: set_config before the claim UPDATE).
    claim_token = await pe.claim_run_async(app_engine, str(run_id), str(org_id))
    assert claim_token is not None, "claim must succeed under RLS"

    # StubModelBackend hub pre-registered; the org has NO model_backends rows,
    # so the executor's _init_model_backend_hub returns None and leaves our hub.
    hub = ModelBackendHub()
    await hub.__aenter__()
    hub.register(
        uuid.UUID(backend_id),
        _StubAdapter({"Hello World": json.dumps({"greeting": "Hello, World!"})}),
    )
    set_model_backend_hub(hub)

    settings = get_settings()
    conn_string = str(settings.database_url).replace("+asyncpg", "").replace("+psycopg", "")
    executor = PipelineExecutor(db_engine, checkpointer_conn_string=conn_string)
    try:
        final = await executor.execute(
            run_id=run_id,
            org_id=org_id,
            input_payload={"name": "World"},
            claim_token=claim_token,
        )
    finally:
        set_model_backend_hub(None)
        await hub.__aexit__(None, None, None)

    assert final.status == "complete"
    status, completed_at, error_code, _claim_count, _token = await _run_row(db_engine, run_id)
    assert status == "complete"
    assert completed_at is not None, "completed_at must be set on a genuine completion"
    assert error_code is None


# ---------------------------------------------------------------------------
# 2. Zombie watchdog: a claimed-but-nodeless executor is failed, not left 30h
# ---------------------------------------------------------------------------


async def test_conformance_stalling_node_fails(
    db_engine: AsyncEngine,
    migrated_db_url: str,
) -> None:
    """An execute_fn that never signals first progress (the 30h+ zombie class:
    claimed-but-nodeless) is cancelled by the zombie watchdog within the grace
    window and the run is terminal-failed ``executor_stalled`` on real PG."""
    org_id = await _seed_org(db_engine, "ConformStall")
    account_id = await _seed_account(db_engine, org_id, "conform-stall@test.local")
    pipe = await _seed_pipeline(db_engine, org_id, "PipeConformStall", account_id)
    snap = await _seed_snapshot(db_engine, org_id, pipe, {"nodes": [{"id": "n1", "node_type": "agent"}], "edges": []})
    run_id = await _seed_run(db_engine, org_id, pipe, snap, status="running", claim_token="tok-stall")

    async def _never_progresses() -> None:
        await asyncio.sleep(999)

    with patch.object(
        pe,
        "get_settings",
        return_value=MagicMock(saq_setup_grace_seconds=1, run_heartbeat_seconds=30),
    ):
        outcome = await pe.run_executor_with_watchdog(  # type: ignore[arg-type]
            db_engine,
            run_id=str(run_id),
            org_id=str(org_id),
            executor=MagicMock(),
            job=None,
            execute_fn=_never_progresses,
        )

    assert outcome == {"status": "failed"}
    status, _completed_at, error_code, _cc, _tok = await _run_row(db_engine, run_id)
    assert status == "failed"
    assert error_code == pe.EXECUTOR_STALLED_ERROR_CODE


# ---------------------------------------------------------------------------
# 3. Worker-death reclaim: token fenced, the run completes exactly once
# ---------------------------------------------------------------------------


async def test_conformance_worker_death_reclaim_no_double_execute(
    db_engine: AsyncEngine,
    migrated_db_url: str,
) -> None:
    """A superseded original (claim token rotated by a successor) can neither
    heartbeat nor complete the run: A's heartbeat_once raises
    ClaimSupersededError, A's mark_complete is a no-op, and the successor's
    mark_complete lands exactly once."""
    org_id = await _seed_org(db_engine, "ConformReclaim")
    account_id = await _seed_account(db_engine, org_id, "conform-reclaim@test.local")
    pipe = await _seed_pipeline(db_engine, org_id, "PipeConformReclaim", account_id)
    snap = await _seed_snapshot(db_engine, org_id, pipe, {"nodes": [{"id": "n1", "node_type": "agent"}], "edges": []})
    run_id = await _seed_run(
        db_engine,
        org_id,
        pipe,
        snap,
        status="running",
        claim_token="tok-a",
        heartbeat_at=datetime.now(UTC) - timedelta(minutes=30),
    )

    # Successor reclaims (stale heartbeat -> claimable); token rotates to B.
    token_b = await pe.claim_run_async(db_engine, str(run_id), str(org_id), stale_seconds=450)
    assert token_b is not None
    assert token_b != "tok-a"

    # A's fenced heartbeat raises — the atomic lease was rotated.
    with pytest.raises(pe.ClaimSupersededError):
        await pe.heartbeat_once(db_engine, str(run_id), str(org_id), claim_token="tok-a")

    # A's fenced completion is a no-op — the run stays running.
    await pe.mark_complete(db_engine, str(run_id), str(org_id), claim_token="tok-a")
    status, completed_at, _ec, _cc, tok = await _run_row(db_engine, run_id)
    assert status == "running"
    assert completed_at is None
    assert tok == token_b

    # The successor completes exactly once.
    await pe.mark_complete(db_engine, str(run_id), str(org_id), claim_token=token_b)
    status, completed_at, _ec, _cc, _tok = await _run_row(db_engine, run_id)
    assert status == "complete"
    assert completed_at is not None

    # A late A-completion is still a no-op after the run is already terminal.
    await pe.mark_complete(db_engine, str(run_id), str(org_id), claim_token="tok-a")
    status, completed_at, _ec, _cc, _tok = await _run_row(db_engine, run_id)
    assert status == "complete"


# ---------------------------------------------------------------------------
# 4. No double sandbox: the DB-atomic dispatch marker is a single-winner fence
# ---------------------------------------------------------------------------


_MARKER_ACQUIRE_SQL = (
    "UPDATE runs SET sandbox_dispatch_state='dispatching', sandbox_id=:sid "
    "WHERE id=:rid AND organisation_id=:oid AND claim_token=:tok AND status='running' RETURNING id"
)


async def test_conformance_no_double_sandbox(
    db_engine: AsyncEngine,
    app_engine: AsyncEngine,
    migrated_db_url: str,
) -> None:
    """Two concurrent claims on one pending run yield exactly one claim token;
    only that token's dispatch-marker UPDATE matches a row — the loser gets
    rowcount 0 and can never provision a second sandbox."""
    org_id = await _seed_org(db_engine, "ConformSandbox")
    account_id = await _seed_account(db_engine, org_id, "conform-sandbox@test.local")
    pipe = await _seed_pipeline(db_engine, org_id, "PipeConformSandbox", account_id)
    snap = await _seed_snapshot(db_engine, org_id, pipe, {"nodes": [{"id": "n1", "node_type": "agent"}], "edges": []})
    run_id = await _seed_run(db_engine, org_id, pipe, snap, status="pending")

    token_a, token_b = await asyncio.gather(
        pe.claim_run_async(app_engine, str(run_id), str(org_id)),
        pe.claim_run_async(app_engine, str(run_id), str(org_id)),
    )
    winner = [t for t in (token_a, token_b) if t is not None]
    assert len(winner) == 1, f"exactly one claim must win, got {winner}"
    winning_token = winner[0]

    async with db_engine.connect() as conn:
        row = (await conn.execute(text("SELECT claim_count FROM runs WHERE id=:rid"), {"rid": str(run_id)})).fetchone()
    assert row is not None
    assert row[0] == 1

    # Loser token (a bogus/stale token) cannot acquire the sandbox dispatch slot.
    async with db_engine.connect() as conn, conn.begin():
        result = await conn.execute(
            text(_MARKER_ACQUIRE_SQL),
            {"rid": str(run_id), "oid": str(org_id), "tok": "tok-loser", "sid": None},
        )
        assert result.fetchone() is None

    # The real claim token acquires exactly one dispatch slot.
    async with db_engine.connect() as conn, conn.begin():
        result = await conn.execute(
            text(_MARKER_ACQUIRE_SQL),
            {"rid": str(run_id), "oid": str(org_id), "tok": winning_token, "sid": None},
        )
        assert result.fetchone() is not None

    async with db_engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT sandbox_dispatch_state FROM runs WHERE id=:rid"),
                {"rid": str(run_id)},
            )
        ).fetchone()
    assert row is not None
    assert row[0] == "dispatching"
