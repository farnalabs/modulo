"""Mid-run capability re-check (FAR-215 T3).

T1 ships the three-state conformance derivation (:func:`derive_conformance_state`)
as a PURE helper with enforcement *planned*. This module wires that enforcement
at NODE START against a LIVE manifest — the promise that a connector scope or
sandbox/environment policy change between run-creation and a node actually
executing is re-validated, not carved out.

Behaviour on change (fail-closed for block-action guardrails, per
``ConformanceDerivation`` semantics):
  - ``absent`` or ``unknown`` on a ``block``-action guardrail -> BLOCK the node
    and route to HITL (never silently abort, never fail open). The run pauses
    ``awaiting_human`` with a machine-readable detail.
  - ``absent`` or ``unknown`` on ``warn``/``observe`` guardrails -> log + audit
    warning, continue (advisory never blocks).
  - ``present`` (or no conformance claim) -> continue normally (fast path).
  - Zero bound guardrails carrying a conformance claim -> fast path, no DB
    round-trip for the manifest.

The live manifest is built from CURRENT capability sources for the node's bound
surfaces (connector scopes/allowed-operations, EnvironmentProfile capabilities,
the bound agent's required environment capabilities, and — for a
``sandbox_agent`` node — the mechanically-derived sandbox write/egress
capability surface). A source that is absent or unreadable contributes ``None``
(unknown) for its capabilities, so a ``block`` guardrail fails CLOSED (unknown
blocks), never fail-open.

All DB access is org-scoped (RLS via ``set_rls_org``). No credentials, no raw
payloads, and no decrypted state ever enter the returned decision or audit
payloads — only capability names and their confirmed state.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.core.guardrails import (
    ConformanceDerivation,
    ConformanceState,
    GuardrailAction,
    derive_conformance_state,
)

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ConformanceRecheckResult:
    """Outcome of a mid-run capability re-check at node start.

    ``blocked`` is True when a block-action guardrail is absent/unknown and the
    node must be blocked + routed to HITL. ``gate_id`` is the HITL gate to open.
    ``detail`` is a machine-readable reason. ``warned`` records that at least
    one advisory (warn/observe) guardrail flagged a conformance gap.
    """

    blocked: bool
    gate_id: str | None
    detail: str
    state: ConformanceState
    warned: bool
    claimed: bool = False


# ---------------------------------------------------------------------------
# Live manifest reader
# ---------------------------------------------------------------------------


def _capabilities_for_connector(row: Any) -> set[str]:
    """Capability surface of a ConnectorInstance row (live).

    The instance's ``allowed_operations`` is the authoritative declared scope;
    when it is empty (unscoped) we cannot confirm any operation is granted, so
    an empty set means "no confirmed capabilities from this surface". We never
    read credential material here — only the declared operations and the
    connector type id (a non-secret).
    """
    allowed = row.allowed_operations if hasattr(row, "allowed_operations") else None
    if isinstance(allowed, list):
        return {str(c) for c in allowed if isinstance(c, str)}
    return set()


def _capabilities_for_profile(row: Any) -> set[str]:
    """Capability surface of an EnvironmentProfile row (live)."""
    caps = row.capabilities_json if hasattr(row, "capabilities_json") else None
    if isinstance(caps, list):
        return {str(c) for c in caps if isinstance(c, str)}
    return set()


def _capabilities_for_agent(row: Any) -> set[str]:
    """Capability surface of an Agent row (live)."""
    caps = row.required_environment_capabilities if hasattr(row, "required_environment_capabilities") else None
    if isinstance(caps, list):
        return {str(c) for c in caps if isinstance(c, str)}
    return set()


# Sandbox capabilities whose block-guarantee is a DENY/negative guarantee —
# ``required_capabilities=["sandbox.write_files"]`` certifies writes are
# IMPOSSIBLE and ``["sandbox.egress"]`` certifies no egress, so the RAW
# mechanical polarity (True = risk present) would be INVERTED when stamped into
# the manifest. ``sandbox.git_credentials`` is a POSITIVE guarantee (certifies
# git credentials are scoped/limited) whose raw polarity already matches the
# manifest, so it is never inverted. The set is resolved LAZILY from the
# ``sandbox_mode`` vocabulary constants (never re-hard-coded literals) so a
# renamed/removed capability can never drift silently from the polarity set.
# NOTE (FAR-212 PR A): ``sandbox.write_files`` / ``sandbox.git_credentials``
# today always derive None (no enforced read-only-mount / git-credential-surface
# exists yet), so they fail CLOSED as unknown — only ``sandbox.egress`` is a
# live certifyable surface.
def _sandbox_deny_polarity_caps() -> frozenset[str]:
    from modulo.core.pipeline_engine.sandbox_mode import (
        SANDBOX_CAPABILITY_EGRESS,
        SANDBOX_CAPABILITY_WRITE_FILES,
    )

    return frozenset({SANDBOX_CAPABILITY_WRITE_FILES, SANDBOX_CAPABILITY_EGRESS})


def _add_sandbox_surface(registered: dict[str, bool | None], sandbox_caps: dict[str, bool | None]) -> None:
    """Stamp the mechanically-derived sandbox surface with CONFORMANCE polarity.

    A block-action conformance claim on the sandbox surface is, for the
    write/egress pair, a DENY/negative guarantee: ``["sandbox.write_files"]``
    certifies that writes are IMPOSSIBLE, ``["sandbox.egress"]`` certifies no
    egress. The mechanical derivation (:func:`derive_sandbox_capabilities`)
    returns the RAW polarity (True = writable / egress allowed), so the
    manifest INVERTS those two and the existing three-state derivation reads
    them correctly:

      mechanical False (confirmed absent) -> registered True  (guarantee holds)
      mechanical True  (present)           -> registered False (claim violated)
      None (unknown)                       -> registered None (fail-closed)

    ``sandbox.git_credentials`` is a POSITIVE guarantee — ``["sandbox.git_credentials"]``
    certifies the node's git credentials are SCOPED (limited) — so its raw
    polarity already matches the manifest (scoped=True -> registered True) and
    is stamped AS-IS, never inverted. Inverting it would certify the risky
    state (unscoped/full-access) and block the safe state (scoped).

    The sandbox surface is stamped LAST (overriding any earlier surface) — the
    node's actual sandbox configuration is authoritative for the sandbox
    surface. Never contains credentials — only capability names and their
    confirmed state.
    """
    deny_polarity = _sandbox_deny_polarity_caps()
    for capability, value in sandbox_caps.items():
        if value is None:
            registered[capability] = None
        elif capability in deny_polarity:
            registered[capability] = not value
        else:
            registered[capability] = value


async def build_live_manifest(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    connector_instance_ids: list[uuid.UUID],
    environment_profile_id: uuid.UUID | None,
    agent_id: uuid.UUID | None,
    node_def: dict[str, Any] | None = None,
) -> dict[str, bool | None]:
    """Read CURRENT capability state for the node's bound surfaces.

    Returns ``{capability: bool | None}``:
      True  — confirmed present on at least one live surface.
      False — the relevant surface is present but does not declare it.
      None  — the surface could not be read (unknown) OR capability not seen.

    A capability is marked True if ANY live surface declares it. It is marked
    False when we confirmed a relevant surface but it does not declare it.
    ``None`` (unknown) is recorded for a surface that could not be read (a
    missing connector instance, profile, or agent, or a read failure) — a
    block-action guardrail then fails CLOSED (unknown blocks).

    For a ``sandbox_agent`` node, *node_def* additionally contributes the
    sandbox capability surface (``sandbox.egress`` — mechanically certified
    from the enforced egress policy; ``sandbox.write_files`` and
    ``sandbox.git_credentials`` stay unknown until their enforcement surfaces
    land in PR B), stamped with conformance polarity (FAR-212 PR A).

    IMPORTANT: a capability that is absent from ``registered`` entirely also
    resolves to ``None`` (unknown) in ``derive_conformance_state`` — a surface
    that could not be read contributes nothing, and the derivation's
    ``registered.get(capability)`` miss fails CLOSED. So an unreadable surface
    needs no explicit ``None`` stamping; its claimed capabilities stay unknown.
    """
    registered: dict[str, bool | None] = {}

    def _add(surface: set[str]) -> None:
        for cap in surface:
            registered[cap] = True

    if connector_instance_ids:
        from modulo.db.models.connector_instance import ConnectorInstance

        try:
            stmt = select(ConnectorInstance).where(ConnectorInstance.id.in_(connector_instance_ids))
            rows = (await session.execute(stmt)).scalars().all()
            found = {str(r.id) for r in rows}
            for r in rows:
                # A deactivated/archived connector no longer grants its scopes —
                # the capability is absent from the live surface even though the
                # row still exists. Never read credential material here.
                if getattr(r, "status", "active") != "active":
                    _log.warning(
                        "guardrail.conformance.connector_inactive",
                        extra={"org_id": str(org_id), "connector_instance_id": str(r.id)},
                    )
                    continue
                _add(_capabilities_for_connector(r))
            for cid in connector_instance_ids:
                if str(cid) not in found:
                    _log.warning(
                        "guardrail.conformance.connector_missing",
                        extra={"org_id": str(org_id), "connector_instance_id": str(cid)},
                    )
        except Exception:
            _log.exception("guardrail.conformance.connector_surface_unreadable", extra={"org_id": str(org_id)})

    if environment_profile_id is not None:
        from modulo.db.models.environment_profile import EnvironmentProfile

        try:
            profile = (
                await session.execute(select(EnvironmentProfile).where(EnvironmentProfile.id == environment_profile_id))
            ).scalar_one_or_none()
            if profile is None:
                _log.warning(
                    "guardrail.conformance.profile_missing",
                    extra={"org_id": str(org_id), "environment_profile_id": str(environment_profile_id)},
                )
            elif getattr(profile, "status", "active") != "active":
                # A deactivated profile no longer grants its capabilities.
                _log.warning(
                    "guardrail.conformance.profile_inactive",
                    extra={"org_id": str(org_id), "environment_profile_id": str(environment_profile_id)},
                )
            else:
                _add(_capabilities_for_profile(profile))
        except Exception:
            _log.exception("guardrail.conformance.profile_surface_unreadable", extra={"org_id": str(org_id)})

    if agent_id is not None:
        from modulo.db.models.agent import Agent

        try:
            agent = (await session.execute(select(Agent).where(Agent.id == agent_id))).scalar_one_or_none()
            if agent is None:
                _log.warning("guardrail.conformance.agent_missing", extra={"org_id": str(org_id)})
            else:
                _add(_capabilities_for_agent(agent))
        except Exception:
            _log.exception("guardrail.conformance.agent_surface_unreadable", extra={"org_id": str(org_id)})

    # FAR-212 PR A: the sandbox capability surface for a sandbox_agent node —
    # mechanically derived from the node's ACTUAL config (egress_policy, the
    # read-only workspace flag, git-credential scope), not a declared claim.
    # Lazy import keeps this module's import surface light: sandbox_mode itself
    # is dependency-free, but the pipeline_engine package init (LangGraph,
    # executor, DB) is heavy and must not be pulled in by guardrail consumers
    # that never build a sandbox manifest.
    if node_def is not None:
        from modulo.core.pipeline_engine.sandbox_mode import derive_sandbox_capabilities

        _add_sandbox_surface(registered, derive_sandbox_capabilities(node_def))

    return registered


# ---------------------------------------------------------------------------
# Pure decision
# ---------------------------------------------------------------------------


def _action_of(config: dict[str, Any]) -> str:
    action = config.get("action")
    if isinstance(action, str):
        return action
    return GuardrailAction.OBSERVE.value


def _required_of(config: dict[str, Any]) -> list[str]:
    required = config.get("required_capabilities")
    if isinstance(required, list):
        return [str(c) for c in required if isinstance(c, str)]
    return []


def decide_conformance(
    required_capabilities: list[str],
    registered: dict[str, bool | None],
) -> ConformanceDerivation:
    """Derive the conformance state for one guardrail's claim (reuses T1)."""
    return derive_conformance_state(required_capabilities, registered)


def worst_state(derivations: list[ConformanceDerivation]) -> ConformanceState:
    """Order states worst-first: absent > unknown > present (no claim)."""
    if any(d.state == "absent" for d in derivations):
        return "absent"
    if any(d.state == "unknown" for d in derivations):
        return "unknown"
    return "present"


def evaluate_conformance(
    guardrails: list[Any],
    registered: dict[str, bool | None],
) -> ConformanceRecheckResult:
    """Evaluate bound guardrails against the LIVE manifest (pure).

    *guardrails* is a list of engine ``EvalDefinition`` DTOs (or objects with
    ``.config`` and ``.name``) carrying a conformance claim. Returns the node
    decision. Zero claims -> not blocked, fast path.
    """
    claimed: list[ConformanceDerivation] = []
    advisory_warned = False
    blocked_detail_parts: list[str] = []
    blocking_guardrails: list[str] = []

    for gr in guardrails:
        config = gr.config if hasattr(gr, "config") else {}
        required = _required_of(config)
        if not required:
            continue
        action = _action_of(config)
        derivation = decide_conformance(required, registered)
        claimed.append(derivation)
        if derivation.state == "present":
            continue
        if action == GuardrailAction.BLOCK.value:
            blocking_guardrails.append(gr.name)
            blocked_detail_parts.append(
                f"guardrail {gr.name!r} requires capabilities {required} which are no longer present "
                f"(state={derivation.state})"
            )
        else:
            # warn / observe / redact-as-non-blocking: advisory only.
            advisory_warned = True
            _log.warning(
                "guardrail.conformance.advisory_gap",
                extra={"guardrail": gr.name, "state": derivation.state, "missing": list(derivation.missing)},
            )

    if not claimed:
        return ConformanceRecheckResult(blocked=False, gate_id=None, detail="", state="present", warned=False)

    if blocking_guardrails:
        return ConformanceRecheckResult(
            blocked=True,
            gate_id=f"guardrail_conformance_{blocking_guardrails[0]}",
            detail=("; ".join(blocked_detail_parts))[:5000],
            state=worst_state(claimed),
            warned=advisory_warned,
            claimed=True,
        )
    return ConformanceRecheckResult(
        blocked=False,
        gate_id=None,
        detail="; ".join(blocked_detail_parts)[:5000],
        state=worst_state(claimed),
        warned=advisory_warned,
        claimed=True,
    )


# ---------------------------------------------------------------------------
# Node-start orchestration (async, DB-backed)
# ---------------------------------------------------------------------------


async def load_node_guardrails(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    node_id: str | None,
) -> list[Any]:
    """Load guardrails bound to the pipeline (org-level + node-bound).

    Mirrors the run-creation interception seam's row-loading, scoped to this
    node: org-level rows (``node_id IS NULL``) AND rows bound to *node_id*.
    Returns engine DTOs via ``to_engine_definition``.
    """
    from modulo.core.guardrails import to_engine_definition
    from modulo.db.models.eval_definition import EvalDefinition

    stmt = select(EvalDefinition).where(
        EvalDefinition.pipeline_id == pipeline_id,
        EvalDefinition.organisation_id == org_id,
        EvalDefinition.eval_type == "guardrail",
    )
    if node_id:
        node_uuid = uuid.UUID(node_id) if _is_uuid(node_id) else None
        stmt = stmt.where((EvalDefinition.node_id.is_(None)) | (EvalDefinition.node_id == node_uuid))
    else:
        stmt = stmt.where(EvalDefinition.node_id.is_(None))
    rows = (await session.execute(stmt)).scalars().all()
    return [to_engine_definition(row) for row in rows]


async def load_claimed_guardrails(
    session_factory: Any,
    *,
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID,
) -> tuple[list[Any], bool]:
    """Hoisted run-start claim discovery for the conformance re-check (FAR-215).

    Loads ALL guardrail rows bound to the pipeline (org-level AND node-bound)
    ONCE per run and returns only those carrying a conformance claim (a
    non-empty ``required_capabilities`` config). The executor seeds the result
    into the run-scoped conformance context, so the per-node check pays zero
    DB round-trips when there are no claims and one query per run when there
    are.

    Returns ``(claimed, load_failed)``. ``load_failed`` is True when the load
    could not be completed — the caller MUST fail CLOSED (treat as unknown for
    block-action claims), never silently skip claims.
    """
    from modulo.core.guardrails import to_engine_definition
    from modulo.db.models.eval_definition import EvalDefinition

    try:
        async with session_factory() as session, session.begin():
            await _set_rls(session, org_id)
            stmt = select(EvalDefinition).where(
                EvalDefinition.pipeline_id == pipeline_id,
                EvalDefinition.organisation_id == org_id,
                EvalDefinition.eval_type == "guardrail",
            )
            rows = (await session.execute(stmt)).scalars().all()
        guardrails = [to_engine_definition(row) for row in rows]
    except Exception:
        _log.exception("guardrail.conformance.run_start_load_failed", extra={"org_id": str(org_id)})
        return [], True
    claimed = [g for g in guardrails if _required_of(g.config if hasattr(g, "config") else {})]
    return claimed, False


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except (ValueError, TypeError):
        return False
    return True


def _claims_for_node(claimed_guardrails: list[Any], node_id: str | None) -> list[Any]:
    """Filter hoisted claimed guardrail DTOs down to THIS node's bindings.

    Mirrors ``load_node_guardrails``'s node scoping in memory: org-level rows
    (``node_id IS NULL``) apply to every node; node-bound rows apply only when
    the node id is a UUID equal to the row's bound node.
    """
    org_level = [g for g in claimed_guardrails if getattr(g, "node_id", None) is None]
    if node_id is None or not _is_uuid(node_id):
        return org_level
    return org_level + [g for g in claimed_guardrails if getattr(g, "node_id", None) == node_id]


async def check_node_start(
    session_factory: Any,
    *,
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    node_id: str | None,
    connector_instance_ids: list[uuid.UUID],
    environment_profile_id: uuid.UUID | None,
    agent_id: uuid.UUID | None,
    node_def: dict[str, Any] | None = None,
    claimed_guardrails: list[Any] | None = None,
    claims_load_failed: bool = False,
) -> ConformanceRecheckResult:
    """Full node-start re-check: load guardrails, build live manifest, decide.

    This is the single seam the node runner invokes at node start. It performs
    its own short-lived, org-scoped DB session (RLS set inside the
    transaction). A manifest/DB failure degrades to ``unknown`` for block
    guardrails (fail CLOSED — never fail open) via ``build_live_manifest``'s
    unreadable handling. A failure to LOAD guardrails entirely is also fail
    CLOSED: we cannot confirm whether a block claim exists, so the node is
    blocked with state ``unknown`` (deny on error) rather than silently
    continuing. The reader never raises — the node decision is what matters,
    and a broken reader must not crash the node into a terminal error.

    *node_def* is the node's definition dict. For a ``sandbox_agent`` node it
    contributes the mechanically-derived sandbox capability surface to the
    manifest (FAR-212 PR A); for every other node type it is inert.

    *claimed_guardrails* is the hoisted list of claimed guardrail DTOs the
    executor precomputed ONCE at run start (one query per run). When provided,
    the per-node guardrail-load query is skipped entirely — the node scoping
    (org-level + node-bound) happens in memory. ``claims_load_failed`` marks a
    run-start claim-discovery failure and fails CLOSED (unknown blocks). When
    *claimed_guardrails* is ``None`` the legacy per-node DB load is used.
    """
    if claims_load_failed:
        # Fail CLOSED on run-start claim-discovery failure: cannot confirm
        # whether a block claim exists, so the node is blocked with state
        # unknown (never fail open).
        return ConformanceRecheckResult(
            blocked=True,
            gate_id="guardrail_conformance_check_failed",
            detail="mid-run capability re-check could not load bound guardrails; failing closed",
            state="unknown",
            warned=False,
            claimed=True,
        )

    if claimed_guardrails is not None:
        # Hoisted path: the executor precomputed the claimed DTOs at run start.
        # Filter to THIS node's bindings in memory — no per-node DB round-trip.
        claimed = _claims_for_node(claimed_guardrails, node_id)
    else:
        async with session_factory() as session, session.begin():
            await _set_rls(session, org_id)
            try:
                guardrails = await load_node_guardrails(
                    session,
                    org_id=org_id,
                    pipeline_id=pipeline_id,
                    node_id=node_id,
                )
            except Exception:
                _log.exception("guardrail.conformance.load_failed", extra={"org_id": str(org_id)})
                # Fail CLOSED on load failure: cannot confirm conformance, so
                # the node is blocked with state unknown (never fail open).
                return ConformanceRecheckResult(
                    blocked=True,
                    gate_id="guardrail_conformance_check_failed",
                    detail="mid-run capability re-check could not load bound guardrails; failing closed",
                    state="unknown",
                    warned=False,
                    claimed=True,
                )
            claimed = [g for g in guardrails if _required_of(g.config if hasattr(g, "config") else {})]

    if not claimed:
        # Zero-claim fast path — no manifest round-trip needed.
        return ConformanceRecheckResult(blocked=False, gate_id=None, detail="", state="present", warned=False)

    async with session_factory() as session, session.begin():
        await _set_rls(session, org_id)
        registered = await build_live_manifest(
            session,
            org_id=org_id,
            connector_instance_ids=connector_instance_ids,
            environment_profile_id=environment_profile_id,
            agent_id=agent_id,
            node_def=node_def,
        )
    return evaluate_conformance(claimed, registered)


async def _set_rls(session: Any, org_id: uuid.UUID) -> None:
    from modulo.db.rls import set_rls_execution_context, set_rls_org

    await set_rls_org(session, org_id)
    await set_rls_execution_context(session)


__all__ = [
    "ConformanceRecheckResult",
    "build_live_manifest",
    "check_node_start",
    "decide_conformance",
    "evaluate_conformance",
    "load_claimed_guardrails",
    "load_node_guardrails",
    "worst_state",
]
