"""Unit tests for ModuloPostgresSaver reconnect-on-stale-connection.

A long-running pipeline can idle the DB connection until the server closes it;
every checkpoint operation (aput, aget_tuple, alist, setup, aput_writes) must
transparently reconnect before opening a cursor instead of failing the whole
run with ``psycopg.OperationalError: the connection is closed``. The
``aput_writes`` retry loop remains as defense-in-depth; ``aput`` now carries
the SAME reconnect-retry (a connection drop on the checkpoint write path is
the costliest single failure class).
"""

import asyncio
import uuid
from typing import Self
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from psycopg.rows import dict_row

from modulo.core.pipeline_engine import modulo_saver as ms
from modulo.core.pipeline_engine.modulo_saver import ModuloPostgresSaver


class OperationalError(Exception):
    """Stand-in for psycopg.errors.OperationalError — matched by type name."""


class _FakeConn:
    def __init__(self) -> None:
        self.closed = False
        self.broken = False

    async def close(self) -> None:
        self.closed = True


class _Cursor:
    """Async cursor that fails its first ``fail_times`` execute() calls.

    ``conn`` is optional: when provided, an OperationalError raised by the
    cursor marks the connection ``broken`` (mirroring psycopg, which breaks an
    in-use connection on a mid-execute failure) so ``_reconnect``'s stale
    double-check sees it.
    """

    def __init__(
        self,
        error: Exception | None = None,
        fail_times: int = 0,
        rows: list[dict[str, object]] | None = None,
        conn: object | None = None,
    ) -> None:
        self.error = error
        self.fail_times = fail_times
        self.call_count = 0
        self.executed: list[tuple[str, object]] = []
        self.rows = rows or []
        self.conn = conn

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    async def execute(self, sql: str, params: object | None = None, **kwargs: object) -> None:
        self.call_count += 1
        self.executed.append((sql, params))
        if self.error is not None and self.call_count <= self.fail_times:
            if self.conn is not None and type(self.error).__name__ == "OperationalError":
                self.conn.broken = True
            raise self.error

    async def __aiter__(self):
        for row in self.rows:
            yield row


def _make_saver(
    error: Exception | None = None,
    fail_times: int = 0,
    *,
    patch_cursor: bool = True,
) -> ModuloPostgresSaver:
    saver = ModuloPostgresSaver(
        _FakeConn(),
        organisation_id=uuid.uuid4(),
        fernet_key=None,
        conn_string="postgresql://fake:fake@localhost:5432/fake",
    )
    if patch_cursor:
        saver._cursor = MagicMock(return_value=_Cursor(error=error, fail_times=fail_times, conn=saver.conn))
    saver.serde = MagicMock()
    saver.serde.dumps_typed = MagicMock(return_value=("json", b"blob"))
    return saver


def _config() -> dict[str, dict[str, str]]:
    return {"configurable": {"thread_id": "t1", "checkpoint_ns": "", "checkpoint_id": "ckp-1"}}


def _checkpoint() -> dict[str, object]:
    return {
        "v": 1,
        "ts": "2026-08-01T00:00:00Z",
        "id": "ckp-1",
        "channel_values": {},
        "channel_versions": {},
        "versions_seen": {},
        "pending_sends": [],
    }


def _metadata() -> dict[str, object]:
    return {"source": "test"}


async def test_retries_once_on_operational_error_then_succeeds():
    saver = _make_saver(
        error=OperationalError("consuming input failed: server closed the connection unexpectedly"),
        fail_times=1,
    )
    original_conn = saver.conn
    fake_new_conn = _FakeConn()

    with patch("psycopg.AsyncConnection.connect", new_callable=AsyncMock) as mock_connect:
        mock_connect.return_value = fake_new_conn
        await saver.aput_writes(_config(), [("channel1", {"data": "x"})], "task-1")

    assert mock_connect.await_count == 1
    call = mock_connect.await_args
    assert call.args[0] == "postgresql://fake:fake@localhost:5432/fake"
    assert call.kwargs["autocommit"] is True
    assert call.kwargs["prepare_threshold"] == 0
    assert call.kwargs["row_factory"] is dict_row
    assert original_conn.closed is True
    assert saver.conn is fake_new_conn


async def test_non_operational_error_is_not_retried():
    saver = _make_saver(error=RuntimeError("boom"), fail_times=999)

    with (
        patch("psycopg.AsyncConnection.connect", new_callable=AsyncMock) as mock_connect,
        pytest.raises(RuntimeError),
    ):
        await saver.aput_writes(_config(), [("channel1", {"data": "x"})], "task-1")

    mock_connect.assert_not_awaited()
    assert saver.conn.closed is False


async def test_second_operational_error_is_not_retried():
    saver = _make_saver(error=OperationalError("drop"), fail_times=999)

    with (
        patch("psycopg.AsyncConnection.connect", new_callable=AsyncMock) as mock_connect,
        pytest.raises(OperationalError),
    ):
        await saver.aput_writes(_config(), [("channel1", {"data": "x"})], "task-1")

    assert mock_connect.await_count == 1


async def test_aput_reconnects_when_connection_closed():
    saver = _make_saver(patch_cursor=False)
    saver.conn.closed = True
    original_conn = saver.conn
    fake_new_conn = _FakeConn()

    with (
        patch("psycopg.AsyncConnection.connect", new_callable=AsyncMock) as mock_connect,
        patch(
            "langgraph.checkpoint.postgres.aio.AsyncPostgresSaver._cursor",
            return_value=_Cursor(),
        ),
    ):
        mock_connect.return_value = fake_new_conn
        await saver.aput(_config(), _checkpoint(), _metadata())

    assert mock_connect.await_count == 1
    assert original_conn.closed is True
    assert saver.conn is fake_new_conn


async def test_aget_tuple_reconnects_when_connection_broken():
    saver = _make_saver(patch_cursor=False)
    saver.conn.broken = True
    original_conn = saver.conn
    fake_new_conn = _FakeConn()

    with (
        patch("psycopg.AsyncConnection.connect", new_callable=AsyncMock) as mock_connect,
        patch(
            "langgraph.checkpoint.postgres.aio.AsyncPostgresSaver._cursor",
            return_value=_Cursor(),
        ),
    ):
        mock_connect.return_value = fake_new_conn
        result = await saver.aget_tuple(_config())

    assert mock_connect.await_count == 1
    assert original_conn.closed is True
    assert saver.conn is fake_new_conn
    assert result is None


async def test_no_reconnect_when_connection_healthy():
    saver = _make_saver(patch_cursor=False)
    original_conn = saver.conn

    with (
        patch("psycopg.AsyncConnection.connect", new_callable=AsyncMock) as mock_connect,
        patch(
            "langgraph.checkpoint.postgres.aio.AsyncPostgresSaver._cursor",
            return_value=_Cursor(),
        ),
    ):
        await saver.aput(_config(), _checkpoint(), _metadata())

    mock_connect.assert_not_awaited()
    assert saver.conn is original_conn


async def test_no_reconnect_without_conn_string():
    saver = ModuloPostgresSaver(
        _FakeConn(),
        organisation_id=uuid.uuid4(),
        fernet_key=None,
        conn_string=None,
    )
    saver.conn.closed = True
    saver.serde = MagicMock()
    saver.serde.dumps_typed = MagicMock(return_value=("json", b"blob"))

    with (
        patch("psycopg.AsyncConnection.connect", new_callable=AsyncMock) as mock_connect,
        patch(
            "langgraph.checkpoint.postgres.aio.AsyncPostgresSaver._cursor",
            return_value=_Cursor(error=OperationalError("the connection is closed"), fail_times=1),
        ),
        pytest.raises(OperationalError),
    ):
        await saver.aput(_config(), _checkpoint(), _metadata())

    mock_connect.assert_not_awaited()


# ---------------------------------------------------------------------------
# aput retry (P3): a connection-drop OperationalError on the checkpoint write
# path is the costliest single failure class — aput now reconnects + retries
# exactly like aput_writes, bounded by _RECONNECT_TIMEOUT_SECONDS.
# ---------------------------------------------------------------------------


async def test_aput_retries_mid_execute_on_operational_error(caplog):
    saver = _make_saver(error=OperationalError("server closed the connection"), fail_times=1)
    original_conn = saver.conn
    fake_new_conn = _FakeConn()

    with patch("psycopg.AsyncConnection.connect", new_callable=AsyncMock) as mock_connect:
        mock_connect.return_value = fake_new_conn
        result = await saver.aput(_config(), _checkpoint(), _metadata())

    assert saver._cursor.return_value.call_count == 2
    assert mock_connect.await_count == 1
    assert result["configurable"]["checkpoint_id"] == "ckp-1"
    assert original_conn.closed is True
    assert saver.conn is fake_new_conn
    assert "checkpoint.aput_retry" in caplog.text


async def test_aput_non_operational_error_not_retried():
    saver = _make_saver(error=RuntimeError("boom"), fail_times=999)

    with (
        patch("psycopg.AsyncConnection.connect", new_callable=AsyncMock) as mock_connect,
        pytest.raises(RuntimeError),
    ):
        await saver.aput(_config(), _checkpoint(), _metadata())

    mock_connect.assert_not_awaited()
    assert saver._cursor.return_value.call_count == 1


async def test_aput_second_operational_error_not_retried():
    saver = _make_saver(error=OperationalError("drop"), fail_times=999)

    with (
        patch("psycopg.AsyncConnection.connect", new_callable=AsyncMock) as mock_connect,
        pytest.raises(OperationalError),
    ):
        await saver.aput(_config(), _checkpoint(), _metadata())

    assert mock_connect.await_count == 1
    assert saver._cursor.return_value.call_count == 2


async def test_reconnect_timeout_bounds_hang(monkeypatch):
    """A reconnect that never resolves must not hang — bounded by
    _RECONNECT_TIMEOUT_SECONDS, and the ORIGINAL OperationalError (never a
    bare timeout) propagates."""
    monkeypatch.setattr(ms, "_RECONNECT_TIMEOUT_SECONDS", 0.01)
    saver = _make_saver(error=OperationalError("drop"), fail_times=999)
    never = asyncio.get_running_loop().create_future()

    with patch("psycopg.AsyncConnection.connect", new_callable=AsyncMock) as mock_connect:
        mock_connect.return_value = never
        with pytest.raises(OperationalError):
            await saver.aput(_config(), _checkpoint(), _metadata())

    mock_connect.assert_awaited_once()


async def test_concurrent_reconnect_serialized_by_lock():
    """Two concurrent aput on a stale connection perform ONE reconnect — the
    lock + stale double-check inside _reconnect makes the second waiter skip."""
    saver = _make_saver(patch_cursor=False)
    saver.conn.closed = True
    original_conn = saver.conn
    fake_new_conn = _FakeConn()

    with (
        patch("psycopg.AsyncConnection.connect", new_callable=AsyncMock) as mock_connect,
        patch(
            "langgraph.checkpoint.postgres.aio.AsyncPostgresSaver._cursor",
            return_value=_Cursor(),
        ),
    ):
        mock_connect.return_value = fake_new_conn
        await asyncio.gather(
            saver.aput(_config(), _checkpoint(), _metadata()),
            saver.aput(_config(), _checkpoint(), _metadata()),
        )

    assert mock_connect.await_count == 1
    assert original_conn.closed is True
    assert saver.conn is fake_new_conn


# ---------------------------------------------------------------------------
# Sync paths (put / put_writes → _run_sync). These MUST be plain `def` tests —
# asyncio_mode=auto would run an async def in a loop and _run_sync raises.
# ---------------------------------------------------------------------------


def test_put_sync_retries_on_operational_error():
    # AsyncPostgresSaver.__init__ requires a running loop, so the saver is
    # CONSTRUCTED inside asyncio.run; the sync `put` is then called OUTSIDE any
    # loop so _run_sync takes its asyncio.run branch.
    async def _construct():
        return _make_saver(error=OperationalError("drop"), fail_times=1)

    saver = asyncio.run(_construct())
    fake_new_conn = _FakeConn()

    with patch("psycopg.AsyncConnection.connect", new_callable=AsyncMock) as mock_connect:
        mock_connect.return_value = fake_new_conn
        saver.put(_config(), _checkpoint(), _metadata())

    assert mock_connect.await_count == 1
    assert saver.conn is fake_new_conn


def test_put_writes_sync_retries_on_operational_error():
    async def _construct():
        return _make_saver(error=OperationalError("drop"), fail_times=1)

    saver = asyncio.run(_construct())
    fake_new_conn = _FakeConn()

    with patch("psycopg.AsyncConnection.connect", new_callable=AsyncMock) as mock_connect:
        mock_connect.return_value = fake_new_conn
        saver.put_writes(_config(), [("channel1", {"data": "x"})], "task-1")

    assert mock_connect.await_count == 1
    assert saver.conn is fake_new_conn
