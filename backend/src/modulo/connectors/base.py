"""Connector base types, ABCs, and ACL enforcement."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


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
    TRIVY = "trivy"
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
            case ConnectorType.TRIVY:
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
        return "fail_open"

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
