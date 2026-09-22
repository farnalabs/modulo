# Schema Validator Tightening: Lenient to Strict

This guide covers the operator procedure for moving from lenient to strict
schema validation mode. **Flipping the mode is a deliberate, reversible
operator action, never automatic.** The flip guard only advises; it does
not change the mode.

## Background

Modulo ships with `lenient` as the hard default for schema validation. In
lenient mode, validation failures are recorded as warnings but do not fail
the node. In strict mode, validation failures trigger the repair loop
(up to 3 re-prompting attempts) and the node fails if validation cannot
be satisfied.

## Before flipping

### 1. Check the flip guard

The flip guard endpoint examines ALL enforcement records for your
organisation and reports whether flipping is safe:

```
GET /api/v1/runs/schema-enforcement/flip-guard
```

The response includes:

- `safe_to_flip`: `true` when zero lenient-mode warnings exist.
- `lenient_warning_count`: how many warnings would become hard failures.
- `advisory`: human-readable explanation.

**Read the advisory carefully.** A `safe_to_flip: false` verdict means
there are recorded `lenient_validation_bypassed` outcomes that would
become hard failures in strict mode. These are real validation failures
that the repair loop would need to resolve.

### 2. Review affected runs

Use the run-detail enforcement surface to inspect which nodes and
attempts produced lenient-mode warnings:

```
GET /api/v1/runs/{run_id}/schema-enforcement
```

Look for:

- `outcome: "lenient_validation_bypassed"` -- these are the failures that
  would become hard errors in strict mode.
- `validation_errors` -- the specific constraint violations (JSON Pointer,
  constraint type, expected/actual values).
- `wasted_attempts` -- attempts where the sole failure was schema
  rejection (the repair loop consumed budget but produced nothing usable).

### 3. Fix the underlying issues

Before flipping, resolve the validation failures. Common causes:

- **Output shape mismatch:** The model produces output that does not
  match the declared schema. Fix the prompt or adjust the schema to
  match what the model actually produces.
- **Missing required fields:** The model omits fields listed in
  `required`. Strengthen the prompt or remove the field from `required`.
- **Type mismatches:** The model returns a string where the schema
  expects a number, or vice versa. Adjust the schema types or the
  prompt.

The goal is zero `lenient_validation_bypassed` records before flipping.

## Flipping the mode

### Set the mode

The schema validator mode is resolved from (in priority order):

1. `organisations.settings_json["schema_validator_mode"]` (per-org)
2. `system_config` row with key `"schema_validator_mode"`
   (instance-wide)
3. `MODULO_SCHEMA_VALIDATOR_MODE` env var (first-boot seed only)
4. `"lenient"` (hard default)

To set the mode, update the organisation's `settings_json` or the system
config via the admin API or MCP tools. For example, to set instance-wide:

```sql
INSERT INTO system_config (key, value)
VALUES ('schema_validator_mode', 'strict')
ON CONFLICT (key) DO UPDATE SET value = 'strict';
```

Or per-org via the organisation settings endpoint.

### Verify

After flipping:

1. Run a pipeline that previously had lenient-mode warnings.
2. Check the enforcement endpoint to confirm the mode changed:
   ```
   GET /api/v1/runs/{run_id}/schema-enforcement
   ```
   The `schema_validator_mode` field should show `strict`.
3. Confirm that `lenient_validation_bypassed` outcomes no longer appear.
   New outcomes should be `passed_after_repair` (if the repair loop
   resolved the issue) or `repair_exhausted` (if the model cannot
   satisfy the schema).

### If strict mode breaks a pipeline

If a pipeline fails under strict mode, you can revert to lenient:

1. Update the mode back to `lenient` (same configuration path as above).
2. The next run will use lenient mode. No code changes or restarts
   required; the mode is read from the DB at run time.

## Flip guard behaviour

The flip guard is advisory only. Key properties:

- **Never mutates the mode.** It only examines enforcement records and
  returns a verdict. Changing the mode is always a separate, explicit
  operator action.
- **Counts `LENIENT_VALIDATION_BYPASSED` outcomes.** These are the
  records that would become hard failures in strict mode.
- **Operates on accumulated evidence.** The guard examines all
  enforcement records for the organisation, not just recent ones. If old
  lenient warnings exist from before issues were fixed, they still
  count. After fixing underlying issues, the guard will show
  `safe_to_flip: true` once no `LENIENT_VALIDATION_BYPASSED` records
  remain.

## Quick reference

| Action | How |
|--------|-----|
| Check if safe to flip | `GET /api/v1/runs/schema-enforcement/flip-guard` |
| Inspect per-run enforcement | `GET /api/v1/runs/{run_id}/schema-enforcement` |
| Set mode to strict | Update `schema_validator_mode` in org settings or system config |
| Revert to lenient | Update `schema_validator_mode` back to `lenient` |
| Check current mode | Query `runs.schema_validator_mode` on recent runs |

## See also

- [Schema Reference](./schema-reference.md#validation-semantics) for
  validation semantics and outcomes.
- [Schema Enforcement Operations](./operations/schema-enforcement.md) for
  observability queries and the enforcement data model.
