# Architecture Guide

Modulo is a self-hosted agent governance platform for building governed, repeatable AI-assisted software delivery pipelines. This document covers the system architecture, tech stack, key components, data flow, database schema, authentication, and deployment.

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

Built on LangGraph's `StateGraph` with `dict[str, Any]` state. Each pipeline snapshot compiles to a StateGraph at run-start. Node types: `agent`, `sandbox_agent`, `manual` (human output), `composite` (expand-only), `router` (ordered JMESPath rules + `default`, lowers to the conditional-edge compile path, FAR-402 P1), `hitl` (human-in-the-loop gate that compiles to the existing synthetic-gate path, FAR-402 P1), and `join` (fan-in for scatter/gather patterns). `connector` is an internal engine resolution, never an API-authored node type. Edges carry HITL gate config, rejection routing, or a `loop`/`conditional`/`normal`/`reject` edge type.

Key design:
- Compiled graphs cached by `(pipeline_id, snapshot_id)` with LRU eviction
- `run_context` and `artifact` are sibling keys in state – context-setter-only write enforcement
- Human/manual nodes produce `interrupt()` in LangGraph
- `@cancellable_node` decorator wraps every node for graceful cancellation and per-node timeouts (`asyncio.wait_for`)
- Pipeline nesting max depth: 3 levels

### Eval Engine (`modulo/core/eval_engine/`)

Post-node automated quality checks. Runs before any HITL gate check on the same edge. Supports four eval types:

| Type | Description |
|------|-------------|
| `llm_judge` | LLM-as-judge – passes agent output to a model for scoring |
| `regex` | Pattern match against output |
| `json_schema` | Validate output against a JSON Schema |
| `custom_function` | User-defined Python function |

Each eval has a pass threshold and failure behaviour: `warn` (soft – run continues) or `block` (hard – run fails at this node). Eval results feed into the Feedback System.

### HITL Manager (`modulo/core/hitl_manager/`)

Manages Human-in-the-Loop gates using LangGraph's `interrupt()`. Atomic claim semantics via `SELECT ... FOR UPDATE` on `hitl_claims` table. Claim tokens are opaque random strings (alpha) or short-lived JWTs (v1).

Features:
- `human_only` flag – blocks LLM approval via MCP; defaults to `true` (FAR-609) so every gate is human-only unless it explicitly opts out, and non-browser credentials cannot claim or decide such gates (REST claim route + MCP review_hitl claim both enforce it). Reject is not an agent escape hatch: it requires a claim token, and non-browser principals can no longer claim a default-human_only gate, so only a principal already holding a claim can reject; agent-only runs on default gates require browser-human intervention (intended policy)
- `required_team_id` – restricts claims to specific team members
- Claim expiry background job (default: 60s interval, Postgres advisory lock for single-worker execution)
- `manual` node type – same as HITL but human provides full output
- `hitl` node type (FAR-402 P1) – a draggable human-in-the-loop gate; compiles to the same synthetic-gate path as a legacy edge-level HITL gate. `manual` remains the non-gating human-output step.

**Decision-payload contract (normative, FAR-541):** every resume decision is a dict `{"action": <verdict>, "gate_id": <the identity it resolves>}` plus any per-action members (`output`, `modified_output`, `reason`, `notes`). `HITLManager._decide` is the single stamp authority: it stamps a payload that lacks `gate_id` with the claim row's gate id and refuses (422) a payload stamped for a *different* gate; call-site stamps (API routes, MCP) remain because they feed the direct `executor.resume` injection that bypasses `_decide`. A decision is honoured ONLY by the gate/node its stamp names: every consumer verifies the stamp against its own identity and fails closed on a missing/foreign stamp (re-interrupt, never resume):

| Writer | Stamp (`gate_id`) | Consumer | Recognized actions |
|---|---|---|---|
| `POST /runs/{id}/hitl/{gate}/approve`, `/approve-with-modification`, `/reject`, `/deliver-manual`; MCP `review_hitl` | the gate id from the URL | `_hitl_gate_resume_result` | `approved` (incl. `modified_output`), `rejected`, `deliver_manual` |
| `POST /runs/{id}/manual/{node}/submit` | the manual node's id | `_manual_node` | any dict payload stamped with this node's id completes the node (the documented writer is `manual_output` (+ `output`)) |
| `POST /runs/{id}/nodes/{node}/recover` (operator break-glass) | the run's pending claim row's gate id (node id for manual nodes; the guardrail gate id for conformance blocks); unstamped when no undecided row exists | `_manual_node` / `_handle_conformance_resume` | `skip`, `replay` |
| Conformance override via HITL API | the blocked node id or the block's guardrail gate id | `_handle_conformance_resume` | `approved`, `deliver_manual` (override); `rejected` fails closed |

Interrupt payloads carry the same identity: the gate node interrupts with its `gate_id`, a manual node with `gate_id: <node_id>`, a conformance block with the block's guardrail gate id. The executor keys the pending `hitl_claims` row on that `gate_id` verbatim. The dispatcher reconcile resumes an `awaiting_human`/`claimed` run ONLY per this scoping matrix: claimed-undecided, skip (under the `uq_hitl_claims_run_gate` `UNIQUE (run_id, gate_id)` constraint a claimed-undecided row and a committed decision for the same gate cannot coexist; crash recovery for claimed runs routes through the no-undecided-rows branch once the decision commits); unclaimed undecided row, conservative skip; no undecided rows, crash-recovery resume when the decision's stamp routes it to a consumer that accepts it. `hitl_gate_*`/guardrail identities accept only the verdict actions; MANUAL-node identities also accept a committed `manual_output` with its `output` (legacy pre-stamping rows are stranded by design, at most the 2026-09-02 incident cohort; ops remedy is a manual DB stamp or ticket, no backfill migration). Recover-node refuses HITL gate targets (422), gate decisions must go through approve/reject; user node ids squatting the reserved `hitl_gate_` prefix are rejected at graph-validation time.

**Gate coalescing (FAR-604 D4):** when a run reaches a HITL gate and an OPEN gate (undecided + unclaimed, same gate id) already covers the same work item on ANOTHER run of the pipeline, matched via the webhook coalesce key stamped on `runs.input_payload`, the gate is NOT raised twice. If the entity SHA (`runs.input_hash`) is unchanged, the duplicate run is terminalised `failed`/`executor_superseded` and the existing gate decides for the work item (the model does not support multiple runs per gate, `uq_hitl_claims_run_gate`, so reuse means skipping the duplicate gate). If the SHA changed, the old gate is auto-closed with a system-committed `rejected` decision (loudly audited as `hitl.gate_superseded`) and the old run, if parked, un-parks so the committed-decision resume machinery terminalises it through the normal reject path, while the new run raises fresh. Claimed gates are never superseded (a human holding the claim is mid-review; the claim TTL + a later raise close the loop).

### Connector Hub (`modulo/connectors/`)

Abstraction over external tool integrations. ConnectorType defines an abstract capability category (e.g. `git-host`, `shell`). ConnectorInstance is a configured, authenticated binding. ConnectorHub decrypts credentials once at run-start into a run-scoped context object – credentials never enter LangGraph state, checkpoints, OTel spans, or logs.

| Connector | Type | Operations |
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
| `TrivyConnector` | `security` | scan container images |
| `CodeClimateConnector` | `quality` | query code quality metrics |
| *(41 built-in connectors total; see `modulo/connectors/`)* | | |

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
| *(27 model backends total; see `modulo/model_backends/`)* | |

### Trigger Engine (`modulo/core/trigger_engine/`)

Accepts manual, webhook, cron, polling, and agent_signal trigger types. Creates Run records and initiates pipeline execution. Webhook flood protection via Postgres `SELECT ... FOR UPDATE SKIP LOCKED`. Payload deduplication via `webhook_dedup_hashes` table with configurable window.

### Audit Logger (`modulo/core/audit_logger/`)

Immutable event recording for all state-changing actions. Written in alpha; viewer/export is team-gated. All events carry `organisation_id`, `actor_id`, `action`, `resource_type`, `resource_id`, and `timestamp`.

### Notification System (`modulo/core/notifier/`)

Push notifications (WebSocket events) and outbound webhooks. Per-endpoint HMAC-signed delivery with 3 retries and dead-letter logging. Endpoints auto-disable after repeated failures. Team-scoped notification endpoints.

### Runtime Provider Hub (`modulo/core/runtime_provider/`)

Agent execution environments for the Runner tier (ADR 029: Agent Execution Tiers + the Bundled Runner). Modulo has exactly two node execution mechanisms: the **Inline Prompt** (`node_type: agent`), an in-process model call in the SAQ worker resolved through the Model Backend Hub with no isolation, and the **Runner** (`node_type: sandbox_agent`), where the agent runtime executes inside a provisioned workspace (provision -> execute -> collect structured output). The RuntimeProvider ABC (parallel to ConnectorHub/ModelBackendHub) resolves the EnvironmentProfile for a Runner dispatch deterministically (delivered by D2): an explicit `provider_hint` or `provider_type` match wins, and anything unresolvable raises `ProviderNotConfiguredError` naming the env var that would register the provider; there is no silent fallback. Providers: `local` (always registered, host processes; its provider-neutral `workspace_metadata` is ignored), `e2b` (registered when `MODULO_E2B_API_KEY` is set; metadata maps to E2B sandbox metadata), and `runner_docker` (registered when a `MODULO_RUNNER_*` variable or a Docker endpoint (`MODULO_DOCKER_HOST`/`DOCKER_HOST`) is configured; `docker` and legacy `local_docker` are explicit aliases of the same Docker tier). Runners come in three packagings of the same tier: **Bundled Runner (Docker)** (the `runner_docker` provider ships with D2; D4 completes it with the first-party runner image and the compose overlay behind a filtered socket-proxy), **remote Docker** (the same provider pointed at a remote engine via `MODULO_DOCKER_HOST`), and **External Runner (E2B)** (the operator's own E2B account via `AsyncSandbox.create`), plus the bare `local` provider tier, counted by the capacity gate alongside Docker. Hubs are fresh per `build_hub()` factory call (no singleton) and provider-owned clients are released via `aclose()`. D2 removed the unused WorkspaceLease scaffolding, including its API reader (FAR-587): workspace state lives in `runs.sandbox_dispatch_state`, and `GET /runs/{run_id}/workspace-lease` answers a deliberate 410. D8 will replace the dispatch-time capacity check with an atomic advisory-locked gate accounting Runner capacity by run dispatch-state.

#### Bundled Runner (Docker) packaging (D4, FAR-590)

The Bundled Runner is the self-hosted default Runner packaging: a first-party `modulo-runner:opencode` image (`deploy/docker/runner-opencode.Dockerfile` – digest-pinned base, version-pinned opencode via the `OPENCODE_VERSION` build arg, non-root `runner` uid 1001, tini PID 1) executed as a hardened workspace container on the deployment's own Docker engine.

- **Dispatch adapter (`modulo/core/bundled_runner/runner_dispatch.py`)** – the sandbox_agent dispatch branches on the PIPELINE-LEVEL bound profile's provider type (the same-org-enforced `PipelineSnapshot.environment_profile_id`, consumed at dispatch). `runner_docker` routes through a per-dispatch `build_hub()`-resolved Docker provider with both `sandbox_mode` values supported; the E2B path is untouched and gains a LOUD dispatch-time timeout validation (GraphValidator parity at <=3300, no silent clamp). The D4 upgrade rule: pipeline-level refs to providers that were never dispatch-relevant (`local`, legacy-inert `local_docker`) raise a typed `SandboxDispatchUnboundError` at dispatch instead of silently activating old bindings.
- **Streaming exec primitive** – the RuntimeProvider ABC gains `exec_command_stream` (async decoded chunks + a kill handle, `ExecProcess`) alongside the collect-then-return `exec_command`. Script-mode's live-log drain and stall/no-output detection work on Docker exactly as on E2B; an engine/proxy drop mid-stream surfaces as a stream ERROR – the dispatch classifies it retryable and a zero exit code is NEVER fabricated. A stall (no output within the stall window) or a total timeout fires the kill handle.
- **Workspace hardening (at provision)** – non-root user (uid 1001 on first-party images), read-only rootfs + tmpfs `workdir`/`/tmp` (512m/128m), dropped capabilities + `no-new-privileges`, 1.0 CPU / 1 GiB, a dedicated workspace bridge network (never the compose/backend network; per-profile `none` egress opt-in), and provider-neutral `workspace_metadata` mapped to container Labels: `modulo.run.id`, `modulo.org.id`, `modulo.node.id`, plus the machine deployment-identity label (`modulo.machine.id`, from `MODULO_RUNNER_MACHINE_ID` with a hostname fallback) and a creation-marker label.
- **Compose overlay (`deploy/compose/runner.yml`)** – opt-in via the `runner` compose profile. It adds a digest-pinned **filtered socket proxy** (the linuxserver/socket-proxy class; per-category allowlist derived mechanically from the backend's real Docker API surface – containers list/create/start/stop/remove + inspect + exec-create, exec-start/inspect, images inspect/list/create, ping/version/info; no networks endpoints, the workspace network is compose-defined) as the DEFAULT Docker endpoint via `MODULO_DOCKER_HOST`, and guarantees the dedicated `modulo-runner-workspace` bridge network exists (compose never creates unreferenced networks under `--profile` – the holder service references it). Workspaces never join the compose network, so a provisioned workspace cannot reach the proxy endpoint; the proxy publishes no host ports. Full posture + residual exposure: `docs/security/bundled-runner-trust-boundary.md`; setup: `docs/security/bundled-runner-operator-guide.md`.
- **Orphan reconciler (`runner_reconciler.py`, system cron every 5 min)** – leak repair without WorkspaceLease: lists labelled workspace containers (machine-scoped by the deployment-identity label), cross-references active runs (fail-safe: ANY cross-reference error aborts the sweep destroying nothing and emits `runner.reconciler.sweep_aborted`), destroys orphans older than the 5-min grace period with a destroy-path false-positive re-check (`runner.reconciler.suspected_false_positive` is the D4 rollback signal), applies the 24h max-lifetime backstop regardless of run state (`runner.workspace.reclaimed_max_lifetime`), and runs in a LOG-ONLY soak mode until `RUNNER_RECONCILER_DESTROY_ENABLED=true`. Liveness rides the shared sweep-stats contract (`saq:cron:stats:runner_workspace_reconcile`), surfaced as the advisory `runner_workspace_reconcile` check on `/healthz/ready`.
- **Seeded Environment Profile "Bundled Runner (Docker)"** – provider `runner_docker`, per-minor release-advanced digest constant (`modulo/db/bundled_runner_template.py`; migration 0191 mirrors it and the release job's digest-drift guard asserts they match), hardening + network preset, and `persistence_policy` LOCKED to `ephemeral` (CRUD validator rejects `retained`/`cache` for `runner_docker`, dispatch re-checks). Seeded per org at org-creation; orgs predating the hook receive it via the one-time backfill migration 0191, which re-points the legacy `modulo-dev` `local_docker` row to the Bundled Runner. Template drift is computed live against the shipped constants (`template_drift_status`) – operator-pinned older digests survive and divergent rows surface "shipped template updated - apply" (D5 UI) instead of being silently re-applied.
- **Docker-marked acceptance suite** – `backend/tests/docker/` (marker `docker`, run explicitly with `pytest -m docker`; `MODULO_RUNNER_DIND_TESTS=1` opts into the dind engine-kill strip) verifies hardening, streaming live output, container/engine kill classifying retryable (never `ExecResult(0)`), destroy, reconciler orphan destroy + active-run spare, and the proxy allowlist matrix with the runtime completeness assertion (the exercise must trigger no proxy rejection). CI/compose wiring of this suite plus the GHCR publish job (structure test, trivy/grype gates, digest-drift guard, cosign/SBOM) is the D4 GA/CI follow-up item.

### Auth System (`modulo/auth/`)

Authentication and authorization – JWT, API keys, OIDC/SAML (v1), Basic Auth (alpha). Dual-layer scope enforcement for MCP (middleware + ViewModel command layer). See dedicated section below.

### Schema Registry (`modulo/core/schema_registry/`)

Versioned JSON Schema definitions (Draft 2020-12). Schemas are org-scoped, versioned (semver), reusable, and composable. Abstract schemas enable type-constraint matching during workflow import. Schema inference generates draft schemas from sample connector data.

### Library Service (`modulo/core/library_service/`)

Manages the local and community library of reusable primitives (agents, schemas, workflows, integrations). Community primitives are Ed25519-signed. Copy-to-adapt via `CopyToAdaptWizard` UI component (ownership picker + optional binding step).

### Declarative Configuration CLI (`modulo/cli/apply/`) – FAR-681

`modulo apply -f <config.yaml>` applies an org's schemas (+ versions), model backends, pipelines and triggers to a live deployment from one YAML file (`api_version: modulo.dev/v1`), driven by `MODULO_URL` + `MODULO_API_KEY` (bearer `mk_` org key). Planning is name-based upsert (RLS-bound to the key's org): each entity chooses created / updated / unchanged / blocked from a canonical managed-field hash, so rerun is idempotent and runtime state (`next_fire_at`, `streak_epoch`, ...) never causes drift. Keys: entities apply in dependency order with per-entity containment; secrets are refs-only (`${env:VAR}` / `secretref://<key>` -- inline literals are a validation error; the server masks stored secrets, so `--refresh-secrets` re-sends trigger configs whose secrets rotated); backend writes are health-check-verified; `--dry-run/--plan` reports without writing; `--diff` is a read-only drift report (plan-shaped, labelled `mode=drift`, with graph node/edge breakdown for drifted pipelines) used as a CI gate -- exit 0 when the org matches the config, exit 1 on drift (created/updated/blocked), and real apply exits 1 on any blocked/failed entity.

## Data Flow

### Pipeline run lifecycle

1. **Trigger** – A trigger fires (manual POST, webhook HMAC-verified, cron schedule, or agent_signal). TriggerEngine validates input against the entry agent's `input_schema`. A Run record is created in `pending` status. TriggerEvent is logged.

2. **Snapshot** – The pipeline's current definition is frozen as a PipelineSnapshot (all agent versions, schema pins, connector bindings, model backend pins, environment profile). The run now executes against this immutable snapshot; the snapshot is tagged `version_kind='run'`.

   **Live-edit history + release channels (ADR 025 / FAR-402 P6):** the snapshot
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
   f. EvalEngine runs configured evals (llm_judge, regex, json_schema, custom_function)
   g. If eval fails with `block` behaviour, run enters `failed` state
   h. If the outgoing edge has a HITL gate, `interrupt()` pauses the run

5. **HITL** – A human claims the gate (atomic DB lock), inspects context, and approves or rejects. Approval continues to the next node; rejection routes to the reject-target node (or produces a FeedbackRecord).

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
org triggers pause). Four independent mechanisms keep that gate healthy:

- **Slot reconciliation sweep:** a system cron (every 5 min) terminalises
  `running` runs whose heartbeat is stale past `SLOT_RECONCILE_STALE_SECONDS`
  (default 30 min) with the `worker_lost` error code, force-releasing the
  pipeline slots a crashed worker leaked. Journeys and daily facts advance for
  each released run.
- **HITL park-on-expiry sweep:** a system cron (every 5 min) parks a run whose
  open HITL gate expired UNANSWERED past `HITL_PARK_GRACE_SECONDS` (default
  24h): the run moves `awaiting_human` to `hitl_parked` (a non-terminal status
  that holds no pipeline capacity). The STATUS itself is the parked signal
  the HITL UI reads to show "expired, parked". Park is not decide: the gate row
  stays OPEN AND CLAIMABLE (a claim takes a fresh TTL), and the moment a
  decision commits
  (`HITLManager._decide`, API or MCP) the run un-parks to `awaiting_human` and
  re-enters normal admission: approve resumes from the checkpoint through the
  normal resume path, reject terminalises via the reject path. Each park is
  logged loudly (`hitl_park.parked`).
- **Queue coalescing (latest-wins):** for webhook deliveries with a stable
  work-item key (GitHub: `repository.full_name` + `pull_request.number`, or
  `issue.number`; anything else, no key, no coalescing), a new delivery folds
  into the pipeline's UNSTARTED `pending` run for the same key instead of
  inserting a row: the pending run's input payload is replaced and its
  `created_at` bumped, and a `coalesced` TriggerEvent is recorded. On by
  default; disable per trigger with `config_json.coalesce_pending: false`.
  Replays never coalesce.
- **Dispatcher backpressure:** trigger dispatch (webhook, cron, polling)
  refuses NEW runs when the pipeline's pending queue exceeds
  `max(3 x max_concurrent_runs, 5)` rows or its oldest pending run is older
  than `TRIGGER_BACKPRESSURE_MAX_AGE_SECONDS` (default 60 min). Refusals are
  loud: a `backpressure_skipped` TriggerEvent carries the depths, and the
  webhook path answers 429 so the sender retries.
- **Legacy stale-run sweep:** pending runs past the never-dispatched window
  (`SAQ_NEVER_DISPATCHED_WINDOW`), capacity-marked runs past the TTL
  (`capacity_timeout`), and legacy non-SAQ `running` rows with 5+ claims
  (`worker_lost`) are terminalised or re-dispatched as before.

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
  │   ├── PipelineEdge (pipeline_id, source, target, edge_type, hitl_gate_config)
  │   └── Run (pipeline_id, snapshot_id, status, state machine)
  │       ├── hitl_claims (run_id, gate_id, claimed_by, claim_token, expires_at)
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
- `(run_id, gate_id)` unique on `hitl_claims` – one claim per gate per run
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
| JWT (access + refresh) | Alpha | Browser UI sessions – 15-min access, 7-day refresh |
| API key (bearer token) | Alpha | CI/CD, MCP clients – role-scoped (operator/runner) + caller-scoped (`org`/`user` scope axis, ADR 030) |
| Basic Auth | Alpha | Multi-user alpha (`MODULO_USERS` env var) |
| OAuth 2.0 (authlib) | V1 | MCP clients (PKCE, exact redirect_uri) |
| OIDC / SAML 2.0 | V1 (team) | SSO with JIT provisioning |

### JWT Security

- Access tokens: 15-min expiry
- Refresh tokens: 7-day expiry, rotated on use
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

### Row-Level Security

All tenant isolation is at the database layer via `SET LOCAL app.organisation_id` inside transactions. Every query runs within the org scope. This prevents cross-tenant leaks even if application-level scoping is bypassed. Team-visibility resources return 404 (not 403) for non-members – no existence enumeration.

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
| HITL review actions (`/api/v1/runs/{run_id}/hitl/{gate_id}/{action}` and `/api/v1/runs/{run_id}/manual/{gate_id}/submit`, POST) | 20 per user (aggregate) | 60s |
| `/mcp` | 200 | 60s |
| Auth endpoints (`/api/v1/auth/`) | 10 attempts | 60s (configurable via `MODULO_AUTH_MAX_ATTEMPTS`) |

Redis-backed sliding window (ZADD + ZREMRANGEBYSCORE). Falls back to in-memory no-op when Redis is unavailable. Auth rate limiter requires Redis and is disabled without it.

The HITL budget is AGGREGATE per identity (JWT user, API-key prefix, or IP) across runs, gates, review actions, AND both surfaces; the bucket key normalizes the whole variable tail (FAR-611), so rotating gates, runs, or actions cannot dodge the 20/min cap (the 2026-09-05 bulk-approve sweep spread 22 decisions across per-gate buckets and was never throttled). The manual-output submit route (`/runs/{run_id}/manual/{gate_id}/submit`, an approve-capability HITL surface whose path has no `/hitl/` segment, shares the SAME aggregate bucket, so a sweep alternating `/hitl/` review actions and `/manual/` submits exhausts one budget. MCP review actions sit behind the general `/mcp` 200/min rule rather than the HITL rule; they are machine-surface, human_only gates are already denied there, and tightening the MCP budget is follow-up work if MCP-side sweeps ever warrant it.

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

The Kubernetes/Helm example deployment configs were removed – they were never
exercised by CI or used in production. Self-hosting is via Docker Compose
(`deploy/compose/docker-compose.prod.yml`); the managed deployment path is Fly.io.

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
- Each backend/frontend container is built once, scanned with Trivy, and published to ghcr.io only from `main` or a version tag
- Staging smoke, WCAG, and regression suites share one dependency/browser setup while retaining separate result artifacts
- Release workflow (tag-driven, semver)

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

ADRs live in the private `farnalabs/devtools` repo at `Repos/devtools/adr/` (migrated out of this repo 2026-09-02, FAR-434; they were previously in-repo under `docs/adr/`). They document key trade-offs:

| ADR | Title | Status |
|-----|-------|--------|
| 001 | Agent Execution Environment as a V1 Primitive | Implemented (provider/tier model superseded by ADR 029) |
| 002 | Multi-Backend Database Abstraction Strategy | Draft |
| 003 | Agent Dispatch Model | Supersedes ADR 001 |
| 003 | Packaging & Distribution Strategy | Draft |
| 004 | Agent as a Self-Contained Bundle | Accepted |
| 004 | User Offboarding Uses Deactivation (Not Hard Deletion) | Accepted |
| 005 | Agent Architecture: Two-Tier Orchestration + Execution | Superseded by ADR 029 |
| 005 | Self-Hosted Deployments Use One Org; Teams Are the Separation Boundary | Active |
| 006 | Dashboard Performance: Application Cache Over Materialized View | Active |
| 007 | Remy UI Commands: Frontend-Mediated Browser Automation | Active |
| 008 | Core Shared Manifest: Single Source of Truth for Page Structure | Active |
| 009 | Frontend Monitor Backend Abstraction | Accepted |
| 010 | Integration Tier Classification (Native / Preview / In-Dev) | Accepted |
| 011 | Remy Context Sources: Configurable Knowledge Domains with Progressive Disclosure | Active |
| 012 | Migrate to Managed Fly Postgres | Proposed – implementation deferred until production data warrants backups |
| 014 | Remy Stream: JWT as MCP API Key | Accepted |
| 015 | Bundle Format v2 (YAML) | Accepted |
| 016 | Agent Log Observability | Accepted |
| 017 | Celery to SAQ Migration | Accepted |
| 017/018 | Centralized Authorization: Shared Permission Registry for REST + MCP | v9 – revised after 7 plan-review-iterate cycles |
| 019 | Cost Formula Engine + E2B Rate/Fallback Decision | Accepted |
| 020 | Analytics: run_daily_facts + typed-params query surface | Accepted |
| 025 | Generic REST Integration Connector | Accepted |
| 029 | Agent Execution Tiers + the Bundled Runner | Accepted |

Note: ADR numbers 003/004/005 are shared by two distinct ADR files each (the numbering mirrors the filesystem). ADR 017/018 – Centralized Authorization – exists as both `017-centralized-authorization.md` and `018-centralized-authorization.md` (a duplicated file), so it is listed once here under the combined number.

## Import Contracts (enforced by import-linter)

- `modulo.api` must not import `langgraph` directly
- `modulo.connectors` must not import `modulo.api` or `modulo.auth`
- `modulo.core`, `.api`, `.connectors` must not import `modulo_cloud` (removed; this contract is retained as a forward-compatibility guard)
- `modulo.otel_bridge` must not import `core.pipeline_engine`, `hitl_manager`, `eval_engine`

## Testing Strategy

| Layer | Tool | Speed | DB |
|-------|------|-------|----|
| Unit | pytest | <30s | None (mocked) |
| Integration | testcontainers | <2m | Real Postgres |
| BDD | pytest-bdd | <5m | Real Postgres |
| E2E | Playwright | <10m | Real Postgres + Frontend |

Coverage targets: `modulo.auth` 90%, `pipeline_engine` 85%, `db.rls` 95%, overall 80%.
