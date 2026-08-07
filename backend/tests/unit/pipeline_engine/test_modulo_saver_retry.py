"""Unit tests for ModuloPostgresSaver aput_writes retry-on-OperationalError.

The saver reconnects once and retries the write when the DB connection drops
mid-checkpoint (psycopg.OperationalError), so transient connection failures
self-heal instead of failing the whole pipeline run.
"""

import uuid
from typing import Self
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from psycopg.rows import dict_row

from modulo.core.pipeline_engine.modulo_saver import ModuloPostgresSaver


class OperationalError(Exception):
    """Stand-in for psycopg.errors.OperationalError — matched by type name."""


class _FakeConn:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class _Cursor:
    """Async cursor that fails its first ``fail_times`` execute() calls."""

    def __init__(self, error: Exception | None = None, fail_times: int = 0) -> None:
        self.error = error
        self.fail_times = fail_times
        self.call_count = 0
        self.executed: list[tuple[str, object]] = []

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    async def execute(self, sql: str, params: object | None = None, **kwargs: object) -> None:
        self.call_count += 1
        self.executed.append((sql, params))
        if self.error is not None and self.call_count <= self.fail_times:
            raise self.error


def _make_saver(error: Exception | None = None, fail_times: int = 0) -> ModuloPostgresSaver:
    saver = ModuloPostgresSaver(
        _FakeConn(),
        organisation_id=uuid.uuid4(),
        fernet_key=None,
        conn_string="postgresql://fake:fake@localhost:5432/fake",
    )
    saver._cursor = MagicMock(return_value=_Cursor(error=error, fail_times=fail_times))
    saver.serde = MagicMock()
    saver.serde.dumps_typed = MagicMock(return_value=("json", b"blob"))
    return saver


def _config() -> dict[str, dict[str, str]]:
    return {"configurable": {"thread_id": "t1", "checkpoint_ns": "", "checkpoint_id": "ckp-1"}}


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
