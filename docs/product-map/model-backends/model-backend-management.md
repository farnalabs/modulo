---
id: feat-model-backends-management
prd: 8.1
delivery-tasks: []
bdd:
  - backend/tests/bdd/features/model_backends/backend_crud.feature
  - backend/tests/bdd/features/model_backends/backend_selection.feature
  - backend/tests/bdd/features/model_backends/backend_health_check.feature
  - backend/tests/bdd/features/model_backends/backend_error_handling.feature
  - backend/tests/bdd/features/model_backends/rate_limiting.feature
unit-tests:
  - backend/tests/unit/model_backends/test_ai21.py
  - backend/tests/unit/model_backends/test_anthropic.py
  - backend/tests/unit/model_backends/test_azure_openai.py
  - backend/tests/unit/model_backends/test_bedrock.py
  - backend/tests/unit/model_backends/test_cohere.py
  - backend/tests/unit/model_backends/test_deepseek.py
  - backend/tests/unit/model_backends/test_fireworks.py
  - backend/tests/unit/model_backends/test_gemini.py
  - backend/tests/unit/model_backends/test_grok.py
  - backend/tests/unit/model_backends/test_groq.py
  - backend/tests/unit/model_backends/test_jan.py
  - backend/tests/unit/model_backends/test_llamacpp.py
  - backend/tests/unit/model_backends/test_lm_studio.py
  - backend/tests/unit/model_backends/test_localai.py
  - backend/tests/unit/model_backends/test_mistral.py
  - backend/tests/unit/model_backends/test_ollama.py
  - backend/tests/unit/model_backends/test_openai.py
  - backend/tests/unit/model_backends/test_openrouter.py
  - backend/tests/unit/model_backends/test_perplexity.py
  - backend/tests/unit/model_backends/test_qwen.py
  - backend/tests/unit/model_backends/test_stub.py
  - backend/tests/unit/model_backends/test_tgi.py
  - backend/tests/unit/model_backends/test_togetherai.py
  - backend/tests/unit/model_backends/test_vertexai.py
  - backend/tests/unit/model_backends/test_vllm.py
  - backend/tests/unit/model_backends/test_watsonx.py
code:
  - backend/src/modulo/api/routes/mcp_setup.py
  - backend/src/modulo/core/mcp_setup_handoff/
  - backend/src/modulo/db/models/mcp_setup_token.py
  - backend/src/modulo/model_backends/base.py
  - backend/src/modulo/model_backends/anthropic/__init__.py
  - backend/src/modulo/model_backends/openai/__init__.py
  - backend/src/modulo/model_backends/ollama/__init__.py
  - backend/src/modulo/model_backends/opencode/__init__.py
  - backend/src/modulo/model_backends/stub/backend.py
  - backend/src/modulo/model_backends/ai21/__init__.py
  - backend/src/modulo/model_backends/azure_openai/__init__.py
  - backend/src/modulo/model_backends/bedrock/__init__.py
  - backend/src/modulo/model_backends/cohere/__init__.py
  - backend/src/modulo/model_backends/deepseek/__init__.py
  - backend/src/modulo/model_backends/fireworks/__init__.py
  - backend/src/modulo/model_backends/gemini/__init__.py
  - backend/src/modulo/model_backends/grok/__init__.py
  - backend/src/modulo/model_backends/groq/__init__.py
  - backend/src/modulo/model_backends/jan/__init__.py
  - backend/src/modulo/model_backends/llamacpp/__init__.py
  - backend/src/modulo/model_backends/lm_studio/__init__.py
  - backend/src/modulo/model_backends/localai/__init__.py
  - backend/src/modulo/model_backends/mistral/__init__.py
  - backend/src/modulo/model_backends/openrouter/__init__.py
  - backend/src/modulo/model_backends/perplexity/__init__.py
  - backend/src/modulo/model_backends/qwen/__init__.py
  - backend/src/modulo/model_backends/tgi/__init__.py
  - backend/src/modulo/model_backends/togetherai/__init__.py
  - backend/src/modulo/model_backends/vertexai/__init__.py
  - backend/src/modulo/model_backends/vllm/__init__.py
  - backend/src/modulo/model_backends/watsonx/__init__.py
depends-on: []
status: partial
---

# Model Backend Management

Model backends are a first-class resource, parallel to connector instances. Every agent depends on a model backend. This covers the entity lifecycle, credential management, health checks, and runtime resolution — the management plane of the model backend subsystem.

## Behaviours

### ModelBackend Entity — fields and validation

- [x] `ModelBackendBase` ABC defines: `invoke()`, `stream()`, `backend_id` property
- [x] `backend_id` follows `"{vendor}/{model_id}"` format
- [x] `id`, `name`, `display_name` fields on DB entity (`model_backends` table)
- [x] `provider` enum with values `ai21 | anthropic | azure_openai | bedrock | cohere | custom | deepseek | fireworks | gemini | grok | groq | jan | llamacpp | lm_studio | localai | mistral | ollama | openai | opencode | openrouter | perplexity | qwen | replicate | tgi | togetherai | vertexai | vllm | watsonx` — DB CheckConstraint + `ModelBackendProvider` enum (28 providers in enum; 26 have backend implementations; `replicate` and `custom` registered in enum but no backend implementation)
- [x] `model_id` string field
- [x] `default_params` JSON column (temperature, max_tokens, timeout)
- [x] `cost_tracking` flag (`enabled`/`disabled` with CheckConstraint)
- [x] `currency` field (default: USD)
- [x] `organisation_id` foreign key (via `OrgScoped`)
- [x] Fernet-encrypted `credentials_ciphertext` column — key never exposed in API responses, only `has_credentials` boolean
- [ ] `health_check` test inference call on save — not yet implemented

### Built-in Backends — alpha provider implementations

- [x] `AnthropicBackend`: wraps `ChatAnthropic(model=model_id, api_key=api_key)`
- [x] `OpenAIBackend`: wraps `ChatOpenAI(model=model_id, api_key=api_key)`
- [x] `OllamaBackend`: wraps `ChatOpenAI` pointed at Ollama's OpenAI-compatible endpoint
- [x] `OllamaBackend` defaults base URL to `http://localhost:11434/v1`
- [x] `OllamaBackend` substitutes `"ollama"` as placeholder API key when `None` is provided
- [x] `OpenCodeBackend`: wraps `ChatOpenAI` pointed at OpenCode's OpenAI-compatible endpoint
- [x] `StubModelBackend`: deterministic test double keyed by normalized input
- [x] All backends expose `invoke(messages)` returning `BaseMessage`
- [x] All backends expose `stream(messages)` returning `AsyncIterator[BaseMessage]`
- [x] `AzureOpenAIBackend`: wraps `AzureChatOpenAI`
- [x] `BedrockBackend`: wraps `ChatBedrock`
- [x] `Ai21Backend`: wraps `langchain_ai21`
- [x] `CohereBackend`: wraps `ChatCohere`
- [x] `DeepSeekBackend`: wraps `ChatOpenAI`
- [x] `FireworksBackend`: wraps `ChatFireworks`
- [x] `GeminiBackend`: wraps `ChatGoogleGenerativeAI`
- [x] `GrokBackend`: wraps `ChatOpenAI`
- [x] `GroqBackend`: wraps `ChatGroq`
- [x] `JanBackend`: wraps `ChatOpenAI`
- [x] `LLamaCppBackend`: wraps `ChatLiteLLM`
- [x] `LmStudioBackend`: wraps `ChatOpenAI`
- [x] `LocalAIBackend`: wraps `ChatOpenAI`
- [x] `MistralBackend`: wraps `ChatMistralAI`
- [x] `OpenRouterBackend`: wraps `ChatOpenAI`
- [x] `PerplexityBackend`: wraps `ChatPerplexity`
- [x] `QwenBackend`: wraps `ChatOpenAI`
- [x] `TgiBackend`: wraps `ChatHuggingFace`
- [x] `TogetherAIBackend`: wraps `ChatTogether`
- [x] `VertexAIBackend`: wraps `ChatVertexAI`
- [x] `VllmBackend`: wraps `ChatOpenAI`
- [x] `WatsonXBackend`: wraps `WatsonxLLM`
- [ ] `ReplicateBackend`: wraps `ChatReplicate` — no backend implementation file exists; enum value registered in `ModelBackendProvider` but factory has no `replicate` case
- [ ] `Custom` backend (user-provided endpoint) — not implemented
- [ ] Provider-specific `invoke()` param forwarding (e.g. `max_tokens` to Anthropic) — not validated
- [x] All 26 provider backend implementations registered in `ModelBackendHub._build_backend()` factory; `replicate` and `custom` have enum entries but no factory case — would fall through to plugin registry or raise `ValueError`

### Credential Management — encryption and rotation

- [x] Credentials stored encrypted with Fernet at rest — `_encrypt()` in `model_backends.py` uses `cryptography.fernet.Fernet`
- [x] API key never exposed in API responses — only `has_credentials: bool` returned (confirmed by `test_create_model_backend_does_not_expose_credentials` and all endpoint tests)
- [x] PATCH endpoint supports API key update — re-encrypts with Fernet on update
- [x] Credentials never enter LangGraph state, checkpoint blobs, OTel spans, or logs (§6.13 credential-in-state rule) — `# nosemgrep: credential-not-in-state` annotation on update path
- [ ] "Rotate credentials" action (separate from PATCH update) creates new encrypted record — PATCH handles key changes but no dedicated rotate endpoint
- [ ] Post-rotation health check fires automatically
- [ ] Old credential continues serving in-flight runs (credential snapshot at run-start)
- [ ] Rotation does not affect active runs

### Health Check — connectivity and auth validation

- [x] `HealthResult` dataclass defined in `base.py` with `ok: bool` and `detail: str`
- [x] `ModelBackendBase.health_check()` method exists in `base.py` with default implementation using `asyncio.wait_for` (timeout 10s)
- [x] Graph validator checks backend health at pipeline save time — `_check_model_backends()` in `graph_validator/__init__.py` produces `MODEL_BACKEND_UNHEALTHY` error when `last_health_check_error` is set
- [x] Graph validator checks backend health at run creation time — same `_check_model_backends` called for run pre-check
- [x] Graph validator returns `MODEL_BACKEND_NOT_FOUND` when pin references non-existent backend
- [x] Graph validator returns `MODEL_BACKEND_INACTIVE` when backend status is not `"active"`
- [x] `last_health_check_at` and `last_health_check_error` persisted on `ModelBackend` entity
- [x] `_openai_compatible_health_check()` in `base.py` — endpoint ping for OpenAI-compatible providers
- [ ] Test inference call on save validates credentials — no route-level health check on create/update
- [ ] Surfaces auth failures with named error (e.g. `authentication_failed`, `quota_exceeded`)
- [ ] Surfaces network errors with endpoint info
- [ ] Health check result cached with 5-minute staleness bound
- [ ] On-demand health check endpoint for manual revalidation
- [ ] No standalone health check API route — health only checked during graph validation

### Deletion Protection — safe removal lifecycle

- [x] `status` column on `ModelBackend` entity (server default `"active"`)
- [x] Graph validator blocks inactive backends — `MODEL_BACKEND_INACTIVE` error when status != `"active"`
- [x] Agent definitions reference `model_backend_id` (UUID FK to `model_backends.id` with `ON DELETE RESTRICT`) — DB-level referential integrity prevents deletion of backends referenced by agents
- [x] PipelineSnapshot stores `model_backend_pins_json` — pinning at snapshot time prevents stale-backend runs
- [x] Hard delete blocked if referenced by any active agent definition — `ON DELETE RESTRICT` on agent FK enforces this at DB level
- [ ] Hard delete blocked if referenced by any PipelineSnapshot associated with a non-terminal run
- [ ] Soft-delete (`status: deprecated`) — `status` column exists but no soft-delete API or deprecation workflow
- [ ] Deprecated backends serve in-progress runs but hidden from new pipeline pickers
- [ ] Deprecated backends cannot be selected for new agent definitions
- [ ] Hard delete requires zero active references — same policy as schema version deletion (§7.3)

### Runtime Resolution — model_id pinning

- [x] `model_backend_pins_json` stored in `PipelineSnapshot` — created by `_resolve_graph_references()` in `pipelines.py`
- [x] Model backends pinned at snapshot creation time — each agent's `model_backend_id` captured in pins list
- [x] Consistent execution across paused/resumed runs — pinning prevents entity updates from affecting in-flight runs
- [x] Graph validator checks pinned backends against live `ModelBackend` entity state at run pre-check — validates status, health, and existence
- [x] Pre-run model backend health check blocks run start on failed backends — `MODEL_BACKEND_UNHEALTHY` error produced by `_check_model_backends`
- [x] Pre-run check blocks run on deleted backends — `MODEL_BACKEND_NOT_FOUND` error
- [x] Pre-run check blocks run on inactive backends — `MODEL_BACKEND_INACTIVE` error
- [ ] Operator updates to ModelBackend entity take effect only on new runs (new snapshots) — architecturally guaranteed by pinning but not explicitly tested
- [ ] Cost computed against pinned `model_id` — no pricing config integration
- [ ] If pinned model_id no longer exists in pricing config, cost falls back to zero with logged warning

### Edge Cases and Error States

- [x] Non-existent backend in pipeline graph → `MODEL_BACKEND_NOT_FOUND` error at save/run time
- [x] Inactive backend in pipeline graph → `MODEL_BACKEND_INACTIVE` error at save/run time
- [x] Unhealthy backend in pipeline graph → `MODEL_BACKEND_UNHEALTHY` error at save/run time
- [x] Health check timeout (10s) → `HealthResult(ok=False, detail="Health check timed out")` in base.py
- [x] Fernet encryption: API key encrypted at create time, re-encrypted at update time
- [x] Credentials never exposed in API responses — `has_credentials` boolean only (confirmed by 10+ unit tests)
- [x] ProgrammingError on any CRUD operation → 501 Not Implemented (all 5 routes)
- [x] Unauthenticated access → 401/403 (confirmed by `test_list_model_backends_unauthenticated_returns_4xx`)
- [x] Not-found on GET/PATCH/DELETE → 404
- [x] Duplicate name on create → 409 (BDD scenario defined, with_for_update() on duplicate check, IntegrityError→409 catch for defense-in-depth)
- [x] IntegrityError on CRUD operation → 409 Conflict (handled on all 5 routes)
- [ ] Concurrent credential rotation and run start — rotation waits for active snapshot release
- [ ] Null `model_id` in snapshot — validation error at run start, not at pipeline save time
- [ ] Backend provider returns unexpected status code — mapped to typed error
- [x] `asyncio.CancelledError` propagated correctly (not swallowed) in `ModelBackendBase.health_check()` — fixed `except Exception`→`except asyncio.CancelledError: raise`
- [x] PATCH with null `api_key` does not crash — guard added to skip encryption when value is `None`
- [x] Hub `initialise()` logs warning when zero backends registered after all instances fail

## Known Gaps

### Resolved (implemented since last QA pass)
- [RESOLVED] **DB entity**: `ModelBackend` model exists with all PRD fields (name, display_name, provider, model_id, credentials_ciphertext, default_params, cost_tracking, currency, owner_team_id, visibility, status, last_health_check_at, fallback_backend_ids)
- [RESOLVED] **Credential encryption**: Fernet encryption confirmed at create and update — API key never exposed in responses, only `has_credentials: bool`
- [RESOLVED] **`model_backend_pins_json`**: Implemented in `PipelineSnapshot` — created by `_resolve_graph_references()`, checked by graph validator at save and run time
- [RESOLVED] **BDD steps**: All 5 feature files (backend_crud, backend_selection, backend_health_check, backend_error_handling, rate_limiting) have step definitions in `test_model_backends.py` — step definitions pass
- [RESOLVED] **REST API**: Full CRUD API at `/api/v1/model-backends` with Fernet encryption, ProgrammingError→501, RLS scoping, and pagination

### Unresolved

- [ ] **No health check on save**: no test inference call when creating or updating a model backend
- [ ] **No soft-delete / deprecation workflow**: `status` column exists but no API to deprecate, no deprecation warning in graph validator (inactive = blocked entirely; no "deprecated but allowed" state)
- [ ] **No cost tracking**: no pricing config integration, no cost calculation against pinned `model_id`
- [ ] **No health check API route**: no standalone `/api/v1/model-backends/{id}/health` endpoint for manual revalidation
- [ ] **No credential rotation endpoint**: PATCH handles key changes but no dedicated "rotate" action with post-rotation health check
- [ ] **No health check result caching**: no staleness bound on health check results
- [ ] **No DB-level unique constraint on name**: `name` uniqueness per org enforced at application level via `with_for_update()` check in create route — no DB UNIQUE constraint for defense-in-depth
- [ ] **No rate limiting on model backend API endpoints**: only general per-endpoint rate limits apply
- [ ] **No multi-backend test coverage**: `fallback_backend_ids` stored on entity but no runtime fallback logic verified in tests
- [ ] **BDD rate_limiting.feature out of place**: rate_limiting.feature lives in model_backends directory but tests general API rate limiting (runs, webhooks, MCP), not backend-specific rate limiting
- [ ] **ReplicateBackend and CustomBackend missing**: `ModelBackendProvider` enum includes `replicate` and `custom` but neither has a backend implementation directory; `_build_backend()` factory has no case for either — would fall through to plugin registry or raise `ValueError`

## QA History

- 2026-07-08 (improve-architecture index 273): Cross-cutting QA. Fixed CRITICAL — `ModelBackendResponse.model_config` used `from_attributes: False` preventing `model_validate()` from ORM; changed to `from_attributes: True, populate_by_name: True` matching all other response models. `created_by` uses `validation_alias="account_id"` which requires `from_attributes=True`. Fixed CRITICAL — frontend provider dropdown sent `"google"` (backed expects `gemini`) and `"together"` (backed expects `togetherai`) — dead controls that always returned 422. Fixed CRITICAL — frontend created/had hardcoded English strings across entire view (add button, form labels, table headers, credentials badge, delete confirmation, action buttons) — wrapped ~30 strings in `$t()` wrappers with new i18n keys. Fixed CRITICAL — frontend error handlers used `String(err)` and `e instanceof Error ? e.message : String(e)` instead of `formatApiError(err)` — would display `[object Object]` on API errors. Fixed MAJOR — added `with_for_update()` to duplicate name check in create route (TOCTOU race prevention). Fixed MAJOR — added `except IntegrityError → 409` catch to create route (FK/constraint violations now return 409 instead of propagating to SQLAlchemyError→503). Fixed MAJOR — added `replicate` and `custom` to `_VALID_PROVIDERS` (were missing from API validation, so frontend provider dropdown options for these returned 422). Removed `replicate` and `custom` from frontend provider dropdown (no backend implementation — ReplicateBackend missing from factory, Custom not implemented). Added 1 new unit test `test_create_model_backend_integrity_error_returns_409` to `test_model_backends_endpoint.py`. Updated product map: added IntegrityError→409 behaviour checkbox, added QA History section. All existing tests pass (unchanged). Merged to main. Status: partial.
- 2026-07-07 (improve-architecture index 313): Cross-cutting QA pass 4. **Fixed CRITICAL** — ModelBackendBase.health_check() did not catch asyncio.CancelledError (a BaseException subclass, not Exception in Python 3.12); added except asyncio.CancelledError: raise before generic except Exception so cancellation propagates correctly. **Fixed MAJOR** — PATCH endpoint crashed with AttributeError when api_key: null was passed (_encrypt(None, ...).encode()); added null guard that treats null api_key as no-op. **Fixed MINOR** — Hub initialise() silently succeeded when all backends failed decryption; added warning log when zero backends registered. Updated product map: added 3 new edge-case [x] checkboxes. All 11 known gaps from previous pass remain unresolved.
- 2026-07-06 (qa-iterate prodmap model-backends): Fixes from code verification. Corrected exception base classes (`BackendNotFoundError(KeyError)`→`BackendNotFoundError(Exception)`, `BackendUnavailableError(RuntimeError)`→`BackendUnavailableError(Exception)`). Corrected `_build_backend` error message to `ValueError("Unknown model backend provider: {provider!r}")`. Corrected UUID fallback handling claim (caught, not uncaught). Fixed logging claim — Hub has 11+ logger calls; real gap is failover events not surfaced via `audit_logger` callback. Updated provider count from 25 to 28 (enum) / 26 (implemented). Added `opencode` to `code:` frontmatter and provider list. Added `custom` to provider list. Updated ReplicateBackend gap to also cover CustomBackend.
- 2026-07-05 (qa-iterate prodmap model-backends): Corrected false claims: ReplicateBackend changed from `[x]` to `[ ]` (no backend implementation file exists; enum-only). "All 26 provider backends registered" changed from `[x]` to `[ ]` (only 25 built-in; `replicate` missing from `_build_backend()` factory). Updated provider count text from "26 providers" to "25 providers; replicate registered in enum but no backend implementation". Added ReplicateBackend missing to Known Gaps.
- 2026-07-03 (improve-architecture index 84): Cross-cutting QA pass 2. Fixed massively stale product map — marked 40+ behaviours [ ]→[x] across DB entity, Fernet encryption, CRUD API routes, PipelineSnapshot pinning, graph validator health checks, and BDD step definitions. Resolved 5 stale known gaps (DB entity, credential encryption, model_backend_pins_json, BDD steps, REST API). Added ReplicateBackend to provider list. Added 11 new known gaps (no health check on save, no deletion protection FK, no soft-delete, no cost tracking, no health API route, no credential rotation endpoint, no caching, no duplicate name constraint, no rate limiting, no multi-backend fallback test coverage, rate_limiting.feature out of place). Added 9 edge case [x] from error path audit (MODEL_BACKEND_NOT_FOUND/INACTIVE/UNHEALTHY, 404, 501, 401/403, credential encryption). Created website docs stub.
- 2026-07-02 (improve-architecture index 53): Cross-cutting QA pass 1. Fixed frontmatter YAML (bdd/unit-tests/code paths). Added 22 missing provider backends. Added 5 BDD feature file refs. Marked health_check method and HealthResult dataclass as [x].
- 2026-07-12 (qa-iterate r2-docs-mb-66): Code verification pass. Corrected stale factory registration count — all 26 implementations registered in `_build_backend()` factory (was claimed "only 24 of 26"). Updated IntegrityError claim — handled on all 5 routes (was claimed "create route only").
