# Schema Reference

Modulo uses JSON Schema for structured data validation across pipeline nodes.

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
| `created_by` | UUID FK → users | Creator |

## SchemaVersion entity

| Field | Type | Description |
|-------|------|-------------|
| `id` | UUID | Primary key |
| `schema_id` | UUID FK → schemas | Parent schema |
| `version` | String(50) | Version label (e.g. "1.0.0") |
| `version_number` | Integer | Monotonic version counter |
| `definition_json` | JSON | JSON Schema definition |
| `published` | Boolean | Published flag |
| `deprecated` | Boolean | Deprecation flag |
| `created_by` | UUID FK → users | Creator |

## JSON Schema usage

Schema definitions follow [JSON Schema draft-07](https://json-schema.org/):

```json
{
  "$schema": "https://json-schema.org/draft-07/schema#",
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

## Schema inference

Modulo can auto-generate schemas from sample data via `POST /api/v1/schemas/infer`:

```json
{
  "connector_instance_id": "<uuid>",
  "sample_query": {"path": "/issues?limit=5"}
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
requiring a concrete schema definition. They act as type constraints that must
be satisfied when a pipeline is published.

## Deprecation lifecycle

1. Mark version as `deprecated` — graph validator emits warnings but does not
   block existing pipeline runs
2. Pipelines using deprecated schemas can still run (backward-compatible)
3. New pipeline versions must pin a non-deprecated schema version
