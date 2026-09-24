"""FAR-1050 R2a: file-I/O primitives on the runtime-provider ABC.

Exercises, without a live sandbox or network:

1. The ABC's exec-based defaults: a bytes round-trip through
   ``write_file``/``read_file`` against a fake ``exec_command`` that
   emulates a POSIX shell over an in-memory filesystem (path quoting
   included, base64 wrapping tolerated), plus ``list_files`` /
   ``get_info`` parsing and the typed failure on a non-zero exit.
2. The E2B native overrides against a mocked sandbox handle: tracked
   handle used directly, by-ref reconnect for untracked refs, the SDK
   ``files.*`` call shapes, and the ``WorkspaceFileInfo`` mapping.
3. ``WorkspaceFileInfo`` itself (frozen value object) and its export.
"""

from __future__ import annotations

import asyncio
import base64
import posixpath
import shlex
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from e2b import FileType

from modulo.core.runtime_provider import (
    ExecResult,
    RuntimeProvider,
    RuntimeProviderError,
    WorkspaceFileInfo,
    WorkspaceSpec,
)
from modulo.core.runtime_provider.e2b import E2BRuntimeProvider

_BINARY = bytes(range(256)) * 4  # every byte value, incl. NUL / 0xFF


# ---------------------------------------------------------------------------
# 1. Exec-based ABC defaults (fake exec_command)
# ---------------------------------------------------------------------------


class _FakeShellProvider(RuntimeProvider):
    """Concrete provider whose ``exec_command`` emulates a tiny POSIX shell.

    It parses exactly the ``sh -c`` scripts the ABC's file-I/O default
    bodies emit (``base64 <``, ``mkdir -p && printf | base64 -d >``,
    ``ls -1A``, ``stat -c``) with ``shlex.split`` — so a path the ABC
    failed to quote correctly would not round-trip. ``.``/``..`` are
    deliberately emitted by ``ls`` to exercise the ABC's filter.
    """

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.dirs: set[str] = {"/", "."}
        self.commands: list[list[str]] = []

    async def create_workspace(self, spec: WorkspaceSpec) -> str:
        return "fake-ref"

    async def destroy_workspace(self, provider_ref: str) -> None:
        self.files.clear()

    async def get_workspace_status(self, provider_ref: str) -> str:
        return "running"

    async def exec_command(
        self,
        provider_ref: str,
        command: list[str],
        *,
        cmd_timeout: int | None = None,
    ) -> ExecResult:
        self.commands.append(list(command))
        assert len(command) == 3
        assert command[0] == "sh"
        assert command[1] == "-c"
        tokens = shlex.split(command[2])
        op = tokens[0]
        if op == "base64" and tokens[1] == "<":
            return self._read(tokens[2])
        if op == "mkdir":
            return self._write(tokens)
        if op == "ls":
            return self._ls(tokens[2])
        if op == "stat":
            return self._stat(tokens[3])
        return ExecResult(exit_code=127, stdout="", stderr=f"unsupported script: {command[2]!r}")

    def _read(self, path: str) -> ExecResult:
        if path not in self.files:
            return ExecResult(exit_code=1, stdout="", stderr="base64: No such file or directory")
        # Wrap at 76 columns like GNU base64 — the ABC must tolerate newlines.
        payload = base64.b64encode(self.files[path]).decode("ascii")
        wrapped = "\n".join(payload[i : i + 76] for i in range(0, len(payload), 76))
        return ExecResult(exit_code=0, stdout=wrapped + "\n", stderr="")

    def _write(self, tokens: list[str]) -> ExecResult:
        parent = tokens[2]
        sep = tokens.index("&&")
        printf_idx = tokens.index("printf", sep)
        payload = tokens[printf_idx + 2]
        path = tokens[tokens.index(">", printf_idx) + 1]
        self._add_dirs(posixpath.dirname(path) or parent)
        self.files[path] = base64.b64decode(payload)
        return ExecResult(exit_code=0, stdout="", stderr="")

    def _ls(self, path: str) -> ExecResult:
        if path.rstrip("/") not in self.dirs:  # ls treats "/ws/" and "/ws" alike
            return ExecResult(exit_code=2, stdout="", stderr=f"ls: cannot access '{path}'")
        prefix = path.rstrip("/") + "/"
        names = sorted(
            key[len(prefix) :] for key in self.files if key.startswith(prefix) and "/" not in key[len(prefix) :]
        )
        listing = "\n".join([".", "..", *names])
        return ExecResult(exit_code=0, stdout=listing + "\n", stderr="")

    def _stat(self, path: str) -> ExecResult:
        # Malformed-output hooks for the ABC's parse-error branches.
        if path == "/malformed":
            return ExecResult(exit_code=0, stdout="garbage-without-separator\n", stderr="")
        if path == "/malformed-size":
            return ExecResult(exit_code=0, stdout="regular file\tnot-a-number\n", stderr="")
        if path in self.files:
            return ExecResult(exit_code=0, stdout=f"regular file\t{len(self.files[path])}\n", stderr="")
        if path in self.dirs:
            return ExecResult(exit_code=0, stdout="directory\t4096\n", stderr="")
        return ExecResult(exit_code=1, stdout="", stderr=f"stat: cannot statx '{path}'")

    def _add_dirs(self, path: str) -> None:
        while path and path not in self.dirs:
            self.dirs.add(path)
            parent = posixpath.dirname(path)
            if parent == path:
                break
            path = parent


async def test_write_then_read_round_trips_bytes() -> None:
    """write_file -> read_file round-trips binary data through the exec defaults."""
    provider = _FakeShellProvider()
    data = b"\x00\x01binary\xff\xfe\npayload" + _BINARY

    await provider.write_file("fake-ref", "/ws/out.bin", data)
    read_back = await provider.read_file("fake-ref", "/ws/out.bin")

    assert read_back == data


async def test_round_trip_of_empty_file() -> None:
    """An empty payload survives printf/base64 with no special-casing loss."""
    provider = _FakeShellProvider()

    await provider.write_file("fake-ref", "/ws/empty.bin", b"")
    read_back = await provider.read_file("fake-ref", "/ws/empty.bin")

    assert read_back == b""


async def test_round_trip_quotes_hostile_path() -> None:
    """A path with spaces and a quote survives shlex quoting end to end."""
    provider = _FakeShellProvider()
    path = "/ws dir/it's a f.bin"
    data = b"hostile-path payload"

    await provider.write_file("fake-ref", path, data)

    assert await provider.read_file("fake-ref", path) == data


async def test_write_file_creates_parent_directories() -> None:
    """The mkdir -p prefix means listing a newly written nested parent works."""
    provider = _FakeShellProvider()

    await provider.write_file("fake-ref", "/ws/nested/deep/a.bin", b"aa")
    listed = await provider.list_files("fake-ref", "/ws/nested/deep")

    assert listed == ["/ws/nested/deep/a.bin"]


async def test_list_files_returns_sorted_full_paths_without_dot_entries() -> None:
    """list_files joins names onto the directory path, sorts, drops . and .. ."""
    provider = _FakeShellProvider()
    provider.files["/ws/b.txt"] = b"b"
    provider.files["/ws/a.txt"] = b"aa"
    provider.dirs.add("/ws")

    listed = await provider.list_files("fake-ref", "/ws")

    assert listed == ["/ws/a.txt", "/ws/b.txt"]


async def test_list_files_normalises_trailing_slash() -> None:
    """A trailing slash addresses the same directory as the bare path."""
    provider = _FakeShellProvider()
    provider.files["/ws/a.txt"] = b"aa"
    provider.dirs.add("/ws")

    assert await provider.list_files("fake-ref", "/ws/") == await provider.list_files("fake-ref", "/ws")


async def test_get_info_reports_file_size_and_dir_flag() -> None:
    """get_info maps the stat output onto the WorkspaceFileInfo fields."""
    provider = _FakeShellProvider()
    provider.files["/ws/a.bin"] = b"12345"
    provider.dirs.add("/ws")

    file_info = await provider.get_info("fake-ref", "/ws/a.bin")
    dir_info = await provider.get_info("fake-ref", "/ws")

    assert file_info == WorkspaceFileInfo(path="/ws/a.bin", size=5, is_dir=False)
    assert dir_info == WorkspaceFileInfo(path="/ws", size=4096, is_dir=True)


async def test_read_file_missing_file_raises_typed_error() -> None:
    """A non-zero exec exit surfaces as RuntimeProviderError, not a silent b''."""
    provider = _FakeShellProvider()

    with pytest.raises(RuntimeProviderError, match="read_file") as excinfo:
        await provider.read_file("fake-ref", "/ws/nope.bin")

    assert "/ws/nope.bin" in str(excinfo.value)


async def test_write_file_failure_raises_typed_error() -> None:
    """A failing write (here: an unsupported script) is a typed refusal."""
    provider = _FakeShellProvider()

    async def _unsupported(
        provider_ref: str,
        command: list[str],
        *,
        cmd_timeout: int | None = None,
    ) -> ExecResult:
        return ExecResult(exit_code=1, stdout="", stderr="disk full")

    provider.exec_command = _unsupported  # type: ignore[method-assign]

    with pytest.raises(RuntimeProviderError, match="write_file") as excinfo:
        await provider.write_file("fake-ref", "/ws/a.bin", b"data")

    assert "disk full" in str(excinfo.value)


async def test_list_files_missing_directory_raises_typed_error() -> None:
    """Listing a path the substrate does not have is a typed failure."""
    provider = _FakeShellProvider()

    with pytest.raises(RuntimeProviderError, match="list_files"):
        await provider.list_files("fake-ref", "/no/such/dir")


async def test_get_info_missing_path_raises_typed_error() -> None:
    """Statting a missing path is a typed failure, not a fabricated FileInfo."""
    provider = _FakeShellProvider()

    with pytest.raises(RuntimeProviderError, match="get_info"):
        await provider.get_info("fake-ref", "/ws/ghost.bin")


async def test_get_info_rejects_unparseable_stat_output() -> None:
    """Stat output without the size separator is rejected, never guessed at."""
    provider = _FakeShellProvider()

    with pytest.raises(RuntimeProviderError, match="could not parse stat output"):
        await provider.get_info("fake-ref", "/malformed")


async def test_get_info_rejects_non_numeric_size() -> None:
    """A non-numeric size is rejected, never coerced to a wrong value."""
    provider = _FakeShellProvider()

    with pytest.raises(RuntimeProviderError, match="could not parse stat size"):
        await provider.get_info("fake-ref", "/malformed-size")


# ---------------------------------------------------------------------------
# 2. E2B native overrides (mocked sandbox handle)
# ---------------------------------------------------------------------------


def _provider_with_sandbox(files: Any) -> tuple[E2BRuntimeProvider, Any]:
    provider = E2BRuntimeProvider(api_key="r2a-key")
    sandbox = SimpleNamespace(files=files)
    provider._sandboxes["sbx-r2a"] = sandbox  # pre-seed the tracked handle
    return provider, sandbox


async def test_e2b_read_file_uses_native_files_read() -> None:
    """The native override calls the SDK (bytes format) and returns bytes."""
    files = SimpleNamespace(read=AsyncMock(return_value=bytearray(b"\x00\xffraw")))
    provider, _sandbox = _provider_with_sandbox(files)

    result = await provider.read_file("sbx-r2a", "/home/user/out.bin")

    assert result == b"\x00\xffraw"
    files.read.assert_awaited_once()
    assert files.read.await_args.args[0] == "/home/user/out.bin"
    assert files.read.await_args.kwargs["format"] == "bytes"


async def test_e2b_write_file_uses_native_files_write() -> None:
    """The native override passes path and raw bytes straight to the SDK."""
    files = SimpleNamespace(write=AsyncMock())
    provider, _sandbox = _provider_with_sandbox(files)

    await provider.write_file("sbx-r2a", "/home/user/in.bin", b"\x00data")

    files.write.assert_awaited_once_with("/home/user/in.bin", b"\x00data", request_timeout=30)


async def test_e2b_list_files_maps_entries_to_sorted_full_paths() -> None:
    """SDK entries map to the ABC contract: full paths, sorted, no dot entries."""
    entries = [
        SimpleNamespace(name="b.txt", path="/home/user/b.txt"),
        SimpleNamespace(name=".", path="/home/user/."),
        SimpleNamespace(name="a.txt", path="/home/user/a.txt"),
        SimpleNamespace(name="..", path="/home/user/.."),
    ]
    files = SimpleNamespace(list=AsyncMock(return_value=entries))
    provider, _sandbox = _provider_with_sandbox(files)

    listed = await provider.list_files("sbx-r2a", "/home/user")

    assert listed == ["/home/user/a.txt", "/home/user/b.txt"]
    files.list.assert_awaited_once_with("/home/user", request_timeout=30)


async def test_e2b_get_info_maps_sdk_entry_info() -> None:
    """FileType.DIR -> is_dir True (size preserved); file -> is_dir False."""
    files = SimpleNamespace(
        get_info=AsyncMock(
            side_effect=[
                SimpleNamespace(type=FileType.DIR, size=4096),
                SimpleNamespace(type=FileType.FILE, size=42),
            ]
        )
    )
    provider, _sandbox = _provider_with_sandbox(files)

    dir_info = await provider.get_info("sbx-r2a", "/home/user")
    file_info = await provider.get_info("sbx-r2a", "/home/user/a.bin")

    assert dir_info == WorkspaceFileInfo(path="/home/user", size=4096, is_dir=True)
    assert file_info == WorkspaceFileInfo(path="/home/user/a.bin", size=42, is_dir=False)


async def test_e2b_file_ops_use_tracked_handle_without_connect() -> None:
    """A tracked handle (provider-created workspace) is used directly."""
    files = SimpleNamespace(read=AsyncMock(return_value=bytearray(b"x")))
    provider, _sandbox = _provider_with_sandbox(files)

    with patch(
        "e2b.AsyncSandbox.connect",
        new=AsyncMock(side_effect=AssertionError("connect must not run for a tracked sandbox")),
    ):
        await provider.read_file("sbx-r2a", "/home/user/a.bin")

    files.read.assert_awaited_once()


async def test_e2b_file_ops_reconnect_by_ref_when_untracked() -> None:
    """The R2a shape: a fresh provider reconnects by ref, as apply_isolation does."""
    files = SimpleNamespace(read=AsyncMock(return_value=bytearray(b"x")))
    sandbox = SimpleNamespace(files=files)
    provider = E2BRuntimeProvider(api_key="r2a-key")
    connect = AsyncMock(return_value=sandbox)

    with patch("e2b.AsyncSandbox.connect", new=connect):
        await provider.read_file("sbx-untracked", "/home/user/a.bin")

    connect.assert_awaited_once()
    files.read.assert_awaited_once()


async def test_e2b_file_ops_reconnect_failure_raises_runtime_error() -> None:
    """A failed reconnect surfaces as a RuntimeError naming the operation."""
    provider = E2BRuntimeProvider(api_key="r2a-key")

    with (
        patch("e2b.AsyncSandbox.connect", new=AsyncMock(side_effect=OSError("control plane down"))),
        pytest.raises(RuntimeError, match="reconnect to sandbox"),
    ):
        await provider.read_file("sbx-gone", "/home/user/a.bin")


async def test_e2b_file_ops_reconnect_cancellation_propagates() -> None:
    """CancelledError from the reconnect is re-raised, never swallowed."""
    provider = E2BRuntimeProvider(api_key="r2a-key")

    with (
        patch("e2b.AsyncSandbox.connect", new=AsyncMock(side_effect=asyncio.CancelledError())),
        pytest.raises(asyncio.CancelledError),
    ):
        await provider.get_info("sbx-cancel", "/home/user")


# ---------------------------------------------------------------------------
# 3. WorkspaceFileInfo carrier
# ---------------------------------------------------------------------------


def test_workspace_file_info_fields() -> None:
    info = WorkspaceFileInfo(path="/ws/a.bin", size=12, is_dir=False)
    assert info.path == "/ws/a.bin"
    assert info.size == 12
    assert info.is_dir is False


def test_workspace_file_info_is_frozen() -> None:
    """The carrier is an immutable value object (parity across providers)."""
    info = WorkspaceFileInfo(path="/ws/a.bin", size=1, is_dir=False)
    with pytest.raises(FrozenInstanceError, match="cannot assign"):
        info.size = 2  # type: ignore[misc]
    assert info.size == 1


def test_workspace_file_info_exported_in_all() -> None:
    import modulo.core.runtime_provider as pkg

    assert "WorkspaceFileInfo" in pkg.__all__
    assert pkg.WorkspaceFileInfo is WorkspaceFileInfo
