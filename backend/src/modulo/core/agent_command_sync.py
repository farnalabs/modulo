"""FAR-488a / FAR-2008: node ``agent_commands`` → bound Agent row sync.

Every writer of a pipeline graph must keep the bound ``Agent`` rows in step
with the node-level ``agent_commands`` that graph declares, inside the SAME
transaction as the graph write and BEFORE it: a run reads an immutable
snapshot, and snapshot materialisation
(:func:`modulo.db.crud.pipeline_snapshot._apply_agent_fields`) lets a non-NULL
Agent-row ``agent_commands`` OVERWRITE the node value. A graph write that only
persisted ``graph_nodes_json`` therefore read back correctly while every run
silently executed the stale Agent-row command (the FAR-488 incident).

This module is the neutral home for that contract so every writer can share
ONE implementation:

* the REST full-graph / graph-in-PATCH / node-conversion writers
  (``modulo.api.routes.pipelines``),
* the MCP full-graph writer (``modulo.api.mcp_server``),
* snapshot rollback (``modulo.api.routes.pipelines``' post-lock hook), and
* bundle import (``modulo.core.workflow_import_export``).

It cannot live only in ``api/routes/pipelines.py`` because bundle import runs
in ``core`` and core may not import api (import-linter
``core-does-not-import-api``); it cannot live in the db layer because the db
layer may not import api either and the import/pipeline writers are not db
code. It imports only ``db.models.Agent`` and the pure Agent save gate in
``core.pipeline_engine.git_content``, so it sits happily inside ``core``.

Field ownership contract (FAR-2008 decision — the sync is deliberately
narrow)
--------------------------------------------------------------------------

At snapshot time ``_apply_agent_fields`` OVERWRITES five node fields from the
bound Agent row — these are Agent-authoritative:

* ``token_budget``
* ``prompt_template``
* ``model_backend_id``
* ``agent_commands``
* ``parameter_schema_id``

and it only supplies a DEFAULT for ``schema_profile`` (``setdefault``), which
is node-authoritative — an explicit node value wins over the Agent row.

Of those five Agent-authoritative fields, ``agent_commands`` is the ONLY one
the typed REST graph model (``PipelineGraphNode``) exposes as a node-level
editable field. It is therefore the only field this sync covers: a save/restore
writes exactly the command the caller declared, so "what you save/restore is
what runs". The other four Agent-authoritative fields stay owned by the Agent:
copying a historical snapshot's stale values onto a SHARED Agent row would
silently revert an Agent edit made after that snapshot was taken (one Agent row
can be bound by many pipelines). Adding them here is explicitly out of scope.

``schema_profile`` is the sixth field in the list above, but it is NOT part of
this contract: it is node-authoritative (``_apply_agent_fields`` only supplies
it via ``setdefault``), so it is intentionally neither synced nor claimed here.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, TypeGuard

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.core.pipeline_engine.git_content import validate_agent_git_content_values
from modulo.db.models.agent import Agent

logger = logging.getLogger(__name__)


def is_clean_agent_commands(value: object) -> TypeGuard[list[str]]:
    """Whether ``value`` is a syncable node-level ``agent_commands`` list (FAR-2008).

    The bound ``Agent`` row's column is typed ``list[str] | None``, so a list
    carrying ANY non-``str`` item cannot be persisted without breaking
    ``AgentResponse`` validation on every subsequent read of the organisation's
    agents (``GET /api/v1/agents`` — one bad row takes the whole list down).
    A node value is therefore accepted only when it is

    * a non-empty ``list``,
    * whose EVERY item is a ``str`` (empty strings are allowed — ``list[str]``
      permits them), and
    * with at least one NON-EMPTY string, so a commandless list never clobbers
      a populated Agent row.

    Anything else — a scalar, a mixed-type list such as ``["ok", 3]``, ``[None]``,
    an empty list, an empty-string-only list — is treated as ABSENT. Callers
    skip the WHOLE value rather than syncing its well-typed prefix: a partial
    sync would persist a command set nobody declared.

    A ``TypeGuard``: a true result narrows the caller's value to ``list[str]``.
    """
    if not isinstance(value, list) or not value:
        return False
    has_non_empty = False
    for item in value:
        if not isinstance(item, str):
            return False
        if item:
            has_non_empty = True
    return has_non_empty


def extract_agent_command_sync_updates(nodes: list[dict[str, Any]]) -> dict[uuid.UUID, list[str]]:
    """FAR-488a: full ``agent_commands`` list per distinct bound agent.

    A node WITHOUT an ``agent_id`` needs no sync (its node-level command always
    stands at snapshot time — ``_apply_agent_fields`` only materializes bound
    agents). A node carrying no usable ``agent_commands`` value has nothing to
    sync. The WHOLE list is carried (not just its first item) so a multi-item
    bound node keeps every command at snapshot time — the Agent row is what
    actually runs, and truncating to the first item would be a silent
    divergence (FAR-488-class). When several nodes bind the SAME agent, the
    FIRST node's list wins (deterministic; snapshot materialization applies one
    row value to every node bound to that agent, so per-node divergence is not
    representable).

    Requirement (FAR-2008): the value must be a non-empty ``list[str]`` — every
    item a ``str`` — with at least one non-empty string, per
    :func:`is_clean_agent_commands`. A mixed list such as ``["ok", 3]`` is
    skipped ENTIRELY (never partially synced): the target column is
    ``list[str] | None``, so writing it verbatim would wedge
    ``GET /api/v1/agents`` response validation for the whole organisation.
    Callers that accept untrusted input (bundle import) additionally strip such
    a value from the graph and warn, so the graph and the Agent row agree.
    """
    updates: dict[uuid.UUID, list[str]] = {}
    for node in nodes:
        if not isinstance(node, dict):
            continue
        raw_agent_id = node.get("agent_id")
        agent_commands = node.get("agent_commands")
        if raw_agent_id is None or not is_clean_agent_commands(agent_commands):
            continue
        try:
            agent_id = uuid.UUID(str(raw_agent_id))
        except (TypeError, ValueError):
            continue
        updates.setdefault(agent_id, list(agent_commands))
    return updates


async def sync_agent_row_commands(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    nodes: list[dict[str, Any]],
    pipeline_id: uuid.UUID,
) -> list[uuid.UUID]:
    """FAR-488a: sync a written node ``agent_commands`` into the bound Agent row.

    At snapshot time ``_apply_agent_fields`` overwrites a bound node's
    ``agent_commands`` with the Agent row's non-NULL value, while a graph write
    that persisted node-level commands to ``graph_nodes_json`` only read back
    correctly but every run silently executed the stale Agent-row command
    (FAR-488 incident, 2026-08-29). Syncing the row inside the SAME
    transaction as the graph write makes "what you write is what runs" hold.

    Deliberate skip cases: a node without ``agent_id`` (nothing bound — the
    node value already stands); an Agent row with a NULL ``agent_commands``
    (the node value already stands at snapshot time); an incoming list equal to
    the row value (no-op — including a multi-item list already matching the
    node, so re-saving an unchanged multi-item bound node does NOT drop items).
    Returns the IDs of the Agent rows updated (empty when nothing changed).

    ``pipeline_id`` is required attribution (FAR-2008): every caller knows
    which pipeline's graph it is writing, so the structured log line names the
    pipeline as well as the Agent. It never affects which rows are selected or
    written.

    FAR-220: every incoming command list passes the shared Agent save gate
    (:func:`validate_agent_git_content_values`) BEFORE any write. The graph
    validator's git-content gate runs only for ``sandbox_agent`` nodes, so an
    ``agent``-type node could otherwise carry an unpinned ``git+`` ref into
    this sync and overwrite a gated Agent row ungated — validation would pass
    and the poisoned row would flow into bound sandbox nodes at snapshot time
    via ``_apply_agent_fields``. The typed :class:`GitContentRefError`
    propagates out of the enclosing transaction, so the graph write rolls back
    and the route handlers map it to 422.
    """
    updates = extract_agent_command_sync_updates(nodes)
    if not updates:
        return []
    # Fail closed before any read or write: an unpinned ref is invalid input
    # regardless of the row's current value, so even a no-op PATCH carrying
    # one is rejected (the row itself must be remediated through the gated
    # Agent save paths first).
    for incoming_commands in updates.values():
        validate_agent_git_content_values(agent_commands=incoming_commands)
    result = await session.execute(select(Agent).where(Agent.id.in_(list(updates)), Agent.organisation_id == org_id))
    changed: list[uuid.UUID] = []
    for agent in result.scalars():
        incoming = updates.get(agent.id)
        if incoming is None or agent.agent_commands is None or agent.agent_commands == incoming:
            continue
        logger.info(
            "pipeline.graph.agent_commands_synced",
            extra={
                "agent_id": str(agent.id),
                "organisation_id": str(org_id),
                "pipeline_id": str(pipeline_id),
            },
        )
        agent.agent_commands = list(incoming)
        changed.append(agent.id)
    return changed
