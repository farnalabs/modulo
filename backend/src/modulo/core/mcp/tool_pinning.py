"""MCP tool-definition pinning: canonical hash + drift decision (FAR-1692 S2).

Detect-and-surface only (ADR 055 — record, do not police). This module is pure
and DB-free on purpose: the canonical hash and the drift decision are the
security-relevant logic and are unit-tested directly, while the persistence and
the notification/audit side effects live in the route layer.

The contract:

* :func:`compute_tools_hash` canonicalises a reported tool-definition set to a
  stable SHA-256 over each tool's *identity-relevant* projection (name, title,
  description, input schema) — list order and volatile server metadata do not
  change the hash, so re-observation of an unchanged tool set is idempotent.
* :func:`decide_observation` maps (observed hash, approved hash, current status)
  to the next approval status: an observed hash different from a recorded
  ``approved_hash`` is DRIFT and flips the pin to ``approved-stale``; a
  re-observation that matches the approved hash clears the stale flag back to
  ``approved``; an operator ``blocked`` state is never downgraded by drift.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.core.audit_logger import append_audit_event
from modulo.db.crud.notifications import create_notification
from modulo.db.models.mcp_server import (
    APPROVAL_STATUS_APPROVED,
    APPROVAL_STATUS_APPROVED_STALE,
    APPROVAL_STATUS_BLOCKED,
    APPROVAL_STATUS_UNAPPROVED,
    McpServer,
)

_log = logging.getLogger(__name__)

_DRIFT_AUDIT_EVENT_TYPE = "mcp_server_tools_drifted"
_DRIFT_NOTIFICATION_CATEGORY = "mcp_server_tools_drift"


class ToolPinningError(ValueError):
    """A tool-definition set cannot be canonicalised (malformed entry)."""


def _project_tool(tool: Mapping[str, Any]) -> dict[str, Any]:
    """Reduce one tool definition to its stable identity projection."""
    name = tool.get("name")
    if not isinstance(name, str) or not name:
        raise ToolPinningError("each tool definition must carry a non-empty string 'name'")
    input_schema = tool.get("inputSchema") if "inputSchema" in tool else tool.get("input_schema")
    return {
        "name": name,
        "title": tool.get("title"),
        "description": tool.get("description"),
        "inputSchema": input_schema,
    }


def canonicalise_tools(tools: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Return the canonical (order-independent) projection of a tool set."""
    projected: list[dict[str, Any]] = []
    for tool in tools:
        if not isinstance(tool, Mapping):
            raise ToolPinningError("each tool definition must be a mapping")
        projected.append(_project_tool(tool))
    # Sort by the serialised projection so two tools sharing a name still
    # canonicalise deterministically.
    projected.sort(key=lambda entry: json.dumps(entry, sort_keys=True, separators=(",", ":"), default=str))
    return projected


def compute_tools_hash(tools: Sequence[Mapping[str, Any]]) -> str:
    """SHA-256 hex digest of a tool set's canonical projection."""
    canonical = canonicalise_tools(tools)
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":"), default=str, ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ObservationOutcome:
    """The result of applying one tool-set observation to a server's pin state."""

    tools_hash: str
    approval_status: str
    drifted: bool


def decide_observation(
    *,
    observed_hash: str,
    approved_hash: str | None,
    current_status: str,
) -> ObservationOutcome:
    """Decide the next pin state for an observation (see the module docstring).

    * A ``blocked`` server is NEVER downgraded — drift cannot un-block it, and
      no drift alert is raised (it is already surfaced to the operator).
    * With no recorded ``approved_hash`` there is no approval to invalidate: the
      observed hash is stored and the status is left as ``unapproved`` (a
      defensive ``approved``/``approved-stale`` without a hash cannot be trusted
      to be current and is reset to ``unapproved``).
    * An observed hash equal to the recorded ``approved_hash`` is ``approved``
      (this also clears a previous ``approved-stale`` once the server returns to
      the approved tool set).
    * Any other hash is DRIFT: ``approved-stale`` + ``drifted=True``.
    """
    if current_status == APPROVAL_STATUS_BLOCKED:
        return ObservationOutcome(tools_hash=observed_hash, approval_status=APPROVAL_STATUS_BLOCKED, drifted=False)

    if approved_hash is None:
        # No approved hash to compare against: store the observed hash and reset
        # to ``unapproved``. A defensive approved/approved-stale without a hash is
        # untrustworthy; ``blocked`` never reaches here (returned above).
        return ObservationOutcome(tools_hash=observed_hash, approval_status=APPROVAL_STATUS_UNAPPROVED, drifted=False)

    if observed_hash == approved_hash:
        return ObservationOutcome(tools_hash=observed_hash, approval_status=APPROVAL_STATUS_APPROVED, drifted=False)

    return ObservationOutcome(tools_hash=observed_hash, approval_status=APPROVAL_STATUS_APPROVED_STALE, drifted=True)


@dataclass(frozen=True)
class ObservationResult:
    """The persisted result of one tool-set observation, for the route response."""

    tools_hash: str
    approval_status: str
    drifted: bool
    newly_drifted: bool
    previous_tools_hash: str | None


async def _append_drift_audit(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    server: McpServer,
    actor_user_id: uuid.UUID | None,
    previous_hash: str | None,
) -> None:
    """Best-effort audit append for a NEW drift event (fail-open).

    ``append_audit_event`` already wraps each attempt in its own savepoint, so a
    failure here cannot roll back the observation itself.
    """
    try:
        await append_audit_event(
            session,
            org_id=org_id,
            event_type=_DRIFT_AUDIT_EVENT_TYPE,
            actor_user_id=actor_user_id,
            resource_type="mcp_server",
            resource_id=server.id,
            payload_json={
                "name": server.name,
                "previous_tools_hash": previous_hash,
                "observed_tools_hash": server.tools_hash,
                "approved_hash": server.approved_hash,
            },
        )
    except SQLAlchemyError:
        _log.exception("mcp.tool_pinning.drift_audit_failed mcp_server_id=%s", server.id)


async def _create_drift_notification(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    server: McpServer,
) -> None:
    """Best-effort operator notification for a NEW drift event (fail-open, savepoint)."""
    try:
        async with session.begin_nested():
            await create_notification(
                session,
                org_id=org_id,
                scope="org",
                level="warning",
                category=_DRIFT_NOTIFICATION_CATEGORY,
                title=f"MCP server '{server.name}' tool definitions changed",
                body=(
                    "The MCP server's tool definitions no longer match the approved set. "
                    "Its tool pin is now 'approved-stale' and it should be re-reviewed. "
                    "Modulo does not block the server (it is outside Modulo's control); this is a prompt to re-approve."
                ),
                action_url="/settings/mcp",
            )
    except SQLAlchemyError:
        _log.exception("mcp.tool_pinning.drift_notification_failed mcp_server_id=%s", server.id)


async def apply_tool_observation(
    session: AsyncSession,
    *,
    server: McpServer,
    org_id: uuid.UUID,
    tools: Sequence[Mapping[str, Any]],
    actor_user_id: uuid.UUID | None,
) -> ObservationResult:
    """Persist one tool-set observation and apply the pin/drift decision.

    Stores ``tools_json``/``tools_hash``/``last_observed_at`` and the decided
    ``approval_status``. On a NEW drift (a previously-approved tool set changed)
    it also appends an audit event and raises an operator notification; both are
    best-effort (each runs in its own savepoint) so an alerting failure can never
    roll back the observation itself. Caller owns the transaction and RLS
    context.

    Raises :class:`ToolPinningError` for a malformed tool set (never persists).
    """
    observed_hash = compute_tools_hash(tools)
    previous_hash = server.tools_hash
    previous_status = server.approval_status
    outcome = decide_observation(
        observed_hash=observed_hash,
        approved_hash=server.approved_hash,
        current_status=previous_status,
    )
    newly_drifted = outcome.drifted and previous_status != APPROVAL_STATUS_APPROVED_STALE

    server.tools_json = [dict(tool) for tool in tools]
    server.tools_hash = outcome.tools_hash
    server.approval_status = outcome.approval_status
    server.last_observed_at = datetime.now(UTC)
    await session.flush()

    if newly_drifted:
        await _append_drift_audit(
            session,
            org_id=org_id,
            server=server,
            actor_user_id=actor_user_id,
            previous_hash=previous_hash,
        )
        await _create_drift_notification(session, org_id=org_id, server=server)

    return ObservationResult(
        tools_hash=outcome.tools_hash,
        approval_status=outcome.approval_status,
        drifted=outcome.drifted,
        newly_drifted=newly_drifted,
        previous_tools_hash=previous_hash,
    )
