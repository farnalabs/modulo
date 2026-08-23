# Plugin API

Modulo's plugin system extends the platform with **connector types** and **model backend providers** via standard Python `importlib.metadata.entry_points`. Plugins are third-party Python packages that register builders at startup, enabling new tool integrations and LLM providers without modifying Modulo core.

## Architecture

```
Plugin package          Modulo Core
    │                       │
    ├─ entry_point ─────────┤
    │  modulo.connectors     │  PluginRegistry.discover_plugins()
    │  or                   │       │
    │  modulo.model_backends │       ├─ ConnectorHub (fallback)
    │                       │       └─ ModelBackendHub (fallback)
    │                       │
    ├─ builder function ────┤  Called at runtime with config/creds
    │                       │  to produce a ConnectorBase or
    │                       │  ModelBackendBase instance
```

Plugins are **discovered at startup**: all builder functions are loaded from installed packages and cached in-memory. At runtime, the `ConnectorHub` and `ModelBackendHub` first check their built-in types, then fall back to the plugin registry for any type they do not recognise.

### Entry point groups

| Group | Purpose | Builder signature |
|---|---|---|
| `modulo.connectors` | Third-party connector types | `(config: dict, creds: dict) -> ConnectorBase` |
| `modulo.model_backends` | Third-party model backends | `(api_key: str, model_id: str, **params) -> ModelBackendBase` |

### Future groups (documented, not yet implemented)

| Group | Purpose | Status |
|---|---|---|
| `modulo.evals` | Custom eval functions | v1 |
| `modulo.schema_types` | Custom schema field types | v1 |

## Plugin Registry reference

### `PluginManifest`

Metadata for an installed plugin, populated from the distribution package metadata:

| Field | Type | Source |
|---|---|---|
| `PLUGIN_ID` | `str` | Distribution name (`dist.name`) |
| `display_name` | `str` | Package metadata `Name` |
| `description` | `str` | Package metadata `Summary` |
| `version` | `str` | Package metadata `Version` |
| `capabilities` | `set[str]` | `"connector_type"` or `"model_backend"` |

### `PluginHealth`

Runtime health status for a plugin:

| Field | Type | Description |
|---|---|---|
| `ok` | `bool` | Whether the plugin loaded successfully or is still importable |
| `detail` | `str` | Human-readable detail (error message or status) |
| `checked_at` | `datetime` | Timestamp of the last health check |

### `PluginRegistry`

The central registry. Accessed via the module-level singleton:

```python
from modulo.core.plugin_registry import get_plugin_registry

registry = get_plugin_registry()
```

#### Discovery

```python
def discover_plugins(self) -> list[PluginManifest]
```

Iterates all registered entry point groups (`modulo.connectors`, `modulo.model_backends`), loads each entry point, and stores its builder. Returns a list of discovered manifests. Plugins whose entry points fail to load are still recorded (appear in listings) but marked unhealthy.

#### Builder lookup

```python
def build_connector(self, type_id: str, config: dict, creds: dict) -> ConnectorBase
def build_model_backend(self, provider: str, model_id: str, api_key: str, **params) -> ModelBackendBase
```

Look up a registered builder by type ID or provider name and call it with the given arguments. Raises `PluginNotFoundError` (a `KeyError` subclass) if no builder is registered for the given key.

#### Manual registration (in-tree)

```python
def register_connector_type(self, type_id: str, builder: Callable, manifest: PluginManifest) -> None
def register_model_backend(self, provider: str, builder: Callable, manifest: PluginManifest) -> None
```

Register a builder directly without going through entry point discovery. Used for first-party (in-tree) registrations. Sets health to `"Registered in-tree"`.

#### Queries

```python
def list_plugins(self) -> dict[str, PluginManifest]
def get_plugin(self, plugin_id: str) -> PluginManifest | None
def has_connector_type(self, type_id: str) -> bool
def has_model_backend(self, provider: str) -> bool
@property
def connector_types(self) -> frozenset[str]
@property
def backend_providers(self) -> frozenset[str]
```

#### Health checks

```python
def health_check(self, plugin_id: str | None = None) -> dict[str, PluginHealth]
```

Verifies that the plugin's distribution package is still importable via `importlib.metadata.metadata(PLUGIN_ID)`. If the package has been uninstalled since discovery, returns `ok=False`. Returns health for all plugins when called without arguments, or for a specific plugin when called with a plugin ID.

### Singleton

```python
from modulo.core.plugin_registry import get_plugin_registry
```

The module-level `get_plugin_registry()` returns a lazily-initialised singleton. All consumers (the API layer, `ConnectorHub`, `ModelBackendHub`) share the same instance.

### REST API

Two authenticated endpoints expose plugin information:

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/v1/plugins` | List all discovered plugins with health status |
| `GET` | `/api/v1/plugins/{plugin_id}/health` | Health check for a single plugin |

Both require `plugin.list` permission (returning a `TenantPrincipal`) and the `plugin_management` feature flag. Plugin management (install, upgrade, remove) is not handled through this API; see [Installation](#installation).

## How plugins interact with Modulo

### ConnectorHub fallback

The connector factory `_build_connector()` first matches `type_id` against all built-in connector types (GitHub, GitLab, Linear, Jira, Slack, Filesystem, Shell, CI runners). If no built-in matches, it falls through to the plugin registry:

```python
# pseudocode from connector_hub/__init__.py
case _:
    registry = get_plugin_registry()
    if registry.has_connector_type(type_id):
        return registry.build_connector(type_id, config, creds)
    raise ValueError(f"Unknown connector type: {type_id!r}")
```

### ModelBackendHub fallback

Identical pattern: match the built-in providers (OpenAI, Anthropic, DeepSeek, Grok, Ollama, and others), then fall through to the plugin registry:

```python
# pseudocode from model_backend_hub/__init__.py
case _:
    registry = get_plugin_registry()
    if registry.has_model_backend(provider):
        api_key = creds.get("api_key")
        if not api_key:
            raise ValueError(...)
        return registry.build_model_backend(provider, model_id, api_key, **default_params)
    raise ValueError(f"Unknown model backend provider: {provider!r}")
```

### Pipeline execution

When a run is triggered, graph validation confirms bound connector instances exist and are active (`CONNECTOR_*` codes). The connector or model backend *type* is resolved only when it is built at run time. If a plugin-provided type has been uninstalled since discovery, the run fails at build time with `ValueError("Unknown connector type: …")` / `ValueError("Unknown model backend provider: …")`.

## Creating a plugin

### Step 1: Implement the connector or backend

Create a Python package that implements either `ConnectorBase` or `ModelBackendBase`:

```python
# my_plugin/connector.py
from modulo.connectors.base import (
    ConnectorBase,
    ConnectorPayload,
    ConnectorQuery,
    ConnectorResult,
    ConnectorType,
    HealthResult,
)


class MyConnector(ConnectorBase):
    @property
    def connector_type(self) -> ConnectorType:
        return ConnectorType.GITHUB  # reuse a built-in type, or register a custom one

    async def health_check(self) -> HealthResult:
        # verify your external service is reachable
        return HealthResult(ok=True)

    async def query(self, q: ConnectorQuery) -> ConnectorResult:
        # read data, keyed on q.resource with q.filters
        ...

    async def write(self, payload: ConnectorPayload) -> dict[str, Any]:
        # write data; return the created/updated resource
        ...
```

### Step 2: Write a builder function

The entry point must point to a **builder function**, not a class directly. The builder receives configuration and credentials and returns an instance:

```python
# my_plugin/__init__.py
from my_plugin.connector import MyConnector


def build_my_connector(config: dict, creds: dict) -> MyConnector:
    return MyConnector(api_key=creds["api_key"])
```

### Step 3: Register the entry point

Add to your `pyproject.toml`:

```toml
[project.entry-points."modulo.connectors"]
my_connector = "my_plugin:build_my_connector"
```

For model backends:

```toml
[project.entry-points."modulo.model_backends"]
my_provider = "my_plugin:build_my_backend"
```

### Step 4: Install

Add your package to the deployment's dependencies (see [Installation](#installation)).

## Example: Slack notifier plugin

A complete plugin that adds a "Slack notify" connector type:

```python
"""modulo-connector-slack – Slack notification connector for Modulo."""

from modulo.connectors.base import (
    ConnectorBase,
    ConnectorPayload,
    ConnectorResult,
    ConnectorType,
    HealthResult,
)


class SlackNotifier(ConnectorBase):  # custom type, registered by the plugin
    @property
    def connector_type(self) -> ConnectorType:
        return ConnectorType.SLACK

    def __init__(self, bot_token: str, default_channel: str = "#general"):
        self._bot_token = bot_token
        self._default_channel = default_channel

    async def health_check(self) -> HealthResult:
        # basic reachability check
        return HealthResult(ok=True)

    async def query(self, q: ConnectorQuery) -> ConnectorResult:
        # reads are keyed on q.resource + q.filters
        ...

    async def write(self, payload: ConnectorPayload) -> dict[str, Any]:
        # posts a message to payload.resource using payload.data
        channel = payload.data.get("channel", self._default_channel)
        message = payload.data["message"]
        # send message via Slack Bot API (uses bot_token from creds)
        ...
        return {"channel": channel, "sent": True}


def build_slack_connector(config: dict, creds: dict) -> SlackNotifier:
    return SlackNotifier(
        bot_token=creds["bot_token"],
        default_channel=config.get("default_channel", "#general"),
    )
```

**`pyproject.toml`:**

```toml
[build-system]
requires = ["setuptools"]
build-backend = "setuptools.backends._legacy:_Backend"

[project]
name = "modulo-connector-slack"
version = "0.1.0"
description = "Slack notification connector for Modulo"

[project.entry-points."modulo.connectors"]
slack_notify = "modulo_connector_slack:build_slack_connector"
```

## Installation

### Build-time only

Modulo uses **build-time plugin installation** through v2. Plugins are added to the deployment's `pyproject.toml` or `requirements.txt` and the container (or Python environment) is rebuilt. Runtime `pip install` is explicitly disallowed; it is a supply-chain security risk and incompatible with read-only Docker containers.

**Self-hosted deployments:**

```bash
# Add to requirements.txt or pyproject.toml
echo "modulo-connector-slack==0.1.0" >> requirements.txt

# Rebuild the Python environment
uv pip install -r requirements.txt

# Restart the Modulo server
```

**Docker deployments:**

```dockerfile
# In your Dockerfile
FROM modulo:latest
RUN pip install modulo-connector-slack==0.1.0
```

### Environment variables

| Variable | Default | Description |
|---|---|---|
| `MODULO_PLUGIN_DISCOVERY` | `true` | Enable automatic plugin discovery at startup. Set to `false` to disable. |

### What happens at startup

1. On application startup, `PluginRegistry.discover_plugins()` is called if `MODULO_PLUGIN_DISCOVERY` is `true`.
2. The registry iterates both entry point groups (`modulo.connectors`, `modulo.model_backends`).
3. For each entry point, it reads distribution metadata and loads the builder function.
4. Successful loads are stored keyed by entry point name; failed loads are recorded as unhealthy.
5. The singleton is shared across `ConnectorHub`, `ModelBackendHub`, and the REST API.

### What happens if a plugin is uninstalled

If a plugin package is removed from the environment between restarts:

- `ConnectorInstances` referencing the missing type still exist in the database.
- When the connector is built at run time, the build fails with `ValueError("Unknown connector type: …")`, so the run is blocked.
- Existing completed runs are unaffected (they execute against immutable snapshots).
- The Plugins admin view shows each plugin's package health as Active/Inactive.
- Resolution: reinstall the package or migrate affected instances to a different type.

### SaaS deployments (v3+)

A dedicated plugin volume approach will be required for SaaS (hosted environments cannot rebuild the container per-org). Design deferred to v3. The entry-point API is designed to support both build-time and volume-based approaches without code changes in the plugin itself.

## Testing

Unit tests use mocked entry points via `unittest.mock.patch`:

```python
from modulo.core.plugin_registry import PluginRegistry, PluginManifest


def test_manual_registration():
    registry = PluginRegistry()
    manifest = PluginManifest(
        PLUGIN_ID="test-plugin",
        display_name="Test Plugin",
        description="",
        version="0.1.0",
    )
    registry.register_connector_type("test-type", lambda c, cr: StubConnector(), manifest)
    assert registry.has_connector_type("test-type")
```

See `backend/tests/unit/plugin_registry/test_plugin_registry.py` for the full test suite.
