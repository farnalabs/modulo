# Sensitive Data Handling in Agent Outputs

This document describes what constitutes sensitive data in Modulo agent outputs,
how the platform protects it, and best practices for agent authors.

---

## What Constitutes Sensitive Data

Sensitive data includes any value that, if exposed, could compromise security,
privacy, or access control. In Modulo, sensitive data falls into these categories:

| Category | Examples | Typical keys |
|---|---|---|
| **API credentials** | API keys, tokens, secrets | `api_key`, `token`, `secret`, `credential` |
| **Authentication secrets** | Passwords, auth tokens, bearer tokens | `password`, `passwd`, `auth_token`, `bearer_token` |
| **Encryption material** | Encryption keys, session keys | `encryption_key`, `session_key`, `fernet_key` |
| **Service identifiers** | Access keys, client secrets | `access_key`, `secret_key`, `client_secret`, `private_key` |
| **OAuth material** | Refresh tokens, client IDs | `refresh_token`, `client_id` |

### Key pattern detection

Modulo detects sensitive fields by matching key names against known patterns.
The detection is **case-insensitive** and normalises dashes and spaces to
underscores before matching. Any key whose lowercased, normalised name
**contains** any of these substrings is treated as sensitive:

- `token`
- `secret`
- `api_key`
- `password`
- `passwd`
- `key`
- `credential`
- `database_url`
- `encryption`
- `signing`
- `private`

This substring-based approach means `auth_token`, `bearer_token`,
`webhook_secret`, and `session_key` are all automatically caught without
being listed explicitly.

---

## How Modulo Handles Sensitive Data

### 1. API response masking

All API responses that could contain sensitive values (connector configs,
SSO provider details, observability settings, agent outputs) mask those
values as `••••••` (six bullet characters, Unicode `U+2022`).

Masking is applied at these points:

| Endpoint / tool | Mechanism | Coverage |
|---|---|---|
| `GET /api/v1/connectors/{id}` | `mask_config_json()` on `config_json` | Top-level sensitive keys |
| `GET /api/v1/triggers/{id}` | `mask_config_json()` on `config_json` | Top-level sensitive keys |
| `GET /api/v1/settings/observability` | `_mask_headers()` on OTLP headers | Known header keys |
| `GET /api/v1/runs/{run_id}/nodes/{node_id}/output` | `_mask_output_value()` recursive traversal | Nested sensitive keys, depth-limited to 20 |
| `GET /api/v1/runs/{run_id}/io` | `_mask_output_value()` on `outputs_json` | Nested sensitive keys |
| `GET /api/v1/runs/{run_id}/export-fixture` | `_mask_output_value()` on `outputs_json` and `input_payload`; `mask_pipeline_graph_node()` on each node of `snapshot_graph_json` (via the shared snapshot-graph helper) | Nested sensitive keys; snapshot graph node credentials masked as on the graph read |
| `POST /api/v1/runs/diff` | `_mask_output_value()` on both sides | Nested sensitive keys |
| `POST /api/v1/runs/{run_id}/nodes/{node_id}/prompt/reveal` | `_mask_prompt_text()` / `_mask_message_list()` | Regex-based credential masking in prompt text |
| `get_run_output` MCP tool | `_mask_output_value()` from `runs.py` | Nested sensitive keys, returns `masked_fields` list |
| Admin SSO routes | `SensitiveValue` Pydantic type | Auto-masks on serialisation |
| `GET /api/v1/pipelines/{id}/graph` (and convert/revert responses) | `mask_pipeline_graph_node()` on every node | Sensitive env-var keys masked whole; remaining env values + context file contents redacted by the canonical secret-VALUE patterns; `composite_parameter_values` / `parameter_overrides` deep-masked |
| `GET /api/v1/pipelines/{id}/snapshots/{snapshot_id}` | `mask_pipeline_graph_node()` on each node of `graph_json` | Same node masking as the graph read |
| Snapshot diff endpoint | `mask_pipeline_graph_node()` on `nodes_added` / `nodes_removed` and the diffed graphs | Same node masking |
| `GET /api/v1/composite-templates/...` (list / get / create / patch / restore / editor GET+PUT) | `mask_pipeline_graph_node()` on every node of `sub_pipeline_graph_json` (via `_mask_sub_pipeline_graph` / `_mask_template_response`) | Same node masking as the pipeline graph read; the editor PUT and the PATCH endpoint resolve mask echoes against the stored template nodes via `merge_masked_graph_nodes()` |
| `POST /api/v1/pipelines/{id}/save-as-composite` | `mask_pipeline_graph_node()` on every copied node | Secret env values are masked BEFORE the template is persisted, so the org-readable template storage never receives them in the clear |
| MCP `get_pipeline_graph` tool | `mask_pipeline_graph_node()` on every node of the response | Same node masking as the REST graph read |
| MCP `update_pipeline_graph` tool | `merge_masked_graph_nodes()` before the write; `mask_pipeline_graph_node()` on the response | Same read/write neutrality as the REST graph endpoint |
| MCP `modulo://pipelines/{id}/snapshots/{snapshot_id}` resource | `mask_pipeline_graph_node()` on each node of `graph_json` before rendering | Same node masking as the REST snapshot detail |
| `POST /api/v1/library/export/{pipeline_id}?format=v1` | `strip_graph_node_credentials()` on `graph_nodes_json` — **STRIP**, not mask | Credential entries are removed and recorded in `redacted_credentials`, never masked (see §6) |

Graph READ masking must not corrupt data on WRITE: the graph endpoints are
full-replace, so a PATCH round-tripping a masked GET would otherwise persist
the mask literals over the stored secrets. `merge_masked_graph_nodes()`
resolves mask echoes against the stored graph before a graph write commits —
an echoed value is restored from storage, an echo with no stored counterpart
is dropped (fail closed), and keys the caller removed stay removed. The same
invariant holds on the MCP tool surface and on the composite-template editor /
PATCH surfaces.

**CLI hashing parity (FAR-1232).** The declarative `modulo apply` CLI compares
its YAML declarations against the API's MASKED reads, so the CLI carries its own
redaction (`strip_secret_shaped_graph` in `modulo/cli/apply/models.py`): pipeline
graph node env vars, context files, composite parameter values and parameter
overrides pass through the identical mask tiers (`is_sensitive_env_key` + the
canonical `modulo.core.secret_patterns` value patterns + deep-dict masking).
Because re-redacting already-redacted content is idempotent, the declared and the
stored-masked sides hash equal whenever the declaration matches stored state (no
phantom re-send per plan run). The same redaction runs on BOTH plan sides, so a
graph drift breakdown compares like-for-like rather than showing mask-only
"modified" entries. A rotated secret is invisible to that hash (both sides show
the mask sentinel) — `--refresh-secrets` re-sends the declared graph (true values
write through) for pipelines whose resolved graph declares secret-shaped
entries, and triggers keep the existing config_json refresh behaviour.

**Audited non-graph surfaces (FAR-1232).** No pipeline-graph serialisation was
found on: the feedback, HITL, variant-group, and run daily-facts surfaces
(structured records carry no graph payloads); the pipeline list / detail REST
endpoints (node COUNT only, never node contents); the workflow engine internals
and demo seed fixtures (server-internal, never client-facing). The sentinel-mask
storage pattern means the org read never exposes a clear secret for these
surfaces to leak.

### 2. Log redaction

Every log record passes through `SensitiveFieldFilter`, which redacts
18 known sensitive key patterns before the record is written. This prevents
accidental credential leakage in log files, structured JSON logs, and
aggregation systems.

The log redaction list (`logging_config._SENSITIVE_KEYS`) is more
comprehensive than the response masking list because logs capture internal
state that the public API never exposes (e.g. `fernet_key`, `private_key`).

### 3. Encryption at rest

Credentials are never stored as plaintext in the database:

- **Connector credentials** — encrypted with Fernet symmetric encryption
  using `FERNET_KEY` before storage.
- **LangSmith API keys** — encrypted with Fernet before storage in
  `otel_config_json`.
- **SSO provider secrets** — stored as-is in the `sso_providers` table
  (the column itself is restricted via RLS).
- **Secrets backend** — pluggable: Fernet DB encryption, HashiCorp Vault,
  or AWS Secrets Manager.

### 4. The reveal endpoint

When an admin needs to see a masked value, the `POST /api/v1/admin/sensitive/reveal`
endpoint provides temporary unmasking:

1. The admin sends a request with `resource_type`, `resource_id`, and optional `field`.
2. The server validates the admin role, fetches the resource, and applies RLS.
3. The plaintext value is returned immediately in the response body.
4. A 30-second Redis-backed token is generated for authenticated follow-up use.

Supported resource types: `connector`, `sso_provider`, `observability`.

### 5. Agent output masking in MCP

The `get_run_output` MCP tool returns agent outputs with sensitive fields
masked. It also returns a `masked_fields` list so the calling agent knows
which fields were redacted. This allows agents to proceed with their workflow
while being aware of redacted data.

### 6. Library export bundle (stripped, never masked)

`POST /api/v1/library/export/{pipeline_id}?format=v1` builds the **v1
portability bundle** — a ZIP carrying the pipeline's graph nodes, agents,
schemas and model backends so a pipeline can be reconstructed on a *fresh*
Modulo instance. Because it is cross-instance, it uses **strip** semantics:

| Surface | Mechanism | Coverage |
|---|---|---|
| `POST /api/v1/library/export/{pipeline_id}?format=v1` (`export_pipeline_bundle`) | `strip_graph_node_credentials()` on `pipeline.graph_nodes_json` | `env_vars` entries removed when the KEY is classified sensitive (`is_sensitive_env_key`) or the VALUE matches a secret pattern; `context_files` entries removed when the file CONTENT matches a secret pattern; `composite_parameter_values` / `parameter_overrides` entries removed recursively through nested dicts and lists (key tier + value tier at every depth) |

**Credentials are never exported; re-provision them on import.** Each removal
is recorded in the bundle's top-level `redacted_credentials` list as
`{node_id, field, path, reason}`. Importing that bundle emits a warning naming
every removed key/path — *"N credential(s) were not exported with this bundle;
re-provision them on this instance: …"* — and the import otherwise behaves as
before. Non-secret node content (app URLs, regions, retry counts, positions) is
exported unchanged.

This surface deliberately does **not** use the `••••••` mask. On a
same-instance read surface a mask placeholder means "unchanged — restore the
stored value" (`merge_masked_graph_nodes` resolves it on write); in a bundle
imported onto an instance that has no stored value, that meaning would silently
drop the credential with no warning. Removing the entry — and telling the
operator exactly what is missing — is the only safe semantics for a
cross-instance artefact.

If stripping itself fails, the whole credential-bearing field is dropped
(fail-closed, mirroring `mask_pipeline_graph_node`) and recorded as a
`stripping_failed` redaction, so a detector fault can never leak a raw
credential into an exported bundle.

`?format=v2` (ADR 015) carries no graph-node payload, so it needs no stripping.

---

## Best Practices for Agent Authors

### Do not include credentials in LLM prompts

When constructing agent prompts or system messages, avoid embedding API keys,
tokens, or secrets as literal values. Instead:

- Reference stored connectors by their ID (the pipeline engine resolves these).
- Use the secrets backend (`SecretsBackend`) to retrieve credentials at runtime.
- Pass input payloads that contain references, not raw secrets.

### Use structured output with safe key names

If your agent needs to output credential-like data for downstream processing,
use key names that are **not** caught by the sensitive key patterns (e.g.
`credential_ref` instead of `credential`, `auth_id` instead of `auth_token`).

### Handle masked fields gracefully in downstream agents

When reading another agent's output through `get_run_output`, check the
`masked_fields` list. If a field you need is masked, consider:
- Using the reveal endpoint if you have admin access.
- Restructuring the pipeline so credentials flow through internal state
  rather than agent outputs.
- Adding a connector or secrets backend lookup step.

### Avoid logging sensitive data

Never log raw credentials, tokens, or secrets using `logger.info()` or
`logger.debug()`. If you must log the presence of a credential, log its
key name and a boolean presence indicator:

```python
_log.info("Connector configured", extra={"has_api_key": bool(api_key)})
```

### Use the `SensitiveValue` Pydantic type for new response models

If you add a new API response model that includes a sensitive field, annotate
the field with the `SensitiveValue` type from `sensitive_mask.py`:

```python
from modulo.api.middleware.sensitive_mask import SensitiveValue


class MyResponse(BaseModel):
    public_data: str
    secret_value: SensitiveValue | None = None
```

This auto-masks the field on serialisation with zero additional code.

---

## How to Configure Which Fields Are Considered Sensitive

Sensitive key patterns are defined in three places:

### API response masking and library export stripping

File: `backend/src/modulo/core/secret_patterns.py` — re-exported by
`backend/src/modulo/api/middleware/sensitive_mask.py`, which is where API-layer
callers import it from. It lives in `core` so core export paths
(`workflow_import_export`, §6) can share the exact same classifier without the
API layer being imported from `core`.

```python
_SENSITIVE_KEY_PATTERNS = frozenset(
    {
        "token",
        "secret",
        "api_key",
        "password",
        "passwd",
        "key",
        "credential",
        "database_url",
        "encryption",
        "signing",
        "private",
    }
)
```

To add a new pattern, edit this set and verify tests pass. The function
`is_sensitive_key(key)` performs case-insensitive substring matching, so
adding `"pwd"` would catch `db_pwd`, `ldap_pwd`, etc. Env-var names matched
wholesale by `is_sensitive_env_key(key)` (on top of the substring patterns)
are `MODULO_USERS`, `DATABASE_URL` and `PYPI_TOKEN`.

### Log redaction

File: `backend/src/modulo/core/logging_config.py`

```python
_SENSITIVE_KEYS: frozenset[str] = frozenset(
    {
        "api_key",
        "api_secret",
        "access_key",
        "secret_key",
        "token",
        "password",
        "passwd",
        "secret",
        "private_key",
        "credential",
        "fernet_key",
        "auth_token",
        "bearer_token",
        "refresh_token",
        "client_secret",
        "client_id",
        "session_key",
        "encryption_key",
    }
)
```

For log redaction, each key is matched **exactly** (case-insensitive) — not
as a substring. This prevents over-redaction of benign fields.

### Prompt text masking

File: `backend/src/modulo/api/routes/runs.py`, function `_mask_prompt_text()`

Regex patterns are defined for each sensitive key prefix. When adding a new
pattern, add both the regex and the replacement.

---

## What Happens When Sensitive Data Leaks

### Detection

- **Log monitoring** — The structured JSON logger emits all log records
  with redacted sensitive fields. If a raw credential appears in logs, it
  indicates the `SensitiveFieldFilter` missed a pattern.
- **Span attribute inspection** — Observability test steps verify that no
  known credential keys appear in OpenTelemetry span attributes (see
  `tests/bdd/steps/test_observability.py`).
- **MCP masked_fields** — The `get_run_output` tool reports which fields
  were masked, allowing automated detection of fields that should have been
  masked but were not.

### Audit events

Sensitive data access via the reveal endpoint is gated by:
1. **Authentication** — the caller must have a valid session.
2. **Role check** — only `admin` role can reveal.
3. **RLS enforcement** — cross-organisation access returns 404.
4. **Resource-level authorisation** — each resource type has its own query.

Failed reveal attempts (wrong role, resource not found, unknown type) return
appropriate HTTP errors but do **not** log the requested value.

### Incident response

If sensitive data is discovered in an unmasked location:
1. Rotate the affected credentials immediately.
2. Identify the leak path (e.g. unredacted log, missing masking on a new
   endpoint, prompt template exposing secrets).
3. Add the missing key pattern to the appropriate `_SENSITIVE_KEY_PATTERNS`
   or `_SENSITIVE_KEYS` set.
4. Add a test that would have caught the leak.
5. Update the product contract/schema documentation to reflect the new key pattern.

---

## Implementation Review

### Existing coverage

| Protection layer | Status | Key files |
|---|---|---|
| API response masking | ✅ Complete | `sensitive_mask.py` |
| Agent output masking | ✅ Complete | `runs.py`, `mcp_server.py` |
| Prompt text masking | ✅ Complete | `runs.py` |
| Log redaction | ✅ Complete | `logging_config.py` |
| Encryption at rest | ✅ Complete | `secrets_backend/` |
| Reveal endpoint | ✅ Complete | `sensitive_mask.py` |
| Library export bundle (v1) credential stripping | ✅ Complete | `core/workflow_import_export.py` (`strip_graph_node_credentials`, import warning) |
| BDD test coverage | ✅ Complete | `dom_sensitive_data.feature` |
| Unit test coverage | ✅ Complete | `test_sensitive_mask.py` |

### Known gaps and mitigations

| Gap | Status | Mitigation |
|---|---|---|
| `passwd` pattern not in `sensitive_mask.py` | ✅ Fixed | Added to `_SENSITIVE_KEY_PATTERNS` and prompt mask regex |
| `get_run_io_endpoint` output masking | ✅ Fixed | Applies `_mask_output_value` to outputs |
| `export_run_fixture` output masking | ✅ Fixed | Applies `_mask_output_value` to outputs and input payload |
