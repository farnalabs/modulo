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
  - backend/src/modulo/model_backends/base.py
  - backend/src/modulo/model_backends/anthropic/__init__.py
  - backend/src/modulo/model_backends/openai/__init__.py
  - backend/src/modulo/model_backends/ollama/__init__.py
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
- [ ] `id`, `name`, `display_name` fields on DB entity — not yet implemented
- [ ] `provider` enum with values `anthropic | openai | azure_openai | bedrock | ollama | custom` — not yet implemented
- [ ] `model_id` string field — not yet implemented
- [ ] `default_params` (temperature, max_tokens, timeout) — not yet implemented
- [ ] `cost_tracking` flag — not yet implemented
- [ ] `currency` field (default: USD) — not yet implemented
- [ ] `organisation_id` foreign key — not yet implemented
- [ ] Fernet-encrypted `credentials` column — not yet implemented
- [ ] `health_check` test inference call on save — not yet implemented

### Built-in Backends — alpha provider implementations

- [x] `AnthropicBackend`: wraps `ChatAnthropic(model=model_id, api_key=api_key)`
- [x] `OpenAIBackend`: wraps `ChatOpenAI(model=model_id, api_key=api_key)`
- [x] `OllamaBackend`: wraps `ChatOpenAI` pointed at Ollama's OpenAI-compatible endpoint
- [x] `OllamaBackend` defaults base URL to `http://localhost:11434/v1`
- [x] `OllamaBackend` substitutes `"ollama"` as placeholder API key when `None` is provided
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
- [ ] `Custom` backend (user-provided endpoint) — not implemented
- [ ] Provider-specific `invoke()` param forwarding (e.g. `max_tokens` to Anthropic) — not validated

### Credential Management — encryption and rotation

- [ ] Credentials stored encrypted with Fernet at rest — not yet implemented
- [ ] "Rotate credentials" action creates new encrypted record
- [ ] Post-rotation health check fires automatically
- [ ] Old credential continues serving in-flight runs (credential snapshot at run-start)
- [ ] Rotation does not affect active runs
- [ ] Credentials never enter LangGraph state, checkpoint blobs, OTel spans, or logs (§6.13 credential-in-state rule)

### Health Check — connectivity and auth validation

- [x] `HealthResult` dataclass defined in `base.py` with `ok: bool` and `detail: str`
- [x] `ModelBackendBase.health_check()` method exists in `base.py` with default implementation using `asyncio.wait_for`
- [ ] Test inference call on save validates credentials
- [ ] Surfaces auth failures with named error (e.g. `authentication_failed`, `quota_exceeded`)
- [ ] Surfaces network errors with endpoint info
- [ ] Health check result cached with 5-minute staleness bound
- [ ] On-demand revalidation at pipeline validation time
- [ ] health_check base timeout is hardcoded at 10s in base.py

### Deletion Protection — safe removal lifecycle

- [ ] Hard delete blocked if referenced by any active agent definition
- [ ] Hard delete blocked if referenced by any PipelineSnapshot associated with a non-terminal run
- [ ] Soft-delete (`status: deprecated`) always available
- [ ] Deprecated backends serve in-progress runs but hidden from new pipeline pickers
- [ ] Deprecated backends cannot be selected for new agent definitions
- [ ] Hard delete requires zero active references — same policy as schema version deletion (§7.3)

### Runtime Resolution — model_id pinning

- [ ] `model_id` resolved from `PipelineSnapshot.model_backend_pins_json` — not live entity
- [ ] Ensures consistent execution across paused/resumed runs
- [ ] Operator updates to ModelBackend entity take effect only on new runs (new snapshots)
- [ ] Cost computed against pinned `model_id`
- [ ] If pinned model_id no longer exists in pricing config, cost falls back to zero with logged warning
- [ ] Pre-run model backend health check blocks run start on failed backends

### Edge Cases and Error States

- [ ] Referencing a deleted ModelBackend in a new pipeline returns 404
- [ ] Health check timeout (default: 10s) returns `HealthResult(ok=False, detail="timeout")`
- [ ] Concurrent credential rotation and run start — rotation waits for active snapshot release
- [ ] Null `model_id` in snapshot — validation error at run start, not at pipeline save time
- [ ] Backend provider returns unexpected status code — mapped to typed error

## Known Gaps

- [ ] **No DB entity exists**: ModelBackend CRUD (id, name, provider, credentials, default_params) has no database model, no Alembic migration, no REST endpoints — only in-memory backend wrappers exist
- [ ] **No credential encryption**: credentials are passed as constructor args in plaintext — no Fernet, no `credentials` column
- [ ] **No health check on save**: no test inference call on create/update
- [ ] **No deletion protection**: no DB constraints preventing delete when referenced
- [ ] **No `model_backend_pins_json` in PipelineSnapshot**: runtime resolution model not yet implemented
- [ ] **No cost tracking**: no pricing config, no cost calculation against model_id
- [ ] **BDD steps not wired**: 5 BDD feature files exist (backend_crud, backend_selection, backend_health_check, backend_error_handling, rate_limiting) but have no step definitions or conftest — scenarios not runnable
- [ ] **No REST API**: no routes for CRUD, health check trigger, or credential rotation

## QA History

- 2026-07-02 (improve-architecture index 53): Cross-cutting QA pass 1. Fixed frontmatter YAML (bdd/unit-tests/code paths). Added 22 missing provider backends (Ai21, AzureOpenAI, Bedrock, Cohere, DeepSeek, Fireworks, Gemini, Grok, Groq, Jan, LLamaCpp, LmStudio, LocalAI, Mistral, OpenRouter, Perplexity, Qwen, Tgi, TogetherAI, VertexAI, Vllm, WatsonX) with unit test refs and code paths. Added 5 BDD feature file refs. Marked health_check method and HealthResult dataclass as [x]. Added health_check timeout discrepancy to known gaps. All 205 unit tests pass.
