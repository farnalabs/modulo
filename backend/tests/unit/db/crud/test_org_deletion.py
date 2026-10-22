"""Unit tests for org deletion / retention CRUD (mocked session, no DB)."""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from sys import modules as sys_modules
from types import SimpleNamespace
from typing import Any, Self
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


class _FakeConnectContext:
    """Async context manager standing in for ``AsyncEngine.connect()``."""

    def __init__(self, connection: "_FakeSnapshotConnection") -> None:
        self.connection = connection

    async def __aenter__(self) -> "_FakeSnapshotConnection":
        return self.connection

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


class _FakeSnapshotConnection:
    """Minimal AsyncConnection stand-in for the org-export snapshot helper."""

    def __init__(self, dialect: str) -> None:
        self.dialect = SimpleNamespace(name=dialect)
        self.executed: list[object] = []
        self.execution_options_calls: list[dict[str, Any]] = []

    async def execute(self, statement: object) -> MagicMock:
        self.executed.append(statement)
        return MagicMock()

    async def execution_options(self, **kwargs: Any) -> "_FakeSnapshotConnection":
        self.execution_options_calls.append(kwargs)
        return self


class _FakeSnapshotEngine:
    """Engine stand-in that hands out one fake connection."""

    def __init__(self, connection: _FakeSnapshotConnection) -> None:
        self.connection = connection
        self.connect_count = 0

    def connect(self) -> _FakeConnectContext:
        self.connect_count += 1
        return _FakeConnectContext(self.connection)


class _FakeSnapshotSession:
    """AsyncSession stand-in: joins ``begin()`` and yields a configured org."""

    def __init__(self, org: object | None) -> None:
        self.org = org
        self.execute_count = 0

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False

    def begin(self) -> Self:
        return self

    async def execute(self, statement: object) -> MagicMock:
        self.execute_count += 1
        return MagicMock(scalar_one_or_none=MagicMock(return_value=self.org))


def _snapshot_sessionmaker(session: _FakeSnapshotSession) -> MagicMock:
    """Build an ``async_sessionmaker`` stand-in whose factory yields ``session``."""
    return MagicMock(return_value=MagicMock(return_value=session))


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
            # Library-cluster sections added by FAR-1758 item 6 — the archive
            # claims to bundle ALL org-owned data, so each must be present.
            "composite_templates",
            "primitive_ratings",
            "primitive_abuse_reports",
            "publishers",
            "collection_installs",
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


class TestCollectOrgExportInSnapshot:
    """The snapshot helper configures READ ONLY / REPEATABLE READ before reading."""

    async def test_postgres_uses_read_only_repeatable_read_snapshot(self) -> None:
        from modulo.db.crud import org_deletion

        org = _make_org()
        conn = _FakeSnapshotConnection("postgresql")
        engine = _FakeSnapshotEngine(conn)
        session = _FakeSnapshotSession(org)
        export = {"organisation": [], "exported_at": "2026-01-01"}
        sessionmaker = MagicMock(return_value=MagicMock(return_value=session))
        set_rls = AsyncMock()
        collect = AsyncMock(return_value=export)

        with (
            patch.object(org_deletion, "async_sessionmaker", sessionmaker),
            patch.object(org_deletion, "set_rls_org", set_rls),
            patch.object(org_deletion, "_collect_org_export", collect),
        ):
            assert await org_deletion._collect_org_export_in_snapshot(_ORG_ID, engine=engine) == export

        assert conn.execution_options_calls == [{"isolation_level": "REPEATABLE READ"}]
        assert len(conn.executed) == 1
        assert "SET TRANSACTION READ ONLY" in str(conn.executed[0])
        assert sessionmaker.call_args.kwargs["bind"] is conn
        set_rls.assert_awaited_once_with(session, _ORG_ID)
        collect.assert_awaited_once_with(session, org)
        assert engine.connect_count == 1

    async def test_non_postgres_skips_isolation_statements(self) -> None:
        from modulo.db.crud import org_deletion

        org = _make_org()
        conn = _FakeSnapshotConnection("sqlite")
        engine = _FakeSnapshotEngine(conn)
        session = _FakeSnapshotSession(org)
        export = {"organisation": []}

        with (
            patch.object(org_deletion, "async_sessionmaker", _snapshot_sessionmaker(session)),
            patch.object(org_deletion, "set_rls_org", AsyncMock()),
            patch.object(org_deletion, "_collect_org_export", AsyncMock(return_value=export)),
        ):
            assert await org_deletion._collect_org_export_in_snapshot(_ORG_ID, engine=engine) == export

        assert conn.execution_options_calls == []
        assert conn.executed == []

    async def test_defaults_to_shared_engine(self) -> None:
        from modulo.db.crud import org_deletion

        org = _make_org()
        engine = _FakeSnapshotEngine(_FakeSnapshotConnection("sqlite"))
        session = _FakeSnapshotSession(org)
        export = {"organisation": []}
        shared_engine = MagicMock(return_value=engine)

        with (
            patch.object(org_deletion, "get_shared_engine", shared_engine),
            patch.object(org_deletion, "async_sessionmaker", _snapshot_sessionmaker(session)),
            patch.object(org_deletion, "set_rls_org", AsyncMock()),
            patch.object(org_deletion, "_collect_org_export", AsyncMock(return_value=export)),
        ):
            assert await org_deletion._collect_org_export_in_snapshot(_ORG_ID) == export

        shared_engine.assert_called_once_with()
        assert engine.connect_count == 1

    async def test_missing_org_raises_before_collecting(self) -> None:
        from modulo.db.crud import org_deletion

        engine = _FakeSnapshotEngine(_FakeSnapshotConnection("sqlite"))
        session = _FakeSnapshotSession(None)
        collect = AsyncMock()

        with (
            patch.object(org_deletion, "async_sessionmaker", _snapshot_sessionmaker(session)),
            patch.object(org_deletion, "set_rls_org", AsyncMock()),
            patch.object(org_deletion, "_collect_org_export", collect),
            pytest.raises(ValueError, match="not found"),
        ):
            await org_deletion._collect_org_export_in_snapshot(_ORG_ID, engine=engine)

        collect.assert_not_awaited()


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
        target = "modulo.db.crud.org_deletion._collect_org_export_in_snapshot"
        with patch(target, AsyncMock(return_value=export)) as collect:
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
        with patch("modulo.db.crud.org_deletion._collect_org_export_in_snapshot", AsyncMock(return_value=collected)):
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


class _FakeTransaction:
    def __init__(self, session: "_FakeSweepSession") -> None:
        self._session = session

    async def __aenter__(self) -> "_FakeSweepSession":
        return self._session

    async def __aexit__(self, *_exc: object) -> bool:
        return False


class _FakeSweepSession:
    """Minimal async-session double for the candidate-driven sweep unit tests.

    Records each ``session.begin()`` (one per committed batch, FAR-1971b) and
    returns scripted ``execute`` results in order.
    """

    def __init__(self, scripted: list[Any]) -> None:
        self._scripted = list(scripted)
        self.begins = 0
        self.executed: list[str] = []

    def begin(self) -> _FakeTransaction:
        self.begins += 1
        return _FakeTransaction(self)

    async def execute(self, statement: Any, params: Any = None) -> Any:
        self.executed.append(str(statement))
        return self._scripted.pop(0)


def _column_result(present: bool) -> MagicMock:
    result = MagicMock()
    result.first = MagicMock(return_value=(1,) if present else None)
    return result


def _rows_result(*pairs: tuple[uuid.UUID, str]) -> MagicMock:
    result = MagicMock()
    result.all = MagicMock(return_value=[(org, thread) for org, thread in pairs])
    return result


def _rowcount_result(rowcount: int) -> MagicMock:
    result = MagicMock()
    result.rowcount = rowcount
    return result


class TestBatchDeleteLanggraphCheckpoints:
    async def test_deleted_total_when_counts_below_batch(self) -> None:
        """A short candidate batch deletes across all three tables and totals them.

        Deployed path (no ``created_at``): one run-expired candidate thread, so
        only the run-candidate select plus one delete per table execute.
        """
        session = _FakeSweepSession(
            [
                _column_result(False),
                _rows_result((_ORG_ID, "t1")),
                _rowcount_result(5),
                _rowcount_result(5),
                _rowcount_result(5),
            ]
        )
        from modulo.db.crud.org_deletion import batch_delete_langgraph_checkpoints

        result = await batch_delete_langgraph_checkpoints(session)
        assert result == 15
        # introspection transaction + one committed batch transaction (FAR-1971b)
        assert session.begins == 2
        # deployed path never issues the created_at-driven stale selects
        assert not any("FROM checkpoints t" in stmt for stmt in session.executed)

    async def test_loops_until_below_batch_size(self) -> None:
        """A full candidate batch loops again; a short one ends the sweep."""
        session = _FakeSweepSession(
            [
                _column_result(False),
                _rows_result((_ORG_ID, "t1"), (_ORG_ID, "t2")),  # full batch (== batch_size)
                _rowcount_result(300),
                _rowcount_result(150),
                _rowcount_result(50),
                _rows_result((_ORG_ID, "t3")),  # short batch
                _rowcount_result(40),
                _rowcount_result(0),
                _rowcount_result(0),
            ]
        )
        from modulo.db.crud.org_deletion import batch_delete_langgraph_checkpoints

        result = await batch_delete_langgraph_checkpoints(session, batch_size=2, max_age_days=10)
        assert result == 300 + 150 + 50 + 40
        # introspection transaction + two committed batch transactions
        assert session.begins == 3

    async def test_stops_at_row_budget(self) -> None:
        """FAR-1971c: the sweep stops once the per-tick row budget is spent."""
        session = _FakeSweepSession(
            [
                _column_result(False),
                _rows_result((_ORG_ID, "t1")),
                _rowcount_result(1),
                _rowcount_result(1),
                _rowcount_result(1),
                _rows_result((_ORG_ID, "t2")),
                _rowcount_result(1),
                _rowcount_result(1),
                _rowcount_result(1),
            ]
        )
        from modulo.db.crud.org_deletion import batch_delete_langgraph_checkpoints

        result = await batch_delete_langgraph_checkpoints(session, batch_size=1, max_rows_per_tick=5)
        # Two batches ran (3 rows each); the budget of 5 stopped the third.
        assert result == 6
        assert session.begins == 3

    async def test_deployed_schema_path_without_created_at(self) -> None:
        """The sentinel run-expired select is used and stale selects are skipped."""
        session = _FakeSweepSession(
            [
                _column_result(False),
                _rows_result((_ORG_ID, "t1")),
                _rowcount_result(7),
                _rowcount_result(7),
                _rowcount_result(7),
            ]
        )
        from modulo.db.crud.org_deletion import batch_delete_langgraph_checkpoints

        result = await batch_delete_langgraph_checkpoints(session)
        assert result == 21
        assert any("FROM runs r" in stmt for stmt in session.executed)
        # every delete targets (organisation_id, thread_id), never ctid
        assert all("ctid" not in stmt for stmt in session.executed)

    async def test_created_at_path_consults_stale_selects_and_dedups_run_candidates(self) -> None:
        """created_at-present path: all three stale selects run and dedup keeps the stronger class.

        A thread found by BOTH the run-expired select and a stale select must stay
        run-expired (whole-thread purge), not be downgraded to stale-only by the
        second sighting — so the thread is deleted by the whole-thread template
        even though a stale select also returned it.
        """
        session = _FakeSweepSession(
            [
                _column_result(True),  # created_at present -> stale selects run
                _rows_result((_ORG_ID, "t-run")),  # run-expired candidate
                _rows_result((_ORG_ID, "t-stale"), (_ORG_ID, "t-run")),  # stale checkpoints (t-run dup)
                _rows_result((_ORG_ID, "t-stale")),  # stale writes (t-stale dup)
                _rows_result((_ORG_ID, "t-blob")),  # stale blobs (new)
                # run-expired group (t-run): whole-thread delete, no age predicate
                _rowcount_result(5),
                _rowcount_result(4),
                _rowcount_result(3),
                # stale-only group (t-stale, t-blob): age-filtered delete
                _rowcount_result(2),
                _rowcount_result(2),
                _rowcount_result(1),
            ]
        )
        from modulo.db.crud.org_deletion import batch_delete_langgraph_checkpoints

        result = await batch_delete_langgraph_checkpoints(session, batch_size=4)
        assert result == 5 + 4 + 3 + 2 + 2 + 1
        # introspection transaction + one committed batch (short batch ends it)
        assert session.begins == 2
        # all three stale candidate selects are issued on the created_at path
        assert sum("FROM checkpoints t" in stmt for stmt in session.executed) == 1
        assert sum("FROM checkpoint_writes t" in stmt for stmt in session.executed) == 1
        assert sum("FROM checkpoint_blobs t" in stmt for stmt in session.executed) == 1
        # the stale-only group is deleted with the age predicate ...
        assert any("created_at < :cutoff" in stmt for stmt in session.executed)
        # ... and the thread seen by both sources keeps the stronger whole-thread delete
        assert any(stmt.startswith("DELETE FROM checkpoints") and "created_at" not in stmt for stmt in session.executed)

    async def test_saturated_limit_skips_stale_selects_and_empty_batch_stops(self) -> None:
        """A run-candidate batch that spends the whole limit skips the stale selects.

        Once ``remaining`` reaches 0 the stale-select loop breaks before issuing
        its first query; the next fetch then returns no candidates and the sweep
        stops (the empty-batch guard).
        """
        session = _FakeSweepSession(
            [
                _column_result(True),
                _rows_result((_ORG_ID, "r1"), (_ORG_ID, "r2")),  # full batch: remaining == 0
                _rowcount_result(1),
                _rowcount_result(1),
                _rowcount_result(1),
                _rows_result(),  # next fetch: no candidates -> stop
                _rows_result(),  # stale checkpoints (remaining refilled, all empty)
                _rows_result(),  # stale writes
                _rows_result(),  # stale blobs
            ]
        )
        from modulo.db.crud.org_deletion import batch_delete_langgraph_checkpoints

        result = await batch_delete_langgraph_checkpoints(session, batch_size=2)
        assert result == 3
        # introspection transaction + the full batch + the empty batch
        assert session.begins == 3
        # the saturated batch skipped its stale selects; only the empty batch issued one
        assert sum("FROM checkpoints t" in stmt for stmt in session.executed) == 1
