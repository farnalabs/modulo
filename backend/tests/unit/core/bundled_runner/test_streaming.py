"""Unit tests for the D4 streaming-exec primitive (FAR-590).

Covers: the ABC default (collect-then-return providers keep their contract),
the Docker implementation's chunk/error/exit-code semantics (an engine or
proxy drop mid-stream is NEVER a fabricated zero-exit completion), and the
dispatch consume loop (stall + deadline kills, live publication, stream
error classification as retryable).
"""

import asyncio
from collections.abc import AsyncIterator
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from modulo.core.bundled_runner.runner_dispatch import _consume_stream
from modulo.core.runtime_provider import ExecProcess, ExecStreamChunk
from modulo.core.runtime_provider.docker import DockerRuntimeProvider, _split_exec_frame

# ---------------------------------------------------------------------------
# ABC default + frame splitting
# ---------------------------------------------------------------------------


async def test_abc_default_exec_stream_raises_not_implemented() -> None:
    from modulo.core.runtime_provider import RuntimeProvider

    class _Bare(RuntimeProvider):
        async def create_workspace(self, spec):  # type: ignore[no-untyped-def]
            return "ref"

        async def exec_command(self, provider_ref, command, *, cmd_timeout=None):  # type: ignore[no-untyped-def]
            raise AssertionError

        async def destroy_workspace(self, provider_ref):  # type: ignore[no-untyped-def]
            return None

        async def get_workspace_status(self, provider_ref):  # type: ignore[no-untyped-def]
            return "running"

    with pytest.raises(NotImplementedError, match="exec_command_stream"):
        await _Bare().exec_command_stream("ref", ["echo"])


def test_split_exec_frame_tuple_shape() -> None:
    out, err = _split_exec_frame((b"hello", b"boom"))
    assert out == b"hello"
    assert err == b"boom"
    out, err = _split_exec_frame((None, b"boom"))
    assert out == b""
    assert err == b"boom"


def test_split_exec_frame_aiodocker_message_shape() -> None:
    message = SimpleNamespace(stream=1, data=b"stdout-data")
    out, err = _split_exec_frame(message)
    assert out == b"stdout-data"
    assert err == b""
    message = SimpleNamespace(stream=2, data=b"stderr-data")
    out, err = _split_exec_frame(message)
    assert out == b""
    assert err == b"stderr-data"


# ---------------------------------------------------------------------------
# DockerRuntimeProvider.exec_command_stream
# ---------------------------------------------------------------------------


def _stream_provider() -> tuple[DockerRuntimeProvider, MagicMock]:
    provider = DockerRuntimeProvider(docker_host="tcp://engine:2375")
    client = MagicMock()
    stream = MagicMock()
    container = MagicMock()
    client.containers.get = AsyncMock(return_value=container)
    provider._client = client  # type: ignore[assignment]
    provider._workspaces["ref-1"] = "cntnr-1"
    return provider, stream


async def test_exec_stream_yields_chunks_and_exit_code() -> None:
    provider, stream = _stream_provider()
    exec_instance = MagicMock()
    exec_instance.start = AsyncMock(return_value=stream)
    exec_instance.inspect = AsyncMock(return_value={"ExitCode": 3})
    container = (await provider._get_client()).containers.get.return_value
    container.exec = AsyncMock(return_value=exec_instance)
    stream.read_out = AsyncMock(
        side_effect=[
            (b"out-1", b""),
            SimpleNamespace(stream=2, data=b"err-1"),
            None,
        ]
    )

    process = await provider.exec_command_stream("ref-1", ["echo", "hi"], environment={"A": "1"})

    chunks = [chunk async for chunk in process.chunks]

    assert [c.data for c in chunks] == ["out-1", "err-1"]
    assert [c.stream for c in chunks] == ["stdout", "stderr"]
    container.exec.assert_awaited_once()
    assert container.exec.await_args.kwargs["environment"] == {"A": "1"}
    # The stream drained healthily -> the real exit code is populated.
    await asyncio.wait_for(process.done.wait(), timeout=1)
    assert process.exit_code == 3
    assert process.error is None


async def test_exec_stream_error_is_never_a_fabricated_success() -> None:
    """Engine/proxy drop mid-stream: process.error set, exit_code stays None,
    `done` fires — the dispatch layer classifies this as RETRYABLE."""
    provider, stream = _stream_provider()
    exec_instance = MagicMock()
    exec_instance.start = AsyncMock(return_value=stream)
    exec_instance.inspect = AsyncMock(return_value={"ExitCode": 0})
    container = (await provider._get_client()).containers.get.return_value
    container.exec = AsyncMock(return_value=exec_instance)

    async def _boom() -> None:
        raise ConnectionResetError("proxy dropped")

    stream.read_out = AsyncMock(side_effect=_boom)

    process = await provider.exec_command_stream("ref-1", ["echo", "hi"])
    chunks = [chunk async for chunk in process.chunks]

    assert chunks == []
    await asyncio.wait_for(process.done.wait(), timeout=1)
    assert process.error is not None
    assert "proxy dropped" in process.error
    # NEVER a fabricated zero-exit completion on a dropped stream.
    assert process.exit_code is None


async def test_exec_stream_unknown_ref_raises() -> None:
    provider, _ = _stream_provider()
    with pytest.raises(ValueError, match="Unknown workspace"):
        await provider.exec_command_stream("missing", ["echo"])


# ---------------------------------------------------------------------------
# _consume_stream (dispatch consume loop)
# ---------------------------------------------------------------------------


def _fake_process(
    chunks: list[ExecStreamChunk], *, delay: float = 0.0, error: str | None = None
) -> tuple[ExecProcess, dict[str, bool]]:
    killed: dict[str, bool] = {"killed": False}

    async def _chunks():
        for chunk in chunks:
            await asyncio.sleep(delay)
            yield chunk

    async def _kill() -> None:
        killed["killed"] = True

    process = ExecProcess(chunks=None, kill=None)  # type: ignore[arg-type]
    process.chunks = _chunks()
    process._kill = _kill
    process.error = error
    return process, killed


def _never_stream() -> AsyncIterator[ExecStreamChunk]:
    async def _gen():
        await asyncio.sleep(30)
        yield ExecStreamChunk(stream="stdout", data="late")

    return _gen()


async def test_consume_collects_all_chunks() -> None:
    process, killed = _fake_process(
        [
            ExecStreamChunk(stream="stdout", data="a"),
            ExecStreamChunk(stream="stderr", data="b"),
            ExecStreamChunk(stream="stdout", data="c"),
        ]
    )
    collected, timed_out, _stalled = await _consume_stream(process, node_id="n", sandbox_timeout=5.0, stall_timeout=3.0)
    assert timed_out is False
    assert collected == [("stdout", "a"), ("stderr", "b"), ("stdout", "c")]
    assert not killed["killed"]


async def test_consume_deadline_kills_and_reports_timeout() -> None:
    process, killed = _fake_process([ExecStreamChunk(stream="stdout", data="x")], delay=10.0)
    collected, timed_out, _stalled = await _consume_stream(
        process, node_id="n", sandbox_timeout=0.1, stall_timeout=10.0
    )
    assert timed_out is True
    assert collected == []
    assert killed["killed"]


async def test_consume_stall_kills_without_total_timeout() -> None:
    """No output within the stall window -> the kill handle fires, the
    consume exits with ``stalled=True`` (the caller classifies the stall as a
    retryable no-output failure)."""
    process, killed = _fake_process([])
    process.chunks = _never_stream()
    collected, timed_out, stalled = await _consume_stream(process, node_id="n", sandbox_timeout=5.0, stall_timeout=0.1)
    assert timed_out is False
    assert stalled is True
    assert collected == []
    assert killed["killed"]


async def test_consume_publishes_live_chunks_throttled() -> None:
    broker = MagicMock()
    broker.publish = MagicMock()
    process, _ = _fake_process([ExecStreamChunk(stream="stdout", data="live")])
    collected, _timed_out, _stalled = await _consume_stream(
        process,
        node_id="n",
        sandbox_timeout=5.0,
        stall_timeout=3.0,
        stream_broker=broker,
    )
    assert collected == [("stdout", "live")]
    broker.publish.assert_called_once()
    event_type, payload = broker.publish.call_args.args
    assert event_type == "node.stdout_chunk"
    assert payload["node_id"] == "n"


async def test_consume_stream_error_sets_process_error_for_caller() -> None:
    process, _ = _fake_process(
        [ExecStreamChunk(stream="stdout", data="partial")], error="ConnectionResetError: dropped"
    )
    collected, timed_out, _stalled = await _consume_stream(process, node_id="n", sandbox_timeout=5.0, stall_timeout=3.0)
    assert timed_out is False
    assert collected == [("stdout", "partial")]
    assert process.error is not None
    # The caller's retryable classification keys on process.error.
    assert "ConnectionResetError" in process.error


async def test_consume_touches_stall_detector_on_progress() -> None:
    stall = MagicMock()
    process, _ = _fake_process([ExecStreamChunk(stream="stdout", data="progress")])
    collected, _timed_out, _stalled = await _consume_stream(
        process,
        node_id="n",
        sandbox_timeout=5.0,
        stall_timeout=3.0,
        stall_detector=stall,
        touch_heartbeat=True,
    )
    assert collected
    touched = {call.args[0] for call in stall.touch.call_args_list}
    assert "output" in touched
    assert "heartbeat" in touched
