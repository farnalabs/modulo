"""Author-warning checks for racy/indeterminate evidence keys (FAR-957, chunk 9a §3).

When an operator binds a blocking PolicyGate to an evidence key that can be
indeterminate (undefined) or racy at decision time, this module returns
ADVISORY (non-blocking) warnings.  The warnings surface risk without
preventing binding.

Three conditions (§3.2):
    (a) **No guaranteed producer** — the key appears in no eval definition
        bound to the pipeline, no evidence-producing node config, and no
        system-state producer pattern.
    (b) **Temporal ordering** — the key's producer node is downstream of the
        gate's binding node.
    (c) **Recent undefined** — the key produced undefined in recent runs
        (query the evidence store).

Fallback is the safe direction (§3.2): if the producer set cannot be
determined (graph not loaded, evals not queried, unknown namespace) → warn.

Advisory only (§3.5): the warning NEVER blocks binding — a gate with
warnings is still created.
"""

from __future__ import annotations

import logging
import uuid
from collections import deque
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.models.eval import Eval
from modulo.db.models.evidence import Evidence
from modulo.db.models.pipeline import Pipeline
from modulo.db.models.pipeline_edge import PipelineEdge

_log = logging.getLogger(__name__)

# Number of recent runs to check for undefined outcomes (§3.2).
_RECENT_RUNS_WINDOW = 10

# System-state producer key prefixes (§3.2 item 3).
# Keys matching these patterns are written unconditionally by the runtime.
_SYSTEM_STATE_KEY_PREFIXES = ("system.", "connector_", "sandbox_", "capability_")


class AuthorWarning:
    """A single advisory warning about a gate binding."""

    __slots__ = ("code", "message")

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


def _is_system_state_key(key: str) -> bool:
    """Check if a key matches a system-state producer pattern (§3.2 item 3)."""
    return any(key.startswith(prefix) for prefix in _SYSTEM_STATE_KEY_PREFIXES)


def _extract_evidence_keys_from_eval(eval_row: Eval) -> set[str]:
    """Extract evidence key patterns from an eval definition's config.

    Only the ``evidence_key`` field is treated as a genuine evidence-key
    reference.  A bare ``key`` field is too ambiguous — it appears in many
    non-evidence configs (thresholds, feature keys, etc.) and treating it as
    a producer would suppress genuine ``no_producer`` warnings (the
    dangerous direction — criterion 16 is about not over-warning, but
    over-SUPPRESSING is worse).

    Eval configs may reference evidence keys directly or via key-prefix
    patterns.  Returns the set of concrete key strings found.
    """
    keys: set[str] = set()
    config = eval_row.config_json or {}

    # Direct evidence-key reference (unambiguous)
    if "evidence_key" in config:
        keys.add(str(config["evidence_key"]))

    # Nested key references (common in guardrail detection configs)
    detection = config.get("detection")
    if isinstance(detection, dict) and "evidence_key" in detection:
        keys.add(str(detection["evidence_key"]))

    return keys


def _extract_evidence_keys_from_node(node: dict[str, Any]) -> set[str]:
    """Extract evidence key patterns from a pipeline graph node.

    Only ``evidence_key`` is treated as a genuine evidence-key reference.
    A bare ``key`` field in node config is ambiguous (e.g. feature keys,
    threshold names) and could suppress a valid ``no_producer`` warning.
    """
    keys: set[str] = set()

    # Agent/sandbox nodes may have evidence-producing config
    config = node.get("config", {})
    if isinstance(config, dict) and "evidence_key" in config:
        keys.add(str(config["evidence_key"]))

    # Agent commands may reference evidence keys
    commands = node.get("agent_commands", [])
    if isinstance(commands, list):
        for cmd in commands:
            if isinstance(cmd, dict) and "evidence_key" in cmd:
                keys.add(str(cmd["evidence_key"]))

    return keys


def _is_producer_downstream(
    producer_node_id: uuid.UUID,
    binding_node_id: uuid.UUID,
    edges: list[dict[str, Any]],
) -> bool:
    """Check if producer_node is downstream of binding_node in the pipeline graph.

    Uses BFS from binding_node along edge directions.  Returns True if
    producer_node is reachable from binding_node (i.e., the producer runs
    AFTER the gate evaluates).
    """
    if producer_node_id == binding_node_id:
        return False

    # Build adjacency list (source -> [targets])
    adj: dict[uuid.UUID, list[uuid.UUID]] = {}
    for edge in edges:
        src = edge.get("source_node_id")
        tgt = edge.get("target_node_id")
        if src is None or tgt is None:
            continue
        src_uuid = uuid.UUID(str(src)) if not isinstance(src, uuid.UUID) else src
        tgt_uuid = uuid.UUID(str(tgt)) if not isinstance(tgt, uuid.UUID) else tgt
        adj.setdefault(src_uuid, []).append(tgt_uuid)

    # BFS from binding_node
    visited: set[uuid.UUID] = {binding_node_id}
    queue: deque[uuid.UUID] = deque([binding_node_id])

    while queue:
        current = queue.popleft()
        for neighbor in adj.get(current, []):
            if neighbor == producer_node_id:
                return True
            if neighbor not in visited:
                visited.add(neighbor)
                queue.append(neighbor)

    return False


def _graph_node_uuid(node: dict[str, Any]) -> uuid.UUID | None:
    """Return the graph node's id as a UUID, or None when the entry has none."""
    node_id_raw = node.get("id")
    if node_id_raw is None:
        return None
    if isinstance(node_id_raw, uuid.UUID):
        return node_id_raw
    return uuid.UUID(str(node_id_raw))


async def _check_one_rule_family(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    evidence_key: str,
    graph_nodes: list[dict[str, Any]],
) -> tuple[bool, uuid.UUID | None]:
    """Condition (a): resolve the key's guaranteed producer, if any.

    The producer-lookup ladder — eval definitions bound to the pipeline,
    evidence-key references in graph-node configs, then system-state
    producer patterns — stops at the first hit.  Returns
    ``(producer_found, producer_node_id)``: the node id seeds the
    temporal-ordering check, and is ``None`` for system-state producers
    (runtime builtins have no single node) or a graph node without an id.
    """
    evals_result = await session.execute(
        select(Eval).where(
            Eval.pipeline_id == pipeline_id,
            Eval.organisation_id == org_id,
            Eval.deleted_at.is_(None),
        )
    )

    for eval_row in evals_result.scalars().all():
        if evidence_key in _extract_evidence_keys_from_eval(eval_row):
            return True, eval_row.node_id

    for node in graph_nodes:
        if evidence_key in _extract_evidence_keys_from_node(node):
            return True, _graph_node_uuid(node)

    # System-state producers are written unconditionally by the runtime;
    # temporal ordering is N/A for them.
    if _is_system_state_key(evidence_key):
        return True, None

    return False, None


def _check_temporal_ordering(
    *,
    evidence_key: str,
    producer_node_id: uuid.UUID | None,
    binding_node_id: uuid.UUID | None,
    edges: list[dict[str, Any]],
) -> list[AuthorWarning]:
    """Condition (b): producer-vs-binding temporal ordering (§3.2).

    Only checked when the producer has a known node id.  When the binding
    node is indeterminate the safe direction is to warn — a false
    advisory is preferred over a missed warning.
    """
    if producer_node_id is None:
        return []

    if binding_node_id is None:
        return [
            AuthorWarning(
                "temporal_ordering",
                f"Key '{evidence_key}' producer node is known but binding "
                f"node is indeterminate — temporal ordering cannot be verified.",
            )
        ]

    if _is_producer_downstream(producer_node_id, binding_node_id, edges):
        return [
            AuthorWarning(
                "temporal_ordering",
                f"Key '{evidence_key}' producer node runs after the gate's binding node.",
            )
        ]

    return []


async def _check_recent_undefined(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    evidence_key: str,
) -> list[AuthorWarning]:
    """Condition (c): recent undefined outcomes in the evidence store.

    The evidence store may be dormant (no rows yet) — that is expected
    and produces no warning.  A query failure falls back to warn (the
    safe direction).
    """
    try:
        recent_result = await session.execute(
            select(Evidence.value)
            .where(
                Evidence.organisation_id == org_id,
                Evidence.key == evidence_key,
            )
            .order_by(Evidence.created_at.desc())
            .limit(_RECENT_RUNS_WINDOW)
        )
        recent_values: list[Any] = list(recent_result.scalars().all())

        # undefined = value is None (JSONB null)
        undefined_count = sum(1 for v in recent_values if v is None)
    except Exception:
        # Evidence store query failure — fallback to warn (safe direction)
        _log.warning(
            "author_warnings.evidence_store_query_failed",
            extra={"key": evidence_key, "org_id": str(org_id)},
            exc_info=True,
        )
        return [
            AuthorWarning(
                "recent_undefined",
                f"Cannot determine recent undefined status for key '{evidence_key}': evidence store query failed.",
            )
        ]

    if undefined_count > 0:
        return [
            AuthorWarning(
                "recent_undefined",
                f"Key '{evidence_key}' produced undefined in {undefined_count} of the last {len(recent_values)} runs.",
            )
        ]

    return []


async def check_author_warnings(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    evidence_key: str,
    binding_node_id: uuid.UUID | None = None,
) -> list[AuthorWarning]:
    """Check for racy/indeterminate evidence key warnings (§3.2).

    Returns a list of advisory warnings.  An empty list means no concerns
    were detected.  The check NEVER raises — all failures produce warnings
    (the safe direction).

    When ``binding_node_id`` is ``None`` the gate's position in the pipeline
    graph is indeterminate — temporal-ordering and ordering-related warnings
    cannot be resolved, so a warning is emitted (the safe direction per §3.2).

    Parameters:
        session: Database session.
        org_id: Organisation ID.
        pipeline_id: Pipeline ID.
        evidence_key: The evidence key being bound to a gate.
        binding_node_id: The node where the gate is being bound (or None
            when the binding position is unknown).

    Returns:
        List of AuthorWarning objects (advisory, non-blocking).
    """
    warnings: list[AuthorWarning] = []

    try:
        # Load the pipeline graph
        pipeline_result = await session.execute(
            select(Pipeline).where(
                Pipeline.id == pipeline_id,
                Pipeline.organisation_id == org_id,
            )
        )
        pipeline = pipeline_result.scalar_one_or_none()
        if pipeline is None:
            # Pipeline not found — fallback to warn (safe direction)
            warnings.append(
                AuthorWarning(
                    "no_producer",
                    "Cannot determine producers: pipeline not found.",
                )
            )
            return warnings

        graph_edges_raw = await session.execute(select(PipelineEdge).where(PipelineEdge.pipeline_id == pipeline_id))
        edges_raw = graph_edges_raw.scalars().all()
        edges = [
            {
                "source_node_id": e.source_node_id,
                "target_node_id": e.target_node_id,
            }
            for e in edges_raw
        ]

        # ── Condition (a): no guaranteed producer ──────────────────────────
        producer_found, producer_node_id = await _check_one_rule_family(
            session,
            org_id=org_id,
            pipeline_id=pipeline_id,
            evidence_key=evidence_key,
            graph_nodes=pipeline.graph_nodes_json or [],
        )
        if not producer_found:
            warnings.append(
                AuthorWarning(
                    "no_producer",
                    f"Key '{evidence_key}' has no guaranteed producer in this pipeline.",
                )
            )

        # ── Condition (b): temporal ordering ───────────────────────────────
        warnings.extend(
            _check_temporal_ordering(
                evidence_key=evidence_key,
                producer_node_id=producer_node_id,
                binding_node_id=binding_node_id,
                edges=edges,
            )
        )

        # ── Condition (c): recent undefined ────────────────────────────────
        warnings.extend(
            await _check_recent_undefined(
                session,
                org_id=org_id,
                evidence_key=evidence_key,
            )
        )

    except Exception:
        # Any unexpected failure — fallback to warn (safe direction)
        _log.warning(
            "author_warnings.check_failed",
            extra={
                "key": evidence_key,
                "org_id": str(org_id),
                "pipeline_id": str(pipeline_id),
            },
            exc_info=True,
        )
        warnings.append(
            AuthorWarning(
                "no_producer",
                "Cannot determine producers due to an unexpected error.",
            )
        )

    return warnings
