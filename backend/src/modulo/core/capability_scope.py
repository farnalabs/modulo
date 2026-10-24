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
from collections.abc import Iterable
from typing import Any

logger = logging.getLogger(__name__)

# Stable dotted error-code taxonomy entry (see pipeline_engine.error_codes).
SCOPE_VIOLATION_CODE = "scope.violation"

# ``ScopeViolationError.kind`` for a blank / whitespace-only
# ``allowed_connectors`` entry. Distinct from a widen (``connector``) because it
# is not a scope violation at all — it is a meaningless entry, and the route
# attaches the remedy it needs (remove it, or leave the list empty) to the 422.
BLANK_CONNECTOR_KIND = "blank_connector"

# OTel meter namespace shared with the pipeline engine.
_METER_NAME = "modulo.pipeline_engine"

# run_context keys never dropped by context_scope gating: the node's own input
# is already schema-gated upstream and is required for the node to run.
_CONTEXT_ALWAYS_KEPT = ("input",)

# FAR-620: the caller-scoped (FAR-614) permission-key suffix. A tool whose
# permission key ends in ``.self`` operates on the CALLER'S OWN account and is
# therefore forbidden from node allowed_tools (see validate_no_self_tools).
_SELF_SUFFIX = ".self"


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
    """Return True when *value* parses as a UUID (a connector instance-id).

    Used to split ``allowed_connectors`` entries into instance-ids (opaque at
    compile time, enforced by the runtime fetch scope) and connector TYPES
    (checkable against the Agent's grants).
    """
    try:
        uuid.UUID(str(value))
        return True
    except (ValueError, TypeError):
        return False


def _is_malformed_connector_scope(allowed_connectors: Any) -> bool:
    """True when a non-``None`` value cannot express a connector allowlist.

    Every REST graph write coerces ``allowed_connectors`` to ``list[str] | None``
    through the Pydantic node model, so this shape is reachable only through a
    writer that stores raw graph dicts — MCP ``update_pipeline_graph``, the
    workflow-bundle import, library/template install, clone and snapshot
    rollback — none of which run the save-time checks. Such a value is
    unintelligible, NOT "unrestricted": the readers fail CLOSED on it, because a
    security gate must never read malformed input as permission.
    """
    return allowed_connectors is not None and not isinstance(allowed_connectors, (str, list, tuple))


def _non_blank_connector_entries(allowed_connectors: Any) -> set[str]:
    """The STRIPPED, non-blank entries of an ``allowed_connectors`` value.

    ``capability_scope.allowed_connectors`` is documented as "``None`` or EMPTY
    means connector-unrestricted", and a blank / whitespace-only entry can never
    name a connector — it is only ever a placeholder (an empty form field, a
    cleared list row). Returning them stripped has two consequences, both
    required for the documented contract to hold:

    * a list that is TRUTHY but contains only blanks normalises to the EMPTY
      set, so it behaves exactly like an ABSENT list (unrestricted) instead of
      silently narrowing the node to nothing (which made
      ``_node_connector_contribution`` contribute NOTHING, drove
      ``compute_run_fetch_scope`` to ``None``, and made the hub decrypt EVERY
      active org connector — the over-exposure FAR-2089 fixed for the absent
      case);
    * a whitespace-padded entry normalises to the connector it names, so it can
      never be contributed (or matched) verbatim as a scope string that matches
      no connector at all.

    This is the ONE interpretation of an entry: both runtime readers and the
    save-time widen check go through here, so save and run can never disagree
    about what an entry means.

    A non-empty result stays AUTHORITATIVE — this helper never widens, so a node
    that genuinely narrows to specific connectors still contributes exactly
    those (the FAR-418 invariant).
    """
    if not allowed_connectors:
        return set()
    # A BARE STRING is ONE entry, never a sequence of characters: iterating it
    # would turn "github" into {"g", "i", "t", ...} — a scope naming no connector
    # at all. A blank bare string names nothing, so it is unrestricted exactly
    # like a blank list entry.
    if isinstance(allowed_connectors, str):
        text = allowed_connectors.strip()
        return {text} if text else set()
    if not isinstance(allowed_connectors, (list, tuple)):
        # Malformed (a dict, a number, an arbitrary object): it cannot express an
        # allowlist, so it contributes nothing here — each reader fails CLOSED on
        # it through :func:`_is_malformed_connector_scope` rather than treating
        # the empty contribution as unrestricted.
        return set()
    entries: set[str] = set()
    for entry in allowed_connectors:
        text = str(entry).strip()
        if text:
            entries.add(text)
    return entries


def validate_allowed_connectors_not_blank(
    *,
    node_id: str,
    allowed_connectors: list[str] | None,
) -> None:
    """Save-time guard: a blank / whitespace-only ``allowed_connectors`` entry is
    REFUSED rather than silently stored.

    Such an entry is meaningless (it can name no connector) and destructive: a
    blanks-only list is truthy, so before this guard it stored a node that was
    "narrowed" to nothing — the run computed an empty scope and fetched every
    active org connector, while the node itself matched no connector at the hub.

    This guard's coverage is deliberately NARROW: it fires only on the REST
    graph-write paths that run ``_validate_capability_scopes`` (exactly one call
    site, inside ``_resolve_graph_references``). Every other writer persists raw
    graph dicts and never reaches it — MCP ``update_pipeline_graph``, the
    workflow-bundle import, library/template install, pipeline clone and snapshot
    rollback can all still store ``allowed_connectors: [""]``. The runtime
    normalisation in :func:`_non_blank_connector_entries` is therefore the
    BACKSTOP for every graph those writers store (and for every graph stored
    before this guard existed); refusing the write covers the REST routes only,
    so a runtime reader must never assume a persisted list is non-blank.

    Applies to EVERY scoped node (checked before the Agent reference is
    consulted): blankness is a property of the list, independent of what the
    node's Agent grants. ``None``/empty stays valid (unrestricted).
    """
    if not allowed_connectors:
        return
    for entry in allowed_connectors:
        if not str(entry).strip():
            # repr() so the offending entry is visible in the 422 detail: an
            # empty string and a whitespace-only string are otherwise
            # indistinguishable from a missing value. The dedicated ``kind`` is
            # what lets the route attach the remedy this entry needs.
            raise ScopeViolationError(
                node_id=node_id,
                target=repr(entry),
                kind=BLANK_CONNECTOR_KIND,
            )


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

    A blank / whitespace-only entry is rejected first (see
    :func:`validate_allowed_connectors_not_blank`) — it is not a widen, it is a
    meaningless entry that must never be persisted.

    Entries are classified AFTER the same strip both runtime readers apply (see
    :func:`_non_blank_connector_entries`), so save and run interpret an entry
    identically. Classifying the raw value made the two disagree: a padded
    instance-id (``" <uuid> "``) was read as a connector TYPE and refused as a
    widen, while the runtime stripped it into a legitimate instance-id and
    honoured it.

    When ``allowed_connectors`` is ``None``/empty the node is unrestricted
    (default), so nothing is rejected.
    """
    validate_allowed_connectors_not_blank(node_id=node_id, allowed_connectors=allowed_connectors)
    for entry in _non_blank_connector_entries(allowed_connectors):
        if _looks_like_instance_id(entry):
            continue
        if entry not in granted_types:
            raise ScopeViolationError(node_id=node_id, target=entry, kind="connector")


def self_scoped_tools() -> frozenset[str]:
    """Registry-derived set of tools whose permission key ends in ``.self``.

    FAR-620: the ``.self`` suffix marks caller-scoped tools (FAR-614) — the
    tool operates on the CALLER'S OWN account. Derived from
    ``TOOL_SCOPE_REQUIREMENTS`` at call time so the permission registry stays
    the single source of truth (never a hardcoded tool list). The lazy import
    keeps this module's import surface free of the auth layer.
    """
    from modulo.core.mcp.scope_validator import TOOL_SCOPE_REQUIREMENTS

    return frozenset(
        tool for tool, permission_key in TOOL_SCOPE_REQUIREMENTS.items() if permission_key.endswith(_SELF_SUFFIX)
    )


def validate_no_self_tools(*, node_id: str, allowed_tools: list[str] | None) -> None:
    """Save-time guard: a node's ``allowed_tools`` may not name a ``.self`` tool.

    FAR-620 spec item 10: FAR-614-classified (caller-scoped, ``.self``) tools
    are FORBIDDEN from node ``capability_scope.allowed_tools``. A pipeline node
    is an org-context principal — it can never be the "caller" a ``.self``
    tool operates on, so allowing one in a node's tool list would either fail
    at run time (the resolver denies non-user credentials on caller-scoped
    tools, fail-closed) or mislead graph authors into shipping a dead node.
    This compile-time twin mirrors :func:`validate_allowed_connectors_subset`:
    a violation raises ``ScopeViolationError`` so the graph save is REFUSED
    (422 at the route) instead of discovered mid-run. Runtime stays fail-closed
    regardless. Unrestricted (``allowed_tools`` ``None``/empty) passes.
    """
    if not allowed_tools:
        return
    forbidden = self_scoped_tools()
    for entry in allowed_tools:
        if entry in forbidden:
            raise ScopeViolationError(node_id=node_id, target=entry, kind="self_tool")


def is_connector_allowed(
    *,
    connector_instance_id: uuid.UUID,
    connector_type: str,
    allowed_connectors: list[str] | None,
) -> bool:
    """Runtime allow-check for a single connector against a node's scope.

    Unrestricted (``allowed_connectors`` is ``None``, empty, or blanks-only)
    -> always True, matching the pre-scope default (a node may use everything
    the hub fetched). Otherwise the connector must be named by instance-id OR
    type. Blanks-only counts as unrestricted because a blank entry names no
    connector: treating it as a deny-everything scope would fail a node whose
    own binding the run fetch scope (correctly) included.

    A bare string is ONE entry, never a character sequence. Any other
    non-list/non-tuple value (a dict, a number, an object) is malformed and
    fails CLOSED — it is reachable only through a raw-dict writer, and a
    security gate must not read unintelligible input as permission.
    """
    if _is_malformed_connector_scope(allowed_connectors):
        return False
    entries = _non_blank_connector_entries(allowed_connectors)
    if not entries:
        return True
    id_str = str(connector_instance_id)
    return any(entry in (id_str, connector_type) for entry in entries)


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


def _node_connector_contribution(
    node: Any,
    agent_connector_grants: dict[str, set[str]] | None,
) -> set[str]:
    """Return the connector id/type strings ONE graph node contributes.

    A node-level ``capability_scope.allowed_connectors`` is authoritative and is
    never widened with agent grants (FAR-418). A connector-unrestricted agent
    node contributes its Agent's ``connector_type_refs`` grants; a node that
    would otherwise contribute NOTHING but whose binding the engine ROUTES to a
    connector contributes that binding's instance id; any other unrestricted
    node contributes nothing (FAR-435 least-privilege tightening).

    "Unrestricted" means the list is ``None``, EMPTY, or contains only blank /
    whitespace-only entries — a blank entry names no connector, so a blanks-only
    list is not a narrowing (see :func:`_non_blank_connector_entries`). It
    falls through to the arms below instead of contributing the empty set,
    which previously drove the run scope to ``None`` (fetch-everything) while
    omitting the very connector the node names. A MALFORMED value (not a string,
    list or tuple — reachable only through a raw-dict writer) is not
    unrestricted: it contributes nothing and never falls through to the arms
    below, so it can never be widened with the Agent's grants (see
    :func:`_is_malformed_connector_scope`).

    The routed-binding arm (FAR-2089) exists because a ``connector`` /
    ``dispatch`` node has no ``agent_id`` and usually no ``capability_scope`` —
    without it such a node contributed NOTHING, the union came out empty, and
    ``compute_run_fetch_scope`` returned ``None``, so the hub decrypted EVERY
    active org connector for a run that needs exactly one. Contributing the
    instance id keeps the union non-empty and narrows the fetch to the
    connector the node actually names. It is consulted only after the agent
    grant arm, so every shape that contributed before contributes the same set.
    """
    if not isinstance(node, dict):
        return set()
    cap_scope = node.get("capability_scope") or {}
    allowed = cap_scope.get("allowed_connectors") if isinstance(cap_scope, dict) else None
    # Filter blanks BEFORE the truthiness test: `[""]` is truthy but narrows to
    # nothing, so it must behave like an absent list (unrestricted).
    narrowed = _non_blank_connector_entries(allowed)
    if narrowed:
        return narrowed
    if _is_malformed_connector_scope(allowed):
        # Fail closed: a malformed scope contributes NOTHING and is never widened
        # with the Agent's grants (FAR-418) — the runtime allow-check denies the
        # node instead of the malformed value reading as unrestricted.
        return set()
    agent_id = node.get("agent_id")
    if agent_id is not None and agent_connector_grants:
        granted = set(agent_connector_grants.get(str(agent_id)) or set())
        if granted:
            return granted
    # Lazy import: this module is deliberately import-light (it is pulled in by
    # the API layer and the node runner), and the routing predicate lives in the
    # connectors layer.
    from modulo.connectors.base import node_routes_binding_to_connector

    if node_routes_binding_to_connector(node):
        binding = node.get("connector_binding") or {}
        instance_id = binding.get("instance_id") if isinstance(binding, dict) else None
        if instance_id:
            return {str(instance_id)}
    return set()


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
    decrypted) lives in :func:`_node_connector_contribution`:

    * A node that declares a non-blank (i.e. genuinely narrowing)
      ``capability_scope.allowed_connectors`` contributes EXACTLY those
      connectors. Its Agent's broader ``connector_type_refs`` grants are NOT
      unioned in — widening a narrowed node would be a FAR-418 regression, and
      any other (unrestricted) node that needs those grants contributes them
      independently.
    * A node that is connector-unrestricted (no ``capability_scope``, or a scope
      with an empty/absent/blanks-only ``allowed_connectors``) but is bound to an Agent
      contributes that Agent's ``connector_type_refs`` grants — an agent node has
      no static connector binding and tool-calls the hub at runtime, so its grants
      must be fetched for its runtime access to survive. This is WITHIN scope: an
      unrestricted agent node may use everything its grant allows.
    * A node that is connector-unrestricted (no ``capability_scope``, or a scope
      with an empty/absent/blanks-only ``allowed_connectors``) but whose ``connector_binding``
      the engine ROUTES to a connector (a ``connector`` node, a ``dispatch``
      node) contributes that binding's ``instance_id`` — the run needs exactly
      the connector the node names, so it is fetched and nothing else is.
    * A connector-unrestricted node with NO agent scope and no routed binding
      contributes nothing and is subject to the union.

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
        scope.update(_node_connector_contribution(node, agent_connector_grants))
    return sorted(scope) if scope else None


def _is_connector_object(value: Any) -> bool:
    """Duck-typed connector/secret-object identity check (no heavy import).

    A packaged connector object exposes a ``connector_type`` property and a
    callable ``query`` — the _TracedConnector proxy forwards both via
    ``__getattr__``, so this also rejects the wrapped (secret-bearing) form.
    Plain dicts / ConnectorResult records are never matched.
    """
    return hasattr(value, "connector_type") and callable(getattr(value, "query", None))


def _iter_nested_values(value: Any) -> Iterable[Any] | None:
    """Yield the values a payload guard must descend into, or ``None`` for a leaf.

    Dispatch order matters and mirrors the historical guard: mapping values,
    then list/tuple elements, then dataclass fields, then a non-primitive
    object's ``__dict__`` values. Returning ``None`` marks a leaf (primitive,
    or an object with no attribute bag) that needs no recursion.
    """
    if isinstance(value, dict):
        return value.values()
    if isinstance(value, (list, tuple)):
        return value
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return (getattr(value, field.name) for field in dataclasses.fields(value))
    if hasattr(value, "__dict__") and not isinstance(value, (str, bytes, bytearray, int, float, bool)):
        # Backstop for non-dataclass objects (agents may wrap payloads in plain
        # classes). Skip primitives and types; descend into attributes.
        attributes: dict[str, Any] = vars(value)
        return attributes.values()
    return None


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
    nested_values = _iter_nested_values(value)
    if nested_values is None:
        return
    for nested in nested_values:
        assert_no_secret_objects(nested, node_id=node_id, _seen=_seen)
