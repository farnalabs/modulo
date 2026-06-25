# Connector Authoring Guide

Connectors are Modulo's abstraction for external tool integrations. Each connector
implements the `ConnectorBase` ABC and registers with the `ConnectorHub`.

## Architecture

```
ConnectorBase (ABC)          ← modulo/connectors/base.py
  ├── FilesystemConnector    ← modulo/connectors/filesystem/
  ├── GitHubConnector        ← modulo/connectors/github/
  ├── GitLabConnector        ← modulo/connectors/gitlab/
  ├── JiraConnector          ← modulo/connectors/jira/
  ├── LinearConnector        ← modulo/connectors/linear/
  ├── SlackConnector         ← modulo/connectors/slack/
  ├── ShellConnector         ← modulo/connectors/shell/
  └── YourConnector          ← your package
```

## ConnectorBase interface

```python
class ConnectorBase(ABC):
    """Abstract base for all connector implementations."""

    @property
    @abstractmethod
    def connector_type(self) -> str:
        """Unique type identifier (e.g. 'git-host', 'issue-tracker')."""

    @property
    @abstractmethod
    def supported_operations(self) -> list[str]:
        """Operations this connector supports (e.g. ['read', 'write'])."""

    @abstractmethod
    async def health_check(self) -> HealthResult:
        """Verify the connector's external service is reachable."""

    @abstractmethod
    async def execute(self, query: ConnectorQuery) -> ConnectorResult:
        """Execute a single operation."""
```

## Capability contract

Each connector type declares its capabilities via `connector_type` and
`supported_operations`. The graph validator checks that pipeline node
requirements are satisfied by the bound connector at save-time and run-time.

**Connector type naming convention:** `kebab-case` identifiers like `git-host`,
`issue-tracker`, `ci-runner`, `shell`.

## Credential handling

Credentials are **never** stored in the connector class itself. The
`ConnectorHub` decrypts credentials once at run-start and passes them to a
run-scoped context object:

```python
class YourConnector(ConnectorBase):
    async def execute(self, query: ConnectorQuery) -> ConnectorResult:
        # Access pre-decrypted credentials from run context
        token = query.context.get_decrypted_credential("api_token")
        ...
```

## Swappable binding

Pipelines bind to connectors by type, not by instance. This means swapping
`GitHubConnector` for `GitLabConnector` requires **zero pipeline changes** —
just rebind to a different connector instance of the same `connector_type`.

## Testing

Unit tests use `unittest.mock` to stub external calls. Integration tests use
the real connector against test fixtures (e.g., local Git repositories for
`FilesystemConnector`).

```python
async def test_your_connector():
    connector = YourConnector(config={"base_url": "https://example.com"})
    result = await connector.execute(ConnectorQuery(
        operation="read",
        params={"path": "/test"},
        context=MockContext(),
    ))
    assert result.status == "ok"
```

## Registration

Connectors auto-register via Python entry points in `pyproject.toml`:

```toml
[project.entry-points."modulo.connectors"]
your_connector = "your_package.connector:YourConnector"
```

Or register manually in the plugin system:

```python
from modulo.core.plugin_registry import get_plugin_registry
registry = get_plugin_registry()
registry.register_connector_type("custom", YourConnector)
```
