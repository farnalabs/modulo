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
