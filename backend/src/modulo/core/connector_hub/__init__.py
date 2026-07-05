"""ConnectorHub — run-scoped credential decryption and connector lifecycle.

Usage:
    hub = ConnectorHub(secrets_backend=secrets_backend)
    async with hub:
        await hub.initialise(connector_instances)
        connector = hub.get(connector_id)
        result = await connector.query(...)

All connector operations (query, write, health_check) are automatically wrapped
in OpenTelemetry spans with connector_type, operation_name, and org_id attributes.
Sensitive data (credentials, API keys, user content) is never included in span attributes.
"""

import copy
import json
import logging
import uuid
from collections.abc import Sequence
from typing import Any, Self, cast

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

from modulo.connectors.asana import AsanaConnector
from modulo.connectors.azure_key_vault import AzureKeyVaultConnector
from modulo.connectors.azure_pipelines import AzurePipelinesConnector
from modulo.connectors.azure_repos import AzureReposConnector
from modulo.connectors.base import (
    ConnectorACL,
    ConnectorBase,
    ConnectorPayload,
    ConnectorQuery,
    ConnectorResult,
    ConnectorType,
    HealthResult,
)
from modulo.connectors.bitbucket import BitbucketConnector
from modulo.connectors.buildkite import BuildkiteConnector
from modulo.connectors.ci_runner import GitHubActionsCIRunner, GitLabCIRunner
from modulo.connectors.circleci import CircleCIConnector
from modulo.connectors.codeclimate import CodeClimateConnector
from modulo.connectors.confluence import ConfluenceConnector
from modulo.connectors.datadog import DatadogConnector
from modulo.connectors.discord import DiscordConnector
from modulo.connectors.dropbox_paper import DropboxPaperConnector
from modulo.connectors.filesystem import FilesystemConnector
from modulo.connectors.gitea import GiteaConnector
from modulo.connectors.github import GitHubConnector
from modulo.connectors.gitlab import GitLabConnector
from modulo.connectors.grafana import GrafanaConnector
from modulo.connectors.jenkins import JenkinsConnector
from modulo.connectors.jira import JiraConnector
from modulo.connectors.linear import LinearConnector
from modulo.connectors.microsoft_teams import MicrosoftTeamsConnector
from modulo.connectors.monday import MondayConnector
from modulo.connectors.n8n import N8NConnector
from modulo.connectors.notion import NotionConnector
from modulo.connectors.npm import NpmConnector
from modulo.connectors.onepassword import OnePasswordConnector
from modulo.connectors.opsgenie import OpsgenieConnector
from modulo.connectors.pagerduty import PagerDutyConnector
from modulo.connectors.pypi import PyPIConnector
from modulo.connectors.sentry import SentryConnector
from modulo.connectors.sharepoint import SharePointConnector
from modulo.connectors.shortcut import ShortcutConnector
from modulo.connectors.slack import SlackConnector
from modulo.connectors.snyk import SnykConnector
from modulo.connectors.sonarqube import SonarQubeConnector
from modulo.connectors.teamcity import TeamCityConnector
from modulo.connectors.trello import TrelloConnector
from modulo.connectors.trivy import TrivyConnector
from modulo.connectors.youtrack import YouTrackConnector
from modulo.core.pipeline_engine.output_filter import filter_payload_for_injection
from modulo.core.plugin_registry import get_plugin_registry
from modulo.core.secrets_backend import SecretsBackend
from modulo.db.models.connector_instance import ConnectorInstance

from .locking import ConnectorLockError as ConnectorLockError

logger = logging.getLogger(__name__)


class ConnectorNotFoundError(KeyError):
    """Raised when hub.get() is called with an unregistered connector ID."""

    def __init__(self, connector_id: uuid.UUID) -> None:
        super().__init__(str(connector_id))
        self.connector_id = connector_id


class ConnectorDecryptError(ValueError):
    """Raised when credentials cannot be decrypted (wrong key or corrupted data)."""

    def __init__(self, connector_id: uuid.UUID) -> None:
        super().__init__(f"Failed to decrypt credentials for connector {connector_id}")
        self.connector_id = connector_id


class ConnectorHub:
    """Decrypts connector credentials once at run-start; discards them on exit.

    Not thread-safe. Each run gets its own ConnectorHub instance.
    """

    def __init__(
        self,
        secrets_backend: SecretsBackend,
        org_id: str | None = None,
        runtime_provider: Any = None,
    ) -> None:
        self._secrets_backend = secrets_backend
        self._connectors: dict[uuid.UUID, ConnectorBase] = {}
        self._acls: dict[uuid.UUID, ConnectorACL] = {}
        self._tracer = trace.get_tracer("modulo.connector_hub")
        self._org_id = org_id
        self._runtime_provider = runtime_provider
        self._initialised = False

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        self._connectors.clear()
        self._acls.clear()

    async def initialise(self, instances: Sequence[ConnectorInstance]) -> None:
        """Decrypt credentials and initialise connectors. Call once at run start.

        ACLs are built from instance visibility and allowed_operations columns.
        Connectors that fail to initialise are skipped and logged individually
        so that one misconfigured connector does not block the rest.
        """
        if self._initialised:
            logger.warning("ConnectorHub already initialised — skipping")
            return
        for ci in instances:
            try:
                raw_str = await self._secrets_backend.get_secret(str(ci.id))
                try:
                    creds: dict[str, Any] = json.loads(raw_str)
                except json.JSONDecodeError as exc:
                    raise ConnectorDecryptError(ci.id) from exc
                connector = _build_connector(
                    ci.connector_type_id,
                    ci.config_json,
                    creds,
                    runtime_provider=self._runtime_provider,
                )
                acl = ConnectorACL(
                    visibility=ci.visibility,
                    allowed_operations=ci.allowed_operations,
                )
                traced = _TracedConnector(
                    connector,
                    tracer=self._tracer,
                    org_id=self._org_id,
                    acl=acl,
                )
                self._connectors[ci.id] = traced
                self._acls[ci.id] = acl
            except Exception as exc:
                logger.warning(
                    "Skipping connector %s (%s): %s",
                    ci.id,
                    ci.connector_type_id,
                    exc,
                    exc_info=True,
                )
        self._initialised = True

    def _get_or_raise(self, connector_id: uuid.UUID) -> ConnectorBase:
        try:
            return self._connectors[connector_id]
        except KeyError:
            raise ConnectorNotFoundError(connector_id) from None

    def _lookup(self, connector_id: uuid.UUID) -> ConnectorBase:
        return self._get_or_raise(connector_id)

    def get(self, connector_id: uuid.UUID, *, operation: str | None = None) -> ConnectorBase:
        """Return the initialised connector. Raises ConnectorNotFoundError if absent.

        When *operation* is provided, ACL is checked before returning the connector.
        Callers that already enforce ACL at a higher layer may omit it.
        """
        connector = self._lookup(connector_id)
        if operation is not None:
            self._acls[connector_id].check(operation)
        return connector

    def acl(self, connector_id: uuid.UUID) -> ConnectorACL:
        """Return the ACL for a connector. Raises ConnectorNotFoundError if absent."""
        self._get_or_raise(connector_id)
        return self._acls[connector_id]

    async def sample(
        self,
        connector_id: uuid.UUID,
        resource: str,
        filters: dict[str, Any] | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Sample data from a connector by querying the given resource.

        Convenience method that wraps get() + query() into a single call.
        ACL is enforced for 'read' operation.
        """
        connector = self.get(connector_id, operation="read")
        query = ConnectorQuery(
            resource=resource,
            filters=filters or {},
            limit=limit,
        )
        result = await connector.query(query)
        return result.records

    @property
    def connector_ids(self) -> frozenset[uuid.UUID]:
        return frozenset(self._connectors)


class _TracedConnector(ConnectorBase):
    """Proxy wrapper that adds OTel spans and ACL enforcement around every connector operation.

    Spans carry connector_type, operation_name, and org_id attributes but NEVER
    include credentials, API keys, or user content (queries, payloads).
    ACL is checked before each operation when an ACL object is provided.
    """

    def __init__(
        self,
        inner: ConnectorBase,
        tracer: trace.Tracer,
        org_id: str | None = None,
        acl: ConnectorACL | None = None,
    ) -> None:
        self._inner = inner
        self._tracer = tracer
        self._acl = acl
        self._base_attrs: dict[str, str] = {}
        if org_id is not None:
            self._base_attrs["connector.org_id"] = org_id

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    @property
    def connector_type(self) -> ConnectorType:
        return self._inner.connector_type

    def _enforce_acl(self, operation: str) -> None:
        if self._acl is not None:
            self._acl.check(operation)

    async def _run_with_tracing(
        self,
        span_name: str,
        operation: str,
        method: Any,
        *args: Any,
        extra_attrs: dict[str, Any] | None = None,
        post_span: Any = None,
        acl_operation: str | None = None,
        **kwargs: Any,
    ) -> Any:
        if acl_operation is not None:
            self._enforce_acl(acl_operation)
        attrs = self._base_attrs | {
            "connector.type": str(self._inner.connector_type),
            "connector.operation": operation,
        }
        if extra_attrs:
            attrs |= extra_attrs
        with self._tracer.start_as_current_span(span_name, attributes=attrs) as span:
            try:
                result = await method(*args, **kwargs)
                span.set_status(Status(StatusCode.OK))
                if post_span:
                    post_span(span, result)
                return result
            except Exception as exc:
                span.set_status(Status(StatusCode.ERROR, f"{operation} failed: {exc}"))
                span.record_exception(exc)
                raise

    async def health_check(self) -> HealthResult:
        return cast(
            HealthResult,
            await self._run_with_tracing(
                f"connector.{self._inner.connector_type}.health_check",
                "health_check",
                self._inner.health_check,
                post_span=lambda span, result: span.set_attribute("connector.healthy", result.ok),
                acl_operation="read",
            ),
        )

    async def query(self, q: ConnectorQuery) -> ConnectorResult:
        return cast(
            ConnectorResult,
            await self._run_with_tracing(
                f"connector.{self._inner.connector_type}.query",
                "query",
                self._inner.query,
                q,
                extra_attrs={"connector.resource": q.resource, "connector.limit": q.limit},
                post_span=lambda span, result: (
                    span.set_attribute("connector.result_total", result.total) if result.total is not None else None
                ),
                acl_operation="read",
            ),
        )

    async def write(self, payload: ConnectorPayload) -> dict[str, Any]:
        payload = copy.deepcopy(payload)
        filter_payload_for_injection(payload)
        return cast(
            dict[str, Any],
            await self._run_with_tracing(
                f"connector.{self._inner.connector_type}.write",
                "write",
                self._inner.write,
                payload,
                extra_attrs={"connector.resource": payload.resource},
                acl_operation="write",
            ),
        )


def _get_cred(creds: dict[str, Any], key: str, type_id: str) -> Any:
    try:
        return creds[key]
    except KeyError:
        raise ValueError(f"Missing credential key {key!r} for connector type {type_id!r}") from None


def _require_config(config: dict[str, Any] | None, key: str, label: str) -> str:
    """Extract and validate a required config value. Raises ValueError if missing."""
    value = (config or {}).get(key)
    if not value:
        raise ValueError(f"{label} requires {key!r} in config_json")
    return value


def _build_connector(
    type_id: str,
    config: dict[str, Any] | None,
    creds: dict[str, Any],
    runtime_provider: Any = None,
) -> ConnectorBase:
    config = config or {}
    match type_id:
        case "filesystem":
            base_path = _require_config(config, "base_path", "FilesystemConnector")
            return FilesystemConnector(base_path=base_path)
        case "gitea":
            base_url = config.get("base_url", "https://codeberg.org")
            return GiteaConnector(token=_get_cred(creds, "token", type_id), base_url=base_url)
        case "azure_repos":
            organization = _require_config(config, "organization", "AzureReposConnector")
            return AzureReposConnector(token=_get_cred(creds, "token", type_id), organization=organization)
        case "bitbucket":
            return BitbucketConnector(token=_get_cred(creds, "token", type_id))
        case "github":
            return GitHubConnector(token=_get_cred(creds, "token", type_id))
        case "github_actions_ci":
            return GitHubActionsCIRunner(token=_get_cred(creds, "token", type_id))
        case "gitlab_ci":
            base_url = config.get("base_url", "https://gitlab.com/api/v4")
            return GitLabCIRunner(token=_get_cred(creds, "token", type_id), base_url=base_url)
        case "gitlab":
            return GitLabConnector(token=_get_cred(creds, "token", type_id))
        case "shell":
            allowed = config.get("allowed_commands")
            from modulo.connectors.shell import ShellConnector

            return ShellConnector(runtime_provider=runtime_provider, allowed_commands=allowed)
        case "linear":
            return LinearConnector(api_key=_get_cred(creds, "api_key", type_id))
        case "jira":
            instance = config.get("instance") or config.get("base_url")
            if not instance:
                raise ValueError("JiraConnector requires 'instance' or 'base_url' in config_json")
            return JiraConnector(instance=instance, creds=creds)
        case "slack":
            return SlackConnector(bot_token=_get_cred(creds, "bot_token", type_id))
        case "sharepoint":
            return SharePointConnector(token=_get_cred(creds, "token", type_id))
        case "shortcut":
            return ShortcutConnector(token=_get_cred(creds, "token", type_id))
        case "trello":
            return TrelloConnector(
                api_key=_get_cred(creds, "api_key", type_id),
                token=_get_cred(creds, "token", type_id),
            )
        case "asana":
            return AsanaConnector(personal_access_token=_get_cred(creds, "personal_access_token", type_id))
        case "monday":
            return MondayConnector(api_key=_get_cred(creds, "api_key", type_id))
        case "youtrack":
            return YouTrackConnector(
                token=_get_cred(creds, "token", type_id),
                base_url=config.get("base_url", "https://youtrack.mycompany.com/api"),
            )
        case "notion":
            return NotionConnector(token=_get_cred(creds, "token", type_id))
        case "npm":
            return NpmConnector(token=_get_cred(creds, "token", type_id))
        case "pypi":
            return PyPIConnector(token=_get_cred(creds, "token", type_id))
        case "dropbox_paper":
            return DropboxPaperConnector(token=_get_cred(creds, "token", type_id))
        case "buildkite":
            return BuildkiteConnector(token=_get_cred(creds, "token", type_id))
        case "circleci":
            return CircleCIConnector(token=_get_cred(creds, "token", type_id))
        case "jenkins":
            return JenkinsConnector(
                username=_get_cred(creds, "username", type_id),
                token=_get_cred(creds, "token", type_id),
                base_url=config.get("base_url", "http://localhost:8080"),
            )
        case "confluence":
            instance = _require_config(config, "instance", "ConfluenceConnector")
            return ConfluenceConnector(instance=instance, creds=creds)
        case "teamcity":
            return TeamCityConnector(
                token=_get_cred(creds, "token", type_id),
                base_url=config.get("base_url", "http://localhost:8111"),
            )
        case "azure_key_vault":
            vault_url = _require_config(config, "vault_url", "AzureKeyVaultConnector")
            return AzureKeyVaultConnector(
                token=_get_cred(creds, "token", type_id),
                vault_url=vault_url,
            )
        case "azure_pipelines":
            organization = _require_config(config, "organization", "AzurePipelinesConnector")
            project = config.get("project", "")
            return AzurePipelinesConnector(
                token=_get_cred(creds, "token", type_id),
                organization=organization,
                project=project,
            )
        case "datadog":
            return DatadogConnector(
                api_key=_get_cred(creds, "api_key", type_id),
                app_key=_get_cred(creds, "app_key", type_id),
                site=config.get("site", "us"),
            )
        case "sentry":
            return SentryConnector(
                token=_get_cred(creds, "token", type_id),
                organization=config.get("organization", ""),
                base_url=config.get("base_url", "https://sentry.io"),
            )
        case "pagerduty":
            return PagerDutyConnector(token=_get_cred(creds, "token", type_id))
        case "grafana":
            return GrafanaConnector(
                token=_get_cred(creds, "token", type_id), base_url=config.get("base_url", "http://localhost:3000")
            )
        case "microsoft_teams":
            return MicrosoftTeamsConnector(token=_get_cred(creds, "token", type_id))
        case "discord":
            return DiscordConnector(token=_get_cred(creds, "token", type_id))
        case "onepassword":
            return OnePasswordConnector(
                token=_get_cred(creds, "token", type_id),
                base_url=config.get("base_url", "http://localhost:8080"),
            )
        case "opsgenie":
            return OpsgenieConnector(api_key=_get_cred(creds, "api_key", type_id))
        case "sonarqube":
            return SonarQubeConnector(
                token=_get_cred(creds, "token", type_id),
                base_url=config.get("base_url", "http://localhost:9000"),
            )
        case "codeclimate":
            return CodeClimateConnector(token=_get_cred(creds, "token", type_id))
        case "snyk":
            return SnykConnector(token=_get_cred(creds, "token", type_id))
        case "trivy":
            return TrivyConnector(
                token=_get_cred(creds, "token", type_id),
                base_url=config.get("base_url", "http://localhost:8080"),
            )
        case "n8n":
            return N8NConnector(
                token=_get_cred(creds, "token", type_id),
                base_url=config.get("base_url", "http://localhost:5678"),
            )
        case _:
            registry = get_plugin_registry()
            if registry.has_connector_type(type_id):
                return registry.build_connector(type_id, config, creds)
            raise ValueError(f"Unknown connector type: {type_id!r}")
