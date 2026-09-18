"""Unit tests for the FAR-775 variant_batch_state CRUD in variant_group.py.

Pure unit tests (no DB, no RLS) that exercise the org-scoped batch-state
helpers so the new production code reaches the SonarCloud new-code coverage
gate. Coverage is the goal; each function is driven with a mocked session.
"""

import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from unittest.mock import patch as _patch

from modulo.db.crud.variant_group import (
    get_all_state_batch_ids,
    get_batch_runs,
    get_batch_state,
    list_batch_runs_for_batch_ids,
    list_batch_states,
    soft_delete_batch_state,
    upsert_batch_state,
)
from modulo.db.models.variant_batch_state import VariantBatchState


def _make_result(
    *,
    scalar_one_or_none: Any = None,
    scalar_one: Any = 0,
    scalars_all: list[Any] | None = None,
    all_rows: list[Any] | None = None,
) -> MagicMock:
    result = MagicMock()
    result.scalar_one_or_none.return_value = scalar_one_or_none
    result.scalar_one.return_value = scalar_one
    scalars_mock = MagicMock()
    _scalars = scalars_all if scalars_all is not None else []
    scalars_mock.__iter__.return_value = iter(_scalars)
    scalars_mock.all.return_value = _scalars
    result.scalars.return_value = scalars_mock
    result.all.return_value = all_rows if all_rows is not None else []
    result.first.return_value = None
    return result


def _make_session(result: MagicMock | None = None) -> AsyncMock:
    session = AsyncMock()
    session.execute = AsyncMock(return_value=result if result is not None else _make_result())
    session.add = MagicMock()
    session.flush = AsyncMock()
    return session


@contextmanager
def _patch_get_batch_state(return_value: Any):
    with _patch("modulo.db.crud.variant_group.get_batch_state", new_callable=AsyncMock, return_value=return_value):
        yield


class TestGetBatchState:
    async def test_returns_state_row(self) -> None:
        state = VariantBatchState(batch_id=uuid.uuid4())
        session = _make_session(_make_result(scalar_one_or_none=state))
        got = await get_batch_state(session, batch_id=state.batch_id, org_id=uuid.uuid4())
        assert got is state

    async def test_returns_none_when_absent(self) -> None:
        session = _make_session(_make_result(scalar_one_or_none=None))
        got = await get_batch_state(session, batch_id=uuid.uuid4(), org_id=uuid.uuid4())
        assert got is None


class TestGetBatchRuns:
    async def test_returns_org_scoped_runs(self) -> None:
        run = MagicMock()
        run.id = uuid.uuid4()
        run.batch_id = uuid.uuid4()
        session = _make_session(_make_result(scalars_all=[run]))
        runs = await get_batch_runs(session, org_id=uuid.uuid4(), batch_id=run.batch_id)
        assert runs == [run]


class TestListBatchRunsForBatchIds:
    async def test_empty_batch_ids_short_circuits(self) -> None:
        session = _make_session()
        result = await list_batch_runs_for_batch_ids(session, org_id=uuid.uuid4(), batch_ids=[])
        assert result == {}

    async def test_groups_runs_by_batch(self) -> None:
        bid1, bid2 = uuid.uuid4(), uuid.uuid4()
        run1, run2, run3 = MagicMock(), MagicMock(), MagicMock()
        run1.batch_id = bid1
        run2.batch_id = bid1
        run3.batch_id = bid2
        session = _make_session(_make_result(scalars_all=[run1, run2, run3]))
        by_batch = await list_batch_runs_for_batch_ids(session, org_id=uuid.uuid4(), batch_ids=[bid1, bid2])
        assert len(by_batch[bid1]) == 2
        assert by_batch[bid2] == [run3]


class TestGetAllStateBatchIds:
    async def test_returns_id_set(self) -> None:
        bid1, bid2 = uuid.uuid4(), uuid.uuid4()
        row1, row2 = MagicMock(), MagicMock()
        row1.__getitem__.return_value = bid1
        row2.__getitem__.return_value = bid2
        session = _make_session(_make_result(all_rows=[row1, row2]))
        ids = await get_all_state_batch_ids(session, org_id=uuid.uuid4())
        assert ids == {bid1, bid2}


class TestListBatchStates:
    async def test_paginated_listing(self) -> None:
        state = VariantBatchState(batch_id=uuid.uuid4())
        session = _make_session(_make_result(scalar_one=7, scalars_all=[state]))
        items, total = await list_batch_states(session, org_id=uuid.uuid4(), page=1, page_size=20)
        assert items == [state]
        assert total == 7


class TestUpsertBatchState:
    async def test_inserts_when_absent(self) -> None:
        org_id = uuid.uuid4()
        batch_id = uuid.uuid4()
        session = _make_session()
        with _patch_get_batch_state(None):
            result = await upsert_batch_state(
                session,
                batch_id=batch_id,
                org_id=org_id,
                name="n",
                pipeline_id=uuid.uuid4(),
                variant_group_id=uuid.uuid4(),
                input_payload={"k": "v"},
            )
        assert isinstance(result, VariantBatchState)
        session.add.assert_called_once()

    async def test_updates_when_present(self) -> None:
        org_id = uuid.uuid4()
        batch_id = uuid.uuid4()
        existing = VariantBatchState(batch_id=batch_id)
        session = _make_session()
        with _patch_get_batch_state(existing):
            result = await upsert_batch_state(
                session,
                batch_id=batch_id,
                org_id=org_id,
                name="n",
                pipeline_id=uuid.uuid4(),
                variant_group_id=uuid.uuid4(),
                input_payload={"k": "v"},
            )
        assert result is existing
        assert existing.name == "n"
        session.add.assert_not_called()

    async def test_noop_update_when_fields_omitted(self) -> None:
        batch_id = uuid.uuid4()
        existing = VariantBatchState(batch_id=batch_id)
        session = _make_session()
        with _patch_get_batch_state(existing):
            result = await upsert_batch_state(session, batch_id=batch_id, org_id=uuid.uuid4())
        assert result is existing
        assert existing.name is None


class TestSoftDeleteBatchState:
    async def test_returns_false_when_absent(self) -> None:
        session = _make_session()
        with _patch_get_batch_state(None):
            assert await soft_delete_batch_state(session, batch_id=uuid.uuid4(), org_id=uuid.uuid4()) is False

    async def test_already_deleted_returns_true(self) -> None:
        existing = VariantBatchState(batch_id=uuid.uuid4(), deleted_at=datetime.now(UTC))
        session = _make_session()
        with _patch_get_batch_state(existing):
            assert await soft_delete_batch_state(session, batch_id=uuid.uuid4(), org_id=uuid.uuid4()) is True

    async def test_soft_deletes(self) -> None:
        existing = VariantBatchState(batch_id=uuid.uuid4())
        session = _make_session()
        with _patch_get_batch_state(existing):
            assert await soft_delete_batch_state(session, batch_id=uuid.uuid4(), org_id=uuid.uuid4()) is True
        assert existing.deleted_at is not None
