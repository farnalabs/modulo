# Connector Authoring Guide

Connectors are Modulo's abstraction for external tool integrations. Each connector
implements the `ConnectorBase` ABC and registers with the `ConnectorHub`.

## Architecture

```
ConnectorBase (ABC)          ← modulo/connectors/base.py
  ├── 40+ built-in connectors (filesystem, github, gitlab, jira,
  │   linear, slack, shell, pagerduty, sentry, datadog, and more)
  │   including the generic REST connector (rest)
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

Each connector type is identified by its `connector_type` enum member, which
the graph validator uses to bind pipeline node requirements to the bound
connector at save-time and run-time. Read/write permissions are enforced by
the `ConnectorPermissionError` checks in `modulo/connectors/base.py`.

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
the real connector against test fixtures (e.g., local Git repositories for
`FilesystemConnector`).

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
[`docs/rest-connector.md`](rest-connector.md) – config shape, auth modes, the
verb-agnostic read/write mapping, retry, and security guards.
