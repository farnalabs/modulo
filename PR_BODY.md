## fix(FAR-874): harden graph read — preserve node data and stop leaking secrets in validation_issues

### Defects fixed

#### CRITICAL — node fallback silently truncates stored fields (data loss)
**File:** `backend/src/modulo/api/routes/pipelines.py` — `_graph_response()` tier-3 fallback (line ~1370)

**Was:** `model_construct(id=..., node_type=..., position=...)` — every other stored field (`agent_id`, `template_id`, `agent_prompt`, `agent_commands`, `env_vars`, `connector_binding`, `hitl_config`, `router_config`, `workspace_inputs`, `label`, etc.) was dropped silently.

**Fix:** Filter the raw dict to `PipelineGraphNode.model_fields`, coerce only `id` (UUID) and `position` (GraphPosition), and `model_construct(**fallback_fields)`. The identical approach is applied to the edge fallback (preserving `hitl_gate_config` and `condition_expression`).

**Impact:** API consumers no longer receive structurally valid but empty nodes. A read→edit→save round-trip no longer truncates the graph. The read remains total (never 422).

#### MAJOR — validation_issues leaks secrets in error messages
**File:** `backend/src/modulo/api/routes/pipelines.py` — both node and edge validation failure sites

**Was:** `exc.errors(include_url=False)` includes Pydantic's `input` key, which carries the entire node/edge dict (including `env_vars` with API keys, `connector_binding`, `workspace_inputs`) into the HTTP response body and log lines.

**Fix:** Added `_safe_validation_errors()` helper that uses `exc.errors(include_url=False, include_input=False)` (Pydantic 2.13.5 supports this flag). Applied to both node and edge sites, in both the message and the log line. Verified Pydantic version: **2.13.5**.

#### MAJOR — CLI apply validates nodes without read context (parity break)
**File:** `backend/src/modulo/cli/apply/pipeline_apply.py` — `normalize_current_graph()`

**Was:** Edges validated with `context={"legacy_read": True}` but nodes validated with bare `PipelineGraphNode.model_validate(node)` — a legacy node the API read tolerates would raise `ValidationError` in the CLI apply/drift path.

**Fix:** Both nodes and edges now use `context=LEGACY_READ_CONTEXT`. The `LEGACY_READ_CONTEXT` constant is defined once in `pipelines.py` and imported by `pipeline_apply.py`.

### Cheap wins
- **`LEGACY_READ_CONTEXT`** hoisted to a module-level constant in `pipelines.py`, imported by `pipeline_apply.py`. A rename cannot silently break the leniency contract at one site.
- **Comment** added on the edge path explaining why edges only need 2 tiers while nodes need 3 (edges have no per-type validators).
- **`GraphValidationIssue.severity`** bounded to `Literal["error", "warning", "info"]` instead of unbounded `str`.
- **Non-dict node entries** handled gracefully (logged and skipped, never crash).

### Tests added (all in `test_graph_read_tolerance.py`)
| Test | What it proves |
|------|---------------|
| `TestDataLossRegression.test_tier3_node_preserves_distinctive_fields` | Tier-3 fallback preserves `label`, `template_id`, `agent_prompt`, `env_vars` |
| `TestDataLossRegression.test_tier3_endpoint_preserves_fields` | Same through GET /graph endpoint |
| `TestSecretNonLeak.test_env_vars_not_in_validation_issues` | Sentinel not in issue messages (unit) |
| `TestSecretNonLeak.test_env_vars_not_in_endpoint_validation_issues` | Sentinel not in issue messages (endpoint) |
| `TestSecretNonLeak.test_connector_binding_not_in_issue_message` | connector_binding not in issue messages |
| `TestEdgeFallbackPreservesConfig.test_edge_hitl_gate_config_preserved` | Edge fallback keeps `hitl_gate_config` and `condition_expression` |
| `TestTier3Reachability.test_tier3_reached_on_invalid_uuid` | Proves tier-3 is reachable (not just tier-2) |
| `TestTier3Reachability.test_tier2_reached_on_missing_composite_ref` | Sanity: composite missing ref hits tier-2, not tier-3 |
| `TestNonDictNodeEntry.test_string_in_nodes_list` | Non-dict entries don't crash |
| `TestNonDictNodeEntry.test_string_in_nodes_list_mixed_with_valid` | Mixed valid+non-dict works |
| `TestBrokenButRealisticGraph.test_composite_none_ref_with_edge` | Composite with `composite_ref: None` + edge returns 200 with issues |

Each new test fails without the fix (the node fallback truncation test fails because `label`/`template_id` are `None`; the secret test fails because the sentinel appears in the issue message; the edge test fails because `hitl_gate_config` is `None`; the non-dict test crashes with `AttributeError`).

### Verification
- Pydantic version: **2.13.5** — `include_input=False` confirmed to strip both `input` and `ctx` keys
- All 37 graph read tolerance tests pass
- All 75 pipelines routes coverage tests pass
- All CLI tests pass
- Test suite architecture quality scanner passes
- ruff check + ruff format clean
