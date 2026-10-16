# Connector Authoring Guide

Connectors are Modulo's abstraction for external tool integrations. Each connector
implements the `ConnectorBase` ABC and registers with the `ConnectorHub`.

## Architecture

```
ConnectorBase (ABC)          ← modulo/connectors/base.py
  ├── 43 connector types (filesystem, github, gitlab, jira,
  │   linear, slack, shell, pagerduty, sentry, datadog, and more;
  │   one `ConnectorType` enum member each)
  │   including the generic REST connector (rest) and the
  │   provider-dispatched `ticket-tracker` family; note that
  │   `ci-runner` is an abstract label the hub cannot build
  │   (the buildable CI types are github_actions_ci, gitlab_ci,
  │   woodpecker);
  │   see modulo/connectors/ for the full list
  ├── CIRunnerBase           ← modulo/connectors/ci_runner/ (abstract CI runner)
  ├── TicketTrackerBase      ← modulo/connectors/ticket_tracker/ (abstract ticket-tracker base)
  └── YourConnector          ← your package (via entry point)
```

## ConnectorBase interface

The abstract interface in `modulo/connectors/base.py` separates **reads**
(`query`) from **writes** (`write`):

```python
class ConnectorBase(ABC):
    """Abstract base for all connector implementations."""

    @property
    @abstractmethod
    def connector_type(self) -> ConnectorType:
        """Type identifier for this connector (a ConnectorType enum)."""

    @abstractmethod
    async def health_check(self) -> HealthResult:
        """Verify the connector's external service is reachable."""

    @abstractmethod
    async def query(self, q: ConnectorQuery) -> ConnectorResult:
        """Read data from the external tool."""

    @abstractmethod
    async def write(self, payload: ConnectorPayload) -> dict[str, Any]:
        """Write data to the external tool. Returns the created/updated resource."""
```

### Overridable (non-abstract) methods

These methods have default implementations and can be overridden for custom behaviour:

```python
class ConnectorBase(ABC):
    def on_unknown_for(self, resource: str) -> str:
        """Policy hook for unknown resource types (FAR-458 idempotency gate)."""

    def write_reported_failure(self, result: Any) -> bool:
        """Detect write failures from the result (FAR-531 failure detection)."""

    async def compensate(self, operation, *, context, error) -> CompensationResult:
        """Compensate a failed write operation (FAR-213 compensation)."""
```

### Credential redaction hooks (required if you hold secrets)

Two hooks are not optional for any connector that holds a credential. A
conformance test walks every class under `modulo.connectors` and fails if a
concrete connector holds credentials without a redaction recipe
(`backend/tests/unit/connectors/test_health_detail_credential_redaction.py`):

```python
from modulo.connectors.security import CredentialRedactor


class ConnectorBase(ABC):
    def _credential_values(self) -> Sequence[str]:
        """The connector's live credential strings as they appear in requests.

        Subclasses holding a token, API key or app password MUST override this
        to return the exact secret strings they send upstream, since upstream
        error bodies, redirect targets and proxies can echo them back verbatim.
        Connectors holding no secrets keep the empty default.
        """
        return ()

    def _redacted_detail(self, text: str) -> str:
        """Return *text* with this connector's credential values masked."""
        return CredentialRedactor(self._credential_values()).redact(text)
```

Route every string you embed into a `HealthResult.detail` (upstream body,
exception message, request URL) through `_redacted_detail`, otherwise a
credential echoed by the upstream service reaches the caller. The allowlist of
classes exempt from the test is `CIRunnerBase`, `FilesystemConnector`,
`ShellConnector` and `TicketTrackerBase`.

Two further optional seams worth knowing:

- `_egress_ssl_context: ssl.SSLContext | None` — the class attribute every
  connector's outbound client is built from; the `None` default means an
  instance that never makes an outbound call pays nothing.
- `async def close(self) -> None` — duck-typed, not declared on `ConnectorBase`.
  The hub awaits it on every held connector at teardown if present, so
  implement it if your connector owns a client or session pool.

The payload types are simple dataclasses:

```python
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
```

## Capability contract

Each connector type is identified by its `connector_type` enum member. The
graph validator binds pipeline node requirements from the connector instance's
stored `connector_type_id` column and its `allowed_operations` list (see
`core/graph_validator/`); the enum member itself is consulted only for the
CI-dispatch capability check. Read/write permissions are enforced by the
`ConnectorPermissionError` checks in `modulo/connectors/base.py`.

Note that swapping one provider for another usually changes the enum member as
well as the class: `GitHubConnector` and `GitLabConnector` are separate
`ConnectorType` members with different capability sets, so they are not
interchangeable behind one type id.

**Connector type naming convention:** mostly lowercase `kebab-case`
identifiers like `filesystem`, `github`, `ci-runner`, `ticket-tracker`, though
some members use `snake_case` (e.g. `azure_repos`, `dropbox_paper`,
`microsoft_teams`).

## Credential handling

Credentials are **never** stored as literal values in the connector source.
The `ConnectorHub` decrypts credentials once at run-start and passes them into
the connector **constructor**:

```python
class YourConnector(ConnectorBase):
    def __init__(self, api_key: str) -> None:
        self._api_key = api_key
```

## Swappable binding

Pipelines bind to connectors by type, not by instance. This means swapping
`GitHubConnector` for `GitLabConnector` requires **zero pipeline changes** –
just rebind to a different connector instance of the same `connector_type`.

## Testing

Unit tests use `unittest.mock` to stub external calls. Integration tests use
the real connector against test fixtures (e.g., `tmp_path` directories for
`FilesystemConnector`, which is a plain path connector with `file` and
`directory` resources and no Git behaviour).

```python
async def test_your_connector():
    connector = YourConnector(api_key="test-token")  # creds passed to the constructor
    result = await connector.query(ConnectorQuery(resource="issues", filters={"state": "open"}))
    assert result.total is None or len(result.records) <= result.total
```

## Registration

An entry point must point to a **builder function**, not to the class directly.
The builder receives `(config, creds)` and returns a connector instance:

```python
def build_your_connector(config: dict, creds: dict) -> ConnectorBase:
    return YourConnector(api_key=creds["api_key"])
```

```toml
[project.entry-points."modulo.connectors"]
your_connector = "your_package.connector:build_your_connector"
```

Or register the builder manually. Registration requires a `PluginManifest`:

```python
from modulo.core.plugin_registry import PluginManifest, get_plugin_registry

registry = get_plugin_registry()
registry.register_connector_type(
    "your_connector",
    build_your_connector,
    PluginManifest(
        PLUGIN_ID="my-plugins",
        display_name="My Plugins",
        description="Custom connector plugin",
        version="0.1.0",
    ),
)
```

## Generic REST connector

For pointing Modulo at an arbitrary HTTP endpoint (no vendor client), see
[`rest-connector.md`](rest-connector.md) – config shape, auth modes, the
verb-agnostic read/write mapping, retry, and security guards.
