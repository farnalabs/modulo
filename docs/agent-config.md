# Agent Configuration

## Schema profile configuration

An operator can set a `schema_profile` on an agent or on an individual pipeline
node to control how the node's output schema is translated before dispatch to
the model backend.

### Valid values

| Value | Effect |
|-------|--------|
| `verbatim` | Identity (no translation). This is the default when no profile is set. |
| `provider-strict` | Strips JSON Schema keywords the target provider does not support. See [Schema Reference](./schema-reference.md#schema-profiles) for per-provider keyword sets. |
| `runtime-sdk` | Renders for the target runtime. No keyword stripping, but `$ref` inlining still applies; reserved for future transforms. |

### Setting on an agent

Set `schema_profile` when creating or updating an agent via the REST API or
MCP:

```json
POST /api/v1/agents
{
  "name": "my-agent",
  "prompt_template": "...",
  "schema_profile": "provider-strict"
}
```

The agent-level value becomes the default for all nodes that use this agent.
A `NULL` value (or omitting the field) defaults to `verbatim`.

### Setting on a node

Set `schema_profile` on an individual node in the pipeline graph:

```json
PUT /api/v1/pipelines/{pipeline_id}/graph
{
  "nodes": [
    {
      "id": "node-1",
      "node_type": "sandbox_agent",
      "agent_id": "...",
      "schema_profile": "provider-strict",
      "output_schema_json": { "type": "object", ... }
    }
  ],
  "edges": [...]
}
```

### Resolution order

The effective profile for a node is resolved in this order:

1. **Node-level** `schema_profile` -- set directly on the node in the
   pipeline graph. This always wins.
2. **Agent-level** default -- `Agent.schema_profile`, embedded into the
   pipeline snapshot at trigger time. The node-level value (if set) is
   already present in the snapshot via `setdefault` and is never overwritten.
3. **Absent on both** -- the caller treats it as `verbatim` (identity,
   no translation).

This means an operator can set a broad default on an agent (e.g. all
OpenAI-backed agents use `provider-strict`) and override specific nodes
that need a different profile.

### Design-time preview

Requesting `GET /api/v1/pipelines/{pipeline_id}/graph?include_schema_warnings=true`
returns a graph response whose `schema_translation_report` lists which keywords
would be stripped for each node's target provider, so operators can preview the
impact before running a pipeline. The flag is read-only: it is a query
parameter on the graph GET only, and the save (`PATCH .../graph`) response
never populates a report - re-GET the graph with the flag after saving.

### Migration safety

The `schema_profile` field is optional and defaults to `verbatim`. Existing
pipelines and agents are unaffected; no graph migration is required. The
CHECK constraint on `agents.schema_profile` allows `NULL` or one of
`verbatim`, `provider-strict`, `runtime-sdk`.

## Pipeline Graph Validation Rules

Pipeline graphs are validated at three enforcement layers. Each layer catches different classes of issues.

### Layer 1: Pydantic Model (`PipelineGraphNode`)

Caught at HTTP request deserialisation – blocks the API call with a 422 response.

| Code | Severity | Rule |
|---|---|---|
| `node_type` validation | Error | `node_type` must be one of: `agent`, `manual`, `composite`, `sandbox_agent`, `router`, `hitl`, `join`, `dispatch`, `connector` |
| `agent_commands` / `agent_prompt` / `script_command` | Error | Sandbox agent nodes are `mode`-driven (`mode` is `"llm"` or `"script"`). In `script` mode a non-empty `script_command` is required; in the default `llm` mode both a non-empty `agent_commands` list and a non-empty `agent_prompt` are required. Setting `agent_commands` and `script_command` together is rejected as mutually exclusive. Note the graph node field is `agent_commands` (a list, joined at run time); `agent_command` is not a node field |
| `template_id` | Error | Sandbox agent nodes require a `template_id` (e.g. `"opencode"`) |
| `timeout_seconds` | Error | Per-node timeout must be between 60 and 604800 seconds if set |
| `env_vars` reserved keys | Error | Sandbox agent `env_vars` keys must not start with `MODULO_` or `OPENCODE_API_KEY` |
| `context_files` paths | Error | Sandbox agent `context_files` source paths must be absolute (start with `/`) |
| `connector_binding` (dispatch) | Error | Dispatch nodes (`node_type: dispatch`, FAR-1141) require a `connector_binding` (the CI-runner dispatch target) and cannot reference an `agent_id` |
| `operation` (dispatch) | Error | A dispatch node's `connector_binding.operation` must be `dispatch` (omitting it inherits the engine fallback; an explicit `query`/`write` is rejected). The binding `type` must implement the CI-runner operations (`trigger_run` / `get_run_status` / `get_run_logs` / `list_runs`); `dispatch_action` selects one (default `trigger_run`) |
| `await_completion` / `wait_timeout` | Error | Dispatch-only fields: `await_completion=True` and any `wait_timeout` on a non-`dispatch` node are rejected. `await_completion=True` additionally requires `dispatch_action='trigger_run'`; `wait_timeout` (> 0, <= 3600) requires `await_completion=True` |
| `connector_binding` (connector) | Error | Connector nodes (`node_type: connector`, FAR-2089) are a query/write node whose whole job is its `connector_binding`: a missing binding is rejected (without one the engine would fall through to the LLM node factory and run the node as an agent), and an `agent_id` is rejected (the node has no LLM step, so the reference would be dead configuration) |
| `resource` (connector) | Error | A `connector` node whose binding operation is not `dispatch` requires a non-empty `connector_binding.resource`; without one the connector is called with an empty resource and fails at run time |
| sandbox-only fields | Error | `agent_commands`, `commands_concatenation_string` (any value other than the default `" && "`), `model_class`, `read_only`, `git_credentials`, `single_pr_per_run`, `egress_policy` and `egress_allowlist` are sandbox-only: setting them on any other node type is rejected rather than stored as a declared-but-unread field (FAR-212 / FAR-1273 / FAR-1743 / FAR-1846) |
| graph size | Error | A graph may hold at most 500 nodes and 1000 edges |
| duplicate IDs / edge paths | Error | Node IDs must be unique, edge IDs must be unique, and two edges may not share the same `(source_node_id, target_node_id, edge_type)` path |

### Layer 2: MCP Tool (`update_pipeline_graph`)

Caught before the graph is persisted via the MCP interface. Returns structured errors in the MCP response. The tool re-validates the submitted payload against the Layer 1 Pydantic contract (`PipelineGraphUpdate`), so every Layer 1 rule applies here too, then adds the checks below that a raw-dict write could otherwise skip.

| Code | Severity | Rule |
|---|---|---|
| `agent_commands` / `agent_prompt` / `script_command` (MCP) | Error | The shared mode-aware sandbox gate over the raw node dicts: `llm` mode requires `agent_commands` plus `agent_prompt`, `script` mode requires `script_command`, the two are mutually exclusive, the joined command must be Jinja-renderable, and the managed-inputs config must be valid |
| `HITL_REVIEW_DESCRIPTION_REQUIRED` (MCP) | Error | A node-level `hitl_config` or edge-level `hitl_review_config` has no human-authored description of at least 20 characters. Node-level gate config is an unvalidated dict on this surface, so the check is applied explicitly (FAR-613) |
| `connector_type` (MCP, dispatch) | Error | The bound instance's own `connector_type_id` must implement the CI-runner operations (`trigger_run` / `get_run_status` / `get_run_logs` / `list_runs`). The Layer 1 contract only inspects the caller-declared `connector_binding.type`, so the MCP write resolves the instance row too and fails closed on a type it cannot prove dispatch-capable (FAR-1141) |

### Layer 3: GraphValidator (`core/graph_validator/`)

Runs on-save (via the REST API's `PUT /{pipeline_id}/graph` endpoint) and pre-run (when a pipeline is triggered). Errors block execution; warnings are advisory. Codes below reflect the current validator. Several are fail-closed: an invalid or unknown value is a hard error so a declared control can never silently no-op.

**Topology**

| Code | Severity | Rule |
|---|---|---|
| `TOPOLOGY_NO_NODES` | Error | Graph has no nodes |
| `TOPOLOGY_UNKNOWN_SOURCE` | Error | Edge source references a non-existent node |
| `TOPOLOGY_UNKNOWN_TARGET` | Error | Edge target references a non-existent node |
| `TOPOLOGY_CYCLE` | Error | Graph has a cycle or no entry node |
| `TOPOLOGY_NODE_MISSING_ID` | Error | A node is missing its `id` field |
| `TOPOLOGY_NODE_RESERVED_ID_PREFIX` | Error | Node id uses the reserved `hitl_review_` prefix, which is reserved for synthesized HITL gate nodes |
| `TOPOLOGY_NODE_DB_SENTINEL_PREFIX` | Error | Node id uses the reserved `__` prefix, the `run_node_outputs` sentinel namespace (`__run_meta__` / `__unknown__` / `__final__`) |
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
| `SANDBOX_MISSING_COMMAND` | Error | Sandbox agent node is missing a required command or prompt (`agent_commands` and `agent_prompt` in `llm` mode, `script_command` in `script` mode) |
| `SANDBOX_MISSING_TEMPLATE` | Warning | Sandbox agent node has no `template_id` |
| `SANDBOX_UNKNOWN_TEMPLATE` | Warning | `template_id` is not a known-good template (expected `opencode` or `modulo-opencode`) |
| `SANDBOX_BAD_JINJA_TEMPLATE` | Error | the joined command (built from `agent_commands`) is not Jinja-renderable (llm mode only) |
| `SANDBOX_TIMEOUT_BOUNDS` | Warning | `timeout_seconds` outside recommended 60-604800s range |
| `SANDBOX_TIMEOUT_INVALID` | Warning | `timeout_seconds` is not a valid integer |
| `SANDBOX_TIMEOUT_EXCEEDS_E2B_CAP` | Error | `timeout_seconds` exceeds the E2B 1-hour sandbox cap; use `<= 3300` for provisioning headroom (FAR-511) |
| `SANDBOX_TIMEOUT_EXCEEDS_PROFILE_CAP` | Error | `timeout_seconds` exceeds the bound `EnvironmentProfile`'s `max_node_seconds` capability (capped at the run transport ceiling). Only raised when the snapshot binds an environment profile |
| `SANDBOX_HEREDOC_TERMINATOR_IN_LIST_ITEM` | Error | an `agent_commands` list item ends with a heredoc terminator; list items are joined with the concatenation operator and the terminator would be corrupted – use a single `agent_command` or base64-embed the script body (FAR-664) |
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
| `SANDBOX_POLICY_FIELD_ON_NON_SANDBOX` | Error | `read_only` / `git_credentials` / `single_pr_per_run` / `egress_policy` / `egress_allowlist` set on a non-`sandbox_agent` node |
| `SANDBOX_WALLCLOCK_BUDGET_INVALID` | Error | `wallclock_budget_seconds` is not a valid positive budget (FAR-296) |
| `SANDBOX_LOOP_INTERCEPT_MALFORMED` | Error | `loop_intercept` config is not a valid object (FAR-211) |
| `SANDBOX_LOOP_INTERCEPT_EMPTY_PATTERNS` | Warning | `loop_intercept.intercepted_tool_patterns` is empty; no tool calls will be intercepted |
| `SANDBOX_SINGLE_PR_PER_RUN_INVALID` | Error | `single_pr_per_run` is not a boolean (true = arm the one-PR-per-run guard for this sandbox run) |
| `SANDBOX_MANAGED_INPUTS_INVALID` | Error | The sandbox node's managed workspace inputs config is invalid |
| `SANDBOX_MODEL_VAR_REQUIRES_MODEL_CLASS` | Error | The `{{ model }}` template variable has no resolution source (the node's `model_class`, or registry pipeline-map / `pipelines_default` coverage for this pipeline); without one the reference would render empty and dispatch the sandbox with a blank model (FAR-1743 / ADR 037) |

**Dispatch / connector binding (FAR-1141)**

| Code | Severity | Rule |
|---|---|---|
| `CONNECTOR_NOT_FOUND` | Error | A connector binding references a `connector_instance_id` that does not resolve to an existing connector instance |
| `CONNECTOR_INACTIVE` | Error | A connector binding targets an instance whose status is not `active` |
| `CONNECTOR_MISSING_OPERATIONS` | Error | A connector binding targets an instance whose allowed-operations ACL does not permit an operation the node's routed verb requires |
| `CONNECTOR_DISPATCH_UNSUPPORTED` | Error | A dispatch binding targets a connector instance whose type does not implement the CI-runner operations (`trigger_run` / `get_run_status` / `get_run_logs` / `list_runs`) |

**Model backend and environment bindings**

| Code | Severity | Rule |
|---|---|---|
| `MODEL_BACKEND_NOT_FOUND` | Error | A pinned `model_backend_id` does not resolve to an existing model backend |
| `MODEL_BACKEND_INACTIVE` | Error | A pinned model backend's `status` is not `active` |
| `MODEL_BACKEND_UNHEALTHY` | Error | A pinned model backend has a recorded `last_health_check_error` |
| `ENV_PROFILE_NOT_FOUND` | Error | The snapshot's `environment_profile_id` does not resolve to an existing `EnvironmentProfile` |
| `ENV_MISSING_CAPABILITIES` | Error | An agent requires capabilities the bound `EnvironmentProfile` does not declare |
| `ENV_PROFILE_WALL_CLOCK_EXCEEDS_RUN_CEILING` | Error | The bound `EnvironmentProfile`'s `max_node_seconds` (default 3300) exceeds the run transport ceiling (`SAQ_RUN_TIMEOUT`, default 7200s), so a node allowed by the profile could outlive the run |

**Schema compatibility and run input**

| Code | Severity | Rule |
|---|---|---|
| `SCHEMA_INCOMPATIBLE` | Error | An edge's source output-schema id differs from the target's input-schema id (reject, kickback and loop edges are exempt) |
| `SCHEMA_FIELD_INCOMPATIBLE` | Error | An output-schema field is missing from the target input schema or has an incompatible type |
| `SCHEMA_CHECK_DEFERRED` | Warning | The edge's schema uses deferred keywords (`$ref`, `oneOf`, ...), so only the identity check ran |
| `INPUT_SCHEMA_MISMATCH` | Error | The run's `input_payload` does not validate against the entry node's input schema (pre-run only) |
| `INPUT_NULL_PAYLOAD` | Error | `input_payload` is `None` (pre-run only) |
| `SCHEMA_PRE_EXISTING_BLOCKED` | Error | A pre-Phase-1 snapshot failed schema or input compatibility; the run is blocked rather than exempted (pre-run only) |

**Conditional and loop edges**

| Code | Severity | Rule |
|---|---|---|
| `CONDITION_MISSING_EXPRESSION` | Error | A conditional edge must carry a non-empty `condition_expression` |
| `CONDITION_INVALID_EXPRESSION` | Error | `condition_expression` is not valid JMESPath |
| `LOOP_MISSING_DEFAULT_TARGET` | Error | A `loop` edge must declare a `default_target` |
| `LOOP_DEFAULT_TARGET_NOT_FOUND` | Error | The loop edge's `default_target` is not an existing node id |
| `LOOP_INVALID_MAX_ITERATIONS` | Error | `max_iterations` is not a non-negative integer |
| `LOOP_INVALID_EXPRESSION` | Error | A loop edge's `condition_expression` is not valid JMESPath |

**LLM routing nodes (`routing_mode: "llm"`)**

| Code | Severity | Rule |
|---|---|---|
| `LLM_ROUTING_MISSING_PROMPT` | Error | The node has no non-empty `routing_prompt` |
| `LLM_ROUTING_MISSING_LABEL` | Error | An outgoing non-`reject` edge has no `routing_label` |
| `LLM_ROUTING_DUPLICATE_LABEL` | Error | Two outgoing edges share the same `routing_label` |
| `LLM_ROUTING_MISSING_DEFAULT` | Error | The node has no `default_target` |
| `LLM_ROUTING_DEFAULT_NOT_FOUND` | Error | The node's `default_target` is not an existing node id |

**HITL gate configuration**

These checks cover both edge-level `hitl_review_config` and node-level `hitl_config`. Most are save-time only: the description, `subject_path`, `response_contract` and `on_reject` checks run when the graph is saved and do not re-run when a pipeline is triggered. Only the `HITL_EVAL_CONDITION_*` checks also run pre-run, as part of the topology pass.

| Code | Severity | Rule |
|---|---|---|
| `HITL_REVIEW_DESCRIPTION_REQUIRED` | Error | The gate has no human-authored description of at least 20 characters explaining why it exists |
| `HITL_SUBJECT_PATH_TOO_LONG` | Error | `subject_path` exceeds 500 characters |
| `HITL_SUBJECT_PATH_INVALID_JMESPATH` | Error | `subject_path` is not valid JMESPath |
| `HITL_EVAL_CONDITION_MISSING_NAME` | Error | `eval_condition` has no non-empty `eval_name` |
| `HITL_EVAL_CONDITION_INVALID_THRESHOLD` | Error | `eval_condition.threshold` is not a number |
| `HITL_EVAL_CONDITION_THRESHOLD_RANGE` | Error | `eval_condition.threshold` is outside 0.0 to 1.0 |
| `HITL_EVAL_CONDITION_INVALID_OPERATOR` | Error | `eval_condition.operator` is not one of `lt`, `gt`, `lte`, `gte`, `eq`, `neq` |
| `HITL_RESPONSE_CONTRACT_INVALID` | Error | `response_contract` is not an object |
| `HITL_RESPONSE_CONTRACT_UNKNOWN_KIND` | Error | `response_contract.kind` is not `approval` or `choice` |
| `HITL_RESPONSE_CONTRACT_CHOICE_REQUIRES_OPTIONS` | Error | A `choice` contract has no non-empty `options` list |
| `HITL_RESPONSE_CONTRACT_INVALID_OPTION` | Error | An option entry is not an object |
| `HITL_RESPONSE_CONTRACT_OPTION_MISSING_ID` | Error | An option has no non-empty string `id` |
| `HITL_RESPONSE_CONTRACT_OPTION_MISSING_LABEL` | Error | An option has no non-empty string `label` |
| `HITL_RESPONSE_CONTRACT_DUPLICATE_OPTION_ID` | Error | Two options share the same `id` |
| `HITL_ON_REJECT_INVALID` | Error | `on_reject` is not `terminate` or `proceed` |

**Parameter schemas and sets**

| Code | Severity | Rule |
|---|---|---|
| `PARAMETER_SCHEMA_INVALID_ID` | Error | The node's `parameter_schema_id` is not a UUID |
| `PARAMETER_SCHEMA_NOT_FOUND` | Error | The node's `ParameterSchema` does not exist |
| `PARAMETER_SET_INVALID_ID` | Error | The node's `parameter_set_id` is not a UUID |
| `PARAMETER_SET_NOT_FOUND` | Error | The node's `ParameterSet` does not exist or belongs to another org |
| `PARAMETER_SET_SCHEMA_MISMATCH` | Error | The `ParameterSet` belongs to a different `ParameterSchema` than the node declares |
| `PARAMETER_SCHEMA_DRIFT` | Warning | The schema has a newer version than the one the `ParameterSet` was created against |
| `PARAMETER_SCHEMA_DRIFT_COMPOSITE` | Warning | The schema declares parameters absent from the set; their defaults are used |

**Composite template nodes**

| Code | Severity | Rule |
|---|---|---|
| `COMPOSITE_INVALID_REF` | Error | `composite_ref` is not a UUID |
| `COMPOSITE_TEMPLATE_NOT_FOUND` | Error | The referenced `CompositeTemplate` does not exist |
| `COMPOSITE_MISSING_PARAMETER` | Error | A required template parameter has no value |
| `COMPOSITE_SUBGRAPH_EMPTY` | Error | The template has no sub-pipeline nodes |
| `COMPOSITE_SUBGRAPH_INVALID_NODE` | Error | A sub-node is not an object |
| `COMPOSITE_SUBGRAPH_DUPLICATE_NODE_ID` | Error | Two sub-nodes share an id |
| `COMPOSITE_SUBGRAPH_INVALID_TYPE` | Error | A sub-node has an invalid `node_type` |
| `COMPOSITE_SUBGRAPH_SANDBOX_MISSING_COMMAND` | Error | A sandbox sub-node has no `agent_commands` |
| `COMPOSITE_SUBGRAPH_SANDBOX_MISSING_TEMPLATE` | Error | A sandbox sub-node has no `template_id` |
| `COMPOSITE_SUBGRAPH_INVALID_EDGE` | Error | A sub-edge is not an object |
| `COMPOSITE_SUBGRAPH_EDGE_BAD_SOURCE` | Error | A sub-edge references an unknown sub-node as its source |
| `COMPOSITE_SUBGRAPH_EDGE_BAD_TARGET` | Error | A sub-edge references an unknown sub-node as its target |
| `COMPOSITE_SUBGRAPH_GATE_UNSUPPORTED` | Error | A sub-edge carries HITL gate config, which composite sub-pipelines do not support |
| `COMPOSITE_SUBGRAPH_MASKED_CREDENTIAL` | Error | A credential-bearing node field still holds the read-side mask sentinel instead of a real value |
| `COMPOSITE_VALIDATION_INVALID_TYPE` | Error | An output validation eval's `type` is not a supported value |
| `COMPOSITE_VALIDATION_INVALID_BEHAVIOUR` | Error | An output validation eval's `failure_behaviour` is not a supported value |
| `COMPOSITE_VALIDATION_RETRIES_RANGE` | Error | `max_validation_retries` is not an integer between 0 and 5 |
| `COMPOSITE_VALIDATION_REGEX_NO_FIELD` | Error | A `regex` eval has no `config.field` |
| `COMPOSITE_VALIDATION_REGEX_NO_PATTERN` | Error | A `regex` eval has no `config.pattern` |
| `COMPOSITE_VALIDATION_REGEX_INVALID` | Error | A `regex` eval's pattern does not compile |
| `COMPOSITE_VALIDATION_REGEX_INVALID_TYPE` | Error | A `regex` eval's pattern is not a string |
| `COMPOSITE_VALIDATION_SCHEMA_MISSING` | Error | A `json_schema` eval has neither `config.schema` nor `config.schema_ref` |

**Guardrails**

| Code | Severity | Rule |
|---|---|---|
| `GUARDRAIL_CAP_EXCEEDED` | Error | A node's effective guardrail count (org-level plus node-bound) exceeds the org's per-node cap |
| `REDACT_CORRECT_BLOCKED` | Error | A `redact`-action guardrail declares a `correction` block, which would re-emit the data redaction protects |

**Retry policy, idempotency and send budgets**

| Code | Severity | Rule |
|---|---|---|
| `RETRY_POLICY_MALFORMED` | Error | `retry_policy` is not an object of the form `{"on": [...], "max_retries": 0-5}` |
| `RETRY_POLICY_SCHEDULE_MALFORMED` | Error | `retry_policy.backoff_schedule` is not `{"delay_seconds": 1-300, "multiplier": 1.0-10.0}` |
| `NODE_IDEMPOTENT_INVALID` | Error | `idempotent` is not a boolean |
| `NODE_SEND_BUDGET_OVERSUBSCRIBED` | Warning | A fan-out binding's `max_cardinality` x `per_item_timeout` x attempts exceeds the node's `timeout_seconds`, so retries would be cancelled mid-send |
| `PARALLEL_RUN_CONTEXT_WRITE` | Warning | A source fans out to several branches that write `run_context`; writes are last-write-wins and order-dependent |

**Node categories**

| Code | Severity | Rule |
|---|---|---|
| `CATEGORY_INVALID_ID` | Error | A node's `category_id` is not a UUID |
| `CATEGORY_NOT_FOUND` | Error | A node references a category that does not exist |

**Git content references**

Stored graphs must pin git-sourced content to a commit SHA (`git+<repo>@<40-hex-sha>#<path>`); `modulo apply` resolves and pins movable refs before saving.

| Code | Severity | Rule |
|---|---|---|
| `GIT_CONTENT_REF_INVALID` | Error | A git content ref does not parse |
| `GIT_CONTENT_REF_UNPINNED` | Error | A git content ref uses a movable ref (branch, tag) instead of a 40-hex commit SHA |
| `GIT_CONTENT_REF_NOT_WHOLE_FIELD` | Error | A git content ref appears as one item of a multi-item command field; it must be the whole field |
