# ADR 018 — Schema Enforcement: Two-Phase Validation Plan

**Date**: 2026-07-22
**Status**: Approved

---

## Context

Modulo's schema validation infrastructure has a critical structural defect: the graph validator's schema compatibility check has never actually executed in production. The _build_schema_pins_map() function expects {node_id, direction, schema_id} tuples but receives a deduplicated {schema_id, version, abstract_name} list from PipelineSnapshot.schema_pins_json. The format mismatch produces an empty map, causing every schema compatibility check to silently skip. Schema mismatches between connected pipeline nodes pass validation without error.

Beyond this immediate defect, the schema system has four related problems:

1. **Version pinning drift**: _resolve_schema_definitions() resolves the latest published schema version, not the version that was pinned at snapshot creation time. Schema changes between snapshot creation and run time silently shift the validation target.

2. **Deletion protection with soft references**: PipelineSnapshot has no FK constraint to SchemaVersion — schema pins are stored in an opaque JSON column. Deletion protection checks use string contains() on a JSON column cast to text, which produces false positives and can be bypassed.

3. **No runtime enforcement**: _validate_against_schema() in the node runner reads output_schema_json from the node definition, but the snapshot builder never sets this field. Runtime output validation is dead code.

4. **No connector schema declarations**: Connectors declare llowed_operations but no data shape, making it impossible to validate connector output before it enters pipeline state.

### Design process

This ADR is the product of six rounds of adversarial design review. Each round surfaced flaws that were addressed before the next round. The final two-phase plan below incorporates all findings from that process.

---

## Decision

Adopt a **two-phase approach** to schema enforcement. Phase 1 fixes the broken infrastructure. Phase 2 extends with runtime enforcement and additional schema surfaces.

### Phase 1 — Fix Schema Validation Foundation (MVP)

Scope: repair the existing schema validation infrastructure without adding new features.

**1a — Per-node schema pins**

Add input_schema_pin and output_schema_pin to PipelineGraphNode:

`python
class SchemaPin(BaseModel):
    schema_id: uuid.UUID
    schema_version: str

    @field_validator("schema_version")
    @classmethod
    def version_must_be_concrete(cls, v):
        if v in ("latest", "*", "") or len(v) > 50:
            raise ValueError(f"schema_version must be concrete, got '{v}'")
        return v

class PipelineGraphNode(BaseModel):
    input_schema_pin: SchemaPin | None = None
    output_schema_pin: SchemaPin | None = None
    output_schema_id: uuid.UUID | None = None        # DEPRECATED — API compat
`

These are optional — existing snapshots without them skip validation.

**1b — Normalized snapshot_schema_pins table**

Create a dedicated table with indexed FKs:

`sql
CREATE TABLE snapshot_schema_pins (
    id UUID PRIMARY KEY,
    snapshot_id UUID NOT NULL REFERENCES pipeline_snapshots(id) ON DELETE CASCADE,
    node_id UUID NOT NULL,
    direction VARCHAR(10) NOT NULL CHECK (direction IN ('input', 'output')),
    schema_id UUID NOT NULL REFERENCES schemas(id) ON DELETE RESTRICT,
    schema_version VARCHAR(50) NOT NULL,
    FOREIGN KEY (schema_id, schema_version)
        REFERENCES schema_versions(schema_id, version) ON DELETE RESTRICT
);
`

Written alongside in-graph-json pins during snapshot creation. The table is the source of truth for deletion-protection queries and rapid lookups.

**1c — Migration from old format**

The migration reads the agent's output_schema_id from existing PipelineGraphNode fields to determine direction (input vs output) for each old-format pin. It does NOT query the live Agent table for current FK values — it only uses data already present in each snapshot's graph_json plus the old schema_pins_json entries.

Key rule: when input_schema_id == output_schema_id (same schema for both directions), the old format has only one deduplicated entry. The migration creates TWO snapshot_schema_pins rows with the same (schema_id, version) but different directions.

Manual nodes' output_schema_id is also migrated from the existing PipelineGraphNode.output_schema_id field. Nodes without any matching old pins are left with None pins.

**1d — Fix validator pin resolution**

_resolve_schema_definitions() queries by (schema_id, version) from the pin, not by latest published. Removes published filter.

**1e — Fix _build_schema_pins_map**

Reads per-node pins from PipelineGraphNode in graph JSON instead of the old flat schema_pins parameter. Emits SCHEMA_CHECK_PARTIAL warning for edges with missing pins.

**1f — Fix _check_input_schema_compatibility**

Consolidates with the same per-node pin format. Removes the separate schema_pins parameter.

**1g — Deep compatibility check (scoped)**

| Check | Rule |
|---|---|
| Required fields | Input's equired fields must exist in output's properties |
| Type promotion | integer → number OK; 
umber → integer NOT OK |
| Nullable output | ["string","null"] → "string" NOT OK |
| Nullable input | "string" → ["string","null"] OK |
| Nullable arrays | Subset check: output values ⊆ input values |
| additionalProperties:false | Output extra field + input dditionalProperties: false = NOT OK (all nesting levels) |
| Nested objects | Recursive type check on nested properties. Depth limit 20. |
| Array items | items.type compatibility. Tuple-form not checked (deferred). |
| Enum | Both present: output values ⊆ input values |

Deferred gaps ($ref, oneOf/nyOf/llOf, if/then/else, pattern, ormat, const, dependentRequired, tuple-form items) emit SCHEMA_CHECK_DEFERRED warnings.

**1h — Fix deletion protection**

Query snapshot_schema_pins table for indexed lookups. System schemas are undeletable.

**1i — Ship system schemas per-org**

Create schema_freeform@v1 ({type: "object"}), schema_text@v1, and schema_trigger_payload@v1 in every org via the startup seeder. Add system: bool flag to Schema model. Use INSERT ... ON CONFLICT DO NOTHING for idempotency.

**1j — Update model_validator**

Accept both output_schema_pin and deprecated output_schema_id on PipelineGraphNode. Reject inconsistent data (both present with mismatched values).

**1k — Grace period for existing pipelines**

When alidate_for_run encounters a schema incompatibility (pre-existing pipeline that was silently accepted before Phase 1):
- Run proceeds with degraded validation status
- Incompatibility is logged with exact field paths
- UI surfaces the warning
- Grace period lasts until Phase 2 makes validation mandatory

**1l — Update ollback_to_snapshot and clone_pipeline**

Both preserve embedded per-node pins from the source graph JSON. create_snapshot_from_live_graph() handles pin population automatically.

**1m — Proof-of-brokenness tests**

1. Incompatible schemas → 0 errors pre-fix, correctly rejected post-fix
2. Backfill direction inference: old-format snapshot → correct per-node pins
3. Migration idempotency: run twice → same pins
4. Compatibility matrix: every rule tested pass + fail
5. Version pinning: v1 vs v2 → pinned to v1 resolves v1
6. Deletion protection: pinned schema → blocked; force-delete → resolver errors
7. Grace period: incompatible pipeline → run proceeds with degraded warning
8. System schema per-org: Agent FK constraints work
9. Manual node validator: valid request passes, missing output rejected

**Phase 1 does NOT include**: making pins required, runtime validation, connector schema declarations, $ref resolution, oneOf/nyOf/llOf traversal, composite composition checks, or frontend changes.

### Phase 2 — Extend (future, dates TBD)

- Make input_schema_pin/output_schema_pin required on all new nodes
- Runtime validation: resolve pinned definitions at snapshot time, validate node output at execution time
- Connector schema declarations on ConnectorBase (optional, opt-in per connector)
- $ref/$defs resolution and oneOf/nyOf/llOf traversal in the deep check
- Composite template schema composition with visited-set cycle detection
- Frontend schema port badges and edge compatibility visualization

---

## Consequences

### Positive

- **Fixes the silent no-op**: the schema compatibility check finally runs, catching mismatches before they reach production
- **Version pinning**: runs validate against the exact schema version that was current at snapshot creation time, not latest published
- **Fast deletion protection**: indexed FK lookups replace JSONB string scanning
- **No breaking changes**: Phase 1 is entirely additive — old snapshots without per-node pins continue with the existing skip behaviour (which is no worse than the current broken state)
- **Gradual migration**: the grace period ensures existing pipelines don't suddenly break

### Negative

- **No runtime enforcement yet**: Phase 1 validates schema compatibility between nodes but never checks that actual data conforms — schemas remain metadata until Phase 2
- **Partial validation**: old snapshots without per-node pins get skipped edges with only a warning
- **Migration complexity**: the direction inference logic requires care for deduplicated schema IDs (when input == output)

### Risks

- **False positives in the deep check**: existing pipelines with schemas that were silently compatible could now fail validation on re-snapshot. Mitigated by the grace period (degraded mode, not hard block).
- **Migration TOCTOU**: the migration runs in a serializable transaction. Schema changes during migration are blocked by the transaction isolation level.

---

## Related ADRs

- **ADR 001 — Agent Execution Environment**: first established the concept of schema version pinning in snapshots
- **ADR 004 — Agent as Bundle**: established schema references by ID + version, not embedded
- **ADR 010 — Integration Tier Classification**: established the tier pattern used for system schema identification
- **ADR 015 — Bundle Format V2**: established the snapshot binding pattern extended here
- **ADR 017 — Constraint Entity (deferred to post-MVP)**: first-class constraint system that this schema enforcement plan may eventually feed into
