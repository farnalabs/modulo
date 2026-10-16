"""FAR-1838: ``snapshot_version`` on the run list item and run detail response.

The run payload carried ``snapshot_id`` but not the version number, so no run
surface could render a ``v{n}`` chip without a second GET against the snapshot.
Pinned here:

* the DETAIL response surfaces the version of the snapshot ALREADY loaded on
  the run (the rerun/manual-trigger paths attach the snapshot they just loaded;
  the detail path eager-loads it in the run-load transaction) and never issues
  a lazy load — the session factories run ``autobegin=False``, so a run whose
  snapshot was never loaded must degrade to ``None``, not raise;
* the LIST item surfaces the version from the page-wide pre-fetched map, and
  ``_do_list_runs`` fetches that map in EXACTLY ONE ``WHERE id IN (...)``
  statement for the whole page — a per-row snapshot query (N+1) fails the
  statement-count assertion below;
* the version is NULL exactly when ``snapshot_id`` is NULL (legacy runs) —
  never a fabricated ``0``, never the run number;
* the generated frontend contract (``frontend/src/lib/api/schema.ts``) carries
  the field, mirroring the FAR-1233 cancel-reason round-trip check.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy.dialects import postgresql

from modulo.api.routes.runs import (
    _attached_snapshot_version,
    _build_list_item,
    _build_run_response,
    _coerce_snapshot_version,
    _do_list_runs,
    _ListPageContext,
    _ListRunsQuery,
    _load_snapshot_versions,
)
from modulo.auth.jwt import TenantPrincipal
from modulo.db.models.pipeline_snapshot import PipelineSnapshot
from modulo.db.models.run import Run

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_PIPELINE_ID = uuid.uuid4()
_SNAPSHOT_ID = uuid.uuid4()
_SNAPSHOT_VERSION = 7


def _make_ctx(**overrides: Any) -> _ListPageContext:
    kwargs: dict[str, Any] = {
        "child_rollup": {},
        "account_labels": {},
        "trigger_labels": {},
        "active_count": 0,
        "concurrency_limit": None,
    }
    kwargs.update(overrides)
    return _ListPageContext(**kwargs)


def _make_run(**overrides: object) -> MagicMock:
    """MagicMock run stand-in with every field ``_build_list_item`` reads."""
    run = MagicMock()
    run.id = uuid.uuid4()
    run.status = "complete"
    run.pipeline_id = _PIPELINE_ID
    run.pipeline = None
    run.run_number = 1
    run.langgraph_thread_id = "thread-1"
    run.snapshot_id = _SNAPSHOT_ID
    run.error_detail = None
    run.error_code = None
    run.total_cost_usd = None
    run.total_tokens = None
    run.node_token_usage = None
    run.cost_breakdown = None
    run.trigger_type = None
    run.trigger_id = None
    run.account_id = None
    run.heartbeat_at = None
    run.work_item_refs = None
    run.created_at = datetime(2026, 8, 1, 10, 0, 0, tzinfo=UTC)
    run.started_at = None
    run.completed_at = None
    run.execution_origin = None
    run.cancel_reason = None
    run.cancelled_by = None
    run.input_payload = None
    run.guardrail_summary_json = None
    run.run_classification = None
    run.blocked_partial_summary = None
    for key, value in overrides.items():
        setattr(run, key, value)
    return run


def _make_orm_run(snapshot_id: uuid.UUID | None = _SNAPSHOT_ID) -> Run:
    """A real (transient) Run row — the shape the production detail path builds."""
    run = Run(
        id=uuid.uuid4(),
        organisation_id=_ORG_ID,
        pipeline_id=_PIPELINE_ID,
        status="complete",
        langgraph_thread_id="thread-orm",
        snapshot_id=snapshot_id,
        created_at=datetime(2026, 8, 1, 10, 0, 0, tzinfo=UTC),
    )
    run.pipeline = None
    return run


def _make_orm_snapshot(snapshot_id: uuid.UUID = _SNAPSHOT_ID, version: int = _SNAPSHOT_VERSION) -> PipelineSnapshot:
    return PipelineSnapshot(
        id=snapshot_id,
        organisation_id=_ORG_ID,
        pipeline_id=_PIPELINE_ID,
        snapshot_version=version,
        graph_json={},
        connector_bindings_json=[],
        schema_pins_json=[],
        prompt_pins_json=[],
        model_backend_pins_json=[],
    )


# ---------------------------------------------------------------------------
# Detail response — version of the ALREADY loaded snapshot
# ---------------------------------------------------------------------------


def test_run_response_surfaces_attached_snapshot_version() -> None:
    """A run whose snapshot object is in hand surfaces its version.

    This is the attach pattern the rerun/manual-trigger paths use (and what
    the detail path's ``selectinload`` produces). Fails without FAR-1838:
    ``RunResponse`` had no ``snapshot_version`` field at all.
    """
    run = _make_orm_run()
    run.snapshot = _make_orm_snapshot()

    resp = _build_run_response(run)

    assert resp.snapshot_id == _SNAPSHOT_ID
    assert resp.snapshot_version == _SNAPSHOT_VERSION


def test_run_response_version_is_none_when_snapshot_id_is_null() -> None:
    """A legacy run with no snapshot surfaces None — never a fabricated 0."""
    run = _make_run(snapshot_id=None)

    resp = _build_run_response(run)

    assert resp.snapshot_id is None
    assert resp.snapshot_version is None


def test_run_response_version_none_when_snapshot_never_loaded() -> None:
    """A run row whose relationship was never loaded degrades to None.

    The reader must NEVER emit SQL (the factories run ``autobegin=False``, so
    a lazy load outside a transaction raises) — a detached real Run with an
    unloaded ``snapshot`` relationship therefore reads as None, not as a 500.
    """
    run = _make_orm_run()

    resp = _build_run_response(run)

    assert resp.snapshot_id == _SNAPSHOT_ID
    assert resp.snapshot_version is None


def test_run_response_version_none_for_test_double_even_with_snapshot_id() -> None:
    """A MagicMock stand-in never leaks a version (defensive coercion)."""
    run = _make_run()  # snapshot_id set, relationship is a MagicMock child

    resp = _build_run_response(run)

    assert resp.snapshot_id == _SNAPSHOT_ID
    assert resp.snapshot_version is None


def test_coerce_snapshot_version_rejects_bool_and_non_int() -> None:
    """A bool would serialise as the fabricated 0/1 — it must coerce to None."""
    assert _coerce_snapshot_version(3) == 3
    assert _coerce_snapshot_version(True) is None
    assert _coerce_snapshot_version("3") is None
    assert _coerce_snapshot_version(MagicMock()) is None
    assert _coerce_snapshot_version(None) is None


# ---------------------------------------------------------------------------
# List item — version from the page-wide pre-fetched map
# ---------------------------------------------------------------------------


def test_list_item_carries_snapshot_version_from_page_map() -> None:
    run = _make_run()
    ctx = _make_ctx(snapshot_versions={str(_SNAPSHOT_ID): _SNAPSHOT_VERSION})

    item = _build_list_item(run, ctx)

    assert item["snapshot_id"] == str(_SNAPSHOT_ID)
    assert item["snapshot_version"] == _SNAPSHOT_VERSION


def test_list_item_version_none_when_snapshot_id_null_even_with_map_entry() -> None:
    """NULL snapshot_id forces NULL version — the map can never resurrect one."""
    run = _make_run(snapshot_id=None)
    ctx = _make_ctx(snapshot_versions={str(_SNAPSHOT_ID): _SNAPSHOT_VERSION})

    item = _build_list_item(run, ctx)

    assert item["snapshot_id"] is None
    assert item["snapshot_version"] is None


def test_list_item_version_none_when_page_map_lacks_the_snapshot() -> None:
    run = _make_run()
    ctx = _make_ctx(snapshot_versions={})

    item = _build_list_item(run, ctx)

    assert item["snapshot_version"] is None


# ---------------------------------------------------------------------------
# _do_list_runs — ONE batched statement for the whole page (no N+1)
# ---------------------------------------------------------------------------


class _AsyncCM:
    def __init__(self, value: Any = None) -> None:
        self._value = value

    async def __aenter__(self) -> Any:
        return self._value

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _FakeResult:
    def __init__(self, rows: list[Any] | None = None) -> None:
        self._rows = rows if rows is not None else []

    def all(self) -> list[Any]:
        return list(self._rows)

    def scalars(self) -> _FakeResult:
        return self

    def scalar_one_or_none(self) -> Any:
        return None

    def first(self) -> Any:
        return None


class _RecordingSessionFactory:
    """Minimal ``async_sessionmaker`` double that records every statement."""

    def __init__(self, session: _RecordingSession) -> None:
        self._session = session

    def __call__(self) -> _AsyncCM:
        return _AsyncCM(self._session)


class _RecordingSession:
    def __init__(self, snapshot_rows: list[tuple[uuid.UUID, int]]) -> None:
        self.statements: list[Any] = []
        self._snapshot_rows = snapshot_rows

    def begin(self) -> _AsyncCM:
        return _AsyncCM()

    async def execute(self, stmt: Any, *_a: object, **_kw: object) -> _FakeResult:
        self.statements.append(stmt)
        if "pipeline_snapshots" in str(stmt):
            return _FakeResult(list(self._snapshot_rows))
        return _FakeResult()


def _snapshot_statements(session: _RecordingSession) -> list[Any]:
    return [stmt for stmt in session.statements if "pipeline_snapshots" in str(stmt)]


def _make_page(items: list[Any]) -> MagicMock:
    page = MagicMock()
    page.items = items
    page.total = len(items)
    page.page = 1
    page.page_size = 20
    page.next_cursor = None
    page.has_more = False
    return page


async def test_do_list_runs_fetches_snapshot_versions_in_one_query() -> None:
    """A 3-run page with distinct snapshots costs ONE snapshot statement.

    Discriminates against the N+1 regression: a per-row snapshot read would
    issue three statements here (and fail both the count and the value
    assertions, since the fake session only serves the batched result shape).
    """
    snapshot_a, snapshot_b, snapshot_c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    runs = [
        _make_run(snapshot_id=snapshot_a),
        _make_run(snapshot_id=snapshot_b),
        _make_run(snapshot_id=snapshot_c),
        _make_run(snapshot_id=None),  # legacy run — no snapshot, no version
    ]
    session = _RecordingSession([(snapshot_a, 11), (snapshot_b, 22), (snapshot_c, 33)])
    principal = TenantPrincipal(
        username="testuser",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
    )

    with (
        patch("modulo.api.routes.runs.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.runs.set_rls_user_context", new_callable=AsyncMock),
        patch("modulo.api.routes.runs.db_list_runs", new_callable=AsyncMock, return_value=_make_page(runs)),
        patch("modulo.api.routes.runs.get_child_run_rollup", new_callable=AsyncMock, return_value={}),
        patch("modulo.api.routes.runs.get_run_cost_breakdowns", new_callable=AsyncMock, return_value={}),
        patch("modulo.api.routes.runs.count_active_runs_for_org", new_callable=AsyncMock, return_value=0),
        patch("modulo.api.routes.runs.get_org_run_concurrency_limit", new_callable=AsyncMock, return_value=None),
    ):
        result = await _do_list_runs(_RecordingSessionFactory(session), principal, _ListRunsQuery())

    items = result["items"]
    assert [item["snapshot_version"] for item in items] == [11, 22, 33, None]

    snapshot_stmts = _snapshot_statements(session)
    assert len(snapshot_stmts) == 1, (
        "the runs list must load snapshot versions for the whole page in ONE "
        f"statement, not per row; got {len(snapshot_stmts)}: {snapshot_stmts}"
    )
    # The single statement must batch the page's ids (an IN over all three),
    # not address any one of them alone. Compile with literal binds so the
    # parameters are visible in the SQL text.
    rendered = str(snapshot_stmts[0].compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
    assert " IN (" in rendered, f"expected a batched IN (...) statement, got: {rendered}"
    for snapshot_id in (snapshot_a, snapshot_b, snapshot_c):
        assert str(snapshot_id) in rendered, f"batched statement misses {snapshot_id}: {rendered}"


async def test_do_list_runs_issues_no_snapshot_query_for_legacy_only_page() -> None:
    """No snapshot ids on the page → no snapshot statement at all."""
    session = _RecordingSession([])
    principal = TenantPrincipal(
        username="testuser",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
    )

    with (
        patch("modulo.api.routes.runs.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.runs.set_rls_user_context", new_callable=AsyncMock),
        patch(
            "modulo.api.routes.runs.db_list_runs",
            new_callable=AsyncMock,
            return_value=_make_page([_make_run(snapshot_id=None)]),
        ),
        patch("modulo.api.routes.runs.get_child_run_rollup", new_callable=AsyncMock, return_value={}),
        patch("modulo.api.routes.runs.get_run_cost_breakdowns", new_callable=AsyncMock, return_value={}),
        patch("modulo.api.routes.runs.count_active_runs_for_org", new_callable=AsyncMock, return_value=0),
        patch("modulo.api.routes.runs.get_org_run_concurrency_limit", new_callable=AsyncMock, return_value=None),
    ):
        result = await _do_list_runs(_RecordingSessionFactory(session), principal, _ListRunsQuery())

    assert result["items"][0]["snapshot_version"] is None
    assert not _snapshot_statements(session)


# ---------------------------------------------------------------------------
# _load_snapshot_versions — id-shape handling (UUID str vs non-UUID str)
# ---------------------------------------------------------------------------


async def test_load_snapshot_versions_parses_uuid_string_ids() -> None:
    """A run whose ``snapshot_id`` is a UUID STRING is widened, not skipped.

    Production list fakes can surface the id as ``str``; the loader must parse
    it to ``uuid.UUID`` before the batched IN, or the version silently never
    resolves. Exercises the ``isinstance(raw_id, str)`` arm.
    """
    snapshot_id = uuid.uuid4()
    session = _RecordingSession([(snapshot_id, 5)])

    versions = await _load_snapshot_versions(session, [_make_run(snapshot_id=str(snapshot_id))])

    assert versions == {str(snapshot_id): 5}
    statements = _snapshot_statements(session)
    assert len(statements) == 1
    rendered = str(statements[0].compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
    assert str(snapshot_id) in rendered


async def test_load_snapshot_versions_skips_non_uuid_string_ids() -> None:
    """A non-UUID string id degrades to a skip, never a 500.

    ``uuid.UUID("not-a-uuid")`` raises ``ValueError`` — the loader must swallow
    it and keep the rest of the batch. Exercises the ``except ValueError``
    fallback.
    """
    good_id = uuid.uuid4()
    runs = [_make_run(snapshot_id="not-a-uuid"), _make_run(snapshot_id=good_id)]
    session = _RecordingSession([(good_id, 9)])

    versions = await _load_snapshot_versions(session, runs)

    assert versions == {str(good_id): 9}


async def test_load_snapshot_versions_returns_empty_for_only_bad_string_ids() -> None:
    """Only non-UUID strings → no ids collected → no query, no version."""
    session = _RecordingSession([])

    versions = await _load_snapshot_versions(session, [_make_run(snapshot_id="nope")])

    assert versions == {}
    assert not _snapshot_statements(session)


async def test_load_snapshot_versions_omits_uncoercible_version_rows() -> None:
    """A row whose version is not a real int is dropped, never fabricated.

    ``_coerce_snapshot_version`` returns None for a bool/non-int (which would
    otherwise serialise as the fabricated 0/1) — the loader must skip that row
    while keeping the valid ones. Exercises the ``coerced is None`` branch.
    """
    good_id, bad_id = uuid.uuid4(), uuid.uuid4()
    session = _RecordingSession([(good_id, 3), (bad_id, None)])

    versions = await _load_snapshot_versions(session, [_make_run(snapshot_id=good_id)])

    assert versions == {str(good_id): 3}


# ---------------------------------------------------------------------------
# _attached_snapshot_version — instance-state reading
# ---------------------------------------------------------------------------


def test_attached_snapshot_version_none_for_mapped_object_without_snapshot_attr() -> None:
    """A mapped instance with no ``snapshot`` attribute reads as None.

    The reader keys off the instance state's own attributes — an object whose
    mapper has no ``snapshot`` relationship (not a Run) must degrade to None
    rather than raise ``KeyError``. Exercises the ``"snapshot" not in
    state.attrs`` arm.
    """
    assert _attached_snapshot_version(_make_orm_snapshot()) is None


# ---------------------------------------------------------------------------
# Generated API contract
# ---------------------------------------------------------------------------


def test_schema_ts_carries_snapshot_version() -> None:
    """Contract round-trip: the generated frontend type carries the field.

    ``frontend/src/lib/api/schema.ts`` is produced from this model's OpenAPI
    schema by the pinned generator and CI's schema-freshness gate diffs it, so
    this is the fast local half of that gate.
    """
    from modulo.api.routes.runs import RunResponse

    repo_root = Path(__file__).resolve().parents[4]
    props = RunResponse.model_json_schema()["properties"]

    assert "snapshot_version" in props
    assert {"type": "null"} in props["snapshot_version"]["anyOf"]

    schema_ts = (repo_root / "frontend" / "src" / "lib" / "api" / "schema.ts").read_text(encoding="utf-8")
    assert "snapshot_version?: number | null;" in schema_ts
