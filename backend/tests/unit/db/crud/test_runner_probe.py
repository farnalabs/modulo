"""Unit tests for the RunnerProbeCache CRUD (FAR-591, D5).

The integration suite (tests/integration/crud/test_runner_probe.py) also
exercises these, but the SonarCloud coverage import only sees the unit-suite
``coverage.xml`` — and the integration tests never run there (no database).
These unit tests pin each CRUD function's behaviour with a mocked session so
the new code is covered in the unit-suite report too.
"""

import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from modulo.db.crud.runner_probe import (
    get_runner_probe_cache,
    list_org_image_refs,
    list_orgs_with_runner_profiles,
    list_runner_probe_cache,
    prune_stale_runner_probe_rows,
    upsert_runner_probe_cache,
)
from modulo.db.models.runner_probe_cache import RunnerProbeCache

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_OTHER_ORG = uuid.UUID("00000000-0000-0000-0000-000000000002")


def _make_session() -> AsyncMock:
    session = AsyncMock()
    session.execute = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()
    return session


def _result(scalar: Any = None, all_rows: list[Any] | None = None, rowcount: int | None = None) -> MagicMock:
    result = MagicMock()
    result.scalar_one_or_none.return_value = scalar
    scalars = MagicMock()
    scalars.all.return_value = all_rows or []
    result.scalars.return_value = scalars
    result.all.return_value = all_rows or []
    if rowcount is not None:
        result.rowcount = rowcount
    return result


class TestUpsertRunnerProbeCache:
    @pytest.mark.asyncio
    async def test_inserts_new_row_when_absent(self) -> None:
        session = _make_session()
        session.execute.return_value = _result(scalar=None)
        row = await upsert_runner_probe_cache(
            session,
            org_id=_ORG_ID,
            machine_id="machine-1",
            engine_reachable=True,
            images_present=True,
            image_checks={"ref": True},
            engine_info={"cpu_count": 8},
            probe_error=None,
        )
        session.add.assert_called_once()
        assert isinstance(row, RunnerProbeCache)
        assert row.organisation_id == _ORG_ID
        assert row.machine_id == "machine-1"
        assert row.engine_reachable is True
        assert row.images_present is True
        assert row.image_checks_json == {"ref": True}
        assert row.engine_info_json == {"cpu_count": 8}
        session.flush.assert_awaited()

    @pytest.mark.asyncio
    async def test_refreshes_existing_row_in_place(self) -> None:
        existing = RunnerProbeCache(organisation_id=_ORG_ID, machine_id="machine-1")
        existing.engine_reachable = True
        session = _make_session()
        session.execute.return_value = _result(scalar=existing)
        row = await upsert_runner_probe_cache(
            session,
            org_id=_ORG_ID,
            machine_id="machine-1",
            engine_reachable=False,
            images_present=None,
            probe_error="down",
        )
        # No new row added — the existing instance is reused and mutated.
        session.add.assert_not_called()
        assert row is existing
        assert row.engine_reachable is False
        assert row.images_present is None
        assert row.probe_error == "down"
        assert row.image_checks_json == {}
        assert row.engine_info_json == {}

    @pytest.mark.asyncio
    async def test_defaults_timestamp_when_not_supplied(self) -> None:
        session = _make_session()
        session.execute.return_value = _result(scalar=None)
        before = datetime.now(UTC)
        row = await upsert_runner_probe_cache(
            session, org_id=_ORG_ID, machine_id="m", engine_reachable=True, images_present=True
        )
        assert row.probed_at >= before


class TestGetRunnerProbeCache:
    @pytest.mark.asyncio
    async def test_returns_cached_row(self) -> None:
        existing = RunnerProbeCache(organisation_id=_ORG_ID, machine_id="machine-1")
        session = _make_session()
        session.execute.return_value = _result(scalar=existing)
        got = await get_runner_probe_cache(session, org_id=_ORG_ID, machine_id="machine-1")
        assert got is existing


class TestListRunnerProbeCache:
    @pytest.mark.asyncio
    async def test_returns_all_machine_rows_for_org(self) -> None:
        r1 = RunnerProbeCache(organisation_id=_ORG_ID, machine_id="m1")
        r2 = RunnerProbeCache(organisation_id=_ORG_ID, machine_id="m2")
        session = _make_session()
        session.execute.return_value = _result(all_rows=[r1, r2])
        rows = await list_runner_probe_cache(session, org_id=_ORG_ID)
        assert rows == [r1, r2]


class TestPruneStaleRunnerProbeRows:
    @pytest.mark.asyncio
    async def test_returns_number_of_deleted_rows(self) -> None:
        session = _make_session()
        session.execute.return_value = _result(rowcount=3)
        assert await prune_stale_runner_probe_rows(session, retention_seconds=86400) == 3


class TestListOrgsWithRunnerProfiles:
    @pytest.mark.asyncio
    async def test_returns_distinct_org_ids(self) -> None:
        session = _make_session()
        session.execute.return_value = _result(all_rows=[(_ORG_ID,), (_OTHER_ORG,)])
        orgs = await list_orgs_with_runner_profiles(session)
        assert orgs == [_ORG_ID, _OTHER_ORG]


class TestListOrgImageRefs:
    @pytest.mark.asyncio
    async def test_returns_sorted_non_null_refs(self) -> None:
        session = _make_session()
        session.execute.return_value = _result(all_rows=[("ref-b",), (None,), ("ref-a",)])
        refs = await list_org_image_refs(session, org_id=_ORG_ID)
        assert refs == ["ref-a", "ref-b"]
