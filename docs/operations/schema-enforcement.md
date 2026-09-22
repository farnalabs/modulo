# Schema Enforcement Operations

This guide covers observability for schema enforcement: the enforcement
endpoints, the enforcement data model, SQL queries for analytics, and the
cleanup/retention behaviour for schema files.

## Endpoints

### Run-detail enforcement surface

```
GET /api/v1/runs/{run_id}/schema-enforcement
```

Returns per-node enforcement records for a specific run, including the
resolved mode, validation outcome, and per-node aggregate counts. The
response shape:

```json
{
  "run_id": "550e8400-e29b-41d4-a716-446655440000",
  "schema_validator_mode": "lenient",
  "schema_validation_outcome": "lenient_validation_bypassed",
  "nodes": [
    {
      "node_id": "node-1",
      "records": [
        {
          "outcome": "lenient_validation_bypassed",
          "resolved_profile": "provider-strict",
          "native_output": true,
          "repair_attempts": 0,
          "wasted_attempts": 0,
          "validation_errors": [],
          "total_error_count": 0,
          "truncated": false
        }
      ],
      "aggregate": {
        "native_count": 1,
        "verbatim_count": 0,
        "repair_count": 0,
        "wasted_count": 0,
        "total_attempts": 1
      }
    }
  ],
  "aggregate": {
    "native_count": 1,
    "verbatim_count": 0,
    "repair_count": 0,
    "wasted_count": 0,
    "total_attempts": 1,
    "enforcement_record_count": 1
  }
}
```

The per-record fields:

| Field | Description |
|-------|-------------|
| `outcome` | The `SchemaValidationOutcome` for this attempt. See [Schema Reference](../schema-reference.md#validation-outcomes). |
| `resolved_profile` | The schema profile used (`verbatim`, `provider-strict`, `runtime-sdk`, or `None` when no schema was assigned). |
| `native_output` | `true` when the sandbox returned structured output via the native JSON channel. |
| `repair_attempts` | Count of repair loop invocations on this attempt. |
| `wasted_attempts` | Count of attempts whose sole failure cause was schema rejection (repair loop consumed budget but produced nothing usable). |
| `validation_errors` | The first N structured validation error dicts (capped at 50). Each has `pointer`, `constraint`, `message`, and optionally `expected`, `actual`, `allowed`. |
| `total_error_count` | Total number of validation errors before truncation. |
| `truncated` | `true` when the error list was truncated to the cap. |

### Flip guard endpoint

```
GET /api/v1/runs/schema-enforcement/flip-guard
```

Examines ALL enforcement records for the caller's organisation and returns
the guard's advisory verdict on whether switching from lenient to strict
mode is safe.

```json
{
  "safe_to_flip": false,
  "lenient_warning_count": 12,
  "total_records": 48,
  "affected_outcomes": ["lenient_validation_bypassed"],
  "advisory": "NOT safe to flip: 12 of 48 enforcement record(s) are lenient-mode warnings that would become hard failures in strict mode. Outcomes: lenient_validation_bypassed. Resolve the underlying validation failures before switching to strict."
}
```

| Field | Description |
|-------|-------------|
| `safe_to_flip` | `true` when zero lenient-mode warnings exist. |
| `lenient_warning_count` | Number of `LENIENT_VALIDATION_BYPASSED` records that would become hard failures in strict mode. |
| `total_records` | Total enforcement records examined across the organisation. |
| `affected_outcomes` | Distinct outcome types from lenient-mode warnings. |
| `advisory` | Human-readable explanation of the verdict. |

**This endpoint never mutates the mode.** Changing the mode remains a
separate, explicit operator action.

## Data model

### Per-attempt records (`run_node_outputs.schema_enforcement_json`)

Every non-`__final__` attempt that has a schema assigned gets a
`schema_enforcement_json` row in `run_node_outputs`. The JSON payload is
bounded to 64 KB; validation errors are truncated with `total_count` and
`truncated` flags when the full list exceeds the cap.

A partial index `ix_run_node_outputs_enforcement_pending` on
`run_id WHERE schema_enforcement_json IS NOT NULL` supports the run-detail
enforcement query.

### Run-level columns (`runs`)

| Column | Type | Description |
|--------|------|-------------|
| `schema_validator_mode` | String(30) | The mode the run actually executed under (`lenient` or `strict`). |
| `schema_validation_outcome` | String(40) | The run-level aggregate outcome (most severe across all attempts). |

### Daily facts counters (`run_daily_facts`)

| Column | Description |
|--------|-------------|
| `enforcement_native_count` | Count of attempts with native structured output. |
| `enforcement_verbatim_count` | Count of attempts with verbatim output. |
| `enforcement_repair_count` | Total repair loop invocations across all attempts. |
| `enforcement_wasted_count` | Total wasted attempts (schema rejection only). |

## SQL queries

### Find runs with lenient-mode warnings

```sql
SELECT run_id, schema_validator_mode, schema_validation_outcome
FROM runs
WHERE schema_validator_mode = 'lenient'
  AND schema_validation_outcome = 'lenient_validation_bypassed'
ORDER BY created_at DESC
LIMIT 20;
```

### Per-node enforcement breakdown for a run

```sql
SELECT
  node_id,
  attempt_key,
  schema_enforcement_json->>'outcome' AS outcome,
  schema_enforcement_json->>'resolved_profile' AS profile,
  schema_enforcement_json->>'native_output' AS native,
  schema_enforcement_json->>'repair_attempts' AS repairs,
  schema_enforcement_json->>'wasted_attempts' AS wasted
FROM run_node_outputs
WHERE run_id = :run_id
  AND schema_enforcement_json IS NOT NULL
  AND node_id != '__run_meta__'
  AND attempt_key != '__final__'
ORDER BY node_id, attempt_key;
```

### Organisation-wide wasted-attempt rate

```sql
SELECT
  date_trunc('day', r.created_at) AS day,
  SUM(f.enforcement_wasted_count) AS total_wasted,
  SUM(f.enforcement_native_count + f.enforcement_verbatim_count) AS total_validated,
  ROUND(
    SUM(f.enforcement_wasted_count)::numeric /
    NULLIF(SUM(f.enforcement_native_count + f.enforcement_verbatim_count), 0),
    3
  ) AS wasted_rate
FROM run_daily_facts f
JOIN runs r ON r.id = f.run_id
WHERE r.organisation_id = :org_id
  AND f.enforcement_wasted_count IS NOT NULL
GROUP BY 1
ORDER BY 1 DESC;
```

### Repair loop effectiveness

```sql
SELECT
  outcome,
  COUNT(*) AS attempts,
  SUM((schema_enforcement_json->>'repair_attempts')::int) AS total_repairs,
  SUM((schema_enforcement_json->>'wasted_attempts')::int) AS total_wasted
FROM run_node_outputs
WHERE schema_enforcement_json IS NOT NULL
  AND node_id != '__run_meta__'
  AND attempt_key != '__final__'
GROUP BY outcome
ORDER BY attempts DESC;
```

## Schema file lifecycle

### Write path

Schema files are written into the sandbox filesystem at run start, before
the node dispatches to the model backend. See
[Schema Reference: On-disk sandbox contract](../schema-reference.md#on-disk-sandbox-contract)
for the file layout and sanitisation rules.

### Cleanup / retention

Schema files are cleaned up at run terminalization via
`cleanup_schema_contract()`. When a `node_id` is provided, only that
node's `schemas/<node_id>/` directory is removed. When no `node_id` is
provided, the entire `schemas/` directory is removed. The cleanup is
tolerant of absence and never raises.

Schema file retention is tied to run retention: when a run is purged, its
sandbox filesystem (including schema files) is cleaned up as part of the
run lifecycle.

### Orphan cleanup

Orphan `.tmp` files left behind by failed writes are cleaned up after
every `write_schema_contract()` call as a best-effort operation. This
prevents stale temp files from accumulating in the sandbox filesystem.
