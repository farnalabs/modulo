# Pipeline graph schema source assessment

## Scope

This assessment covers `PipelineGraphNode`, `PipelineGraphEdge`, and `PipelineGraphUpdate` and the currently committed frontend contract. It does not change the graph wire contract or migrate graph consumers.

## Current contract

The backend Pydantic models in `backend/src/modulo/api/routes/pipelines.py` are the executable graph contract:

- `PipelineGraphNode` contains the complete node field set, including type-specific literals, defaults, constraints, and validators that enforce relationships between fields and `node_type`.
- `PipelineGraphEdge` contains topology fields, routing fields, defaults, and the conditional-edge constraint.
- `PipelineGraphUpdate` contains the graph-wide size, identity, and duplicate-path validators.
- `PipelineGraphResponse` extends the update model with read-only validation and translation-report fields.

The committed `frontend/src/lib/api/schema.ts` is generated from the complete FastAPI OpenAPI document. The graph models reference shared schemas such as `ConnectorBinding`, `SchemaPin`, `CapabilityScope`, `HitlReviewConfig`, `FanOutConfig`, and join specifications. The generated document therefore represents a graph contract closure, not three isolated JSON objects.

The pipeline editor currently consumes graph responses dynamically (`any` / object spreading) and intentionally preserves all backend-accepted node fields on save. This is a compatibility-sensitive round trip: narrowing or replacing that shape would risk dropping fields, defaults, or legacy values.

## Generator and freshness gates

`backend/src/modulo/api/routes/pipelines.py` is imported to produce the OpenAPI document. `scripts/run_generate_api_types.py` then runs the pinned `openapi-typescript@7.13.0` generator to produce `frontend/src/lib/api/schema.ts`. The same generated-file freshness check is duplicated in:

- `.github/workflows/ci.yml` (`schema-freshness` job)
- `.pre-commit-config.yaml` (pre-push schema-freshness gate)

Those gates compare the generated OpenAPI-derived TypeScript file with the committed file. They do not consume a separate graph JSON Schema.

## Assessment

A minimal standalone JSON Schema under a contracts directory could document a snapshot of the graph shape, but it cannot safely become the source for the existing API and frontend types without one of these broad changes:

1. Replacing or wrapping the Pydantic models with generated models, while preserving custom validators, defaults, legacy-read context, and error behaviour.
2. Adding a schema-to-TypeScript generation path alongside OpenAPI generation, then proving that both outputs remain equivalent for the graph model closure and all referenced shared models.
3. Changing the frontend graph save/read consumers to use a new generated graph type and ensuring dynamic round-tripping still preserves every accepted field.

Any one of these would create a second contract-generation system or require a wider graph-contract migration. A hand-maintained JSON Schema would be a third representation and would drift from the executable validators. Generating a JSON Schema from the Pydantic models would be useful as a derived documentation artefact, but it would not make that file the authoritative source and would not remove the existing OpenAPI freshness path.

## Decision

Do not introduce a standalone JSON Schema as a source of truth in this slice. Keep the Pydantic graph models as the executable source and OpenAPI as the existing frontend contract source. A future single-source effort should begin with a complete graph-contract equivalence plan covering the model closure, custom validation semantics, legacy reads, OpenAPI generation, and editor round-trip tests before changing either wire contracts or consumers.

This assessment is intentionally documentation-only: no graph model, generated schema, generator, CI gate, pre-push gate, or frontend consumer was changed.
