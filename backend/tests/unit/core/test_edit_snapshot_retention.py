"""FAR-1815: the edit-snapshot retention sweep is enumerable like its siblings.

The sweep's selection is the registered housekeeping category
``edit_snapshot_retention`` (``modulo.core.housekeeping``); ``scan_all`` — the
exact surface ``GET /api/v1/admin/housekeeping`` and the MCP ``list_housekeeping``
tool call — must enumerate it like every other category, with the standard
``pipeline_snapshot`` cleanup entity stamped on its candidates.

The DELETION leg (the real housekeeping cleanup deleting exactly those rows on
real Postgres) is proven in
``tests/integration/test_far1815_edit_snapshot_retention.py``; it cannot run on
the in-memory SQLite unit engine for a pre-existing, unrelated reason: the
cleanup route binds its string ids through SQLAlchemy's character-based Uuid
processor, which calls ``value.hex`` on the bind value and raises on ``str``.
Without the FAR-1815 scanner the category does not exist below, so this test
fails before any deletion question arises.

The reference-safety selection tests (FAR-1815 review C1/M1) pin the non-FK
exclusions a "no external references" seed cannot see: release-channel-tagged,
trigger-pinned and variant-referenced snapshots inside the would-be delete band
are never surfaced as candidates.
"""

import uuid
from collections.abc import AsyncGenerator

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from modulo.core.housekeeping import SCANNERS_BY_CATEGORY, _scan_edit_snapshot_retention, scan_all
from modulo.db.models.base import Base
from modulo.db.models.organisation import Organisation
from modulo.db.models.pipeline import Pipeline
from modulo.db.models.pipeline_snapshot import PipelineSnapshot
from modulo.db.models.trigger import Trigger
from modulo.db.models.variant_group import VariantGroup

_CATEGORY = "edit_snapshot_retention"
_ORG = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
_ACCOUNT = uuid.UUID("00000000-0000-0000-0000-0000000000c1")

# Same table set as test_housekeeping_scanners (every table ``scan_all``
# touches, plus ``organisations`` so the invalid-org-FK scanner resolves the
# seeded org and returns instead of logging a missing-table failure).
_TABLE_NAMES = {
    "accounts",
    "agents",
    "composite_templates",
    "connector_instances",
    "environment_profiles",
    "lifecycle_maps",
    "model_backends",
    "org_api_keys",
    "org_memberships",
    "organisations",
    "parameter_schemas",
    "pipelines",
    "pipeline_snapshots",
    "runs",
    "schemas",
    "secrets",
    "snapshot_schema_pins",
    "sso_providers",
    "team_memberships",
    "teams",
    "triggers",
    "variant_groups",
    "webhook_dedup_hashes",
}


@pytest.fixture
async def engine() -> AsyncGenerator[AsyncEngine, None]:
    eng = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with eng.begin() as conn:
        tables = [t for name, t in Base.metadata.tables.items() if name in _TABLE_NAMES]
        await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=tables))
    # SQLite documents ``PRAGMA foreign_keys`` as a no-op inside a transaction,
    # so the pragma is issued on the bare pooled connection instead (StaticPool →
    # one in-memory connection, covering every later session). Observed on this
    # stack: the default is already OFF (``PRAGMA foreign_keys`` reads 0 on a
    # fresh connection), and the fixture relies on that — seed rows carry
    # synthetic parent ids (e.g. ``account_id`` with no ``accounts`` row) that
    # real FK enforcement would reject.
    async with eng.connect() as conn:
        await conn.exec_driver_sql("PRAGMA foreign_keys = OFF")
    yield eng
    await eng.dispose()


@pytest.fixture
async def session(engine: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s


def _snapshot(*, pipeline_id: uuid.UUID, version: int, version_kind: str, created_kind: str) -> PipelineSnapshot:
    return PipelineSnapshot(
        id=uuid.uuid4(),
        organisation_id=_ORG,
        pipeline_id=pipeline_id,
        snapshot_version=version,
        account_id=_ACCOUNT,
        graph_json={},
        connector_bindings_json=[],
        schema_pins_json=[],
        prompt_pins_json=[],
        model_backend_pins_json=[],
        version_kind=version_kind,
        created_kind=created_kind,
    )


async def _seed_history(session: AsyncSession) -> tuple[Pipeline, dict[int, uuid.UUID]]:
    """Seed one pipeline's full snapshot history.

    Layout: v1 rollback, v2..v207 plain edits (206 rows), v208 initial,
    v209 run, v210 plain edit = the pipeline's latest snapshot. The provenance-
    edit pool is therefore 207 rows (v2..v207 + v210), so retention must select
    exactly v2..v8 — the rows beyond the newest 200 — and nothing else.
    """
    pipeline = Pipeline(id=uuid.uuid4(), organisation_id=_ORG, name="edited", account_id=_ACCOUNT)
    session.add(Organisation(id=_ORG, name="FAR-1815 Org", slug="far-1815-org"))
    session.add(pipeline)

    by_version: dict[int, uuid.UUID] = {}
    rows: list[PipelineSnapshot] = [
        _snapshot(pipeline_id=pipeline.id, version=1, version_kind="edit", created_kind="rollback"),
        _snapshot(pipeline_id=pipeline.id, version=208, version_kind="edit", created_kind="initial"),
        _snapshot(pipeline_id=pipeline.id, version=209, version_kind="run", created_kind="run"),
        _snapshot(pipeline_id=pipeline.id, version=210, version_kind="edit", created_kind="edit"),
    ]
    for version in range(2, 208):
        rows.append(_snapshot(pipeline_id=pipeline.id, version=version, version_kind="edit", created_kind="edit"))
    session.add_all(rows)
    await session.flush()
    for row in rows:
        by_version[row.snapshot_version] = row.id
    await session.commit()
    return pipeline, by_version


class TestEditSnapshotRetentionEnumeration:
    async def test_scan_all_enumerates_the_retention_category_with_exact_candidates(
        self,
        session: AsyncSession,
    ) -> None:
        _pipeline, by_version = await _seed_history(session)
        expected_deleted_versions = {2, 3, 4, 5, 6, 7, 8}

        # Discoverable/enumerable like every sibling category — this is the
        # same scan_all the REST and MCP housekeeping surfaces call.
        results = await scan_all(session, _ORG)
        by_category = {r.category: r for r in results}
        assert _CATEGORY in by_category
        result = by_category[_CATEGORY]
        assert result.label == "Edit Snapshot Retention"
        assert result.description
        for candidate in result.candidates:
            assert candidate.entity_type == "pipeline_snapshot"
        candidate_ids = {c.id for c in result.candidates}
        assert candidate_ids == {str(by_version[version]) for version in expected_deleted_versions}

        # Registry contract: a deletable category (not detection-only) pointing
        # at the standard snapshot cleanup entity.
        entry = SCANNERS_BY_CATEGORY.get(_CATEGORY)
        assert entry is not None
        assert entry.entity_type == "pipeline_snapshot"
        assert entry.scan_func.__name__ == "_scan_edit_snapshot_retention"


class TestEditSnapshotRetentionReferenceSafety:
    """FAR-1815 review C1/M1: externally referenced rows never leave the pool.

    All three tests seed the SAME 207-row provenance-edit pool as the
    enumeration test (so the unfiltered delete band is exactly v2..v8), then
    mark one would-be-deleted rank (v5 — rank 204 in the unfiltered pool) as
    externally referenced. Without the safety predicate that row IS surfaced
    and the first assertion in each test fails; with it the pool shrinks to
    206 rows and the newest-200 bound resurfaces exactly the six oldest
    remaining plain edits.
    """

    async def test_canary_tagged_snapshot_in_the_delete_band_is_never_surfaced(
        self,
        session: AsyncSession,
    ) -> None:
        _pipeline, by_version = await _seed_history(session)
        # C1: release-channel snapshots carry edit/edit kinds but are routing
        # state for resolve_snapshot_for_channel — not save history.
        canary = await session.get(PipelineSnapshot, by_version[5])
        assert canary is not None
        canary.channel = "canary"
        await session.commit()

        candidates = await _scan_edit_snapshot_retention(session, _ORG)
        candidate_ids = {c.id for c in candidates}

        assert str(by_version[5]) not in candidate_ids
        assert candidate_ids == {str(by_version[version]) for version in (2, 3, 4, 6, 7, 8)}

    async def test_trigger_pinned_snapshot_in_the_delete_band_is_never_surfaced(
        self,
        session: AsyncSession,
    ) -> None:
        pipeline, by_version = await _seed_history(session)
        # M1: a trigger pins a specific snapshot via config_json.snapshot_id
        # (the shape api/routes/triggers.py stores; cron_helpers resolves it at
        # fire time) — a non-FK reference the sweep must never surface.
        pinned_version = 5
        session.add(
            Trigger(
                id=uuid.uuid4(),
                organisation_id=_ORG,
                pipeline_id=pipeline.id,
                name="pin-v5",
                trigger_type="manual",
                account_id=_ACCOUNT,
                config_json={"snapshot_id": str(by_version[pinned_version])},
            )
        )
        await session.commit()

        candidates = await _scan_edit_snapshot_retention(session, _ORG)
        candidate_ids = {c.id for c in candidates}

        assert str(by_version[pinned_version]) not in candidate_ids
        assert candidate_ids == {str(by_version[version]) for version in (2, 3, 4, 6, 7, 8)}

    async def test_variant_pinned_snapshot_in_the_delete_band_is_never_surfaced(
        self,
        session: AsyncSession,
    ) -> None:
        pipeline, by_version = await _seed_history(session)
        # M1 (variant leg): a variant group pins one snapshot per variant via
        # the ``variants`` JSON array — the shape variant_group.py fires with
        # (``_coerce_snapshot_id(variant["snapshot_id"])``); no FK exists.
        pinned_version = 5
        session.add(
            VariantGroup(
                id=uuid.uuid4(),
                organisation_id=_ORG,
                pipeline_id=pipeline.id,
                name="ab-pin-v5",
                variants=[
                    {"id": str(uuid.uuid4()), "name": "A", "snapshot_id": str(by_version[pinned_version])},
                    {"id": str(uuid.uuid4()), "name": "B", "snapshot_id": str(uuid.uuid4())},
                ],
            )
        )
        await session.commit()

        candidates = await _scan_edit_snapshot_retention(session, _ORG)
        candidate_ids = {c.id for c in candidates}

        assert str(by_version[pinned_version]) not in candidate_ids
        assert candidate_ids == {str(by_version[version]) for version in (2, 3, 4, 6, 7, 8)}
