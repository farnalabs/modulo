# ADR 015 — Bundle Format v2 (YAML)

**Date**: 2026-07-16
**Status**: Accepted

---

## Context

ADR 004 established the Agent-as-a-Self-Contained Bundle format (v1), a ZIP+JSON
structure used for import/export of pipelines between orgs and from the Modulo
community library. The v1 format embeds agent definitions, schemas, edges, and
HITL gate config but has several limitations:

1. **No triggers.** Bundles describe pipeline topology but cannot carry trigger
   configuration (webhook, cron, polling, agent_signal). Every imported pipeline
   requires manual trigger setup.
2. **No team ownership.** The `owner_team_id` and `visibility` fields are stripped
   on export and default to org-wide on import. There is no way to ship a bundle
   that belongs to a specific team.
3. **No lifecycle map reference.** Bundles cannot declare which lifecycle stage
   they participate in (e.g. `code-review`, `deploy-canary`, `incident-response`).
4. **No composite template refs.** Multi-template workflows (e.g. a PR review
   pipeline that spans code-review + deploy-canary) cannot reference templates
   from within the bundle.
5. **No partial bundles.** There is no mechanism to express an incomplete pipeline
   that needs assembly with other bundles (e.g. a reusable "notify on failure"
   subgraph).
6. **Agent runtime coupling.** The E2B `template_id` and `agent_command` fields
   (added by ADR 004) exist on the Agent entity but the v1 JSON structure was
   never updated to carry them in bundles.
7. **No signature envelope.** Community library bundles are published to a
   registry but there is no mechanism for publishers to sign their bundles or
   for consumers to verify authenticity.
8. **ZIP is opaque.** The v1 ZIP+JSON format cannot be inspected, diffed, or
   edited with standard tools. Version control integration is poor — reviewing
   a bundle change requires unzipping and diffing JSON manually.

## Decision

The v2 bundle format is a **single YAML file** with optional Ed25519 signature
envelope. YAML was chosen over JSON, TOML, or protobuf for the following reasons:

- **Readability.** YAML with comments is human-writable and human-reviewable.
  A bundle can be inspected in any text editor or diff tool.
- **Composability.** YAML anchors and aliases allow schema definitions to be
  referenced multiple times without duplication — useful for multi-agent
  pipelines where the same schema serves as input to one agent and output
  of another.
- **Tooling.** Standard CI workflows can diff, lint, and validate YAML files
  without custom tooling. `yq`, `yamllint`, and JSON Schema validators all
  support YAML natively.
- **No opaque container.** A single `.yaml` file is trivially version-controlled,
  reviewed in pull requests, and inspected in transit. No unzip step needed.

The format specification is defined below and validated by the JSON Schema at
`docs/schemas/bundle-v2-schema.json`.

---

## Decision — Format Specification

### Top-level structure

```yaml
modulo_workflow:
  id: <unique-id>
  name: "Human-readable name"
  version: "1.0.0"
  author: "author@example.com"

  # NEW: team ownership — resolved during import
  owner_team: null | "<team-name>"

  # NEW: visibility — defaults to org on import
  visibility: org | team | private

  # NEW: lifecycle map reference
  lifecycle_map_ref: null | "<lifecycle-map-id>"

  # NEW: composite template references
  composite_template_refs:
    - "<template-id-1>"
    - "<template-id-2>"

  # NEW: partial bundle support
  partial: false | true

  requires:
    connector_types:
      - git-host
      - issue-tracker
    abstract_schemas:
      - document-input
      - issue-ticket

  # NEW: trigger configuration
  triggers:
    - trigger_type: webhook | cron | polling | agent_signal
      config: {}
      active: true | false

  agents:
    - id: agent-1
      prompt_template: "..."
      input_schema: schema-ref
      output_schema: schema-ref
      template_id: null | "e2b-template-id"
      agent_command: null | "opencode ..."

  edges:
    - source: agent-1
      target: agent-2
      edge_type: normal | reject | conditional
      hitl_gate_config: null | { ... }

  schemas:
    - ...
```

### Key differences from v1

| Aspect | v1 (ZIP+JSON) | v2 (YAML) |
|---|---|---|
| Container | ZIP archive | Single `.yaml` file |
| Format | JSON | YAML 1.2 |
| Triggers | ❌ Not supported | ✅ webhook, cron, polling, agent_signal |
| Team ownership | ❌ Stripped on export | ✅ `owner_team` field, resolved on import |
| Visibility | ❌ Always `org` | ✅ `org` / `team` / `private` |
| Lifecycle map | ❌ Not supported | ✅ `lifecycle_map_ref` |
| Composite templates | ❌ Not supported | ✅ `composite_template_refs` |
| Partial bundles | ❌ Not supported | ✅ `partial: true/false` |
| Agent `template_id` | ❌ Not in bundle | ✅ Included in agent definition |
| Agent `agent_command` | ❌ Not in bundle | ✅ Included in agent definition |
| Signature envelope | ❌ Not supported | ✅ Ed25519 envelope |
| Comments | ❌ JSON has no comments | ✅ YAML supports comments |

### Trigger configuration

The `triggers` section allows bundles to ship pre-configured triggers. Each
trigger has:

| Field | Type | Description |
|---|---|---|
| `trigger_type` | enum | One of `webhook`, `cron`, `polling`, `agent_signal` |
| `config` | object | Type-specific JSON configuration |
| `active` | boolean | Whether the trigger is enabled on import; defaults to `true` |

**`webhook` config:**
```yaml
config:
  secret_name: "my-webhook-secret"     # references a connector or env secret
  payload_mapping:
    body.title: "$.issue.title"
    body.description: "$.issue.body"
  flood_protection:
    max_per_minute: 60
```

**`cron` config:**
```yaml
config:
  expression: "0 */6 * * *"
  timezone: "UTC"
```

**`polling` config:**
```yaml
config:
  connector_type: git-host
  resource_type: pull_request
  poll_interval_seconds: 300
  filters:
    state: open
```

**`agent_signal` config:**
```yaml
config:
  signal_name: "review-complete"
  payload_schema_id: "review-result"
```

### Team ownership resolution

On import, the `owner_team` field is resolved against the importing org's team
roster:

1. If `owner_team` is `null` — ownership defaults to org-wide (same as v1)
2. If `owner_team` matches a team name in the importing org — the imported
   pipeline is owned by that team
3. If `owner_team` does not match any team — the importer is prompted to
   select a team or default to org-wide, and a warning is emitted

The `owner_team` value in the bundle is a **team name** (human-readable), not
an internal UUID. Team UUIDs are org-specific and meaningless outside the
source org. Resolution by name allows the importer to match teams that share
the same name across orgs (common in monorepo setups).

### Visibility

The `visibility` field controls who can see and use the imported pipeline:

| Value | Behaviour |
|---|---|
| `org` | Any member of the importing org can see and use the pipeline |
| `team` | Only members of the owning team can see and use the pipeline |
| `private` | Only the importing user and org admins can see and use the pipeline |

On import, `visibility` defaults to `org` if not specified. The importer is
presented with a visibility picker alongside the ownership picker.

### Partial bundles

A `partial: true` bundle represents an incomplete pipeline that must be
assembled with other bundles or manually extended before it is runnable.
Partial bundles cannot be triggered — the `triggers` section must be empty or
absent.

Use cases:
- **Reusable subgraphs.** A "notify on failure" subgraph with a single agent
  that sends a notification. Assembles into any pipeline that needs failure
  notification.
- **Template components.** A "PR review" subgraph that performs code review
  but needs a trigger and a "resolve conversation" agent to be connected by
  the user.
- **In-progress work.** A pipeline being developed across multiple sessions
  can be exported as `partial: true` and re-imported for continued editing.

On import, partial bundles are created in `draft` status — they cannot run
until `partial` is set to `false` and all required connectors and schemas are
bound.

### Composite template references

`composite_template_refs` allows a bundle to declare that it participates in
one or more lifecycle templates (e.g. `code-review`, `deploy-canary`). These
are template IDs from the Modulo lifecycle map — the importing org must have
compatible templates defined.

On import:
1. Each `composite_template_ref` is checked against the org's registered templates
2. If a matching template exists, the bundle is offered as a participant in that
   template's lifecycle
3. If no matching template exists, the ref is ignored (warning emitted)

### Ed25519 signature envelope

Bundles published to the Modulo community registry may be wrapped in an
Ed25519 signature envelope. The envelope is an optional JSON wrapper around
the YAML content:

```json
{
  "signature": "base64-encoded-ed25519-sig",
  "public_key_hash": "sha256-of-public-key",
  "signed_at": "2026-07-16T00:00:00Z",
  "payload": "base64-encoded-yaml-bundle"
}
```

- The `payload` is the raw YAML bundle encoded as base64
- The `signature` is an Ed25519 signature over `sha256(payload)`
- The `public_key_hash` allows consumers to look up the publisher's public key
  from the registry
- Unverified bundles are accepted with a warning; verified bundles display a
  "verified publisher" badge
- Modulo never signs bundles on behalf of users — signing is always
  publisher-initiated

### JSON Schema validation

The complete JSON Schema for v2 bundles is defined at
`docs/schemas/bundle-v2-schema.json`. Key validation rules:

- `modulo_workflow` is required, must be an object
- `modulo_workflow.id` is required, must be a valid UUID (v4)
- `modulo_workflow.name` is required, must be a non-empty string ≤ 256 chars
- `modulo_workflow.agents` is required, must be a non-empty array
- Each agent must have a unique `id` within the bundle
- Each agent must have a non-empty `prompt_template`
- `edges` is optional; if present, each edge must reference valid agent IDs
- `triggers` is optional; if present, each trigger must have a valid `trigger_type`
- `partial: true` bundles must have no triggers
- `visibility` must be one of `org`, `team`, `private`
- `owner_team` must be a string or null

---

## Security considerations

1. **YAML parsing.** All v2 bundles MUST be parsed with `yaml.safe_load()`.
   Arbitrary YAML tags (`!python/object`, `!tag:...`) must be rejected.
   Semgrep rule `yaml_safe_load` already enforces this across the codebase.

2. **Signature verification.** The Ed25519 signature envelope is optional but
   when present, the signature MUST be verified before the bundle payload is
   parsed. An invalid signature MUST produce a hard error (not a warning) for
   signed bundles. Unsigned bundles are accepted with a warning.

3. **Prompt template sanitization.** All `prompt_template` values are Jinja2
   templates and MUST use `SandboxedEnvironment` when rendered. This is
   already enforced by the `sandboxed_jinja2` semgrep rule.

4. **Trigger secret handling.** Webhook trigger configs may reference secrets
   by name. Secret names are resolved against the org's connector instances
   at import time — never embedded in the bundle.

5. **Path traversal.** The `template_id` field references E2B template IDs or
   Docker image refs. Both are validated against allowlists — no arbitrary
   image references are accepted.

6. **Partial bundle validation.** Partial bundles cannot be triggered — the
   engine must reject any attempt to run a pipeline with `partial: true`.
   This check happens at the engine level, not just in the UI.

---

## Migration path from v1

### Backward compatibility

v1 ZIP+JSON bundles continue to be importable. Detection is by file extension:

| Extension | Format |
|---|---|
| `.yaml` / `.yml` | v2 (YAML) |
| `.zip` | v1 (ZIP+JSON) |

The import endpoint checks the file extension and dispatches to the
appropriate parser. Both formats produce the same internal pipeline model.

### v1 to v2 conversion

A conversion script is provided at `docs/ops/bundle-migration-v1-to-v2.md`:

```
python -m modulo.tools.migrate-bundle --input bundle-v1.zip --output bundle-v2.yaml
```

The conversion maps:

| v1 field | v2 field |
|---|---|
| `agents[].prompt_template` | `agents[].prompt_template` (unchanged) |
| `agents[].input_schema` | `agents[].input_schema` (unchanged) |
| `agents[].output_schema` | `agents[].output_schema` (unchanged) |
| `edges` | `edges` (unchanged, if present) |
| — (not present) | `agents[].template_id` → `null` |
| — (not present) | `agents[].agent_command` → `null` |
| — (not present) | `triggers` → `[]` |
| — (not present) | `owner_team` → `null` |
| — (not present) | `visibility` → `org` |
| — (not present) | `partial` → `false` |
| — (not present) | `lifecycle_map_ref` → `null` |
| — (not present) | `composite_template_refs` → `[]` |

### Deprecation timeline

| Milestone | v1 support |
|---|---|
| v2.0 release | Full backward compatibility. Both v1 and v2 importable. |
| v2.1 release | v1 import emits deprecation warning. Conversion script documented. |
| v2.2 release | v1 import requires `--allow-legacy-format` flag. |
| v3.0 release | v1 import removed. Only v2 YAML supported. |

---

## Consequences

### Positive

- **Human-readable.** YAML can be inspected, edited, and diffed in standard tools.
- **Richer metadata.** Triggers, team ownership, visibility, lifecycle maps, and
  composite templates make bundles truly self-contained.
- **Partial bundles enable composition.** Reusable subgraphs can be published
  and assembled — not every pipeline needs to be monolithic.
- **Runtime configuration travels with agents.** `template_id` and `agent_command`
  on agent definitions mean agent runtimes are not lost on export/import.
- **Signature verification.** The optional Ed25519 envelope gives consumers
  confidence that a bundle came from its claimed publisher.
- **Single-file simplicity.** No zip/unzip step. A bundle is one file that can
  be uploaded, downloaded, and stored in git.

### Negative

- **YAML complexity.** YAML 1.2 is more complex than JSON — edge cases around
  implicit typing, indentation sensitivity, and multi-line strings require
  careful handling.
- **Migration cost.** Existing v1 bundles need conversion. The conversion tool
  mitigates this but users with many bundles face a one-time migration effort.
- **Signature envelope is an optional add-on.** The envelope adds complexity
  for registry publishing that most self-hosted users will never use.

### Migration

1. Create the v2 YAML parser (`modulo.core.workflow_import_export.v2_loader`)
2. Create the v1→v2 conversion script (`modulo.tools.migrate-bundle`)
3. Update the import/export API to detect format by extension
4. Add `triggers`, `owner_team`, `visibility`, `lifecycle_map_ref`,
   `composite_template_refs`, and `partial` fields to the internal pipeline model
5. Add trigger resolution to the import binding flow
6. Add team ownership resolution to the import flow
7. Add signature verification to the registry download flow
8. Deprecate v1 per the timeline in §Migration path from v1

### Open questions

- Should the YAML file extension be `.yaml` (preferred) or `.yml`? Proposal:
  accept both, canonicalise to `.yaml` in the UI and docs.
- Should partial bundles support merging at the YAML level (e.g. `!include`
  directives)? Proposal: no — YAML `!include` is not standard. Assembly is
  handled at the application level during import.
- Should the signature envelope be mandatory for community library bundles?
  Proposal: no — signing is optional. Verified bundles get a badge; unsigned
  bundles are accepted with a warning.
