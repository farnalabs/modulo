"""LocalRuntimeProvider — in-process agent execution with a concurrency cap.

This provider runs commands as subprocesses on the host machine. It is **not
sandboxed** — agents have full access to the filesystem, network, and
environment of the host process. Suitable for:
  - Solo dev / demo deployments (Fly.io, Railway, single VPS)
  - Proving out pipelines before investing in scaled infra
  - Local development where Docker isn't available

Concurrency is capped by a module-level ``asyncio.Semaphore`` (default 2)
configurable via ``MODULO_MAX_LOCAL_CONCURRENCY``.

**When you outgrow it:** add an E2B API key (or any other RuntimeProvider)
and your pipelines continue to work unchanged — the RuntimeProvider ABC
hides the backend.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tempfile
import time
import uuid
from typing import Any

from modulo.core.runtime_provider import ExecResult, RuntimeProvider, WorkspaceSpec

_log = logging.getLogger(__name__)

_DEFAULT_CMD_TIMEOUT = 300
_GRACE_KILL_TIMEOUT = 5


class LocalRuntimeProvider(RuntimeProvider):
    """Run agent commands as subprocesses on the host, with a concurrency cap.

    Workspaces are temp directories on the host filesystem. The concurrency
    semaphore limits how many ``exec_command`` calls may run simultaneously
    across all workspaces — agents waiting on the semaphore are queued.
    """

    provider_id = "local"

    def __init__(self, max_concurrency: int = 2) -> None:
        self._max_concurrency = max_concurrency
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._workspaces: dict[str, str] = {}

    def supports(self, profile: Any) -> bool:
        """Return True for profiles with ``provider_hint=local`` or no E2B hint."""
        hint = getattr(profile, "provider_hint", None) or ""
        if hint.lower() == "local":
            return True
        if hint.lower() == "e2b":
            return False
        image_ref = getattr(profile, "image_ref", None) or ""
        return "e2b" not in image_ref.lower()

    async def create_workspace(self, spec: WorkspaceSpec) -> str:
        """Create a temp directory on the host as the workspace root.

        If ``spec.labels`` contains ``repo_url``, clone the repo into the
        workspace directory. If the clone fails, the temp directory is
        cleaned up before propagating the error.
        """
        try:
            workspace_dir = tempfile.mkdtemp(prefix=f"modulo-workspace-{spec.environment_profile_id}-")
        except OSError as exc:
            raise RuntimeError(
                f"Failed to create workspace temp directory for profile {spec.environment_profile_id}: {exc}"
            ) from exc
        ref = str(uuid.uuid4())

        try:
            repo_url = (spec.labels or {}).get("repo_url", "")
            if repo_url:
                await self._run_command(
                    ["git", "clone", repo_url, "."],
                    cwd=workspace_dir,
                    cmd_timeout=spec.timeout_seconds,
                )
        except (asyncio.CancelledError, Exception):
            await asyncio.to_thread(shutil.rmtree, workspace_dir, ignore_errors=True)
            raise

        self._workspaces[ref] = workspace_dir
        return ref

    async def exec_command(
        self,
        provider_ref: str,
        command: list[str],
        *,
        cmd_timeout: int | None = None,
    ) -> ExecResult:
        cwd = self._workspaces.get(provider_ref)
        if cwd is None:
            raise ValueError(f"Unknown workspace: {provider_ref}")
        return await self._run_command(command, cwd=cwd, cmd_timeout=cmd_timeout)

    async def destroy_workspace(self, provider_ref: str) -> None:
        """Remove the workspace temp directory."""
        workspace_dir = self._workspaces.pop(provider_ref, None)
        if workspace_dir is None:
            return
        try:
            await asyncio.to_thread(shutil.rmtree, workspace_dir, ignore_errors=True)
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("Failed to remove workspace %s", provider_ref)

    async def get_workspace_status(self, provider_ref: str) -> str:
        return "running" if provider_ref in self._workspaces else "terminated"

    async def _run_command(
        self,
        command: list[str],
        cwd: str,
        cmd_timeout: int | None = None,
    ) -> ExecResult:
        """Run a command, respecting the concurrency semaphore."""
        effective_timeout = cmd_timeout if cmd_timeout is not None else _DEFAULT_CMD_TIMEOUT

        async with self._semaphore:
            start = time.monotonic()
            try:
                proc = await asyncio.create_subprocess_exec(
                    *command,
                    cwd=cwd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            except (FileNotFoundError, PermissionError) as exc:
                duration = int((time.monotonic() - start) * 1000)
                return ExecResult(
                    exit_code=-1,
                    stdout="",
                    stderr=f"Failed to start process: {exc}",
                    duration_ms=duration,
                )

            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=effective_timeout,
                )
            except TimeoutError:
                proc.kill()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=_GRACE_KILL_TIMEOUT)
                except TimeoutError:
                    _log.warning("Process did not exit after kill, detaching")
                duration = int((time.monotonic() - start) * 1000)
                return ExecResult(
                    exit_code=-1,
                    stdout="",
                    stderr="Command timed out",
                    duration_ms=duration,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                proc.kill()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=_GRACE_KILL_TIMEOUT)
                except TimeoutError:
                    _log.warning("Process did not exit after signal, detaching")
                duration = int((time.monotonic() - start) * 1000)
                _log.exception("exec_command failed")
                return ExecResult(
                    exit_code=-1,
                    stdout="",
                    stderr="Command execution failed",
                    duration_ms=duration,
                )

            duration = int((time.monotonic() - start) * 1000)
            return ExecResult(
                exit_code=proc.returncode if proc.returncode is not None else -1,
                stdout=stdout.decode("utf-8", errors="replace") if stdout else "",
                stderr=stderr.decode("utf-8", errors="replace") if stderr else "",
                duration_ms=duration,
            )


def create_local_provider_from_env() -> LocalRuntimeProvider:
    """Build a LocalRuntimeProvider configured from environment variables.

    Reads ``MODULO_MAX_LOCAL_CONCURRENCY`` (default 2) as the concurrency cap.
    """
    raw = os.environ.get("MODULO_MAX_LOCAL_CONCURRENCY", "2")
    try:
        max_concurrency = max(1, int(raw))
    except ValueError:
        _log.warning("Invalid MODULO_MAX_LOCAL_CONCURRENCY value '%s', falling back to 2", raw)
        max_concurrency = 2
    return LocalRuntimeProvider(max_concurrency=max_concurrency)
