# Agent Configuration

## Pipeline Graph Validation Rules

Pipeline graphs are validated at three enforcement layers. Each layer catches different classes of issues.

### Layer 1: Pydantic Model (`PipelineGraphNode`)

Caught at HTTP request deserialisation – blocks the API call with a 422 response.

| Code | Severity | Rule |
|---|---|---|
| `node_type` validation | Error | `node_type` must be one of: `agent`, `manual`, `composite`, `sandbox_agent`, `router`, `hitl`, `join` |
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

Runs on-save (via the REST API's `PATCH /{pipeline_id}/graph` endpoint) and pre-run (when a pipeline is triggered). Errors block execution; warnings are advisory. Codes below reflect the current validator. Several are fail-closed: an invalid or unknown value is a hard error so a declared control can never silently no-op.

**Topology**

| Code | Severity | Rule |
|---|---|---|
| `TOPOLOGY_NO_NODES` | Error | Graph has no nodes |
| `TOPOLOGY_UNKNOWN_SOURCE` | Error | Edge source references a non-existent node |
| `TOPOLOGY_UNKNOWN_TARGET` | Error | Edge target references a non-existent node |
| `TOPOLOGY_CYCLE` | Error | Graph has a cycle or no entry node |
| `TOPOLOGY_NODE_MISSING_ID` | Error | A node is missing its `id` field |
| `TOPOLOGY_NESTING_EXCEEDED` | Error | Graph nesting depth exceeds the maximum allowed depth (3) |
| `TOPOLOGY_UNREACHABLE` | Warning | Node is unreachable from the entry node |

**Graph**

| Code | Severity | Rule |
|---|---|---|
| `GRAPH_DUPLICATE_NODE_ID` | Error | Duplicate node ID found in graph (belt-and-suspenders with Layer 1) |
| `GRAPH_NODE_ID_FORMAT` | Warning | Node ID does not look like a standard UUID format |
| `GRAPH_NO_EDGES` | Warning | Graph has nodes but no edges defined |

**Sandbox agent**

| Code | Severity | Rule |
|---|---|---|
| `SANDBOX_MISSING_COMMAND` | Error | Sandbox agent node has empty `agent_command` |
| `SANDBOX_MISSING_TEMPLATE` | Warning | Sandbox agent node has no `template_id` |
| `SANDBOX_UNKNOWN_TEMPLATE` | Warning | `template_id` is not a known-good template (expected `opencode` or `modulo-opencode`) |
| `SANDBOX_BAD_JINJA_TEMPLATE` | Error | `agent_command` is not Jinja-renderable (llm mode only) |
| `SANDBOX_TIMEOUT_BOUNDS` | Warning | `timeout_seconds` outside recommended 60-604800s range |
| `SANDBOX_TIMEOUT_INVALID` | Warning | `timeout_seconds` is not a valid integer |
| `SANDBOX_TIMEOUT_EXCEEDS_E2B_CAP` | Error | `timeout_seconds` exceeds the E2B 1-hour sandbox cap; use `<= 3300` for provisioning headroom (FAR-511) |
| `SANDBOX_STALL_TIMEOUT_INVALID` | Warning | `stall_timeout_seconds` is not a positive number |
| `SANDBOX_STALL_TIMEOUT_GT_TIMEOUT` | Warning | `stall_timeout_seconds` exceeds `timeout_seconds` |
| `SANDBOX_STDOUT_DELTA_INVALID` | Warning | `stdout_percentage_delta` is outside (0, 1] or not a valid number |
| `SANDBOX_WATCH_GLOBS_INVALID` | Warning | `watch_globs` is not an array of strings |
| `SANDBOX_WATCH_LOG_PATH_INVALID` | Warning | `watch_log_path` is not a string |
| `SANDBOX_WATCH_LOG_PATH_RELATIVE` | Warning | `watch_log_path` is not an absolute path (should start with `/`) |
| `SANDBOX_ENABLE_HEARTBEAT_INVALID` | Warning | `enable_heartbeat` is not a boolean |
| `SANDBOX_CONTEXT_PATH_RELATIVE` | Warning | `context_files` source path is not absolute (should start with `/`) |
| `SANDBOX_RESERVED_ENV_VAR` | Warning | `env_vars` key uses a reserved system prefix |
| `SANDBOX_SCHEMA_INCOMPLETE` | Warning | `output_schema_json` lacks `type` or `$ref` |
| `SANDBOX_EGRESS_POLICY_INVALID` | Error | `egress_policy` is not one of `None` / `default` / `deny_all` / `selected` (FAR-296) |
| `SANDBOX_EGRESS_ALLOWLIST_INVALID` | Error | `egress_allowlist` is malformed; `selected` requires a non-empty allowlist |
| `SANDBOX_EGRESS_SELECTED_METADATA_ONLY` | Warning | `egress_policy='selected'` currently denies all egress; the allowlist is metadata-only until a template-side enforcement point exists |
| `SANDBOX_RESOURCE_LIMITS_INVALID` | Error | `resource_limits` is not an object |
| `SANDBOX_RESOURCE_LIMITS_UNKNOWN_KEY` | Error | `resource_limits` contains unknown keys (fail-closed, never silently dropped) |
| `SANDBOX_READ_ONLY_INVALID` | Error | `read_only` is not a genuine boolean (FAR-212) |
| `SANDBOX_GIT_CREDENTIALS_INVALID` | Error | `git_credentials` scope is not recognised (FAR-212) |
| `SANDBOX_POLICY_FIELD_ON_NON_SANDBOX` | Error | `read_only` / `git_credentials` set on a non-`sandbox_agent` node |
| `SANDBOX_WALLCLOCK_BUDGET_INVALID` | Error | `wallclock_budget_seconds` is not a valid positive budget (FAR-296) |
| `SANDBOX_LOOP_INTERCEPT_MALFORMED` | Error | `loop_intercept` config is not a valid object (FAR-211) |
| `SANDBOX_LOOP_INTERCEPT_EMPTY_PATTERNS` | Warning | `loop_intercept.intercepted_tool_patterns` is empty; no tool calls will be intercepted |
