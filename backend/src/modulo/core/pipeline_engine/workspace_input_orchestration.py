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
import uuid
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Error code constants (S1192)
# ---------------------------------------------------------------------------

_CODE_INPUT_RESOLUTION_FAILED = "sandbox.input_resolution_failed"
_CODE_INPUT_CREDENTIAL_FAILED = "sandbox.input_credential_failed"


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
    error_code: str = _CODE_INPUT_RESOLUTION_FAILED
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
    if not stripped:
        return False
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


async def _derive_url_from_connector(
    session: Any,
    connector_instance_id: uuid.UUID,
) -> str:
    """Derive a git clone URL from a connector instance's stored config.

    Reads the ``ConnectorInstance`` row and extracts the clone URL from its
    ``config_json``.  GitHub connectors are repo-scoped: the row stores the
    repository (``owner/repo``) and an API ``base_url`` in config_json — the
    git clone host is derived from ``base_url`` (``api.github.com`` maps to
    ``github.com``; GitHub Enterprise hosts reuse the same host).

    Raises :class:`ProvisioningError` when the connector is not found, has
    no ``repo`` in its config, or carries an unsupported connector type.
    """
    from sqlalchemy import select

    from modulo.db.models.connector_instance import ConnectorInstance

    result = await session.execute(select(ConnectorInstance).where(ConnectorInstance.id == connector_instance_id))
    ci = result.scalar_one_or_none()
    if ci is None:
        raise ProvisioningError(
            f"connector instance {connector_instance_id} not found — "
            "cannot derive clone URL for connector-backed workspace input",
            error_code=_CODE_INPUT_CREDENTIAL_FAILED,
            retryable=False,
        )

    config: dict[str, Any] = getattr(ci, "config_json", None) or {}

    # Known connector types and how to derive a git clone URL.
    if ci.connector_type_id == "github":
        # GitHub connectors are repo-scoped: the connector row stores the
        # repository (``owner/repo``) and an API base URL in config_json —
        # never an ``html_url``. Derive the git clone host from the API base
        # URL (GitHub.com's API host ``api.github.com`` maps to the git host
        # ``github.com``; GitHub Enterprise hosts use the same host for both).
        repo = str(config.get("repo") or "").strip().rstrip("/")
        if not repo or "/" not in repo:
            raise ProvisioningError(
                f"connector instance {connector_instance_id} (github) has no "
                "'repo' (owner/repo) in its config — cannot derive a clone URL",
                error_code=_CODE_INPUT_RESOLUTION_FAILED,
                retryable=False,
            )
        base_url = str(config.get("base_url") or "https://api.github.com").strip()
        clone_host = _derive_github_clone_host(base_url)
        return f"https://{clone_host}/{repo}.git"

    raise ProvisioningError(
        f"connector type {ci.connector_type_id!r} does not support URL "
        "derivation for workspace inputs — provide an explicit url or "
        "use a connector type with a known clone URL derivation",
        error_code=_CODE_INPUT_RESOLUTION_FAILED,
        retryable=False,
    )


def _derive_github_clone_host(base_url: str) -> str:
    """Map a GitHub connector API base URL to its git clone host.

    GitHub.com's API host (``api.github.com``) is not the git host, so it is
    special-cased to ``github.com``. GitHub Enterprise base URLs (e.g.
    ``https://ghe.example.com/api/v3``) use the same host for both API and
    git, so the netloc is used directly.
    """
    parsed = urlparse(base_url)
    netloc = (parsed.hostname or "api.github.com").lower()
    if netloc == "api.github.com":
        return "github.com"
    return netloc


async def resolve_managed_inputs_host_side(
    workspace_inputs: list[dict[str, Any]],
    *,
    org_id: str,
    session_factory: Any | None = None,
    max_retries: int = 2,
    http_client: Any | None = None,
) -> list[ResolvedInput]:
    """Resolve all managed workspace inputs HOST-SIDE before sandbox creation.

    For each input:

    1. If the input has no ``url`` but has a ``connector_instance_id``, derive
       the clone URL from the connector's stored config (host-side DB read).
    2. If ``ref.kind`` is ``"sha"``, use the value directly (no ls-remote).
    3. Otherwise, run ``git ls-remote <url>`` and resolve the ref to a SHA.
    4. If a ``connector_instance_id`` is present, resolve the clone credential
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

        # --- (0) Derive URL from connector when no explicit URL ---
        if not url:
            if connector_id_raw is None:
                raise ProvisioningError(
                    f"workspace_input dest={dest!r} has no url and no connector_instance_id",
                    error_code=_CODE_INPUT_RESOLUTION_FAILED,
                    retryable=False,
                )
            if session_factory is None:
                raise ProvisioningError(
                    f"workspace_input dest={dest!r} has no url but a connector_instance_id "
                    "requires a session_factory to derive the clone URL",
                    error_code=_CODE_INPUT_RESOLUTION_FAILED,
                    retryable=False,
                )

            connector_instance_id = (
                uuid.UUID(str(connector_id_raw)) if not isinstance(connector_id_raw, uuid.UUID) else connector_id_raw
            )
            try:
                async with session_factory() as session, session.begin():
                    url = await _derive_url_from_connector(session, connector_instance_id)
            except ProvisioningError:
                raise
            except Exception as exc:
                if _is_transient_error(exc):
                    raise ProvisioningError(
                        f"workspace_input dest={dest!r}: transient error deriving URL from connector: {exc}",
                        error_code=_CODE_INPUT_RESOLUTION_FAILED,
                        retryable=True,
                    ) from exc
                raise ProvisioningError(
                    f"workspace_input dest={dest!r}: unexpected error deriving URL from connector: {exc}",
                    error_code=_CODE_INPUT_RESOLUTION_FAILED,
                    retryable=False,
                ) from exc
            if not url:
                raise ProvisioningError(
                    f"workspace_input dest={dest!r}: connector {connector_id_raw} did not provide a clone URL",
                    error_code=_CODE_INPUT_RESOLUTION_FAILED,
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
                    error_code=_CODE_INPUT_RESOLUTION_FAILED,
                    retryable=False,
                ) from exc
            except Exception as exc:
                if _is_transient_error(exc):
                    raise ProvisioningError(
                        f"workspace_input dest={dest!r}: transient network error during ref resolution: {exc}",
                        error_code=_CODE_INPUT_RESOLUTION_FAILED,
                        retryable=True,
                    ) from exc
                raise ProvisioningError(
                    f"workspace_input dest={dest!r}: unexpected error during ref resolution: {exc}",
                    error_code=_CODE_INPUT_RESOLUTION_FAILED,
                    retryable=False,
                ) from exc

        # --- (2) Resolve clone credential (host-side) ---
        cred_setup = ""
        cred_teardown = ""
        host = _extract_host_from_url(url)

        if session_factory is not None and connector_id_raw is not None:
            connector_instance_id = (
                uuid.UUID(str(connector_id_raw)) if not isinstance(connector_id_raw, uuid.UUID) else connector_id_raw
            )
            # FAR-801 tenancy: validate the connector instance belongs to
            # the same org as the run.  An RLS-scoped query against the
            # ConnectorInstance table enforces this; fail CLOSED on
            # mismatch (raise ProvisioningError → sandbox.input_credential_failed).
            try:
                from sqlalchemy import select as _sa_select

                from modulo.db.models.connector_instance import ConnectorInstance

                async with session_factory() as _tenancy_session, _tenancy_session.begin():
                    # FAR-801 tenancy: enforce the connector belongs to THIS org
                    # explicitly (not just via RLS on the caller's session) so the
                    # check is correct regardless of session scope — a system /
                    # bypass-RLS session must NOT admit a cross-org connector.
                    _ci_row = (
                        await _tenancy_session.execute(
                            _sa_select(ConnectorInstance.id).where(
                                ConnectorInstance.id == connector_instance_id,
                                ConnectorInstance.organisation_id == org_id,
                            )
                        )
                    ).scalar_one_or_none()
                    if _ci_row is None:
                        raise ProvisioningError(
                            f"workspace_input dest={dest!r}: connector instance "
                            f"{connector_instance_id} not found in current org "
                            "(tenancy check failed)",
                            error_code="sandbox.input_credential_failed",
                            retryable=False,
                        )
            except ProvisioningError:
                raise
            except Exception as exc:
                if _is_transient_error(exc):
                    raise ProvisioningError(
                        f"workspace_input dest={dest!r}: transient error during tenancy check: {exc}",
                        error_code="sandbox.input_credential_failed",
                        retryable=True,
                    ) from exc
                raise ProvisioningError(
                    f"workspace_input dest={dest!r}: unexpected error during tenancy check: {exc}",
                    error_code="sandbox.input_credential_failed",
                    retryable=False,
                ) from exc

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
                    error_code=_CODE_INPUT_CREDENTIAL_FAILED,
                    retryable=False,
                ) from exc
            except Exception as exc:
                if _is_transient_error(exc):
                    raise ProvisioningError(
                        f"workspace_input dest={dest!r}: transient error during credential resolution: {exc}",
                        error_code=_CODE_INPUT_CREDENTIAL_FAILED,
                        retryable=True,
                    ) from exc
                raise ProvisioningError(
                    f"workspace_input dest={dest!r}: unexpected error during credential resolution: {exc}",
                    error_code=_CODE_INPUT_CREDENTIAL_FAILED,
                    retryable=False,
                ) from exc

            if cred is not None:
                # --- (2a) Assert credential is read-only (least privilege) ---
                # The assertion requires an http_client to probe GitHub token
                # scope.  When no client is provided (e.g. the legacy E2B path),
                # we skip the assertion — callers that provide an http_client
                # enforce the check.  SSH credentials are always accepted by the
                # assertion without a probe.
                from modulo.core.pipeline_engine.workspace_input_credentials import (
                    assert_clone_credential_is_read_only,
                    build_provisioning_credential_scripts,
                )

                if http_client is not None:
                    try:
                        await assert_clone_credential_is_read_only(
                            cred,
                            http_client=http_client,
                        )
                    except CredentialResolutionError as exc:
                        raise ProvisioningError(
                            f"workspace_input dest={dest!r}: credential is not read-only: {exc}",
                            error_code="sandbox.input_credential_failed",
                            retryable=False,
                        ) from exc

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
                    error_code=_CODE_INPUT_CREDENTIAL_FAILED,
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
