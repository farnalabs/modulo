"""FAR-1050 R6 test double: the RuntimeProvider ABC over a mocked ``AsyncSandbox``.

R6 deleted the legacy direct path, so ``_sandbox_agent_impl`` now resolves its
provider through ``_build_dispatch_provider`` and drives create / stream / kill
/ file-I/O / log-tail / isolation through the ABC. The existing unit suite
drives the dispatch with ``patch("e2b.AsyncSandbox.create", ...)`` and asserts
on the mock sandbox's ``files`` / ``commands`` / ``kill`` surface.

:class:`MockSandboxBridge` is the adapter: it implements every ABC method the
dispatch reaches by delegating onto that mock, so the suite exercises the
**provider path** (the dispatch body, the mediated handle, the stream wait)
while keeping the SDK-shaped assertions it already owns. It is a test double -
the real ``E2BRuntimeProvider`` is covered by
``tests/unit/core/runtime_provider/test_e2b*.py``.

**Known gap (found by the R6 slice, reported for a follow-up):** the bridge
forwards ``spec.timeout_seconds`` as the SDK ``timeout`` kwarg so the
FAR-487/489 sandbox-lifetime assertions keep pinning the dispatch -> create
contract. The real ``E2BRuntimeProvider.create_workspace`` does NOT forward
it - it bounds only the provisioning await - so the SDK falls back to its
``default_sandbox_timeout`` of 300s. That must be fixed in
``core/runtime_provider/e2b.py`` before the R5 default flip; ``e2b.py`` is
outside this slice's file allowlist.
"""

from __future__ import annotations

import asyncio
import inspect
import os
from typing import TYPE_CHECKING, Any

# The bridge the autouse fixture installed for the CURRENT test. Tests request
# the ``e2b_bridge`` fixture to inspect ``spec`` / ``events``.
_CURRENT: MockSandboxBridge | None = None

# The real seam bodies, captured before the autouse fixture replaced them, so
# a test that exercises the REAL builder chain can restore it locally.
ORIGINAL_SEAMS: dict[str, Any] = {}


def original_seam(name: str) -> Any:
    """The un-patched ``node_runner`` builder named *name*."""
    if name not in ORIGINAL_SEAMS:  # pragma: no cover - fixture always ran first
        raise RuntimeError(f"no original seam recorded for {name}")
    return ORIGINAL_SEAMS[name]


def current_bridge() -> MockSandboxBridge:
    if _CURRENT is None:  # pragma: no cover - fixture always installs first
        raise RuntimeError("the e2b sandbox bridge fixture has not run")
    return _CURRENT


if TYPE_CHECKING:
    from modulo.core.runtime_provider import (
        ExecProcess,
        IsolationPolicy,
        RuntimeProvider,
        WorkspaceSpec,
    )


class _StreamEnd:
    """Terminal marker for the bridge's ``exec_command_stream`` queue."""

    __slots__ = ("error", "exc", "exit_code")

    def __init__(self, exit_code: int | None, error: str | None, exc: BaseException | None) -> None:
        self.exit_code = exit_code
        self.error = error
        self.exc = exc


def _wrapped_command(command: list[str]) -> str:
    """``["sh" | "bash", "-c", wrapped]`` -> ``wrapped`` (the legacy argv[0]).

    Both shells are accepted: FAR-1470 moved the dispatch to ``bash`` (``sh``
    on the sandbox image is dash, which has no ``pipefail``), but some paths
    still build an ``sh -c`` argv.
    """
    if len(command) == 3 and command[0] in ("sh", "bash") and command[1] == "-c":
        return command[2]
    return " ".join(command)


class MockSandboxBridge:
    """ABC implementation backed by the test's mocked ``AsyncSandbox``."""

    provider_id = "e2b"
    provider_aliases: frozenset[str] = frozenset()

    def __init__(self) -> None:
        self.sandbox: Any = None
        self.spec: Any = None
        self.events: list[str] = []
        self._stream_waiters: set[asyncio.Task[Any]] = set()

    # -- plumbing ---------------------------------------------------------
    def _sbx(self) -> Any:
        if self.sandbox is None:
            raise RuntimeError("MockSandboxBridge: create_workspace has not run yet")
        return self.sandbox

    def attach(self, sandbox: Any) -> MockSandboxBridge:
        """Drive *sandbox* directly (for unit tests that build a watchdog
        without going through ``create_workspace``)."""
        self.sandbox = sandbox
        return self

    # -- create -----------------------------------------------------------
    async def create_workspace(self, spec: WorkspaceSpec) -> str:
        """Call the (test-patched) ``AsyncSandbox.create`` and keep the handle.

        Kwargs mirror what ``E2BRuntimeProvider.create_workspace`` hands the
        SDK: template, provider-owned credential, metadata. ``MODULO_SCHEMA_DIR``
        deliberately is NOT passed here - FAR-1050 R4 routes it through the
        agent command's environment instead.

        ``timeout=spec.timeout_seconds`` forwards the dispatch's sandbox
        lifetime (FAR-487/489: node timeout + grace, an int) to the SDK's
        ``NewSandbox.timeout``. See the module docstring for why the bridge
        forwards it explicitly.
        """
        from e2b import AsyncSandbox

        from modulo.core.runtime_config.key_bridge import get_e2b_api_key

        self.spec = spec
        self.events.append("create")
        sandbox = await AsyncSandbox.create(
            template=spec.image_ref or "base",
            api_key=get_e2b_api_key() or os.environ.get("E2B_API_KEY"),
            timeout=int(spec.timeout_seconds),
            allow_internet_access=spec.egress_policy is None,
            metadata=dict(spec.workspace_metadata) or None,
        )
        self.sandbox = sandbox
        if sandbox is None:
            # No handle: the dispatch's post-create invariant must fire.
            return ""
        ref = getattr(sandbox, "sandbox_id", None)
        return ref if isinstance(ref, str) and ref else "sbx-bridge"

    # -- buffered exec (T10 workspace inputs / drift) -----------------------
    async def exec_command(
        self,
        provider_ref: str,
        command: list[str],
        *,
        cmd_timeout: int | None = None,
    ) -> Any:
        from modulo.core.runtime_provider import ExecResult

        self.events.append("exec")
        sandbox = self._sbx()
        handle = await sandbox.commands.run(_wrapped_command(command), timeout=cmd_timeout)
        result = handle
        if inspect.isawaitable(handle):  # pragma: no cover - commands.run is already awaited above
            result = await handle
        waiter = getattr(result, "wait", None)
        if callable(waiter):
            outcome = waiter()
            if inspect.isawaitable(outcome):
                outcome = await outcome
        else:
            outcome = result
        exit_code = getattr(outcome, "exit_code", getattr(result, "exit_code", 0))
        if not isinstance(exit_code, int):
            exit_code = 0
        stdout = getattr(outcome, "stdout", getattr(result, "stdout", "")) or ""
        stderr = getattr(outcome, "stderr", getattr(result, "stderr", "")) or ""
        return ExecResult(
            exit_code=exit_code,
            stdout=stdout if isinstance(stdout, str) else "",
            stderr=stderr if isinstance(stderr, str) else "",
            duration_ms=None,
        )

    # -- streaming exec (T3) ------------------------------------------------
    async def exec_command_stream(
        self,
        provider_ref: str,
        command: list[str],
        *,
        environment: dict[str, str] | None = None,
    ) -> ExecProcess:
        """Start the command on the mock and wrap its handle in an ``ExecProcess``.

        Mirrors ``E2BRuntimeProvider.exec_command_stream``: output callbacks
        feed a queue the dispatch's pump drains, ``done`` fires when
        ``handle.wait()`` resolves. A ``TimeoutError`` from ``wait()`` is a
        *slice* timeout (legacy shield semantics) and is retried; the idle
        window lives in the dispatch's own watchdog, which kills via
        ``ExecProcess.kill`` -> ``handle.kill()``.
        """
        from modulo.core.runtime_provider import ExecProcess, ExecStreamChunk

        self.events.append("stream")
        sandbox = self._sbx()
        queue: asyncio.Queue[Any] = asyncio.Queue()
        delivered = {"stdout": False, "stderr": False}

        async def _on_stdout(chunk: str) -> None:
            delivered["stdout"] = True
            await queue.put(ExecStreamChunk(stream="stdout", data=chunk))

        async def _on_stderr(chunk: str) -> None:
            delivered["stderr"] = True
            await queue.put(ExecStreamChunk(stream="stderr", data=chunk))

        handle = await sandbox.commands.run(
            _wrapped_command(command),
            background=True,
            envs=environment or {},
            on_stdout=_on_stdout,
            on_stderr=_on_stderr,
        )

        process: Any = ExecProcess(chunks=None, kill=None)  # type: ignore[arg-type]
        killed = asyncio.Event()

        async def _chunks() -> Any:
            try:
                while True:
                    item = await queue.get()
                    if isinstance(item, _StreamEnd):
                        if item.exc is not None:
                            process.exit_code = None
                            process.error = str(item.exc)
                            raise item.exc
                        process.exit_code = item.exit_code
                        process.error = item.error
                        return
                    yield item
            finally:
                process.done.set()

        process.chunks = _chunks()

        async def _await_terminal() -> None:
            while not killed.is_set():
                try:
                    result = await handle.wait()
                except asyncio.CancelledError:
                    await queue.put(_StreamEnd(None, None, asyncio.CancelledError()))
                    return
                except TimeoutError:
                    # Legacy slice-timeout semantics: poll again; the dispatch's
                    # idle watchdog owns the stall decision.
                    await asyncio.sleep(0.01)
                    continue
                except Exception as exc:
                    await queue.put(_StreamEnd(None, str(exc)[:500], None))
                    return
                if result is None:
                    # No terminal outcome (the legacy wait returned ``None``):
                    # leave the stream open so the dispatch's idle window
                    # decides, exactly as the legacy helper's stall branch did.
                    return
                exit_code = getattr(result, "exit_code", 0)
                if not isinstance(exit_code, int):
                    exit_code = 0
                stdout = getattr(result, "stdout", "") or ""
                stderr = getattr(result, "stderr", "") or ""
                # The real SDK delivers the stream through the on_stdout /
                # on_stderr callbacks; ``handle.wait()``'s captured copy only
                # matters when the mock never invoked them (the non-redirected
                # legacy capture), so push it as a fallback and never twice.
                if isinstance(stdout, str) and stdout and not delivered["stdout"]:
                    await queue.put(ExecStreamChunk(stream="stdout", data=stdout))
                if isinstance(stderr, str) and stderr and not delivered["stderr"]:
                    await queue.put(ExecStreamChunk(stream="stderr", data=stderr))
                await queue.put(_StreamEnd(exit_code, None, None))
                return

        async def _kill() -> None:
            killed.set()
            self.events.append("stream_kill")
            kill = getattr(handle, "kill", None)
            if callable(kill):
                outcome = kill()
                if inspect.isawaitable(outcome):
                    await outcome
            process.exit_code = process.exit_code if process.exit_code is not None else -1
            process.done.set()

        process._kill = _kill  # type: ignore[attr-defined]

        waiter = asyncio.ensure_future(_await_terminal())
        self._stream_waiters.add(waiter)
        waiter.add_done_callback(lambda _t: self._stream_waiters.discard(waiter))
        return process

    # -- file I/O (T4) -------------------------------------------------------
    async def write_file(self, provider_ref: str, path: str, data: bytes) -> None:
        self.events.append(f"write:{path}")
        content = data.decode("utf-8", errors="replace") if isinstance(data, bytes) else str(data)
        await self._sbx().files.write(path, content)

    async def read_file(self, provider_ref: str, path: str) -> bytes:
        self.events.append(f"read:{path}")
        raw = await self._sbx().files.read(path, format="text")
        if isinstance(raw, bytes):
            return raw
        return str(raw).encode("utf-8")

    async def get_info(self, provider_ref: str, path: str) -> Any:
        from modulo.core.runtime_provider import WorkspaceFileInfo

        self.events.append(f"get_info:{path}")
        info = await self._sbx().files.get_info(path)
        size = getattr(info, "size", 0)
        return WorkspaceFileInfo(path=path, size=int(size) if isinstance(size, int) else 0, is_dir=False)

    async def list_files(self, provider_ref: str, path: str) -> list[str]:
        self.events.append(f"list:{path}")
        entries = await self._sbx().files.list(path=path)
        if not isinstance(entries, list):
            # Preserve the legacy listing coercion (an object carrying a
            # ``files`` collection); a listing whose ``files`` attribute
            # itself blows up propagates to probe-failure handling.
            entries = list(getattr(entries, "files", []) or [])
        paths: list[str] = []
        for entry in entries:
            candidate = getattr(entry, "path", None) or getattr(entry, "name", None)
            if isinstance(candidate, str):
                paths.append(candidate)
        return sorted(paths)

    # -- log tail (T6) -------------------------------------------------------
    async def read_log_tail(self, provider_ref: str, *, max_bytes: int) -> bytes:
        # No network in unit tests: the seam tests patch
        # ``node_runner._read_log_tail_via_provider`` when they need a tail.
        self.events.append("log_tail")
        return b""

    # -- isolation (T7) ------------------------------------------------------
    async def apply_isolation(self, provider_ref: str, spec: WorkspaceSpec, policy: IsolationPolicy) -> None:
        from modulo.core.pipeline_engine.sandbox_policy import apply_sandbox_policy

        self.events.append("isolation")
        await apply_sandbox_policy(
            self._sbx(),
            read_only=policy.read_only,
            git_credentials=policy.git_credentials,
            egress_policy=policy.egress_policy,
            egress_allowlist=policy.egress_allowlist,
            allowed_hosts=policy.allowed_hosts,
            command_timeout=policy.command_timeout,
        )

    # -- teardown -------------------------------------------------------------
    async def destroy_workspace(self, provider_ref: str) -> None:
        self.events.append("destroy")
        await self._kill_mock()

    async def destroy_workspace_by_ref(self, provider_ref: str) -> bool:
        self.events.append("destroy_by_ref")
        await self._kill_mock()
        return True

    async def _kill_mock(self) -> None:
        sandbox = self.sandbox
        if sandbox is None:
            return
        kill = getattr(sandbox, "kill", None)
        if callable(kill):
            # Mirrors ``E2BRuntimeProvider._kill_sandbox_best_effort``: no
            # ``request_timeout`` kwarg, so a teardown kill can never be
            # mistaken for the resource-cap killer's explicit 10s kill.
            outcome = kill()
            if inspect.isawaitable(outcome):
                await outcome

    async def get_workspace_status(self, provider_ref: str) -> str:
        return "running"

    async def close(self) -> None:
        # The dispatch already tore the sandbox down; a bridge has no hub of
        # tracked workspaces to dispose.
        self.events.append("close")


def install_bridge(monkeypatch: Any, bridge: RuntimeProvider | None = None) -> MockSandboxBridge:
    """Route all four node_runner provider seams at one shared bridge instance.

    Tests that install their own fake (``install_fake_dispatch``,
    ``fake_file_io``) or that patch a seam in the test body still win - their
    ``monkeypatch`` calls run after this fixture.
    """
    global _CURRENT

    from modulo.core.pipeline_engine import node_runner as nr

    shared = bridge if bridge is not None else MockSandboxBridge()
    _CURRENT = shared

    async def _seam(*_args: Any, **_kwargs: Any) -> Any:
        return shared

    for name in (
        "_build_dispatch_provider",
        "_build_file_io_provider",
        "_build_log_tail_provider",
        "_build_isolation_provider",
    ):
        ORIGINAL_SEAMS.setdefault(name, getattr(nr, name))
        monkeypatch.setattr(nr, name, _seam)
    return shared


__all__ = ["ORIGINAL_SEAMS", "MockSandboxBridge", "current_bridge", "install_bridge", "original_seam"]
