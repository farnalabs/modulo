# Agent Configuration

## Pipeline Graph Validation Rules

Pipeline graphs are validated at three enforcement layers. Each layer catches different classes of issues.

### Layer 1: Pydantic Model (`PipelineGraphNode`)

Caught at HTTP request deserialisation – blocks the API call with a 422 response.

| Code | Severity | Rule |
|---|---|---|
| `node_type` validation | Error | `node_type` must be one of: `agent`, `manual`, `composite`, `sandbox_agent`, `join` |
| `agent_command` | Error | Sandbox agent nodes require a non-empty `agent_command` |
| `template_id` | Error | Sandbox agent nodes require a `template_id` (e.g. `"opencode"`) |
| `timeout_seconds` | Error | Per-node timeout must be between 60 and 604800 seconds if set |
| `env_vars` reserved keys | Error | Sandbox agent `env_vars` keys must not start with `MODULO_` or `OPENCODE_API_KEY` |
| `context_files` paths | Error | Sandbox agent `context_files` source paths must be absolute (start with `/`) |

### Layer 2: MCP Tool (`update_pipeline_graph`)

Caught before the graph is persisted via the MCP interface. Returns structured errors in the MCP response.

| Code | Severity | Rule |
|---|---|---|
| Duplicate node IDs | Error | Node IDs must be unique across the graph |
| Duplicate edge IDs | Error | Edge IDs must be unique across the graph |
| `agent_command` (MCP) | Error | Sandbox agent nodes require a non-empty `agent_command` |
| `template_id` (MCP) | Error | Sandbox agent nodes require a `template_id` |

### Layer 3: GraphValidator (`core/graph_validator/`)

Runs on-save (via the REST API's `PATCH /{pipeline_id}/graph` endpoint) and pre-run (when a pipeline is triggered). Errors block execution; warnings are advisory.

| Code | Severity | Rule |
|---|---|---|
| `TOPOLOGY_NO_NODES` | Error | Graph has no nodes |
| `TOPOLOGY_UNKNOWN_SOURCE` | Error | Edge source references a non-existent node |
| `TOPOLOGY_UNKNOWN_TARGET` | Error | Edge target references a non-existent node |
| `TOPOLOGY_CYCLE` | Error | Graph has a cycle or no entry node |
| `TOPOLOGY_NESTING_EXCEEDED` | Error | Graph nesting depth exceeds 3 |
| `TOPOLOGY_UNREACHABLE` | Warning | Node is unreachable from the entry node |
| `GRAPH_DUPLICATE_NODE_ID` | Error | Duplicate node ID found in graph |
| `GRAPH_NODE_ID_FORMAT` | Warning | Node ID does not look like a standard UUID format |
| `GRAPH_NO_EDGES` | Warning | Graph has nodes but no edges defined |
| `SANDBOX_MISSING_COMMAND` | Error | Sandbox agent node has empty `agent_command` |
| `SANDBOX_MISSING_TEMPLATE` | Warning | Sandbox agent node has no `template_id` |
| `SANDBOX_TIMEOUT_BOUNDS` | Warning | Sandbox agent `timeout_seconds` outside recommended 60-604800s range |
| `SANDBOX_TIMEOUT_INVALID` | Warning | Sandbox agent `timeout_seconds` is not a valid integer |
| `SANDBOX_CONTEXT_PATH_RELATIVE` | Warning | Sandbox agent `context_files` source path is not absolute (should start with `/`) |
| `SANDBOX_RESERVED_ENV_VAR` | Warning | Sandbox agent `env_var` uses a reserved system prefix |
| `SANDBOX_SCHEMA_INCOMPLETE` | Warning | Sandbox agent `output_schema_json` lacks `type` or `$ref` |
