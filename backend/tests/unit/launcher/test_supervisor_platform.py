"""Platform-seam tests for the supervisor's process-identity backends (FAR-2185).

The launcher used to be Linux-only because the child-death watchdog read
``/proc/<pid>/stat`` field 22 (STARTTIME). macOS has no ``/proc``; libproc's
``proc_pidinfo(PROC_PIDTBSDINFO)`` carries the same fact
(``pbi_start_tvsec`` / ``pbi_start_tvusec``). This module locks the seam:

* the libproc struct layout and start-time token encoding,
* every "unknown" path (the answer must be None/[] — never a guess),
* the single-place platform dispatch,
* the Linux unreadable-STARTTIME sentinel, unchanged,
* the macOS child-shim wrapping decision.

Everything here except the two ``@macos_only`` tests runs on LINUX too: the
darwin backend is driven through an injected fake libproc, so its logic is
covered in ordinary CI. The ``@macos_only`` tests exercise the REAL libproc
calls and the FAR-678 orphan-reap fault end-to-end; they SKIP on Linux with
the reason below and MUST be run on a Mac before a darwin build is trusted.
"""

from __future__ import annotations

import ctypes
import logging
import os
import sys
import time
import types
from collections.abc import Callable
from subprocess import Popen
from typing import Any

import pytest

import modulo.launcher.supervisor as supervisor_module
from modulo.launcher.supervisor import (
    SHIM_POLL_SECONDS,
    ChildSpec,
    Supervisor,
    SupervisorKnobs,
    _children_of,
    _DarwinProcessBackend,
    _is_postgres_process,
    _LinuxProcessBackend,
    _pid_alive,
    _pid_starttime_epoch,
    _ProcBsdInfo,
    _process_backend,
    child_shim_argv,
    read_proc_starttime,
)

macos_only = pytest.mark.skipif(
    sys.platform != "darwin",
    reason=(
        "FAR-2185 macOS-only fault test: exercises the REAL libproc start-time seam and the "
        "darwin child-death binding with live processes. It cannot execute on Linux — run it on "
        "a Mac with `uv run pytest tests/unit/launcher/test_supervisor_platform.py`."
    ),
)


# ---------------------------------------------------------------------------
# Fake libproc: the darwin backend without a darwin kernel
# ---------------------------------------------------------------------------


class FakeLibproc:
    """Minimal libproc stand-in for the three functions the backend calls.

    ``start=None`` / ``path=None`` model the kernel's failure answer (0), so
    the "unknown identity" paths are exercised as real call paths, not as
    monkeypatched returns.
    """

    def __init__(
        self,
        *,
        start: tuple[int, int] | None = (1_700_000_000, 123_456),
        path: bytes | None = b"/opt/modulo/bin/postgres",
        children: tuple[int, ...] = (),
        child_count_override: int | None = None,
    ) -> None:
        self.start = start
        self.path = path
        self.children = children
        self.child_count_override = child_count_override
        self.calls: list[tuple[str, int]] = []

    def proc_pidinfo(self, pid: int, _flavor: int, _arg: int, buffer: Any, _size: int) -> int:
        self.calls.append(("proc_pidinfo", pid))
        if self.start is None:
            return 0
        info = ctypes.cast(buffer, ctypes.POINTER(_ProcBsdInfo)).contents
        info.pbi_pid = pid
        info.pbi_start_tvsec = self.start[0]
        info.pbi_start_tvusec = self.start[1]
        return ctypes.sizeof(_ProcBsdInfo)

    def proc_pidpath(self, pid: int, buffer: Any, _size: int) -> int:
        self.calls.append(("proc_pidpath", pid))
        if self.path is None:
            return 0
        ctypes.memmove(buffer, self.path + b"\x00", len(self.path) + 1)
        return len(self.path)

    def proc_listchildpids(self, ppid: int, buffer: Any, size: int) -> int:
        self.calls.append(("proc_listchildpids", ppid))
        capacity = size // ctypes.sizeof(ctypes.c_int)
        count = len(self.children) if self.child_count_override is None else self.child_count_override
        written = min(count, capacity)
        for index in range(min(len(self.children), written)):
            buffer[index] = self.children[index]
        return count


@pytest.fixture
def fake_libproc(monkeypatch: pytest.MonkeyPatch) -> Callable[..., FakeLibproc]:
    """Install a fake libproc and return the factory that builds it."""

    def install(**kwargs: Any) -> FakeLibproc:
        fake = FakeLibproc(**kwargs)
        monkeypatch.setattr(supervisor_module, "_load_libproc", lambda: fake)
        return fake

    return install


class StubBackend:
    """A complete process-identity backend with recorded calls."""

    def __init__(self, *, shim_starttime: int | None = 4242) -> None:
        self.calls: list[tuple[str, int]] = []
        self._shim_starttime = shim_starttime

    def starttime(self, pid: int) -> int | None:
        self.calls.append(("starttime", pid))
        return 4242

    def shim_starttime(self, pid: int) -> int | None:
        self.calls.append(("shim_starttime", pid))
        return self._shim_starttime

    def start_epoch(self, pid: int) -> float | None:
        self.calls.append(("start_epoch", pid))
        return 1_700_000_000.0

    def children(self, pid: int) -> list[int]:
        self.calls.append(("children", pid))
        return [7, 8]

    def is_postgres(self, pid: int) -> bool | None:
        self.calls.append(("is_postgres", pid))
        return True


# ---------------------------------------------------------------------------
# libproc struct layout (the ABI the kernel fills)
# ---------------------------------------------------------------------------


def test_proc_bsdinfo_layout_matches_the_darwin_abi() -> None:
    """The start-time tail is read straight from kernel memory — pin the ABI."""
    assert ctypes.sizeof(_ProcBsdInfo) == 136
    assert _ProcBsdInfo.pbi_start_tvsec.offset == 120
    assert _ProcBsdInfo.pbi_start_tvusec.offset == 128


# ---------------------------------------------------------------------------
# Darwin backend: start-time identity (the STARTTIME replacement)
# ---------------------------------------------------------------------------


def test_darwin_starttime_encodes_sec_and_usec(fake_libproc: Callable[..., FakeLibproc]) -> None:
    fake = fake_libproc(start=(1_700_000_123, 456_789))
    token = _DarwinProcessBackend().starttime(os.getpid())
    assert token == 1_700_000_123 * 1_000_000 + 456_789
    assert fake.calls == [("proc_pidinfo", os.getpid())]


def test_darwin_starttime_changes_when_the_pid_is_recycled(fake_libproc: Callable[..., FakeLibproc]) -> None:
    """The recycle-detection semantic: same PID, different process → new token."""
    fake = fake_libproc(start=(1_700_000_000, 10))
    backend = _DarwinProcessBackend()
    before = backend.starttime(os.getpid())
    fake.start = (1_700_000_000, 11)  # the kernel handed the PID to a new process
    after = backend.starttime(os.getpid())
    assert before != after


def test_darwin_starttime_is_none_when_pidinfo_fails(fake_libproc: Callable[..., FakeLibproc]) -> None:
    fake_libproc(start=None)
    assert _DarwinProcessBackend().starttime(os.getpid()) is None


def test_darwin_starttime_is_none_without_libproc(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(supervisor_module, "_load_libproc", lambda: None)
    assert _DarwinProcessBackend().starttime(os.getpid()) is None


def test_darwin_start_epoch_is_the_absolute_process_start(fake_libproc: Callable[..., FakeLibproc]) -> None:
    fake_libproc(start=(1_700_000_000, 250_000))
    assert _DarwinProcessBackend().start_epoch(os.getpid()) == pytest.approx(1_700_000_000.25)


def test_darwin_shim_starttime_never_becomes_the_linux_sentinel(
    fake_libproc: Callable[..., FakeLibproc],
) -> None:
    """An unreadable darwin identity must be None (do not wrap), never -1.

    ``-1`` is the Linux sentinel that makes the shim abandon immediately; on
    darwin a wrapped shim would then kill the service it just spawned.
    """
    fake_libproc(start=None)
    assert _DarwinProcessBackend().shim_starttime(os.getpid()) is None


# ---------------------------------------------------------------------------
# Darwin backend: children + executable identity
# ---------------------------------------------------------------------------


def test_darwin_children_lists_direct_children(fake_libproc: Callable[..., FakeLibproc]) -> None:
    fake = fake_libproc(children=(11, 22, 33))
    assert _DarwinProcessBackend().children(1234) == [11, 22, 33]
    assert fake.calls == [("proc_listchildpids", 1234)]


def test_darwin_children_clamps_to_the_buffer_and_drops_zero_slots(
    fake_libproc: Callable[..., FakeLibproc],
) -> None:
    fake_libproc(children=(11, 22), child_count_override=9999)
    assert _DarwinProcessBackend().children(1234) == [11, 22]


def test_darwin_children_empty_when_the_call_reports_failure(
    fake_libproc: Callable[..., FakeLibproc],
) -> None:
    fake_libproc(children=(11,), child_count_override=-1)
    assert not _DarwinProcessBackend().children(1234)


def test_darwin_children_empty_without_libproc(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(supervisor_module, "_load_libproc", lambda: None)
    assert not _DarwinProcessBackend().children(1234)


def test_darwin_is_postgres_matches_the_executable_basename(fake_libproc: Callable[..., FakeLibproc]) -> None:
    fake_libproc(path=b"/opt/modulo/bin/postgres")
    assert _DarwinProcessBackend().is_postgres(1234) is True


def test_darwin_is_postgres_rejects_a_foreign_executable(fake_libproc: Callable[..., FakeLibproc]) -> None:
    fake_libproc(path=b"/usr/local/bin/redis-server")
    assert _DarwinProcessBackend().is_postgres(1234) is False


def test_darwin_is_postgres_is_unknowable_when_the_path_fails(
    fake_libproc: Callable[..., FakeLibproc],
) -> None:
    fake_libproc(path=None)
    assert _DarwinProcessBackend().is_postgres(1234) is None


def test_darwin_is_postgres_is_unknowable_without_libproc(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(supervisor_module, "_load_libproc", lambda: None)
    assert _DarwinProcessBackend().is_postgres(1234) is None


# ---------------------------------------------------------------------------
# The seam itself: ONE platform switch
# ---------------------------------------------------------------------------


def test_process_backend_is_selected_by_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    assert isinstance(_process_backend(), _LinuxProcessBackend)
    monkeypatch.setattr(sys, "platform", "darwin")
    assert isinstance(_process_backend(), _DarwinProcessBackend)
    monkeypatch.setattr(sys, "platform", "win32")
    assert _process_backend() is None
    monkeypatch.setattr(sys, "platform", "freebsd14")
    assert _process_backend() is None


def test_module_helpers_delegate_to_the_selected_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = StubBackend()
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setitem(supervisor_module._PROCESS_BACKENDS, "darwin", stub)
    assert read_proc_starttime(99) == 4242
    assert _children_of(99) == [7, 8]
    assert _pid_starttime_epoch(99) == 1_700_000_000.0
    assert _is_postgres_process(99) is True
    assert stub.calls == [
        ("starttime", 99),
        ("children", 99),
        ("start_epoch", 99),
        ("is_postgres", 99),
    ]


def test_helpers_keep_the_unknown_contract_without_a_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    assert read_proc_starttime(os.getpid()) is None
    assert not _children_of(os.getpid())
    assert _pid_starttime_epoch(os.getpid()) is None
    assert _is_postgres_process(os.getpid()) is None


# ---------------------------------------------------------------------------
# Linux path: unchanged
# ---------------------------------------------------------------------------


def test_linux_shim_starttime_preserves_the_unreadable_sentinel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Linux keeps wrapping with -1 when STARTTIME is unreadable (unchanged)."""
    backend = _LinuxProcessBackend()
    monkeypatch.setattr(backend, "starttime", lambda _pid: None)
    assert backend.shim_starttime(1234) == -1


def test_linux_child_shim_argv_is_byte_identical_to_the_historical_shape() -> None:
    if sys.platform != "linux":
        pytest.skip("pins the Linux argv shape")
    parent = os.getpid()
    expected_starttime = _LinuxProcessBackend().starttime(parent)
    argv = child_shim_argv(["redis-server", "--port", "1"], parent_pid=parent)
    assert argv == [
        sys.executable,
        "-m",
        "modulo.launcher.supervisor",
        "child-shim",
        "--parent-pid",
        str(parent),
        "--parent-starttime",
        str(expected_starttime),
        "--",
        "redis-server",
        "--port",
        "1",
    ]


# ---------------------------------------------------------------------------
# Child shim: the darwin wrapping decision
# ---------------------------------------------------------------------------


def test_child_shim_argv_wraps_when_darwin_identity_is_readable(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = StubBackend()
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setitem(supervisor_module._PROCESS_BACKENDS, "darwin", stub)
    argv = child_shim_argv(["redis-server", "--port", "1"], parent_pid=4242)
    assert argv == [
        sys.executable,
        "-m",
        "modulo.launcher.supervisor",
        "child-shim",
        "--parent-pid",
        "4242",
        "--parent-starttime",
        "4242",
        "--",
        "redis-server",
        "--port",
        "1",
    ]


def test_child_shim_argv_passes_through_when_darwin_identity_is_unreadable(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Unwrapped + a loud warning beats a shim that kills its own service."""
    stub = StubBackend(shim_starttime=None)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setitem(supervisor_module._PROCESS_BACKENDS, "darwin", stub)
    with caplog.at_level(logging.WARNING, logger="modulo.launcher.supervisor"):
        argv = child_shim_argv(["redis-server"], parent_pid=4242)
    assert argv == ["redis-server"]
    assert "supervisor.shim_identity_unreadable" in caplog.text


# ---------------------------------------------------------------------------
# Linux backend: malformed / unreadable /proc edges
# ---------------------------------------------------------------------------


class _FakeProcPath:
    """Stand-in for ``Path`` behind the Linux /proc readers."""

    def __init__(
        self,
        *,
        text: str | None = None,
        data: bytes | None = None,
        error: BaseException | None = None,
    ) -> None:
        self._text = text
        self._data = data
        self._error = error

    def read_text(self, encoding: str | None = None) -> str:
        if self._error is not None:
            raise self._error
        assert self._text is not None
        return self._text

    def read_bytes(self) -> bytes:
        if self._error is not None:
            raise self._error
        assert self._data is not None
        return self._data


def _install_linux_proc(monkeypatch: pytest.MonkeyPatch, **kwargs: Any) -> None:
    monkeypatch.setattr(supervisor_module, "Path", lambda _path: _FakeProcPath(**kwargs))


def test_linux_starttime_is_none_when_stat_has_too_few_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_linux_proc(monkeypatch, text="1234 (postgres) S 1 2 3")
    assert _LinuxProcessBackend().starttime(1234) is None


def test_linux_starttime_is_none_when_starttime_is_not_an_int(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tokens = ["S", *(str(index) for index in range(1, 19)), "not-a-number"]
    _install_linux_proc(monkeypatch, text=f"1234 (postgres) {' '.join(tokens)}")
    assert _LinuxProcessBackend().starttime(1234) is None


def test_linux_start_epoch_is_none_when_uptime_is_unreadable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _LinuxProcessBackend()
    monkeypatch.setattr(backend, "starttime", lambda _pid: 100)
    _install_linux_proc(monkeypatch, error=OSError("no /proc"))
    assert backend.start_epoch(1234) is None


def test_linux_start_epoch_is_none_when_clock_tick_rate_is_not_positive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _LinuxProcessBackend()
    monkeypatch.setattr(backend, "starttime", lambda _pid: 100)
    _install_linux_proc(monkeypatch, text="10.00 20.00")
    monkeypatch.setattr(supervisor_module.os, "sysconf", lambda _name: 0)
    assert backend.start_epoch(1234) is None


def test_linux_children_is_empty_when_a_token_is_not_an_int(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_linux_proc(monkeypatch, text="11 not-a-pid 33")
    assert not _LinuxProcessBackend().children(1234)


def test_linux_is_postgres_is_unknowable_on_permission_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_linux_proc(monkeypatch, error=PermissionError("denied"))
    assert _LinuxProcessBackend().is_postgres(1234) is None


# ---------------------------------------------------------------------------
# libproc loader: bind the symbols, fall back, answer None when unavailable
# ---------------------------------------------------------------------------


class _FakeLibprocLibrary:
    """A CDLL stand-in exposing the three libproc symbols."""

    def __init__(self) -> None:
        self.proc_pidinfo = types.SimpleNamespace()
        self.proc_pidpath = types.SimpleNamespace()
        self.proc_listchildpids = types.SimpleNamespace()


@pytest.fixture
def clean_libproc_cache() -> Any:
    """The loader is lru_cached — never let a fake leak into another test."""
    supervisor_module._load_libproc.cache_clear()
    yield
    supervisor_module._load_libproc.cache_clear()


def test_load_libproc_binds_the_three_symbols(
    monkeypatch: pytest.MonkeyPatch,
    clean_libproc_cache: None,
) -> None:
    lib = _FakeLibprocLibrary()
    monkeypatch.setattr(ctypes.util, "find_library", lambda _name: "/usr/lib/libproc.dylib")
    monkeypatch.setattr(ctypes, "CDLL", lambda *_args, **_kwargs: lib)
    assert supervisor_module._load_libproc() is lib
    assert lib.proc_pidinfo.restype is ctypes.c_int
    assert lib.proc_pidpath.restype is ctypes.c_int
    assert lib.proc_listchildpids.restype is ctypes.c_int


def test_load_libproc_falls_through_to_the_bare_dylib(
    monkeypatch: pytest.MonkeyPatch,
    clean_libproc_cache: None,
) -> None:
    lib = _FakeLibprocLibrary()
    attempted: list[str] = []

    def fake_cdll(name: str, **_kwargs: Any) -> Any:
        attempted.append(name)
        if name == "libproc.dylib":
            return lib
        raise OSError("not found")

    monkeypatch.setattr(ctypes.util, "find_library", lambda _name: "candidate-a")
    monkeypatch.setattr(ctypes, "CDLL", fake_cdll)
    assert supervisor_module._load_libproc() is lib
    assert attempted == ["candidate-a", "libproc.dylib"]


def test_load_libproc_is_none_when_every_candidate_fails_to_load(
    monkeypatch: pytest.MonkeyPatch,
    clean_libproc_cache: None,
) -> None:
    monkeypatch.setattr(ctypes.util, "find_library", lambda _name: None)

    def fake_cdll(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError("not found")

    monkeypatch.setattr(ctypes, "CDLL", fake_cdll)
    assert supervisor_module._load_libproc() is None


def test_load_libproc_is_none_when_a_symbol_is_missing(
    monkeypatch: pytest.MonkeyPatch,
    clean_libproc_cache: None,
) -> None:
    monkeypatch.setattr(ctypes.util, "find_library", lambda _name: None)
    monkeypatch.setattr(ctypes, "CDLL", lambda *_args, **_kwargs: object())
    assert supervisor_module._load_libproc() is None


# ---------------------------------------------------------------------------
# Darwin backend: real-call failures (0-byte answers and raised OSErrors)
# ---------------------------------------------------------------------------


def _raise_oserror(*_args: Any, **_kwargs: Any) -> Any:
    raise OSError("libproc call failed")


def test_darwin_start_epoch_is_none_when_pidinfo_fails(
    fake_libproc: Callable[..., FakeLibproc],
) -> None:
    fake_libproc(start=None)
    assert _DarwinProcessBackend().start_epoch(os.getpid()) is None


def test_darwin_starttime_is_none_when_pidinfo_raises(
    fake_libproc: Callable[..., FakeLibproc],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = fake_libproc()
    monkeypatch.setattr(fake, "proc_pidinfo", _raise_oserror)
    assert _DarwinProcessBackend().starttime(1234) is None


def test_darwin_children_is_empty_when_listchildpids_raises(
    fake_libproc: Callable[..., FakeLibproc],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = fake_libproc()
    monkeypatch.setattr(fake, "proc_listchildpids", _raise_oserror)
    assert not _DarwinProcessBackend().children(1234)


def test_darwin_is_postgres_is_unknowable_when_pidpath_raises(
    fake_libproc: Callable[..., FakeLibproc],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = fake_libproc()
    monkeypatch.setattr(fake, "proc_pidpath", _raise_oserror)
    assert _DarwinProcessBackend().is_postgres(1234) is None


# ---------------------------------------------------------------------------
# macOS-only: real libproc + the FAR-678 orphan-reap fault test
# ---------------------------------------------------------------------------


def _wait_for_child_pids(parent_pid: int, *, attempts: int = 600) -> list[int]:
    """Poll for a freshly spawned child's PIDs (bounded; 0.05s ticks)."""
    for _ in range(attempts):
        pids = _children_of(parent_pid)
        if pids:
            return pids
        time.sleep(0.05)
    return []


@macos_only
def test_darwin_read_proc_starttime_for_self_is_stable() -> None:
    """The real libproc seam: one process, one stable identity token."""
    first = read_proc_starttime(os.getpid())
    second = read_proc_starttime(os.getpid())
    assert first is not None
    assert isinstance(first, int)
    assert first > 0
    assert second == first


@macos_only
def test_darwin_children_of_self_lists_a_live_child() -> None:
    """The real proc_listchildpids seam."""
    # Popen (not run+timeout): the child must stay ALIVE for the probe.
    # subprocess.Popen takes no construction-time timeout keyword, so the
    # finally below bounds it with kill + wait(timeout=10).
    child = Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        pids = _wait_for_child_pids(os.getpid())
        assert child.pid in pids
    finally:
        child.kill()
        child.wait(timeout=10)


@macos_only
def test_orphan_is_reaped_when_its_launcher_dies() -> None:
    """FAR-678 fault test: the supervisor dies → the child is reaped.

    A dedicated "launcher" process stands in for the supervisor so its death
    is explicit. The shim watches THAT pid (its start-time identity) and must
    kill the service and exit 2 when it disappears. A shim that never notices
    makes ``shim.wait`` time out — the failure signal — rather than passing
    vacuously.
    """
    # Popen: the launcher must stay ALIVE until this test kills it, and the
    # finally bounds every path with kill + wait(timeout=10).
    launcher = Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    recorded = read_proc_starttime(launcher.pid)
    assert recorded is not None
    # Bounded in the finally: kill + wait(timeout=10). The in-test wait is
    # ALSO the assertion (a shim that never notices times out and fails).
    shim = Popen(  # noqa: S603 — argv built by child_shim_argv, test driver
        child_shim_argv([sys.executable, "-c", "import time; time.sleep(120)"], parent_pid=launcher.pid),
        start_new_session=True,
    )
    try:
        service_pids = _wait_for_child_pids(shim.pid)
        assert service_pids
        launcher.kill()
        launcher.wait(timeout=10)
        # The shim polls at most every SHIM_POLL_SECONDS; allow generous slack
        # for a loaded machine, but the wait itself is the assertion.
        assert shim.wait(timeout=SHIM_POLL_SECONDS + 30) == 2
        assert not any(_pid_alive(pid) for pid in service_pids)
    finally:
        if shim.poll() is None:
            shim.kill()
            shim.wait(timeout=10)
        if launcher.poll() is None:
            launcher.kill()
            launcher.wait(timeout=10)


@macos_only
def test_darwin_supervisor_teardown_leaves_no_orphans() -> None:
    """The supervisor's own spawn/teardown path on darwin (shim + killpg)."""
    supervisor = Supervisor(SupervisorKnobs(shutdown_grace_seconds=2.0, pg_fast_shutdown_timeout=2.0))
    sleep_argv = [sys.executable, "-c", "import time; time.sleep(60)"]
    supervisor.add(ChildSpec(name="sleeper", argv_builder=lambda: sleep_argv))
    supervisor.start(start_monitor=False)
    child = supervisor._children["sleeper"]
    shim = child.process
    assert shim is not None
    service_pids = _wait_for_child_pids(shim.pid)
    assert service_pids
    supervisor.shutdown()
    assert shim.poll() is not None
    for pid in service_pids:
        assert not _pid_alive(pid)
