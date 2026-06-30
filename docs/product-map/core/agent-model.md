---
id: feat-core-agent-model
prd: 8.2
delivery-tasks:
  - task-prd-generic-agents-criteria
code:
  - backend/src/modulo/db/models/agent.py
  - backend/src/modulo/db/crud/agent.py
  - backend/src/modulo/api/routes/agents.py
depends-on: []
bdd:
  - backend/tests/features/agents/configure.feature
  - backend/tests/features/agents/prompt_versioning.feature
  - backend/tests/features/agents/schema_assignment.feature
  - backend/tests/bdd/steps/test_alpha_agents.py
unit-tests:
  - backend/tests/unit/api/test_agents_endpoint.py
  - backend/tests/unit/api/test_agent_prompt_versioning.py
  - backend/tests/unit/api/test_agent_prompts.py
  - backend/tests/unit/db/test_agent_executable.py
  - backend/tests/integration/crud/test_agent.py
status: partial
---

# Core Agent Model

An Agent is an atomic unit of work: it takes a defined input, applies a
sandboxed prompt against a model backend, and produces a defined output.
Agents are the fundamental building block of pipeline nodes.

Two categories exist:

* **Library agents** — published to the community library and imported via
  copy-to-adapt. They carry a `library_id` FK and inherit trust from their
  source. Schema/prompt pairs are pre-validated by the publisher.
* **Generic (user-defined) agents** — created from scratch by a pipeline
  author with a custom prompt template, schemas, and model backend. These
  are **experimental** per PRD §8.2 and must satisfy documented criteria
  before they can execute in a pipeline.

## Agent Model Fields

- `id`, `organisation_id`, `name`, `description`
- `is_executable` — if `False`, the agent is a template/blueprint (not
  directly runnable as a pipeline node)
- `input_schema_id`, `input_schema_version` — FK to schema version
- `output_schema_id`, `output_schema_version` — FK to schema version
- `prompt_template` — Jinja2 template (rendered via SandboxedEnvironment)
- `prompt_version_history` — list of prior versions with timestamps
- `model_backend_id` — FK to ModelBackend
- `connector_type_refs` — required ConnectorType IDs with operations
- `evals` — list of eval definitions
- `retry_policy` — max_retries, retry_on, backoff
- `token_budget` — optional per-run token limit
- `max_input_length` — input length cap
- `library_id` — nullable FK; non-null = library primitive provenance
- `prompt_always_visible` — if `True`, prompt masking is disabled

## Behaviours

### Database / Model
- [x] Agent model has all fields defined in PRD §8.2
- [x] `is_executable` column defaults to `True`
- [x] `library_id` nullable FK to `library_primitives`; null = generic agent
- [x] Input/output schema FK constraints use `(schema_id, version, org_id)`

### CRUD
- [x] `create_agent()` inserts a new agent with RLS org scoping
- [x] `get_agent()` retrieves by ID with RLS
- [x] `list_agents()` returns paginated results ordered by created_at desc
- [x] `update_agent()` applies field-level updates via `apply_updates()`
- [x] `delete_agent()` hard-deletes and returns success boolean
- [x] Prompt versioning: `add_prompt_version`, `rollback_prompt_version` with
      history preservation

### REST API — `/api/v1/agents`
- [x] `GET /api/v1/agents` — list (paginated)
- [x] `POST /api/v1/agents` — create (201); 422 on validation failure
- [x] `GET /api/v1/agents/{id}` — get by ID
- [x] `PATCH /api/v1/agents/{id}` — update fields
- [x] `DELETE /api/v1/agents/{id}` — delete (204)
- [x] Prompt endpoints: GET list, GET version, PUT rollback, POST diff,
      POST optimize, POST apply
- [x] All endpoints enforce RLS org scoping via `set_rls_org`
- [x] Unauthenticated requests return 401/403

### Generic (non-library) Agent Criteria — PRD §8.2 Experimental
- [x] **Executable generic agents MUST have a description.** If
      `is_executable=True` and `library_id` is null, a non-empty
      `description` is required on create. The description helps pipeline
      authors understand the agent's purpose.
- [x] **Non-executable (template) agents MUST have a description.** If
      `is_executable=False`, a description is always required since these
      agents serve as documentation/blueprints for future work.
- [x] **Library-sourced agents skip generic-agent checks.** Agents with a
      `library_id` inherit trust from their library source and are not
      subject to generic-agent validation.
- [x] **Create returns 422 with specific error message** when description
      is missing on a generic agent.
- [x] **Update validates against the merged state** — if an update would
      leave a generic agent without a description (removing it or making
      an agent non-executable without one), the request is rejected.
- [x] **No-eval warning logged.** Executable generic agents created without
      evals emit a `logging.warning` — advisory only in alpha; intended
      as a hard requirement before production promotion per PRD §15.
- [x] **Generic agents remain runnable** — the criteria are documentation
      and safety gates, not execution blockers (in alpha).

### Test Coverage
- [x] Generic agent missing description returns 422 on create
- [x] Library agent without description succeeds (skips check)
- [x] Non-executable agent missing description returns 422
- [x] Generic agent with description succeeds
- [x] Update removing description on generic agent returns 422
- [x] Update removing description on library agent succeeds
- [x] Making an agent non-executable without description returns 422
- [x] `is_executable` persists and round-trips correctly
- [x] Default `is_executable` is `True`

## Known Gaps

- **Website docs stub missing.** No website docs page exists for the Agent Model feature.
  A stub should be created at `Website/modulo-website/src/docs/agents.md` with a link to
  PRD §8.2.
- **BDD features test old `/api/agents` endpoints.** The feature files at
  `tests/features/agents/` and step defs at `tests/bdd/steps/test_alpha_agents.py` use the
  legacy `/api/agents` path with `modulo.core.pipeline_engine.run_crud` patches. A new
  BDD feature (`tests/bdd/features/agents/crud.feature`) targets the current `/api/v1/agents`
  endpoints but is a smoke-level coverage — full BDD coverage for error paths
  (RLS enforcement, validation edge cases) is tracked separately.
- **Production eval requirement.** PRD §15 states generic agents require
  eval rubric before production promotion. Currently this is a logged
  warning in alpha. When the Eval System (§8.17) ships in v1, the create
  endpoint should reject generic agents without at least one eval
  definition. Tracked by: v1 delivery dependency graph.
- **Schema→prompt construction.** PRD §8.2 mentions automatic prompt
  template construction from novel input/output schema pairs. Not yet
  implemented. When built, the constructed prompt should also pass the
  generic agent criteria validation above.
- **Generic agent promotion workflow.** No UI workflow exists to promote
  a generic agent to a library primitive. The data model supports it
  (setting `library_id`), but there is no "Publish to library" action.
