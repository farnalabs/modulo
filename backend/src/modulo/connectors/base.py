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
    TRIGGER_RUN = "trigger_run"
    GET_RUN_STATUS = "get_run_status"
    GET_RUN_LOGS = "get_run_logs"
    LIST_RUNS = "list_runs"
    ISSUE_READ = "issue_read"
    ISSUE_WRITE = "issue_write"
    ISSUE_SEARCH = "issue_search"
    MONITORING = "monitoring"
    OBSERVABILITY = "observability"
    INCIDENT_MANAGEMENT = "incident_management"
    COLLABORATION = "collaboration"
    MESSAGING = "messaging"
    NOTIFICATION = "notification"


class ConnectorType(StrEnum):
    FILESYSTEM = "filesystem"
    GITHUB = "github"
    BITBUCKET = "bitbucket"
    CI_RUNNER = "ci-runner"
    GITEA = "gitea"
    GITLAB = "gitlab"
    AZURE_REPOS = "azure_repos"
    JIRA = "jira"
    LINEAR = "linear"
    TRELLO = "trello"
    ASANA = "asana"
    SLACK = "slack"
    SHELL = "shell"
    SHAREPOINT = "sharepoint"
    MONDAY = "monday"
    CUSTOM = "custom"
    SHORTCUT = "shortcut"
    YOUTRACK = "youtrack"
    NOTION = "notion"
    CONFLUENCE = "confluence"
    DROPBOX_PAPER = "dropbox_paper"
    CIRCLECI = "circleci"
    BUILDKITE = "buildkite"
    JENKINS = "jenkins"
    TEAMCITY = "teamcity"
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

    @property
    def capabilities(self) -> frozenset[Capability]:
        """Default capabilities per connector type."""
        match self:
            case ConnectorType.FILESYSTEM:
                return frozenset({Capability.READ, Capability.WRITE})
            case ConnectorType.GITHUB:
                return frozenset({Capability.READ, Capability.WRITE, Capability.GIT_PUSH, Capability.CREATE_PR})
            case ConnectorType.BITBUCKET:
                return frozenset({Capability.READ, Capability.WRITE, Capability.GIT_PUSH, Capability.CREATE_PR})
            case ConnectorType.CI_RUNNER:
                return frozenset(
                    {
                        Capability.TRIGGER_RUN,
                        Capability.GET_RUN_STATUS,
                        Capability.GET_RUN_LOGS,
                        Capability.LIST_RUNS,
                    }
                )
            case ConnectorType.GITEA:
                return frozenset({Capability.READ, Capability.WRITE, Capability.GIT_PUSH, Capability.CREATE_PR})
            case ConnectorType.GITLAB:
                return frozenset({Capability.READ, Capability.WRITE, Capability.GIT_PUSH, Capability.CREATE_PR})
            case ConnectorType.AZURE_REPOS:
                return frozenset({Capability.READ, Capability.WRITE, Capability.GIT_PUSH, Capability.CREATE_PR})
            case ConnectorType.JIRA:
                return frozenset({Capability.ISSUE_READ, Capability.ISSUE_WRITE, Capability.ISSUE_SEARCH})
            case ConnectorType.LINEAR:
                return frozenset({Capability.ISSUE_READ, Capability.ISSUE_WRITE, Capability.ISSUE_SEARCH})
            case ConnectorType.TRELLO:
                return frozenset({Capability.READ, Capability.WRITE})
            case ConnectorType.ASANA:
                return frozenset({Capability.READ, Capability.WRITE})
            case ConnectorType.SLACK:
                return frozenset({Capability.READ, Capability.WRITE})
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
                    }
                )
            case ConnectorType.BUILDKITE:
                return frozenset(
                    {
                        Capability.TRIGGER_RUN,
                        Capability.GET_RUN_STATUS,
                        Capability.GET_RUN_LOGS,
                        Capability.LIST_RUNS,
                    }
                )
            case ConnectorType.JENKINS:
                return frozenset(
                    {
                        Capability.TRIGGER_RUN,
                        Capability.GET_RUN_STATUS,
                        Capability.GET_RUN_LOGS,
                        Capability.LIST_RUNS,
                    }
                )
            case ConnectorType.TEAMCITY:
                return frozenset(
                    {
                        Capability.TRIGGER_RUN,
                        Capability.GET_RUN_STATUS,
                        Capability.GET_RUN_LOGS,
                        Capability.LIST_RUNS,
                    }
                )
            case ConnectorType.AZURE_PIPELINES:
                return frozenset(
                    {
                        Capability.TRIGGER_RUN,
                        Capability.GET_RUN_STATUS,
                        Capability.GET_RUN_LOGS,
                        Capability.LIST_RUNS,
                    }
                )
            case ConnectorType.DATADOG:
                return frozenset({Capability.MONITORING, Capability.OBSERVABILITY, Capability.READ, Capability.WRITE})
            case ConnectorType.SENTRY:
                return frozenset({
                    Capability.MONITORING, Capability.INCIDENT_MANAGEMENT,
                    Capability.READ, Capability.WRITE,
                })
            case ConnectorType.PAGERDUTY:
                return frozenset({
                    Capability.INCIDENT_MANAGEMENT, Capability.MONITORING,
                    Capability.READ, Capability.WRITE,
                })
            case ConnectorType.GRAFANA:
                return frozenset({
                    Capability.MONITORING, Capability.OBSERVABILITY,
                    Capability.READ, Capability.WRITE,
                })
            case ConnectorType.MICROSOFT_TEAMS:
                return frozenset({
                    Capability.COLLABORATION, Capability.MESSAGING, Capability.NOTIFICATION,
                    Capability.READ, Capability.WRITE,
                })
            case ConnectorType.DISCORD:
                return frozenset({
                    Capability.COLLABORATION, Capability.MESSAGING, Capability.NOTIFICATION,
                })
            case ConnectorType.OPSGENIE:
                return frozenset({
                    Capability.INCIDENT_MANAGEMENT, Capability.MONITORING, Capability.NOTIFICATION,
                })
            case ConnectorType.SONARQUBE:
                return frozenset({
                    Capability.READ, Capability.WRITE, Capability.MONITORING, Capability.OBSERVABILITY,
                })
            case ConnectorType.CODECLIMATE:
                return frozenset({Capability.MONITORING, Capability.OBSERVABILITY})
            case _:
                return frozenset()


class ConnectorPermissionError(ValueError):
    """Raised when a connector operation violates its ACL."""

    pass


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
                    "No operations allowed — the allowlist is empty. Operator must grant at least one operation."
                )
            if operation not in self.allowed_operations:
                raise ConnectorPermissionError(
                    f"Operation {operation!r} is not in allowed_operations: {sorted(self.allowed_operations)}"
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


@dataclass(frozen=True)
class HealthResult:
    ok: bool
    detail: str = ""


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
