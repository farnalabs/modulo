"""RunTeardownSeal — one sealed terminal step that closes a run's credential and
workspace TOGETHER (FAR-1690 A3 / D2, gap G1 — OWASP Q3 control set).

A run can be terminated from several places (the executor's terminalisation; the
compensating sweeps for non-executor terminalisers). Today the credential revoke
and the workspace destroy are separate, uncoordinated, best-effort steps, so a
workspace can outlive a revoke and vice-versa. This module makes them ONE step:

1. **revoke the run's credentials** — every Modulo-minted per-run key linked to
   the run (``auth.api_key.revoke_run_api_key``);
2. **destroy every workspace labelled ``modulo.run.id = <run_id>``** — across the
   deployment's applicable runtime tiers (Docker / Kubernetes / E2B), reusing the
   provider-neutral sources the workspace-orphan reconciler owns;
3. **write ONE ``run.teardown_sealed`` fact** to ``system_audit_events``,
   carrying the per-leg outcome in its payload.

Order is deliberate: **revoke-first, fail-closed** (design D2 Decision 2). The
credential must not outlive the run even if workspace destroy is partial or
delayed — a credentialless workspace can do nothing useful. If credential
revocation fails, the destroy leg is NOT attempted (the workspace is left for the
compensating sweep / orphan reconciler to retry) and the failure is surfaced:
the audit fact records ``sealed=False`` and :func:`seal_run_teardown` raises
:class:`RunTeardownSealError` after writing it.

"Sealed" means ONE function + ONE audit fact, idempotent, reachable from every
terminalisation path — NOT a Postgres transaction (workspace destroy is a remote
best-effort call). Migration-free: ``system_audit_events.event_type`` is
free-text ``String(100)`` (migration 0285), the same home the org-lifecycle
records use.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

_log = logging.getLogger(__name__)

#: The one audit event the seal writes. Free-text ``event_type`` column —
#: migration-free (the same ledger the org-lifecycle records use).
TEARDOWN_SEALED_EVENT_TYPE = "run.teardown_sealed"

#: Per-leg wall-clock bound on the workspace-destroy leg. The destroy is a
#: remote, best-effort call; bounding it keeps the seal from wedging a
#: terminalisation behind an unreachable substrate.
_DESTROY_LEG_TIMEOUT_SECONDS = 120.0


class RunTeardownSealError(RuntimeError):
    """The seal could not complete fail-closed — the credential revoke failed.

    Raised AFTER the ``run.teardown_sealed`` fact (``sealed=False``) is written,
    so every caller and the durable ledger both see the failure. Deliberately
    NOT raised for a workspace-destroy failure: destroy is best-effort by design
    (the compensating sweep / orphan reconciler retries it), whereas a live
    credential past terminalisation is a security failure.
    """

    def __init__(self, run_id: uuid.UUID, detail: str, *, keys_revoked: int = 0) -> None:
        self.run_id = run_id
        self.detail = detail
        self.keys_revoked = keys_revoked
        super().__init__(f"RunTeardownSeal failed for run {run_id}: {detail}")


@dataclass(frozen=True)
class TeardownSealResult:
    """Per-leg outcome of one :func:`seal_run_teardown` call.

    ``credentials_revoked`` is the fail-closed gate: when False the destroy leg
    never ran. ``sealed`` is True only when BOTH legs completed cleanly.
    """

    run_id: uuid.UUID
    credentials_revoked: bool
    keys_revoked: int
    workspaces_scanned: int
    workspaces_destroyed: int
    sealed: bool
    error: str | None = None


def _append_error(current: str | None, message: str) -> str:
    """Join per-leg failure messages into one ``error`` field (``; ``-separated)."""
    return message if current is None else f"{current}; {message}"


async def _revoke_credentials(
    *,
    run_id: uuid.UUID,
    org_id: uuid.UUID,
    session_factory: Callable[..., Any],
) -> int:
    """Revoke every per-run key linked to the run. Raises on failure (fail-closed)."""
    from modulo.auth.api_key import revoke_run_api_key
    from modulo.db.rls import set_rls_execution_context, set_rls_org

    async with session_factory() as session, session.begin():
        await set_rls_org(session, org_id)
        await set_rls_execution_context(session)
        return await revoke_run_api_key(session, run_id=run_id, org_id=org_id)


async def _destroy_run_workspaces(run_id: uuid.UUID) -> dict[str, int]:
    """Destroy every workspace labelled ``modulo.run.id == run_id`` (best-effort).

    Imported lazily so this module (and the executor's terminalisation path) does
    not pull the runtime-provider stack at import time; the reconciler owns the
    provider-neutral tier sources.
    """
    from modulo.core.bundled_runner.runner_reconciler import destroy_workspaces_for_run

    return await destroy_workspaces_for_run(str(run_id))


async def _write_seal_fact(
    session_factory: Callable[..., Any],
    *,
    run_id: uuid.UUID,
    org_id: uuid.UUID,
    payload: dict[str, Any],
) -> None:
    """Append the single ``run.teardown_sealed`` fact (fail-closed on append error)."""
    from modulo.core.system_audit_logger import append_system_audit_event

    async with session_factory() as session, session.begin():
        await append_system_audit_event(
            session,
            event_type=TEARDOWN_SEALED_EVENT_TYPE,
            org_id=org_id,
            resource_type="run",
            resource_id=run_id,
            payload_json=payload,
        )


async def _revoke_leg(
    *,
    run_id: uuid.UUID,
    org_id: uuid.UUID,
    session_factory: Callable[..., Any],
) -> tuple[int, str | None]:
    """Run the fail-closed credential-revoke leg. Returns ``(keys_revoked, error)``.

    ``error`` is ``None`` on success. A revoke failure is logged and returned
    (never raised) so the caller records ``sealed=False``; a cancellation is a
    control-flow signal and is re-raised untouched.
    """
    try:
        keys_revoked = await _revoke_credentials(
            run_id=run_id,
            org_id=org_id,
            session_factory=session_factory,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _log.exception(
            "run_teardown.credential_revoke_failed",
            extra={"run_id": str(run_id), "org_id": str(org_id)},
        )
        return 0, f"credential_revoke_failed: {exc}"
    return keys_revoked, None


async def _destroy_leg(
    *,
    run_id: uuid.UUID,
    org_id: uuid.UUID,
) -> tuple[int, int, str | None]:
    """Run the best-effort workspace-destroy leg. Returns ``(scanned, destroyed, error)``.

    A destroy failure is logged and returned (never raised): destroy is
    best-effort by design; the compensating sweep / orphan reconciler retries it.
    """
    try:
        outcome = await asyncio.wait_for(
            _destroy_run_workspaces(run_id),
            timeout=_DESTROY_LEG_TIMEOUT_SECONDS,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _log.exception(
            "run_teardown.workspace_destroy_failed",
            extra={"run_id": str(run_id), "org_id": str(org_id)},
        )
        return 0, 0, f"workspace_destroy_failed: {exc}"
    return int(outcome.get("scanned", 0)), int(outcome.get("destroyed", 0)), None


async def _best_effort_write_seal_fact(
    session_factory: Callable[..., Any],
    *,
    run_id: uuid.UUID,
    org_id: uuid.UUID,
    payload: dict[str, Any],
) -> None:
    """Append the seal fact; a ledger failure is logged, never fatal.

    The fact is the durable evidence of BOTH legs — written even when the
    revoke failed (``sealed=False``), so a fail-closed stop is never silent.
    A ledger write failure on an already-completed teardown must not re-run it.
    """
    try:
        await _write_seal_fact(
            session_factory,
            run_id=run_id,
            org_id=org_id,
            payload=payload,
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception(
            "run_teardown.seal_fact_write_failed",
            extra={"run_id": str(run_id), "org_id": str(org_id)},
        )


async def seal_run_teardown(
    *,
    run_id: uuid.UUID,
    org_id: uuid.UUID,
    session_factory: Callable[..., Any] | None,
) -> TeardownSealResult:
    """Seal a run's teardown: revoke → destroy → one audit fact (revoke-first, fail-closed).

    See the module docstring for the ordering contract. Never raises for a
    workspace-destroy failure (best-effort, recorded in the fact); raises
    :class:`RunTeardownSealError` when credential revocation could not be
    confirmed — after recording the failure in the fact.
    """
    keys_revoked = 0
    credentials_revoked = False
    error: str | None = None

    if session_factory is None:
        error = "no_session_factory: cannot revoke run credentials"
    else:
        keys_revoked, revoke_error = await _revoke_leg(
            run_id=run_id,
            org_id=org_id,
            session_factory=session_factory,
        )
        credentials_revoked = revoke_error is None
        if revoke_error is not None:
            error = _append_error(error, revoke_error)

    workspaces_scanned = 0
    workspaces_destroyed = 0
    # Fail closed: a workspace destroy is NEVER attempted while the credential is
    # still live — the credential outliving the run is the security failure.
    if credentials_revoked:
        workspaces_scanned, workspaces_destroyed, destroy_error = await _destroy_leg(
            run_id=run_id,
            org_id=org_id,
        )
        if destroy_error is not None:
            error = _append_error(error, destroy_error)

    sealed = credentials_revoked and error is None
    result = TeardownSealResult(
        run_id=run_id,
        credentials_revoked=credentials_revoked,
        keys_revoked=keys_revoked,
        workspaces_scanned=workspaces_scanned,
        workspaces_destroyed=workspaces_destroyed,
        sealed=sealed,
        error=error,
    )

    if session_factory is not None:
        await _best_effort_write_seal_fact(
            session_factory,
            run_id=run_id,
            org_id=org_id,
            payload={
                "run_id": str(run_id),
                "credentials_revoked": credentials_revoked,
                "keys_revoked": keys_revoked,
                "workspaces_scanned": workspaces_scanned,
                "workspaces_destroyed": workspaces_destroyed,
                "sealed": sealed,
                "error": error,
            },
        )

    if not credentials_revoked:
        raise RunTeardownSealError(
            run_id,
            error or "credential_revoke_failed",
            keys_revoked=keys_revoked,
        )
    return result
