# Architecture Guide

Modulo is a self-hosted Agentic Delivery Governance platform for building governed, repeatable AI-assisted software delivery pipelines. This document covers the system architecture, tech stack, key components, data flow, database schema, authentication, and deployment.

## System overview

```
┌──────────────────────────────────────────────────────────────┐
│                    Browser UI (Vue 3 SPA)                     │
│  Standard Theme (light) │ Agent Theme (dark, v1)             │
│  Pinia stores → Composables → Views → Components             │
│  PrimeVue component library                             │
└──────────────────────┬───────────────────────────────────────┘
                       │ HTTP REST + WebSocket (Event Bus)
┌──────────────────────▼───────────────────────────────────────┐
│                   API Layer (FastAPI)                         │
│  Routes → Dependencies → Auth → ViewModel Commands           │
│  MCP Server at /mcp (HTTP + SSE)                             │
└──────────────────────┬───────────────────────────────────────┘
                       │
┌──────────────────────▼───────────────────────────────────────┐
│                  Core Engine (Python)                         │
│  ┌────────────┐  ┌──────────┐  ┌──────────┐  ┌───────────┐  │
│  │ Pipeline   │  │  HITL    │  │  Eval    │  │ Trigger   │  │
│  │ Engine     │  │ Manager  │  │  Engine  │  │ Engine    │  │
│  └────────────┘  └──────────┘  └──────────┘  └───────────┘  │
│  ┌────────────┐  ┌──────────┐  ┌──────────┐  ┌───────────┐  │
│  │ Connector  │  │  Model   │  │  Audit   │  │ Feedback  │  │
│  │ Hub        │  │BackendHub│  │  Logger  │  │ Manager   │  │
│  └────────────┘  └──────────┘  └──────────┘  └───────────┘  │
│  ┌────────────┐  ┌──────────┐  ┌──────────┐  ┌───────────┐  │
│  │ Notifier   │  │ Runtime  │  │  Schema  │  │ Library   │  │
│  │            │  │ Provider │  │ Registry │  │ Service   │  │
│  └────────────┘  └──────────┘  └──────────┘  └───────────┘  │
└──────────────────────┬───────────────────────────────────────┘
                       │ LangGraph (StateGraph execution)
                       │ SQLAlchemy async (asyncpg)
┌──────────────────────▼───────────────────────────────────────┐
│              PostgreSQL 16 + Redis 8                          │
│  Models → Migrations → RLS → LangGraph checkpoints            │
│  SAQ worker jobs (Redis-backed)                                │
│  Rate limiting (Redis token bucket or in-memory)              │
└──────────────────────────────────────────────────────────────┘
```

## Tech Stack

| Layer | Technology | Purpose |
|-------|-----------|---------|
| **Backend** | Python 3.12+ | Runtime |
| **API framework** | FastAPI | REST + WebSocket + MCP server |
| **Graph execution** | LangGraph (StateGraph) | Pipeline agent orchestration |
| **ORM** | SQLAlchemy 2.0 (async) | Database access |
| **Migrations** | Alembic | Schema versioning |
| **Task queue** | SAQ + Redis | Async job processing (required for multi-replica) |
| **Auth** | PyJWT[crypto], authlib (v1) | JWT, OAuth 2.0 |
| **LLM SDKs** | anthropic, openai | Model backend integrations |
| **Observability** | OpenTelemetry | Tracing, metrics |
| **Frontend** | Vue 3 + TypeScript | SPA |
| **State** | Pinia | Client-side state |
| **UI primitives** | PrimeVue | Component library (themed via `primevue-theme.ts` token bridge) |
| **Routing** | Vue Router | Client-side routing |
| **Styling** | CSS custom properties | Theming (standard/agent) |
| **Database** | PostgreSQL 16 | Primary data store |
| **Cache/queue** | Redis 8 | SAQ broker, rate limiting |
| **Container** | Docker Compose | Local dev, production |
| **Orchestration** | Docker Compose + Fly.io | Managed hosting (app.modulo.run) / self-hosted single-server |

## Key Components

### API Layer (`modulo/api/`)

FastAPI application providing REST endpoints, WebSocket event streaming, and the Remote MCP server at `/mcp`. Implements the ViewModel pattern – every user action maps to a named command. Routes are thin: they validate input, resolve dependencies (auth, org context, PlanContext), and delegate to core engine services.

Includes:
- CORS middleware (configurable via `CORS_ORIGINS`)
- Rate limiting middleware (Redis token bucket or in-memory fallback)
- OTel instrumentation middleware
- MCP server adapter (HTTP + SSE transport)

### Pipeline Engine (`modulo/core/pipeline_engine/`)

Built on LangGraph's `StateGraph` with `dict[str, Any]` state. Each pipeline snapshot compiles to a StateGraph at run-start. Node types: `agent`, `sandbox_agent`, `manual` (human output), `composite` (expand-only), `router` (ordered JMESPath rules + `default`, lowers to the conditional-edge compile path, FAR-402 P1), `hitl` (human-in-the-loop gate that compiles to the existing synthetic-gate path, FAR-402 P1), `join` (fan-in for scatter/gather patterns), `dispatch` (fires a CI-runner operation through a connector binding, FAR-1141), and `connector` (an API-authorable node type whose whole job is its `connector_binding`: the binding is routed to the bound connector by `make_connector_fn`, FAR-2089). Edges carry HITL gate config, rejection routing, or a `loop`/`conditional`/`normal`/`reject` edge type.

Key design:
- Compiled graphs cached by `(pipeline_id, snapshot_id)` with LRU eviction
- `run_context` and `artifact` are sibling keys in state – context-setter-only write enforcement
- Human/manual nodes produce `interrupt()` in LangGraph
- `@cancellable_node` decorator wraps every node for graceful cancellation and per-node timeouts (`asyncio.wait_for`)
- Pipeline nesting max depth: 3 levels

### Eval Engine (`modulo/core/eval_engine/`)

Post-node automated quality checks. Runs before any HITL gate check on the same edge. Supports six eval types (see `docs/evals.md` for the full contract, including which types are trustworthy for correctness claims):

| Type | Description |
|------|-------------|
| `model_judge` | LLM-as-judge – passes agent output to a model for scoring (soft signal only, never a correctness gate) |
| `regex` | Pattern match against output (shape only) |
| `json_schema` | Validate output against a JSON Schema (shape only) |
| `custom_function` | User-defined Python function |
| `guardrail` | Deterministic data-safety check on the input payload (not routed through `EvalEngine`) |
| `human_set` | Registered, versioned, human-authored assertion set (the trustworthy correctness path) |

Each eval has a pass threshold and failure behaviour: `warn` (soft – run continues) or `block` (hard – run fails at this node). Eval results feed into the Feedback System.

**Reviewer Independence Note (FAR-2031):** `model_judge` evals may share a model backend with the node under evaluation – the `ModelBackendHub` decrypts credentials once per run and the same backend ID can be referenced by both the node and its eval. This is acceptable because `model_judge` is explicitly a soft-signal eval (default `failure_behaviour: "warn"`), the evaluated content is wrapped with a guard instruction and delimiters to prevent prompt injection, and `human_set` evals provide the trustworthy correctness path. For correctness-critical gates, use `human_set` evals.

### HITL Manager (`modulo/core/hitl_manager/`)

Manages Human-in-the-Loop gates using LangGraph's `interrupt()`. Atomic claim semantics via `SELECT ... FOR UPDATE` on `hitl_claims` table. Claim tokens are opaque random strings (alpha) or short-lived JWTs (v1).

Features:
- `human_only` flag – blocks LLM approval via MCP; defaults to `true` (FAR-609) so every gate is human-only unless it explicitly opts out, and non-browser credentials cannot claim or decide such gates (REST claim route + MCP review_hitl claim both enforce it). Reject is not an agent escape hatch: it requires a claim token, and non-browser principals can no longer claim a default-human_only gate, so only a principal already holding a claim can reject; agent-only runs on default gates require browser-human intervention (intended policy)
- `required_team_id` – restricts claims to specific team members
- Claim expiry background job (default: 60s interval, Postgres advisory lock for single-worker execution)
- `manual` node type – same as HITL but human provides full output
- `hitl` node type (FAR-402 P1) – a draggable human-in-the-loop gate; compiles to the same synthetic-gate path as a legacy edge-level HITL gate. `manual` remains the non-gating human-output step.
- Review window (FAR-1257) and approaching-deadline warning (FAR-1270) – the effective window is resolved ONCE at gate fire (pipeline override > org default > `HITL_CLAIM_TTL_SECONDS` + `HITL_REVIEW_CANCEL_GRACE_SECONDS`, clamped to 60s..7d) and stamped as `hitl_claims.terminalize_at`; the dispatcher-reconcile terminaliser cancels the run at that deadline, and a fire-once `hitl_deadline_warning` cron emails the gate-fire recipients at the halfway lead so a human can act first. Both mechanisms are described under "Run admission and healing" below.

**Decision-payload contract (normative, FAR-541):** every resume decision is a dict `{"action": <verdict>, "review_id": <the identity it resolves>}` plus any per-action members (`output`, `modified_output`, `reason`, `notes`). `HITLManager._decide` is the single stamp authority: it stamps a payload that lacks `review_id` with the claim row's gate id and refuses (422) a payload stamped for a *different* gate; call-site stamps (API routes, MCP) remain because they feed the direct `executor.resume` injection that bypasses `_decide`. A decision is honoured ONLY by the gate/node its stamp names: every consumer verifies the stamp against its own identity and fails closed on a missing/foreign stamp (re-interrupt, never resume):

| Writer | Stamp (`review_id`) | Consumer | Recognized actions |
|---|---|---|---|
| `POST /runs/{id}/hitl/{gate}/approve`, `/approve-with-modification`, `/reject`, `/deliver-manual`; MCP `review_hitl` | the gate id from the URL | `_hitl_review_resume_result` | `approved` (incl. `modified_output`), `rejected`, `deliver_manual` |
| `POST /runs/{id}/manual/{node}/submit` | the manual node's id | `_manual_node` | any dict payload stamped with this node's id completes the node (the documented writer is `manual_output` (+ `output`)) |
| `POST /runs/{id}/nodes/{node}/recover` (operator break-glass) | the run's pending claim row's gate id (node id for manual nodes; the guardrail gate id for conformance blocks); unstamped when no undecided row exists | `_manual_node` / `_handle_conformance_resume` | `skip`, `replay` |
| Conformance override via HITL API | the blocked node id or the block's guardrail gate id | `_handle_conformance_resume` | `approved`, `deliver_manual` (override); `rejected` fails closed |

Interrupt payloads carry the same identity: the gate node interrupts with its `review_id`, a manual node with `review_id: <node_id>`, a conformance block with the block's guardrail gate id. The executor keys the pending `hitl_claims` row on that `review_id` verbatim. The dispatcher reconcile resumes an `awaiting_human`/`claimed` run ONLY per this scoping matrix: claimed-undecided, skip (under the `uq_hitl_claims_run_gate` `UNIQUE (run_id, review_id)` constraint a claimed-undecided row and a committed decision for the same gate cannot coexist; crash recovery for claimed runs routes through the no-undecided-rows branch once the decision commits); unclaimed undecided row, conservative skip; no undecided rows, crash-recovery resume when the decision's stamp routes it to a consumer that accepts it. `hitl_review_*`/guardrail identities accept only the verdict actions; MANUAL-node identities also accept a committed `manual_output` with its `output` (legacy pre-stamping rows are stranded by design, at most the 2026-09-02 incident cohort; ops remedy is a manual DB stamp or ticket, no backfill migration). Recover-node refuses HITL gate targets (422), gate decisions must go through approve/reject; user node ids squatting the reserved `hitl_review_` prefix are rejected at graph-validation time.

**Gate coalescing (FAR-604 D4):** when a run reaches a HITL gate and an OPEN gate (undecided + unclaimed, same gate id) already covers the same work item on ANOTHER run of the pipeline, matched via the webhook coalesce key stamped on `runs.input_payload`, the gate is NOT raised twice. If the entity SHA (`runs.input_hash`) is unchanged, the duplicate run is terminalised `failed`/`executor_superseded` and the existing gate decides for the work item (the model does not support multiple runs per gate, `uq_hitl_claims_run_gate`, so reuse means skipping the duplicate gate). If the SHA changed, the old gate is auto-closed with a system-committed `rejected` decision (loudly audited as `hitl.gate_superseded`) and the old run, if parked, un-parks so the committed-decision resume machinery routes it through the gate's reject route when one is configured. With no reject route (and no explicit `on_reject: proceed`) the old run now ENDS `rejected` with `error_code=hitl.superseded` (FAR-1487, labelled "Superseded by a newer version" and never counted as a human reject), while the new run raises fresh. Claimed gates are never superseded (a human holding the claim is mid-review; the claim TTL + a later raise close the loop).

### Connector Hub (`modulo/connectors/`)

Abstraction over external tool integrations. Each connector has a concrete `ConnectorType` id (e.g. `github`, `shell`, `slack`); the table below groups them by family for readability. ConnectorInstance is a configured, authenticated binding. ConnectorHub decrypts credentials once at run-start into a run-scoped context object – credentials never enter LangGraph state, checkpoints, OTel spans, or logs.

| Connector | Family | Operations |
|-----------|------|------------|
| `FilesystemConnector` | `git-host` | read/write files, git commit/push |
| `GitHubConnector` | `git-host` | read/write via API, create PR |
| `GitLabConnector` | `git-host` | read/write via API, merge requests |
| `BitbucketConnector` | `git-host` | read/write via API |
| `GiteaConnector` | `git-host` | read/write via API |
| `AzureReposConnector` | `git-host` | read/write via API |
| `ShellConnector` | `shell` | run commands in a runtime-provider workspace |
| `SlackConnector` | `messaging` | send messages, search channels |
| `DiscordConnector` | `messaging` | send messages |
| `MicrosoftTeamsConnector` | `messaging` | send messages |
| `JiraConnector` | `issue-tracker` | create/search/update issues |
| `LinearConnector` | `issue-tracker` | create/search/update issues |
| `TrelloConnector` | `issue-tracker` | create/search/update cards |
| `AsanaConnector` | `issue-tracker` | create/search/update tasks |
| `MondayConnector` | `issue-tracker` | create/search/update items |
| `ShortcutConnector` | `issue-tracker` | create/search/update stories |
| `YouTrackConnector` | `issue-tracker` | create/search/update issues |
| `NotionConnector` | `documentation` | read/write pages and databases |
| `ConfluenceConnector` | `documentation` | read/write pages |
| `DropboxPaperConnector` | `documentation` | read/write pages |
| `PagerDutyConnector` | `incident-management` | trigger/acknowledge/resolve incidents |
| `OpsgenieConnector` | `incident-management` | create/acknowledge/resolve alerts |
| `SentryConnector` | `error-tracking` | list/search issues, create events |
| `DatadogConnector` | `monitoring` | query metrics, create monitors |
| `GrafanaConnector` | `monitoring` | query dashboards and alerts |
| `SonarQubeConnector` | `monitoring` | query project quality gates |
| `SnykConnector` | `monitoring` | list vulnerabilities |
| `RestConnector` | `rest` | verb-agnostic HTTP read/write against a declared endpoint (see `docs/rest-connector.md`) |
| `N8NConnector` | `rest` | trigger n8n workflows |
| `JenkinsConnector` | `ci-cd` | trigger/query builds |
| `CircleCIConnector` | `ci-cd` | trigger/query pipelines |
| `BuildkiteConnector` | `ci-cd` | trigger/query builds |
| `AzurePipelinesConnector` | `ci-cd` | trigger/query pipelines |
| `TeamCityConnector` | `ci-cd` | trigger/query builds |
| `NpmConnector` | `package-manager` | query package metadata |
| `PyPIConnector` | `package-manager` | query package metadata |
| `OnePasswordConnector` | `secrets` | read secrets |
| `AzureKeyVaultConnector` | `secrets` | read secrets |
| `SharePointConnector` | `documentation` | read/write files and pages |
| `CodeClimateConnector` | `quality` | query code quality metrics |
| *(40 concrete connectors; 43 `ConnectorType` enum members including the provider-dispatched `ci-runner` / `ticket-tracker` families and `custom`; see `modulo/connectors/`)* | | |

#### Integration Validation Levels (FAR-935)

Every connector type and model backend provider resolves to a validation level: a four-tier classification of how thoroughly the integration has been tested:

| Level | Meaning |
|-------|---------|
| `unit-only` | Hand-written mocks / SDK patched; no real server contacted |
| `contract-recorded` | Replaying a recorded real interaction |
| `self-hosted-e2e` | Exercised against a real container-hosted server in CI |
| `canary-green` | Live call against the real vendor API succeeded recently |

The baseline (ceiling) for each integration type is defined in a static map in `core/validation_level.py`. The current level is written to the DB by the health/canary sweep (`core/connector_hub/health_sweep.py`), which mirrors the `last_health_check_at` pattern: a successful canary writes the baseline level, while a failed canary degrades the level one step below. The level is surfaced on `get_integration_status` (MCP tool), the connectors REST API, and the model-backends REST API.

### Model Backend Hub (`modulo/model_backends/`)

Registered LLM provider wrappers. Agents bind to a model backend at pipeline-save time; `model_id` is resolved from `PipelineSnapshot.model_backend_pins_json` at run time – not the live entity – ensuring consistency across pauses/resumes.

Model backends stay first-class for Runner nodes too (ADR 029): once D6 lands, a Runner agent's model credentials will bind as per-agent env vars at dispatch, so no standing Modulo credentials enter the workspace (pending D6; today's dispatch injects standing host credentials).

| Provider | Status |
|----------|--------|
| Anthropic Claude | Alpha |
| OpenAI GPT | Alpha |
| Azure OpenAI | V1 |
| Bedrock | V1 |
| Ollama | V1 |
| DeepSeek | V1 |
| Gemini | V1 |
| Grok | V1 |
| Groq | V1 |
| Mistral | V1 |
| Cohere | V1 |
| Vertex AI | V1 |
| Together AI | V1 |
| OpenRouter | V1 |
| Perplexity | V1 |
| Fireworks | V1 |
| Ai21 | V1 |
| Qwen | V1 |
| WatsonX | V1 |
| vLLM | V1 |
| TGI | V1 |
| LLamaCpp | V1 |
| LM Studio | V1 |
| LocalAI | V1 |
| Jan | V1 |
| OpenCode | V1 |
| *(26 production providers plus the `stub` test double (27 directories); see `modulo/model_backends/`)* | |

### Trigger Engine (`modulo/core/trigger_engine/`)

Accepts manual, webhook, cron, polling, and agent_signal trigger types. Creates Run records and initiates pipeline execution. Webhook flood protection via Postgres `SELECT ... FOR UPDATE SKIP LOCKED`. Payload deduplication via `webhook_dedup_hashes` table with configurable window.

### Audit Logger (`modulo/core/audit_logger/`)

Immutable event recording for all state-changing actions. Written in alpha; viewer/export is team-gated. All events carry `organisation_id`, `actor_id`, `action`, `resource_type`, `resource_id`, and `timestamp`. An export can be emitted as **offline-verifiable evidence**: an Ed25519-signed canonical manifest over the exported files and the re-derived chain result, publishable with the org's verification key and checkable with only the artefact + key; see [`docs/offline-verifiable-evidence.md`](offline-verifiable-evidence.md).

### Compliance Control Mapping (`modulo/compliance/`)

Maps Modulo's controls and evidence to the frameworks buyers actually test - SOC 2 Trust Services Criteria (CC6, CC7, CC8 plus the evidence layer), ISO/IEC 42001 clauses touching change approval, oversight and traceability, and the EU AI Act obligations that apply to a system changing software under human oversight. The mapping content is versioned YAML shipped in the product (`backend/src/modulo/compliance/mappings/`, `pack_version` + a deterministic content hash), not a website document, so it moves with the release it describes. Coverage is attributed per row and per framework (`satisfied` / `supported` / `customer-responsibility` / `gap`) and every row states the provenance of its evidence (`observed` / `derived` / `customer-supplied`); the loader rejects a pack that claims everything, presents a customer control as Modulo's, or drops a required SOC 2 clause.

`GET /api/v1/compliance/evidence-index` (permission `compliance.evidence_index.read`, admin) generates the per-instance evidence index: the audit chain, run snapshots, HITL decisions (including human-only approvals), eval results, autonomy promotions and demotions, the release SBOM and the build identity - each keyed to the mapping rows it satisfies, each with its identifiers, location and provenance. A mapped artefact with no records is reported `absent` with a reason and named in `missing_artifact_types`; it is never omitted. The index records the pack version and content hash that produced it. See [`docs/security/compliance-control-mapping.md`](security/compliance-control-mapping.md).

### Notification System (`modulo/core/notifier/`)

Push notifications (WebSocket events) and outbound webhooks. Per-endpoint HMAC-signed delivery with 3 retries and dead-letter logging. Endpoints auto-disable after repeated failures. Team-scoped notification endpoints.

### Runtime Provider Hub (`modulo/core/runtime_provider/`)

Agent execution environments for the Runner tier (ADR 029: Agent Execution Tiers + the Bundled Runner). Modulo has exactly two node execution mechanisms: the **Inline Prompt** (`node_type: agent`), an in-process model call in the SAQ worker resolved through the Model Backend Hub with no isolation, and the **Runner** (`node_type: sandbox_agent`), where the agent runtime executes inside a provisioned workspace (provision -> execute -> collect structured output). The RuntimeProvider ABC (parallel to ConnectorHub/ModelBackendHub) resolves the EnvironmentProfile for a Runner dispatch deterministically (delivered by D2): an explicit `provider_hint` or `provider_type` match wins, and anything unresolvable raises `ProviderNotConfiguredError` naming the env var that would register the provider; there is no silent fallback. Providers: `local` (always registered, host processes; its provider-neutral `workspace_metadata` is ignored), `e2b` (registered when `MODULO_E2B_API_KEY` is set; metadata maps to E2B sandbox metadata), `runner_docker` (registered when a `MODULO_RUNNER_*` variable or a Docker endpoint (`MODULO_DOCKER_HOST`/`DOCKER_HOST`) is configured; `docker` and legacy `local_docker` are explicit aliases of the same Docker tier), and `kubernetes` (alias `k8s`; registered when `MODULO_KUBERNETES_ENABLED` is a non-falsy value; workspace pods run in `MODULO_KUBERNETES_NAMESPACE`, default `modulo`, under ServiceAccount `MODULO_KUBERNETES_SERVICE_ACCOUNT`, default `default`). Runners come in three packagings of the same tier: **Bundled Runner (Docker)** (the `runner_docker` provider ships with D2; D4 completes it with the first-party runner image and the compose overlay behind a filtered socket-proxy), **remote Docker** (the same provider pointed at a remote engine via `MODULO_DOCKER_HOST`), and **External Runner (E2B)** (the operator's own E2B account via `AsyncSandbox.create`), plus the bare `local` provider tier, counted by the capacity gate alongside Docker. Hubs are fresh per `build_hub()` factory call (no singleton) and provider-owned clients are released via `aclose()`. D2 removed the unused WorkspaceLease scaffolding, including its API reader (FAR-587): workspace state lives in `runs.sandbox_dispatch_state`, and `GET /runs/{run_id}/workspace-lease` answers a deliberate 410. D8 will replace the dispatch-time capacity check with an atomic advisory-locked gate accounting Runner capacity by run dispatch-state.

E2B direct-path inventory, sanctioned-site list, and the rewire plan that moved every E2B dispatch onto the RuntimeProvider ABC (FAR-1050 deliverable 1). The `MODULO_E2B_VIA_PROVIDER` flag the plan originally gated on was **retired by slice R6 (PR #1033)**: the provider path is now unconditional, and `via_provider` survives only as a historical attribution field on the dispatch marker and node telemetry: [`docs/design/e2b-provider-conformance-rewire.md`](design/e2b-provider-conformance-rewire.md).

#### Bundled Runner (Docker) packaging (D4, FAR-590)

The Bundled Runner is the self-hosted default Runner packaging: a first-party `modulo-runner:opencode` image (`deploy/docker/runner-opencode.Dockerfile` – digest-pinned base, version-pinned opencode via the `OPENCODE_VERSION` build arg, non-root `runner` uid 1001, tini PID 1) executed as a hardened workspace container on the deployment's own Docker engine.

- **Dispatch adapter (`modulo/core/bundled_runner/runner_dispatch.py`)** – the sandbox_agent dispatch branches on the PIPELINE-LEVEL bound profile's provider type (the same-org-enforced `PipelineSnapshot.environment_profile_id`, consumed at dispatch). `runner_docker` routes through a per-dispatch `build_hub()`-resolved Docker provider with both `sandbox_mode` values supported; the E2B path is untouched and gains a LOUD dispatch-time timeout validation (GraphValidator parity at <=3300, no silent clamp). The D4 upgrade rule: pipeline-level refs to providers that were never dispatch-relevant (`local`, legacy-inert `local_docker`) raise a typed `SandboxDispatchUnboundError` at dispatch instead of silently activating old bindings.
- **Streaming exec primitive** – the RuntimeProvider ABC gains `exec_command_stream` (async decoded chunks + a kill handle, `ExecProcess`) alongside the collect-then-return `exec_command`. Script-mode's live-log drain and stall/no-output detection work on Docker exactly as on E2B; an engine/proxy drop mid-stream surfaces as a stream ERROR – the dispatch classifies it retryable and a zero exit code is NEVER fabricated. A stall (no output within the stall window) or a total timeout fires the kill handle.
- **Workspace hardening (at provision)** – non-root user (uid 1001 on first-party images), read-only rootfs + tmpfs `workdir`/`/tmp` (512m/128m), dropped capabilities + `no-new-privileges`, 1.0 CPU / 1 GiB, a dedicated workspace bridge network (never the compose/backend network; per-profile `none` egress opt-in), and provider-neutral `workspace_metadata` mapped to container Labels: `modulo.run.id`, `modulo.org.id`, `modulo.node.id`, plus the machine deployment-identity label (`modulo.machine.id`, from `MODULO_RUNNER_MACHINE_ID` with a hostname fallback) and a creation-marker label.
- **Compose overlay (`deploy/compose/runner.yml`)** – opt-in via the `runner` compose profile. It adds a digest-pinned **filtered socket proxy** (the linuxserver/socket-proxy class; per-category allowlist derived mechanically from the backend's real Docker API surface – containers list/create/start/stop/remove + inspect + exec-create, exec-start/inspect, images inspect/list/create, ping/version/info; no networks endpoints, the workspace network is compose-defined) as the DEFAULT Docker endpoint via `MODULO_DOCKER_HOST`, and guarantees the dedicated `modulo-runner-workspace` bridge network exists (compose never creates unreferenced networks under `--profile` – the holder service references it). Workspaces never join the compose network, so a provisioned workspace cannot reach the proxy endpoint; the proxy publishes no host ports. Full posture + residual exposure: [`docs/security/bundled-runner-trust-boundary.md`](security/bundled-runner-trust-boundary.md); setup: [`docs/security/bundled-runner-operator-guide.md`](security/bundled-runner-operator-guide.md).
- **Orphan reconciler (`runner_reconciler.py`, system cron every 5 min)** – leak repair without WorkspaceLease: lists labelled workspace containers (machine-scoped by the deployment-identity label), cross-references active runs (fail-safe: ANY cross-reference error aborts the sweep destroying nothing and emits `runner.reconciler.sweep_aborted`), destroys orphans older than the 5-min grace period with a destroy-path false-positive re-check (`runner.reconciler.suspected_false_positive` is the D4 rollback signal), applies the 24h max-lifetime backstop regardless of run state (`runner.workspace.reclaimed_max_lifetime`), and runs in a LOG-ONLY soak mode until `RUNNER_RECONCILER_DESTROY_ENABLED=true`. Liveness rides the shared sweep-stats contract (`saq:cron:stats:runner_workspace_reconcile`), surfaced as the advisory `runner_workspace_reconcile` check on `/healthz/ready`.
- **Seeded Environment Profile "Bundled Runner (Docker)"** – provider `runner_docker`, per-minor release-advanced digest constant (`modulo/db/bundled_runner_template.py`; migration 0191 mirrors it and the release job's digest-drift guard asserts they match), hardening + network preset, and `persistence_policy` LOCKED to `ephemeral` (CRUD validator rejects `retained`/`cache` for `runner_docker`, dispatch re-checks). Seeded per org at org-creation; orgs predating the hook receive it via the one-time backfill migration 0191, which re-points the legacy `modulo-dev` `local_docker` row to the Bundled Runner. Template drift is computed live against the shipped constants (`template_drift_status`) – divergent rows surface "shipped template updated - apply" (D5 UI) instead of being silently re-applied. The apply refreshes the image reference only while the row still carries the release placeholder digest (or no image); an operator-set image reference survives and is not reported as drift.
- **Docker-marked acceptance suite** – `backend/tests/docker/` (marker `docker`, run explicitly with `pytest -m docker`; `MODULO_RUNNER_DIND_TESTS=1` opts into the dind engine-kill strip) verifies hardening, streaming live output, container/engine kill classifying retryable (never `ExecResult(0)`), destroy, reconciler orphan destroy + active-run spare, and the proxy allowlist matrix with the runtime completeness assertion (the exercise must trigger no proxy rejection). CI/compose wiring of this suite plus the GHCR publish job (structure test, trivy/grype gates, digest-drift guard, cosign/SBOM) is the D4 GA/CI follow-up item.

### Auth System (`modulo/auth/`)

Authentication and authorization – JWT, API keys, OIDC/SAML (v1), Basic Auth (alpha). Dual-layer scope enforcement for MCP (middleware + ViewModel command layer). See dedicated section below.

### Schema Registry (`modulo/core/schema_registry/`)

Versioned JSON Schema definitions (Draft 2020-12). Schemas are org-scoped, versioned (semver), reusable, and composable. Abstract schemas enable type-constraint matching during workflow import. Schema inference generates draft schemas from sample connector data.

### Library Service (`modulo/core/library_service/`)

Manages the local and community library of reusable primitives (agents, schemas, workflows, integrations). Community primitives are Ed25519-signed. Copy-to-adapt via `CopyToAdaptWizard` UI component (ownership picker + optional binding step).

### Declarative Configuration CLI (`modulo/cli/apply/`) – FAR-681

`modulo apply -f <config.yaml>` applies an org's schemas (+ versions), model backends, pipelines and triggers to a live deployment from one YAML file (`api_version: modulo.dev/v1`), driven by `MODULO_URL` + `MODULO_API_KEY` (bearer `mk_` org key). Planning is name-based upsert (RLS-bound to the key's org): each entity chooses created / updated / unchanged / blocked from a canonical managed-field hash, so rerun is idempotent and runtime state (`next_fire_at`, `streak_epoch`, ...) never causes drift. Keys: entities apply in dependency order with per-entity containment; secrets are refs-only (`${env:VAR}` / `secretref://<key>` -- inline literals are a validation error; the server masks stored secrets, so `--refresh-secrets` re-sends trigger configs whose secrets rotated); backend writes are health-check-verified; `--dry-run/--plan` reports without writing; `--diff` is a read-only drift report (plan-shaped, labelled `mode=drift`, with graph node/edge breakdown for drifted pipelines) used as a CI gate -- exit 0 when the org matches the config, exit 1 on drift (created/updated/blocked), and real apply exits 1 on any blocked/failed entity.

#### Git-sourced content refs (FAR-220)

A `sandbox_agent` node's content fields (`agent_prompt`, each `agent_commands` item, and `script_command`) may reference a file tracked in a git repository instead of inlining the content (the "checkout-and-execute" / "fetch-and-render" pattern, productised):

```
git+<repo-url>[@<ref>]#<path>
```

- `git+` marks the value as a ref (it sits in a string field exactly like `secretref://` does, as an alternative value, not a new field). A value that merely *mentions* `git+` mid-string stays inline content.
- `<repo-url>` uses the same scheme set managed workspace inputs accept (`https://`, `ssh://`, SCP-style `git@host:path`). Credentials are forbidden in the URL, since `@` after the repository is reserved for the ref separator.
- `@<ref>` is an optional branch / tag / commit SHA; omitted means `HEAD`. A 40-hex value is a **pin**.
- `#<path>` is a repository-relative file path (non-empty, no `..`, no `#`, no newlines).

Lifecycle (the shared parse/resolve/pin helpers live in `modulo/core/pipeline_engine/git_content.py`; the save-time gate is part of the graph validator):

1. **Graph-save validation** (`GraphValidator`, codes `GIT_CONTENT_REF_INVALID` / `GIT_CONTENT_REF_UNPINNED`): a `git+` value must parse, and stored graphs must carry a **pinned** `@<40-hex>` ref, so every run snapshot (which deep-copies the live graph at snapshot creation) surfaces the resolved commit SHA for audit. Movable refs are rejected at save with a hint to pin them or use `modulo apply`. Composite-template sandbox sub-nodes obey the same gate. Agent rows are gated at every write path too: the REST/MCP Agent saves run `validate_agent_git_content_values` directly, the `PUT /graph` Agent-command sync validates each incoming list before writing the row (the graph validator only checks `sandbox_agent` nodes, so an `agent`-type node could otherwise carry an unpinned ref into the row ungated), and prompt rollback re-validates the restored template (a legacy history entry must not re-poison a clean row). A `GitContentRefError` from any of these surfaces is mapped to HTTP 422 and rolls back the enclosing write.
2. **Pin-on-apply**: `modulo apply` resolves a movable spec ref to its current commit SHA at plan time (`git ls-remote`, bounded, fail-closed; an unresolvable ref BLOCKS the entity) and rewrites the field to the canonical pinned form. The desired managed view, the write payload, the stored graph, and every subsequent run snapshot therefore hold the same pinned SHA.
3. **Run-time rendering**: at the agent-command rendering point (`node_runner`), a whole-field pinned ref is fetched at exactly that commit (host-side `git clone --no-checkout` + `git show <sha>:<path>`) and the file content replaces the ref before dispatch (fetched content then flows through the normal Jinja render for llm mode / verbatim for script mode). Unpinned refs and fetch failures raise typed errors, so the node fails closed rather than dispatching the raw ref string. Host-side fetch is public-repository-only in this increment (no credential material is ever embedded in the ref or the process environment); private-repo credential support is a later increment.
4. **Drift (`--diff`)**: because the desired side is the resolved pin and the deployed side is the stored pin, the managed-field hash reports `updated` when the tracked ref moved (branch/tag advanced, spec changed, or the deployed graph was saved unpinned). `drift_detail` additionally names the move per content field as a `git_content` list (`{node, field, desired, current}` with the full old/new refs), rendered as `drift detail git-content ...` lines, so a CI gate on `--diff` fails on content drift and the report shows the actual commit transition. Re-running `modulo apply` converges the deployed pin.

## Feature Flags

Feature flags are declared once in `_KNOWN_FLAGS` (`core/feature_flags.py`). The DB seed catalog (`core/seed_data/catalog.py`) derives from it automatically. Flags gate behaviour behind tier thresholds and per-org overrides.

**Policy:** A feature flag is permitted only if BOTH the enabled state AND the disabled state are safe AND CI-tested in both states. If the "off" state breaks behaviour (the feature cannot function without the flag), delete the flag and hardcode the behaviour. Flags that fail-closed silently are worse than no flag at all.

The reference-integrity guard test (`tests/architecture/test_feature_flag_reference_integrity.py`) enforces this: every flag name referenced by a gate must exist in `_KNOWN_FLAGS` (no phantoms), and every flag in `_KNOWN_FLAGS` must have at least one external reference (no orphans).

## Data Flow

### Pipeline run lifecycle

1. **Trigger** – A trigger fires (manual POST, webhook HMAC-verified, cron schedule, or agent_signal). TriggerEngine validates input against the entry agent's `input_schema`. A Run record is created in `pending` status. TriggerEvent is logged.

2. **Snapshot** – The pipeline's current definition is frozen as a PipelineSnapshot (all agent versions, schema pins, connector bindings, model backend pins, environment profile). The run now executes against this immutable snapshot; the snapshot is tagged `version_kind='run'`.

   **Live-edit history + release channels (ADR 049 / FAR-402 P6):** the snapshot
   machinery is reused for versioning beyond run-start freezes. The editor's
   save action creates a new snapshot tagged `version_kind='edit'` (the live-edit
   chain), leaving prior rows immutable so rollback is a pointer swap to a prior
   snapshot. A snapshot also carries a `release_channel` (`none` | `stable` |
   `canary`); a trigger bound to a `stable`/`canary` channel resolves to the
   latest snapshot of that channel (`TriggerEngine.resolve_snapshot_id_for_trigger`),
   while an unbound trigger pins the live graph (current behaviour).
   `diff_snapshots` surfaces port-signature deltas + a deterministic downstream
   impact oracle (`compute_port_change_impact`), and a save-time check
   (`check_port_change_breaking`) flags port changes that would drop/alter data
   read by a downstream edge.

3. **Compile** – PipelineExecutor loads the snapshot, compiles the `StateGraph`, and caches it by `(pipeline_id, snapshot_id)`.

4. **Execute** – Each node:
   a. ConnectorHub resolves bound ConnectorInstances and decrypts credentials once per run
   b. ModelBackendHub resolves the pinned model backend
   c. The agent's Jinja2 prompt is rendered (sandboxed environment) with `run_context` and previous outputs
   d. The LLM is called through the model backend
   e. Output is validated against the output Schema
   f. EvalEngine runs configured evals (model_judge, regex, json_schema, custom_function, human_set)
   g. If eval fails with `block` behaviour, run enters `failed` state
   h. If the outgoing edge has a HITL gate, `interrupt()` pauses the run

5. **HITL** – A human claims the gate (atomic DB lock), inspects context, and approves or rejects. Approval continues to the next node; rejection routes to the reject-target node when a reject route is configured (`reject_target` or a reject edge) or produces a FeedbackRecord, and with no reject route it ENDS the run with the terminal `rejected` status (FAR-1487; not `failed`, not `cancelled`). The disposition is `route` (a reject destination exists), `terminate` (the default) or `proceed` (an explicit `on_reject: proceed` on the gate continues down the approve path, as before); an automated `correction_target` wins over terminating, and the reviewer briefing states the consequence in all three cases.

6. **Complete** – After the terminal node, the run transitions to `complete` or `failed`. OTel spans, audit events, and run metrics are persisted. Notifications are dispatched.

#### Run admission and healing (FAR-604)

Dispatch admission is capacity-gated twice: per pipeline (`max_concurrent_runs`,
counted over `running`/`claimed`/`unknown` runs; `awaiting_human` and
`hitl_parked` are EXCLUDED: a run parked on a human decision is not executing,
and a human decision may take days without starving admission; the 2026-09-04
incident had 20 `awaiting_human` runs consuming a 20-cap pipeline for 26h) and
per org (`run_concurrency_limit`, still counted over
`running`/`awaiting_human`/`hitl_parked`/`claimed`/`unknown`; the org-wide
worker pool stays bounded by parked runs). A capacity-deferred run stays
`pending` (marked
`pipeline_capacity` / `org_capacity_limited`) and is re-dispatched when a slot
frees; `pipeline.max_concurrent_runs` must be >= 1 (create/update reject 0 and
negatives; 0 would silently wedge admission forever; pausing admission is the
org triggers pause). Several independent mechanisms keep those gates healthy:

- **Slot reconciliation sweep:** a system cron (every 5 min) terminalises
  `running` runs whose heartbeat is stale past `SLOT_RECONCILE_STALE_SECONDS`
  (default 30 min) with the `worker_lost` error code, force-releasing the
  pipeline slots a crashed worker leaked. Journeys and daily facts advance for
  each released run.
- **Worker-shutdown handoff (FAR-1627):** a deploy SIGTERMs the SAQ runs
  worker while agent runs are mid-execution. Instead of leaving those runs
  `running` until the 30-minute heartbeat TTL above detects the loss (and
  misattributes it to the dispatch path), the worker's SAQ `shutdown` hook
  processes every run it still owns the moment shutdown begins, and each run
  takes one of two paths. If a re-run is safe – a pre-node run (zero nodes
  executed, so nothing can double-execute) within its retry budget, or a
  node-started run whose graph is idempotent and whose `retry_policy` permits
  the re-run – the claim is freed (row reset to `pending`,
  `dispatched_at`/`dispatcher`/`heartbeat_at` cleared) and stamped with the
  truthful `harness.worker_lost` code plus a detail naming the shutdown;
  `dispatcher_reconcile` re-dispatches it on its next 60s tick, so an
  in-flight run at a release is handed back within ~1 minute of the new worker
  becoming ready – never 30-35 minutes. If a re-run is NOT safe – a
  non-idempotent node-started graph, or a `retry_policy` that forbids it – the
  run is failed in place with the same `harness.worker_lost` code and a detail
  naming why, rather than being put on the un-gated re-dispatch leg (which
  would re-EXECUTE a side-effecting node). Both writes are claim-token-fenced
  (a successor's re-claim is never clobbered) and cancellation-requested runs
  belong to the cancel path. The hook only hands back runs whose task actually
  observed SAQ's cancellation: SAQ runs the shutdown callbacks even when
  cancellation timed out, so a stuck, still-live predecessor's run is left
  untouched rather than re-dispatched against it. The heartbeat TTL remains
  the backstop for a hard crash (SIGKILL / OOM), where no shutdown hook runs,
  and for any run the observation gate skips.
- **Over-budget retry guard (FAR-2025):** a run-level retry re-executes the
  WHOLE run, so it is refused when the attempt that just ended had already
  consumed the graph's node-budget allowance – the sum of its resolved
  per-node `timeout_seconds` (the pipeline-level `node_timeout_seconds` fills
  in any node that does not set one), less a 300 s window for provisioning,
  the post-run envelope, and a task's own end-of-budget reserve. Such an
  attempt is an over-budget TASK, not a transient failure: spending the same
  allowances again cannot rescue it, so the retry only multiplies slot
  occupancy. The guard lives in the shared `_can_retry_after_policy`, so it
  covers both retry paths (the in-execute decision and the watchdog hook that
  reuses it) and applies whatever event the policy matched. Unknown timing
  data fails OPEN (the retry is allowed, as before). The worker-shutdown
  handback and the nodeless-zombie re-dispatch do not consume node budget and
  are unaffected.
- **HITL review-window terminaliser (FAR-648, FAR-1257):** a sweep wired into
  `dispatcher_reconcile` (every 60s) cancels an `awaiting_human` run whose open
  gate is still UNCLAIMED and UNDECIDED past the resolved review window:
  `cancelled` / `hitl_review_expired`, which releases the org concurrency slot
  (pipeline capacity was already released when the run left `running`). The
  window is resolved once at fire time (pipeline override > org default >
  `HITL_CLAIM_TTL_SECONDS` + `HITL_REVIEW_CANCEL_GRACE_SECONDS`, clamped to
  60s..7d) and stamped as `hitl_claims.terminalize_at`, so stamped rows ignore
  the grace knob while legacy unstamped rows keep the old arithmetic. A run
  with ANY claimed or still-in-window undecided gate is left alone (live human
  work), cancellation-requested runs belong to the cancel path, and analytics
  classifies these runs as `excluded` / `hitl_timeout` rather than a deliberate
  `operator_or_hitl_cancelled` cancel.
- **HITL approaching-deadline warning (FAR-1270):** a dedicated
  `hitl_deadline_warning` SAQ cron (every 60s) emails the same recipients the
  gate-fire alert uses while an unclaimed, undecided gate is still inside its
  lead band, `min(max(window / 2, 60s), window, 3600s)` before the deadline, so
  someone can approve/reject BEFORE the terminaliser cancels. At most ONE
  warning per gate (Redis `SET NX` fire-once marker, in-process backstop when
  Redis is down), skipped when the gate is already claimed or decided, already
  past its deadline, covered by a claimed open sibling gate, or has no opted-in
  recipients.
- **HITL park-on-expiry sweep:** a system cron (every 5 min) parks a run whose
  open HITL gate expired UNANSWERED: the run moves `awaiting_human` to
  `hitl_parked` (a non-terminal status that holds no pipeline capacity). The
  deadline is `HITL_PARK_GRACE_SECONDS` (default 24h) after expiry for legacy
  unstamped rows, but since FAR-1257 the sweep anchors on the review deadline
  (`terminalize_at` + a positive margin), so the terminaliser above always acts
  first and parking is unreachable for the unclaimed-expired class at stamped
  rows (a parked run still holds the org slot, which is why the terminaliser
  exists). The STATUS itself is the parked signal the HITL UI reads to show
  "expired, parked". Park is not decide: the gate row stays OPEN AND CLAIMABLE
  (a claim takes a fresh TTL), and the moment a decision commits
  (`HITLManager._decide`, API or MCP) the run un-parks to `awaiting_human` and
  re-enters normal admission: approve resumes from the checkpoint through the
  normal resume path; reject routes to the gate's `reject_target`/reject edge
  when one is configured, and when the gate declares no reject route the run
  ENDS with the terminal `rejected` status (FAR-1487; `on_reject: proceed`
  opts back into continuing along the normal edge). Each park is logged loudly
  (`hitl_park.parked`).
- **Queue coalescing (latest-wins):** for webhook deliveries with a stable
  work-item key (GitHub: `repository.full_name` + `pull_request.number`, or
  `issue.number`; anything else, no key, no coalescing), a new delivery folds
  into the pipeline's UNSTARTED `pending` run for the same key instead of
  inserting a row: the pending run's input payload is replaced and its
  `created_at` bumped, and a `coalesced` TriggerEvent is recorded. On by
  default; disable per trigger with `config_json.coalesce_pending: false`.
  Replays never coalesce.
- **CI-failure coalesce (Branch Fixer deduplication, FAR-1034):**

  > This section reflects the behaviour on the `dist/conflict-dispatch-dedupe` branch (PR #2186); it merges after that PR.

  **Purpose.** Prevent duplicate Branch Fixer dispatches for the same failing PR at the same head SHA. The canonical payload-hash deduplication (`webhook_dedup_hashes`) cannot suppress these deliveries, for two different reasons per sender:

  - `ci.yml`'s `notify-pr-failure` job includes the volatile `runUrl` of the failed CI run in its payload, so every delivery hashes differently and would mint a full Branch Fixer run.
  - `branch-fixer.yml` has never carried `runUrl`. Before #2036 its payload omitted `headSha` entirely, so no coalesce key could be derived and every delivery dispatched; today its only volatile content is the caller-composed `failureDescription`, and a head movement dispatches fresh by design.

  CI-failure coalescing therefore keys the suppression on the stable `(prNumber, headSha)` subset instead.

  **Identity key.** `derive_ci_failure_coalesce_key` produces `ci-failure:pr:<n>:sha:<sha>` from the payload's `prNumber` and `headSha` fields. A PR with no `headSha` derives no key and is never coalesced. A new commit changes the head SHA and dispatches fresh.

  **Two legs (executed in order).**

  1. **Age window (primary, fast path).** Default 3600s (`_CI_FAILURE_COALESCE_WINDOW_SECONDS`). Overridable per trigger via `config_json.ci_failure_coalesce_window_seconds`, clamped to a minimum of 1s and a maximum of 7 days (`_CI_FAILURE_COALESCE_WINDOW_MAX_SECONDS`). A repeat delivery whose derived key already has a live entry in `webhook_dedup_hashes` (TTL = the window) is suppressed immediately. A `coalesced` TriggerEvent is recorded and `CiFailureCoalescedError` raised. The route commits the event inside its transaction and acks the sender with a handled 202.

  2. **In-flight run (secondary).** When the age window has elapsed, a same-key delivery is STILL suppressed if any run for the same pipeline and derived key is non-terminal. Implemented in `has_in_flight_ci_failure_run` (called via the shared `_suppress_ci_failure` path). The check is pipeline-scoped, bounded by `CI_FAILURE_INFLIGHT_BACKSTOP_SECONDS` (default 7200s), and limited to `COALESCE_CANDIDATE_LIMIT` recent candidates. Each candidate run's key is recomputed and compared against the key derived at entry: the engine-stamped key is authoritative, with derivation from the candidate's `input_payload` as the legacy/verbatim-payload fallback. Fail-open: an unreadable query logs `ci_failure_coalesce.inflight_check_failed` and falls through to the age decision – dispatch is never blocked.

  **Audit.** Both legs funnel through the same suppression point. The `coalesced` TriggerEvent and `CiFailureCoalescedError` carry the stable marker `ci_failure_coalesced:<key>` in `error_detail`. The log distinguishes the leg (`"inside the <window>s coalesce window"` vs `"a run for the same PR + head SHA is still in flight"`), but the event shape is identical.

  **Config.** The age window is per-trigger via the optional `config_json.ci_failure_coalesce_window_seconds` override; a trigger without an override (the Branch Fixer CI-failure trigger among them) uses the 3600s default. The in-flight backstop is a global constant. Trigger mappings live in the Branch Fixer pipeline's configuration rather than the repository, so which mappings carry a `headSha` is a deployment fact; a mapping whose payload has no `headSha` (for example a review-trigger mapping that does not include one) derives no key and can never coalesce, by the rule above.

  **Why the second leg exists.** The Branch Fixer `sandbox_agent` node has a 3300s (55 min) runtime cap and the node-deadline watchdog can re-dispatch it, so a single logical run can occupy the pipeline for up to ~110 min against a 1-hour default window (the exact re-dispatch count and node timeout are pipeline configuration). Without the in-flight leg, a same-key delivery after the window elapsed would mint a second full run while the first was still executing, doubling concurrency consumption.

- **Dispatcher backpressure (per-funnel wedge posture):** trigger dispatch
  refuses NEW runs when the pipeline's pending queue exceeds
  `max(3 x max_concurrent_runs, 5)` rows, or its oldest genuinely-wedged
  pending run is older than `TRIGGER_BACKPRESSURE_MAX_AGE_SECONDS` (default
  60 min). `org_capacity_limited` rows are excluded from the age leg (a busy
  org is not a wedged pipeline), while `pipeline_capacity` rows still count: a
  pipeline pinned at its own cap (possibly by a leaked slot) is itself the
  wedge this guard exists to break. How the refusal reaches the delivery
  depends on the funnel (FAR-1342):

  | Funnel | At a wedged pipeline | Why |
  |---|---|---|
  | Webhook | HTTP 429, no run created | The delivery is discrete and the sender can retry; refusing beats queueing work that will never run. |
  | Cron | The fire is CONSUMED and recorded: a `backpressure_skipped` TriggerEvent plus `last_fired_at` stamped | A cron fire has no sender to retry it; the next scheduled fire carries the future. `next_fire_at` was already advanced at enqueue time, so the epoch is consumed rather than re-fired (the missed-fire catch-up only re-fires an epoch advanced but never fired). |
  | Polling | Skipped and recorded; the next poll interval re-fires | The poll repeats naturally on its own schedule, so deferral is free. |
  | Manual / MCP | Exempt: no backpressure check | A human explicitly asked for this run; capacity gates may still DEFER its dispatch, but they do not refuse it. |

  Invariant: **a pending run may wait indefinitely while its pipeline makes
  progress; a pipeline making NO progress stops accepting new work.** A
  delivery that IS admitted and then capacity-queued is not lost: it waits
  indefinitely by default (FAR-1883), and a pipeline may bound that wait with
  its nullable `queue_timeout_seconds` (anchored on the queued run's
  `created_at`), whose expiry ENDS the run as a supersession (`rejected` /
  `queue.expired`) - visible and filterable, but out of failure counts and
  paging, with the queue-state alert as the escalation path whether or not a
  timeout is configured. Every refusal is recorded: the webhook arm re-writes
  its `backpressure_skipped` event in a fresh transaction after the main
  transaction unwinds (otherwise the in-transaction write would roll back with
  the refusal), and all three refusing funnels record the same
  `validation_result`, so an operator sees the skip in
  `/api/v1/admin/trigger-events` and the per-trigger event log, and it is
  counted in the capacity-outcome summary. The FAR-1344 wedge alert evaluates
  the same predicate on every dispatcher-reconcile tick, pairing the skip with
  a notification. Webhook replays bypass the guard (an intentional re-fire must
  never be refused). The guard fails OPEN on a read error (admits, with a
  warning) and is today shape-based: a slow-but-progressing pipeline can still
  be refused until FAR-1355's progress-based detection replaces it.
- **Legacy stale-run sweep:** pending runs past the never-dispatched window
  (`SAQ_NEVER_DISPATCHED_WINDOW`) and legacy non-SAQ `running` rows with 5+
  claims (`worker_lost`) are terminalised or re-dispatched as before. There is
  no instance-wide queued TTL: one TTL per state class, so capacity-marked
  rows are bounded only by the pipeline's `queue_timeout_seconds` (above),
  while never-dispatched and enqueue-failed rows keep their own windows.

#### Runner capacity gate (FAR-594 D8)

Sandbox-agent dispatches (every `sandbox_mode`, every provider tier) reserve a
runner slot through ONE atomic transaction --
`runner_capacity.acquire_runner_dispatch_slot` -- replacing the pre-D8 racy
check-then-act count. Transaction shape: `SET LOCAL lock_timeout`
(`RUNNER_CAPACITY_LOCK_TIMEOUT_MS`, default 2s) → **own-row
claim-token-fenced lock FIRST** → per-org advisory lock (the RESERVED
`modulo:runner-capacity:org-v1` namespace, per-org derived; never the shared
`_uuid_to_lock_keys` keyspace) → lock-free count → decide → the fenced
dispatch-marker UPDATE commits the reservation → the workspace provisions
OUTSIDE the transaction. The uniform row→advisory ordering is shared with the
resume path (`executor.resume` writes the run row before its advisory lock),
which makes the scheme cycle-free by construction, including same-run
dispatch+resume overlap. SQLSTATE 55P03 (lock_timeout) degrades to a RETRYABLE
capacity denial with the distinct `runner.capacity.lock_degraded` event; 40P01
is a separate `runner.capacity.deadlock_degraded` alarm (expected impossible
under the uniform ordering). Every other DB error fails OPEN
(`runner.capacity.gate_error`) and the dispatch marker is still written
best-effort in its own transaction; if even that write fails
(`sandbox_agent.best_effort_marker_failed`) the dispatch proceeds
markerless fail-open; the sweep and the re-dispatch path own healing, a DB
hiccup must never become a dispatch outage.

- **Count population (unified across all four capacity paths: dispatch gate,
  resume gate, HITL pre-check, claim-time read):** flag ON: `running` runs
  holding a live dispatch marker only. `awaiting_human`/`pending`/`claimed`/
  `unknown`/`hitl_parked` hold no slot (a parked HITL run cannot starve the
  org; the resume re-acquires a slot when the approval re-dispatches through
  the decided-gate auto-resume loop). The count excludes the run's OWN marker,
  so a re-dispatch of a run still carrying a fence-carrying stale marker
  cannot self-block. Flag OFF: the pre-D8 population exactly
  (`ACTIVE_RUN_STATUSES`, no tombstone exclusion) -- the flag-off window is the
  pre-D8 behaviour, not a hybrid.
- **Tier attribution:** the gate's marker write carries `"provider"`
  (`runner_docker` | `e2b` | `local`) + `"written_at"`. Legacy tier-less
  markers count as Docker-tier (fail-safe) and age out via the sweep.
- **Tier-scoped default:** with the rollout flag
  (`RUNNER_CAPACITY_GATE_ENABLED`) ON and the org key ABSENT, the
  Docker-tier default 4 gates Docker+Local dispatches only (e2b carries its
  own platform-side quota and is neither counted into that bucket nor denied
  by it); an explicit value gates ALL runner dispatches; an explicit `null` is
  no gate; `0` is deny-all. Flag OFF keeps the pre-D8 behaviour exactly: the
  pre-D8 count population above (no advisory lock, no lock_timeout,
  `enforced_cap` semantics; absent key = no gate), the legacy
  `_uuid_to_lock_keys` keyspace on the resume path, and NO HITL tombstone.
  The flag is short-lived (removed at GA).
- **HITL boundary (flag ON):** at interrupt handling, before the run enters
  `awaiting_human`, any remaining NON-FENCE dispatch marker becomes the
  `{"state": "cleared_at_hitl"}` tombstone: capacity-neutral (the count is
  running-only AND excludes tombstones) while still recording the dispatch.
  The exactly-once `script_executing` fence is NEVER tombstoned (a fence
  proves a script PROCESS may have started; replacing it would let the fence
  be re-acquired, double-execute). Flag OFF the tombstone does not fire at
  all (pre-D8: the marker simply survives the park).
- **State-aware reconciliation sweep** (wired into `dispatcher_reconcile`
  every 60s and a dedicated 5-min `runner_marker_sweep` cron): clears non-fence
  markers on genuinely terminal runs and markers stale beyond 25h
  (`RUNNER_MARKER_STALE_SECONDS`; marker `written_at`, legacy tier-less
  fall back to `runs.updated_at`); a stale clear on a non-terminal RUNNING run
  also terminalises the run (`worker_lost`; the slot is reclaimed by killing
  the zombie, so no running-without-marker-without-workspace state can be
  minted) unless the row itself is FRESH (a freshly-resumed long-parked run:
  only the stale marker is cleared, never a live attempt); every
  clear/transition UPDATE is CAS-guarded on the classified marker text (a
  concurrent fresh-marker commit makes the UPDATE match 0 rows; the sweep
  never clobbers a reservation it did not classify), the candidate scan is
  batched (500-row cursor pages) over the
  `ix_runs_org_runner_marker_sweep` partial index, and any org-index or
  org-pass failure raises `RunnerMarkerSweepError` carrying the partial
  counts (the cron wrapper persists them + re-raises, retries engage).
  Fence-carrying `script_executing` components on NON-terminal rows are
  NEVER cleared (precedence over staleness); on TERMINAL rows the
  rollback detector's anomaly-code exemption outranks the fence, and a
  terminal non-anomaly crash-leaked fence clears past the staleness cap.
  Runs `dispatcher_reconcile` considers recoverable keep their markers (the
  sweep evaluates the reconciler's OWN OR-composed recovery predicates,
  parity by construction, evaluated lazily and never for terminal rows);
  terminal runs carrying the rollback detector's anomaly error
  codes keep their markers (`rollback_thresholds._count_claim_without_marker`
  requires them). Every COMMITTED clear emits `runner.capacity.marker_cleared`;
  the coordination note the D4 container reconciler consumes (the cleared
  run is terminal or capacity-neutral, so its workspace becomes an orphan the
  D4 reconciler owns destroying); a rolled-back org pass emits nothing. After
  each org's pass the live count is asserted ≤ cap with
  `runner.capacity.violation` on a genuine breach (a cap-less org never
  violates); the D8 rollback signal. The claim-time demotion
  (`_check_capacity`) remains an explicitly
  ADVISORY, lock-free, population-only read (its own-row lock exists only at
  the fenced demote write); the sweep is its named backstop.

### Work-item refs → Lifecycle Map journeys (FAR-794)

Every run records the work items it serves as canonical refs (`kind` + `ref`, e.g. `linear`/`FAR-123`, `github`/`owner/repo#123`) in `runs.work_item_refs`, anchored to deterministic `journeys` rows (`uuid5(org:kind:ref)`, derivable at create and finalise time, no mint races). The refs travel through ONE reserved input-payload key, `_work_item_refs`: reserved (a trigger `payload_mapping` can never target it and a context-setter node may never write it) but EXEMPT from the create-time reserved-key strip so refs survive the coalesce identity: the engine re-stamps the stored value with canonicalised entries before hashing, so a forged key cannot escalate provenance.

Provenance is ENGINE-ASSIGNED per supply channel; the wire `source` value is never trusted. `caller` for authenticated channels (manual REST/MCP triggers, operator reruns), `derived` for extraction-driven channels (webhook, cron, polling, agent_signal, replay), `agent` for a node's own emission. The legacy `reported` alias was dropped (FAR-795): the persisted vocabulary is exactly `caller` / `derived` / `agent`, migration 0222 rewrote stored rows to `agent`, and any entry still declaring `source: reported` (e.g. an old snapshot or replay payload) is rejected/dropped by the validator. ONE shape validator (`modulo.db.lifecycle_refs.validate_ref_entry`) owns kind/ref canonicalisation, the source vocabulary, and status (`done`/`attempted`) for every surface.

Minting is owned by provenance: `caller`/`derived` refs mint journey rows at run creation and again at terminal finalise via a rank-guarded upsert (`agent(0) < derived(1) < caller(2)`: provenance only ever upgrades, never downgrades; `first_seen_source` is immutable after mint; `latest_*`/`run_count` stay owned by the finalise evidence CAS). `agent`-sourced refs are collected and stored on the run and confirmed against EXISTING journey rows (advisory, match-only), but never mint: agent minting is deferred pending its safety substrate (FAR-795). Coalesced deliveries merge refs into the surviving run with the same rank-guarded union and re-mint newly-seen entries.

Completeness over precision: the unified cap (`modulo_work_item_refs_cap`, env `MODULO_WORK_ITEM_REFS_CAP`, default 100, clamped to [1, 100000]) bounds self-report normalisation, the finalise merge, and node-input injection with one value; cap drops drain lowest-provenance first (agent → derived → caller, then array position) and are counted. Pipelines declaring `work_item_refs_required: true` in `run_context_defaults` refuse a delivery that supplied no refs: HTTP 422 with a `work_item_refs_required` error on the MCP and webhook paths, and a 422 whose detail is prose on the REST path (reject-and-retry for webhooks, not acked-as-accepted).

Nodes opt in to reading the run's refs by declaring a `work_item_refs` input property in the node's `input_schema_json`: the create-time refs are injected read-only as a `{{ work_item_refs }}` template variable at node start (a read failure degrades to fewer refs, never a failed node). Journeys carry `provenance` + `first_seen_source` columns (migration 0220; `runs.work_item_refs` containment is served by the existing partial GIN index) and the refs pipeline emits OTel counters (`modulo_work_item_refs_by_source_total`, `..._unknown_source_total`, `..._shadow_strip_hits_total`, `..._malformed_total`, `..._cap_dropped_total`).

### WebSocket event flow

```
LangGraph astream_events()
  → Per-run event broker (in-process pub/sub)
    → WebSocket connections subscribe (per Vue tab)
    → MCP SSE connections subscribe (per LLM client)
```

In multi-worker deployments: Redis pub/sub replaces in-process broker.
On reconnect: client re-fetches current state via `GET /api/v1/runs/{id}`, then replays missed events via `?since_event_seq=N` (ring buffer, 100 events).

## Database Schema

### Core entities

```
Organisation
  ├── User (org-scoped)
  │   ├── TeamMembership (user_id, team_id, team_role)
  │   └── ApiKey (user_id, role, key_hash)
  ├── Team (org-scoped)
  │   └── TeamMembership (as above)
  ├── Pipeline (org-scoped, optional owner_team_id, visibility)
  │   ├── PipelineSnapshot (immutable, run-start freeze)
  │   ├── Trigger (pipeline_id, trigger_type, config_json)
  │   ├── PipelineEdge (pipeline_id, source, target, edge_type, hitl_review_config)
  │   └── Run (pipeline_id, snapshot_id, status, state machine)
  │       ├── hitl_claims (run_id, review_id, claimed_by, claim_token, expires_at)
  │       └── TriggerEvent (trigger_id, validation_result, run_id)
  ├── Stage (org-scoped, optional owner_team_id, visibility)
  ├── Schema (org-scoped)
  │   └── SchemaVersion (schema_id, version, definition_json)
  ├── Agent (org-scoped)
  │   └── prompt_version_history (agent_id, version, template)
  ├── ConnectorInstance (org-scoped, optional owner_team_id)
  ├── ModelBackend (org-scoped)
  ├── EnvironmentProfile (org-scoped)
  ├── LibraryPrimitive (org-scoped, primitive_type, content_json)
  ├── AuditEvent (org-scoped, immutable)
  ├── EvalDefinition (org-scoped)
  ├── FeedbackRecord (org-scoped, run_id, node_id)
  └── VariantGroup (org-scoped, run comparisons)
```

### RLS enforcement

Every table carries `organisation_id`. Row-Level Security is enforced via `SET LOCAL app.organisation_id` inside transactions. The session pool resets org context on checkout. LangGraph checkpoint tables (`checkpoints`, `checkpoint_blobs`, `checkpoint_writes`) do not have RLS – this is a known gap for SaaS (V2).

### Key constraints

- `(trigger_id, payload_hash)` unique on `webhook_dedup_hashes` – deduplication window
- `(run_id, review_id)` unique on `hitl_claims` – one claim per gate per run
- SchemaVersion deletion protected by active agent/pipeline references
- ModelBackend deletion protected by active references (soft-delete via `status: deprecated`) – agent_runner_bindings carries RESTRICT FKs, so a bound backend cannot be deleted while any agent binding references it (the CRUD pre-delete inventory reports "in use by N agents"; the FK is the race-proof backstop)

### Per-agent runner bindings (Model Backends, FAR-592 / D6)

Model Backend credentials are configure-once-reuse-everywhere: an agent declares **runner bindings** (`agent_runner_bindings`, org-scoped table behind `rls_org_isolation` + RLS grants) of the shape `{model_backend, target_env_var, source_field}`. At provision time, in the runner dispatch path, a fresh short-lived `ModelBackendHub` resolves ONLY the referenced backends, decrypts via the secrets backend, injects `target_env_var = <decrypted source_field value>` into the container/workspace env, and is explicitly disposed. Precedence is deliberate and preserved: profile secrets < runner bindings < node `env_vars_extra` – the NODE wins (the PR Reviewer's `GITHUB_TOKEN` override keeps working).

Tier applicability: bindings inject on container/workspace tiers; the Local (host-subprocess) provider refuses at provision time (typed error, `sandbox.tier_refused`) unless the profile's `config_json.allow_runner_env_bindings` is set. Resolution failure is a first-class retryable error (`sandbox.binding_resolution`) whose rate is the D6 rollback trigger. The refusal is terminal end-to-end: the runtime retry/compensation machinery treats `SandboxTierRefusedError` as never-retryable, the executor's run-level handler terminal-fails it directly (before the transient requeue machinery), and the pipeline `retry_policy` gate is skipped for it – a deterministic refusal is only ever resolved by reconfiguration, never by re-dispatch. Bindings accept **org-visible backends only** (`visibility = 'org'`): a save referencing a missing or team-visible backend is rejected with 400 ("bindings accept org-visible backends only"); team-visible backends are a share surface, not a credential source for another agent. Loading a provided-but-unresolvable environment profile also fails CLOSED (refusal) rather than defaulting to the open tier.

The posture for injected values (distinguish from the FAR-296 per-run minted key): the value IS a standing user-configured credential readable by the agent's own code – that is the feature's purpose. Mitigations: bindings may never target a Modulo-reserved var (`RESERVED_ENV_VARS` + `MODULO_*` / `APP_MODULO_*` / `GIT_*` – a tested, named denylist; the denylist limitation documented), every injection is audit-logged without values, save-time validation covers name shape/uniqueness/source-field surface, and manage endpoints sit behind the elevated `model_backend.binding.manage` permission. Docs recommend a dedicated restricted runner key per backend. Related org-secret machinery (`env_vars`, `{{ secrets.* }}`, `env_vars_extra`) stays the org-secret alternative; D6 serves per-agent Model-Backend credentials – a different source, not a replacement.

## Authentication & Authorization

### Authentication methods

| Method | Status | Use case |
|--------|--------|----------|
| JWT (access + refresh) | Alpha | Browser UI sessions – 15-min access, sliding 24-h refresh (idle-logout window; configurable 1–168h via `MODULO_REFRESH_TOKEN_TTL_HOURS`) |
| API key (bearer token) | Alpha | CI/CD, MCP clients – role-scoped (operator/runner) + caller-scoped (`org`/`user` scope axis, ADR 030) |
| Basic Auth | Alpha | Multi-user alpha (`MODULO_USERS` env var) |
| OAuth 2.0 (authlib) | V1 | MCP clients (PKCE, exact redirect_uri) |
| OIDC / SAML 2.0 | V1 (team) | SSO with JIT provisioning |

### JWT Security

- Access tokens: 15-min expiry
- Refresh tokens: sliding 24-h expiry by default (configurable 1–168h via `MODULO_REFRESH_TOKEN_TTL_HOURS`), rotated on use (each rotation mints a fresh token with the full lifetime, so this IS the idle-logout window: a session abandoned longer than the TTL cannot refresh and is logged out)
- Algorithm pinning: `HS256` only – `none` and other algs rejected
- SECRET_KEY: minimum 32 bytes (256 bits) – refused at startup if insufficient
- Token family invalidation on revocation
- WebSocket auth via short-lived opaque `ws-token` (60s TTL, single-use, in `Authorization` header, never query string)

### API keys

Format: `mk_<lookup_prefix>_<random_secret>`. Stored as SHA-256 hash. Role set: `operator` (trigger runs, approve HITL) and `runner` (trigger runs, read-only). Admin actions require human session. Keys shown once at creation.

Every key carries a **caller scope** (`scope` column, immutable post-mint):
`org` (org-level machine identity, the historical default; includes
team-scoped and per-run sandbox keys) or `user` (a per-user key that acts as
its creator's identity and is quota'd to 10 active per account). User-scoped
minting is REST-JWT-only, gated by the org `user_scoped_mcp_keys` flag
(ADR 030). MCP tools whose permission key ends in `.self` (e.g.
`get_hitl_email_alerts`) are caller-scoped: they target the caller's own
account and are denied under org-wide/run-scoped keys. Key lifecycle events
are audited (`api_key_created` / `api_key_revoked`) on both the REST and MCP
surfaces with `auth_type` / `key_scope` / masked-prefix payload stamps.

**Service accounts (FAR-1478, ADR 056 / 058).** A service principal is a
first-class `accounts` row (`kind='service'`, carrying no email, password or
SSO identity – structurally unreachable by every login/invite/SSO/SCIM path,
FAR-1385 slice 1; `accounts.kind` itself is immutable after insert, enforced
by the `trg_accounts_kind_immutable` DB trigger, so a human row can never be
flipped into a service row – or vice versa – by any writer). Org admins
provision, read, list, rename and decommission them at
`/api/v1/service-accounts`; an `admin` org-role grant on a service
principal is rejected with 422 at the surface and structurally refused by a
DB trigger (`trg_org_memberships_service_role_ceiling`, migration 0315) – the
role ceiling is `operator`, never admin. Service accounts hold team
memberships through the normal team surface (team roles never include admin).
Keys minted for a service principal are OWNED by it (`org_api_keys.account_id`
is the service account, never the minting human): a human admin bootstraps one
via `POST /api/v1/service-accounts/{id}/keys`, and the service principal
mints its own with its own `mk_` credential (MCP `create_api_key`, or the same
endpoint acting as the principal itself – a key may only mint keys for its own
principal). Service-owned keys are capped at **365 days** (ADR 058's tiered
TTL – the service half of the 365d-service / 90d-user-scoped resolution),
enforced at the `create_api_key` AND `update_api_key` choke points so every
surface – mint and extension alike – refuses the same way (a mint-only cap
would let any runner/operator extend a service-owned key arbitrarily via
`PUT /api/v1/api-keys/{id}`). Decommission is explicit and one-way: it stamps
`accounts.decommissioned_at` (CHECK-restricted to service rows), tombstones
the org membership, flips `active` off and bulk-revokes every live key – a
service account survives human offboarding and is retired only by that
deliberate act; the human user-admin surface refuses service targets that are
members of the caller's org (non-members surface as 404 first, so the
rejection never discloses a cross-org service account).

**Grant-sets on API keys (FAR-1477, ADR 058; flag `api_key_grants`, default OFF).**
`org_api_keys.grants` is a nullable, space-joined list of `PERMISSIONS` keys,
tri-state: `NULL` = legacy role-bundle behaviour (unchanged), empty = explicit
deny-all, a list = the exact set. Enforcement is
`effective = grants ∩ bundle(live_role)` on both the REST resolver
(`_assert_tenant_permission`) and the MCP chokepoint (`resolve_tool_access`);
the grant leg only ever narrows and is not lifted by the authz kill switch.
Minting (`POST /api/v1/api-keys` with `grants`) caps requested grants to the
caller's LIVE capability and rejects non-delegable keys. Delegability is a
single registry-level flag, `modulo.auth.permissions.is_delegable`, read live
at enforcement and mint time (never-grantable: human_only HITL, `api_key.*`,
`oauth.client.*`, `system.*`, `org.delete`, break-glass controls); OAuth
scopes and principals must consume it rather than keep a second list. With the
flag OFF a grant-bearing key is denied (401), never widened to its role, and
`grants` on mint is rejected (422). With the flag ON, user-scoped keys default
to and are capped at 90 days. Grants are immutable after mint. The MCP
`create_api_key` tool accepts the same optional `grants` (always an org-scoped
key, so no 90-day user-key TTL) and runs the same mint cap and flag gate as
REST. The Settings > MCP create-key dialog offers a grant picker, sourced from
`GET /api/v1/api-keys/grantable-permissions` (delegable permissions only,
empty with the flag OFF); it omits `grants` unless the user restricts the key.
Privilege helpers that gate admin/operator-only extras inside a route (cost
breaker, guardrail strip, HITL weakening, In-Dev reveal) also require the
matching permission (`cost.manage`, `guardrail.manage`, `pipeline.graph.update`,
`*.list.in_dev`) in the grant-set when the key carries one; a malformed
grant value is denied, never read as unrestricted. The grant decision is carried
into the service layer: for REST callers `replace_pipeline_graph` and
`rollback_to_snapshot` compute the final HITL-privilege and guardrail-admin
flags exactly as before (the live role re-read under the row lock stays
authoritative for REST callers, including upgrading a stale-low flag), then
apply the explicit `grants_deny_privilege` / `grants_deny_guardrail_admin`
parameters, which can only NARROW the result and so a live admin role never
overrides a grants denial (an admin-owned key holding only
`pipeline.graph.update` cannot strip a guardrail binding). The grant flag and
grant-set are resolved after the key-validation DB session closes (never nested
inside it, to avoid holding two pooled connections per request). A transient
failure reading the flag for a grant-bearing key (the strict read propagates the
registry's org-override read error instead of treating it as OFF) answers 503
(not 401) on REST and a retryable denial on MCP; NULL-grants keys never read the
flag. Long-lived MCP connections re-run the grant resolver on every
re-validation. **Rollback caveat:** a key minted with grants (non-NULL
`grants`, including the empty deny-all) becomes a full-role key if code older
than this change ignores the column or if the column is dropped. Migration
0281's downgrade therefore refuses to run while any grant-bearing key exists.
Before rolling code back below this change, turn the `api_key_grants` flag off
and revoke every grant-bearing key. Minting reads the flag strictly: a transient
read failure answers a retryable 503 instead of silently skipping the grants
gate or the 90-day user-key cap. A grant above the key's own minted/live role has no effect,
since `effective = grants ∩ bundle(role)`; the mint cap checks the minter's
capability, not the new key's role. **Known REST/MCP asymmetry (open design question):** over MCP
every read-only tool is gated by the single coarse `resource.read_only` key,
not the fine-grained REST read keys (`pipeline.list`, `pipeline.graph.read`,
...), so MCP reads are all-or-nothing per grant-set; REST enforces the fine keys.


### Row-Level Security

All tenant isolation is at the database layer via `SET LOCAL app.organisation_id` inside transactions. Every query runs within the org scope. This prevents cross-tenant leaks even if application-level scoping is bypassed. Team-visibility resources return 404 (not 403) for non-members – no existence enumeration.

### Soft-Delete Scoping (FAR-1025)

Soft-deleted rows (models inheriting `SoftDeleteMixin`) are **automatically excluded** from every ORM SELECT via a global `do_orm_execute` listener registered at startup (`modulo.db.soft_delete`). The listener injects `WHERE deleted_at IS NULL` through `with_loader_criteria`, so new read paths automatically exclude soft-deleted rows without requiring an explicit hand-written predicate.

**Every model with a `deleted_at` column MUST inherit `SoftDeleteMixin`.** This is enforced by the architecture test `tests/architecture/test_soft_delete_mixin_coverage.py`, which enumerates all registered mappers and fails CI if any model declares `deleted_at` without the mixin. The mixin provides the column definition (`DateTime(timezone=True), nullable=True, default=None`), so normalised models remove their locally-declared `deleted_at` to avoid duplication.

**Opt-out convention:** Legitimate read paths that must see soft-deleted rows (restore flows, historical lookups, purge operations, version resolution) opt out per-statement:

```python
from modulo.db.soft_delete import include_soft_deleted

stmt = include_soft_deleted(select(Model).where(Model.id == uid))
```

Or equivalently:

```python
stmt = select(Model).where(Model.id == uid).execution_options(include_deleted=True)
```

The opt-out is scoped to the individual statement and does not leak across concurrent operations on the same session. Existing hand-written `deleted_at.is_(None)` predicates are now redundant but harmless: they are left in place to avoid risky churn.

**CRUD `include_deleted` parameter pattern:** CRUD functions that accept `include_deleted: bool = False` must bridge to the opt-out when `include_deleted=True`:

```python
if include_deleted:
    stmt = include_soft_deleted(stmt)
else:
    stmt = stmt.where(Model.deleted_at.is_(None))
```

Without this bridge, `include_deleted=True` becomes a no-op because the global filter still injects `deleted_at IS NULL`.

### Run-Execution Service Identity (ADR 038)

When a pipeline runs, the run executes with the **pipeline owner's authority**, not the triggering user's grants. This is a service identity scoped to the pipeline's `owner_team_id` (or the org when `owner_team_id` is NULL).

**Key properties:**

- The only user-facing access check is "can you trigger this pipeline?" (the `trigger_run` team gate). Once triggered, the run inherits the pipeline owner's identity.
- Referenced resources (schema, connector, model backend, agent) are usable by the run **regardless of the triggerer's direct access**. The engine resolves the pipeline's team membership to determine which resources the run may access.
- **Secrets are brokered**: injected into the run environment by the engine, never readable by the user. A user gaining pipeline access must never gain secret read access.
- A run may only use secrets scoped to the **pipeline's owner team**; cross-team secret use requires moving the secret to an org-visible scope.
- `runs.owner_team_id` is **metadata** (for dashboard aggregation), not a security control. Run access is derived from pipeline access; runs do not need their own team gate.

See ADR 038 (rbac-security-boundary) for the full decision.

### Folder Security Model (ADR 038)

`PipelineFolder` is **organisation-scoped only**: it has no `owner_team_id`, no `visibility`, and no security cascade. Pipeline ownership is explicit on the pipeline itself (`Pipeline.owner_team_id`), not inherited from a folder. Folders are a UI organisation concept, not a security boundary.

This is an explicit decision: any future proposal to add folder-level security (inherited ownership, visibility cascading, team-scoped folder access) must address ADR 038's rationale for rejecting folders-as-security. See ADR 038 for the full reasoning.

### MCP Scope Enforcement – Dual Layer

1. **Token middleware** – validates required scope on every request
2. **ViewModel command layer** – re-validates scope for every command

Both layers must agree. This prevents scope bypass via routing misconfiguration.

### Rate limiting

Hardcoded sliding-window rules enforced by `RateLimitMiddleware` (see `backend/src/modulo/api/middleware/rate_limiter.py`):

| Path prefix | Limit | Window |
|-------------|-------|--------|
| `/api/v1/runs` | 60 | 60s |
| `/api/v1/triggers` | 100 | 60s |
| `/api/v1/errors/ingest` | 10 | 60s |
| HITL review actions (`/api/v1/runs/{run_id}/hitl/{review_id}/{action}` and `/api/v1/runs/{run_id}/manual/{review_id}/submit`, POST) | 20 per user (aggregate) | 60s |
| `/mcp` | 200 | 60s |
| Auth endpoints (`/api/v1/auth/`) | 10 attempts | 60s (configurable via `MODULO_AUTH_MAX_ATTEMPTS`) |

Redis-backed sliding window (ZADD + ZREMRANGEBYSCORE). Falls back to in-memory no-op when Redis is unavailable. Auth rate limiter requires Redis and is disabled without it.

The HITL budget is AGGREGATE per identity (JWT user, API-key prefix, or IP) across runs, gates, review actions, AND both surfaces; the bucket key normalizes the whole variable tail (FAR-611), so rotating gates, runs, or actions cannot dodge the 20/min cap (the 2026-09-05 bulk-approve sweep spread 22 decisions across per-gate buckets and was never throttled). The manual-output submit route (`/runs/{run_id}/manual/{review_id}/submit`, an approve-capability HITL surface whose path has no `/hitl/` segment, shares the SAME aggregate bucket, so a sweep alternating `/hitl/` review actions and `/manual/` submits exhausts one budget. MCP review actions sit behind the general `/mcp` 200/min rule rather than the HITL rule; they are machine-surface, human_only gates are already denied there, and tightening the MCP budget is follow-up work if MCP-side sweeps ever warrant it.

## Deployment Architecture

### Modes

| Mode | Components | Use case |
|------|-----------|----------|
| **Standalone** | Single process + SQLite file | Local dev, quick evaluation |
| **Docker Compose** | Backend + Frontend + PostgreSQL 16 + (optional) Redis 8 + (optional) OTel stack | Single-server production |

### Docker Compose

Compose files (`docker-compose*.yml` at the repo root; the non-default ones live under `deploy/compose/`):
- `docker-compose.yml` – dev mode (builds from source, Postgres 16, Redis 8)
- `docker-compose.local.yml` – with observability profile (otel-collector, Prometheus, Grafana)
- `deploy/compose/docker-compose.prod.yml` – self-hosted single-server production (prebuilt image)
- `deploy/compose/docker-compose.test.yml` – CI test environment

### Kubernetes (Helm)

A maintained, vendor-neutral Helm chart ships at `deploy/helm/modulo/`
(validated end-to-end on EKS, FAR-1052, 2026-09-23). It deploys the Modulo
stack; the Kubernetes runtime provider that runs agent workspaces as
long-lived pods also ships (FAR-1051) - enabled with
`MODULO_KUBERNETES_ENABLED` and selected per dispatch via an Environment
Profile with `provider_type: kubernetes`, so agents run under the customer's
ServiceAccount in their own cluster - and its kind conformance gate is green
(FAR-1053), while the scheduled managed-cluster leg has not run yet (its
kubeconfig secrets are not provisioned); see
`docs/deployment/k8s-conformance-parity.md`, or the public reader-facing
guide at https://modulo.run/docs/kubernetes. Docker Compose
(`deploy/compose/docker-compose.prod.yml`) remains the default self-hosted
install; the managed deployment path is Fly.io.

### Redis dependency

Redis is **required** for production: SAQ (the only dispatch path) uses Redis as
its job broker. Redis is also required for:
- Multi-replica coordination (cron triggers, polling, task queues)
- Distributed rate limiting (Redis token bucket)
- WebSocket event broker (Redis pub/sub)

Without Redis: SAQ dispatch, cron firing, and the scheduler are unavailable.
In-memory rate limiting and in-memory event broker are fallbacks for
non-production use.

### Scaling

- **Vertical**: Uvicorn worker processes (`uvicorn --workers`) for multi-core single replica
- **Horizontal**: Multiple backend replicas behind a load balancer. Redis mandatory for coordination. PG advisory locks work cross-replica natively.

### CI/CD Pipeline

Hosted Ubicloud runners (ubicloud-standard-2). Workflows:
- Lint, type-check, unit test, frontend build, audit, and WCAG contrast test on every push
- Containers are built once and published to ghcr.io from `main` (a short-SHA tag plus `:latest`) or, on a release `cut` / `recut`, with versioned tags (`release-unified.yml`). Every GHCR push is gated by image smoke checks (for example `import modulo` and `nginx -t`); the release path also checks `opencode --version` on the runner image. The publish path does not yet scan the built image - the trivy/grype gates, digest-drift guard and cosign/SBOM attest are the D4 GA/CI follow-up item noted above. CI's Trivy check is a filesystem scan of the repository (dependency CVEs + misconfigurations), not a scan of the built images
- Staging smoke, WCAG, and regression suites share one dependency/browser setup while retaining separate result artifacts
- Release: Unified Pipeline (`.github/workflows/release-unified.yml`) owns `cut` / `recut` / `promote` / `dry-run`; it is `workflow_dispatch`-only and semver-driven. The promote path owns stable image and Helm publication; see `docs/release-process.md`

### Observability

OpenTelemetry-native. Default exporter: stdout JSON. Configurable OTLP endpoint (gRPC or HTTP) for Jaeger, Grafana Tempo, or any OTel-compatible backend. Optional LangSmith exporter. Pre-built Grafana dashboards for pipeline performance, HITL review, and cost tracking.

### Supporting Resources

- [System Requirements](./system-requirements.md) – minimum resources, supported databases
- [Configuration Reference](./configuration-reference.md) – full environment variable reference
- [Deployment Guide](./deployment.md) – production deployment instructions
- [Deployment Journeys](./deployment-journey.md) – three deployment paths
- [Upgrade Process](./upgrade-process.md) – upgrading existing deployments
- [Public Launch Checklist](./public-launch-checklist.md) – production readiness verification

---

## Architecture Decision Records

ADRs document key trade-offs and are maintained alongside this project's private development tooling (migrated out of this repo 2026-09-02, FAR-434).

| ADR | Title | Status |
|-----|-------|--------|
| 001 | Agent Execution Environment as a V1 Primitive | Implemented (provider/tier model superseded by ADR 029) |
| 002 | Multi-Backend Database Abstraction Strategy | Draft |
| 044 | Agent Dispatch Model | Supersedes ADR 001 |
| 003 | Packaging & Distribution Strategy | Draft |
| 045 | Agent as a Self-Contained Bundle | Accepted |
| 004 | User Offboarding Uses Deactivation (Not Hard Deletion) | Accepted |
| 046 | Agent Architecture: Two-Tier Orchestration + Execution | Superseded by ADR 029 |
| 005 | Self-Hosted Deployments Use One Org; Teams Are the Separation Boundary | Active |
| 006 | Dashboard Performance: Application Cache Over Materialized View | Active |
| 007 | Assistant UI Commands: Frontend-Mediated Browser Automation | Active |
| 008 | Core Shared Manifest: Single Source of Truth for Page Structure | Active |
| 009 | Frontend Monitor Backend Abstraction | Accepted |
| 010 | Integration Tier Classification (Native / Preview / In-Dev) | Accepted |
| 011 | Assistant Context Sources: Configurable Knowledge Domains with Progressive Disclosure | Active |
| 012 | Migrate to Managed Fly Postgres | Proposed – implementation deferred until production data warrants backups |
| 014 | Assistant Stream: JWT as MCP API Key | Accepted |
| 015 | Bundle Format v2 (YAML) | Accepted |
| 016 | Agent Log Observability | Accepted |
| 017 | Celery to SAQ Migration | Accepted |
| 047/018 | Centralized Authorization: Shared Permission Registry for REST + MCP | v9 – revised after 7 plan-review-iterate cycles |
| 019 | Cost Formula Engine + E2B Rate/Fallback Decision | Accepted |
| 020 | Analytics: run_daily_facts + typed-params query surface | Accepted |
| 025 | Generic REST Integration Connector | Accepted |
| 029 | Agent Execution Tiers + the Bundled Runner | Accepted |
| 038 | RBAC as a Security Boundary: One Rule, One Principal | Accepted |

Note: duplicate ADR numbers were resolved by FAR-1157 (devtools commit `b65b5de`): where two files shared a number, the later arrival was renumbered (003 Agent Dispatch Model → 044, 004 Agent as a Self-Contained Bundle → 045, 005 Agent Architecture: Two-Tier → 046, 017 Centralized Authorization → 047, 021 E2B Session Liveness → 048, 025 Live-Edit History + Release Channels → 049 and Execution-Graph Router/HITL Nodes → 050, 042 Managed Sandbox Adapter Pattern → 051), so each number now names exactly one ADR file (the keeper files: 003 Packaging & Distribution, 004 User Offboarding, 005 Single-Org Self-Hosted, 017 Celery to SAQ, 021 Worker Resilience, 025 Generic REST Integration Connector, 042 Governed Run Contract, which kept their numbers). Centralized Authorization still exists as two copies, `047-centralized-authorization.md` (superseded by the canonical `018-centralized-authorization.md`), so it is listed once above under the combined number 047/018.

## Import Contracts (enforced by import-linter)

The eight contracts in `backend/.importlinter`, all `forbidden` type. Each has explicit
`ignore_imports` seams where a deliberate exception exists.

| Contract name | Source | Forbidden |
|---|---|---|
| API layer uses pipeline_engine, not LangGraph directly | `modulo.api` | `langgraph` |
| Connectors layer is independent of API | `modulo.connectors` | `modulo.api`, `modulo.auth` |
| Core is zero-coupled to modulo-cloud | `modulo.core`, `modulo.api`, `modulo.connectors` | `modulo_cloud` |
| OTel bridge does not import core business logic | `modulo.otel_bridge` | `modulo.core.pipeline_engine`, `modulo.core.hitl_manager`, `modulo.core.eval_engine` |
| Core layer never sees FastAPI | `modulo.core` | `modulo.api` |
| DB layer has no business logic imports | `modulo.db` | `modulo.core`, `modulo.api` |
| Connectors don't reach into core | `modulo.connectors` | `modulo.core` |
| Concrete runtime providers are only imported inside `core/runtime_provider` (FAR-587) | `modulo` | `modulo.core.runtime_provider.{docker,e2b,k8s,local,local_docker}` |

## Testing Strategy

| Layer | Tool | Speed | DB |
|-------|------|-------|----|
| Unit | pytest | <30s | None (mocked) |
| Integration | testcontainers | <2m | Real Postgres |
| BDD | pytest-bdd | <5m | Real Postgres |
| E2E | Playwright | <10m | Real Postgres + Frontend |

Coverage: `[tool.coverage.run]` in `backend/pyproject.toml` measures **branch** coverage
(not just line coverage) over `src/modulo`, and `[tool.coverage.report]` sets
`fail_under = 89`. There is no per-module threshold in that config; the modules named as
targets in earlier drafts (`modulo.auth`, `pipeline_engine`, `db.rls`) are instead the
mutation-testing (`mutmut`) `source_paths` alongside `core/license.py` and
`core/eval_engine/`.

### Frontend API-path guard

`backend/tests/architecture/test_frontend_api_paths_resolve.py` extracts every `/api/` string and template literal from `frontend/src` product code, normalises it (`${expr}` and `:param` → `{param}`, query/fragment stripped), and asserts it resolves (RFC 6570 segment semantics) against the `paths` keys of the generated OpenAPI schema (`frontend/src/lib/api/schema.ts`). A literal that resolves to no schema path fails CI with its `file:line`, unless it is listed in that test's `ALLOWLIST` with a reason. This catches frontend calls to endpoints that were renamed or removed on the backend, which unit tests cannot see because they mock the HTTP layer.
