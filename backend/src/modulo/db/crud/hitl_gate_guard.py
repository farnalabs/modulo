"""HITL-gate weakening guard primitive (hitl-gate-removal-guard-plan.md v19).

Shared diff primitive consumed by the ADR 017 service-layer backstop write
paths (``replace_pipeline_graph`` / ``rollback_to_snapshot``). It detects
"weakening" of an existing HITL gate configuration and reports it so that
non-privileged callers can be denied before any graph mutation executes.

Design notes (plan §1, §3):

- **Pure comparison** — the primitive never consults ``assert_org_role`` or
  ``authz_enforce``. The HITL guard is a non-liftable carve-out (ADR 017
  Decision 3); privilege arrives as a boolean resolved by the caller from a
  flag-independent live-role read.
- **``caller_type == "mcp"`` forces ``is_privileged`` to ``False``** — this is
  the entire MCP exclusion mechanism. There is no other code path by which an
  MCP caller can be privileged.
- **Correlation key** is the server-derived topology tuple
  ``(source_node_id, target_node_id, edge_type)`` — NEVER the client-supplied
  edge ``id`` (closes the topology-bypass from plan iteration 17).
- **Presence signal**: a new edge whose topology key matches a pre-existing row
  preserves the stored value when ``hitl_gate_config_present`` is False;
  ``True`` means use the provided value verbatim, including explicit ``null``
  as genuine removal.
- **Deep-copy invariant**: ``old_edges`` are deep-copied on entry so a later
  write on the session can never mutate the comparison inputs (defense in
  depth, plan §3 item 7).
- **Fail-closed for historical snapshots** (``legacy_snapshot=True``): a
  missing/``None`` gate field in a historical snapshot is treated as weakening
  with reason code ``legacy-snapshot-ambiguous``.
"""

from __future__ import annotations

import copy
import logging
import uuid
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.auth.team_rbac import org_role_level
from modulo.db.models.pipeline_edge import PipelineEdge

_log = logging.getLogger(__name__)

_OPERATOR_LEVEL = org_role_level("operator")
_ADMIN_LEVEL = org_role_level("admin")

# Reason codes (plan §5)
REASON_INSUFFICIENT_ROLE = "insufficient-role"
REASON_ROLE_CHANGED = "role-changed-reauth-required"
REASON_ROLE_CHECK_DB_ERROR = "role-check-db-error"
REASON_CORRELATION_KEY_MISMATCH = "correlation-key-mismatch"
REASON_LEGACY_SNAPSHOT_AMBIGUOUS = "legacy-snapshot-ambiguous"
REASON_MCP_NOT_PERMITTED = "mcp-weakening-not-permitted"

CallerType = Literal["rest", "mcp"]


def is_privileged_role(role: str | None) -> bool:
    """Resolve the service-layer privilege flag from an org role (operator+).

    Uses the flag-independent numeric hierarchy (``org_role_level``), NOT the
    kill-switched ``assert_org_role`` path, so the HITL guard stays live even
    when ``authz_enforce`` is disabled. Mirrors the route helper
    ``pipelines._is_privileged`` — both must agree.
    """
    if role is None:
        return False
    return org_role_level(role) >= _OPERATOR_LEVEL


@dataclass
class EdgeWeakening:
    """A single gate-weakening detection, named by the structural correlation key."""

    correlation_key: tuple[str, str, str]
    weakening_types: list[str]
    reason_code: str


@dataclass
class DiffResult:
    """Outcome of comparing old vs new edge sets for gate weakening."""

    weakened_edges: list[EdgeWeakening]
    has_weakening: bool
    denied: bool
    reason_code: str | None
    caller_type: CallerType


class HitlGateWeakeningDenied(Exception):  # noqa: N818 — matched by callers
    """Raised by guarded write paths when a gate-weakening is denied.

    ``reason_code`` is one of the plan §5 codes. ``correlation_keys`` names
    the affected edges by structural tuple (never DB ``id``).
    """

    def __init__(
        self,
        *,
        reason_code: str,
        correlation_keys: list[tuple[str, str, str]] | None = None,
        weakening_types: list[str] | None = None,
        detail: str = "",
        payload_json: dict[str, Any] | None = None,
    ) -> None:
        self.reason_code = reason_code
        self.correlation_keys = list(correlation_keys or [])
        self.weakening_types = list(weakening_types or [])
        self.detail = detail
        self.payload_json = payload_json
        message = f"hitl gate weakening denied ({reason_code})"
        if detail:
            message += f": {detail}"
        super().__init__(message)


class GuardrailBindingStripDenied(Exception):  # noqa: N818 — matched by callers
    """Raised by service-layer graph writes when a non-admin strips a guardrail binding.

    A node-bound guardrail (``eval_type='guardrail'`` row with non-null
    ``node_id``) binds to its node via the interception seam. Removing the node
    from the graph (replace / update / rollback) drops that binding — an
    end-run around the admin-only guardrail-management path (FAR-309 PR A).

    ``stripped_node_ids`` names the guardrail-bound nodes that would be removed.
    ``reason_code`` distinguishes a genuine strip denial (403) from a fail-closed
    role-resolution failure (``role-check-db-error`` maps to 503 via
    ``denial_http_status``).
    """

    def __init__(
        self,
        *,
        stripped_node_ids: list[str] | None = None,
        detail: str = "",
        reason_code: str = "guardrail-strip-denied",
    ) -> None:
        self.stripped_node_ids = list(stripped_node_ids or [])
        self.detail = detail
        self.reason_code = reason_code
        message = f"guardrail binding strip denied ({reason_code})"
        if detail:
            message += f": {detail}"
        super().__init__(message)


def _topo_key(source_node_id: Any, target_node_id: Any, edge_type: Any) -> tuple[str, str, str]:
    return (str(source_node_id), str(target_node_id), str(edge_type))


def _normalize_edge(edge: PipelineEdge | dict[str, Any]) -> dict[str, Any]:
    """Normalize an old/new edge to a plain dict for comparison.

    Accepts ``PipelineEdge`` ORM rows or plain dicts (REST edge_data / snapshot
    edge dicts). The presence signal for ORM rows is derived from the column
    value (a row that has no gate is not subject to weakening detection);
    for dicts it honours an explicit ``hitl_gate_config_present`` flag and
    otherwise falls back to "the key is present in the dict".
    """
    if isinstance(edge, dict):
        d = edge
        source = d.get("source_node_id") or d.get("source")
        target = d.get("target_node_id") or d.get("target")
        edge_type = d.get("edge_type") or d.get("type") or "normal"
        present = d.get("hitl_gate_config_present", "hitl_gate_config" in d)
        return {
            "source_node_id": str(source) if source is not None else "",
            "target_node_id": str(target) if target is not None else "",
            "edge_type": str(edge_type),
            "hitl_gate_config": d.get("hitl_gate_config"),
            "hitl_gate_config_present": bool(present),
        }
    return {
        "source_node_id": str(edge.source_node_id),
        "target_node_id": str(edge.target_node_id),
        "edge_type": edge.edge_type,
        "hitl_gate_config": edge.hitl_gate_config,
        "hitl_gate_config_present": edge.hitl_gate_config is not None,
    }


def _weakening_types(old_cfg: dict[str, Any], new_cfg: dict[str, Any]) -> list[str]:
    """Return the field-level weakening types between two non-null gate configs.

    The four weakening-capable fields from plan §1:
    - ``human_only``: true -> false.
    - ``required_team_id``: changed to null or any different team.
    - ``condition``: changed at all (evaluated before ``human_only`` is
      consulted, so any change can silently gate off a formerly-always-on gate).
    - ``eval_condition``: changed at all (same reasoning).

    ``claim_expiry_minutes`` is NOT weakening when shortened: a shorter expiry
    is stricter, not weaker. On expiry the claim is reset (run returns to
    ``awaiting_human``) — it never releases the gate or auto-approves the run,
    so a decrease cannot weaken the HITL control (plan §1, review finding).
    REMOVING the field entirely (old non-null -> new null/absent) IS weakening
    because it drops the expiry requirement without tightening anything.
    """
    types: list[str] = []
    if old_cfg.get("human_only") is True and new_cfg.get("human_only") is not True:
        types.append("human_only")
    old_team = old_cfg.get("required_team_id")
    new_team = new_cfg.get("required_team_id")
    if old_team is not None and str(old_team) != str(new_team):
        types.append("required_team_id")
    if old_cfg.get("condition") != new_cfg.get("condition"):
        types.append("condition")
    if old_cfg.get("eval_condition") != new_cfg.get("eval_condition"):
        types.append("eval_condition")
    if old_cfg.get("claim_expiry_minutes") is not None and new_cfg.get("claim_expiry_minutes") is None:
        types.append("claim_expiry_minutes")
    return types


async def apply_gated_edge_diff(
    _session: AsyncSession,
    old_edges: list[PipelineEdge] | list[dict[str, Any]],
    new_edges: list[dict[str, Any]],
    is_privileged: bool,
    caller_type: CallerType,
    *,
    legacy_snapshot: bool = False,
) -> DiffResult:
    """Compute gate-weakening between the current edge set and the proposed set.

    ``session`` is currently unused by the comparison; it is part of the plan's
    contract so future DB-backed resolution can be added without changing the
    signature. ``old_edges`` MUST have been snapshotted (deepcopy) before any
    write on the session; the primitive re-copies defensively.

    Returns a ``DiffResult``. When ``has_weakening`` and ``is_privileged`` is
    False the result is ``denied`` with a plan §5 reason code. For
    ``caller_type == "mcp"`` privilege is forced False regardless of the
    argument.
    """
    if caller_type == "mcp":
        is_privileged = False

    old_norm = [copy.deepcopy(_normalize_edge(e)) for e in (copy.deepcopy(old_edges) if old_edges else [])]
    new_norm = [copy.deepcopy(_normalize_edge(e)) for e in (copy.deepcopy(new_edges) if new_edges else [])]

    old_by_key = {
        _topo_key(e["source_node_id"], e["target_node_id"], e["edge_type"]): e
        for e in old_norm
        if e["hitl_gate_config"] is not None
    }
    new_by_key = {_topo_key(e["source_node_id"], e["target_node_id"], e["edge_type"]): e for e in new_norm}

    weakened: list[EdgeWeakening] = []
    for key, old_edge in old_by_key.items():
        new_edge = new_by_key.get(key)
        if new_edge is None:
            weakened.append(
                EdgeWeakening(
                    correlation_key=key,
                    weakening_types=["structural:edge_deleted"],
                    reason_code=(
                        REASON_LEGACY_SNAPSHOT_AMBIGUOUS if legacy_snapshot else REASON_CORRELATION_KEY_MISMATCH
                    ),
                )
            )
            continue
        if not new_edge["hitl_gate_config_present"]:
            # Key omitted by the client -> preserve the existing stored value.
            # The delete+reinsert write paths MUST mirror this contract and
            # merge the stored value back (see pipeline._preserve_omitted_gate_config).
            # An omitted key is never a gate removal.
            continue
        new_cfg = new_edge["hitl_gate_config"]
        if new_cfg is None:
            weakened.append(
                EdgeWeakening(
                    correlation_key=key,
                    weakening_types=["structural:gate_removed"],
                    reason_code=(
                        REASON_LEGACY_SNAPSHOT_AMBIGUOUS if legacy_snapshot else REASON_CORRELATION_KEY_MISMATCH
                    ),
                )
            )
            continue
        field_types = _weakening_types(old_edge["hitl_gate_config"], new_cfg)
        if field_types:
            weakened.append(
                EdgeWeakening(
                    correlation_key=key,
                    weakening_types=field_types,
                    reason_code=REASON_LEGACY_SNAPSHOT_AMBIGUOUS if legacy_snapshot else REASON_INSUFFICIENT_ROLE,
                )
            )

    has_weakening = bool(weakened)
    denied = has_weakening and not is_privileged
    if not denied:
        reason_code: str | None = None
    elif caller_type == "mcp":
        reason_code = REASON_MCP_NOT_PERMITTED
    elif legacy_snapshot:
        reason_code = REASON_LEGACY_SNAPSHOT_AMBIGUOUS
    elif any("structural" in wt for e in weakened for wt in e.weakening_types):
        reason_code = REASON_CORRELATION_KEY_MISMATCH
    else:
        reason_code = REASON_INSUFFICIENT_ROLE

    return DiffResult(
        weakened_edges=weakened,
        has_weakening=has_weakening,
        denied=denied,
        reason_code=reason_code,
        caller_type=caller_type,
    )


async def resolve_effective_privilege(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    account_id: uuid.UUID | None,
    is_privileged: bool,
    caller_type: CallerType,
) -> bool:
    """Resolve the effective privilege flag under the row lock (plan §3 item 5).

    - ``caller_type == "mcp"``: always ``False``, no DB query attempted at all
      — the entire MCP exclusion mechanism.
    - ``caller_type == "rest"`` with ``account_id``: re-reads the caller's
      live org role via ADR-017's centralized ``resolve_role_from_membership``.
      A DB error denies immediately with ``role-check-db-error`` (fail-closed,
      no retry — retrying inside the same transaction hits Postgres's
      aborted-transaction state, 25P02). A missing/deactivated membership
      denies with ``role-changed-reauth-required``.
    - otherwise: the caller-supplied ``is_privileged`` is used as-is (tests,
      creation-only paths).
    """
    if caller_type == "mcp":
        return False
    if account_id is None:
        return is_privileged
    from modulo.db.crud.org_membership import resolve_role_from_membership

    try:
        live_role = await resolve_role_from_membership(session, str(account_id), str(org_id))
    except SQLAlchemyError:
        _log.exception(
            "hitl_gate_guard.role_check_db_error",
            extra={"org_id": str(org_id), "account_id": str(account_id)},
        )
        raise HitlGateWeakeningDenied(
            reason_code=REASON_ROLE_CHECK_DB_ERROR,
            detail="Failed to re-read the caller's org role under the row lock.",
        ) from None
    if live_role is None:
        raise HitlGateWeakeningDenied(
            reason_code=REASON_ROLE_CHANGED,
            detail="No active org membership for the caller.",
        )
    return is_privileged_role(live_role)


def denial_detail(diff: DiffResult) -> str:
    """Human-readable denial detail naming affected edges by structural key."""
    if not diff.weakened_edges:
        return ""
    parts = []
    for w in diff.weakened_edges:
        src, tgt, etype = w.correlation_key
        parts.append(f"{src}->{tgt} ({etype}): {', '.join(w.weakening_types)}")
    return "; ".join(parts)


def build_gate_diff_payload(diff: DiffResult, caller_type: CallerType) -> dict[str, Any]:
    """Shared audit-payload builder (plan §3 item 9) — same schema for denied
    and allowed-weakening events so the two paths stay schema-parity consistent.
    """
    return {
        "caller_type": caller_type,
        "reason_code": diff.reason_code,
        "denied": diff.denied,
        "affected_edges": [
            {
                "source_node_id": w.correlation_key[0],
                "target_node_id": w.correlation_key[1],
                "edge_type": w.correlation_key[2],
                "weakening_types": w.weakening_types,
                "reason_code": w.reason_code,
            }
            for w in diff.weakened_edges
        ],
    }


def denial_http_status(reason_code: str | None) -> int:
    """Map a denial reason code to an HTTP status for REST translation.

    ``role-check-db-error`` is a service-unavailability denial (the guard
    could not determine privilege because of a DB failure); everything else is
    a 403 Forbidden.
    """
    if reason_code == REASON_ROLE_CHECK_DB_ERROR:
        return 503
    return 403


async def enforce_guardrail_binding_strip(
    session: AsyncSession,
    *,
    pipeline_id: uuid.UUID,
    org_id: uuid.UUID,
    incoming_node_ids: set[str],
    is_guardrail_admin: bool,
    caller_type: CallerType,
    account_id: uuid.UUID | None = None,
) -> None:
    """Service-layer guardrail-binding strip guard (FAR-309 PR A review).

    A node-bound guardrail (``eval_type='guardrail'`` row with non-null
    ``node_id``) binds to its node via the interception seam. Removing the node
    from the graph (replace / update / rollback) drops that binding — an
    end-run around the admin-only guardrail-management path. Non-admins are
    denied; admins (``guardrail.manage``) may remove such nodes.

    Runs under the caller's row lock (the guarded write path already holds
    ``SELECT ... FOR UPDATE`` on the pipeline row) and BEFORE any graph
    mutation. The caller's effective admin flag is re-read via
    ``resolve_effective_privilege`` semantics: for ``caller_type == "rest"``
    with ``account_id`` the live org role is re-read under the lock
    (fail-closed on DB error), for ``caller_type == "mcp"`` the caller-supplied
    flag is used as-is. Raises ``GuardrailBindingStripDenied`` when a
    guardrail-bound node is stripped by a non-admin.
    """
    effective_admin = is_guardrail_admin
    if caller_type == "rest" and account_id is not None:
        effective_admin = await _resolve_effective_guardrail_admin(
            session,
            org_id=org_id,
            account_id=account_id,
            caller_type=caller_type,
        )
    if effective_admin:
        return
    from modulo.db.crud.guardrail_config import load_pipeline_guardrail_rows

    guardrail_rows = await load_pipeline_guardrail_rows(
        session,
        pipeline_id=pipeline_id,
        organisation_id=org_id,
    )
    stripped = sorted(
        {
            str(row.node_id)
            for row in guardrail_rows
            if row.node_id is not None and str(row.node_id) not in incoming_node_ids
        }
    )
    if stripped:
        raise GuardrailBindingStripDenied(
            stripped_node_ids=stripped,
            detail=(
                "Non-admin cannot strip a guardrail binding: removing node(s) "
                + ", ".join(stripped)
                + " from the graph would drop a node-bound guardrail. Only an "
                "admin can remove a node that has a bound guardrail."
            ),
        )


async def _resolve_effective_guardrail_admin(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    account_id: uuid.UUID,
    caller_type: CallerType,
) -> bool:
    """Re-read the caller's live org role under the lock (admin-level check).

    Mirrors ``resolve_effective_privilege``: for ``caller_type == "rest"`` the
    live role is re-read via ADR-017's ``resolve_role_from_membership``. A DB
    error denies with ``role-check-db-error`` (fail-closed, no retry); a
    missing/deactivated membership denies with ``role-changed``. For
    ``caller_type == "mcp"`` the caller-supplied admin flag is used as-is (the
    MCP surface resolves the role at the tool boundary).
    """
    if caller_type == "mcp":
        return False
    from modulo.db.crud.org_membership import resolve_role_from_membership

    try:
        live_role = await resolve_role_from_membership(session, str(account_id), str(org_id))
    except SQLAlchemyError:
        _log.exception(
            "hitl_gate_guard.guardrail_admin_role_check_db_error",
            extra={"org_id": str(org_id), "account_id": str(account_id)},
        )
        raise GuardrailBindingStripDenied(
            reason_code=REASON_ROLE_CHECK_DB_ERROR,
            detail="Failed to re-read the caller's org role under the row lock.",
        ) from None
    if live_role is None:
        raise GuardrailBindingStripDenied(
            reason_code=REASON_ROLE_CHANGED,
            detail="No active org membership for the caller.",
        ) from None
    return org_role_level(live_role) >= _ADMIN_LEVEL
