"""Unit tests for the Bundled Runner dispatch helpers (FAR-590 D4).

Covers the provider-backed file IO helpers and the pure timeout/uuid
validators in ``modulo.core.bundled_runner.runner_dispatch`` that are not
exercised by the route-resolution or streaming suites.
"""

import uuid
from unittest.mock import AsyncMock

import pytest

from modulo.core.bundled_runner import runner_dispatch as rd


class _FakeResult:
    def __init__(self, exit_code: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr


class _FakeProvider:
    def __init__(self, result: _FakeResult) -> None:
        self._result = result
        self.exec_command = AsyncMock(return_value=result)


async def test_write_file_via_exec_success() -> None:
    provider = _FakeProvider(_FakeResult(exit_code=0))
    await rd._write_file_via_exec(provider, "ref1", "/home/user/out.txt", "hello")
    assert provider.exec_command.await_count == 1
    cmd = provider.exec_command.call_args.args[1]
    assert cmd[0] == "sh"
    assert cmd[1] == "-c"
    assert "base64 -d" in cmd[2]


async def test_write_file_via_exec_nonzero_exit_raises() -> None:
    provider = _FakeProvider(_FakeResult(exit_code=1, stderr="boom"))
    with pytest.raises(RuntimeError, match="exit 1"):
        await rd._write_file_via_exec(provider, "ref1", "/home/user/out.txt", "hello")


async def test_read_file_via_exec_success() -> None:
    provider = _FakeProvider(_FakeResult(exit_code=0, stdout="payload"))
    assert await rd._read_file_via_exec(provider, "ref1", "/home/user/out.txt") == "payload"


async def test_read_file_via_exec_missing_file_returns_empty() -> None:
    provider = _FakeProvider(_FakeResult(exit_code=0, stdout=""))
    assert not await rd._read_file_via_exec(provider, "ref1", "/nope.txt")


async def test_read_file_via_exec_error_returns_empty() -> None:
    provider = _FakeProvider(_FakeResult(exit_code=2, stderr="err"))
    assert not await rd._read_file_via_exec(provider, "ref1", "/x.txt")


def test_validate_e2b_dispatch_timeout_none_passes() -> None:
    assert rd.validate_e2b_dispatch_timeout(None) is None
    assert rd.validate_e2b_dispatch_timeout("") is None
    assert rd.validate_e2b_dispatch_timeout(1800) is None


def test_validate_e2b_dispatch_timeout_over_cap_raises() -> None:
    from modulo.core.bundled_runner.runner_dispatch import _E2B_MAX_TIMEOUT_SECONDS

    with pytest.raises(rd.SandboxDispatchTimeoutValidationError):
        rd.validate_e2b_dispatch_timeout(_E2B_MAX_TIMEOUT_SECONDS + 1)


def test_parse_uuid_roundtrip_and_none() -> None:
    assert rd._parse_uuid(None) is None
    value = "12345678-1234-5678-1234-567812345678"
    assert rd._parse_uuid(value) == uuid.UUID(value)
    assert rd._parse_uuid("not-a-uuid") is None
    assert rd._parse_uuid(object()) is None
