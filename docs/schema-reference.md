# Schema Reference

Modulo uses JSON Schema for structured data validation across pipeline nodes.
This reference covers the schema entity model, schema profiles, validation
semantics, the on-disk sandbox contract, and native structured output.

## Architecture

```
Schema (org-scoped entity)
  └── SchemaVersion (versioned)
        └── definition_json (JSON Schema)
```

Each pipeline node declares `input_schema` and `output_schema` bindings. The
graph validator checks schema compatibility between connected nodes at save-time
and run-time.

## Schema entity

| Field | Type | Description |
|-------|------|-------------|
| `id` | UUID | Primary key |
| `organisation_id` | UUID | Org scoping (RLS enforced) |
| `name` | String(255) | Unique within org |
| `description` | String(2000) | Optional |
| `abstract_name` | String(255) | Optional namespaced reference |
| `collection_install_id` | UUID FK → collection_installs | Owning collection install (nullable) |
| `account_id` | UUID FK → accounts | Owning account |
| `folder_id` | UUID FK → schema_folders | Optional parent folder |
| `deprecated` | Boolean | Default false |
| `deprecated_at` | Timestamp | Set when deprecated |
| `system` | Boolean | System-managed schema |
| `created_at` / `updated_at` | Timestamp | Auditing |

## SchemaVersion entity

| Field | Type | Description |
|-------|------|-------------|
| `id` | UUID | Primary key |
| `organisation_id` | UUID | Org scoping (RLS enforced) |
| `schema_id` | UUID FK → schemas | Parent schema |
| `version` | String(50) | Version label (e.g. "1.0.0") |
| `version_number` | Integer | Monotonic version counter |
| `definition_json` | JSON | JSON Schema definition |
| `published` | Boolean | Published flag |
| `deprecated` | Boolean | Deprecation flag |
| `account_id` | UUID FK → accounts | Owning account |
| `created_at` / `updated_at` | Timestamp | Auditing |

## JSON Schema usage

Schema definitions follow [JSON Schema Draft 2020-12](https://json-schema.org/):

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema#",
  "type": "object",
  "required": ["title", "description"],
  "properties": {
    "title": {
      "type": "string",
      "description": "The title of the item"
    },
    "description": {
      "type": "string",
      "description": "A detailed description"
    },
    "priority": {
      "type": "string",
      "enum": ["low", "medium", "high", "critical"]
    },
    "metadata": {
      "type": "object",
      "properties": {
        "labels": {
          "type": "array",
          "items": { "type": "string" }
        }
      }
    }
  }
}
```

## Schema profiles

A schema profile controls how a node's output schema is translated before
dispatch to the model backend. Profiles are set per-node or per-agent; when
both are present, the node-level profile wins. Absent on both defaults to
`verbatim`.

| Profile | Behaviour |
|---------|-----------|
| `verbatim` | Identity. The schema is passed to the provider unchanged. |
| `provider-strict` | Strips JSON Schema keywords the target provider does not support in strict mode. Keywords are stripped per-provider (OpenAI, Anthropic, Google, DeepSeek each have their own unsupported set). Advisory keywords (`default`, `examples`, `title`, `description`, `format`) are always stripped for the strictest common denominator. Structural keywords (`type`, `properties`, `required`, `items`, `anyOf`, `oneOf`, `allOf`, `not`, `if/then/else`, `enum`, `const`, `$ref`, `$defs`) are never stripped. |
| `runtime-sdk` | Renders the schema for the target runtime. Currently identity (pass-through); reserved for future runtime-specific transforms. |

### Rendering details

The rendering pass (FAR-900) is **best-effort** and **never blocks a node**. It
inlines `$ref`/`$defs` (local pointers only; external refs are rejected),
strips unsupported keywords per the profile, and caches results in a bounded
LRU keyed by (schema content SHA-256, profile, provider_id, renderer_version).

Hard limits on `$ref` flattening: max depth 32, max expanded nodes 10,000.
Exceeding either falls back to `verbatim`. Abstract schemas (only `$ref` or
composition keywords, no concrete type/properties) are skipped.

### Design-time warnings

When a graph is saved with `include_schema_warnings=true`, the graph response
includes a `schema_translation_report` listing which keywords would be stripped
for each node's target provider. This lets operators preview the impact before
running a pipeline.

### Per-provider keyword sets

The following keywords are stripped in `provider-strict` mode per provider.
Advisory keywords (always stripped) are in addition to these.

| Provider | Provider-specific stripped keywords |
|----------|--------------------------------------|
| `openai` | `$id`, `$schema`, `$defs`, `definitions`, `default`, `examples`, `minItems`, `maxItems`, `minLength`, `maxLength`, `minimum`, `maximum`, `exclusiveMinimum`, `exclusiveMaximum`, `multipleOf`, `pattern`, `minProperties`, `maxProperties` |
| `anthropic` | `$id`, `$schema`, `$defs`, `definitions`, `default`, `examples`, `minItems`, `maxItems`, `minLength`, `maxLength`, `minimum`, `maximum`, `exclusiveMinimum`, `exclusiveMaximum`, `multipleOf`, `patternProperties`, `additionalProperties`, `minProperties`, `maxProperties` |
| `google` | `$id`, `$schema`, `$defs`, `definitions`, `default`, `examples`, `minItems`, `maxItems`, `minLength`, `maxLength`, `minimum`, `maximum`, `exclusiveMinimum`, `exclusiveMaximum`, `multipleOf`, `pattern`, `patternProperties`, `minProperties`, `maxProperties` |
| `deepseek` | `$id`, `$schema`, `$defs`, `definitions`, `default`, `examples`, `pattern`, `patternProperties`, `minProperties`, `maxProperties`, `minItems`, `maxItems` |

## Validation semantics

Modulo validates node output against the assigned schema using
[jsonschema](https://python-jsonschema.readthedocs.io/) Draft 2020-12. Two
validator modes control behaviour on validation failure.

### Lenient mode (default)

In lenient mode, validation failures are recorded as warnings but the node does
not fail. The run continues with the raw (unvalidated) output. The enforcement
outcome is recorded as `lenient_validation_bypassed` in the per-attempt
telemetry. This is the hard default; no configuration is required to use it.

### Strict mode

In strict mode, validation failures trigger the repair loop. The repair loop
re-prompts the same model backend with the validation errors, up to a
configurable budget (hard cap: 3 attempts). If the repaired output passes
validation, the outcome is `passed_after_repair`. If the budget is exhausted
or the model produces the same errors consecutively (untranslatable), the
outcome is `repair_exhausted` and the node fails.

The repair prompt includes only structural metadata (JSON Pointer, constraint,
expected/actual values, allowed enum values). Free-text schema keywords
(`description`, `title`, `examples`, `default`) are never included in the
repair prompt.

### Validation outcomes

| Outcome | Meaning |
|---------|---------|
| `no_schema` | No schema was assigned to the node; no validation occurred. |
| `native_decoded_and_validated` | The sandbox returned structured output via the native JSON channel and it passed validation. |
| `native_decode_failed` | The sandbox returned structured output but decoding failed. |
| `native_decode_not_json` | The sandbox returned structured output that was not valid JSON. |
| `verbatim_passed` | Post-hoc validation passed on raw (verbatim) output. |
| `posthoc_validation_failed` | Post-hoc validation failed on raw output. In strict mode, triggers the repair loop. |
| `lenient_validation_bypassed` | Validation failed but lenient mode recorded a warning instead of failing the node. |
| `repair_attempted` | The repair loop is in progress (more attempts remain). |
| `passed_after_repair` | Validation passed after one or more repair attempts. |
| `repair_exhausted` | The repair loop budget was exhausted (untranslatable errors or budget cap). |
| `schema_unenforceable` | The schema could not be enforced (malformed schema or runtime error). |

## Native structured output

When a model backend declares `supports_native_structured_output` and the node
has an `output_schema_json` assigned, Modulo forwards the rendered schema to
the provider's native structured-output decoding path. This means the provider
guarantees the output conforms to the schema at the API level.

The rendering pipeline:

1. The output schema is rendered for the effective profile (e.g.
   `provider-strict` strips unsupported keywords for the target provider).
2. The rendered schema is bounds-checked (size, depth, external `$ref`). If
   rendering amplifies the schema past the limits, the raw schema is sent as
   a fallback (the provider resolves `$ref` itself).
3. If neither the rendered nor raw schema passes bounds, no native schema is
   requested.

The `native_output` flag in the enforcement record indicates whether native
structured output was used for a given attempt.

## On-disk sandbox contract

When a pipeline runs, Modulo writes advisory schema files into the sandbox
filesystem at `<output_dir>/schemas/<node_id>/`. These files are inputs to
the dispatched runtime and any operator tooling; Modulo validates independently.

### File layout

```
schemas/
  <node_id>/
    input.canonical.json      Source-of-truth input schema (sanitised)
    input.active.json         Rendered input schema (sanitised)
    output.canonical.json     Source-of-truth output schema (sanitised)
    output.active.json        Rendered output schema (sanitised)
```

- **Canonical** files carry the raw, sanitised schema (free-text keywords
  stripped, `default` preserved).
- **Active** files carry the rendered form. When the profile is `verbatim`,
  active equals canonical. When the profile is `provider-strict` or
  `runtime-sdk`, active is the rendered (keyword-stripped) version.

### Sanitisation rules

Before writing, every schema is sanitised:

- **Stripped at every level:** `description`, `title`, `examples`
- **Never stripped:** `default` (functional keyword)
- **Bounds checked:** `const` strings capped at 256 characters; `enum` arrays
  capped at 50 entries. Exceeding a cap triggers fallback to the original
  schema (no silent truncation).

### Contract version

Every written file carries `_schema_contract_version` (currently `1`) as a
top-level integer key. The host-side validator reads and compares this value.
A version mismatch triggers a warn (lenient) or re-render (strict).

### Sentinel

When schema files cannot be written (any error in the write path), a sentinel
`{"_schema_available": false}` file is emitted so consumers can detect absence
structurally. A write failure never fails the node.

### Atomicity

Each file is written to a `.tmp` sibling, `fsync`'d, then atomically replaced
via `os.replace`. If the canonical file succeeds but the active file fails,
the canonical file is rolled back; a half-updated state is never left on disk.

### Environment variable

The `MODULO_SCHEMA_DIR` environment variable points to the `schemas/`
directory inside the sandbox.

## Schema inference

Modulo can auto-generate schemas from sample data via `POST /api/v1/schemas/infer`:

```json
{
  "connector_instance_id": "<uuid>",
  "sample_query": {"resource": "issues", "limit": 5}
}
```

Or from natural language via `POST /api/v1/schemas/generate`:

```json
{
  "description": "Schema for a GitHub issue with title, body, labels, and assignee",
  "examples": [
    {"title": "Fix login", "body": "Users cannot log in", "labels": ["bug"]}
  ]
}
```

## Abstract schemas

Abstract schemas (`abstract_name` set) can be bound as input/output without
requiring a concrete definition. They act as type constraints that must
be satisfied when a pipeline is published.

## Deprecation lifecycle

1. Mark version as `deprecated`. The graph validator emits warnings but does
   not block existing pipeline runs.
2. Pipelines using deprecated schemas can still run (backward-compatible).
3. New pipeline versions must pin a non-deprecated schema version.

## See also

- [Agent Configuration](./agent-config.md) for setting `schema_profile` on
  nodes and agents.
- [Schema Enforcement Operations](./operations/schema-enforcement.md) for
  observability, endpoints, and the lenient-to-strict migration procedure.
