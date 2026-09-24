"""E2B RuntimeProvider — sandboxed execution environments via E2B."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import time
import urllib.request
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from modulo.core.runtime_config.key_bridge import get_e2b_api_key
from modulo.core.runtime_provider import (
    ExecProcess,
    ExecResult,
    ExecStreamChunk,
    RuntimeProvider,
    WorkspaceSpec,
)
from modulo.core.runtime_provider.log_tail import combine_log_entries

_log = logging.getLogger(__name__)

_DEFAULT_TEMPLATE_ID = "base"
_DEFAULT_CMD_TIMEOUT = 60
_REPO_CLONE_TIMEOUT = 120
_MAX_PROVISION_TIMEOUT = 120
_KILL_TIMEOUT = 30
# Background/streaming commands have no per-command deadline: the dispatch
# layer owns stall/deadline kills through ExecProcess.kill() (ADR 040 — the
# e2b ``timeout=0`` means "do not limit the command connection time").
_STREAM_CMD_TIMEOUT = 0
# Bound only the START of a streaming command (run() returns the handle once
# the start event arrives; a wedged control plane must not hang the caller —
# semgrep sandbox-commands-run-without-wait-for). Matches node_runner's
# background-command start bound (min(sandbox_timeout, 120)).
_STREAM_START_TIMEOUT = 120
# FAR-1050 R1 log-tail primitive: the api.e2b.app HTTP log-tail call moved
# here from node_runner._fetch_sandbox_log_tail. Entry window + fetch timeout
# + raw-payload fallback mirror the legacy helper byte-for-byte (parity is
# pinned by the content-parity unit test).
_LOG_TAIL_ENTRY_LIMIT = 60
_LOG_TAIL_FETCH_TIMEOUT_S = 8
_LOG_TAIL_RAW_FALLBACK = 4000


@dataclass(frozen=True)
class _StreamEnd:
    """Terminal queue marker for the E2B stream: healthy exit XOR stream error.

    Exactly one field is set (by construction in the waiter): a healthy
    stream end carries the real ``exit_code`` with ``error=None``; an
    engine/proxy drop carries the ``error`` description with
    ``exit_code=None`` — never a fabricated zero (ADR 040 "Streaming
    parity").
    """

    exit_code: int | None
    error: str | None


def _stream_error_message(exc: Exception) -> str:
    """Format an engine/proxy stream failure for ``ExecProcess.error`` (ADR 040)."""
    return f"{type(exc).__name__}: {str(exc)[:200]}"


class E2BRuntimeProvider(RuntimeProvider):
    """RuntimeProvider backed by E2B sandboxes.

    Each workspace is an E2B sandbox created from an EnvironmentProfile's
    ``image_ref`` (used as the E2B template ID).

    The E2B API key is resolved in this order:
    1. ``api_key`` argument passed to the constructor
    2. runtime override / ``MODULO_E2B_API_KEY`` env var
       (``key_bridge.get_e2b_api_key`` — the same bridge the provider-
       registration gate and node-runner enforcement check use, FAR-1159)

    The resolved key is passed to every ``AsyncSandbox.create`` call as
    ``api_key=`` (FAR-1171): without it the E2B SDK would resolve its own
    credential from the legacy ``E2B_API_KEY`` env var, and a runtime-key
    rotation via ``PUT /api/v1/admin/runtime-config`` would change the
    registration gate and the node-runner enforcement check but not the
    credential actually used to provision sandboxes.

    To store per-organisation keys securely, use ``FernetSecretsBackend`` at
    the service layer and pass the resolved key to the constructor.
    """

    provider_id = "e2b"

    def __init__(self, api_key: str | None = None) -> None:
        self._api_key = api_key or get_e2b_api_key()
        if not self._api_key:
            raise ValueError("E2B API key is required. Pass api_key= or set MODULO_E2B_API_KEY.")
        self._sandboxes: dict[str, Any] = {}
        # Strong references to in-flight stream waiter tasks (asyncio holds
        # only weak refs to tasks; RUF006). Discarded as each resolves.
        self._stream_waiters: set[asyncio.Task[None]] = set()

    # ------------------------------------------------------------------
    # Hub integration
    # ------------------------------------------------------------------

    def supports(self, profile: Any) -> bool:
        """Best-effort auto-detection — no longer consulted by the hub (FAR-587)."""
        hint = getattr(profile, "provider_hint", None) or ""
        if hint.lower() == "e2b":
            return True
        image_ref = getattr(profile, "image_ref", None) or ""
        return "e2b" in image_ref.lower()

    # ------------------------------------------------------------------
    # RuntimeProvider interface
    # ------------------------------------------------------------------

    async def create_workspace(self, spec: WorkspaceSpec) -> str:
        """Provision an E2B sandbox and optionally clone a repo.

        The template ID is taken from ``spec.image_ref``. If the first-class
        ``spec.repo_url`` field is set (FAR-595 — previously smuggled through
        the ``labels`` dict) the repository is cloned into ``/home/user/repo``
        and optionally checked out to ``spec.repo_ref``. ``spec.labels`` is
        env-var injection on Docker and is ignored by this provider.

        The spec's provider-neutral ``workspace_metadata`` maps to the E2B
        sandbox ``metadata`` (the SDK's tag-like carrier) when non-empty.
        """
        from e2b import AsyncSandbox

        template_id = spec.image_ref.strip() if spec.image_ref else _DEFAULT_TEMPLATE_ID
        timeout = spec.timeout_seconds or _MAX_PROVISION_TIMEOUT

        # FAR-1171: pass the resolved key explicitly — the SDK's ConnectionConfig
        # only falls back to the E2B_API_KEY env var when api_key is falsy, and
        # ``__init__`` guarantees ``self._api_key`` is non-empty (fail-closed).
        create_kwargs: dict[str, Any] = {"template": template_id, "api_key": self._api_key}
        if spec.workspace_metadata:
            create_kwargs["metadata"] = dict(spec.workspace_metadata)

        try:
            sandbox = await asyncio.wait_for(
                AsyncSandbox.create(**create_kwargs),
                timeout=timeout,
            )
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            raise RuntimeError(
                f"Timed out after {timeout}s provisioning E2B sandbox with template {template_id!r}"
            ) from None
        except Exception as exc:
            _log.exception("Failed to create E2B sandbox with template %s", template_id)
            raise RuntimeError(f"Failed to create E2B sandbox with template {template_id!r}: {exc}") from exc

        repo_url = spec.repo_url
        if repo_url:
            try:
                await self._clone_repo(sandbox, repo_url, spec.repo_ref)
            except asyncio.CancelledError:
                await self._kill_sandbox_best_effort(sandbox, "cancellation cleanup")
                raise
            except Exception:
                await self._kill_sandbox_best_effort(sandbox, "clone-failure cleanup")
                raise

        self._sandboxes[sandbox.sandbox_id] = sandbox

        return str(sandbox.sandbox_id)

    async def exec_command(
        self,
        provider_ref: str,
        command: list[str],
        *,
        cmd_timeout: int | None = None,
    ) -> ExecResult:
        """Execute a shell command inside the sandbox.

        The command is run via the E2B commands API. *timeout* is in seconds.
        """
        sandbox = self._get_sandbox(provider_ref)
        cmd_str = " ".join(shlex.quote(c) for c in command)
        effective_timeout = cmd_timeout if cmd_timeout is not None else _DEFAULT_CMD_TIMEOUT

        start = time.monotonic()
        try:
            proc = await asyncio.wait_for(
                sandbox.commands.run(cmd_str, timeout=effective_timeout),
                timeout=effective_timeout,
            )
            duration = int((time.monotonic() - start) * 1000)
            return ExecResult(
                exit_code=getattr(proc, "exit_code", -1),
                stdout=getattr(proc, "stdout", "") or "",
                stderr=getattr(proc, "stderr", "") or "",
                duration_ms=duration,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            duration = int((time.monotonic() - start) * 1000)
            _log.exception("exec_command failed in sandbox %s", provider_ref)
            return ExecResult(
                exit_code=-1,
                stdout="",
                stderr=f"Command execution failed: {exc}",
                duration_ms=duration,
            )

    async def destroy_workspace(self, provider_ref: str) -> None:
        """Kill the sandbox and release all resources.

        Best-effort: if the kill request fails (e.g. sandbox already
        terminated) the error is logged and swallowed.
        """
        sandbox = self._sandboxes.pop(provider_ref, None)
        if sandbox is not None:
            await self._kill_sandbox_best_effort(sandbox, "destroy cleanup")

    async def destroy_workspace_by_ref(self, provider_ref: str) -> bool:
        """Kill an E2B sandbox given only its sandbox id (ADR 040 primitive).

        Reconnects through ``AsyncSandbox.connect(sandbox_id)`` — the SDK's
        connect-by-id classmethod — rather than consulting
        ``self._sandboxes``, so reclamation works after a process restart
        or from another process (never-tracked-but-live refs included).

        Contract (see :meth:`RuntimeProvider.destroy_workspace_by_ref`):
          - already-gone / foreign ref → ``NotFoundException`` from the
            SDK → idempotent success (``True``), no error;
          - a tracked local handle is dropped only on confirmed-gone —
            on an unconfirmed kill it is kept so ``close()`` can retry;
          - connect/kill failures are logged and swallowed, returning
            ``False`` (the destroy could not be confirmed) — the two-phase
            ``destroy_intent`` / ``confirmed`` marker stays the CALLER's
            concern (delivered in a later slice).
        """
        from e2b import AsyncSandbox
        from e2b.exceptions import NotFoundException

        try:
            sandbox = await AsyncSandbox.connect(provider_ref, api_key=self._api_key)
        except asyncio.CancelledError:
            raise
        except NotFoundException:
            # Already destroyed (or a foreign ref): idempotent no-op success.
            self._sandboxes.pop(provider_ref, None)
            _log.debug("destroy_workspace_by_ref: sandbox %s already gone", provider_ref)
            return True
        except Exception:
            _log.exception(
                "destroy_workspace_by_ref: failed to reconnect to sandbox %s",
                provider_ref,
            )
            return False

        killed = await self._kill_sandbox_best_effort(sandbox, "destroy_workspace_by_ref")
        if killed:
            self._sandboxes.pop(provider_ref, None)
        return killed

    async def read_log_tail(self, provider_ref: str, *, max_bytes: int) -> bytes:
        """Read the E2B logs-endpoint tail for *provider_ref* (ADR 040 primitive).

        FAR-1050 R1: this is the ``api.e2b.app`` HTTP log-tail call moved out
        of ``node_runner._fetch_sandbox_log_tail`` — the legacy helper stays
        in-tree as the flag-OFF path until slice R6 retires it. Fetch and
        parse mirror the legacy helper byte-for-byte (preferred-level
        reordering, the 60-entry window, the ``[-max_bytes:]`` final bound,
        the ``min(4000, max_bytes)`` raw-payload fallback); the content-parity
        unit test pins the two implementations to identical output over the
        same payload.

        Contract (T6): never raises — invalid ref, missing key, network
        failure and parse failure all yield ``b""`` (or the raw fallback for
        parse-level failures), so callers keep the legacy "never raises /
        empty on no key" behaviour. Key resolution keeps the legacy fallback
        chain: runtime override / ``MODULO_E2B_API_KEY`` bridge, then the
        legacy ``E2B_API_KEY`` env var, then the constructor-held key.
        """
        if not isinstance(provider_ref, str) or not provider_ref:
            return b""
        api_key = get_e2b_api_key() or os.environ.get("E2B_API_KEY") or self._api_key
        if not api_key:
            return b""

        def _fetch_bytes() -> bytes:
            _req = urllib.request.Request(
                f"https://api.e2b.app/sandboxes/{provider_ref}/logs?limit={_LOG_TAIL_ENTRY_LIMIT}",
                headers={"X-API-KEY": api_key, "Accept": "application/json"},
            )
            # URL is a hard-coded https endpoint, not caller-controlled.
            with urllib.request.urlopen(_req, timeout=_LOG_TAIL_FETCH_TIMEOUT_S) as _resp:  # noqa: S310  # nosec B310
                return bytes(_resp.read())

        try:
            raw = (await asyncio.to_thread(_fetch_bytes)).decode("utf-8", errors="replace")
        except Exception:
            return b""
        try:
            payload = json.loads(raw)
            entries = payload.get("logEntries") if isinstance(payload, dict) else payload
            if not isinstance(entries, list):
                return raw[: min(_LOG_TAIL_RAW_FALLBACK, max_bytes)].encode("utf-8", errors="replace")
            combined = combine_log_entries(entries, _LOG_TAIL_ENTRY_LIMIT)
            return "\n".join(combined)[-max_bytes:].encode("utf-8", errors="replace")
        except Exception:
            return raw[: min(_LOG_TAIL_RAW_FALLBACK, max_bytes)].encode("utf-8", errors="replace")

    async def exec_command_stream(
        self,
        provider_ref: str,
        command: list[str],
        *,
        environment: dict[str, str] | None = None,
    ) -> ExecProcess:
        """Stream a command's output as async chunks with a kill handle (ADR 040).

        E2B implementation of the streaming-parity primitive: the command is
        started as a background command (``commands.run(..., background=True,
        on_stdout=..., on_stderr=...)`` — the same SDK API the legacy
        node-runner path uses) and its output callbacks feed an internal
        queue that ``process.chunks`` drains.

        Lifecycle contract (mirrors the Docker implementation):
          - decoded :class:`ExecStreamChunk` values on ``chunks``, in order;
          - ``done`` fires when the stream ends (healthy or error) — also on
            an early consumer close (same as Docker);
          - ``exit_code`` stays ``None`` until the END of a HEALTHY stream
            (including a non-zero command exit — a command failure is a
            result, not a stream error);
          - an engine/proxy drop mid-stream (or an end-eventless
            termination) sets ``error`` and leaves ``exit_code`` ``None`` —
            never a fabricated ``exit_code == 0``;
          - ``kill()`` SIGKILLs the background command best-effort
            (failures logged and swallowed); the resulting end event then
            terminates ``chunks``.

        The command connection is unbounded (``timeout=0`` — the SDK's
        "do not limit the command connection time" value): the dispatch
        layer owns stall/deadline kills through :meth:`ExecProcess.kill`,
        exactly as it does for Docker's unbounded exec stream.
        """
        from e2b.sandbox.commands.command_handle import CommandExitException

        sandbox = self._get_sandbox(provider_ref)
        cmd_str = " ".join(shlex.quote(c) for c in command)
        queue: asyncio.Queue[ExecStreamChunk | _StreamEnd] = asyncio.Queue()

        async def _on_stdout(chunk: str) -> None:
            await queue.put(ExecStreamChunk(stream="stdout", data=chunk))

        async def _on_stderr(chunk: str) -> None:
            await queue.put(ExecStreamChunk(stream="stderr", data=chunk))

        handle = await asyncio.wait_for(
            sandbox.commands.run(
                cmd_str,
                background=True,
                envs=environment,
                on_stdout=_on_stdout,
                on_stderr=_on_stderr,
                timeout=_STREAM_CMD_TIMEOUT,
            ),
            timeout=_STREAM_START_TIMEOUT,
        )

        process = ExecProcess(chunks=None, kill=None)  # type: ignore[arg-type]

        async def _chunks() -> AsyncIterator[ExecStreamChunk]:
            exit_code: int | None = None
            error: str | None = None
            try:
                while True:
                    item = await queue.get()
                    if isinstance(item, _StreamEnd):
                        exit_code = item.exit_code
                        error = item.error
                        break
                    yield item
            finally:
                if error is not None:
                    process.error = error
                process.exit_code = exit_code
                process.done.set()

        async def _await_terminal() -> None:
            """Resolve the command once and enqueue exactly one terminal marker.

            The SDK resolves ``wait()`` only after every ``on_stdout``/
            ``on_stderr`` callback has run (they are awaited inside the same
            event-handler task), and the queue is FIFO — so the terminal
            marker always lands after all output chunks.
            """
            try:
                result = await handle.wait()
            except asyncio.CancelledError:
                raise
            except CommandExitException as exc:
                # Non-zero command exit: a HEALTHY stream end carrying the
                # real exit code (specific exception before its bases).
                await queue.put(_StreamEnd(exit_code=exc.exit_code, error=None))
            except Exception as exc:
                # Engine/proxy drop or an end-eventless termination: stream
                # ERROR — exit_code must stay None (no zero-exit fabrication).
                await queue.put(_StreamEnd(exit_code=None, error=_stream_error_message(exc)))
            else:
                await queue.put(_StreamEnd(exit_code=result.exit_code, error=None))

        process.chunks = _chunks()
        # Strong ref while in flight (asyncio holds only weak task refs);
        # discarded automatically when the terminal marker has been queued.
        waiter = asyncio.ensure_future(_await_terminal())
        self._stream_waiters.add(waiter)
        waiter.add_done_callback(lambda _t: self._stream_waiters.discard(waiter))

        async def _kill() -> None:
            try:
                await asyncio.wait_for(handle.kill(), timeout=_KILL_TIMEOUT)
            except asyncio.CancelledError:
                raise
            except Exception:
                _log.exception(
                    "exec_command_stream: failed to kill streamed command in sandbox %s",
                    provider_ref,
                )

        process._kill = _kill
        return process

    async def get_workspace_status(self, provider_ref: str) -> str:
        """Return the current status of the sandbox."""
        sandbox = self._sandboxes.get(provider_ref)
        if sandbox is None:
            return "terminated"
        try:
            running = await sandbox.is_running()
            return "running" if running else "stopped"
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("Failed to get status for sandbox %s", provider_ref)
            return "unknown"

    async def close(self) -> None:
        """Destroy every provider-tracked sandbox best-effort (hub-aclose disposal).

        Mirrors ``DockerRuntimeProvider.close()``: on abnormal teardown (e.g. an
        SSE stream dropped mid-test) the hub still disposes this provider, and
        without destroying tracked sandboxes they would keep billing after the
        hub is gone. Best-effort per sandbox — a kill failure is logged and
        never masks the remaining teardown.
        """
        for provider_ref in tuple(self._sandboxes.keys()):
            try:
                await self.destroy_workspace(provider_ref)
            except asyncio.CancelledError:
                raise
            except Exception:
                _log.exception(
                    "Failed to destroy E2B sandbox %s during close()",
                    provider_ref,
                )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_sandbox(self, provider_ref: str) -> Any:
        sandbox = self._sandboxes.get(provider_ref)
        if sandbox is None:
            raise ValueError(f"Unknown sandbox: {provider_ref}")
        return sandbox

    async def _kill_sandbox_best_effort(self, sandbox: Any, context: str) -> bool:
        """Kill a sandbox during error cleanup without masking the cause.

        Logs and swallows kill failures (including a ``TimeoutError`` from the
        ``wait_for`` wrapper) so the caller can always re-raise the original
        exception that triggered cleanup.

        Returns an outcome for the ADR 040 reclamation path: ``True`` when
        the kill request completed (the sandbox was killed, or the API
        reported it already gone — neither is an error), ``False`` when the
        kill attempt failed (logged + swallowed). Cleanup callers ignore the
        return value, as before.
        """
        try:
            await asyncio.wait_for(sandbox.kill(), timeout=_KILL_TIMEOUT)
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception(
                "Failed to kill E2B sandbox %s during %s",
                getattr(sandbox, "sandbox_id", "<unknown>"),
                context,
            )
            return False
        return True

    async def _clone_repo(self, sandbox: Any, repo_url: str, repo_ref: str) -> None:
        """Clone a git repository inside the sandbox.

        Raises RuntimeError if the clone or checkout fails.
        """
        cmds = [f"git clone {shlex.quote(repo_url)} /home/user/repo"]
        if repo_ref:
            cmds.append(f"cd /home/user/repo && git checkout {shlex.quote(repo_ref)}")
        combined = " && ".join(cmds)
        try:
            result = await asyncio.wait_for(
                sandbox.commands.run(combined, timeout=_REPO_CLONE_TIMEOUT),
                timeout=_REPO_CLONE_TIMEOUT,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise RuntimeError(f"Repo clone failed for {repo_url}: {exc}") from exc
        exit_code = getattr(result, "exit_code", 1)
        if exit_code != 0:
            stderr = getattr(result, "stderr", "") or ""
            raise RuntimeError(f"Repo clone failed (exit {exit_code}) for {repo_url}: {stderr}")
