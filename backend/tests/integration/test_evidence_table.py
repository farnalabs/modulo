"""Evidence table tests against real Postgres (FAR-966 chunk 7).

Behavioural portion of the acceptance criteria, where a real database is
required (Docker/testcontainers): migrations and the shipped RLS policy
actually apply.

- **E3** — lookup semantics: most-recent-by-created_at wins.
- **E4** — lookup semantics: higher-id tiebreaker on equal created_at.
- **E5** — absent evidence yields nothing at the fetch boundary.
- **E6** — exact-duplicate inserts are rejected by the composite unique.
- **F1** — one fetch returns evidence for all keys of a subject.
- **F2** — fetch is scoped by subject only, never leaking a sibling subject.
- **E7** — evidence referencing a non-existent organisation is rejected (FK).
- **F3** — the fetch query plans exactly one evidence scan (single statement).
- **E8** — ``ix_evidence_created_at`` covers the fetch query plan (EXPLAIN).
- **E9** — ``ck_evidence_producer_type`` rejects unknown producer types.
- **sup** — RLS org-isolation proofs (extra beyond the numbered criteria):
  the shipped policy separates organisations under a non-superuser role.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from modulo.core.eval_engine.evidence_layer import fetch

pytestmark = pytest.mark.integration

_BASE = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
_SUBJECT = "node_execution"

_EVIDENCE_INSERT = (
    "INSERT INTO evidence "
    "(id, organisation_id, key, subject_type, subject_id, value, producer_type, created_at, observed_at) "
    "VALUES (:id, :org, :key, :stype, :sid, CAST(:value AS jsonb), :producer, :created, :observed)"
)


def _unique_key(prefix: str = "eval_") -> str:
    return f"{prefix}{uuid.uuid4().hex[:8]}"


def _seed(
    evidence_id: uuid.UUID,
    org_id: uuid.UUID,
    key: str,
    subject_id: str,
    value: bool,
    created_at: datetime,
    producer_type: str = "eval",
) -> dict[str, object]:
    # asyncpg binds by native type: the JSONB parameter must be TEXT payloads
    # (a bare Python bool fails with "'bool' object has no attribute 'encode'").
    return {
        "id": str(evidence_id),
        "org": str(org_id),
        "key": key,
        "stype": _SUBJECT,
        "sid": subject_id,
        "value": json.dumps(value),
        "producer": producer_type,
        "created": created_at,
        "observed": _BASE,
    }


async def _run(engine: AsyncEngine, sql: str, params: dict[str, object] | None = None) -> None:
    async with engine.connect() as conn, conn.begin():
        await conn.execute(text(sql), params or {})


async def _clean_subject(engine: AsyncEngine, org_id: uuid.UUID, subject_id: str) -> None:
    await _run(
        engine,
        "DELETE FROM evidence WHERE organisation_id = :org AND subject_id = :sid",
        {"org": str(org_id), "sid": subject_id},
    )


async def _clean_key(engine: AsyncEngine, org_id: uuid.UUID, key: str) -> None:
    await _run(
        engine,
        "DELETE FROM evidence WHERE organisation_id = :org AND key = :key",
        {"org": str(org_id), "key": key},
    )


async def _clean_by_ids(engine: AsyncEngine, evidence_ids: list[str]) -> None:
    await _run(engine, "DELETE FROM evidence WHERE id = ANY(:ids)", {"ids": evidence_ids})


async def _fetch_rows(engine: AsyncEngine, org_id: uuid.UUID, subject_id: str) -> list[Any]:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        return await fetch(_SUBJECT, subject_id, org_id, session)


# ---------------------------------------------------------------------------
# E3 — most-recent-by-created_at wins (explicit timestamp injection)
# ---------------------------------------------------------------------------


async def test_e3_most_recent_created_at_wins(db_engine: AsyncEngine, test_org: uuid.UUID) -> None:
    """Two rows for one {key, subject}: only the newer created_at is returned."""
    key = _unique_key()
    subject_id = f"e3-{uuid.uuid4().hex[:8]}"
    older_id = uuid.uuid4()
    newer_id = uuid.uuid4()
    await _run(
        db_engine,
        _EVIDENCE_INSERT,
        _seed(older_id, test_org, key, subject_id, False, _BASE),
    )
    try:
        await _run(
            db_engine,
            _EVIDENCE_INSERT,
            _seed(newer_id, test_org, key, subject_id, True, _BASE + timedelta(minutes=5)),
        )
        rows = await _fetch_rows(db_engine, test_org, subject_id)
    finally:
        await _clean_subject(db_engine, test_org, subject_id)
    assert len(rows) == 1
    assert rows[0].id == newer_id
    assert rows[0].value is True


# ---------------------------------------------------------------------------
# E4 — higher-id tiebreaker on equal created_at
# ---------------------------------------------------------------------------


async def test_e4_higher_id_tiebreak_wins_on_equal_created_at(db_engine: AsyncEngine, test_org: uuid.UUID) -> None:
    """Two rows sharing a created_at: the higher id wins the tiebreak."""
    key = _unique_key()
    subject_id = f"e4-{uuid.uuid4().hex[:8]}"
    pair = sorted([uuid.uuid4(), uuid.uuid4()])
    id_lo, id_hi = pair[0], pair[1]
    created = _BASE
    try:
        await _run(db_engine, _EVIDENCE_INSERT, _seed(id_lo, test_org, key, subject_id, False, created))
        await _run(db_engine, _EVIDENCE_INSERT, _seed(id_hi, test_org, key, subject_id, True, created))
        rows = await _fetch_rows(db_engine, test_org, subject_id)
    finally:
        await _clean_subject(db_engine, test_org, subject_id)
    assert len(rows) == 1
    assert rows[0].id == id_hi
    assert rows[0].value is True


# ---------------------------------------------------------------------------
# E5 — absent evidence yields nothing at the fetch boundary
# ---------------------------------------------------------------------------


async def test_e5_absent_evidence_yields_nothing(db_engine: AsyncEngine, test_org: uuid.UUID) -> None:
    """Fetching a subject with no evidence returns an empty result."""
    ghost_subject = f"e5-ghost-{uuid.uuid4().hex[:8]}"
    rows = await _fetch_rows(db_engine, test_org, ghost_subject)
    assert len(rows) == 0


async def test_e5_only_present_keys_come_back(db_engine: AsyncEngine, test_org: uuid.UUID) -> None:
    """Two keys present, one never written: only the two come back."""
    key_a = _unique_key()
    key_b = _unique_key()
    never_written = _unique_key()
    subject_id = f"e5-{uuid.uuid4().hex[:8]}"
    ids: list[str] = []
    try:
        for evidence_id, each_key in (
            (uuid.uuid4(), key_a),
            (uuid.uuid4(), key_b),
        ):
            ids.append(str(evidence_id))
            await _run(
                db_engine,
                _EVIDENCE_INSERT,
                _seed(evidence_id, test_org, each_key, subject_id, True, _BASE),
            )
        rows = await _fetch_rows(db_engine, test_org, subject_id)
    finally:
        await _clean_by_ids(db_engine, ids)
    returned_keys = sorted(row.key for row in rows)
    assert returned_keys == sorted([key_a, key_b])
    assert never_written not in returned_keys


# ---------------------------------------------------------------------------
# E6 — exact-duplicate inserts are rejected by the composite unique
# ---------------------------------------------------------------------------


async def test_e6_exact_duplicate_insert_rejected(db_engine: AsyncEngine, test_org: uuid.UUID) -> None:
    """Same six-column identity twice → unique violation; +1s later → ok."""
    key = _unique_key()
    subject_id = f"e6-{uuid.uuid4().hex[:8]}"
    dup_id = uuid.uuid4()
    try:
        await _run(db_engine, _EVIDENCE_INSERT, _seed(dup_id, test_org, key, subject_id, True, _BASE))
        with pytest.raises(IntegrityError):  # exact duplicate → composite unique violation
            await _run(db_engine, _EVIDENCE_INSERT, _seed(dup_id, test_org, key, subject_id, True, _BASE))
        # One second later the same fact is a NEW observation: new id, accepted.
        await _run(
            db_engine,
            _EVIDENCE_INSERT,
            _seed(uuid.uuid4(), test_org, key, subject_id, True, _BASE + timedelta(seconds=1)),
        )
        async with db_engine.connect() as conn, conn.begin():
            count = (
                await conn.execute(
                    text("SELECT count(*) FROM evidence WHERE organisation_id = :org AND key = :key"),
                    {"org": str(test_org), "key": key},
                )
            ).scalar()
        assert count == 2
    finally:
        await _clean_key(db_engine, test_org, key)


# ---------------------------------------------------------------------------
# F1 / F2 — fetch scope and completeness (behavioural round-trip)
# ---------------------------------------------------------------------------


async def test_f1_fetch_returns_all_keys_for_subject(db_engine: AsyncEngine, test_org: uuid.UUID) -> None:
    """Three keys seeded for one subject → one fetch returns all three."""
    subject_id = f"f1-{uuid.uuid4().hex[:8]}"
    keys = [_unique_key(), _unique_key(), _unique_key()]
    ids: list[str] = []
    try:
        for index, each_key in enumerate(keys):
            evidence_id = uuid.uuid4()
            ids.append(str(evidence_id))
            await _run(
                db_engine,
                _EVIDENCE_INSERT,
                _seed(evidence_id, test_org, each_key, subject_id, True, _BASE + timedelta(minutes=index)),
            )
        rows = await _fetch_rows(db_engine, test_org, subject_id)
    finally:
        await _clean_by_ids(db_engine, ids)
    assert sorted(row.key for row in rows) == sorted(keys)


async def test_f2_fetch_is_scoped_by_subject_only(db_engine: AsyncEngine, test_org: uuid.UUID) -> None:
    """Subject B's evidence never leaks into subject A's fetch result."""
    subject_a = f"f2-a-{uuid.uuid4().hex[:8]}"
    subject_b = f"f2-b-{uuid.uuid4().hex[:8]}"
    key_a = _unique_key()
    key_b = _unique_key()
    key_never = _unique_key()
    ids: list[str] = []
    try:
        for evidence_id, each_key, each_subject in (
            (uuid.uuid4(), key_a, subject_a),
            (uuid.uuid4(), key_never, subject_a),
            (uuid.uuid4(), key_b, subject_b),
        ):
            ids.append(str(evidence_id))
            await _run(
                db_engine,
                _EVIDENCE_INSERT,
                _seed(evidence_id, test_org, each_key, each_subject, True, _BASE),
            )
        rows_a = await _fetch_rows(db_engine, test_org, subject_a)
        keys_a = sorted(row.key for row in rows_a)
        rows_b = await _fetch_rows(db_engine, test_org, subject_b)
        keys_b = sorted(row.key for row in rows_b)
    finally:
        await _clean_by_ids(db_engine, ids)
    assert keys_a == sorted([key_a, key_never])
    assert keys_b == [key_b]
    assert key_b not in keys_a


# ---------------------------------------------------------------------------
# F3 / E8 — query-plan coverage under real Postgres
# ---------------------------------------------------------------------------


class _CapturedExec:
    def scalars(self) -> _CapturedExec:
        return self

    def all(self) -> list[Any]:
        return []


class _PostgresBind:
    """Minimal bind double: ``fetch`` resolves its backend from the bind.

    The plan-coverage tests compile and EXPLAIN against real Postgres, so the
    double advertises the Postgres dialect to select the ``DISTINCT ON`` path.
    """

    class _Dialect:
        name = "postgresql"

    dialect = _Dialect()


class _CapturingSession:
    def __init__(self) -> None:
        self.statements: list[Any] = []

    def get_bind(self) -> _PostgresBind:
        return _PostgresBind()

    async def execute(self, statement: Any) -> _CapturedExec:
        self.statements.append(statement)
        return _CapturedExec()


async def _compile_fetch_sql(org_id: uuid.UUID) -> str:
    """Compile the production fetch statement for Postgres with literals."""
    capturing = _CapturingSession()
    await fetch(_SUBJECT, f"e8-{uuid.uuid4().hex[:8]}", org_id, capturing)
    compiled = capturing.statements[0].compile(
        dialect=postgresql.dialect(),
        compile_kwargs={"literal_binds": True},
    )
    return str(compiled).upper()


async def _explain_fetch(db_engine: AsyncEngine, org_id: uuid.UUID) -> list[dict[str, Any]]:
    """EXPLAIN the production fetch, return the flattened JSON plan."""
    sql = await _compile_fetch_sql(org_id)
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(text("SET LOCAL enable_seqscan = off"))
        raw = (await conn.execute(text(f"EXPLAIN (FORMAT JSON) {sql}"))).scalar()
    if isinstance(raw, str):
        raw = json.loads(raw)
    return raw


def _evidence_scan_nodes(plan_flat: list[dict[str, Any]]) -> list[dict[str, Any]]:
    top = plan_flat[0]["Plan"]
    scans: list[dict[str, Any]] = []
    for node in _walk_plan(top):
        if node.get("Node Type") in ("Seq Scan", "Index Scan", "Index Only Scan", "Bitmap Heap Scan"):
            scans.append(node)
    return scans


def _walk_plan(node: dict[str, Any]) -> list[dict[str, Any]]:
    nodes = [node]
    for child in node.get("Plans", []):
        nodes.extend(_walk_plan(child))
    return nodes


async def test_f3_fetch_plans_exactly_one_evidence_scan(db_engine: AsyncEngine, test_org: uuid.UUID) -> None:
    """Cross-key consistency lives in ONE statement: one scan in the plan."""
    plan = await _explain_fetch(db_engine, test_org)
    scans = _evidence_scan_nodes(plan)
    evidence_scans = [node for node in scans if node.get("Relation Name") == "evidence"]
    assert len(evidence_scans) == 1
    other_scans = [node for node in scans if node.get("Relation Name") != "evidence"]
    assert len(other_scans) == 0


async def test_e8_created_at_index_covers_fetch_plan(db_engine: AsyncEngine, test_org: uuid.UUID) -> None:
    """ix_evidence_created_at (or the org_subject one) serves the subject fetch."""
    plan = await _explain_fetch(db_engine, test_org)
    scans = _evidence_scan_nodes(plan)
    assert len(scans) == 1
    index_name = scans[0].get("Index Name")
    assert index_name in {"ix_evidence_created_at", "ix_evidence_org_subject"}


# ---------------------------------------------------------------------------
# E7 — organisation FK integrity
# ---------------------------------------------------------------------------


async def test_e7_nonexistent_organisation_fk_rejected(db_engine: AsyncEngine) -> None:
    """An evidence row naming a non-existent organisation is rejected by FK."""
    phantom_org = uuid.uuid4()
    params = _seed(uuid.uuid4(), phantom_org, _unique_key(), f"e7-{uuid.uuid4().hex[:8]}", True, _BASE)
    with pytest.raises(IntegrityError):  # phantom org violates the FK
        await _run(db_engine, _EVIDENCE_INSERT, params)


# ---------------------------------------------------------------------------
# E9 — producer_type CHECK constraint
# ---------------------------------------------------------------------------


async def test_e9_unknown_producer_type_rejected(db_engine: AsyncEngine, test_org: uuid.UUID) -> None:
    """producer_type outside the shipped vocabulary violates the CHECK."""
    params = {
        **_seed(uuid.uuid4(), test_org, _unique_key(), f"e9-{uuid.uuid4().hex[:8]}", True, _BASE),
        "producer": "unknown_kind",
    }
    with pytest.raises(IntegrityError):  # unknown producer type → CHECK failure
        await _run(db_engine, _EVIDENCE_INSERT, params)


async def test_e9_every_valid_producer_type_is_accepted(db_engine: AsyncEngine, test_org: uuid.UUID) -> None:
    """All five valid producer types insert cleanly."""
    subject_id = f"e9-valid-{uuid.uuid4().hex[:8]}"
    key = _unique_key()
    inserted_ids: list[str] = []
    try:
        for producer_type in ("eval", "policy_gate", "run", "system_state", "derived"):
            evidence_id = uuid.uuid4()
            inserted_ids.append(str(evidence_id))
            params = _seed(evidence_id, test_org, key, subject_id, True, _BASE)
            params["producer"] = producer_type
            await _run(db_engine, _EVIDENCE_INSERT, params)
        async with db_engine.connect() as conn, conn.begin():
            count = (
                await conn.execute(
                    text("SELECT count(*) FROM evidence WHERE organisation_id = :org AND key = :key"),
                    {"org": str(test_org), "key": key},
                )
            ).scalar()
        assert count == 5
    finally:
        await _clean_key(db_engine, test_org, key)


# ---------------------------------------------------------------------------
# sup — RLS org-isolation proofs on the shipped policy
# ---------------------------------------------------------------------------


async def _create_other_org(db_engine: AsyncEngine) -> uuid.UUID:
    """Commit a second organisation row (mirrors the conftest test_org shape)."""
    org_id = uuid.uuid4()
    await _run(
        db_engine,
        "INSERT INTO organisations (id, name, slug, settings_json) VALUES (:id, :name, :slug, '{}'::json)",
        {"id": str(org_id), "name": "RLS Other Org", "slug": f"rls-{org_id.hex[:8]}"},
    )
    return org_id


async def _scoped_ids(app_engine: AsyncEngine, own_org: uuid.UUID, key: str) -> list[str]:
    """SELECT evidence under a scoped non-superuser session, list org ids."""
    async with app_engine.connect() as conn, conn.begin():
        await conn.execute(
            text("SELECT set_config('app.organisation_id', :oid, true)"),
            {"oid": str(own_org)},
        )
        rows = (
            await conn.execute(
                text("SELECT organisation_id::text FROM evidence WHERE key = :key"),
                {"key": key},
            )
        ).all()
    return [row[0] for row in rows]


async def test_rls_cross_org_rows_invisible_to_scoped_session(
    db_engine: AsyncEngine, app_engine: AsyncEngine, test_org: uuid.UUID
) -> None:
    """The integrator role scoped to one org cannot see other orgs' evidence."""
    shared_key = _unique_key()
    subject_id = f"rls-sel-{uuid.uuid4().hex[:8]}"
    other_org_id = await _create_other_org(db_engine)
    own_id = uuid.uuid4()
    other_id = uuid.uuid4()
    try:
        await _run(db_engine, _EVIDENCE_INSERT, _seed(own_id, test_org, shared_key, subject_id, True, _BASE))
        await _run(db_engine, _EVIDENCE_INSERT, _seed(other_id, other_org_id, shared_key, subject_id, True, _BASE))
        visible = await _scoped_ids(app_engine, test_org, shared_key)
    finally:
        await _clean_subject(db_engine, test_org, subject_id)
        await _run(db_engine, "DELETE FROM organisations WHERE id = :oid", {"oid": str(other_org_id)})
    assert visible == [str(test_org)]
    assert str(other_org_id) not in visible


async def test_rls_write_rejected_when_scope_disagrees_with_row(
    db_engine: AsyncEngine, app_engine: AsyncEngine, test_org: uuid.UUID
) -> None:
    """An INSERT naming a different org than the session scope fails closed."""
    other_org_id = await _create_other_org(db_engine)
    row_id = uuid.uuid4()
    key = _unique_key()
    subject_id = f"rls-deny-{uuid.uuid4().hex[:8]}"
    params = _seed(row_id, other_org_id, key, subject_id, True, _BASE)
    try:
        async with app_engine.connect() as conn, conn.begin():
            await conn.execute(
                text("SELECT set_config('app.organisation_id', :oid, true)"),
                {"oid": str(test_org)},
            )
            with pytest.raises(DBAPIError) as excinfo:  # RLS WITH CHECK violations raise
                await conn.execute(text(_EVIDENCE_INSERT), params)
        assert "violates row-level security policy" in str(excinfo.value)
    finally:
        await _run(db_engine, "DELETE FROM organisations WHERE id = :oid", {"oid": str(other_org_id)})
    superuser_probe = await _fetch_rows(db_engine, other_org_id, subject_id)
    assert len(superuser_probe) == 0


async def test_rls_write_accepted_when_scope_matches_row(
    db_engine: AsyncEngine, app_engine: AsyncEngine, test_org: uuid.UUID
) -> None:
    """An INSERT naming the row's own scoped org succeeds and persists."""
    key = _unique_key()
    subject_id = f"rls-ok-{uuid.uuid4().hex[:8]}"
    row_id = uuid.uuid4()
    try:
        async with app_engine.connect() as conn, conn.begin():
            await conn.execute(
                text("SELECT set_config('app.organisation_id', :oid, true)"),
                {"oid": str(test_org)},
            )
            await conn.execute(text(_EVIDENCE_INSERT), _seed(row_id, test_org, key, subject_id, True, _BASE))
        persisted = await _fetch_rows(db_engine, test_org, subject_id)
    finally:
        await _clean_key(db_engine, test_org, key)
    assert len(persisted) == 1
    assert persisted[0].id == row_id
