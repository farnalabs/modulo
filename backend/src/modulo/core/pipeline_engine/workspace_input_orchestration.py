"""Workspace input orchestration for sandbox_agent nodes (FAR-800).

This module owns the runtime orchestration of managed workspace inputs —
resolving refs → SHA *host-side* before sandbox creation, provisioning
clones inside the sandbox, and detecting drift after the agent command.

Responsibilities:

1. **Host-side ref resolution**: parse ``git ls-remote`` output and resolve
   each input's movable ref (branch/tag) to a commit SHA before any
   sandbox is created.  A resolution failure never creates a sandbox.
2. **Credential resolution**: resolve clone credentials from connector
   instances on the host (the connector store is the source of truth,
   supporting token rotation).
3. **In-sandbox provisioning**: build and run clone scripts inside the
   sandbox (credential setup → clone → credential teardown).
4. **Drift detection**: after the agent command, compare each input's
   ``git rev-parse HEAD`` against the resolved SHA.

Dependencies: stdlib + existing project imports only (no new deps).

Transient vs permanent error classification:

- ``ConnectionError``, ``TimeoutError``, ``OSError`` → TRANSIENT (retry
  with backoff); these are network-level failures that may resolve on retry.
- ``CredentialResolutionError`` (connector not found / decrypt failure) →
  PERMANENT; re-resolving will not fix a missing or broken connector.
- ``RefResolutionError`` (ref not found in ls-remote output) → PERMANENT;
  the ref does not exist on the remote.
"""

from __future__ import annotations

import asyncio
import logging
import shlex
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ResolvedInput:
    """A workspace input whose ref has been resolved to a commit SHA.

    Produced by :func:`resolve_managed_inputs_host_side` and consumed by
    :func:`provision_workspace_inputs_in_sandbox`.
    """

    url: str
    dest: str
    resolved_sha: str
    connector_instance_id: Any | None = None
    credential_setup_script: str = ""
    credential_teardown_script: str = ""


@dataclass(frozen=True, slots=True)
class DriftResult:
    """Post-agent drift detection result for a single workspace input."""

    dest: str
    expected_sha: str
    final_sha: str
    drift_detected: bool


@dataclass(frozen=True, slots=True)
class ProvisioningError(Exception):
    """A workspace input provisioning failure that must kill the sandbox.

    Carries an ``error_code`` for structured failure classification
    and a ``retryable`` flag for transient-vs-permanent distinction.
    """

    message: str
    error_code: str = "sandbox.input_resolution_failed"
    retryable: bool = False


# ---------------------------------------------------------------------------
# Transient error classification
# ---------------------------------------------------------------------------

# Error types that indicate a transient network failure (retry with backoff).
_TRANSIENT_ERROR_TYPES: tuple[type[Exception], ...] = (
    ConnectionError,
    TimeoutError,
    OSError,
)


def _is_transient_error(exc: Exception) -> bool:
    """Return True when *exc* indicates a transient network failure."""
    return isinstance(exc, _TRANSIENT_ERROR_TYPES)


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------


def _extract_host_from_url(url: str) -> str:
    """Extract the hostname from a git URL (https://, ssh://, git@...)."""
    if url.startswith("git@"):
        # SCP-style: git@github.com:org/repo.git → github.com
        return url[4:].split(":", maxsplit=1)[0]
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if not host:
        raise ValueError(f"Cannot extract host from URL: {url!r}")
    return host


def _is_sha(value: str) -> bool:
    """Return True when *value* looks like a hex SHA (40 or 64 chars)."""
    stripped = value.strip()
    if len(stripped) not in (40, 64):
        return False
    return all(c in "0123456789abcdefABCDEF" for c in stripped)


# ---------------------------------------------------------------------------
# Host-side ref resolution
# ---------------------------------------------------------------------------


async def _run_git_ls_remote(url: str) -> str:
    """Run ``git ls-remote <url>`` and return the raw output.

    Raises ``ConnectionError`` / ``TimeoutError`` on transient network
    failures (classified by the caller).
    """
    proc = await asyncio.create_subprocess_exec(
        "git",
        "ls-remote",
        url,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        err_msg = (stderr or b"").decode("utf-8", errors="replace").strip()
        raise ConnectionError(f"git ls-remote failed for {url!r} (exit {proc.returncode}): {err_msg}")
    return stdout.decode("utf-8", errors="replace")


async def _resolve_ref_with_retry(
    url: str,
    kind: str,
    value: str,
    *,
    max_retries: int = 2,
) -> str:
    """Resolve a movable ref via ``git ls-remote`` with transient-error retry.

    Returns the resolved commit SHA.  Retries on transient network errors
    (``ConnectionError``, ``TimeoutError``, ``OSError``); permanent errors
    (ref not found, unknown kind) propagate immediately.
    """
    from modulo.core.pipeline_engine.workspace_inputs import (
        RefResolutionError,
        parse_ls_remote,
        resolve_movable_ref,
    )

    # SHA refs resolve to themselves — no ls-remote needed.
    if kind == "sha":
        return value

    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            raw_output = await _run_git_ls_remote(url)
            refs = parse_ls_remote(raw_output)
            return resolve_movable_ref(refs, kind, value)
        except RefResolutionError:
            raise  # permanent — ref not found
        except Exception as exc:
            last_exc = exc
            if not _is_transient_error(exc):
                raise  # permanent — non-network failure
            if attempt < max_retries:
                _backoff = 0.5 * (2**attempt)
                logger.warning(
                    "workspace_input.ls_remote_retry",
                    extra={"url": url, "attempt": attempt + 1, "backoff": _backoff},
                )
                await asyncio.sleep(_backoff)
    # Exhausted retries — raise the last transient error.
    raise last_exc  # type: ignore[misc]


async def resolve_managed_inputs_host_side(
    workspace_inputs: list[dict[str, Any]],
    *,
    org_id: str,
    session_factory: Any | None = None,
    max_retries: int = 2,
) -> list[ResolvedInput]:
    """Resolve all managed workspace inputs HOST-SIDE before sandbox creation.

    For each input:

    1. If ``ref.kind`` is ``"sha"``, use the value directly (no ls-remote).
    2. Otherwise, run ``git ls-remote <url>`` and resolve the ref to a SHA.
    3. If a ``connector_instance_id`` is present, resolve the clone credential
       from the connector store (host-side DB read).

    Raises :class:`ProvisioningError` on any failure.  The error carries a
    structured ``error_code`` and ``retryable`` flag so the caller can
    classify transient-vs-permanent and fail CLOSED without creating a
    sandbox.

    Credentials are re-read at clone time inside the sandbox (the snapshot
    stores the connector instance id only — supports rotation).
    """
    from modulo.core.pipeline_engine.workspace_input_credentials import (
        CredentialResolutionError,
        resolve_clone_credential,
    )
    from modulo.core.pipeline_engine.workspace_inputs import RefResolutionError

    resolved: list[ResolvedInput] = []
    for inp in workspace_inputs:
        url: str = inp.get("url", "")
        dest: str = inp.get("dest", "")
        ref: dict[str, Any] | None = inp.get("ref")
        connector_id_raw: Any = inp.get("connector_instance_id")

        if not url:
            raise ProvisioningError(
                f"workspace_input dest={dest!r} has no url — "
                "connector-backed inputs without a URL are not yet supported",
                error_code="sandbox.input_resolution_failed",
                retryable=False,
            )

        # --- (1) Resolve ref → SHA ---
        ref_kind: str = (ref or {}).get("kind", "branch")
        ref_value: str = (ref or {}).get("value", "main")

        if ref_kind == "sha" and _is_sha(ref_value):
            resolved_sha = ref_value.strip()
        else:
            try:
                resolved_sha = await _resolve_ref_with_retry(
                    url,
                    ref_kind,
                    ref_value,
                    max_retries=max_retries,
                )
            except RefResolutionError as exc:
                raise ProvisioningError(
                    f"workspace_input dest={dest!r}: ref resolution failed: {exc}",
                    error_code="sandbox.input_resolution_failed",
                    retryable=False,
                ) from exc
            except Exception as exc:
                if _is_transient_error(exc):
                    raise ProvisioningError(
                        f"workspace_input dest={dest!r}: transient network error during ref resolution: {exc}",
                        error_code="sandbox.input_resolution_failed",
                        retryable=True,
                    ) from exc
                raise ProvisioningError(
                    f"workspace_input dest={dest!r}: unexpected error during ref resolution: {exc}",
                    error_code="sandbox.input_resolution_failed",
                    retryable=False,
                ) from exc

        # --- (2) Resolve clone credential (host-side) ---
        cred_setup = ""
        cred_teardown = ""
        host = _extract_host_from_url(url)

        if session_factory is not None and connector_id_raw is not None:
            import uuid as _uuid

            connector_instance_id = (
                _uuid.UUID(str(connector_id_raw)) if not isinstance(connector_id_raw, _uuid.UUID) else connector_id_raw
            )
            try:
                async with session_factory() as session, session.begin():
                    cred = await resolve_clone_credential(
                        session,
                        connector_instance_id=connector_instance_id,
                        host=host,
                    )
            except CredentialResolutionError as exc:
                raise ProvisioningError(
                    f"workspace_input dest={dest!r}: credential resolution failed: {exc}",
                    error_code="sandbox.input_credential_failed",
                    retryable=False,
                ) from exc
            except Exception as exc:
                if _is_transient_error(exc):
                    raise ProvisioningError(
                        f"workspace_input dest={dest!r}: transient error during credential resolution: {exc}",
                        error_code="sandbox.input_credential_failed",
                        retryable=True,
                    ) from exc
                raise ProvisioningError(
                    f"workspace_input dest={dest!r}: unexpected error during credential resolution: {exc}",
                    error_code="sandbox.input_credential_failed",
                    retryable=False,
                ) from exc

            if cred is not None:
                from modulo.core.pipeline_engine.workspace_input_credentials import (
                    build_provisioning_credential_scripts,
                )

                cred_setup, cred_teardown = build_provisioning_credential_scripts(
                    cred=cred,
                    host=host,
                )

        resolved.append(
            ResolvedInput(
                url=url,
                dest=dest,
                resolved_sha=resolved_sha,
                connector_instance_id=connector_id_raw,
                credential_setup_script=cred_setup,
                credential_teardown_script=cred_teardown,
            )
        )

    return resolved


# ---------------------------------------------------------------------------
# In-sandbox provisioning
# ---------------------------------------------------------------------------


async def provision_workspace_inputs_in_sandbox(
    sandbox: Any,
    resolved_inputs: list[ResolvedInput],
    *,
    command_timeout: float = 120.0,
) -> None:
    """Clone all resolved workspace inputs inside the sandbox.

    Executes in order: credential setup → clone → credential teardown.
    A clone failure RAISES :class:`ProvisioningError` — the caller must
    kill the sandbox (the agent command must never run on partial provision).

    The clone scripts are built from the resolved SHAs (not ref names) so a
    force-push between resolution and clone cannot drift the checkout.
    """
    from modulo.core.pipeline_engine.workspace_inputs import build_input_clone_script

    for inp in resolved_inputs:
        # (a) Credential setup
        if inp.credential_setup_script:
            try:
                await asyncio.wait_for(
                    asyncio.shield(
                        sandbox.commands.run(
                            inp.credential_setup_script,
                            timeout=command_timeout,
                        )
                    ),
                    timeout=command_timeout,
                )
            except Exception as exc:
                raise ProvisioningError(
                    f"workspace_input dest={inp.dest!r}: credential setup failed in sandbox: {exc}",
                    error_code="sandbox.input_credential_failed",
                    retryable=False,
                ) from exc

        # (b) Clone with resolved SHA
        clone_script = build_input_clone_script(
            url=inp.url,
            dest=inp.dest,
            resolved_sha=inp.resolved_sha,
        )
        try:
            await asyncio.wait_for(
                asyncio.shield(
                    sandbox.commands.run(
                        clone_script,
                        timeout=command_timeout,
                    )
                ),
                timeout=command_timeout,
            )
        except Exception as exc:
            raise ProvisioningError(
                f"workspace_input dest={inp.dest!r}: clone failed in sandbox: {exc}",
                error_code="sandbox.input_checkout_failed",
                retryable=False,
            ) from exc

        # (c) Credential teardown
        if inp.credential_teardown_script:
            try:
                await asyncio.wait_for(
                    asyncio.shield(
                        sandbox.commands.run(
                            inp.credential_teardown_script,
                            timeout=command_timeout,
                        )
                    ),
                    timeout=command_timeout,
                )
            except Exception as exc:
                # Teardown failure is best-effort — log and continue.
                logger.warning(
                    "workspace_input.teardown_failed",
                    extra={"dest": inp.dest, "error": str(exc)},
                )


# ---------------------------------------------------------------------------
# Post-agent drift detection
# ---------------------------------------------------------------------------


async def detect_workspace_input_drift(
    sandbox: Any,
    resolved_inputs: list[ResolvedInput],
    *,
    command_timeout: float = 30.0,
) -> list[DriftResult]:
    """Detect whether any workspace input's HEAD drifted from the resolved SHA.

    After the agent command, run ``git rev-parse HEAD`` in each input's
    destination directory.  Compare against the resolved SHA.

    Returns a list of :class:`DriftResult` — one per input.  A failure
    in drift detection itself is logged and does NOT raise (the audit
    write MUST still occur — the caller wraps this in a try/except).

    If *resolved_inputs* is empty, returns an empty list (no-op).
    """
    if not resolved_inputs:
        return []

    results: list[DriftResult] = []
    for inp in resolved_inputs:
        try:
            check_script = f"#!/bin/sh\ngit -C {shlex.quote(inp.dest)} rev-parse HEAD\n"
            proc = await asyncio.wait_for(
                asyncio.shield(
                    sandbox.commands.run(
                        check_script,
                        timeout=command_timeout,
                    )
                ),
                timeout=command_timeout,
            )
            stdout = (getattr(proc, "stdout", "") or "").strip()
            # Extract the SHA from the first line of stdout.
            final_sha = stdout.splitlines()[0].strip() if stdout else ""
            drift_detected = final_sha != inp.resolved_sha
            results.append(
                DriftResult(
                    dest=inp.dest,
                    expected_sha=inp.resolved_sha,
                    final_sha=final_sha,
                    drift_detected=drift_detected,
                )
            )
        except Exception as exc:
            # Drift detection failure is best-effort — log and report
            # unknown drift (the audit write MUST still occur).
            logger.warning(
                "workspace_input.drift_detection_failed",
                extra={"dest": inp.dest, "error": str(exc)},
            )
            results.append(
                DriftResult(
                    dest=inp.dest,
                    expected_sha=inp.resolved_sha,
                    final_sha="",
                    drift_detected=True,  # fail-closed: unknown = drift
                )
            )

    return results
