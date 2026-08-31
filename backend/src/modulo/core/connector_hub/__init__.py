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

import asyncio
import copy
import inspect
import json
import logging
import uuid
from collections.abc import Callable, Sequence
from typing import Any, Self, cast

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

from modulo.connectors._rate_bucket import SharedBudgetUnavailableError
from modulo.connectors.asana import AsanaConnector
from modulo.connectors.azure_key_vault import AzureKeyVaultConnector
from modulo.connectors.azure_pipelines import AzurePipelinesConnector
from modulo.connectors.azure_repos import AzureReposConnector
from modulo.connectors.base import (
    CompensationContext,
    CompensationOperation,
    CompensationResult,
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
from modulo.connectors.rest import RestConnector, SecurityGuard
from modulo.connectors.sentry import SentryConnector
from modulo.connectors.sharepoint import SharePointConnector
from modulo.connectors.shell import ShellConnector
from modulo.connectors.shortcut import ShortcutConnector
from modulo.connectors.slack import SlackConnector
from modulo.connectors.snyk import SnykConnector
from modulo.connectors.sonarqube import SonarQubeConnector
from modulo.connectors.teamcity import TeamCityConnector
from modulo.connectors.trello import TrelloConnector
from modulo.connectors.trivy import TrivyConnector
from modulo.connectors.youtrack import YouTrackConnector
from modulo.core.plugin_registry import get_plugin_registry
from modulo.core.secrets_backend import SecretsBackend
from modulo.db.models.connector_instance import ConnectorInstance

logger = logging.getLogger(__name__)

_SAMPLE_LIMIT: int = 200
_OTEL_ATTR_CONNECTOR_RESOURCE = "connector.resource"
_LOCALHOST_8080: str = "http://localhost:8080"
_LOCALHOST_3000: str = "http://localhost:3000"
_LOCALHOST_5678: str = "http://localhost:5678"
_LOCALHOST_8111: str = "http://localhost:8111"
_LOCALHOST_9000: str = "http://localhost:9000"

# FAR-496 read-side heal: the REST create/update endpoints store `credentials`
# as a bare string, Fernet-encrypted into `credentials_ciphertext`. Token-keyed
# connector types below read a DIFFERENT credential key in ``_build_connector``,
# so legacy bare-token ciphertext rows for those types always failed
# instantiation with "Missing credential key 'token'". The bare-scalar fallback
# wrap in ``initialise()`` therefore wraps a decrypted bare scalar under the
# connector type's OWN single credential key (see ``_bare_credential_key``),
# healing legacy rows at read time — no write-side migration needed. Multi-key
# types (jira, trello, jenkins, confluence, datadog, rest, ticket-tracker)
# require JSON-dict credentials and keep the legacy "api_key" default, as do
# plugin types.

# Process-lifetime dedup for connector skip-warnings (FAR-465): a fresh hub is
# built per run/request, so one persistently misconfigured connector used to
# re-log its FULL traceback on every initialise() and flood worker logs. The
# first sighting of a (instance, type, "ExcType: message") key logs the full
# traceback; later sightings log a concise repeat line. The key space is
# bounded by the number of connector instances in the deployment, so the set
# cannot grow unbounded within a process.
_SKIP_WARN_SEEN: set[tuple[str, str, str]] = set()


class ConnectorNotFoundError(KeyError):
    """Raised when hub.get() is called with an unregistered connector ID."""

    def __init__(self, connector_id: uuid.UUID) -> None:
        super().__init__(f"Connector not found: {connector_id}")
        self.connector_id = connector_id


class ConnectorDecryptError(ValueError):
    """Raised when credentials cannot be decrypted (wrong key or corrupted data)."""

    def __init__(self, connector_id: uuid.UUID) -> None:
        super().__init__(f"Failed to decrypt credentials for connector {connector_id}")
        self.connector_id = connector_id


def resolve_shared_rate_limit_redis(org_id: str | None) -> Any | None:
    """Resolve the shared rate-limit Redis client for an org, fail-closed (FAR-439).

    This is the single composition root BOTH the run-executor path (through
    ``ConnectorHub``) and the trigger/polling path (FAR-442) use to wire a shared
    per-destination rate budget into REST connectors. It returns ``None`` ONLY when
    a shared budget genuinely does not exist:

    * Redis is not configured (no ``settings.redis_url`` or the DB is SQLite), or
    * no ``org_id`` is supplied (a non-tenant probe path, e.g. health-check /
      schema-inference). Wiring a shared budget without a tenant would bucket every
      organisation under a single ``"default"`` key — a cross-tenant leak. The
      guard is truthiness-based: an empty-string ``org_id`` is also treated as a
      non-tenant probe (a non-empty tenant id is required to compose a Redis key).

    On a tenant path (``org_id`` present) where Redis IS configured the client is
    AUTHORITATIVE and FAIL-CLOSED: a settings-read failure or a client construction
    failure raises :class:`SharedBudgetUnavailableError` rather than returning
    ``None``. Returning ``None`` there would make the REST connector fall back to its
    per-process local bucket, silently reconstructing the fleet-wide ``N x burst``
    fail-open FAR-439 removed.
    """
    try:
        settings = _read_rate_limit_settings()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _handle_settings_read_failure(exc, org_id)
        return None

    if _shared_redis_unconfigured(settings):
        # Redis is genuinely NOT configured — the connector-local bucket is correct
        # (no shared budget exists to multiply).
        return None
    if not org_id:
        # Non-tenant probe path (None or an empty string): never wire a shared
        # budget — every org would otherwise land on the "default" tenant key and
        # share one Redis budget across distinct orgs (cross-tenant leak). These
        # short-lived probes stay on the connector-local bucket, which is correct.
        return None

    return _build_shared_redis_client(settings)


def _read_rate_limit_settings() -> Any:
    """Read app settings (the source of ``redis_url`` for the shared budget)."""
    from modulo.settings import get_settings

    return get_settings()


def _handle_settings_read_failure(exc: Exception, org_id: str | None) -> None:
    """Fail-closed on the tenant path, degrade to the local bucket on a probe path."""
    if org_id:
        logger.error(
            "Settings could not be read on the tenant path — fail-closed (no local-bucket fallback)",
            exc_info=True,
        )
        raise SharedBudgetUnavailableError(
            f"settings could not be read to wire the shared rate-limit budget: {exc}"
        ) from exc
    logger.warning(
        "Unable to read settings for the shared Redis rate limiter — using the local bucket",
        exc_info=True,
    )


def _shared_redis_unconfigured(settings: Any) -> bool:
    """True when no shared Redis budget exists and the connector-local bucket is correct."""
    return not settings.redis_url or settings.modulo_db.lower() == "sqlite"


def _build_shared_redis_client(settings: Any) -> Any:
    """Construct the shared Redis client, fail-closed on any construction error (FAR-439)."""
    try:
        from redis.asyncio import Redis

        return Redis.from_url(settings.redis_url, decode_responses=False, socket_connect_timeout=5, socket_timeout=10)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        # Redis IS configured (tenant path) but the client could not be constructed.
        # `Redis.from_url` only parses the URL, so a failure here is a
        # malformed/unsupported `redis_url` — a hard config error. Never degrade to
        # the local bucket (that would reconstruct the fleet-wide fail-open FAR-439
        # removed).
        logger.exception(
            "Shared Redis client construction failed — fail-closed (no local-bucket fallback)",
        )
        raise SharedBudgetUnavailableError(
            f"shared rate-limit Redis client is configured but could not be constructed: {exc}"
        ) from exc


class ConnectorHub:
    """Decrypts connector credentials once at run-start; discards them on exit.

    Not thread-safe. Each run gets its own ConnectorHub instance.
    All public methods raise ConnectorNotFoundError if called before initialise().
    """

    def __init__(
        self,
        secrets_backend: SecretsBackend,
        org_id: str | None = None,
        runtime_provider: Any = None,
        runtime_provider_hub: Any = None,
    ) -> None:
        self._secrets_backend = secrets_backend
        self._connectors: dict[uuid.UUID, ConnectorBase] = {}
        self._acls: dict[uuid.UUID, ConnectorACL] = {}
        self._tracer = trace.get_tracer("modulo.connector_hub")
        self._org_id = org_id
        self._runtime_provider = runtime_provider
        self._runtime_provider_hub = runtime_provider_hub
        self._initialised = False
        self._init_lock = asyncio.Lock()
        # Lazily-built shared Redis client used to wire the REST connector's
        # shared per-destination rate-limit budget (FAR-439). Owned by the hub
        # (closed at teardown), never by an individual connector. When the client
        # is configured but cannot be constructed (bad redis_url), the failure is
        # recorded so every later call fails closed.
        self._shared_redis: Any = None
        self._redis_attempted = False
        self._redis_error: Exception | None = None

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self._close_connectors()
        self.close()

    async def _close_connectors(self) -> None:
        """Close every held connector's async resources (e.g. pooled clients).

        Consumers that serve streaming/connection-pooled connectors (REST's
        ``httpx.AsyncClient``) need their async ``close()`` awaited at teardown or
        keepalive sockets leak. Only connectors that expose a ``close()`` are
        touched; a connector that does not is left to garbage collection. A
        failing ``close()`` is logged and does not abort the teardown of the rest.
        """
        for connector in self._connectors.values():
            close = getattr(connector, "close", None)
            if close is None:
                continue
            try:
                result = close()
                if inspect.isawaitable(result):
                    await result
            except Exception:
                logger.warning("Failed to close connector", exc_info=True)
        # The shared Redis client (FAR-439) is owned by the hub, not a connector —
        # close it here so the rate-limit budget never leaks a pool connection.
        shared_redis = self._shared_redis
        self._shared_redis = None
        if shared_redis is not None:
            try:
                await shared_redis.aclose()
            except Exception:
                logger.warning("Failed to close shared Redis client", exc_info=True)

    def _shared_redis_client(self) -> Any | None:
        """Return the lazily-built shared Redis client, or None when NOT configured.

        Thin caching wrapper over :func:`resolve_shared_rate_limit_redis` — the
        fail-closed composition root used by both the executor hub and the
        trigger/polling path (FAR-442). The shared client is ONLY wired on a
        tenant path — a ``ConnectorHub`` constructed with an ``org_id``.
        Non-executor hubs (health-check probes, schema-inference, determination
        scanning) carry no ``org_id``: wiring them to Redis would bucket every
        organisation's rate-limited REST connector under a single ``default``
        tenant key, sharing ONE budget across distinct orgs (a cross-tenant
        leak). Those short-lived probes stay on the connector-local per-process
        bucket, which is correct — there is no fleet-wide budget to multiply.

        When Redis *is* wired (tenant path, ``settings.redis_url`` set and the DB
        is not SQLite), the shared client is AUTHORITATIVE. Any settings-read or
        construction failure FAILS CLOSED (raises
        :class:`SharedBudgetUnavailableError`) and is recorded so every later
        call fails too; we NEVER degrade a configured Redis to ``None``, because
        returning ``None`` would make the REST connector fall back to its
        per-process local bucket, silently reconstructing the fleet-wide
        ``N x burst`` fail-open FAR-439 removed. Only the genuinely
        not-configured / non-tenant paths return ``None`` (correct, not a
        degrade).
        """
        if self._redis_error is not None:
            raise SharedBudgetUnavailableError(
                f"shared rate-limit Redis client is configured but could not be constructed: {self._redis_error}"
            ) from self._redis_error
        if self._shared_redis is not None or self._redis_attempted:
            return self._shared_redis
        self._redis_attempted = True
        try:
            client = resolve_shared_rate_limit_redis(self._org_id)
        except SharedBudgetUnavailableError as exc:
            # Record the failure so EVERY later call also fails closed (never
            # degrades to the per-process local bucket on a subsequent call).
            self._redis_error = exc
            raise
        self._shared_redis = client
        return client

    def close(self) -> None:
        """Release every held connector and its decrypted credentials.

        Drops the hub's references to all connectors and ACLs so the
        connector objects (and the credential-bearing state they carry)
        become unreachable and are eligible for garbage collection. The hub
        is marked uninitialised; a subsequent ``initialise()`` rebuilds it
        for the next run. Safe to call multiple times and without an
        ``async with`` block.

        Note: asynchronous connector resources (e.g. the REST connector's pooled
        ``httpx.AsyncClient``) are closed by :meth:`_close_connectors`, which the
        ``async with`` teardown path awaits before this synchronous pruning.
        """
        self._connectors.clear()
        self._acls.clear()
        self._initialised = False

    async def initialise(
        self,
        instances: Sequence[ConnectorInstance],
        *,
        allowed_connectors: Sequence[str] | None = None,
    ) -> None:
        """Decrypt credentials and initialise connectors. Call once at run start.

        ACLs are built from instance visibility and allowed_operations columns.
        Connectors that fail to initialise are skipped and logged individually
        so that one misconfigured connector does not block the rest.

        Fetch-time scoping (FAR-418): when *allowed_connectors* is provided it
        gates the FETCH set BEFORE any credential is decrypted — the hub decrypts
        only the named instance-ids/types, so connectors outside the scope never
        expose credentials (deny-by-default within the scope). When *None* (the
        default) the hub is unrestricted and fetches every instance, preserving
        the pre-scope behaviour exactly.
        """
        if self._initialised:
            logger.warning("ConnectorHub already initialised — skipping")
            return
        async with self._init_lock:
            if self._initialised:
                logger.warning("ConnectorHub already initialised — skipping")
                return
            fetch_scope: set[str] | None = set(allowed_connectors) if allowed_connectors is not None else None
            for ci in instances:
                if fetch_scope is not None and not _in_fetch_scope(ci, fetch_scope):
                    continue
                try:
                    try:
                        raw_str = await asyncio.wait_for(
                            self._secrets_backend.get_secret(str(ci.id)),
                            timeout=30.0,
                        )
                    except KeyError:
                        # Fall back to credentials_ciphertext column
                        ciphertext = getattr(ci, "credentials_ciphertext", None)
                        if ciphertext and isinstance(ciphertext, bytes) and ciphertext != b"":
                            try:
                                from cryptography.fernet import Fernet

                                from modulo.settings import get_settings

                                _settings = get_settings()
                                f = Fernet(_settings.fernet_key.encode())
                                plaintext = f.decrypt(ciphertext).decode("utf-8")
                                # Multi-field creds round-trip: a JSON dict in the
                                # ciphertext is used as-is (REST auth_mode/token/
                                # api_key/...); a bare scalar is wrapped under the
                                # connector type's own single credential key
                                # (FAR-496 read-side heal — see
                                # _bare_credential_key), falling back to the
                                # legacy single api_key wrapper for unlisted types.
                                try:
                                    parsed_plain = json.loads(plaintext)
                                except json.JSONDecodeError:
                                    parsed_plain = None
                                if isinstance(parsed_plain, dict):
                                    raw_str = plaintext
                                else:
                                    # Wrap the bare scalar under the connector
                                    # type's OWN credential key so token-keyed
                                    # types (github, linear, slack, ...) heal
                                    # instead of failing with
                                    # "Missing credential key 'token'" (FAR-496).
                                    cred_key = _bare_credential_key(ci.connector_type_id)
                                    raw_str = json.dumps({cred_key: plaintext})
                            except Exception:
                                logger.warning(
                                    "Failed to decrypt credentials_ciphertext for connector %s", ci.id, exc_info=True
                                )
                                raise ConnectorDecryptError(ci.id) from None
                        else:
                            raw_str = "{}"
                    if raw_str is None:
                        raw_str = "{}"
                    try:
                        parsed = json.loads(raw_str)
                    except json.JSONDecodeError as exc:
                        raise ConnectorDecryptError(ci.id) from exc
                    if not isinstance(parsed, dict):
                        raise ConnectorDecryptError(ci.id) from TypeError(f"Expected dict, got {type(parsed).__name__}")
                    creds: dict[str, Any] = parsed
                    connector = _build_connector(
                        ci.connector_type_id,
                        ci.config_json,
                        creds,
                        runtime_provider=self._runtime_provider,
                        runtime_provider_hub=self._runtime_provider_hub,
                        # NOTE (FAR-439 trade-off): the shared Redis client is
                        # constructed for EVERY connector row here, not only for
                        # rate-limited REST connectors. A malformed ``redis_url``
                        # therefore fail-closes runs whose connectors would never
                        # touch the shared budget (GitHub / Linear / non-rate-limited
                        # REST). This is accepted deliberately: a misconfigured
                        # Redis URL is a fleet-wide configuration error and must fail
                        # loud rather than silently per-process for some connectors
                        # and shared for others. ``get_settings()`` is re-read here
                        # after the executor already read it; the cache makes this a
                        # cheap lookup, not a second DB round-trip.
                        redis_client=self._shared_redis_client(),
                        tenant_id=str(self._org_id) if self._org_id else None,
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
                except (
                    ConnectorDecryptError,
                    ValueError,
                    TypeError,
                    KeyError,
                    OSError,
                ) as exc:
                    # Skip-warn dedup (FAR-465): full traceback once per
                    # (instance, error) per process, concise repeat afterwards.
                    # Check-and-add is synchronous (no await between) so it is
                    # race-free under asyncio.
                    skip_key = (str(ci.id), ci.connector_type_id, type(exc).__name__ + ": " + str(exc))
                    if skip_key in _SKIP_WARN_SEEN:
                        logger.warning(
                            "Skipping connector %s (%s) (repeat; full traceback logged earlier)",
                            ci.id,
                            ci.connector_type_id,
                        )
                    else:
                        logger.warning(
                            "Skipping connector %s (%s)",
                            ci.id,
                            ci.connector_type_id,
                            exc_info=True,
                        )
                        _SKIP_WARN_SEEN.add(skip_key)
                except SharedBudgetUnavailableError:
                    # A configured-but-unconstructable shared Redis client is a
                    # hard config error (FAR-439): degrade to the local bucket
                    # would reconstruct the fleet-wide fail-open. Propagate so the
                    # run fails closed (loudly) instead of silently mis-limiting.
                    raise
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception(
                        "Unexpected error skipping connector %s (%s) — programming bug",
                        ci.id,
                        ci.connector_type_id,
                    )
            self._initialised = True

    def _get_or_raise(self, connector_id: uuid.UUID) -> ConnectorBase:
        if not self._initialised:
            raise ConnectorNotFoundError(connector_id)
        try:
            return self._connectors[connector_id]
        except KeyError:
            raise ConnectorNotFoundError(connector_id) from None

    def get(self, connector_id: uuid.UUID, *, operation: str | None = None) -> ConnectorBase:
        """Return the initialised connector. Raises ConnectorNotFoundError if absent.

        When *operation* is provided, ACL is checked before returning the connector.
        Callers that already enforce ACL at a higher layer may omit it.
        """
        connector = self._get_or_raise(connector_id)
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
        limit: int = _SAMPLE_LIMIT,
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
        post_span: Callable[..., Any] | None = None,
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
                    try:
                        post_span(span, result)
                    except Exception as meta_exc:
                        logger.warning("post_span callback failed: %s", meta_exc, exc_info=True)
                return result
            except asyncio.CancelledError:
                span.set_status(Status(StatusCode.ERROR, f"{operation} cancelled"))
                raise
            except Exception as exc:
                span.set_status(Status(StatusCode.ERROR, f"{operation} failed"))
                span.set_attribute("connector.error_type", type(exc).__name__)
                raise

    async def health_check(self) -> HealthResult:
        return cast(
            "HealthResult",
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
            "ConnectorResult",
            await self._run_with_tracing(
                f"connector.{self._inner.connector_type}.query",
                "query",
                self._inner.query,
                q,
                extra_attrs={_OTEL_ATTR_CONNECTOR_RESOURCE: q.resource, "connector.limit": q.limit},
                post_span=lambda span, result: (
                    span.set_attribute("connector.result_total", result.total) if result.total is not None else None
                ),
                acl_operation="read",
            ),
        )

    async def write(self, payload: ConnectorPayload) -> dict[str, Any]:
        payload = copy.deepcopy(payload)
        from modulo.core.pipeline_engine.output_filter import filter_payload_for_injection

        filter_payload_for_injection(payload)
        return cast(
            "dict[str, Any]",
            await self._run_with_tracing(
                f"connector.{self._inner.connector_type}.write",
                "write",
                self._inner.write,
                payload,
                extra_attrs={_OTEL_ATTR_CONNECTOR_RESOURCE: payload.resource},
                acl_operation="write",
            ),
        )

    async def compensate(
        self,
        operation: CompensationOperation,
        *,
        context: CompensationContext,
        error: str,
    ) -> CompensationResult:
        """Forward a compensating callback to the wrapped connector (FAR-213).

        Compensation is best-effort run-termination undo — no ACL gate (the node
        already performed the write through this connector; compensation is not
        a new capability grant) and never raw payloads in span attributes.
        """
        return cast(
            "CompensationResult",
            await self._run_with_tracing(
                f"connector.{self._inner.connector_type}.compensate",
                "compensate",
                self._inner.compensate,
                operation,
                context=context,
                error=error,
                extra_attrs={_OTEL_ATTR_CONNECTOR_RESOURCE: operation.resource},
                acl_operation=None,
            ),
        )


def _in_fetch_scope(instance: ConnectorInstance, fetch_scope: set[str]) -> bool:
    """Return True when a connector instance is inside the run's fetch scope.

    An instance is fetched when its UUID (string) OR its connector type is
    explicitly named in the scope. The scope is a deny-by-default allow-list:
    anything not named is never decrypted.
    """
    return str(instance.id) in fetch_scope or instance.connector_type_id in fetch_scope


# Token-keyed connector types whose single credential is read under the key
# "token" (see the `_get_cred(creds, "token", type_id)` calls in `_build_connector`
# below). A bare (non-JSON) ciphertext scalar for these types must be wrapped
# under "token" on the read-side fallback, NOT the legacy "api_key" wrapper
# (FAR-496).
_TOKEN_CRED_TYPES: frozenset[str] = frozenset(
    {
        "gitea",
        "azure_repos",
        "bitbucket",
        "github",
        "github_actions_ci",
        "gitlab_ci",
        "gitlab",
        "linear",
        "sharepoint",
        "shortcut",
        "youtrack",
        "notion",
        "npm",
        "pypi",
        "dropbox_paper",
        "buildkite",
        "circleci",
        "teamcity",
        "azure_key_vault",
        "azure_pipelines",
        "sentry",
        "pagerduty",
        "grafana",
        "microsoft_teams",
        "discord",
        "onepassword",
        "sonarqube",
        "codeclimate",
        "snyk",
        "trivy",
        "n8n",
    }
)

# Single-key overrides for token-keyed types whose credential key is not "token".
_BARE_CRED_KEY_OVERRIDES: dict[str, str] = {
    "slack": "bot_token",
    "asana": "personal_access_token",
}


def _bare_credential_key(type_id: str) -> str:
    """Return the credential key a bare (non-JSON) ciphertext scalar is wrapped under.

    Token-keyed connectors read a credential key other than "api_key" (FAR-496):
    wrapping a bare token under "api_key" guarantees instantiation failure with
    ``Missing credential key 'token'``. We therefore wrap under the connector
    type's OWN credential key. api_key-keyed single types (monday, opsgenie, ...)
    and multi-key types (jira, datadog, rest, confluence, trello, jenkins,
    ticket-tracker, ...) keep the legacy "api_key" default — multi-key types
    require a JSON-dict credential anyway, so a bare scalar can never be valid
    for them and their shape must not change.
    """
    if type_id in _BARE_CRED_KEY_OVERRIDES:
        return _BARE_CRED_KEY_OVERRIDES[type_id]
    if type_id in _TOKEN_CRED_TYPES:
        return "token"
    return "api_key"


def _get_cred(creds: dict[str, Any], key: str, type_id: str) -> Any:
    try:
        return creds[key]
    except KeyError:
        raise ValueError(f"Missing credential key {key!r} for connector type {type_id!r}") from None


def _require_config(config: dict[str, Any] | None, key: str, label: str) -> str:
    """Extract and validate a required config value. Raises ValueError if missing, TypeError if not a string."""
    cfg = config or {}
    if key not in cfg:
        raise ValueError(f"{label} requires {key!r} in config_json")
    value = cfg[key]
    if not isinstance(value, str):
        raise TypeError(f"{label} config key {key!r} must be a string, got {type(value).__name__}")
    return value


def _core_security_guard() -> SecurityGuard:
    """Wire the production ``modulo.core`` SSRF + output-injection guards at the root.

    The REST connector depends on the (connector-local) ``SecurityGuard`` port
    rather than importing ``modulo.core`` directly; this is the single place that
    binds the port to the real ``modulo.core`` implementations. Imports are lazy
    to avoid pulling ``pipeline_engine`` / ``ssrf`` at connector-hub import time.
    """

    async def validate_url(url: str) -> None:
        from modulo.core.ssrf import validate_outbound_url_async

        await validate_outbound_url_async(url)

    def filter_strings(values: Sequence[str], resource: str) -> None:
        from modulo.core.pipeline_engine.output_filter import (
            OutputRejectedError,
            filter_output_for_injection,
        )

        for value in values:
            result = filter_output_for_injection(value)
            if not result.passed:
                raise OutputRejectedError(f"{result.reason} (REST resource: {resource!r})")

    return SecurityGuard(validate_url=validate_url, filter_strings=filter_strings)


def _build_connector(
    type_id: str,
    config: dict[str, Any] | None,
    creds: dict[str, Any],
    runtime_provider: Any = None,
    runtime_provider_hub: Any = None,
    *,
    redis_client: Any = None,
    tenant_id: str | None = None,
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
            base_url = config.get("base_url", "https://gitlab.com/api/v4")
            return GitLabConnector(token=_get_cred(creds, "token", type_id), base_url=base_url)
        case "shell":
            allowed = config.get("allowed_commands")
            env_profile_id = config.get("environment_profile_id")
            return ShellConnector(
                runtime_provider=runtime_provider,
                runtime_provider_hub=runtime_provider_hub,
                environment_profile_id=env_profile_id,
                allowed_commands=allowed,
            )
        case "jira":
            instance = config.get("instance", "")
            base_url = config.get("base_url")
            if not instance and not base_url:
                raise ValueError("JiraConnector requires 'instance' or 'base_url' in config_json")
            return JiraConnector(
                instance=instance,
                creds=creds,
                base_url=base_url,
                api_version=config.get("api_version", 3),
            )
        case "linear":
            return LinearConnector(token=_get_cred(creds, "token", type_id))
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
                base_url=_require_config(config, "base_url", "YouTrackConnector"),
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
                base_url=config.get("base_url", _LOCALHOST_8080),
            )
        case "confluence":
            instance = _require_config(config, "instance", "ConfluenceConnector")
            return ConfluenceConnector(instance=instance, creds=creds)
        case "teamcity":
            return TeamCityConnector(
                token=_get_cred(creds, "token", type_id),
                base_url=config.get("base_url", _LOCALHOST_8111),
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
                token=_get_cred(creds, "token", type_id), base_url=config.get("base_url", _LOCALHOST_3000)
            )
        case "microsoft_teams":
            return MicrosoftTeamsConnector(token=_get_cred(creds, "token", type_id))
        case "discord":
            return DiscordConnector(token=_get_cred(creds, "token", type_id))
        case "onepassword":
            return OnePasswordConnector(
                token=_get_cred(creds, "token", type_id),
                base_url=config.get("base_url", _LOCALHOST_8080),
            )
        case "opsgenie":
            return OpsgenieConnector(api_key=_get_cred(creds, "api_key", type_id))
        case "sonarqube":
            return SonarQubeConnector(
                token=_get_cred(creds, "token", type_id),
                base_url=config.get("base_url", _LOCALHOST_9000),
            )
        case "codeclimate":
            return CodeClimateConnector(token=_get_cred(creds, "token", type_id))
        case "snyk":
            return SnykConnector(token=_get_cred(creds, "token", type_id))
        case "trivy":
            return TrivyConnector(
                token=_get_cred(creds, "token", type_id),
                base_url=config.get("base_url", _LOCALHOST_8080),
            )
        case "n8n":
            return N8NConnector(
                token=_get_cred(creds, "token", type_id),
                base_url=config.get("base_url", _LOCALHOST_5678),
            )
        case "rest":
            # Generic REST connector: config_json + decrypted creds dict.
            # Multi-field auth (auth_mode/token/api_key/username/password/...)
            # arrives as a JSON dict via secrets_backend OR credentials_ciphertext
            # — not the single api_key fallback (see initialise()).
            return RestConnector(
                config=config,
                creds=creds,
                security_guard=_core_security_guard(),
                redis_client=redis_client,
                tenant_id=tenant_id,
            )
        case "ticket-tracker":
            provider = config.get("provider", "github")
            if provider == "github":
                from modulo.connectors.ticket_tracker.github import GitHubTicketTracker

                return GitHubTicketTracker(config, creds)
            if provider == "trello":
                from modulo.connectors.ticket_tracker.trello import TrelloTicketTracker

                return TrelloTicketTracker(config, creds)
            raise ValueError(f"Unknown ticket-tracker provider: {provider!r}")
        case _:
            registry = get_plugin_registry()
            if registry.has_connector_type(type_id):
                return registry.build_connector(type_id, config, creds)
            raise ValueError(f"Unknown connector type: {type_id!r}")
