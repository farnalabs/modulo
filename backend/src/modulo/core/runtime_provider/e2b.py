"""E2B RuntimeProvider — sandboxed execution environments via E2B.

Usage:
    provider = E2BRuntimeProvider(api_key="...")
    ref = await provider.create_workspace(spec)
    result = await provider.exec_command(ref, ["python3", "--version"])
    await provider.destroy_workspace(ref)
"""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
from typing import Any

from modulo.core.runtime_provider import ExecResult, RuntimeProvider, WorkspaceSpec

_log = logging.getLogger(__name__)


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
        from e2b import Sandbox

        template_id = spec.image_ref.strip() if spec.image_ref else "default"

        sandbox = await asyncio.to_thread(
            lambda: Sandbox(api_key=self._api_key, template=template_id),  # type: ignore[call-arg]
        )

        self._sandboxes[sandbox.id] = sandbox  # type: ignore[attr-defined]

        repo_url = spec.labels.get("repo_url", "")
        if repo_url:
            await self._clone_repo(sandbox, repo_url, spec.labels)

        return sandbox.id  # type: ignore[no-any-return,attr-defined]

    async def exec_command(
        self,
        provider_ref: str,
        command: list[str],
        *,
        timeout: int | None = None,  # noqa: ASYNC109
    ) -> ExecResult:
        """Execute a shell command inside the sandbox.

        The command is run via the E2B process API. *timeout* is in seconds.
        """
        sandbox = self._get_sandbox(provider_ref)
        cmd_str = " ".join(shlex.quote(c) for c in command)

        proc = await asyncio.to_thread(
            sandbox.process.start_and_wait,
            cmd_str,
            timeout=timeout or 60,
        )

        return ExecResult(
            exit_code=getattr(proc, "exit_code", 0) or 0,
            stdout=getattr(proc, "stdout", "") or "",
            stderr=getattr(proc, "stderr", "") or "",
        )

    async def destroy_workspace(self, provider_ref: str) -> None:
        """Kill the sandbox and release all resources.

        Best-effort: if the kill request fails (e.g. sandbox already
        terminated) the error is logged and swallowed.
        """
        sandbox = self._sandboxes.pop(provider_ref, None)
        if sandbox is not None:
            try:
                await asyncio.to_thread(sandbox.kill)
            except Exception:
                _log.exception("Failed to kill E2B sandbox %s", provider_ref)

    async def get_workspace_status(self, provider_ref: str) -> str:
        """Return the current status of the sandbox."""
        sandbox = self._get_sandbox(provider_ref)
        try:
            if hasattr(sandbox, "get_info"):
                info = await asyncio.to_thread(sandbox.get_info)
                return getattr(info, "status", "running")
        except Exception:
            _log.debug("Failed to get info for sandbox %s", provider_ref)
        return "running"

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_sandbox(self, provider_ref: str) -> Any:
        sandbox = self._sandboxes.get(provider_ref)
        if sandbox is None:
            raise ValueError(f"Unknown sandbox: {provider_ref}")
        return sandbox

    async def _clone_repo(self, sandbox: Any, repo_url: str, labels: dict[str, str]) -> None:
        """Clone a git repository inside the sandbox."""
        repo_ref = labels.get("repo_ref", "main")
        cmds = [f"git clone {shlex.quote(repo_url)} /home/user/repo"]
        if repo_ref != "main":
            cmds.append(f"cd /home/user/repo && git checkout {shlex.quote(repo_ref)}")
        combined = " && ".join(cmds)
        result = await asyncio.to_thread(sandbox.commands.run, combined)
        exit_code = getattr(result, "exit_code", None)
        if exit_code is not None and exit_code != 0:
            stderr = getattr(result, "stderr", "") or ""
            _log.warning("Repo clone exited %d for %s: %s", exit_code, repo_url, stderr)
