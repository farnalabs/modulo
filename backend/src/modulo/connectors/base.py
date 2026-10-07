"""Connector base types, ABCs, and ACL enforcement."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

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
#: hub's ``case`` arms for CI runners.
_HUB_CI_RUNNER_TYPE_IDS: frozenset[str] = frozenset({"github_actions_ci", "gitlab_ci", "ci_runner"})


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

    Enforces *visibility* restrictions and an optional white-list of allowed operations.
    """

    _VALID_VISIBILITY = frozenset({"org", "team"})

    def __init__(self, visibility: str, allowed_operations: list[str] | None = None) -> None:
        if visibility not in self._VALID_VISIBILITY:
            raise ValueError(f"visibility must be 'org' or 'team', got {visibility!r}")
        self.visibility = visibility
        self.allowed_operations: frozenset[str] | None = (
            None if allowed_operations is None else frozenset(allowed_operations)
        )

    def check(self, operation: str, *, request_visibility: str | None = None) -> None:
        """Raise ConnectorPermissionError if the operation is not permitted."""
        if self.allowed_operations is not None:
            if not self.allowed_operations:
                raise ConnectorPermissionError(
                    "No operations allowed — the allowlist is empty. Operator must grant at least one operation.",
                )
            if operation not in self.allowed_operations:
                raise ConnectorPermissionError(
                    f"Operation {operation!r} is not in allowed_operations: {sorted(self.allowed_operations)}",
                )
        if request_visibility == "team" and self.visibility == "org":
            raise ConnectorPermissionError("Attempted team-scoped access on an org-only connector")


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


def health_check_failure(exc: Exception) -> HealthResult:
    """Degrade a failed connector health check into a not-ok result.

    Centralises the truncation policy applied to the error detail so every
    connector reports a consistent, bounded failure message.
    """
    return HealthResult(ok=False, detail=str(exc)[:200])


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
