"""E2B RuntimeProvider — sandboxed execution environments via E2B."""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
import time
from typing import Any

from modulo.core.runtime_provider import ExecResult, RuntimeProvider, WorkspaceSpec

_log = logging.getLogger(__name__)

_DEFAULT_TEMPLATE_ID = "base"
_DEFAULT_CMD_TIMEOUT = 60
_REPO_CLONE_TIMEOUT = 120
_MAX_PROVISION_TIMEOUT = 120
_KILL_TIMEOUT = 30


class E2BRuntimeProvider(RuntimeProvider):
    """RuntimeProvider backed by E2B sandboxes.

    Each workspace is an E2B sandbox created from an EnvironmentProfile's
    ``image_ref`` (used as the E2B template ID).

    The E2B API key is resolved in this order:
    1. ``api_key`` argument passed to the constructor
    2. ``MODULO_E2B_API_KEY`` environment variable

    To store per-organisation keys securely, use ``FernetSecretsBackend`` at
    the service layer and pass the resolved key to the constructor.
    """

    provider_id = "e2b"

    def __init__(self, api_key: str | None = None) -> None:
        self._api_key = api_key or os.environ.get("MODULO_E2B_API_KEY")
        if not self._api_key:
            raise ValueError("E2B API key is required. Pass api_key= or set MODULO_E2B_API_KEY.")
        self._sandboxes: dict[str, Any] = {}

    # ------------------------------------------------------------------
    # Hub integration
    # ------------------------------------------------------------------

    def supports(self, profile: Any) -> bool:
        """Return True if the profile hints at E2B."""
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

        The template ID is taken from ``spec.image_ref``. If the spec's
        ``labels`` dict contains ``repo_url`` the repository is cloned into
        ``/home/user/repo`` and optionally checked out to ``repo_ref``.
        """
        from e2b import AsyncSandbox

        template_id = spec.image_ref.strip() if spec.image_ref else _DEFAULT_TEMPLATE_ID
        timeout = spec.timeout_seconds or _MAX_PROVISION_TIMEOUT

        try:
            sandbox = await asyncio.wait_for(
                AsyncSandbox.create(template=template_id),
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

        repo_url = (spec.labels or {}).get("repo_url", "")
        if repo_url:
            try:
                await self._clone_repo(sandbox, repo_url, spec.labels or {})
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

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_sandbox(self, provider_ref: str) -> Any:
        sandbox = self._sandboxes.get(provider_ref)
        if sandbox is None:
            raise ValueError(f"Unknown sandbox: {provider_ref}")
        return sandbox

    async def _kill_sandbox_best_effort(self, sandbox: Any, context: str) -> None:
        """Kill a sandbox during error cleanup without masking the cause.

        Logs and swallows kill failures (including a ``TimeoutError`` from the
        ``wait_for`` wrapper) so the caller can always re-raise the original
        exception that triggered cleanup.
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

    async def _clone_repo(self, sandbox: Any, repo_url: str, labels: dict[str, str]) -> None:
        """Clone a git repository inside the sandbox.

        Raises RuntimeError if the clone or checkout fails.
        """
        repo_ref = labels.get("repo_ref", "")
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
