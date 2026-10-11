"""Connector base types, ABCs, and ACL enforcement."""

import logging
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from modulo.connectors.security import CredentialRedactor

logger = logging.getLogger(__name__)

# FAR-458 connector-write idempotency gate: the per-op ``on_unknown`` policy
# modes and their default — the SINGLE source of truth for the mode set. Both
# consumers import from here so the set can never drift between the REST
# connector's config validation and the pipeline engine's gate read:
#   - ``modulo.connectors.rest`` validates the connector's ``on_unknown`` config
#     value against :data:`ON_UNKNOWN_MODES` and defaults to
#     :data:`DEFAULT_ON_UNKNOWN` when absent.
#   - ``modulo.core.pipeline_engine.node_runner`` coerces a connector's
#     ``on_unknown_for`` read to :data:`DEFAULT_ON_UNKNOWN` for any value
#     outside :data:`ON_UNKNOWN_MODES`.
# This module is a stdlib-only leaf, so importing it from either side cannot
# create a cycle.
ON_UNKNOWN_MODES = ("fail_open", "fail_closed", "off")
DEFAULT_ON_UNKNOWN = "fail_open"


def unrestricted_allowed_operations(value: object) -> bool:
    """Does *value* mean the connector's operation scope is UNRESTRICTED? (FAR-1564)

    Exactly two values mean unrestricted: ``None`` (the column's other unset
    representation) and ``[]`` (the empty list every connector created through
    REST/MCP/UI stores) — "nothing was configured to restrict". Everything
    else is RESTRICTED: a NON-EMPTY list (the allowlist) and, critically, ANY
    malformed non-list value (``dict``/``str``/``int``/...).

    This is the SINGLE shared predicate for the "unrestricted vs restricted"
    decision, used by every read path (``ConnectorACL``, the graph validator's
    connector-binding check, and the guardrail conformance reader), so the same
    stored value can never certify a full capability set on one path while
    being read restrictively on another. Fail closed: malformed input RESTRICTS
    — it never grants.
    """
    return value is None or value == []


# ---------------------------------------------------------------------------
# Canonical capability vocabulary (FAR-1582 / FAR-1594)
# ---------------------------------------------------------------------------
#
# ONE vocabulary, ONE helper: ``Capability``'s own values (``read``, ``write``,
# ``create_pr``, ...) are the vocabulary every consumer matches on, and the
# helpers below are the single place a spelling is reduced to it. They live
# HERE — a stdlib-only leaf — so both ``ConnectorACL`` (enforcement) and
# ``core.guardrails.conformance`` (certification) import the same code and can
# never again give opposite answers for the same stored value (FAR-1594 defect
# (a): conformance certified a stored ``["github.read"]`` as ``read`` while the
# ACL denied the read).
#
# NOTE the two SIDES deliberately differ (FAR-1594 defect (b)):
#   * a stored ALLOWLIST entry is a *declaration of grant* — its type qualifier
#     is redundant (the surface's own ``connector_type_id`` names the type), so
#     it is reduced to the bare capability by :func:`canonical_capability`;
#     but the qualifier is only TRUSTED when it matches that surface type —
#     :func:`canonical_capability_set` takes the surface's
#     ``connector_type_id`` and REJECTS a mis-typed qualifier (FAR-1616);
#   * a conformance CLAIM is a *binding request* — ``github.read`` asks for a
#     github surface specifically, so it keeps its qualifier (see
#     ``qualified_capability`` and ``conformance._canonical_claim``).


def qualified_capability(value: str) -> tuple[str, str] | None:
    """Split a TYPE-QUALIFIED capability spelling into ``(type_id, capability)``.

    Recognises ``<connector-type>[.:]<capability>`` — e.g. ``github.read``,
    ``github:write``, ``ci-runner.list_runs`` — where the prefix parses as a
    :class:`ConnectorType` and the suffix as a :class:`Capability`. Returns
    ``None`` for a bare capability (``read``), for a non-connector qualified
    string (``sandbox.egress``, ``egress:github.com`` — their surfaces own
    their vocabulary), and for anything that is not a capability in any
    accepted spelling.
    """
    for separator in (".", ":"):
        prefix, found, suffix = value.partition(separator)
        if not found:
            continue
        try:
            ConnectorType(prefix)
        except ValueError:
            continue
        try:
            return prefix, str(Capability(suffix))
        except ValueError:
            continue
    return None


def canonical_capability(value: str) -> str | None:
    """Reduce a capability spelling to the canonical bare :class:`Capability` form.

    Accepted spellings: the bare ``Capability`` value itself (``read``) or a
    legacy type-qualified spelling (``github.read``, ``github:write``), which
    reduces to its bare value. Used for anything that GRANTS or DECLARES a
    capability — a stored ``allowed_operations`` entry and a conformance
    manifest/claim REPORT — because the qualifier adds no information there:
    the surface's own ``connector_type_id`` names the type.

    Returns ``None`` when *value* is not a capability in any accepted spelling
    — ``sandbox.egress``, ``docker``, ``egress:github.com`` belong to the
    sandbox/environment/agent surfaces, whose own vocabulary this must never
    rewrite.

    Consumers (both import THIS function; neither keeps a private copy):
      * :class:`ConnectorACL` — canonicalises the REQUESTED operation when it
        checks it; the stored allowlist goes through
        :func:`canonical_capability_set`, which applies the same reduction per
        entry PLUS the FAR-1616 type-qualifier check, so ``check("read")``
        grants a stored same-type ``["github.read"]`` exactly as the guardrail
        conformance reader certifies it (FAR-1594 defect (a));
      * ``core.guardrails.conformance`` — canonicalises the stored allowlist
        it reads into the live manifest, and the capability names it REPORTS
        back (``missing`` / ``unreadable``).
    """
    try:
        return str(Capability(value))
    except ValueError:
        pass
    qualified = qualified_capability(value)
    if qualified is not None:
        return qualified[1]
    return None


def canonical_capability_set(values: object, *, connector_type_id: str | None = None) -> set[str]:
    """Canonicalise a stored ``allowed_operations`` list to bare capabilities.

    The SINGLE reader of a stored allowlist's ENTRIES, shared by
    :class:`ConnectorACL` and ``core.guardrails.conformance`` so the two can
    never certify and deny different sets for the same stored value (FAR-1594
    defect (a)). Entries that are not capabilities in any accepted spelling are
    DROPPED (with a log): they grant nothing, and carrying them would let an
    arbitrary stored string satisfy a claim of the same spelling.

    Type-qualified entries (FAR-1616): ``github.write`` is a grant whose
    qualifier names the SURFACE'S OWN connector type. It grants bare
    ``write`` only when *connector_type_id* is supplied and matches that
    qualifier — the same-type legacy spelling keeps granting exactly what
    FAR-1594/FAR-1582 intend. It is REJECTED (fail closed, logged) when the
    qualifier names a DIFFERENT type — a stored ``["github.write"]`` on a
    FILESYSTEM connector must not grant ``write`` — and when no surface type
    is supplied at all (an unverifiable qualifier can never grant). Before
    FAR-1616 the qualifier was dropped unconditionally, so the mis-typed
    entry granted on the wrong surface.

    ``[]``/``None`` never reach here — :func:`unrestricted_allowed_operations`
    routes them to the connector TYPE's capability set first; a non-list
    (malformed) value yields the EMPTY set, matching the fail-closed
    :class:`ConnectorACL` treatment of a malformed allowlist (FAR-1564).
    """
    canonical: set[str] = set()
    if not isinstance(values, list):
        return canonical
    surface_type = connector_type_id if isinstance(connector_type_id, str) and connector_type_id else None
    for raw in values:
        if not isinstance(raw, str):
            continue
        qualified = qualified_capability(raw)
        if qualified is not None:
            qualifier_type, qualified_capability_name = qualified
            if surface_type is None or qualifier_type != surface_type:
                # FAR-1616: mis-typed OR unverifiable qualifier -> reject the
                # entry (fail closed). It grants nothing on this surface.
                logger.warning(
                    "connectors.capability.operation_type_mismatch",
                    extra={
                        "operation": str(raw)[:100],
                        "surface_connector_type_id": surface_type,
                        "qualifier_connector_type_id": qualifier_type,
                    },
                )
                continue
            canonical.add(qualified_capability_name)
            continue
        capability = canonical_capability(raw)
        if capability is None:
            logger.warning(
                "connectors.capability.operation_not_a_capability",
                extra={"operation": str(raw)[:100]},
            )
            continue
        canonical.add(capability)
    return canonical


class Capability(StrEnum):
    """Operations a connector can perform."""

    READ = "read"
    WRITE = "write"
    GIT_PUSH = "git_push"
    CREATE_PR = "create_pr"
    CODE_REVIEW = "code_review"
    TRIGGER_RUN = "trigger_run"
    GET_RUN_STATUS = "get_run_status"
    GET_RUN_LOGS = "get_run_logs"
    LIST_RUNS = "list_runs"
    TICKET_READ = "ticket_read"
    TICKET_WRITE = "ticket_write"
    TICKET_SEARCH = "ticket_search"
    MONITORING = "monitoring"
    OBSERVABILITY = "observability"
    VULNERABILITY_SCANNING = "vulnerability_scanning"
    INCIDENT_MANAGEMENT = "incident_management"
    COLLABORATION = "collaboration"
    MESSAGING = "messaging"
    NOTIFICATION = "notification"
    PACKAGE_MANAGEMENT = "package_management"
    SECRETS_MANAGEMENT = "secrets_management"
    AUTOMATION = "automation"


class ConnectorType(StrEnum):
    FILESYSTEM = "filesystem"
    GITHUB = "github"
    BITBUCKET = "bitbucket"
    CI_RUNNER = "ci-runner"
    GITEA = "gitea"
    GITLAB = "gitlab"
    AZURE_REPOS = "azure_repos"
    JIRA = "jira"
    TRELLO = "trello"
    ASANA = "asana"
    TICKET_TRACKER = "ticket-tracker"
    LINEAR = "linear"
    SLACK = "slack"
    SHELL = "shell"
    SHAREPOINT = "sharepoint"
    MONDAY = "monday"
    CUSTOM = "custom"
    SHORTCUT = "shortcut"
    YOUTRACK = "youtrack"
    NOTION = "notion"
    NPM = "npm"
    CONFLUENCE = "confluence"
    DROPBOX_PAPER = "dropbox_paper"
    CIRCLECI = "circleci"
    BUILDKITE = "buildkite"
    JENKINS = "jenkins"
    TEAMCITY = "teamcity"
    AZURE_KEY_VAULT = "azure_key_vault"
    AZURE_PIPELINES = "azure_pipelines"
    DATADOG = "datadog"
    SENTRY = "sentry"
    PAGERDUTY = "pagerduty"
    GRAFANA = "grafana"
    MICROSOFT_TEAMS = "microsoft_teams"
    DISCORD = "discord"
    OPSGENIE = "opsgenie"
    SONARQUBE = "sonarqube"
    CODECLIMATE = "codeclimate"
    SNYK = "snyk"
    ONEPASSWORD = "onepassword"
    PYPI = "pypi"
    N8N = "n8n"
    REST = "rest"

    @property
    def capabilities(self) -> frozenset[Capability]:
        """Default capabilities per connector type."""
        match self:
            case ConnectorType.FILESYSTEM:
                return frozenset({Capability.READ, Capability.WRITE})
            case ConnectorType.GITHUB:
                return frozenset(
                    {
                        Capability.READ,
                        Capability.WRITE,
                        Capability.GIT_PUSH,
                        Capability.CREATE_PR,
                        Capability.CODE_REVIEW,
                        Capability.TICKET_READ,
                        Capability.TICKET_WRITE,
                    },
                )
            case ConnectorType.BITBUCKET:
                return frozenset({Capability.READ, Capability.WRITE, Capability.GIT_PUSH, Capability.CREATE_PR})
            case ConnectorType.CI_RUNNER:
                return frozenset(
                    {
                        Capability.TRIGGER_RUN,
                        Capability.GET_RUN_STATUS,
                        Capability.GET_RUN_LOGS,
                        Capability.LIST_RUNS,
                    },
                )
            case ConnectorType.GITEA:
                return frozenset({Capability.READ, Capability.WRITE, Capability.GIT_PUSH, Capability.CREATE_PR})
            case ConnectorType.GITLAB:
                return frozenset(
                    {
                        Capability.READ,
                        Capability.WRITE,
                        Capability.GIT_PUSH,
                        Capability.CREATE_PR,
                        Capability.TICKET_READ,
                        Capability.TICKET_WRITE,
                        Capability.TICKET_SEARCH,
                        Capability.TRIGGER_RUN,
                    },
                )
            case ConnectorType.AZURE_REPOS:
                return frozenset({Capability.READ, Capability.WRITE, Capability.GIT_PUSH, Capability.CREATE_PR})
            case ConnectorType.JIRA:
                return frozenset({Capability.TICKET_READ, Capability.TICKET_WRITE, Capability.TICKET_SEARCH})
            case ConnectorType.TRELLO:
                return frozenset(
                    {
                        Capability.READ,
                        Capability.WRITE,
                        Capability.TICKET_READ,
                        Capability.TICKET_WRITE,
                        Capability.TICKET_SEARCH,
                    },
                )
            case ConnectorType.ASANA:
                return frozenset(
                    {
                        Capability.READ,
                        Capability.WRITE,
                        Capability.TICKET_READ,
                        Capability.TICKET_WRITE,
                        Capability.TICKET_SEARCH,
                    },
                )
            case ConnectorType.SLACK:
                return frozenset({Capability.MESSAGING, Capability.READ, Capability.WRITE})
            case ConnectorType.SHELL:
                return frozenset({Capability.READ, Capability.WRITE})
            case ConnectorType.MONDAY:
                return frozenset({Capability.READ, Capability.WRITE})
            case ConnectorType.SHORTCUT:
                return frozenset({Capability.READ, Capability.WRITE})
            case ConnectorType.YOUTRACK | ConnectorType.NOTION | ConnectorType.CONFLUENCE | ConnectorType.SHAREPOINT:
                return frozenset({Capability.READ, Capability.WRITE})
            case ConnectorType.DROPBOX_PAPER:
                return frozenset({Capability.READ, Capability.WRITE})
            case ConnectorType.CIRCLECI:
                return frozenset(
                    {
                        Capability.TRIGGER_RUN,
                        Capability.GET_RUN_STATUS,
                        Capability.GET_RUN_LOGS,
                        Capability.LIST_RUNS,
                    },
                )
            case ConnectorType.BUILDKITE:
                return frozenset(
                    {
                        Capability.TRIGGER_RUN,
                        Capability.GET_RUN_STATUS,
                        Capability.GET_RUN_LOGS,
                        Capability.LIST_RUNS,
                    },
                )
            case ConnectorType.JENKINS:
                return frozenset(
                    {
                        Capability.TRIGGER_RUN,
                        Capability.GET_RUN_STATUS,
                        Capability.GET_RUN_LOGS,
                        Capability.LIST_RUNS,
                    },
                )
            case ConnectorType.TEAMCITY:
                return frozenset(
                    {
                        Capability.TRIGGER_RUN,
                        Capability.GET_RUN_STATUS,
                        Capability.GET_RUN_LOGS,
                        Capability.LIST_RUNS,
                    },
                )
            case ConnectorType.AZURE_KEY_VAULT:
                return frozenset(
                    {
                        Capability.SECRETS_MANAGEMENT,
                        Capability.READ,
                        Capability.WRITE,
                    },
                )
            case ConnectorType.AZURE_PIPELINES:
                return frozenset(
                    {
                        Capability.TRIGGER_RUN,
                        Capability.GET_RUN_STATUS,
                        Capability.GET_RUN_LOGS,
                        Capability.LIST_RUNS,
                    },
                )
            case ConnectorType.DATADOG:
                return frozenset({Capability.MONITORING, Capability.OBSERVABILITY, Capability.READ, Capability.WRITE})
            case ConnectorType.SENTRY:
                return frozenset(
                    {
                        Capability.MONITORING,
                        Capability.INCIDENT_MANAGEMENT,
                        Capability.READ,
                        Capability.WRITE,
                    },
                )
            case ConnectorType.PAGERDUTY:
                return frozenset(
                    {
                        Capability.INCIDENT_MANAGEMENT,
                        Capability.MONITORING,
                        Capability.READ,
                        Capability.WRITE,
                    },
                )
            case ConnectorType.GRAFANA:
                return frozenset(
                    {
                        Capability.MONITORING,
                        Capability.OBSERVABILITY,
                        Capability.READ,
                        Capability.WRITE,
                    },
                )
            case ConnectorType.MICROSOFT_TEAMS:
                return frozenset(
                    {
                        Capability.COLLABORATION,
                        Capability.MESSAGING,
                        Capability.NOTIFICATION,
                        Capability.READ,
                        Capability.WRITE,
                    },
                )
            case ConnectorType.DISCORD:
                return frozenset(
                    {
                        Capability.COLLABORATION,
                        Capability.MESSAGING,
                        Capability.NOTIFICATION,
                    },
                )
            case ConnectorType.OPSGENIE:
                return frozenset(
                    {
                        Capability.INCIDENT_MANAGEMENT,
                        Capability.MONITORING,
                        Capability.NOTIFICATION,
                    },
                )
            case ConnectorType.SONARQUBE:
                return frozenset(
                    {
                        Capability.READ,
                        Capability.WRITE,
                        Capability.MONITORING,
                        Capability.OBSERVABILITY,
                    },
                )
            case ConnectorType.CODECLIMATE:
                return frozenset({Capability.MONITORING, Capability.OBSERVABILITY})
            case ConnectorType.SNYK:
                return frozenset(
                    {
                        Capability.READ,
                        Capability.VULNERABILITY_SCANNING,
                        Capability.MONITORING,
                    },
                )
            case ConnectorType.ONEPASSWORD:
                return frozenset(
                    {
                        Capability.SECRETS_MANAGEMENT,
                        Capability.READ,
                        Capability.WRITE,
                    },
                )
            case ConnectorType.NPM:
                return frozenset({Capability.PACKAGE_MANAGEMENT, Capability.READ})
            case ConnectorType.PYPI:
                return frozenset({Capability.PACKAGE_MANAGEMENT, Capability.READ})
            case ConnectorType.N8N:
                return frozenset({Capability.AUTOMATION, Capability.READ, Capability.WRITE})
            case ConnectorType.REST:
                # A verb-agnostic REST connector: query() is the READ surface
                # (ACL "read") and write() is the WRITE surface (ACL "write").
                # PUT/DELETE/PATCH are neither cleanly read nor write, but they
                # MUTATE the remote system, so they live on the write surface.
                return frozenset({Capability.READ, Capability.WRITE})
            case ConnectorType.TICKET_TRACKER:
                return frozenset({Capability.TICKET_READ, Capability.TICKET_WRITE, Capability.TICKET_SEARCH})
            case ConnectorType.LINEAR:
                return frozenset({Capability.TICKET_READ, Capability.TICKET_WRITE})
            case _:
                return frozenset()


# ---------------------------------------------------------------------------
# FAR-1141: dispatch routing — the ONE predicate every consumer shares
# ---------------------------------------------------------------------------
#
# A graph node executes its connector binding through ONE verb, and that verb
# is decided by ``connector_binding.operation`` — NOT by ``node_type``. The
# engine routes it that way (``node_runner.make_connector_fn``), so every other
# reader of "does this node dispatch?" must ask the SAME question of the SAME
# function, or the surfaces drift (the FAR-1141 defect: the run's
# ``execution_origin`` was keyed on ``node_type`` while the engine keyed on the
# binding, so a connector node firing a real external job was stamped NULL and
# a dispatch-typed node running a query was stamped ``dispatched``).
#
# One precondition gates the whole question: the engine must ROUTE the binding
# to a connector at all (``node_routes_binding_to_connector``). Two shapes carry
# a binding the engine never routes — an ``agent`` node with an ``agent_id``
# (LLM factory) and a ``sandbox_agent`` (sandbox factory) — so their binding is
# dead configuration: asking it "which verb?" would read a dispatch that fires
# nothing (FAR-1141 criterion 4's over-claim: an executed run stamped
# ``dispatched``).
#
# They live HERE — a stdlib-only leaf the API, the engine, the validator AND
# the DB layer can all import without a cycle — so ``node_runner``,
# ``executor``, ``runtime_retry``, ``graph_validator``, ``api.routes.pipelines``,
# ``api.mcp_server`` and ``db.crud.run`` cannot grow separate copies of the
# rule. (``modulo.db`` is forbidden from importing ``modulo.core`` by the
# import-linter contract; this module is the seam that lets the run classifier
# share the engine's predicate without an exemption.)

#: The CI-runner capability contract a ``dispatch`` binding requires: the four
#: ``CIRunnerBase`` methods (``trigger_run`` / ``get_run_status`` /
#: ``get_run_logs`` / ``list_runs``). A connector type missing any of them
#: cannot honour a dispatch binding and fails at run time with an
#: ``AttributeError``-shaped error — so save time rejects it instead.
CI_RUNNER_CAPABILITIES: frozenset[Capability] = frozenset(
    {
        Capability.TRIGGER_RUN,
        Capability.GET_RUN_STATUS,
        Capability.GET_RUN_LOGS,
        Capability.LIST_RUNS,
    }
)

#: Hub-native connector-type ids that build a CI runner but are NOT members of
#: the ``ConnectorType`` enum (``connector_hub._build_connector`` matches them
#: by literal), so ``ConnectorType(id)`` would raise. Keep in step with the
#: hub's ``case`` arms for CI runners: ``github_actions_ci`` and ``gitlab_ci``.
#:
#: The ids must be EXACTLY the ids the hub can build. ``ci_runner`` used to sit
#: in here too, but it is the library's *family label* (the ``connector_type``
#: of ``GITHUB_ACTIONS_INTEGRATION`` / the ``connector_binding.type`` of the CI
#: workflow templates), never an instance's ``connector_type_id``: the hub has
#: no ``case "ci_runner"`` arm, so ``_build_connector("ci_runner", ...)`` falls
#: through to the plugin registry and raises ``Unknown connector type``. A
#: dispatch binding to a type the hub cannot build would fail at run time, so
#: the set stays honest and ``connector_type_supports_dispatch`` fails CLOSED
#: on it (the enum member is spelled ``ci-runner`` and reaches the capability
#: table through ``ConnectorType`` below, not through this set).
_HUB_CI_RUNNER_TYPE_IDS: frozenset[str] = frozenset({"github_actions_ci", "gitlab_ci"})


def connector_type_supports_dispatch(connector_type_id: Any) -> bool:
    """True when *connector_type_id* implements the CI-runner contract.

    Fail-closed: an unknown / unparseable type id (including a plugin
    connector type we cannot introspect) reports ``False`` — a dispatch binding
    on a type we cannot prove implements the four operations must be rejected
    at save time, never discovered as an ``AttributeError`` at run time.
    """
    type_id = str(connector_type_id or "").strip()
    if not type_id:
        return False
    if type_id in _HUB_CI_RUNNER_TYPE_IDS:
        return True
    try:
        capabilities = ConnectorType(type_id).capabilities
    except ValueError:
        return False
    return capabilities >= CI_RUNNER_CAPABILITIES


def node_routes_binding_to_connector(node: Any) -> bool:
    """True when the engine routes *node*'s ``connector_binding`` to a connector.

    The ROUTING half of :func:`connector_binding_operation`, mirroring the
    branch order of ``graph_cache._make_node_fn`` exactly:

    * a node with no binding (or a non-dict / empty one) never reaches a
      connector at all;
    * ``sandbox_agent`` builds the sandbox node function BEFORE the binding is
      considered, so its binding is inert;
    * an ``agent`` node carrying an ``agent_id`` builds the LLM node function —
      the binding branch is explicitly skipped for that one shape;
    * every other node type with a binding runs ``make_connector_fn``.

    A node this reports ``False`` for therefore has NO connector verb: reading
    its binding as one is how a graph that fires nothing got stamped
    ``dispatched`` (FAR-1141 criterion 4: an ``agent`` node with a dispatch
    binding never dispatches, yet the run claimed external execution).

    An unrecognised / missing ``node_type`` is read as the default
    (``agent``); such a node fails loud at graph build anyway, so no
    malformed shape can be classified as routed here.
    """
    if not isinstance(node, dict):
        return False
    binding = node.get("connector_binding")
    if not isinstance(binding, dict) or not binding:
        return False
    node_type = str(node.get("node_type") or "agent")
    if node_type == "sandbox_agent":
        return False
    # agent + agent_id builds the LLM node function: its binding branch is the
    # one shape the engine explicitly skips.
    return not (node_type == "agent" and bool(node.get("agent_id")))


def connector_binding_operation(node: Any) -> str:
    """The connector verb the ENGINE will route *node* to (``query`` default).

    Mirrors ``node_runner.make_connector_fn`` EXACTLY: for a node whose
    binding the engine actually routes (:func:`node_routes_binding_to_connector`)
    an explicit, non-empty ``connector_binding.operation`` wins; a
    ``node_type="dispatch"`` node falls back to the dispatch verb; every other
    node type queries. A node the engine NEVER routes to a connector — an
    ``agent`` node with an ``agent_id``, or a ``sandbox_agent`` — has no
    connector verb, so it reads ``query`` even when its binding declares one
    (the binding is dead configuration on that shape, never a dispatch).
    Returns ``"query"`` for a non-dict / binding-less / operation-less node so
    a malformed input is never read as a dispatch.
    """
    if not isinstance(node, dict):
        return "query"
    binding = node.get("connector_binding")
    if isinstance(binding, dict) and binding and node_routes_binding_to_connector(node):
        raw_operation = binding.get("operation")
        if isinstance(raw_operation, str) and raw_operation:
            return raw_operation
    if str(node.get("node_type") or "") == "dispatch":
        return "dispatch"
    return "query"


def node_fires_dispatch_job(node: Any) -> bool:
    """True when executing *node* FIRES a NEW job on an external substrate.

    The non-idempotency predicate (FAR-1141 / FAR-295): a binding that routes
    the ``dispatch`` verb with ``dispatch_action="trigger_run"`` creates a job
    the customer's substrate will run, so re-executing the graph would fire a
    SECOND job. Keyed on the BINDING (operation + action), never on
    ``node_type``, so it covers a ``connector`` / ``router`` / ``hitl`` node
    carrying a dispatch binding exactly as it covers a ``dispatch`` node —
    and, through :func:`connector_binding_operation`, it is ``False`` for a
    node the engine never routes to a connector (an ``agent`` node with an
    ``agent_id``, a ``sandbox_agent``): those fire nothing, so they are safe
    to re-run.

    False for a node with no binding (the engine fails loud on that shape at
    graph build, so it can never fire anything) and for every read-only
    dispatch action (``get_run_status`` / ``get_run_logs`` / ``list_runs``),
    which are safe to re-run.
    """
    if not isinstance(node, dict):
        return False
    binding = node.get("connector_binding")
    if not isinstance(binding, dict) or not binding.get("instance_id"):
        return False
    if connector_binding_operation(node) != "dispatch":
        return False
    return str(binding.get("dispatch_action") or "trigger_run") == "trigger_run"


class ConnectorPermissionError(ValueError):
    """Raised when a connector operation violates its ACL."""


class ConnectorACL:
    """Access-control list for connector operations.

    Enforces the optional white-list of allowed operations. The connector's
    ``visibility`` is carried as validated state but is NOT enforced here:
    team-scope binding rules are enforced at the write gates that create a
    cross-team binding (``core.team_visibility``). The FAR-516 run-gate that
    used to reject a team-scoped request against an ``org``-visibility
    connector was removed by FAR-1618 — teams are a VISIBILITY GROUPING, not
    a credential trust boundary, so an org-visibility connector is shared
    across the organisation and binds to ANY pipeline, including team-owned
    ones. (The team-PRIVATE direction — ``visibility: team`` usable only by
    its owner team's pipelines — is unchanged and stays enforced at those
    write gates.)

    Operation-scope semantics (FAR-1564): ``None`` and an empty list BOTH mean
    UNRESTRICTED — "nothing was configured to restrict". Connectors created
    through REST/MCP/UI store ``allowed_operations=[]``, which is the unset
    value, so treating it as deny-all locked every default-created connector
    out of every operation. A NON-EMPTY list is an allowlist: any operation it
    does not list is denied. There is no explicit deny-all state — removing the
    connector is the lock.

    A MALFORMED value (any non-list other than ``None`` — a ``dict``, ``str``,
    ``int`` read back out of the JSON column) is FAIL-CLOSED: it restricts to
    the empty allowlist, so every operation is denied. Deciding this via
    :func:`unrestricted_allowed_operations` keeps this class in step with the
    graph validator and the guardrail conformance reader, which read the same
    column.

    Canonical vocabulary (FAR-1594): a RESTRICTED allowlist is canonicalised
    through :func:`canonical_capability_set` and the requested operation
    through :func:`canonical_capability`, so a stored legacy spelling
    (``["github.read"]``) GRANTS ``read`` and :meth:`check` answers exactly
    what the guardrail conformance reader certifies for the same stored value.
    Both consume the ONE shared helper — neither keeps a private copy.

    Surface type (FAR-1616): *connector_type_id* is the SURFACE'S OWN
    connector type, supplied by every construction site that knows it (the
    connector hub passes the instance's ``connector_type_id``). A
    type-qualified allowlist entry (``github.write``) grants its bare
    capability ONLY when this type matches the entry's qualifier; a mis-typed
    qualifier — or one that cannot be verified because no type was supplied —
    is REJECTED and grants nothing (fail closed). Without this, the FAR-1594
    canonicalisation dropped the qualifier unconditionally, so a stored
    ``["github.write"]`` on a FILESYSTEM connector granted bare ``write``,
    where it must deny. ``None``/``[]`` remain UNRESTRICTED and a malformed
    value remains fail-closed (FAR-1564) regardless of the type.
    """

    _VALID_VISIBILITY = frozenset({"org", "team"})

    def __init__(
        self,
        visibility: str,
        allowed_operations: object = None,
        *,
        connector_type_id: str | None = None,
    ) -> None:
        if visibility not in self._VALID_VISIBILITY:
            raise ValueError(f"visibility must be 'org' or 'team', got {visibility!r}")
        self.visibility = visibility
        self.connector_type_id = connector_type_id
        # Decided ONCE here from the shared predicate: only ``None``/``[]``
        # are unrestricted. ``check`` reads this flag rather than re-testing
        # truthiness, because an empty frozenset is BOTH the unrestricted-``[]``
        # representation AND the fail-closed representation of a malformed
        # value — truthiness alone cannot tell them apart.
        self._unrestricted = unrestricted_allowed_operations(allowed_operations)
        if self._unrestricted:
            # ``None`` -> ``None``; ``[]`` -> empty frozenset, preserving the
            # attribute's historic shape (an explicit empty allowlist is not
            # ``None``) while meaning exactly the same thing: unrestricted.
            self.allowed_operations: frozenset[str] | None = None if allowed_operations is None else frozenset()
        elif isinstance(allowed_operations, list):
            # FAR-1594 (a): canonicalise to the ONE vocabulary — a stored
            # same-type ``["github.read"]`` grants ``read`` here exactly as the
            # guardrail conformance reader certifies ``read`` for the same
            # value. Shares the conformance reader's helper so the two can
            # never diverge. FAR-1616: the surface's own type is passed so a
            # MIS-TYPED qualified entry (``github.write`` on a filesystem
            # connector) is rejected instead of granting bare ``write``.
            self.allowed_operations = frozenset(
                canonical_capability_set(allowed_operations, connector_type_id=connector_type_id),
            )
        else:
            logger.warning(
                "connectors.acl.malformed_allowed_operations",
                extra={"allowed_operations_type": type(allowed_operations).__name__},
            )
            # Restricted to the empty allowlist: every operation is denied.
            self.allowed_operations = frozenset()

    def check(self, operation: str) -> None:
        """Raise ConnectorPermissionError if the operation is not permitted.

        ``None`` and an empty list are both unrestricted (FAR-1564); a
        NON-EMPTY list restricts to the operations it lists, and a MALFORMED
        value restricts to nothing (fail closed — see the class docstring).

        The requested operation is canonicalised (FAR-1594) with the SAME
        helper that canonicalised the stored allowlist, so ``check("read")``
        grants a stored ``["github.read"]`` — the answer the guardrail
        conformance reader gives for that value. A request that is not a
        capability in any accepted spelling is matched raw (as before).

        There is deliberately NO visibility/request-scope parameter: an
        org-visibility connector is shared across the organisation (FAR-1618).
        """
        if not self._unrestricted:
            allowed = self.allowed_operations or frozenset()
            canonical_operation = canonical_capability(operation) or operation
            if operation not in allowed and canonical_operation not in allowed:
                raise ConnectorPermissionError(
                    f"Operation {operation!r} is not in allowed_operations: {sorted(allowed)}",
                )


@dataclass
class ConnectorQuery:
    resource: str
    filters: dict[str, Any] = field(default_factory=dict)
    limit: int = 100
    cursor: str | None = None


@dataclass
class ConnectorPayload:
    resource: str
    data: dict[str, Any]


@dataclass
class ConnectorResult:
    records: list[dict[str, Any]] = field(default_factory=list)
    next_cursor: str | None = None
    total: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class CompensationOutcome(StrEnum):
    """Outcome of a connector compensating callback (FAR-213).

    ``compensated``     — the inverse action was performed (PR closed, ticket
                          unassigned).
    ``not_supported``   — the connector has no inverse for this operation
                          (the default — connectors OPT IN).
    ``failed``          — an inverse exists but the attempt failed.
    """

    COMPENSATED = "compensated"
    NOT_SUPPORTED = "not_supported"
    FAILED = "failed"


@dataclass(frozen=True)
class CompensationOperation:
    """A connector write operation a run node performed, with the data to invert it.

    ``resource`` is the connector write resource (e.g. ``"pr"`` for GitHub),
    ``data`` the write payload (the performed action's arguments), and
    ``output`` the entity the connector returned (e.g. the created PR dict).
    Summary-only — never raw payloads beyond what the write itself used.
    """

    resource: str
    data: dict[str, Any] = field(default_factory=dict)
    output: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CompensationContext:
    """Run-scoped context for a compensating callback (FAR-213).

    IDs only — never payload content.
    """

    org_id: str
    run_id: str
    node_id: str
    connector_instance_id: str


@dataclass(frozen=True)
class CompensationResult:
    """Outcome of a compensating callback (FAR-213)."""

    outcome: CompensationOutcome
    detail: str = ""
    resource_id: str | None = None


@dataclass(frozen=True)
class HealthResult:
    ok: bool
    detail: str = ""


def health_check_failure(exc: Exception, redact: Callable[[str], str]) -> HealthResult:
    """Degrade a failed connector health check into a not-ok result.

    Centralises the truncation policy applied to the error detail so every
    connector reports a consistent, bounded failure message.

    ``redact`` — REQUIRED (FAR-1651). The detail string is built from the raw
    exception message, which is itself a live credential-echo surface (an
    upstream 4xx body, a transport error's request URL, a connector's own
    ``ValueError`` rendering of a response payload). Every caller passes its
    credential redaction (typically ``self._redacted_detail``) so the FULL
    message is scrubbed before truncation — truncating first can split a
    credential across the 200-char boundary and leave the surviving fragment
    unrecoverable. Redaction is mandatory rather than optional so a new caller
    cannot silently persist an unredacted exception: the signature is the
    enforcement, not a convention.
    """
    return HealthResult(ok=False, detail=redact(str(exc))[:200])


class CIRunStatus(StrEnum):
    PENDING = "pending"
    QUEUED = "queued"
    IN_PROGRESS = "in_progress"
    SUCCESS = "success"
    FAILURE = "failure"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    UNKNOWN = "unknown"


@dataclass
class CIRun:
    id: str
    pipeline_id: str
    status: CIRunStatus
    url: str = ""
    branch: str = ""
    commit_sha: str = ""
    created_at: str = ""
    updated_at: str = ""
    duration_seconds: int | None = None
    triggered_by: str = ""


@dataclass
class CIRunLog:
    run_id: str
    lines: list[str]
    next_cursor: str | None = None
    truncated: bool = False


class ConnectorBase(ABC):
    """Abstract base for all external tool connectors."""

    @property
    @abstractmethod
    def connector_type(self) -> ConnectorType:
        """Type identifier for this connector."""

    def _credential_values(self) -> Sequence[str]:
        """The connector's live credential strings as they appear in requests.

        Subclasses that hold credentials (tokens, API keys, app passwords)
        MUST override this to return the exact secret strings they send to the
        upstream API — the same values that upstream error bodies, redirect
        targets or proxies can echo back verbatim. Connectors
        holding no secrets keep the empty default.

        Returns a sequence, never a bare ``tuple[str]`` of one value: a
        connector may hold several distinct credentials (e.g. access key +
        app key), and every one of them must be redactable.
        """
        return ()

    def _redacted_detail(self, text: str) -> str:
        """Return *text* with this connector's credential values masked.

        Every ``health_check`` (and health-adjacent diagnostic) path that
        embeds an upstream response body, exception message or request URL
        into a ``HealthResult.detail`` MUST route the string through this
        method so a credential echoed by the upstream service never reaches
        the caller. Conformance is enforced by
        ``tests/unit/connectors/test_health_detail_credential_redaction.py``.
        """
        return CredentialRedactor(self._credential_values()).redact(text)

    @abstractmethod
    async def health_check(self) -> HealthResult:
        """Verify connectivity and credential validity."""

    @abstractmethod
    async def query(self, q: ConnectorQuery) -> ConnectorResult:
        """Read data from the external tool."""

    @abstractmethod
    async def write(self, payload: ConnectorPayload) -> dict[str, Any]:
        """Write data to the external tool. Returns the created/updated resource."""

    def on_unknown_for(self, resource: str) -> str:
        """Effective ``on_unknown`` mode for a connector write to *resource*
        (FAR-458).

        Governs the FAR-458 connector-write idempotency gate's AMBIGUOUS-case
        decision (couldn't-confirm-delivery). Three values, validated at
        config-parse time:

        - ``"fail_open"`` (default): on ambiguity the gate does NOT suppress —
          the write fires (possible duplicate, usually recoverable).
        - ``"fail_closed"``: on ambiguity the gate SUPPRESSES — the write does
          not fire (possible silent miss; the operator reconciles).
        - ``"off"``: the write is never deduped (gate bypassed entirely).

        A CONFIRMED-delivered write (``delivery_done is True`` + matching key)
        is suppressed in every mode except ``off`` — that is the point of dedup.
        The default implements the fail-open contract of every other gate
        failure mode here; connectors override to declare a per-op policy.
        """
        return DEFAULT_ON_UNKNOWN

    def write_reported_failure(self, result: Any) -> bool:
        """Did a NON-RAISING ``write()`` result report an upstream failure?

        FAR-531 (AC6): some connectors report a failed write in their RETURN
        value instead of raising (e.g. ``ShellConnector.write`` for the
        ``command`` resource returns ``{"exit_code": <non-zero>}``). For those,
        a non-raising ``write()`` is NOT proof of delivery, and the
        connector-write idempotency stamp (``_stamp_connector_write_delivered``)
        must not treat it as one. Connectors whose results have that shape
        override this hook; the default ``False`` means "a write that returned
        without raising delivered" (every connector that raises on failure).

        ``result`` is the raw value returned by ``write()``. Implementations
        must be pure and never raise for any result shape.
        """
        return False

    async def compensate(
        self,
        operation: CompensationOperation,
        *,
        context: CompensationContext,
        error: str,
    ) -> CompensationResult:
        """Best-effort inverse of a performed connector operation (FAR-213).

        Run-termination compensation for guardrail-blocked runs calls this for
        every executed node that performed a connector write. Contract: given
        the performed operation (resource, write payload, returned entity) and
        the termination reason (the guardrail block detail), attempt the inverse
        (close a PR, unassign a ticket, revert a status) and report an outcome.

        The default returns ``not_supported`` — connectors OPT IN by overriding
        and returning :class:`CompensationResult`. Compensation is best-effort
        and must never raise into the terminalization path; wrap external I/O
        and return ``failed`` with a summary detail instead of raising.
        """
        return CompensationResult(
            outcome=CompensationOutcome.NOT_SUPPORTED,
            detail="connector does not implement compensation",
        )
