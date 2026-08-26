"""Node-level capability scoping (FAR-402 P4 / FAR-418).

A pipeline node may declare a least-privilege contract — ``capability_scope`` —
that narrows (never widens) what the node's Agent is granted:

* ``allowed_connectors``: connector instance-ids and/or connector types the node
  may resolve from the ``ConnectorHub``. Deny-by-default within the scope: the
  hub is fetched with ONLY these connectors (fetch-time scoping — the security
  win is that non-scoped credentials are never decrypted at all).
* ``allowed_tools``: MCP/runtime tools the node's agent may invoke. Wired as an
  additional narrowing filter through the existing ``check_tool_scope``
  chokepoint (``modulo.core.mcp.scope_validator``) — no parallel tool-scope
  system.
* ``context_scope``: allowlist of ``run_context`` keys the node may read
  (need-to-know boundary).

Default = UNRESTRICTED. When ``capability_scope`` is absent the node behaves
exactly as before (it may use all of its Agent's grants); the effective
allow-list for that default is populated from the Agent's existing
``connector_type_refs`` (NOT the empty set), so there is no silent break.

Scope violation semantics: a node that would use a connector/tool/context key
excluded by its scope fails fast with a typed, logged, metric-emitting
``ScopeViolationError`` (stable error code ``scope.violation``).

This module is intentionally lightweight (no DB, no LangGraph) so it is
importable from the API layer, the node runner, and unit tests without pulling
heavy graph machinery.
"""

from __future__ import annotations

import dataclasses
import logging
import uuid
from typing import Any

logger = logging.getLogger(__name__)

# Stable dotted error-code taxonomy entry (see pipeline_engine.error_codes).
SCOPE_VIOLATION_CODE = "scope.violation"

# OTel meter namespace shared with the pipeline engine.
_METER_NAME = "modulo.pipeline_engine"

# run_context keys never dropped by context_scope gating: the node's own input
# is already schema-gated upstream and is required for the node to run.
_CONTEXT_ALWAYS_KEPT = ("input",)


class ScopeViolationError(ValueError):
    """Raised when a node uses a capability outside its capability_scope.

    Carries the structured context (node_id, scoped target, kind) so the
    executor can publish the typed ``scope.violation`` error code. The message
    interpolates all three attributes for log readability.
    """

    def __init__(self, *, node_id: str, target: str, kind: str) -> None:
        self.node_id = node_id
        self.target = target
        self.kind = kind
        super().__init__(f"scope.violation node={node_id} {kind}={target}")


def record_scope_violation(*, node_id: str, target: str, kind: str, graph_id: str | None = None) -> None:
    """Emit the ``scope.violation{graph_id,node_id,connector|tool}`` OTel metric.

    Fail-closed: metrics are advisory, so this never raises — a metric backend
    outage must not mask the (already-emitted) typed error or abort the run.
    """
    try:
        from opentelemetry import metrics

        provider = metrics.get_meter_provider()
        if provider is None:
            return
        meter = provider.get_meter(_METER_NAME, version="0.1.0")
        counter = meter.create_counter(
            name="scope_violations_total",
            description="Node capability_scope violations, by node and scoped target",
            unit="1",
        )
        counter.add(1, {"graph_id": graph_id or "", "node_id": node_id, kind: target})
    except Exception:
        logger.debug("scope.metrics_unavailable", exc_info=True)


def agent_granted_connector_types(connector_type_refs: Any) -> set[str]:
    """Extract the granted connector-type strings from an Agent's connector_type_refs.

    Handles both persisted shapes: the dict form ``{"connector_type": "github",
    "capabilities": ["issue_read"]}`` and the legacy string-list form
    ``["github"]``. Returns the empty set for a missing/None column (an agent
    with no grants narrows every node against nothing — nodes that reference no
    connectors are unaffected).
    """
    types: set[str] = set()
    if not connector_type_refs:
        return types
    for ref in connector_type_refs:
        if isinstance(ref, str):
            types.add(ref)
        elif isinstance(ref, dict):
            connector_type = ref.get("connector_type")
            if isinstance(connector_type, str):
                types.add(connector_type)
    return types


def _looks_like_instance_id(value: str) -> bool:
    try:
        uuid.UUID(str(value))
        return True
    except (ValueError, TypeError):
        return False


def validate_allowed_connectors_subset(
    *,
    node_id: str,
    allowed_connectors: list[str] | None,
    granted_types: set[str],
) -> None:
    """Compile-time narrow-not-widen check (never widens).

    Every connector-TYPE entry in ``allowed_connectors`` must exist in the
    node's Agent grants. Connector instance-id entries cannot be checked against
    ``connector_type_refs`` (which carries types only) — they are opaque at
    compile time and enforced at run time by the deny-by-default fetch scope.
    A widen attempt raises ``ScopeViolationError`` so the graph save rejects it.

    When ``allowed_connectors`` is ``None``/empty the node is unrestricted
    (default), so nothing is rejected.
    """
    if not allowed_connectors:
        return
    for entry in allowed_connectors:
        if _looks_like_instance_id(entry):
            continue
        if entry not in granted_types:
            raise ScopeViolationError(node_id=node_id, target=entry, kind="connector")


def is_connector_allowed(
    *,
    connector_instance_id: uuid.UUID,
    connector_type: str,
    allowed_connectors: list[str] | None,
) -> bool:
    """Runtime allow-check for a single connector against a node's scope.

    Unrestricted (``allowed_connectors`` is ``None`` or empty) -> always True,
    matching the pre-scope default (a node may use everything the hub fetched).
    Otherwise the connector must be named by instance-id OR type.
    """
    if not allowed_connectors:
        return True
    id_str = str(connector_instance_id)
    return any(entry in (id_str, connector_type) for entry in allowed_connectors)


def filter_run_context_scope(run_context: dict[str, Any], context_scope: list[str] | None) -> dict[str, Any]:
    """Allowlist-gate the ``run_context`` keys visible to a node's agent.

    Unrestricted (``context_scope`` ``None``/empty) -> ``run_context`` returned
    unchanged. When set, only ``context_scope`` keys (plus ``_CONTEXT_ALWAYS_KEPT``)
    are retained — a need-to-know boundary. Returns a shallow copy; the original
    mapping is never mutated.
    """
    if not context_scope:
        return run_context
    allowed = set(context_scope)
    return {k: v for k, v in run_context.items() if k in allowed or k in _CONTEXT_ALWAYS_KEPT}


def compute_run_fetch_scope(
    graph_json: dict[str, Any] | None,
    agent_connector_grants: dict[str, set[str]] | None = None,
) -> list[str] | None:
    """Derive the ConnectorHub *fetch-time* scope for an entire run (FAR-435).

    The hub decrypts credentials once per run, so the fetch set is run-wide: the
    UNION of every node's connector needs. The scope is a SUPERSET of every
    contributing node's needs, so a per-node narrow-gate can never be silently
    defeated by an over-tight run scope.

    Per-node contribution (FAR-418: credentials outside a node's scope are never
    decrypted):

    * A node that declares a non-empty ``capability_scope.allowed_connectors``
      contributes EXACTLY those connectors. Its Agent's broader ``connector_type_refs``
      grants are NOT unioned in — widening a narrowed node would be a FAR-418
      regression, and any other (unrestricted) node that needs those grants
      contributes them independently.
    * A node that is connector-unrestricted (no ``capability_scope``, or a scope
      with an empty/absent ``allowed_connectors``) but is bound to an Agent
      contributes that Agent's ``connector_type_refs`` grants — an agent node has
      no static connector binding and tool-calls the hub at runtime, so its grants
      must be fetched for its runtime access to survive. This is WITHIN scope: an
      unrestricted agent node may use everything its grant allows.
    * A connector-unrestricted node with NO agent scope contributes nothing and is
      subject to the union.

    Returns the sorted union, or ``None`` when the union is EMPTY — the
    unrestricted/legacy default (no narrowing; the hub fetches every active org
    connector). This is a DELIBERATE tightening vs the earlier contract: a run that
    MIXES scoped and unscoped nodes no longer falls back to ``None``/fetch-everything
    — it fetches only the scoped union. The legacy "fetch everything" behaviour is
    preserved ONLY for runs in which no node contributes any connector (fully
    unrestricted runs). ``agent_connector_grants`` maps ``agent_id`` (string) → the
    granted connector-type strings (see :func:`agent_granted_connector_types`).
    """
    nodes = (graph_json or {}).get("nodes", [])
    if not nodes:
        return None
    scope: set[str] = set()
    for node in nodes:
        if not isinstance(node, dict):
            continue
        cap_scope = node.get("capability_scope") or {}
        allowed = cap_scope.get("allowed_connectors") if isinstance(cap_scope, dict) else None
        if allowed:
            # Node-level narrow scope — never widen beyond it with agent grants.
            scope.update(str(entry) for entry in allowed if entry)
            continue
        # Connector-unrestricted: an agent node needs its full grants; a
        # non-agent unrestricted node contributes nothing and is subject to the
        # union (FAR-435 least-privilege tightening).
        agent_id = node.get("agent_id")
        if agent_id is not None and agent_connector_grants:
            grants = agent_connector_grants.get(str(agent_id))
            if grants:
                scope.update(grants)
    return sorted(scope) if scope else None


def _is_connector_object(value: Any) -> bool:
    """Duck-typed connector/secret-object identity check (no heavy import).

    A packaged connector object exposes a ``connector_type`` property and a
    callable ``query`` — the _TracedConnector proxy forwards both via
    ``__getattr__``, so this also rejects the wrapped (secret-bearing) form.
    Plain dicts / ConnectorResult records are never matched.
    """
    return hasattr(value, "connector_type") and callable(getattr(value, "query", None))


def assert_no_secret_objects(value: Any, *, node_id: str, _seen: set[int] | None = None) -> None:
    """Secret-hygiene guard: connector/secret OBJECTS are never valid port payloads.

    Only opaque connector IDs may appear in state/ports. Recursively rejects any
    connector/secret object (see :func:`_is_connector_object`) with a typed
    ``ScopeViolationError``; plain-serializable data always passes. Raises on the
    first offending nested value.

    The production query path returns a ``ConnectorResult`` *dataclass* whose
    ``records`` carry the real payload — a shape that is neither ``dict`` nor
    ``list``/``tuple``, so the guard must descend into dataclass fields (and, as
    a backstop, arbitrary object ``__dict__``) or a smuggled connector/secret
    object riding inside ``ConnectorResult.records`` would slip past the guard.
    A ``_seen`` id-set prevents infinite recursion on cyclic payloads.
    """
    if _seen is None:
        _seen = set()
    obj_id = id(value)
    if obj_id in _seen:
        return
    _seen.add(obj_id)
    if _is_connector_object(value):
        raise ScopeViolationError(
            node_id=node_id,
            target=type(value).__name__,
            kind="secret",
        )
    if isinstance(value, dict):
        for nested in value.values():
            assert_no_secret_objects(nested, node_id=node_id, _seen=_seen)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            assert_no_secret_objects(nested, node_id=node_id, _seen=_seen)
    elif dataclasses.is_dataclass(value) and not isinstance(value, type):
        for field in dataclasses.fields(value):
            assert_no_secret_objects(getattr(value, field.name), node_id=node_id, _seen=_seen)
    elif hasattr(value, "__dict__") and not isinstance(value, (str, bytes, bytearray, int, float, bool)):
        # Backstop for non-dataclass objects (agents may wrap payloads in plain
        # classes). Skip primitives and types; descend into attributes.
        for nested in vars(value).values():
            assert_no_secret_objects(nested, node_id=node_id, _seen=_seen)
