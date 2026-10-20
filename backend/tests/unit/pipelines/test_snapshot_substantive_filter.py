"""FAR-1895: the snapshot list's ``exclude_created_kind`` filter.

A run start creates a ``created_kind='run'`` snapshot unconditionally and run
retention never prunes those rows, so a pipeline's snapshot history can be an
unbounded streak of run snapshots. The pipeline editor's version chip resolves
"the current substantive version" by asking for the newest NON-run snapshot;
this module proves that query against a real SQLite database seeded with a
RUN-heavy mixed history (mocked sessions can only assert the SQL text, not
that the rows come back).

Also proves the run-only case: with no substantive version the filtered query
returns NOTHING rather than the newest run snapshot — the failure mode the
unfiltered first page had.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import ARRAY
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

os.environ.setdefault("SECRET_KEY", "a" * 32)
os.environ.setdefault("FERNET_KEY", "a" * 32)

import modulo.db.models  # noqa: F401  (registers every table on Base.metadata)
from modulo.db.crud.pipeline_snapshot_versioning import list_snapshots
from modulo.db.models.account import Account
from modulo.db.models.base import Base
from modulo.db.models.organisation import Organisation
from modulo.db.models.pipeline import Pipeline
from modulo.db.models.pipeline_snapshot import PipelineSnapshot

#: The exclusion the frontend sends (lib/pipelineVersion NON_VERSION_CREATED_KINDS).
_EXCLUDE_RUN = ["run"]


def _sqlite_tables() -> list[object]:
    """Every mapped table SQLite can create (SQLite has no ARRAY type)."""
    return [t for t in Base.metadata.sorted_tables if not any(isinstance(c.type, ARRAY) for c in t.columns)]


@pytest.fixture
async def session_maker(tmp_path) -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'snapshots.db'}", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=_sqlite_tables()))
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


async def _seed(
    session_maker: async_sessionmaker[AsyncSession],
    *,
    run_count: int,
    substantive_versions: list[int],
) -> uuid.UUID:
    """Seed a pipeline whose newest ``run_count`` snapshots are all ``run``.

    Substantive ``edit`` versions are placed BELOW every run snapshot, exactly
    like a long-lived scheduled pipeline's history. Returns the pipeline id.
    """
    org_id = uuid.uuid4()
    pipeline_id = uuid.uuid4()
    creator_id = uuid.uuid4()
    newest_run_version = run_count + len(substantive_versions)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    rows: list[PipelineSnapshot] = []
    for offset in range(run_count):
        version = newest_run_version - offset
        rows.append(
            PipelineSnapshot(
                id=uuid.uuid4(),
                organisation_id=org_id,
                pipeline_id=pipeline_id,
                snapshot_version=version,
                graph_json={"nodes": [], "edges": []},
                connector_bindings_json=[],
                schema_pins_json=[],
                prompt_pins_json=[],
                model_backend_pins_json=[],
                config_json={},
                run_context_defaults={},
                version_kind="run",
                created_kind="run",
                draft=False,
                channel="none",
                created_at=now + timedelta(minutes=offset),
            )
        )
    for index, version in enumerate(substantive_versions):
        rows.append(
            PipelineSnapshot(
                id=uuid.uuid4(),
                organisation_id=org_id,
                pipeline_id=pipeline_id,
                snapshot_version=version,
                graph_json={"nodes": [], "edges": []},
                connector_bindings_json=[],
                schema_pins_json=[],
                prompt_pins_json=[],
                model_backend_pins_json=[],
                config_json={},
                run_context_defaults={},
                version_kind="edit",
                created_kind="edit",
                draft=False,
                channel="none",
                created_at=now - timedelta(days=len(substantive_versions) - index),
            )
        )

    async with session_maker() as session, session.begin():
        session.add(Account(id=creator_id, display_name="Filter Seed"))
        session.add(Organisation(id=org_id, name="Filter Test Org", slug=f"filter-test-{org_id.hex[:8]}"))
        session.add(
            Pipeline(
                id=pipeline_id,
                organisation_id=org_id,
                name=f"Pipeline {pipeline_id.hex[:8]}",
                account_id=creator_id,
            )
        )
        session.add_all(rows)
    return pipeline_id


async def test_filter_returns_only_substantive_kinds_under_a_run_streak(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """A run-only head of 150 rows must not starve the substantive version."""
    pipeline_id = await _seed(session_maker, run_count=150, substantive_versions=[1, 2])

    async with session_maker() as session:
        # Unfiltered page 1 is entirely run snapshots — the starvation shape.
        unfiltered, unfiltered_total = await list_snapshots(session, pipeline_id, page=1, page_size=100)
        # The chip's query: newest substantive version, one row.
        filtered, filtered_total = await list_snapshots(
            session, pipeline_id, page=1, page_size=1, exclude_created_kinds=_EXCLUDE_RUN
        )

    assert unfiltered_total == 152
    assert len(unfiltered) == 100
    assert {row.created_kind for row in unfiltered} == {"run"}

    assert filtered_total == 2
    assert len(filtered) == 1
    assert filtered[0].created_kind == "edit"
    assert filtered[0].snapshot_version == 2


async def test_filter_total_counts_only_matching_rows(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """``total`` describes the FILTERED history, so paging stays coherent."""
    pipeline_id = await _seed(session_maker, run_count=5, substantive_versions=[1])

    async with session_maker() as session:
        page_one, total = await list_snapshots(
            session, pipeline_id, page=1, page_size=1, exclude_created_kinds=_EXCLUDE_RUN
        )

    assert total == 1
    assert [row.snapshot_version for row in page_one] == [1]


async def test_run_only_history_returns_nothing_rather_than_the_newest_run(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """No substantive version exists: the chip must see NOTHING, not a run."""
    pipeline_id = await _seed(session_maker, run_count=20, substantive_versions=[])

    async with session_maker() as session:
        filtered, total = await list_snapshots(
            session, pipeline_id, page=1, page_size=1, exclude_created_kinds=_EXCLUDE_RUN
        )

    assert total == 0
    assert filtered == []


async def test_empty_exclusion_keeps_the_unfiltered_history(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Existing callers (empty/omitted exclusion) are unaffected."""
    pipeline_id = await _seed(session_maker, run_count=3, substantive_versions=[1])

    async with session_maker() as session:
        snapshots, total = await list_snapshots(session, pipeline_id, page=1, page_size=100)

    assert total == 4
    assert [row.created_kind for row in snapshots] == ["run", "run", "run", "edit"]


async def test_explicit_empty_exclusion_is_identical_to_omitted(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """`exclude_created_kinds=[]` and `None` must be the same query (H).

    Both mean "exclude nothing" — the route omits the parameter for every
    existing caller, but the CRUD is also callable with an explicit empty list
    and must not silently become a filter (an empty `not_in(())` would be a
    no-op in SQLAlchemy, but the branch is explicit here for the same reason).
    """
    pipeline_id = await _seed(session_maker, run_count=3, substantive_versions=[1])

    async with session_maker() as session:
        omitted, omitted_total = await list_snapshots(session, pipeline_id, page=1, page_size=100)
        explicit, explicit_total = await list_snapshots(
            session, pipeline_id, page=1, page_size=100, exclude_created_kinds=[]
        )

    assert explicit_total == omitted_total
    assert [(row.id, row.created_kind) for row in explicit] == [(row.id, row.created_kind) for row in omitted]


async def test_exclude_drafts_keeps_an_autosave_from_becoming_the_current_version(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """A newer DRAFT edit must not outrank the last COMMITTED version (D).

    Drafts are written with ``created_kind='edit'`` and a real version, so the
    version filter alone returns the autosave. ``exclude_drafts`` drops it and
    resolves the committed version below it — the same predicate the sibling
    ``resolve_snapshot_for_channel`` applies (``draft.is_(False)``).
    """
    pipeline_id = await _seed(session_maker, run_count=2, substantive_versions=[1, 2])
    draft_id = uuid.uuid4()
    # _seed places 2 runs at v4/v3 and the edits at v2/v1; the draft is newest.
    async with session_maker() as session, session.begin():
        session.add(
            PipelineSnapshot(
                id=draft_id,
                organisation_id=await _pipeline_org(session, pipeline_id),
                pipeline_id=pipeline_id,
                snapshot_version=5,
                graph_json={"nodes": [], "edges": []},
                connector_bindings_json=[],
                schema_pins_json=[],
                prompt_pins_json=[],
                model_backend_pins_json=[],
                config_json={},
                run_context_defaults={},
                version_kind="edit",
                created_kind="edit",
                draft=True,
                channel="none",
                created_at=datetime(2026, 2, 1, tzinfo=UTC),
            )
        )

    async with session_maker() as session:
        with_draft, _ = await list_snapshots(
            session, pipeline_id, page=1, page_size=2, exclude_created_kinds=_EXCLUDE_RUN
        )
        without_draft, _ = await list_snapshots(
            session, pipeline_id, page=1, page_size=2, exclude_created_kinds=_EXCLUDE_RUN, exclude_drafts=True
        )

    # The draft IS the newest non-run row when drafts are not excluded.
    assert with_draft[0].id == draft_id
    assert with_draft[0].draft is True
    # With drafts excluded, the newest committed version resolves instead.
    assert without_draft[0].snapshot_version == 2
    assert without_draft[0].draft is False


async def _pipeline_org(session: AsyncSession, pipeline_id: uuid.UUID) -> uuid.UUID:
    from sqlalchemy import select as _select

    result = await session.execute(_select(Pipeline.organisation_id).where(Pipeline.id == pipeline_id))
    return result.scalar_one()


def test_created_kind_is_non_nullable_so_the_filter_never_sees_null() -> None:
    """H: the NULL-``created_kind`` case is unreachable, pinned to the model.

    Under three-valued logic ``created_kind <> 'run'`` evaluates to NULL for a
    NULL value, so a NULL row would be EXCLUDED from the page rather than
    treated as a version. The CHECK vocabulary only closes the value set — it is
    the NOT NULL column that makes the predicate's NULL arm unreachable. This
    test pins that: if the column were ever made nullable, the filter's meaning
    would change silently, and this assertion fails instead.
    """
    from sqlalchemy import CheckConstraint

    column = PipelineSnapshot.__table__.c.created_kind
    assert column.nullable is False
    assert column.server_default is not None
    assert str(column.server_default.arg) == "run"
    check_names = {c.name for c in PipelineSnapshot.__table__.constraints if isinstance(c, CheckConstraint)}
    assert "ck_pipeline_snapshots_created_kind" in check_names
