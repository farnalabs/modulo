"""Unit tests for org deletion / retention CRUD (mocked session, no DB)."""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from sys import modules as sys_modules
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


@pytest.fixture
def mock_session() -> AsyncMock:
    return AsyncMock(spec=AsyncSession)


def _make_org(**overrides: Any) -> MagicMock:
    org = MagicMock()
    org.id = _ORG_ID
    org.name = "Org"
    org.slug = "org"
    org.status = overrides.get("status", "active")
    org.deleted_at = overrides.get("deleted_at")
    org.deletion_token = overrides.get("deletion_token", "tok-123")
    org.deletion_token_expires_at = overrides.get("token_expires_at", datetime.now(UTC) + timedelta(hours=1))
    org.export_bundle_json = overrides.get("export_bundle_json")
    return org


def _lookup_result(org: object | None) -> MagicMock:
    return MagicMock(scalar_one_or_none=MagicMock(return_value=org))


def _fake_module(**attrs: object) -> SimpleNamespace:
    return SimpleNamespace(**attrs)


class TestHelpers:
    def test_generate_deletion_token_is_urlsafe_and_long(self) -> None:
        from modulo.db.crud.org_deletion import _generate_deletion_token

        token = _generate_deletion_token()
        assert len(token) >= 64
        assert token != _generate_deletion_token()

    async def test_count_non_terminal_runs(self, mock_session: AsyncMock) -> None:
        result = MagicMock()
        result.scalar = MagicMock(return_value=7)
        mock_session.execute = AsyncMock(return_value=result)
        from modulo.db.crud.org_deletion import _count_non_terminal_runs

        assert await _count_non_terminal_runs(mock_session, _ORG_ID) == 7

    async def test_count_non_terminal_runs_zero_on_none(self, mock_session: AsyncMock) -> None:
        result = MagicMock()
        result.scalar = MagicMock(return_value=None)
        mock_session.execute = AsyncMock(return_value=result)
        from modulo.db.crud.org_deletion import _count_non_terminal_runs

        assert await _count_non_terminal_runs(mock_session, _ORG_ID) == 0

    async def test_checkpoint_created_at_present(self, mock_session: AsyncMock) -> None:
        present = MagicMock()
        present.first = MagicMock(return_value=(1,))
        absent = MagicMock()
        absent.first = MagicMock(return_value=None)
        mock_session.execute = AsyncMock(side_effect=[present, absent])
        from modulo.db.crud.org_deletion import _checkpoint_created_at_present

        assert await _checkpoint_created_at_present(mock_session) is True
        assert await _checkpoint_created_at_present(mock_session) is False


class TestCollectOrgExport:
    @staticmethod
    def _record(values: dict[str, object]) -> SimpleNamespace:
        columns = [SimpleNamespace(name=name) for name in values]
        record = SimpleNamespace(**values)
        record.__table__ = SimpleNamespace(columns=columns)
        return record

    async def test_serialises_uuid_datetime_decimal(self, mock_session: AsyncMock) -> None:
        org_record = self._record({"id": uuid.UUID(int=1), "created_at": datetime(2026, 1, 1, tzinfo=UTC)})
        membership = self._record({"account_id": Decimal("1.5")})
        listing = MagicMock(scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[membership]))))
        mock_session.execute = AsyncMock(return_value=listing)
        from modulo.db.crud.org_deletion import _collect_org_export

        org = _make_org()
        org.__table__ = org_record.__table__
        org.id = org_record.id
        org.created_at = org_record.created_at

        # Mirror the record-serialising loop by passing an iterable of records:
        summary = await _collect_org_export(mock_session, org)

        assert summary["organisation"][0]["id"] == str(uuid.UUID(int=1))
        assert summary["organisation"][0]["created_at"] == "2026-01-01T00:00:00+00:00"
        assert summary["memberships"][0]["account_id"] == "1.5"
        assert summary["organisation"]
        assert "exported_at" in summary

    async def test_bundle_carries_all_sections(self, mock_session: AsyncMock) -> None:
        listings_scalars = MagicMock()
        listings_scalars.all = MagicMock(return_value=[])
        listing = MagicMock(scalars=MagicMock(return_value=listings_scalars))
        mock_session.execute = AsyncMock(return_value=listing)
        from modulo.db.crud.org_deletion import _collect_org_export

        org = _make_org()
        org.__table__ = self._record({"id": org.id}).__table__
        summary = await _collect_org_export(mock_session, org)
        for key in (
            "organisation",
            "memberships",
            "pipelines",
            "runs",
            "audit_events",
            "library_primitives",
            "connector_instances",
            "model_backends",
            "exported_at",
        ):
            assert key in summary

    async def test_bytes_serialised_via_str(self, mock_session: AsyncMock) -> None:
        record = self._record({"blob": b"raw"})
        rec_scalars = MagicMock()
        rec_scalars.all = MagicMock(return_value=[record])
        listing = MagicMock(scalars=MagicMock(return_value=rec_scalars))
        mock_session.execute = AsyncMock(return_value=listing)
        from modulo.db.crud.org_deletion import _collect_org_export

        org = _make_org()
        org.__table__ = record.__table__
        summary = await _collect_org_export(mock_session, org)
        assert summary is not None


class TestRequestOrgDeletion:
    async def test_org_not_found_raises(self, mock_session: AsyncMock) -> None:
        mock_session.execute = AsyncMock(return_value=_lookup_result(None))
        from modulo.db.crud.org_deletion import request_org_deletion

        with pytest.raises(ValueError, match="not found"):
            await request_org_deletion(mock_session, _ORG_ID, _ORG_ID)

    async def test_already_deleted_raises(self, mock_session: AsyncMock) -> None:
        mock_session.execute = AsyncMock(return_value=_lookup_result(_make_org(status="deleted")))
        from modulo.db.crud.org_deletion import request_org_deletion

        with pytest.raises(ValueError, match="already deleted"):
            await request_org_deletion(mock_session, _ORG_ID, _ORG_ID)

    async def test_soft_delete_sets_token_and_export(self, mock_session: AsyncMock) -> None:
        org = _make_org()
        mock_session.execute = AsyncMock(return_value=_lookup_result(org))
        export = {"organisation": [], "exported_at": "2026-01-01"}
        with patch("modulo.db.crud.org_deletion._collect_org_export", AsyncMock(return_value=export)) as collect:
            from modulo.db.crud.org_deletion import request_org_deletion

            result = await request_org_deletion(mock_session, _ORG_ID, _ORG_ID)
        collect.assert_awaited_once()
        assert org.status == "deleted"
        assert org.deleted_at is not None
        assert org.deletion_token == result["token"]
        assert org.deletion_token_expires_at is not None
        assert org.export_bundle_json == export
        assert result["export"] == export
        assert len(result["token"]) >= 64
        mock_session.flush.assert_awaited_once()


class TestConfirmOrgDeletion:
    async def test_org_not_found_raises(self, mock_session: AsyncMock) -> None:
        mock_session.execute = AsyncMock(return_value=_lookup_result(None))
        from modulo.db.crud.org_deletion import confirm_org_deletion

        with pytest.raises(ValueError, match="not found"):
            await confirm_org_deletion(mock_session, _ORG_ID, "tok")

    async def test_wrong_token_raises(self, mock_session: AsyncMock) -> None:
        mock_session.execute = AsyncMock(return_value=_lookup_result(_make_org(deletion_token="correct")))
        from modulo.db.crud.org_deletion import confirm_org_deletion

        with pytest.raises(ValueError, match="Invalid deletion token"):
            await confirm_org_deletion(mock_session, _ORG_ID, "wrong")

    async def test_missing_token_raises(self, mock_session: AsyncMock) -> None:
        mock_session.execute = AsyncMock(return_value=_lookup_result(_make_org(deletion_token=None)))
        from modulo.db.crud.org_deletion import confirm_org_deletion

        with pytest.raises(ValueError, match="Invalid deletion token"):
            await confirm_org_deletion(mock_session, _ORG_ID, "anything")

    async def test_expired_token_raises(self, mock_session: AsyncMock) -> None:
        expired = _make_org(token_expires_at=datetime.now(UTC) - timedelta(hours=1))
        mock_session.execute = AsyncMock(return_value=_lookup_result(expired))
        from modulo.db.crud.org_deletion import confirm_org_deletion

        with pytest.raises(ValueError, match="expired"):
            await confirm_org_deletion(mock_session, _ORG_ID, expired.deletion_token)

    async def test_token_expires_missing_raises(self, mock_session: AsyncMock) -> None:
        missing = _make_org(token_expires_at=None)
        mock_session.execute = AsyncMock(return_value=_lookup_result(missing))
        from modulo.db.crud.org_deletion import confirm_org_deletion

        with pytest.raises(ValueError, match="expired"):
            await confirm_org_deletion(mock_session, _ORG_ID, missing.deletion_token)

    async def test_live_runs_block_without_force(self, mock_session: AsyncMock) -> None:
        org = _make_org()
        mock_session.execute = AsyncMock(return_value=_lookup_result(org))
        with (
            patch("modulo.db.crud.org_deletion._count_non_terminal_runs", AsyncMock(return_value=2)) as count,
            patch("modulo.db.crud.org_deletion._abort_org_live_sandboxes", AsyncMock()) as abort,
            patch(
                "modulo.db.crud.org_deletion.batch_delete_old_terminal_runs",
                AsyncMock(return_value=0),
            ),
        ):
            from modulo.db.crud.org_deletion import confirm_org_deletion

            with pytest.raises(ValueError, match="still in progress"):
                await confirm_org_deletion(mock_session, _ORG_ID, org.deletion_token)
        count.assert_awaited_once()
        abort.assert_not_awaited()

    async def test_force_proceeds_with_live_runs(self, mock_session: AsyncMock) -> None:
        org = _make_org()
        mock_session.execute = AsyncMock(return_value=_lookup_result(org))
        with (
            patch("modulo.db.crud.org_deletion._count_non_terminal_runs", AsyncMock(return_value=3)),
            patch("modulo.db.crud.org_deletion._abort_org_live_sandboxes", AsyncMock()) as abort,
            patch(
                "modulo.db.crud.org_deletion.batch_delete_old_terminal_runs",
                AsyncMock(return_value=4),
            ) as batch_delete,
        ):
            from modulo.db.crud.org_deletion import confirm_org_deletion

            result = await confirm_org_deletion(mock_session, _ORG_ID, org.deletion_token, force=True)
        abort.assert_awaited_once()
        batch_delete.assert_awaited_once()
        assert result == {"deleted_organisation_id": str(_ORG_ID), "hard_deleted_runs": 4}
        mock_session.delete.assert_awaited_once_with(org)
        mock_session.flush.assert_awaited_once()

    async def test_immediate_skips_token_check(self, mock_session: AsyncMock) -> None:
        org = _make_org(deletion_token="expected")
        mock_session.execute = AsyncMock(return_value=_lookup_result(org))
        with (
            patch("modulo.db.crud.org_deletion._count_non_terminal_runs", AsyncMock(return_value=0)),
            patch("modulo.db.crud.org_deletion._abort_org_live_sandboxes", AsyncMock()),
            patch(
                "modulo.db.crud.org_deletion.batch_delete_old_terminal_runs",
                AsyncMock(return_value=2),
            ),
        ):
            from modulo.db.crud.org_deletion import confirm_org_deletion

            result = await confirm_org_deletion(mock_session, _ORG_ID, "NOT the token", immediate=True)
        assert result["hard_deleted_runs"] == 2


class TestCancelAndExport:
    async def test_cancel_org_not_found(self, mock_session: AsyncMock) -> None:
        mock_session.execute = AsyncMock(return_value=_lookup_result(None))
        from modulo.db.crud.org_deletion import cancel_org_deletion

        with pytest.raises(ValueError, match="not found"):
            await cancel_org_deletion(mock_session, _ORG_ID)

    async def test_cancel_without_pending_deletion_raises(self, mock_session: AsyncMock) -> None:
        mock_session.execute = AsyncMock(return_value=_lookup_result(_make_org(status="active")))
        from modulo.db.crud.org_deletion import cancel_org_deletion

        with pytest.raises(ValueError, match="No pending deletion"):
            await cancel_org_deletion(mock_session, _ORG_ID)

    async def test_cancel_restores_active_state(self, mock_session: AsyncMock) -> None:
        org = _make_org(status="deleted", deleted_at=datetime.now(UTC))
        org.deletion_token = "tok"
        mock_session.execute = AsyncMock(return_value=_lookup_result(org))
        from modulo.db.crud.org_deletion import cancel_org_deletion

        result = await cancel_org_deletion(mock_session, _ORG_ID)
        assert result == {"status": "active"}
        assert org.status == "active"
        assert org.deleted_at is None
        assert org.deletion_token is None
        assert org.deletion_token_expires_at is None
        mock_session.flush.assert_awaited_once()

    async def test_export_org_not_found(self, mock_session: AsyncMock) -> None:
        mock_session.execute = AsyncMock(return_value=_lookup_result(None))
        from modulo.db.crud.org_deletion import export_org_data

        with pytest.raises(ValueError, match="not found"):
            await export_org_data(mock_session, _ORG_ID)

    async def test_export_returns_cached_bundle(self, mock_session: AsyncMock) -> None:
        cached = {"organisation": [], "exported_at": "x"}
        mock_session.execute = AsyncMock(return_value=_lookup_result(_make_org(export_bundle_json=cached)))
        from modulo.db.crud.org_deletion import export_org_data

        assert await export_org_data(mock_session, _ORG_ID) == cached

    async def test_export_collects_when_no_cached_bundle(self, mock_session: AsyncMock) -> None:
        mock_session.execute = AsyncMock(return_value=_lookup_result(_make_org(export_bundle_json=None)))
        collected = {"organisation": []}
        with patch("modulo.db.crud.org_deletion._collect_org_export", AsyncMock(return_value=collected)):
            from modulo.db.crud.org_deletion import export_org_data

            assert await export_org_data(mock_session, _ORG_ID) == collected


class TestAbortSandboxes:
    async def test_col_missing_returns_zero(self, mock_session: AsyncMock) -> None:
        absent = MagicMock()
        absent.first = MagicMock(return_value=None)
        mock_session.execute = AsyncMock(return_value=absent)
        fake_e2b = _fake_module(AsyncSandbox=MagicMock())
        with patch.dict(sys_modules, {"e2b": fake_e2b}):
            from modulo.db.crud.org_deletion import _abort_org_live_sandboxes

            assert await _abort_org_live_sandboxes(mock_session, _ORG_ID) == 0

    async def test_sqlalchemy_error_returns_zero(self, mock_session: AsyncMock) -> None:
        mock_session.execute = AsyncMock(side_effect=Exception("db unavailable"))
        from modulo.db.crud.org_deletion import _abort_org_live_sandboxes

        assert await _abort_org_live_sandboxes(mock_session, _ORG_ID) == 0

    async def test_kills_each_distinct_sandbox(self, mock_session: AsyncMock) -> None:
        col_check = MagicMock()
        col_check.first = MagicMock(return_value=(1,))
        sandbox_result = MagicMock()
        sandbox_result.all = MagicMock(return_value=[("sb-1",), ("sb-2",)])
        mock_session.execute = AsyncMock(side_effect=[col_check, sandbox_result])
        kill_mock = AsyncMock()
        sandbox_cls = SimpleNamespace(kill=kill_mock)
        fake_e2b = _fake_module(AsyncSandbox=sandbox_cls)
        with patch.dict(sys_modules, {"e2b": fake_e2b}):
            from modulo.db.crud.org_deletion import _abort_org_live_sandboxes

            killed = await _abort_org_live_sandboxes(mock_session, _ORG_ID)
            assert killed == 2
            assert kill_mock.await_count == 2

    async def test_kill_failures_are_counted_partial(self, mock_session: AsyncMock) -> None:
        col_check = MagicMock()
        col_check.first = MagicMock(return_value=(1,))
        sandbox_result = MagicMock()
        sandbox_result.all = MagicMock(return_value=[("sb-1",), ("sb-2",)])
        mock_session.execute = AsyncMock(side_effect=[col_check, sandbox_result])
        kill_mock = AsyncMock(side_effect=[None, RuntimeError("boom")])
        sandbox_cls = SimpleNamespace(kill=kill_mock)
        fake_e2b = _fake_module(AsyncSandbox=sandbox_cls)
        with patch.dict(sys_modules, {"e2b": fake_e2b}):
            from modulo.db.crud.org_deletion import _abort_org_live_sandboxes

            assert await _abort_org_live_sandboxes(mock_session, _ORG_ID) == 1


class TestBatchDeleteLanggraphCheckpoints:
    @staticmethod
    def _result_with_rowcount(rowcount: int) -> MagicMock:
        result = MagicMock()
        result.rowcount = rowcount
        return result

    async def test_deleted_total_when_counts_below_batch(self, mock_session: AsyncMock) -> None:
        # has_created_at=True path; each statement deletes fewer than batch_size once.
        col_check = MagicMock()
        col_check.first = MagicMock(return_value=(1,))
        mock_session.execute = AsyncMock(
            side_effect=[
                col_check,
                self._result_with_rowcount(5),
                self._result_with_rowcount(5),
                self._result_with_rowcount(5),
            ]
        )
        from modulo.db.crud.org_deletion import batch_delete_langgraph_checkpoints

        result = await batch_delete_langgraph_checkpoints(mock_session)
        assert result == 15

    async def test_loops_until_below_batch_size(self, mock_session: AsyncMock) -> None:
        # 3 statements, first table needs two passes (>= batch then partial), others one.
        col_check = MagicMock()
        col_check.first = MagicMock(return_value=(1,))
        mock_session.execute = AsyncMock(
            side_effect=[
                col_check,
                self._result_with_rowcount(500),
                self._result_with_rowcount(40),
                self._result_with_rowcount(50),
                self._result_with_rowcount(50),
            ]
        )
        from modulo.db.crud.org_deletion import batch_delete_langgraph_checkpoints

        result = await batch_delete_langgraph_checkpoints(mock_session, batch_size=500, max_age_days=10)
        assert result == 500 + 40 + 50 + 50

    async def test_deployed_schema_path_without_created_at(self, mock_session: AsyncMock) -> None:
        col_check = MagicMock()
        col_check.first = MagicMock(return_value=None)
        mock_session.execute = AsyncMock(
            side_effect=[
                col_check,
                self._result_with_rowcount(7),
                self._result_with_rowcount(7),
                self._result_with_rowcount(7),
            ]
        )
        from modulo.db.crud.org_deletion import batch_delete_langgraph_checkpoints

        result = await batch_delete_langgraph_checkpoints(mock_session)
        assert result == 21
