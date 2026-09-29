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

    Eval configs may reference evidence keys directly or via key-prefix
    patterns.  Returns the set of concrete key strings found.
    """
    keys: set[str] = set()
    config = eval_row.config_json or {}

    # Direct key reference
    if "evidence_key" in config:
        keys.add(str(config["evidence_key"]))

    # Key patterns in config
    if "key" in config:
        keys.add(str(config["key"]))

    # Nested key references (common in guardrail configs)
    detection = config.get("detection")
    if isinstance(detection, dict):
        if "evidence_key" in detection:
            keys.add(str(detection["evidence_key"]))
        if "key" in detection:
            keys.add(str(detection["key"]))

    return keys


def _extract_evidence_keys_from_node(node: dict[str, Any]) -> set[str]:
    """Extract evidence key patterns from a pipeline graph node.

    Nodes may configure evidence-producing commands or hooks that write
    specific keys.
    """
    keys: set[str] = set()

    # Agent/sandbox nodes may have evidence-producing config
    config = node.get("config", {})
    if isinstance(config, dict):
        if "evidence_key" in config:
            keys.add(str(config["evidence_key"]))
        if "key" in config:
            keys.add(str(config["key"]))

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


async def check_author_warnings(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    evidence_key: str,
    binding_node_id: uuid.UUID,
) -> list[AuthorWarning]:
    """Check for racy/indeterminate evidence key warnings (§3.2).

    Returns a list of advisory warnings.  An empty list means no concerns
    were detected.  The check NEVER raises — all failures produce warnings
    (the safe direction).

    Parameters:
        session: Database session.
        org_id: Organisation ID.
        pipeline_id: Pipeline ID.
        evidence_key: The evidence key being bound to a gate.
        binding_node_id: The node where the gate is being bound.

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

        graph_nodes = pipeline.graph_nodes_json or []
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
        # Check 1: eval definitions bound to this pipeline
        evals_result = await session.execute(
            select(Eval).where(
                Eval.pipeline_id == pipeline_id,
                Eval.organisation_id == org_id,
                Eval.deleted_at.is_(None),
            )
        )
        evals = evals_result.scalars().all()

        producer_found = False
        producer_node_id: uuid.UUID | None = None

        for eval_row in evals:
            eval_keys = _extract_evidence_keys_from_eval(eval_row)
            if evidence_key in eval_keys:
                producer_found = True
                producer_node_id = eval_row.node_id
                break

        # Check 2: evidence-producing node configurations
        if not producer_found:
            for node in graph_nodes:
                node_keys = _extract_evidence_keys_from_node(node)
                if evidence_key in node_keys:
                    producer_found = True
                    node_id_raw = node.get("id")
                    if node_id_raw is not None:
                        producer_node_id = (
                            uuid.UUID(str(node_id_raw)) if not isinstance(node_id_raw, uuid.UUID) else node_id_raw
                        )
                    break

        # Check 3: system-state producer patterns
        if not producer_found and _is_system_state_key(evidence_key):
            producer_found = True
            # System-state producers have no single node — they're runtime
            # builtins.  Temporal ordering is N/A for them.

        if not producer_found:
            warnings.append(
                AuthorWarning(
                    "no_producer",
                    f"Key '{evidence_key}' has no guaranteed producer in this pipeline.",
                )
            )

        # ── Condition (b): temporal ordering ───────────────────────────────
        # Only check if we found a producer with a known node_id
        if (
            producer_found
            and producer_node_id is not None
            and _is_producer_downstream(producer_node_id, binding_node_id, edges)
        ):
            warnings.append(
                AuthorWarning(
                    "temporal_ordering",
                    f"Key '{evidence_key}' producer node runs after the gate's binding node.",
                )
            )

        # ── Condition (c): recent undefined ────────────────────────────────
        # Query the evidence store for recent undefined outcomes on this key.
        # The evidence store may be dormant (no rows yet) — this is expected
        # and produces no warning.
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
            if undefined_count > 0:
                warnings.append(
                    AuthorWarning(
                        "recent_undefined",
                        f"Key '{evidence_key}' produced undefined in "
                        f"{undefined_count} of the last {len(recent_values)} runs.",
                    )
                )
        except Exception:
            # Evidence store query failure — fallback to warn (safe direction)
            _log.warning(
                "author_warnings.evidence_store_query_failed",
                extra={"key": evidence_key, "org_id": str(org_id)},
                exc_info=True,
            )
            warnings.append(
                AuthorWarning(
                    "recent_undefined",
                    f"Cannot determine recent undefined status for key '{evidence_key}': evidence store query failed.",
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
