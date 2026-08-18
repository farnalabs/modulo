# Modulo — Product Requirements Document

**Version**: 0.36
**Date**: 2026-08-15
**Status**: Pre-development
**Changelog**:
- v0.36 — §8.5 Trigger System: ongoing-trigger no-delivery auto-deactivation (FAR-188/189/190). After N consecutive no-delivery terminal runs an ongoing trigger auto-deactivates, notifies, and requires an operator re-enable. Counts empty-backlog completes + infra/sandbox failures; delivered runs reset, `cancelled`/`budget_exceeded` are excluded; config keys `max_no_delivery_streak` (default 5) / `no_delivery_min_window_hours` (default 24h); `MODULO_STREAK_DEACTIVATE_KILL_SWITCH`, per-org per-hour cap (10), mass-cascade alert (5/24h).
- v0.35 — §8.31 Lifecycle Map Journeys: work-item journeys minted from run work_item_refs, map-scoped journey reads with keyset pagination, journey detail with run history + provenance badges, advancement service (compare-and-set stage resolution), self-report parse, reconciliation, persisted map version history, stage junction (one active map per pipeline), restore endpoint. §8.26.2 sidebar restructure to 4 flat groups (BUILD/MONITOR/CONFIGURE/ADMIN, no subgroups). Team Comparison + API Changelog features removed (PR #1018).
- v0.35 — §8.2 Trigger Entity + §8.7 concurrency: new `ongoing` trigger type (FAR-158) that keeps a pipeline topped up to a target number of in-flight (active or queued) runs, like a daemon/worker pool. Scheduler scan (~60s) + per-item top-up job; `pending`/`running`/`claimed` counted, `awaiting_human` excluded; effective pool = `min(trigger, pipeline)` cap; required daily spend limit (DB partial CHECKs); snapshot pin/latest/auto-create; persistent-failure deactivation; `ongoing_trigger` feature flag.
- v0.34 — §8.32 Analytics: rolling-window run/cost/quality series (Last 24h/7d/30d/90d) over a retained `run_daily_facts` table, typed-params query surface (no query language in this delivery), per-org `analytics_page` feature gate. ADR 020.
- v0.33 — §8.31 Lifecycle Map: declarative multi-diagram SDLC model with stage node types (`modulo`\|`external`\|`manual`\|`placeholder`), transition edge trigger metadata, fractal double-click navigation, graduation path from model-only to Modulo-managed. v0.31.
- v0.32 — §8.29 Remy Context Sources: configurable knowledge domains with always-on/tool/off modes, per-skill source_mode, `source_contexts` field on RemyConfig, 4 new MCP retrieval tools (search_documentation, get_integration_status, get_org_config, get_available_features). §8.30 Remy Product Primer: auto-generated always-on product overview in system prompt, primer generator script reading PRD + product map + manifest + live counts. ADR 011.
- v0.31 — §8.25.1 Frontend Monitor Backend Abstraction: plugable MonitorBackend interface (builtin/sentry/datadog-rum/grafana-faro), dual-layer config (build-time VITE_* + runtime MODULO_MONITOR_CONFIG), ErrorTracker refactor with MonitorBackendRegistry dispatch, CSP superset strategy, per-backend privacy data sheets, unauthenticated error ingest endpoint, i18n missing-key capture, 2 existing pipeline bugfixes. ADR 009.
- v0.30 — §8.28 Core Shared Manifest: single YAML source of truth for routes, elements, sidebar, permissions, tiers, product map refs, i18n keys. Binary consumption (frontend Vite import + backend startup load). `get_manifest(path?)` Remy tool. 7-rule pre-commit + CI validator. ADR 008.
- v0.29 — §8.27 Remy UI Commands: frontend-mediated browser automation for Remy, 11 UI commands (navigate, click, fill, select, extract, extract_all, get_page_interactables, wait, go_back, get_url, press), permission system with 3 modes + destructive selector detection, agentic loop (multi-turn LLM within single SSE stream), shadcn/vue component support, visual feedback (highlighting, toast, overlay, turn separators), 3 new endpoints, ADR 007.
- v0.28 — §8.26 Navigation Restructure: Breadcrumb component with route meta chain, sidebar hierarchical subgroups (SidebarSubgroup), new Remy group consolidating user skills + admin config, secondary nav tab bars (PageTabs), back-to-parent links on 5 detail views.
- v0.27 — §8.25 Native Error Tracking: backend + frontend error capture, Postgres-backed storage, fingerprinting/dedup, admin dashboard, built-in alerting, Prometheus metrics, external forwarders to Sentry/DataDog/PagerDuty/Rollbar/OpsGenie/Grafana Loki; Community tier for core, Team tier for integrations.
- v0.26 — §8.23 Remy In-App AI Assistant: floating draggable/dockable/maximisable chat panel on every page, page awareness via `useRemyContext()`, Multi-window independent sessions with last-activity-winner, tool execution via ViewModel API + MCP server, Markdown skill loading from `remy_skills` table (org-level admin-managed + user-level self-service), context-window-aware conversation reconstruction with automatic pruning and summarization, `chat_sessions` + `chat_messages` + `remy_skills` data model, full CRUD API + SSE streaming endpoint, admin config page at `/admin/remy`, Team-tier feature gate with org-level access list.
- v0.25 — §8.22 SSE Event Bus (Real-Time Frontend Sync): in-memory EventBus with optional Redis overlay for multi-worker, SSE endpoint at GET /api/v1/events, SQLAlchemy event listeners for automatic publishing, frontend EventSource composable with dirtyIds conflict detection pattern.
- v0.24 — Tier rename: free→Community, enterprise→Team across all UI text, API responses, backend code, docs, and tests. Community Edition (free, no license key) and Team Edition (self-serve paid, feature-gated, no SLA/support commitment). §6.2 updated to reflect new naming; §6.2.1 Tier System Architecture added describing the future-state flexible tier catalog.
- v0.23 — §8.21 View Modes (Enterprise): multiple named UI views with admin-defined feature visibility per view, assignment to users/teams/org roles, enforcement, self-lockout prevention guards; `view_modes` enterprise feature flag replaces previously planned `view_mode` + `view_mode_enforcement`
- v0.22 — Enterprise tier clarified: no SLAs, no dedicated support, no bespoke services. Enterprise = self-serve feature gate only (SSO, RBAC, audit viewer, admin spend limits). Pricing page updated. BSL 1.1 LICENSE file created at repo root; `Dev-Harness/tools/release.ps1` release script created with placeholder steps for Docker Hub, GitHub release, etc.
- v0.21 — shadcn-vue + Radix Vue added as component library foundation (replaces build-from-scratch UI primitives); tier badge spec (Free/Enterprise pill in sidebar nav footer; lock icon on gated features); `/settings/license` page spec; `planStore` added to Pinia stores; `GET /api/v1/license` endpoint; frontend tech stack table updated
- v0.20 — Licensing and monetization model: BSL/Fair Source with 3-year Apache 2.0 auto-conversion; cryptographic offline license key replaces modulo-cloud plan injection for self-hosted; DefaultPlanContext now defaults to Free Tier (not permissive); enterprise feature gate defined (SSO, team RBAC, audit viewer, admin spend limits); modulo-cloud deferred to V3; billing changed from telemetry-metered to flat annual fee (token counting remains for internal cost controls only); MODULO_LICENSE_KEY env var added; audit event *recording* stays free, viewer/export is enterprise; open question on audit gate documented
- v0.2 — first reviewer critique; shareable workflows, user management, SSO
- v0.3 — security hardening, distribution strategy, community library, runner role, plugin API
- v0.4 — SaaS-first multi-tenant architecture; 2 implementations per primitive; removed time estimates
- v0.5 — RLS `SET LOCAL` fix; API key format; registry Ed25519 signing; model backend management (new); connector_binding spec; webhook payload_mapping; TriggerEvent log; run concurrency controls; modulo-cloud boundary; org migration/deletion policy; prompt versioning; long-run retention; pre-run input validation; rating system spec
- v0.6 — Remote MCP server as first-class MVVM view (replaces LLM driveability stretch goal with standards-based protocol)
- v0.7 — OAuth 2.0 deferred to v1 (API key only in alpha MCP); review_hitl tool merged; human_only HITL flag; SSE conflation fixed; MCP onboarding page; accessibility spec; dual-layer scope enforcement; per-event SSE org validation; pipeline writes browser-only until v2
- v0.8 — Team entity and team-scoped RBAC; pipeline ownership (team vs org visibility); team-scoped HITL gates; team-scoped connector and model backend access; multi-workspace pattern via teams
- v0.9 — Ownership picker on all resource creation; team deletion policy; privilege cap on team operators; JWT stale membership documented + immediate revocation path; DB-live check for required_team_id HITL; view_as_team server-enforced (IDOR fix); human_only + required_team_id additive; Stage spec team ownership; post-snapshot ownership change rules; team notification endpoints; team audit events; owner_team_id stripped on bundle export; copy-to-adapt ownership picker; library primitive visibility; alpha schema includes team columns; team cost attribution moved to v1
- v0.10 — Credential-in-state rule; webhook timestamp in HMAC; FilesystemConnector base_path chroot; schema validation ≠ sanitisation documented; eval injection surface documented; §6.18 API rate limiting; Ed25519 key rotation mechanism; checkpoint blob self-hosted gap documented; JWT algorithm pinning + SECRET_KEY entropy; ConnectorInstance visibility vocabulary unified (private/team-shared → org/team); §7.16 Eval            System (new); Error UX spec; agent picker + schema picker; run inspection UI;     bundle import schema conflict resolution; community library trust tiers; plugin installation mechanism clarified; org/team-level admin spend limits
- v0.20 — §7.19 Break-glass Admin Recovery (consolidated): prevention (org-wide last-admin guard on REST/SCIM mutation surfaces), recovery via operator CLI `modulo-break-glass` (activate/deactivate/status/force-last-admin/smoke) synthesising a single-use TTL-bounded credential consumed by the login-hook compare-and-swap, deny surfaces (API-key mint deny, webhook use-time revalidation, login deny), dedicated `modulo_breakglass` role + `MODULO_BREAK_GLASS_DATABASE_URL`, operator-secret auth, exit-code table 0-9, startup-validated settings, boot-time watchdog + daily `status --all` sweep, trust-anchor residual
- v0.11 — StateGraph compile caching; WebSocket fan-out broker; LangGraph generic dict state (no dynamic TypedDict); ConnectorHub one-decrypt-per-run; StubModelBackend BaseChatModel interface; Alembic+LangGraph startup order; webhook flood protection Postgres-backed; pipeline edge data model; OTel bridge elevated as blocking dependency; AsyncPostgresSaver mandate; claim_token alpha = opaque token; teams/ tests moved to v1; alpha rating system moved to v1; demo mode (added in v0.11, removed in FAR-308); alpha exit criteria; V1 split into V1 Core + V1 Extended;            alpha documentation requirements; API key item moved to Infrastructure; eval JSON column in alpha
- v0.12 — Organisation entity fields; PlanContext interface fully specified; Run entity fields; Trigger entity fields; PipelineSnapshot fields; YAML bundle edges block; token counting mechanism; cancelled state mechanics; ConnectorType registration (in-memory entry_points); local vs community library data model discriminator; claim_token inconsistency fixed in Glossary and §5.4; WebSocket reconnection + event replay spec; Pinia store hydration path; HITL claim failure UX; Vue Flow canvas serialisation note; MCP server URL via MODULO_PUBLIC_URL; Playwright agent theme test strategy; agent output sensitive data caveat; copy-to-adapt ownership picker cross-reference fixed
- v0.13 — WebSocket event typed patch payloads (hitl_claimed/hitl_reviewed added; Pinia patch strategy); CSS custom property theme mechanism (semantic tokens only; [data-theme] layers); focus ring CSS custom properties (--focus-ring-width/color; never suppressed; agent theme high-visibility); Playwright data-loading attribute convention; modulo-state script block removed (replaced by GET /api/v1/viewmodel/current); CopyToAdaptWizard component spec (multi-step modal with configurable steps); canvas viewport state preserved per drill-down level via Vue Router state
- v0.16 — Vision rewritten around implement+improve dual job; governance/audit/observability named as enterprise table stakes; off-the-shelf library and evals named as primary selling points; existing SDLC onboarding named as third major selling point; ICP sharpened (DevX/software engineer who wants control not SaaS black box); Product Goals restructured into three tiers; competitive positioning section added (§3); §7.16 Schema Inference added (v1 — LLM-assisted schema draft from connected tool data; SDLC onboarding path)
- v0.17 — §7.18 Run Context Propagation (alpha): run_context dict alongside artifact payload; context-setter agent role; complexity-reviewer as canonical library primitive; A/B test seeding via run_context_overrides; §7.19 Run Variants (v1): variant groups, side-by-side eval comparison, eval coverage gap signal; §7.20 Feedback System (v1): FeedbackRecord entity, three feedback handler types (human/ai_correction/ai_correction_with_human_review), AI correction agent spec, correction run mechanics (re-start from target node), eval gap detection and proposed eval curation, eval suite growth flywheel, feedback inbox UI; RunContext/VariantGroup/FeedbackRecord added to glossary
- v0.19 — Subsection numbering fixed throughout: all `### X.Y` subsections now match their parent `## X.` outer section (systematic off-by-one corrected); V1 Core roadmap updated to include Schema Inference, Run Variants/A/B Testing, Feedback System, and complexity-reviewer library primitive (4 v1 features previously missing from roadmap); HITL gate definition gains forward reference to §8.20 for `feedback_handler`/`reject_target` interaction; manual (placeholder) node added to alpha feature checklist; Run Context → Feedback System delivery dependency documented explicitly
- v0.18 — Section numbering fixed (§3 Competitive Positioning inserted cleanly; §4–§15 renumbered); changelog restored to chronological order; run_context_defaults added to PipelineSnapshot; context-setter write enforcement spec (pre-node guard; silent discard + audit warning); correction run checkpoint mechanics (new thread pre-seeded from original checkpoint); reject_target vs feedback_handler interaction resolved (feedback_handler supersedes when set); eval gap detection via standalone EvalEngine.evaluate() path; variant partial completion with HITL; write-log-with-last-write-wins replaces "append-only" contradiction; pipeline-level default_feedback_handler; correction run connector op inheritance rule; variant group run quota (N against limits; pre-flight check); human/placeholder node type for manual SDLC steps; n8n as future roadmap item not current capability; Eval→Feedback delivery ordering; Run Variants pre-eval degraded mode; prompt version comparison in variant groups; alpha exit criteria updated to include improvement loop
- v0.15 — FERNET_KEY separated from SECRET_KEY (distinct key material); SQLite mode limitations documented (dev-only; RLS/advisory locks/flood protection unavailable); MODULO_USERS env var for multi-user alpha Basic Auth; alpha exit criterion #4 fixed (two named users, not two shared passwords); schema deprecation operational lifecycle (deprecated = warning not block; new version creation is explicit action); abstract schema defined (user-defined tag; no enforced vocabulary; namespaced in v2); ConnectorType capability check hard-block resolved (block on missing operation; warn on deprecated schema); ConnectorInstance/ModelBackend health check staleness bound (5-min cache; on-demand at validation); model backend binding added to CopyToAdaptWizard import step; YAML bundle import agent/pipeline name conflict rules; ModelBackend deletion protection (same policy as schema); model_id always resolved from snapshot pin (not live entity); PlanContext null plan_id → restrictive baseline
- v0.14 — hitl_claims entity fully defined (schema, FK constraints, claim expiry job); alpha checklist claim_token corrected (opaque string, not JWT); GET /api/v1/viewmodel/current API contract specified; WebSocket auth token lifecycle (ws-token endpoint, reconnect chain); waiting_for_lock timeout field on Pipeline entity + lock_wait_timeout error code; cancellation via @cancellable_node decorator (implementation specified); per-node execution timeout (asyncio.wait_for + node_timeout error code); spend limit atomicity (org_daily_run_counts SELECT FOR UPDATE); API key role set restricted (operator/runner only; admin keys prohibited); library_primitives content_json field defined (type-discriminated by primitive_type); Basic Auth alpha session lifecycle (stateless; logout clears localStorage); retain_payload encrypted storage (Fernet; webhook_payloads table; access-controlled); /me JWT conflict resolution + team_memberships bound (100 max; not in JWT payload); MODULO_PUBLIC_URL env-var-only (no admin UI override); copy_library_primitive community block (403 for community primitives via MCP; browser-only with wizard gate); notification webhook retry + dead-letter (3 retries; delivery log; endpoint auto-disable); rendered prompt DOM masking (server-authenticated reveal; prompt_always_visible flag); dedup cleanup job spec (webhook_dedup_hashes; 5-min interval; advisory lock); run retention job spec (nightly; batch 500; langgraph.* checkpoint delete); StateGraph cache key bug fixed (pipeline_id + snapshot_id)

---

## 1. Vision

Modulo is a self-hosted platform for implementing and continuously improving an agentic software development lifecycle.

The job it does is two things, both substantial:

**1. Implement.** A DevX engineer or software engineer uses Modulo to build out a full agentic SDLC — from PRD ingestion through ticket creation, code review, QA triage, and release notes — as a composed pipeline of AI agents connected to the team's real tools. This includes two starting points: pulling off-the-shelf modules from the community library (fastest path), or onboarding an existing SDLC by mapping current manual or semi-automated steps into Modulo pipelines and then progressively replacing them with AI agents. Both paths land in the same place: a governed, auditable, agentic SDLC running on the team's own infrastructure.

**2. Improve.** Once the SDLC is running, Modulo provides the layer for making it better: evaluations that measure output quality automatically, HITL gates that keep humans in control of the decisions that matter, run inspection to see exactly what each agent did and why, and prompt/schema versioning to iterate safely. Teams improve their agentic SDLC the same way they improve software — incrementally, with observability, and without breaking what's working.

**What makes it viable for enterprises**: governance, audit trail, and observability are not features — they are the baseline requirement for any regulated or IP-sensitive team to consider running AI agents in their SDLC at all. Team-scoped access control, HITL gates with `human_only` enforcement, immutable audit events, and OpenTelemetry-native observability are table stakes. Without them, enterprise teams cannot even evaluate the platform. With them, the off-the-shelf library and the improvement loop become the competitive advantages that drive adoption.

Modulo is a composition layer, not an opinionated workflow tool. Users define their own schemas, configure their own agent prompts, connect their own tools, and set their own stage-transition rules. The AI handles execution. The user defines what "correct" looks like.

**Discipline scope**: the SDLC is the first and primary vertical. The underlying abstractions — typed schemas, swappable connectors, versioned agents, HITL gates, auditable runs — are discipline-agnostic. Any process that moves typed artifacts through a governed, multi-step pipeline could be served by the same platform. Non-SDLC disciplines are a deliberate future expansion; SDLC is the wedge.

> **Core philosophy**: Modulo handles the boilerplate. You handle the remainder.

---

## 2. Goals

### Alpha Goals
Every primitive type ships with two concrete implementations — proof that the abstraction layer works.

- **Connectors (2)**: `FilesystemConnector` and `GitHubConnector` — both `git-host` type, proving swappability
- **Trigger types (2)**: `manual` and `webhook`
- **Library schemas (2)**: `markdown-document` and `structured-requirements`
- **Library agents (2)**: `document-loader` and `requirements-extractor`
- **Library workflows (3)**: `simplest-workflow`, `prd-to-requirements`, and `requirements-to-file`
- **Model backends (2)**: Anthropic Claude and OpenAI GPT — both registered via ModelBackend registry
- Schema editor where users define and version their own output schema
- Simplified HITL gate (pause → claim → approve or reject)
- Sequential pipeline using both connector types in the demo
- `docker compose up` with pre-loaded demo pipeline walkable in under 5 minutes
- Fully headless-executable via ViewModel REST API
- `pytest-bdd` + Playwright test coverage of all happy paths
- No public release — internal use and feedback only

### Product Goals

**Table stakes (enterprise adoption baseline)**
- Team-scoped RBAC, HITL ownership, and SSO — no enterprise team evaluates without these
- Immutable audit trail on all agent actions and human decisions
- OpenTelemetry-native observability — plugs into existing monitoring without custom work
- Self-hosted — data never leaves the team's infrastructure

**Primary selling points**
- A rich community library of off-the-shelf SDLC modules (agents, schemas, workflows, integrations) that a team can deploy in hours, not weeks
- Evaluation framework that measures agent output quality automatically and drives continuous improvement — teams can see whether their SDLC is getting better
- Prompt/schema versioning and run inspection so improvements are made safely and with full visibility into what changed

**Third major selling point**
- Existing SDLC onboarding: teams can map their current process (even manual steps) into Modulo pipelines and progressively replace steps with AI agents — no big-bang replacement required

**Platform goals**
- Every aspect of every agent — model, prompt, schema, tool — is configurable and versioned
- Self-hosted and SaaS are the same codebase — no divergence
- The abstraction layer is discipline-agnostic; SDLC is the first vertical, not the only one

### Target User (Alpha ICP)
**DevX engineer or software engineer** at a company that wants to move toward an agentic SDLC but doesn't want to build the orchestration infrastructure from scratch. They own the tooling decisions (Jira/Linear, GitHub, Notion, CI), they understand the value of AI-in-the-loop, and they want a platform that gives them control over the prompts, schemas, and governance — not a SaaS black box.

They care about: running it on their infrastructure, adapting it to their conventions, keeping humans in the loop for decisions that matter, and being able to see and audit what the agents did.

---

## 3. Competitive Positioning

The closest alternatives in each layer, and why Modulo makes a different bet:

| Tool | What it does | Why Modulo is different |
|---|---|---|
| **Dify** | Visual AI-app builder (chatbots, RAG, LLM APIs). Self-hosted. | Dify is greenfield AI-app creation — no typed schema system, no concept of mapping an existing process, no SDLC-specific community library. Modulo is process-migration tooling with a governed artifact model. |
| **n8n** | General workflow automation with a large connector ecosystem. | n8n is a peer, not a competitor. The architecture supports treating n8n workflows as callable ConnectorTypes (webhook-in, webhook-out), giving Modulo access to n8n's connector ecosystem without rebuilding it. A native n8n ConnectorType is a v2 roadmap item; teams can wire n8n via the generic webhook connector in the interim. |
| **LangGraph Platform** | Hosted LangGraph runtime with Studio UI. | Modulo builds on LangGraph but wraps it: typed schemas, HITL governance, connector swappability, community library, and a Remote MCP server are not part of LangGraph Platform. LangGraph is the engine; Modulo is the platform. |
| **GitHub Copilot Agents** | Issue→PR automation native to the code host. | GitHub-native and code-focused. Cannot serve teams in regulated environments with data-residency constraints, and does not address pre-code SDLC steps (PRD, tickets, grooming). Modulo is the control plane for the steps GitHub doesn't own. |
| **SpecFlow / Dume.ai / Jira AI** | SaaS point solutions for PRD→tickets. | Zero-setup but zero-configurability and zero self-hosted option. Modulo's community library delivers the same off-the-shelf experience, self-hosted, with schemas the team controls. |

**Modulo's defensible position**: the open, self-hosted, schema-governed, bring-your-own-agent control plane for SDLC pipelines — for teams who need self-hosted data handling, tool-specific conventions, and human governance built into the workflow. The community library of SDLC modules is the primary acquisition vector; the governance layer (HITL, audit trail, team-scoped access) is the retention moat.

---

## 4. Non-Goals (Alpha)

- No public release — alpha is internal only
- No active multi-tenant routing — organisation model in schema, single-org in alpha
- No SSO — basic auth only (v1)
- No run trace / observability UI — stdout OTel; UI in v1
- No cron / polling triggers — manual and webhook only
- No kick-back edges (v1) — parallel branches (fan-out) ARE supported in alpha: a node with multiple normal outgoing edges runs all downstream branches concurrently
- No cost controls UI — v1
- No audit log viewer — v1 (event recording is alpha; viewer is v1)
- No community library registry — local library only; registry protocol in v2
- No license key enforcement — license key infrastructure is a V1 concern; alpha runs with Free Tier defaults only

---

## 5. Core Concepts & Glossary

| Term | Definition |
|---|---|
| **Agent** | An atomic unit of work. Dispatches to an external agent runtime (e.g. Claude Code in an E2B sandbox) with a prompt and context, receives structured output, and evaluates against the output contract. Modulo owns dispatch, auth, audit, cost tracking, and eval — the external agent runtime owns the tool-using execution loop. |
| **Sandbox Agent** | A pipeline node type that creates an isolated E2B sandbox, runs an external AI agent (e.g. Claude Code) with a task prompt, collects structured output, and tears down the sandbox. All observable metrics (wall-clock time, exit code, output validity) are captured natively by Modulo — no follow-up step required. |
| **Output Contract** | The expected structured JSON return value from a sandbox agent execution. Always includes: status, summary, changed_files, wall_clock_time_ms, exit_code. May include additional fields defined by the node's output schema. |
| **Schema** | A versioned, reusable data structure definition. The schema is the "remainder" the user controls. |
| **ModelBackend** | A configured, authenticated binding to a specific AI model provider (Claude, GPT-4o, Bedrock, Ollama, etc.). Per-org. Fernet-encrypted credentials. |
| **ConnectorType** | Abstract capability category (e.g. `git-host`, `issue-tracker`). Defines the operations interface. |
| **ConnectorInstance** | A configured, authenticated binding of a ConnectorType to a specific system (e.g. "our GitHub"). Per-org. Fernet-encrypted credentials. |
| **ConnectorBinding** | An explicit mapping on a pipeline node: `{type: "git-host", instance_id: "<uuid>"}`. Set at pipeline-save time. Resolved by ConnectorHub at run time. No auto-selection. |
| **Pipeline** | An ordered graph of agents with explicit ConnectorBindings on each node. Sequential in alpha; parallel fan-out supported (multiple normal outgoing edges run concurrently); cyclic in v1. Optionally owned by a Team; visibility is `org` or `team`. |
| **PipelineSnapshot** | Immutable copy of a pipeline definition (including all ConnectorBindings and schema version pins) taken at run-start. Runs execute against their snapshot. |
| **Stage** | A named SDLC grouping of pipelines (Product, Development, QA, Release). Top-level kanban columns. Carries `owner_team_id` (nullable) and `visibility` (`org`\|`team`). A team-visibility Stage may only contain pipelines owned by the same team. |
| **Trigger** | A first-class object initiating a pipeline run. Belongs to exactly one pipeline. A pipeline may have multiple triggers. Types: manual, webhook (alpha); cron, polling, agent_signal (v1). |
| **TriggerEvent** | A log record of each trigger activation: raw payload hash, timestamp, schema validation result, run ID if created, error if rejected. |
| **HITL Gate** | A Human-in-the-Loop transition point. Execution pauses; a claimed human must approve or reject. May carry `human_only: true` to block LLM approval via MCP. |
| **HITL Claim** | Atomic DB lock acquired when a user opens a HITL gate for review. Returns a `claim_token`. |
| **claim_token** | **Alpha**: cryptographically random opaque string stored in DB with 15-min TTL. **V1**: short-lived JWT scoped to `run_id + gate_id + client_id`. Required for `approve` or `reject` after a HITL claim. Prevents replay across clients. |
| **Run** | A single execution against a PipelineSnapshot and input. Has unique ID, trace, cost record, result. |
| **RunContext** | A named dict in the LangGraph state, separate from the artifact payload, that carries run-level config and signals across all agents. Seeded at trigger time; writable only by context-setter agents; readable by all. |
| **Variant Group** | A set of runs against the same pipeline and input that differ only in `run_context_overrides`. Used for A/B testing (e.g. Sonnet vs Opus). Compared via eval scores. |
| **FeedbackRecord** | A structured record produced by every HITL rejection. Captures the rejection reason, rejected output, producing agent, and feedback routing. The basis for the self-correction loop and eval suite growth. |
| **Eval** | Automated quality check on agent output. Runs as a post-node step before any HITL gate check. Types: `llm_judge`, `regex`, `json_schema`, `custom_function`. Has a configurable pass threshold and failure behaviour (`warn` \| `block`). |
| **Organisation** | The top-level tenancy unit. All resources belong to an Organisation. Self-hosted = one Organisation. SaaS = many. |
| **PlanContext** | A per-request object that carries feature flags and operational limits. Populated from a cryptographic license key (`MODULO_LICENSE_KEY`) if present; otherwise defaults to the Free Tier. Core checks `plan_context.feature_enabled(...)` — no coupling to billing code. |
| **AuditEvent** | Immutable record of any state-changing action. Written in alpha; viewable in v1. |
| **Library Primitive** | Any schema, workflow, agent, or integration published to the community library with metadata (description, author, download count, rating). |
| **Workflow Bundle** | Portable YAML export of a pipeline referencing ConnectorTypes and abstract schema names. No credentials. Parsed exclusively with `yaml.safe_load()`. |
| **Copy (Fork)** | Local editable copy of a library primitive. No live upstream link after copy. `forked_from` is read-only metadata. |
| **Team** | A named group of users within an Organisation. Teams own pipelines, stages, connectors, and model backends. Members have a team-scoped role that governs their access to that team's resources. A user may belong to multiple teams with different roles in each. |
| **TeamMembership** | The join between a user and a team: `{user_id, team_id, team_role}`. `team_role` may be `operator`, `runner`, or `viewer`. |
| **Pipeline Visibility** | `org` — all org members can see the pipeline at their org role. `team` — only team members and admins can see it; access governed by team role. Default: `org`. |

---

## 6. System Architecture

### 6.1 Layered Architecture

```
┌───────────────────────────────────────────────────────────────┐
│                         Browser UI                            │
│   Standard Theme          │  Agent Theme (?mode=agent, v1)   │
└─────────────┬─────────────┴──────────────────┬───────────────┘
              │                                │
     WebSocket (event bus)           REST API (ViewModel)
              │                                │
┌─────────────▼────────────────────────────────▼──────────────┐
│                     Modulo Core (Python)                      │
│                                                               │
│  Pipeline Engine   │ Schema Registry   │ Trigger Engine      │
│  Agent Runtime     │ Connector Hub     │ Eval Engine         │
│  HITL Manager      │ Model Backend Hub │ Observability(OTel) │
│  Audit Logger      │ Cost Controller   │ Notifier            │
│  Library Service   │ User & Auth       │ Graph Validator     │
│  Workflow Import/  │ Plugin Registry   │                     │
│  Export            │                   │                     │
└───────────────────────────────────────────────────────────────┘
              │                  ▲
    LangGraph (execution)        │ PlanContext (feature flags)
              │                  │
              │         ┌────────┴──────────┐
              │         │  modulo-cloud      │  ← SaaS only
              │         │  (org mgmt,        │
              │         │   billing,         │
              │         │   plan enforcement)│
              │         └───────────────────┘
   ┌──────────┴────────────────┐
   │      Model Backends       │
   │  Claude │ GPT-4o │ etc   │
   └───────────────────────────┘
```

### 6.2 SaaS-First Multi-Tenant Architecture

Modulo is multi-tenant from day one. Self-hosted is a single-tenant deployment of the identical codebase.

#### Organisation Model
Every resource belongs to an **Organisation**. All tables carry `organisation_id`. Postgres Row Level Security (RLS) enforces tenant isolation at the database layer.

#### Organisation Entity
`id`, `name`, `slug` (URL-safe, unique, immutable once set), `status` (`active` | `suspended` | `deleted`), `created_at`, `deleted_at` (nullable), `created_by` (user_id), `settings_json` (default currency, retention_days, deployment_url — see below), `plan_id` (nullable — managed exclusively by modulo-cloud; core does not read this field).

**`MODULO_PUBLIC_URL`**: the deployment's externally reachable base URL (e.g. `https://modulo.example.com`). Set via **environment variable only** — not editable through the admin UI or API. This restriction prevents any admin user (including compromised admin accounts) from redirecting notification links, MCP config snippets, and webhook URLs to an attacker-controlled domain. The value is read from `MODULO_PUBLIC_URL` env var at startup, stored in-memory, and exposed in API responses that generate absolute URLs — MCP server config snippets (§5.4), webhook inbound URLs (§7.5), notification links. If unset, absolute-URL generation falls back to the request's `Host` header with a startup warning. SaaS: injected by modulo-cloud per-org subdomain (not settable by org admins).

#### RLS Connection Pooling — Critical Implementation Detail
RLS context is set using `SET LOCAL app.organisation_id = :org_id` **inside a transaction**. `SET LOCAL` resets automatically on transaction commit or rollback, making it safe with connection pooling. `SET` (without LOCAL) persists for the session lifetime and is a data breach in pooled environments. This is non-negotiable and enforced by:
- A linting rule banning bare `SET app.organisation_id` without `LOCAL`
- A SQLAlchemy connection event hook that resets to `SET LOCAL app.organisation_id = 0` on connection checkout
- An integration test asserting cross-tenant isolation holds across pooled connections

#### LangGraph Checkpoint Isolation
The production checkpointer is `ModuloPostgresSaver`, a subclass of LangGraph's `AsyncPostgresSaver`. It creates its own tables (`checkpoints`, `checkpoint_blobs`, `checkpoint_writes`) with an `organisation_id` column included in each table's primary key, and enforces it on every read/write. Checkpoint JSON is additionally encrypted at rest with Fernet.

**Alpha**: Achieved — org-scoped checkpoint isolation and encryption ship with `ModuloPostgresSaver`. The nightly checkpoint retention job operates on this schema.

#### modulo-cloud Service Layer

> **Status: V3 — explicitly deferred.** modulo-cloud is not required for the commercial model. Feature enforcement is handled entirely within Modulo core via the cryptographic license key. modulo-cloud is only needed if/when a hosted SaaS offering is validated by community traction and enterprise sales.

`modulo-cloud` is a separate service that would wrap Modulo core for SaaS deployment. The coupling boundary is zero by design — it calls core's admin API and injects `PlanContext`; core never imports from it.

| Concern | Owner (when modulo-cloud exists) |
|---|---|
| Organisation lifecycle (create, suspend, delete) | modulo-cloud |
| Plan enforcement for SaaS tenants | modulo-cloud — injects `CloudPlanContext` into requests |
| Subdomain / tenant routing | modulo-cloud |
| Hosted community registry | modulo-cloud |
| Core business logic | Modulo core |

#### PlanContext Interface
`PlanContext` is a per-request object injected at FastAPI request time. Core code uses only this interface — no direct imports from billing or modulo-cloud code.

```python
class PlanContext(Protocol):
    def feature_enabled(self, feature_name: str) -> bool: ...
    def max_concurrent_runs(self) -> int | None: ...  # None = unbounded
    def rate_limit(self, endpoint: str) -> int | None:  # None = no plan limit
        ...
```

**`CommunityTier`** (default when no valid license key is present): returns `False` for all Team-tier features listed below, `None` for all limits (unbounded within the Community tier). This is the default for self-hosted instances without a license key.

**`LicenseKeyTier`** (self-hosted with `MODULO_LICENSE_KEY` set): on startup, the application verifies the signed JSON payload in `MODULO_LICENSE_KEY` using the embedded Ed25519 public key. If the signature is valid and the key has not expired, the enabled features listed in the payload's `features` array are activated; all others remain disabled. If the key is invalid, expired, or malformed, startup logs a warning and falls back to `CommunityTier`.

**License key format**: a base64-encoded signed JSON payload, e.g.:
```json
{"org": "Acme Corp", "expires": "2027-06-20", "features": ["team_rbac", "sso", "audit_viewer", "admin_spend_limits"]}
```
Signed offline by the Modulo private key. Verified on startup using the public key embedded in the repository. No outbound network call required.

**Team feature gate**: the following features require a valid license key with the named flag. Absence of a key, or an expired key, returns `False`:
- `sso` — OIDC, SAML 2.0, JIT provisioning
- `team_rbac` — team entity, team-scoped roles, team pipeline visibility
- `audit_viewer` — AuditEvent bulk export and advanced filtering. *Audit event recording is always active (Community tier). A read-only recent-events view (max 50 events, no export) and chain verification endpoint are also Community.* Only bulk export (CSV/JSONL) and batch-detail are Team-gated.
- `admin_spend_limits` — org and team-level run/spend limit configuration

> **Resolved (2026-06-30)**: a read-only recent-events endpoint (`GET /api/v1/admin/audit`) with chain verification (`GET /api/v1/admin/audit/verify`) stays Community — max 50 events, no export. This gives regulated teams tamper-evidence proof during evaluation without requiring a Team license. Bulk export (CSV/JSONL) and batch-detail endpoints remain Team-gated.

**`CloudPlanContext`** (V3 SaaS — not yet implemented): would be injected by the modulo-cloud gateway middleware per-org, enforcing SaaS plan-tier flags and rate limits. Core never knows the plan tier — it only calls the interface.

Named feature flags used by core (exhaustive list as of v0.24):

*Community tier — always enabled:*
- `parallel_branches` — pipeline nodes with multiple outgoing edges running concurrently
- `eval_system` — §7.16 eval engine
- `webhook_trigger` — inbound webhook triggers
- `cron_trigger` — scheduled triggers (v1)
- `mcp_server` — remote MCP endpoint
- `community_library` — browse and copy community registry primitives
*Team tier — requires valid license key:*
- `sso` — OIDC/SAML authentication
- `team_rbac` — team entity and team-scoped roles (previously `team_management`)
- `audit_viewer` — AuditEvent bulk export and batch detail (read-only recent-events view and chain verification always Community; recording always active)
- `admin_spend_limits` — org/team-level spend and run limit configuration
- `view_modes` — multiple named UI views with admin-defined feature visibility per view and user/team/role assignment (§8.21)

> **Tier rename (v0.24)**: the formerly-named "Free" tier is now **Community**; the formerly-named "Enterprise" tier is now **Team**. The reserved future tiers v1 and v2 remain as-is. This rename reflects that the paid tier is a self-serve feature gate (no SLA, no support contract, no bespoke services) — "Team" more accurately describes what the gated features enable (team collaboration: SSO, RBAC, audit, spend limits) without promising enterprise-grade sales support.

##### Tier System Architecture (future state)

The current tier system has the tier names, feature-to-tier assignments, and plan context resolution all hardcoded in Python source. While functional, this makes it difficult to:

- Rename tiers without touching code
- Add new tiers (a third, fourth, fifth tier) without writing new PlanContext classes
- Change which features belong to which tier without a code deploy
- Support non-linear tier hierarchies (e.g., tiers A and B have partially overlapping but not superset features)

A flexible tier system would decouple tier definitions from code via a **DB-backed tier catalog**:

**`tier_catalog` table**: stores tier definitions — name, display label, rank/ordering, whether it requires a license key, description.

```sql
CREATE TABLE tier_catalog (
    tier_id       TEXT PRIMARY KEY,     -- e.g. 'community', 'team'
    label         TEXT NOT NULL,         -- human-readable: 'Community', 'Team'
    rank          INT NOT NULL,          -- for cumulative activation (≤ rank activates)
    requires_license BOOLEAN DEFAULT FALSE,
    description   TEXT
);
```

**`feature_flag_catalog` table**: replaces the hardcoded `_KNOWN_FLAGS` list — each flag belongs to a tier and can be reassigned via data change.

```sql
CREATE TABLE feature_flag_catalog (
    name          TEXT PRIMARY KEY,
    description   TEXT,
    tier_id       TEXT REFERENCES tier_catalog(tier_id),
    depends_on    TEXT[],
    is_active     BOOLEAN DEFAULT TRUE   -- manual override
);
```

**Benefits:**
- Renaming a tier or reassigning a feature is an INSERT/UPDATE, not a code change
- Adding a new tier is an INSERT into `tier_catalog` plus reassigning features into it
- Non-linear tier structures are possible via explicit feature lists per org (instead of rank-based cumulative)
- A `GET /api/v1/admin/tiers` endpoint can list all known tiers and their display labels — the frontend no longer hardcodes `tierSections`
- PlanContext resolution reads from DB instead of hardcoded Python classes

**Seeded tiers**: only **Community** (rank 0) and **Team** (rank 1) are seeded. Flags for undelivered features stay in `_KNOWN_FLAGS` (the hardcoded fallback) and never activate until a license key grants their tier. Adding a new third tier (whatever it's called) is a simple INSERT into `tier_catalog` + optionally moving flags from `_KNOWN_FLAGS` to `feature_flag_catalog` — no code changes beyond the seed script.

**Migration path (completed in phase-tier-catalog):**
1. Create the `tier_catalog` and `feature_flag_catalog` tables ✓
2. Seed with Community + Team + all current feature flags ✓
3. Refactor `FeatureFlagRegistry` to support DB-backed loading with `_KNOWN_FLAGS` fallback ✓
4. Add `GET /api/v1/admin/tiers` endpoint ✓
5. Update frontend to consume tier labels from API ✓
6. Future: migrate PlanContext resolution to read from `tier_catalog`
7. Future: add data migration for existing orgs' `plan_id` values
8. Future: deprecate `_KNOWN_FLAGS` and `TIER_RANK` after one release cycle

#### API Keys
Per-org, role-scoped API keys for CI/CD pipelines and external agents.

**API key role set**: valid roles for an API key are `operator` and `runner`. `admin` role keys are not permitted — admin actions (team management, settings changes, user management) must be performed by an authenticated human session. `viewer` role is not yet defined as an org role and is also not valid. A `runner`-scoped key can trigger runs and call read endpoints only; it cannot approve HITL gates, access connector settings, or modify pipelines. An `operator`-scoped key can trigger runs, approve HITL gates (subject to `human_only` and `required_team_id` constraints on the gate), and access all read endpoints. The `role` field is enforced at the ViewModel command layer — same enforcement path as user JWT roles.

**Role-cap on minting** (ADR 017 DECISION 4): API keys are created at the caller's *live* org role, never above it. A caller cannot mint a key with a role higher than their current live membership role — a `runner` cannot mint an `operator` key, and only members at `runner` or above can create keys at all (`viewer` cannot mint any key). The minting cap uses the caller's live org membership role re-read at request time, so a demoted user immediately loses the ability to mint higher-role keys.

**Live re-validation**: every MCP call re-checks the key owner's live org role. If the key owner has been demoted, the key's effective scope degrades on the next call to the live role (never persisted — the stored key role is unchanged, so a later promotion restores full scope automatically). If the key owner is removed or deactivated from the org, the key stops working (401) immediately.

**Format**: `mk_<lookup_prefix>_<random_secret>` where `lookup_prefix` is the first 8 chars of the key ID (enables fast DB index lookup without full scan).

**Storage**: SHA-256 hash of the full key. API keys are high-entropy random strings — bcrypt's brute-force protection buys nothing and adds 50–200ms per validation. SHA-256 is fast and sufficient.

**Lifecycle**: shown in full once at creation; not recoverable. Rotation creates a new key; old key is immediately invalidated. All API key operations written to AuditEvent.

#### Usage Events
Usage events (run started, tokens consumed, connector operations, HITL actions) are emitted to an internal event bus on every occurrence. Self-hosted: events route to the OTel exporter. Per-org subscription ACLs on the event bus — an org cannot subscribe to another org's stream.

Token counting exists strictly for **internal cost controls** (org/team-level budget enforcement) — not for Modulo's own revenue billing. Modulo's commercial model is a flat annual license fee; no telemetry aggregation is required for billing. If/when modulo-cloud SaaS is built (V3), the event bus design supports a billing consumer without code-path changes in core.

#### Self-Hosted → SaaS Migration
Teams migrating from self-hosted to SaaS use a migration CLI:

```bash
modulo export-org --org-id default > org_export.json
modulo import-org --target-org-id <saas-org-id> < org_export.json
```

Export includes: pipelines, agents, schemas, connector instance configs (credentials excluded — user re-enters), library entries, audit events. Import is idempotent. Documented before v3 SaaS launch.

#### Organisation Deletion Policy
- **Soft delete**: on deletion request, org is deactivated (no new runs, no logins). Data retained for 30 days.
- **Export window**: admin can export all org data during the 30-day window.
- **Hard delete**: after 30 days, all org data permanently deleted (pipelines, agents, schemas, runs, audit events, checkpoints).
- **Regulated orgs**: configurable retention period overriding the 30-day default (e.g. 7-year audit log retention).

#### Data Residency
V3 SaaS concern. `modulo-cloud` routing layer designed for multi-region from v3 day one (region encoded in org metadata, separate Postgres clusters per region). Decision deferred but architecture documented now.

### 6.3 MVVM + Transport Separation

The ViewModel is consumed by three first-class view surfaces. All three share the identical ViewModel — no divergence in business logic or state:

| View | Transport | Consumer | Auth |
|---|---|---|---|
| **Browser UI — standard theme** | REST + WebSocket | Human operators | JWT / Basic Auth |
| **Browser UI — agent theme** | REST + WebSocket | Automated browser drivers, LLM web agents | JWT / Basic Auth |
| **Remote MCP server** | HTTP + SSE (MCP protocol) | LLM clients (Claude Desktop, Cursor, custom agents) | API key bearer token (alpha); OAuth 2.0 (v1) |

The browser UI views are the **same Vue application and the same component tree**. Theme switching is CSS-only — no duplicate components, no separate routes, no alternative HTML structures.

**ViewModel**: serialisable state and named commands — REST contract. Every user action is a named command.
**Event bus**: real-time push via WebSocket consuming LangGraph `astream_events()` — separate contract. MCP clients subscribe to run events via SSE (MCP's native streaming transport).
These must not be conflated. The ViewModel does not manage subscriptions.

**WebSocket fan-out**: `astream_events()` is a single async generator per run. Multiple browser tabs or MCP clients watching the same run must not each trigger a separate `astream_events()` call (which would double-stream events and waste resources). Architecture: one `astream_events()` consumer per active run, managed by a per-run **event broker** (in-process pub/sub). WebSocket and SSE connections subscribe to the broker for their run. The broker fans events to all subscribers. In multi-process deployments (Gunicorn/uvicorn workers), the broker uses Redis pub/sub — Redis is required for production multi-worker deployments (alpha: single-process, in-memory broker acceptable with startup warning).

**WebSocket reconnection and event replay**: WebSocket connections drop under normal network conditions (mobile, sleep/wake, NAT timeout). The client must handle reconnection without losing run state.

Protocol:
1. On WebSocket disconnect, the client immediately calls `GET /api/v1/runs/{id}` to rebuild current run state from REST (current status, per-node status, any error). This snapshot is authoritative.
2. The client re-connects to the WebSocket with a `?since_event_seq=N` query parameter (the sequence number of the last event received before disconnect).
3. The server event broker buffers the last **100 events per run** in a ring buffer (in-memory for alpha; Redis list for multi-worker). On reconnect, the broker replays any buffered events with sequence number > N before resuming live stream.
4. If the run reached a terminal state before reconnect, the WebSocket connection attempt returns 200 with `{"status": "terminal", "run": {...}}` rather than upgrading — the client falls back to REST-only display.

**Pinia store hydration**: on login/page load, the Vue app calls `GET /api/v1/me` which returns `{user, org, team_memberships, org_role}`. This response hydrates the `authStore` (user identity) and `orgStore` (org context including `org_id`, `org_name`, `settings`). All subsequent stores (pipeline store, run store, library store) are keyed by `org_id` and populated lazily. The auth token carries `org_id` in its payload; the `orgStore` cross-validates against the `/me` response on each page load to catch token/API drift.

**`/me` JWT conflict resolution**: if the JWT's `org_id` or `org_role` disagrees with the `/me` response (e.g. the user was demoted while their token was still valid), the `/me` response is authoritative. The client clears the stale JWT, requests a new access token using the refresh token, and re-hydrates. If the refresh token is also stale (expired or revoked), the user is redirected to re-authenticate. The conflict is logged as an `auth_event` with `type: token_claim_drift`.

**`team_memberships` response bound**: `GET /api/v1/me` returns at most 100 team memberships. Users in more than 100 teams receive the first 100 by join date, plus `team_memberships_truncated: true` in the response. This is an unlikely scenario in alpha (single org, team management not active), but the bound prevents unbounded JWT payloads as the v1 team system scales. The JWT payload does not embed team memberships — only `org_id` and `org_role` — to avoid JWT inflation.

### Theme System

The UI ships a first-class theme system from day one. Themes are pure CSS — applied by setting `data-theme` on the root element. The component tree, DOM structure, `data-testid` attributes, and ARIA labels are identical across all themes.

| Theme | Description | Accessibility |
|---|---|---|
| `standard` | Full design system — spacing, colour, typography, animations, decorative chrome | WCAG 2.1 AA (contrast ratios, focus indicators, motion preferences) |
| `agent` | Stripped to functionality — zero decorative padding, no animations, no colour, monospace, maximum information density. A browser agent sees only actionable elements. | Exempt from colour contrast ratios. Retains keyboard nav, focus rings, ARIA labels, screen reader semantics. |

Theme is controlled by:
- `?theme=<name>` query parameter (overrides stored preference; used by automated drivers)
- User preference persisted in `localStorage`
- Admin-configurable deployment default (e.g. a headless CI deployment can default to `agent`)

**CSS custom property mechanism**: themes are implemented as CSS custom property layers. Each theme defines its values under a `[data-theme="<name>"]` attribute selector. All Tailwind components reference only semantic custom properties (e.g. `--color-surface`, `--color-text-primary`, `--spacing-base`) — never raw Tailwind palette values directly. This ensures any `[data-theme]` layer can override the full design without touching component templates.

**Focus rings**: defined via dedicated custom properties `--focus-ring-width` and `--focus-ring-color`. The `standard` theme uses brand-appropriate values; the `agent` theme uses high-visibility values (thick, high-contrast ring) since browser agents depend on focus state for keyboard navigation. Focus rings are **never** suppressed — `outline: none` is prohibited without an explicit `--focus-ring` replacement. A Playwright accessibility test asserts that all interactive elements have a visible focus indicator in both themes.

Future themes (`dark`, `high-contrast`, `compact`) are additive CSS layers — no component changes required.

### Tier Badge and License Page

**Sidebar tier badge**: a small pill badge rendered in the sidebar nav footer, next to the org name, on every authenticated page. Uses the shadcn-vue `Badge` primitive.

| State | Display | Behaviour |
|---|---|---|
| No license key / Community tier | `Community` badge (neutral colour) | Links to `/settings/license` |
| Valid Team key | `Team` badge (accent colour) + expiry date tooltip | Links to `/settings/license` |
| Expired Team key | `License expired` badge (destructive colour) | Links to `/settings/license`; startup also logs a warning |

The badge reads from `planStore` (see below) — it does not make its own API call.

**Team-gated features in the UI**: features gated behind the Team tier are **never hidden**. They render with:
- A lock icon (`🔒` via Radix Vue `LockClosedIcon`) adjacent to the feature label
- A `Team` badge (shadcn-vue `Badge` variant `outline`) inline
- On click/focus, a tooltip: "Requires a Team license — see `/settings/license`"
- The underlying control is `disabled` and `aria-disabled="true"`

This pattern creates a passive upgrade funnel without jarring empty states.

**`planStore` (Pinia)**: a lightweight store hydrated from `GET /api/v1/license` on page load alongside `/me`. Exposes:

```ts
interface PlanState {
  tier: 'community' | 'team'
  features: string[]          // active feature flags from license payload
  expiresAt: string | null    // ISO date string; null for Community tier
  orgName: string | null      // org name from license payload
}
```

`planStore.featureEnabled(flag: string)` is the client-side gate used by all UI components. It mirrors `PlanContext.feature_enabled()` on the backend — both must agree or a component will be incorrectly enabled/disabled.

**`GET /api/v1/license`** endpoint (admin only): returns current tier, active features, expiry, and org name from the validated license payload. Returns `{"tier": "community", "features": [], "expires_at": null, "org_name": null}` when no valid key is present. Never returns the raw signed key material.

**`/settings/license` page** (admin only):

| Section | Content |
|---|---|
| Current tier | `Community` or `Team` card with expiry date and licensed org name |
| Active features | Checklist of all defined feature flags; each shows enabled (✓) or disabled (✗ with "requires Team") |
| License key management | Textarea to paste a new `MODULO_LICENSE_KEY` value (writes to server env / config file); "Verify key" dry-run before applying; confirmation dialog on apply (requires server restart warning) |
| Upgrade CTA | "Get a Team License" link (external); only shown on Community tier |

The license page is also reachable from any Team-gated feature's lock icon tooltip.

**Playwright loading state convention**: async data-loading components set `data-loading="true"` on their root element while fetching and `data-loading="false"` on completion. Playwright tests wait with `waitForSelector('[data-loading="false"]')` rather than arbitrary `page.waitForTimeout()` calls. This applies to all Playwright feature tests — no hard-coded delays.

`data-testid`, ARIA labels, and semantic HTML are authored once and work across all themes. The agent theme is not a separate test surface — it is the same surface with decoration removed.

**Sensitive data and the DOM**: Sensitive field values (credentials, raw webhook secrets, API key secrets) must never be present in the DOM in plaintext unless the user has completed a server-authenticated reveal action. CSS `visibility: hidden` or `opacity: 0` is not a security control — it is visual only and exposes data to DOM scrapers and browser agents operating under the agent theme. Reveal is an explicit API call; the value is injected into the DOM only after the server confirms the authenticated request. This applies equally to both themes.

### 6.4 Remote MCP Server

Modulo exposes a Remote MCP server at `/mcp` on the same FastAPI instance. It implements the [Model Context Protocol](https://modelcontextprotocol.io) over HTTP + SSE, making Modulo directly operable by any MCP-capable LLM client without a browser.

This replaces the previous "LLM driveability" stretch goal with a standards-based, protocol-defined capability. The `x-modulo` REST extension block remains for API clients; MCP is the primary LLM interface.

#### MCP Resources (read-only, browseable by LLM)

| Resource URI | Description |
|---|---|
| `modulo://pipelines` | List of all pipelines in the org |
| `modulo://pipelines/{id}` | Pipeline definition, node graph, current status |
| `modulo://pipelines/{id}/runs` | Run history for a pipeline |
| `modulo://runs/{id}` | Run detail, current state, per-node status |
| `modulo://runs/{id}/hitl/{gate_id}` | HITL gate context (preceding output, expected next input) |
| `modulo://library` | Browse community library primitives |
| `modulo://library/{type}/{slug}` | Library primitive detail (schema, agent, workflow, integration) |
| `modulo://schemas` | Available schemas in the org |
| `modulo://schemas/{id}@{version}` | Schema definition |
| `modulo://connectors` | Registered connector instances and their health status |
| `modulo://model-backends` | Registered model backends and their health status |

#### MCP Tools (executable actions)

`trigger_pipeline` fires a run and returns immediately with the `run_id`. It does not block on run completion. The LLM client polls `get_run_status` to track progress. MCP tools are request/response only — SSE streaming of run events is a separate channel (see below).

| Tool | Maps to ViewModel command | Notes |
|---|---|---|
| `trigger_pipeline` | `POST /api/v1/runs` | Fire-and-forget; returns `run_id` immediately |
| `get_run_status` | `GET /api/v1/runs/{id}` | Summary by default; `detail: true` for per-node breakdown |
| `get_run_output` | `GET /api/v1/runs/{id}/nodes/{node_id}/output` | Retrieve output of a specific completed node |
| `cancel_run` | `POST /api/v1/runs/{id}/cancel` | Cancel a running or queued run |
| `review_hitl` | `POST /runs/{id}/hitl/{gate_id}/review` | Unified HITL action: `action` = `claim` \| `approve` \| `reject` \| `deliver_manual`; `approve`/`reject` require `claim_token`; `reject` requires `reason`; marked `destructive: true` |
| `list_pipelines` | `GET /api/v1/pipelines` | Summary by default; paginated |
| `get_pipeline_graph` | `GET /api/v1/pipelines/{pipeline_id}/graph` | Read-only. Returns the full node/edge graph of a pipeline by ID. |
| `list_pending_hitl` | `GET /api/v1/runs?status=awaiting_human` | All runs awaiting human action |
| `search_library` | `GET /api/v1/library` | Search the library with type filter, text search, cursor pagination. |
| `copy_library_primitive` | `POST /api/v1/library/{slug}/copy` | Community (unverified) primitives: returns 403 via MCP — MCP clients cannot copy community primitives at all; only verified primitives may be copied via MCP. Browser-only: requires explicit user acknowledgement in the CopyToAdaptWizard (not a `confirm: true` API parameter — a UI gate). This prevents an autonomous LLM client from self-supplying `confirm: true` to bypass the warning. |
| `list_trigger_events` | `GET /api/v1/triggers/{id}/events` | View trigger event log |
| `create_pipeline` | `POST /api/v1/pipelines` | Operator role required. Creates a new pipeline definition with name, description, visibility, and default config. |
| `delete_pipeline` | `DELETE /api/v1/pipelines/{pipeline_id}` | Operator role required. Deletes a pipeline by ID. |
| `update_pipeline_graph` | `PUT /api/v1/pipelines/{id}/graph` | Operator role required. Replaces the node/edge graph of a pipeline. |
| `bind_connector_to_node` | MCP-only operation | Operator role required. Binds a connector instance to a pipeline node, updating the node's `connector_binding` in the pipeline graph. The connector must already exist in the organisation. |
| `create_model_backend` | `POST /api/v1/model-backends` | Operator role required. Registers a new LLM provider. API key is provided via a one-time browser setup URL returned by the tool — the key never transits the LLM context. |
| `create_agent` | `POST /api/v1/agents` | Operator role required. Creates a new agent with name, prompt_template, model_backend_id, and optional input/output schemas. |
| `create_connector` | `POST /api/v1/connectors` | Operator role required. Creates a new connector instance (provider configuration); credentials encrypted at rest. |
| `delete_connector` | `DELETE /api/v1/connectors/{connector_id}` | Operator role required. Deletes a connector instance by ID. |
| `list_runs` | `GET /api/v1/runs` | List runs with cursor-based pagination, optional pipeline_id/status filters. |
| `list_triggers` | `GET /api/v1/triggers` | List trigger configurations, optional pipeline_id filter. |
| `get_trigger` | `GET /api/v1/triggers/{id}` | Read a single trigger by ID (runner role required). |
| `update_trigger` | `PUT /api/v1/triggers/{id}` | Update a trigger's configuration — active, max_concurrent_runs, cron_expression/cron_timezone (cron triggers only), daily_spend_limit (clear via `clear_daily_spend_limit`), config_json. Operator role required. |
| `delete_trigger` | `DELETE /api/v1/triggers/{id}` | Soft-delete a trigger by ID. Operator role required. |
| `create_trigger` | `POST /api/v1/pipelines/{pipeline_id}/triggers` | Operator role required. Creates a new trigger for a pipeline (manual/webhook/cron/polling). |
| `create_secret` | MCP-only operation | Operator role required. Creates or updates a secret in the org vault; encrypted at rest. |
| `list_secrets` | MCP-only operation | Operator role required. Lists secret keys in the org vault — never exposes secret values. |
| `delete_secret` | MCP-only operation | Operator role required. Deletes a secret from the org vault by key. |
| `get_run_evals` | `GET /api/v1/runs/{id}/evals` | Get eval results for a completed run. |
| `list_eval_definitions` | `GET /api/v1/eval-definitions` | List eval configurations, optional pipeline_id filter. |
| `list_schemas` | `GET /api/v1/schemas` | Read-only. Lists registered schemas with cursor-based pagination. |
| `infer_schema` | `POST /api/v1/schemas/infer` | Operator role required. AI-assisted schema inference from a sample JSON payload. |
| `validate_payload` | `POST /api/v1/schemas/validate` | Read-only. Validates a payload against a registered schema by schema_id. |
| `get_integration_status` | `GET /api/v1/integrations/status` | Health status of all connectors, model backends, and triggers. |
| `get_org_config` | `GET /api/v1/admin/config` | Org-level configuration, filterable by section (remy, plan, rate_limits). |
| `get_available_features` | `GET /api/v1/features` | List product features enabled on the current plan tier. |
| `list_housekeeping` | `GET /api/v1/admin/housekeeping` | List housekeeping cleanup candidates for the org (runner role required). |
| `perform_housekeeping` | `POST /api/v1/admin/housekeeping/cleanup` | Operator role required. Deletes housekeeping cleanup candidates, grouped by entity type with per-group savepoints. |

**`review_hitl` detail**: the claim step returns a `claim_token` (alpha: cryptographically random opaque string with 15-min TTL; v1: short-lived JWT scoped to `run_id + gate_id + client_id`). Subsequent `approve` or `reject` calls must include this token. The `deliver_manual` action allows supplying an output dict directly — useful for MCP clients that want to provide human-authored content through a gate. The `reject` action accepts an optional `reason` string for audit trail purposes. This prevents replay across clients and enforces that the reviewing client inspected the gate context before acting.

**HITL `human_only` flag**: each HITL gate definition carries a `human_only: boolean` field (default: `false`). When `true`, calling `review_hitl` with `action: approve` via MCP returns 403. This allows pipeline authors to explicitly block LLM autonomous approval for gates that require human judgement. The flag is visible in the gate context resource at `modulo://runs/{id}/hitl/{gate_id}` so the LLM client understands why the action is rejected.

**MCP response size**: all list tools and `get_run_status` return summaries by default. Clients pass `detail: true` for full output. List resources support cursor-based pagination via `next_cursor` in the response. This prevents large run histories or library catalogues from producing responses that overflow LLM context windows.

**MCP resource content annotation**: resources that contain agent-generated content (e.g. `modulo://runs/{id}/nodes/{node_id}/output`) are annotated with `content_type: agent_output` in the resource description. LLM clients should treat this content as untrusted and potentially containing prompt injection attempts. Modulo documents this in the MCP resource manifest; enforcement is the client's responsibility.

**MCP write scope boundary**: write operations beyond run triggering and HITL review are restricted to clients authenticated with the `operator` role. MCP clients with `runner`-role credentials can trigger runs and review HITL gates, but cannot create or modify pipelines, manage model backends, or perform other administrative operations. The `operator` role is assigned at API key creation time (see §5.2). This role-based boundary replaces the earlier browser-only restriction — the `operator` scope gate (`check_tool_scope`) is enforced at the tool handler level for every write operation.

#### MCP Authentication

**Alpha**: API key bearer token only. Clients include `Authorization: Bearer mk_<key>` on all MCP requests. This is the same API key format as §5.2 and requires no OAuth server implementation. Non-interactive clients (CI/CD, custom agents) will use this indefinitely.

**V1**: Full OAuth 2.0 server using `authlib` (not hand-rolled). Mandates:
- PKCE (`code_challenge_method=S256`) — required for all clients
- Exact `redirect_uri` validation (no prefix matching)
- `state` parameter required and verified
- No tokens in query strings (POST body or header only)
- Scopes: `pipelines:read`, `pipelines:run`, `hitl:approve`, `library:read`, `library:write`, `hitl:approve:pipeline:{id}` (per-pipeline scope for targeted HITL delegation)

Dual-layer scope enforcement applies: scopes are validated at the token middleware layer AND re-validated at the ViewModel command layer (ViewModel rejects commands that exceed the token's scope even if middleware passed the request). This prevents scope bypass via unexpected routing.

MCP SSE event streams validate org context on every event — not just at connection establishment. An event from a different org is never sent to a connected client, even if connection-level auth passed.

#### MCP Onboarding

`/settings/mcp` in the Modulo UI (admin only). Provides:
- One-click API key generation for MCP use
- Copy-paste config snippets for Claude Desktop, Cursor, and a generic HTTP client
- Registered MCP client list (name, last seen, scopes)
- Revoke client button

**MCP server URL in config snippets**: snippets must include the full base URL of the Modulo deployment (e.g. `https://modulo.example.com/mcp`). This URL is derived from `MODULO_PUBLIC_URL` (§5.1 Organisation Entity). If `MODULO_PUBLIC_URL` is unset, the onboarding page displays a warning and falls back to `http://localhost:8000/mcp` — correct for local dev, incorrect for network deployments. The page prompts the admin to set `MODULO_PUBLIC_URL` before sharing config snippets.

This page ships in alpha. Without it, connecting an MCP client requires manual config and has no discoverability path.

#### MCP Capabilities and Modulo
The MCP server is a thin adapter over the ViewModel API. It adds no business logic. A Modulo pipeline that surfaces a HITL gate automatically appears as a pending tool call in a connected LLM client — the LLM can inspect the gate context via `modulo://runs/{id}/hitl/{gate_id}` and call `review_hitl` without human intervention (unless `human_only: true`), enabling governed pipeline execution with configurable autonomy when desired.

This is the meta-layer: Modulo manages AI agents; an AI agent can manage Modulo.

### 6.5 LangGraph

Each PipelineSnapshot compiles to a LangGraph `StateGraph` at run-start. Graph is validated before execution (§7.4). LangGraph provides `AsyncPostgresSaver`/`AsyncSqliteSaver` checkpointing, `interrupt()` for HITL, `astream_events()` for real-time progress.

**StateGraph state type**: Modulo uses `dict[str, Any]` as the LangGraph state type — not dynamically generated TypedDicts. Dynamic TypedDicts interact badly with LangGraph's reducers, `Annotated` fields, and Pydantic validation. Schema validation runs as Modulo-layer pre/post node steps outside LangGraph's type system. LangGraph is responsible for graph execution; Modulo is responsible for schema enforcement.

**Compiled graph caching**: compiled `StateGraph` objects are cached in-memory keyed by `(pipeline_id, snapshot_id)` — both fields are required. `snapshot_id` alone is not unique across pipelines (two different pipelines each have a snapshot 1, 2, 3, …). Cache uses LRU eviction. Re-compilation only occurs on first execution of a new `(pipeline_id, snapshot_id)` pair. This avoids per-run recompilation overhead at scale.

**Startup sequence**: (1) Alembic `upgrade head` against `public.*` — idempotent, runs on every startup; (2) `AsyncPostgresSaver.setup()` against `langgraph.*` — also idempotent; LangGraph handles "table already exists." In multi-worker deployments, a Postgres advisory lock prevents concurrent migration runs.

**Async driver mandate**: all database access in the async FastAPI/LangGraph path uses async drivers — `asyncpg` for Postgres, `aiosqlite` for SQLite. `psycopg2` and `sqlite3` (sync) are not permitted in the async request path. Running sync DB calls inside an async event loop blocks it and degrades throughput proportionally to checkpoint frequency. This is a hard implementation rule, not a preference.

**Version policy**: pinned exact version in `pyproject.toml`. Upgrades are migration events: test checkpoint compatibility, write runbook, deploy to staging first.

**Max pipeline nesting depth**: 3 levels. Enforced by validator and UI.

### 6.6 Observability — OpenTelemetry First

All traces emitted as OTel data. LangSmith is one optional exporter. Default: stdout.

**LangGraph→OTel bridge**: LangGraph does not natively emit OTel spans. A custom callback handler maps LangGraph node events to OTel spans (`on_chain_start/end` → pipeline span, `on_llm_start/end` → LLM call span with parent, `on_tool_start/end` → connector operation span). Parent span propagation through LangGraph's async context is non-trivial — LangGraph uses its own callback system, not OTel context propagation natively.

**This bridge is a standalone blocking dependency**: every OTel span in this section, every connector operation span, and every trigger event span requires the bridge to function. It must be built and merged before any OTel span assertions appear in tests. It is a dedicated implementation task — not a by-the-way. Assign it as such.

OTel env var configuration follows the OTel specification:
- `OTEL_EXPORTER_OTLP_ENDPOINT` — Jaeger, Grafana Tempo
- `LANGSMITH_API_KEY` + `LANGSMITH_PROJECT` — enables LangSmith exporter
- Default: stdout JSON

Every LLM call, every connector operation, and every trigger event emits a span.

### 6.20 Agent Dispatch Model

Modulo dispatches work to external agent runtimes in sandboxes, then evaluates the output. Modulo is the SDLC orchestration layer — it owns dispatch, auth, audit, cost tracking, eval gates, and HITL — not the agent loop itself.

#### Sandbox templates vs agent runtimes

Each E2B sandbox template (e.g. `claude-code-v1`, `opencode-v1`, `generic-python`) defines the agent runtime environment. The template pre-installs a specific agent CLI (Claude Code, opencode, etc.) plus supporting tools (git, GitHub/Jira CLIs). Modulo references templates by ID — it does not manage the runtime environment beyond provisioning and teardown.

#### Output contract pattern

Every sandbox agent execution returns structured JSON from a well-known path (`/home/user/output.json`):

```json
{
  "status": "completed" | "failed",
  "summary": "Description of what was done",
  "changed_files": ["path/to/file1.py"],
  "pr_url": "https://github.com/org/repo/pull/123",
  "exit_code": 0,
  "wall_clock_time_ms": 45000
}
```

The output contract is validated at the pipeline level, not inside the sandbox. All observable metrics are captured natively by Modulo — no follow-up step required.

#### Observability

Wall-clock time and exit code are captured on every dispatch natively by the `sandbox_agent` node type. This requires no additional instrumentation — the node records `time.monotonic()` before sandbox creation and after output collection, always returning `wall_clock_time_ms` even on failure.

#### Post-hoc eval as separate concern

The agent's output is evaluated by a separate Modulo pipeline (code review, test coverage check, security scan). This keeps evaluation separate from execution and allows different evaluators to be composed independently.

#### env_vars secret references

`sandbox_agent` nodes may reference org secrets in their `env_vars` using `{{ secrets.KEY }}` syntax. These references are resolved at node run time — on every execution, not at graph compile time — so secret rotation takes effect on the next run and secrets never enter the compiled graph cache or checkpoint state. Resolution order: the org vault (per-org encrypted `secrets` table) is consulted first; if the key is not in the vault, the value falls back to the worker process environment. A reference that resolves to neither becomes an empty string (with a warning) so a missing secret never crashes a run.

---

## 7. Security

### 7.1 Template Rendering — Sandboxed Jinja2
All prompt template rendering uses `jinja2.sandbox.SandboxedEnvironment`. Enforced by pre-commit lint rule. Non-negotiable — standard Jinja2 is an RCE vector when users define templates.

### 7.2 Prompt Injection
Agent prompts interpolate external content (git file contents, Jira bodies, webhook payloads). Fully preventing injection is not possible, but mitigations are required:
- Documented as a known risk in the agent configuration UI and docs
- Input length limits configurable per agent
- Output validation before any connector write operation is recommended practice
- V1: configurable content filtering per agent

**Schema validation is not a sanitisation control**: pre-run input validation (§7.4) checks types, field presence, and format. It does not sanitise string content. A `string` field containing a crafted prompt injection payload passes schema validation and is then interpolated into the rendered Jinja2 prompt. The SandboxedEnvironment prevents template-author RCE; it does not prevent injected *values* from manipulating LLM behaviour. Input length limits are the primary mitigation for this vector.

**LLM-judge eval injection (§7.16)**: if an eval passes agent output to a model (e.g. an LLM-as-judge eval), the eval model call receives the full agent output as input. An injection payload in the agent output could instruct the judge to return a passing score, bypassing the quality gate. LLM-judge evals must treat agent output as untrusted: the eval prompt must use structural separators (e.g. XML-style delimiters around the content-under-review) and explicitly instruct the judge to evaluate only, not follow embedded instructions.

### 7.3 Workflow Bundle Import — Safe Parsing
All YAML parsed with `yaml.safe_load()` exclusively. Enforced by pre-commit lint rule. Imported bundles contain agent prompt templates — an untrusted bundle from a public registry is a potential malicious payload delivery mechanism.

### 7.4 Community Registry — Ed25519 Signing
All primitives published to the community registry are signed with an Ed25519 key pair operated by Modulo. The corresponding public key is shipped with the client (pinned trust anchor). Client-side verification occurs before any bundle content is acted on. A compromised registry or MITM cannot serve a tampered primitive that passes verification. Analogous to apt, Homebrew, and PyPI package signing.

Unverified primitives (e.g. local imports) display a warning and require explicit user confirmation before first run.

**Key rotation**: if the Modulo signing key is compromised, a new key pair is generated and a new Modulo release ships with the updated pinned public key. Clients on old versions continue trusting the compromised key until updated — this is the standard trust anchor update problem. Incident response: (1) revoke compromised key publicly; (2) ship new release with updated pinned key within 24 hours; (3) publish security advisory requiring update within 7 days. V2: versioned key manifest at a well-known registry URL (TOFU-style, pinned on first fetch) to support key rotation without a full release cycle.

### 7.5 Connector Access Control
Each ConnectorInstance has:
- `owner_id`: creating user
- `owner_team_id`: nullable FK — the team that owns this connector (consistent with §8.3 team model)
- `visibility`: `org` (accessible to all org members) | `team` (accessible only to owner team members and admins). Default: `org`. **Note**: `private` (user-private) is not a supported visibility mode — if a user needs a private connector, they create a single-person team. This is an intentional simplification; user-private connectors create sharing UX complexity without clear benefit.
- `allowed_operations`: declared set (e.g. `read-only`, `read-write`)

Graph validation checks pipeline operations against `allowed_operations` before run start. Over-privileged connector use is caught at validation, not at runtime.

### 7.6 HITL Claim — Atomic Lock
```sql
UPDATE hitl_claims
SET claimed_by = :user_id, claimed_at = NOW()
WHERE run_id = :run_id AND claimed_by IS NULL
RETURNING id;
```
Zero rows returned = claim already held. No check-then-act pattern.

**`hitl_claims` entity**: `id` (UUID PK), `run_id` (FK → runs), `gate_id` (string — matches edge `hitl_gate_config.id`), `pipeline_id` (FK → pipelines, for fast index), `claimed_by` (nullable FK → users), `claimed_at` (nullable timestamptz), `claim_token` (text — alpha: random opaque string; v1: JWT), `expires_at` (timestamptz — `claimed_at + claim_expiry_minutes`), `created_at`. One row per gate per run. Row is created when the HITL gate is reached; `claimed_by` is null until claimed. Claim expiry deletes the `claim_token` and resets `claimed_by` to NULL (row is not deleted — it anchors the gate state). Unique constraint on `(run_id, gate_id)`.

**Claim expiry background job**: runs on a configurable interval (default: every 60 seconds). Query: `UPDATE hitl_claims SET claimed_by = NULL, claim_token = NULL, claimed_at = NULL WHERE expires_at < NOW() AND claimed_by IS NOT NULL RETURNING run_id, gate_id`. For each expired claim: dispatch a `claim_expired` notification (§7.11) and reset the gate to claimable. Job is idempotent. In multi-worker deployments, Postgres advisory lock (`pg_try_advisory_lock('hitl_claim_expiry_job')`) ensures only one worker runs the job per interval. If the advisory lock cannot be acquired, this worker skips the interval — next worker will run it.

### 7.7 Inbound Webhook Security
Each webhook trigger has a system-generated secret. Inbound requests must include:
- `X-Modulo-Timestamp: <unix_seconds>` — current time at send
- `X-Modulo-Webhook-Signature: sha256=<hmac>` — HMAC-SHA256 of `timestamp + "." + raw_body` using the trigger secret

The platform validates: (1) HMAC matches (constant-time comparison); (2) timestamp is within ±300 seconds of server time (replay window). Requests outside the window are rejected 403 even if HMAC is valid. Both validations are required — HMAC alone does not prevent replay after the deduplication window closes. Requests failing validation are logged as `TriggerEvent` with `validation_failed` or `timestamp_expired` status.

### 7.8 Outbound Webhook Signing
All outbound notifications include `X-Modulo-Signature: sha256=<hmac>` signed with a per-endpoint secret (HMAC-SHA256).

### 7.9 TLS
Modulo does not terminate TLS. The repo ships a reference Caddy configuration for TLS termination. Deployment guide states prominently: TLS is required before any network exposure. Basic auth over plain HTTP is base64-encoded, not encrypted.

### 7.10 JWT Security
- Access tokens: 15-minute expiry
- Refresh tokens: 7-day expiry, rotated on use
- **Token family invalidation**: a revoked token presented triggers full session revocation for that user
- **WebSocket auth**: short-lived connection token issued by `POST /api/v1/ws-token` (JWT auth required). Token is a cryptographically random opaque string, 60-second TTL, single-use. Passed in the `Authorization` header of the WebSocket upgrade request (not query string — query strings are logged by reverse proxies). The server validates the token before completing the upgrade. If validation fails, the upgrade is rejected 401. Token TTL is intentionally short because the connection is established immediately after issuance.
- **WebSocket long-running token refresh**: for runs with HITL gates that may pause for days or weeks, the underlying WebSocket connection will be replaced by normal reconnection events (NAT timeout, client restart). The client re-authenticates by requesting a new `ws-token` using its current JWT access token — access token refresh is handled by the standard refresh flow. There is no separate long-lived WebSocket credential; all auth chains back to the user's session. A WebSocket connection that drops is simply reconnected via the standard reconnection protocol (§5.3 — REST re-fetch + `?since_event_seq=N`).
- Auth rate limiting: 10 failed attempts per IP per minute → 429 with exponential backoff
- **Algorithm pinning**: JWT decode uses `algorithms=["HS256"]` explicitly — the `none` algorithm and any other algorithm are rejected. Algorithm confusion attacks (passing `alg: none` to bypass signature verification) must be impossible at the library call level, not just by convention.
- **SECRET_KEY entropy**: minimum 32 random bytes (256 bits). Enforced at startup — Modulo refuses to start (not just warns) if `SECRET_KEY` is the default value or fewer than 32 bytes. Startup check: `len(secrets.token_bytes(32)) == len(base64.b64decode(SECRET_KEY))` or equivalent. Document in deployment guide.

### 7.11 GitHub Connector OAuth Scopes
Minimum scopes per operation, declared in the connector's capability manifest:
- Read files: `contents:read` (fine-grained PAT)
- Write/commit/push: `contents:write`
- Create PR: `pull_requests:write`

Health check verifies actual token scopes via GitHub's `X-OAuth-Scopes` response header. Missing scopes surface as named errors (e.g. `missing_scope:pull_requests:write`), not generic auth failures.

### 7.12 LangGraph Checkpoint Data
Checkpoint blobs contain agent inputs/outputs — potentially PII or sensitive content. Currently stored in plaintext. Documented as a known gap. V2: application-layer Fernet encryption of checkpoint blobs before storage.

**Self-hosted admin bypass**: a self-hosted admin with direct Postgres access to the `langgraph.*` schema can read all checkpoint blobs for all pipelines and all runs, bypassing all application-layer access controls including team visibility (§6.14) and org-level RLS (which applies to the `public.*` schema only, not `langgraph.*`). This is an inherent property of self-hosted deployments where the database is administrator-controlled. **Required operational guidance** (deployment docs): restrict Postgres access to the application service account only; do not grant direct DB access to org operators; treat the `langgraph.*` schema as containing sensitive content equivalent to agent outputs.

### 7.13 Secrets Management
- Fernet encryption keyed from `FERNET_KEY` env var — separate from `SECRET_KEY` (which is used exclusively for JWT signing). Two distinct keys, two distinct cryptographic purposes. Key rotation for connector credentials (re-encrypt with new `FERNET_KEY`) must not require JWT session invalidation, and vice versa. Startup refuses to start if either key is absent or < 32 bytes (§6.10)
- Secrets never logged or returned in API responses
- **Credential-in-state rule**: decrypted connector credentials and model backend API keys must never enter LangGraph `StateGraph` state, checkpoint blobs, OTel span attributes, or log output. Connectors receive the decrypted credential in-process only (via a transient context object), use it for the API call, and do not serialise it. Violation means the decrypted key lands in plaintext checkpoint blobs (§6.12 gap) and OTel stdout. Enforced by code review and a lint rule banning credential field names from `state` dict assignments.
- Pluggable `SecretsBackend` interface (Vault, AWS Secrets Manager) — v2

### 7.14 Team Visibility — Server-Side Enforcement
The `view_as_team` parameter (admin board toggle) is enforced at the ViewModel command layer. Any request from a non-admin identity carrying this parameter returns 403. UI hiding is defence-in-depth only. Team-private resources are not returned in list responses, search results, or resource reads for non-members under any circumstances. There is no API path that reveals the existence of a team-private resource to a non-member (no 403 on enumeration — 404 as if the resource does not exist).

### 7.15 MCP Scope Enforcement — Dual Layer

MCP API scopes are enforced at two independent layers:

1. **Token middleware layer**: every request carries a bearer token; middleware validates the token and rejects requests where the required scope is not present. This is the outermost gate.
2. **ViewModel command layer**: every ViewModel command re-validates that the authenticated identity holds the required scope for that specific command. Middleware approval does not bypass this check.

This prevents scope bypass through unexpected routing, middleware misconfiguration, or future route additions that inadvertently skip the middleware layer. The ViewModel is the authoritative enforcement point; middleware is defence-in-depth.

### 7.16 SSE Per-Event Org Context Validation
MCP SSE connections stream run events. Org context is validated on every event emitted, not only at connection establishment. A token can expire or be revoked mid-stream; the SSE handler checks org membership before dispatching each event. An event from a different org is never emitted to a connected client under any circumstances.

### 7.17 DOM Sensitive Data Rule
Sensitive field values (API key secrets, connector credentials, webhook secrets, model backend API keys) must never exist in the DOM in plaintext unless the user has explicitly completed a server-authenticated reveal action. "CSS hidden" (display:none, visibility:hidden, opacity:0) is not a security control. Agent theme browser drivers and DOM-scraping tools have access to all DOM content regardless of CSS state.

**Rule**: sensitive values are rendered as `●●●●●●●` in the DOM by default. A reveal action calls the server; the server verifies authentication and returns the value in the API response body; the frontend injects it into the DOM for a time-limited display window (default: 30 seconds). The DOM is cleared after the window expires.

### 7.18 API Rate Limiting
Auth rate limiting (§6.10) covers login attempts only. Additional rate limits are required on high-value endpoints:

| Endpoint | Limit | Scope | Behaviour on exceed |
|---|---|---|---|
| `POST /api/v1/runs` | 60 requests/min | Per API key | 429 with `retry-after` |
| `POST /api/v1/webhooks/<trigger_id>` | 100 requests/min | Per trigger | 429; logged as `TriggerEvent` with `rate_limited` |
| MCP `trigger_pipeline` tool | 60 calls/min | Per MCP client ID | 429 |
| `POST /runs/{id}/hitl/{gate_id}/review` | 20 requests/min | Per user | 429 |
| Any MCP tool call | 200 requests/min | Per MCP client ID | 429 |

Implementation: Redis-backed token bucket via FastAPI middleware. Redis is an optional dependency in alpha (in-memory fallback with a startup warning: "Rate limiting is in-memory only — not suitable for multi-process deployments"). Required for v1 multi-worker deployments.

### 7.19 Break-glass Admin Recovery

An org can permanently lose access when its only admin cannot authenticate (forgotten password, SCIM deactivation/demotion of the last admin, role demotion, lost env secrets). Break-glass recovery lets a **Modulo operator** synthesize a time-boxed, single-use admin credential for any org — without weakening the last-admin invariants (prevention stays active; break-glass never bypasses it). The credential works through the normal `POST /api/v1/auth/login` flow (email + password). See the operational runbook (`docs/operations/break-glass-admin-recovery-runbook.md`) for the operator procedure.

#### Prevention: the last-admin guard

The last-admin invariant is enforced org-wide and active-aware on every mutation surface: REST `admin_update_user` (demotion and `is_active=false`), REST `admin_deactivate_user`, and SCIM `replace_user` (PUT), `patch_user` (PATCH, post-state), and `delete_user` (DELETE). A mutation that would leave the org with zero active, non-break-glass admins is rejected (REST 422, SCIM 409), with a clear "provision a replacement admin first" message. SCIM is closed as a lockout vector on all three verbs, not just PUT. Break-glass accounts are excluded from the remaining-admin count and never bypass the guard — the only path that removes the last non-break-glass admin is the operator `force-last-admin` command below.

#### Recovery: operator-synthesised, single-use, TTL-bounded credential

The operator CLI synthesizes a fresh `is_break_glass` admin account, its org membership, a `break_glass_activated` audit row, and a single-use credential in ONE transaction. `break_glass_expires_at` is DB-clock-authoritative and TTL-bounded (default 1440 min, max 4320 min / 72h). The account is `auth_provider=local`, invisible to tenant-facing surfaces, and consumed on first successful login by a login-route hook:

- **Early deny** — a break-glass account that is deactivated, NULL-expiry, expired, or inactive is denied immediately with a byte-identical 401 (`detail: "Incorrect email or password"`, no `WWW-Authenticate`), identical to a wrong-password response on a normal account, and recorded on the auth rate limiter.
- **Late CAS (family-first, CAS-last)** — for a live credential, the route mints the token family first, then runs the consume compare-and-swap as the FINAL DB statement before token issuance: `UPDATE accounts SET password_hash = <random non-bcrypt> WHERE id = :id AND password_hash = :old AND <live predicate>`, where the live predicate is emitted from the shared break-glass expiry builder and compares against the DB clock. A rowcount of 1 means this login consumed the credential; a rowcount of 0 (already consumed / expired / deactivated) raises 401 and rolls back the phantom family inside the same transaction. A CAS error is fail-closed to 401.
- **Fail-open for normal accounts** — the hook does zero extra DB queries when the account is not break-glass, and a hook error can never deny a normal login. Fail-closed only for break-glass accounts (a hook/CAS error → 401 + rate-limiter failure).
- **Refresh denied** — once the credential is no longer live (expired or deactivated), the SQL-predicate deny in role resolution folds every membership to `None`, so the refresh path denies 401 without minting or advancing anything. A consumed-but-unexpired session keeps refreshing until the TTL, bounding the session lifetime.

#### Operator CLI (`modulo-break-glass`)
The `modulo.cli.break_glass` CLI (registered as `modulo-break-glass`) connects as the dedicated `modulo_breakglass` DB role via `MODULO_BREAK_GLASS_DATABASE_URL` — a dedicated lazy engine, never the app `database_url`. Commands:

| Command | Purpose |
|---|---|
| `activate <org-id\|org-slug> --reason TEXT [--ttl-minutes N] [--dry-run] [--yes]` | Synthesize a fresh `is_break_glass` admin account, membership, `break_glass_activated` audit, and a single-use credential in ONE transaction. DB-clock expiry. Refuses without `--yes` in non-interactive shells; warns when a live activation already exists for the org. |
| `deactivate <org-id\|org-slug> --reason TEXT [--force] [--account-id UUID]` | Tombstone break-glass account(s) via the caller-bound `deactivate_break_glass` SECURITY DEFINER, with a `break_glass_deactivated` audit in the same transaction. Without `--account-id`, targets ALL undeactivated rows for the org. Refuses while a live activation exists unless `--force`. |
| `status [org-id\|org-slug] [--all] [--json]` | List break-glass rows (live by default; all states with `--all`). `status --all --json` is the daily-sweep form: exits non-zero (5) when any live row exists so the sweep can alert. |
| `force-last-admin <org-id\|org-slug> --reason TEXT` | The ONLY path that removes the org's last live non-break-glass admin. Refuses (exit 5) in a break-glass-only org, when the only live admins are break-glass accounts, or when multiple non-bg admins exist. Writes `last_admin_forcibly_removed` audit in the same transaction. |
| `smoke` | Connectivity probe (`SELECT 1`) + posture assertions (`session_user = modulo_breakglass`, `deactivate_break_glass` exists). Non-zero exit (7) on failure. |

**Operator authentication:** `--secret` or `MODULO_BREAK_GLASS_OPERATOR_SECRET`, checked with `hmac.compare_digest` against the configured primary/standby secrets. The actor written to audit payloads is derived from which secret matched (`operator` / `operator-standby`).

**Credential delivery:** interactive (TTY confirmation) or `--yes` + stdout. Printed ONLY after the activation transaction commits; exit 9 if the print fails after commit. Each credential is single-use (consumed by the login-hook compare-and-swap) and TTL-bounded. Delivery follows the specified out-of-band operator protocol (see the runbook).

**Exit codes (0-9):** 0 success · 1 unexpected · 2 usage (missing/empty `--reason`, bad `--account-id`) · 3 org-not-found (incl. deactivate M2040 target-does-not-exist) · 4 activation-transaction failure · 5 preconditions (force refusals, TTL out of range, the status-sweep live-row exit, operator-auth failure) · 6 deactivate-refused (live activation without `--force`, M2010/M2020) · 7 smoke failure · 8 deactivate atomicity failure · 9 credential-print failure.

#### Deny surfaces (break-glass accounts)

A break-glass account is denied everywhere a normal long-lived credential is minted or used:

- **API-key mint deny** — break-glass accounts cannot create or modify ANY of the long-lived secret-bearing resources: org API keys, outbound notification webhooks, trigger webhook HMAC secrets, OAuth clients, MCP, connector instances, model backends, pipeline nodes with HTTP/webhook-out endpoints, eval definitions, scheduled triggers. Enforced via a shared mint marker on create/update/delete (uniform 403). The login-route token-family mint is deliberately excluded.
- **Webhook use-time revalidation** — webhook dispatch revalidates the owning account against the expiry predicate (fail-closed) rather than trusting a cached decision.
- **Login deny** — the login hook early/late deny (see Recovery above), byte-identical 401.
- **Role-resolution deny** — the SQL-predicate deny folds break-glass memberships to `None` at every live-role site, so an expired/deactivated/NULL-expiry/inactive break-glass account resolves to `None` and every caller's None-check denies.

#### Config (startup-validated)
`MODULO_BREAK_GLASS_ENABLED` (independently settable, defaults from primary OR standby secret presence), `MODULO_BREAK_GLASS_SECRET` + `_STANDBY_SECRET` (must differ, minimum length; ENABLED + both empty is a blocking boot finding in fail-mode), `MODULO_BREAK_GLASS_TTL_MINUTES` (default 1440, min 1, `<= MAX_TTL_MINUTES`, also enforced at CLI invocation), `MODULO_BREAK_GLASS_MAX_TTL_MINUTES` (cap 4320), `MODULO_BREAK_GLASS_DATABASE_URL` (blocking when ENABLED, non-blocking warn otherwise), `MODULO_BREAK_GLASS_BOOT_FAILURE_MODE` (`warn`|`fail`).

#### Watchdog, monitoring, and the daily sweep

Boot-time config assertions (allow-list set-equality, `rolsuper=false`, no privileged-role membership — FATAL in both modes) plus URL/secret-presence checks that honour `warn`/`fail`. Exposed on `/healthz` as ADVISORY only (never flips readiness). Deactivation/force/status remain operable while secrets + URL are present even when ENABLED=false.

Runtime detection is a **daily operator sweep**: `modulo-break-glass status --all --json` on a scheduled workflow/harness task (secrets from the vault), non-zero exit when any live row exists (wired to an alert), plus verification of the last 24h of `break_glass_activated` / `last_admin_forcibly_removed` audit rows and a flag on live rows without a matching activation audit (the raw-INSERT forgery detector). Evidence committed to `Repos/admin/reviews/break-glass-daily-sweep/YYYY-MM-DD.md`, 90-day retention.

#### Invisibility and hygiene

Break-glass accounts are excluded from tenant-facing lists, seat counts, and SCIM sync; they are not billable; they cannot mint API keys, webhooks, OAuth clients, or other long-lived secrets; and deactivation re-randomizes the password hash into a tombstone. The credential is single-use, so replay of a captured password is impossible after the first login.

#### Trust-anchor residual

The operator is the deliberate trust anchor: a single compromised operator secret is effectively a full tenant-DB compromise (read-all + forge an admin + pollute-but-not-erase audit), detected by host-log + the daily sweep + the out-of-band protocol. Audit rows are INSERT-only for every role (append-only triggers block erasure), so the host log is the authoritative signal. Compensating controls are rotation and deactivate-all-on-loss. See the break-glass admin recovery plan and ADR-017/018 for the full threat model and operator protocol.

---

## 8. Feature Specifications

### 8.1 Model Backend Management

Model backends are a first-class resource, parallel to connector instances. Every agent depends on a model backend. The PRD treats them identically to connectors.

#### ModelBackend Entity
- `id`, `name`, `display_name`
- `provider`: `anthropic` | `openai` | `azure_openai` | `bedrock` | `ollama` | `custom`
- `model_id`: e.g. `claude-sonnet-4-6`, `gpt-4o`
- `credentials`: Fernet-encrypted API key / access key / endpoint config
- `default_params`: temperature, max_tokens, timeout
- `cost_tracking`: `enabled` | `disabled` (disabled for self-hosted/open-weight models)
- `currency`: configurable (default: USD)
- `organisation_id`: org-scoped
- `health_check`: test inference call on save; surfaces auth failures and quota errors

#### ModelBackend Registry
All model backends for an org are registered in the `ModelBackendHub`. Agents reference a `model_backend_id`. ConnectorHub and ModelBackendHub follow the same resolution pattern.

Alpha ships with 2 built-in model backend configurations:
1. **Anthropic Claude** (`claude-sonnet-4-6`): API key auth
2. **OpenAI GPT-4o**: API key auth

#### Credential Rotation
"Rotate credentials" action creates a new Fernet-encrypted credential record and validates with a health check before replacing the old one. In-flight runs hold a credential snapshot at run-start; rotation does not affect active runs.

#### Model Backend Deletion Protection
Deletion of a ModelBackend entity is blocked if it is referenced by any active agent definition or any PipelineSnapshot associated with a non-terminal run. Soft-delete (mark `status: deprecated`) is always available. Hard delete requires zero active references — same policy as schema version deletion (§7.3). A deprecated ModelBackend can still serve in-progress runs (the credential snapshot taken at run-start is used); it cannot be selected for new pipelines or new agent definitions (hidden from pickers).

#### model_id Resolution at Runtime
The agent runtime always uses the `model_id` from `PipelineSnapshot.model_backend_pins_json` — not the current ModelBackend entity's `model_id`. This ensures a pipeline paused at HITL for days executes consistently before and after resume, even if the operator has updated the ModelBackend entity in the interim. The operator's update takes effect only on future runs (new snapshots). Cost is computed against the pinned `model_id`; if the pinned model_id no longer exists in `config/model_pricing.yaml`, cost tracking falls back to zero and logs a warning.

### 8.2 Agent Model

An agent definition contains:
- `id`, `name`, `description`, `organisation_id`
- `input_schema`: Schema reference (ID + pinned version)
- `output_schema`: Schema reference (ID + pinned version)
- `prompt_template`: Jinja2 template (rendered via `SandboxedEnvironment`)
- `prompt_version_history`: list of prior prompt versions with timestamps and author — independent of pipeline versioning; rollback to any prior version
- `model_backend_id`: reference to ModelBackend
- `connector_type_refs`: list of required ConnectorType IDs with required operations
- `evals`: list of Eval definitions
- `retry_policy`: `max_retries`, `retry_on`, `backoff`
- `token_budget`: optional per-run token limit
- `library_id` (optional): source library primitive

**Prompt versioning**: every prompt edit creates a new version in `prompt_version_history`. Users can roll back to any prior version without creating a new pipeline version. PipelineSnapshot captures the specific prompt version in use at run-start.

**Generic Agents (Experimental)**: schema→prompt construction for novel pairs. Marked experimental. Requires eval rubric before production promotion.

### 8.3 Schema System

- Versioned (semver), reusable, composable
- **Folder organisation**: the schema list supports organisation-scoped, nested folders for grouping schemas (mirroring the pipeline folders feature). Users can create, rename, reorder, and delete folders, filter the schema list by folder, and move a schema into a folder or back to the unfiled list via drag-and-drop or the actions menu. Deleting a folder does not delete schemas: schemas directly assigned to it become unfiled, and direct child folders become top-level folders. Folder access uses the same organisation RLS context as schema access, and the folder API returns `501 Not Implemented` when the required database migration has not been applied.
- **Deletion protection**: schema versions referenced by any PipelineSnapshot, agent definition, or library entry cannot be deleted. Deletion is soft (mark deprecated); hard delete requires zero active references.
- **Deprecated schema behaviour**: a deprecated schema version can still be selected in the schema picker (shown with a deprecation warning badge) and pinned in new PipelineSnapshots — deprecation is a signal, not a block. Pipelines running against a deprecated schema version succeed; no runtime error is introduced by deprecation. Graph validation surfaces a warning (not error) when building a new snapshot against a deprecated version. Admins see a list of pipelines pinned to deprecated schema versions in the schema editor.
- **New version creation**: creating a new schema version is an explicit action ("New version" button in the schema editor), not an auto-save. Editing an existing version's fields is blocked if the version is pinned by any agent — the user is required to create a new version. Draft versions (unpublished) may be edited freely.
- **Storage in snapshots**: PipelineSnapshot stores schema references (ID + pinned version number), not embedded definitions. Deletion protection is the integrity guarantee.
- **Compatibility contract**: minor bumps backward-compatible. Breaking changes require major bump. Pre-run check surfaces mismatches.
- **Migration functions** (v1): Python functions between schema versions
- **Abstract schemas**: `abstract_name` is a string tag on a concrete schema (e.g. `document-input`, `issue-ticket`). It is user-defined — there is no enforced vocabulary in alpha. Two schemas with the same `abstract_name` are considered compatible for workflow bundle import matching. Abstract names are namespaced `author/name` in the v2 community registry to prevent collisions.

**Alpha schema editor scope**: field definition, type selection, required/optional, version history display, soft-delete guard. No union/collection types in alpha.

### 8.4 Pipeline Builder

#### Pipeline Folders
The pipeline library supports organisation-scoped, nested folders for grouping pipelines. Users can create, rename, reorder, and delete folders, filter the pipeline list by folder, and move a pipeline into a folder or back to the unfiled list. Folder access uses the same organisation RLS context as pipeline access.

Deleting a folder does not delete pipelines: pipelines directly assigned to it become unfiled, and direct child folders become top-level folders. Folder ordering is represented by a non-negative `sort_order`; names are required and limited to 255 characters. The folder API returns `501 Not Implemented` when the required database migration has not been applied.

#### Agent Picker
Adding a node to the pipeline canvas opens a slide-out agent picker panel: searchable by name and tag; shows agent description, input schema name, output schema name, and last-modified date; lists org agents and accessible library agents in separate tabs; "Add to pipeline" closes the panel and places the node. Schema compatibility is indicated: if the selected agent's input schema is incompatible with the previous node's output schema, a warning badge is shown (does not block selection — user may resolve in agent config).

#### Schema Picker
Used in agent configuration to select input/output schemas. Searchable by name, abstract_name, and tag; shows field summary (count, key field names) and current version; version selector dropdown for pinning. Used in schema editor cross-references. Displays deprecation warnings on soft-deleted schema versions.

#### Resource Ownership on Creation
Every resource creation dialog (pipeline, stage, connector, model backend) presents an **ownership picker**: `Org-wide (visible to all)` | `Team: [dropdown of user's teams]`. No silent default — the user must make an active choice. For users in a single team, that team is pre-selected but the picker is still shown. For users in no team, only `Org-wide` is available. Ownership can be changed after creation by an admin, or by the owning team's `operator`, subject to the post-snapshot rules (§7.13).

#### Pipeline Edge Data Model
Edges are first-class entities: `{id, pipeline_id, source_node_id, target_node_id, edge_type: "normal"|"reject", hitl_gate_config: HITLGateConfig|null}`. A HITL gate is a property of the edge, not a node — this is critical for LangGraph mapping. In LangGraph, an edge with `hitl_gate_config` compiles to a conditional edge with a wrapper node injecting `interrupt()` between source and target. PipelineSnapshot serialises the full edge list including gate config. `reject` edges are the target of a HITL rejection.

#### Manual (Placeholder) Node
A **manual node** represents a step in the pipeline with no AI automation attached — a human performs the work outside Modulo. When a run reaches a manual node, it pauses (identical to a HITL gate) and waits for a human to mark the step complete and provide the output manually. The human enters the output directly into the HITL review UI.

Manual nodes are the primary tool for SDLC onboarding: a team maps their existing process into Modulo — including steps that are still done by hand — and the pipeline runs as a governed, observable record of their SDLC even before any AI agents are introduced. Steps can be progressively replaced with agent nodes as automation is added. A manual node has no `agent_id`, no `connector_binding`, and no `model_backend_id`. It carries an `output_schema_id` so the human-provided output is validated before the run continues.

#### Agent Chain
Left-to-right node graph. Each node carries:
- Agent reference
- **`connector_binding`**: explicit `{type: "git-host", instance_id: "<uuid>"}` — set at pipeline-save time, not at run time. ConnectorHub performs a direct lookup at run — no auto-selection algorithm. If a user has multiple `git-host` instances, they must explicitly bind each node. This is the only safe resolution strategy for headless execution.
- HITL gate configuration lives on the outgoing edge (see Pipeline Edge Data Model above)

Nesting: max 3 levels. Breadcrumb drill-down.

**Canvas state across drill-down**: when the user navigates into a sub-pipeline (nested level), the parent canvas viewport (pan position + zoom) is saved to Vue Router state via `getViewport()` before the route transition. On return, `setViewport()` restores it. This applies per level — breadcrumb navigation restores each level's last viewport independently. State is in-session only (not persisted to `localStorage`).

#### CopyToAdaptWizard

Shared multi-step modal component used by copy-to-adapt flows in §7.14, §7.4 (ownership picker), and workflow import/binding (§7.15). Steps are configurable per context:

1. **Preview** — read-only view of the primitive being copied (schema summary, prompt template, agent config, or workflow graph)
2. **Ownership picker** — org-wide or team selection (same rules as §7.4 resource creation; no silent default)
3. **Binding UI** (workflows only) — map abstract connector requirements to configured ConnectorInstances; shown only when the copied primitive is a workflow with unbound abstract connector types
4. **Confirm** — summary of what will be created and under which team

The wizard is a single `CopyToAdaptWizard` component; callers pass a `steps` prop to include or exclude steps 3 and 4. Step navigation is linear; back is allowed at any step.

#### Real-Time Run Progress
WebSocket event stream per run: `node_started`, `node_completed`, `node_failed`, `hitl_awaiting`, `hitl_claimed`, `hitl_reviewed`, `run_completed`. Progress indicators travel along edges. Separate from ViewModel REST.

Each event carries a typed patch payload — Pinia stores apply patches directly rather than re-fetching the full ViewModel:

| Event | Payload fields |
|---|---|
| `node_started` | `run_id`, `node_id`, `started_at` |
| `node_completed` | `run_id`, `node_id`, `output_summary`, `completed_at` |
| `node_failed` | `run_id`, `node_id`, `error_code`, `error_message`, `failed_at` |
| `hitl_awaiting` | `run_id`, `gate_id`, `node_id`, `human_only`, `required_team_id` (nullable) |
| `hitl_claimed` | `run_id`, `gate_id`, `claimed_by_name`, `claimed_at` |
| `hitl_reviewed` | `run_id`, `gate_id`, `action` (`approved` \| `rejected`) |
| `run_completed` | `run_id`, `terminal_status`, `completed_at` |

Stores treat WebSocket events as **patch sources** — each event updates the relevant slice of local store state without invalidating the entire run. On WebSocket reconnect, the REST re-fetch (§5.3) replaces the store wholesale, after which the ring-buffer replay patches forward from `since_event_seq`.

#### Graph Validation
Pre-run hard validation (blocks run if invalid). On-save soft validation (warnings). Checks:
- Schema references exist at pinned versions
- ConnectorBindings have a valid instance_id with all required operations in `allowed_operations` — **hard block** if any required operation is missing (run cannot start; `connector_capability_mismatch` error)
- Deprecated schema version pinned — **soft warning** (run proceeds; surfaced in graph validation panel)
- ModelBackend reference exists and last health check passed within the past 5 minutes — **hard block** if missing or stale (`model_backend_unavailable` error). Health checks run on-demand at graph validation time (pre-run); no background polling.
- Topology valid (no unreachable nodes)
- Nesting depth ≤ 3
- Input/output schema compatibility across sequential edges
- **Pre-run input validation**: trigger input validated against entry agent's `input_schema` before run record is created. Validation errors returned immediately to caller with field-level detail.

Errors shown inline on canvas with user-readable messages.

#### Agent Theme (V1)
`?mode=agent` on any route. Minimal semantic HTML, full `data-testid`/ARIA. Initial state is bootstrapped via `GET /api/v1/viewmodel/current` — a REST endpoint that returns the current ViewModel snapshot for the active org. There is no `<script type="application/json" id="modulo-state">` server-rendered block; that approach is incompatible with the Vue 3 SPA hydration lifecycle and is not used.

**`GET /api/v1/viewmodel/current` contract**: requires JWT or API key bearer auth. Response: `{org: OrgSummary, pipelines: PipelineSummary[], pending_hitl: HitlGateSummary[], active_runs: RunSummary[]}`. Pipelines are filtered by the authenticated user's org and team visibility rules — same scoping as the standard pipeline list endpoint. `active_runs` contains only runs in non-terminal states. `pending_hitl` contains all gates in `awaiting_human` state across all org pipelines (respecting team visibility). Response is not paginated — it is a summary-level snapshot; full list endpoints with cursor pagination exist separately for large collections. Max response size: 500 pipeline summaries; if the org exceeds this, an `overflow: true` flag is included and the client falls back to the standard paginated endpoints.

### 8.5 Trigger System

#### Trigger Entity
`id`, `organisation_id`, `pipeline_id`, `trigger_type` (`manual` | `webhook` | `cron` | `polling` | `agent_signal` | `ongoing`), `active` (boolean — disabled triggers log events but do not create runs), `max_concurrent_runs` (int, default 1), `daily_spend_limit` (nullable decimal USD), `config_json` (type-discriminated; see below), `created_at`, `created_by`.

**`config_json` by type**:
- `manual`: empty `{}`
- `webhook`: `{webhook_secret: string, payload_mapping: JSONPath-map, retain_payload: bool, queue_depth: int}`
- `cron` (v1): `{schedule: cron-string, timezone: IANA-tz, input_template: JSON-object}`
- `polling` (v1): `{connector_instance_id, poll_query, condition_expression, poll_interval_seconds}`
- `agent_signal` (v1): `{source_pipeline_id, source_node_id, signal_schema_id}`
- `ongoing` (v1): `{scan_interval_seconds: int >= 60 (default 60), input_template: JSON-object (default {}), snapshot_id?: uuid (optional pin — invalid/missing pins skip the fire and are never silently auto-created)}`

**`ongoing` semantics (FAR-158)**: an ongoing trigger keeps its pipeline topped up to `max_concurrent_runs` in-flight (active **or queued**) runs — like a daemon / worker pool — until it is deactivated (persistent config failure, or a no-delivery streak; see *Ongoing-trigger auto-deactivation (no-delivery streak)* below). The SAQ system scheduler scans active, non-soft-deleted ongoing triggers every ~60s (or the trigger's own `scan_interval_seconds`, floored at 60) and enqueues a per-item top-up job. The top-up counts runs whose status is `pending` (queued), `running`, or `claimed`; **`awaiting_human` is NOT counted** — a never-answered HITL gate must not permanently starve the pool (claim expiry resets the claim but the run stays awaiting_human; dispatcher_reconcile only resumes awaiting_human with a committed decision). The effective pool is `min(trigger.max_concurrent_runs, pipeline.max_concurrent_runs)` — read at top-up time so a pipeline cap lowered after create (and multiple ongoing triggers on one pipeline) are honoured. When the in-flight count is at/above the effective target the fire is a no-op (no event, no `last_fired_at` write); below target, `target - in_flight` runs are created in one transaction, one `accepted` TriggerEvent each, then dispatched to SAQ post-commit. Committed-but-never-dispatched pendings are recovered by `dispatcher_reconcile`'s existing `pending + dispatched_at IS NULL` branch. **A daily spend limit is REQUIRED** at create/update for ongoing triggers (a never-terminating daemon with no cost ceiling is a runaway hazard) and is enforced per batch inside the top-up transaction (skip-not-defer, `last_fired_at` still stamped); the DB also enforces the target range 1..20 (partial CHECKs `ck_triggers_ongoing_spend_limit` / `ck_triggers_ongoing_target_range`). A pinned `snapshot_id` that is invalid or missing logs a `no_pipeline` event and skips (never silent auto-create); otherwise the latest snapshot is used (pre-resolved by the scan) or auto-created once. Persistent-failure deactivation mirrors the report pattern: after 5 consecutive no_pipeline/snapshot failures the trigger is deactivated. Ongoing is intentionally excluded from the missed-fire catch-up scan and the hourly missed-fire alert — it self-heals (the top-up recomputes from current state) and an at-target no-op is not a missed fire.

Cron expressions are evaluated as wall-clock schedules in their named IANA timezone, while `next_fire_at` is persisted in UTC. Across daylight-saving transitions, a nonexistent local time advances to the first valid instant and an ambiguous local time uses its first occurrence, matching `croniter` with `zoneinfo`.

**Cardinality**: one trigger belongs to exactly one pipeline. One pipeline may have multiple triggers. When a trigger is deleted: in-flight runs continue against their snapshot to completion; no new runs initiated. Trigger record cascade-deleted; run records retained.

| Type | Alpha | Description |
|---|---|---|
| `manual` | Yes | `POST /api/v1/runs` with input payload |
| `webhook` | Yes | Inbound HTTP POST with HMAC secret validation |
| `cron` | v1 | Schedule-based |
| `polling` | v1 | Polls connector for condition |
| `agent_signal` | v1 | Fired by another pipeline's output |
| `ongoing` | v1 | Keeps pipeline topped up to a target number of in-flight (active or queued) runs |

#### Ongoing-trigger auto-deactivation (no-delivery streak)

An ongoing trigger is a daemon by design, but a quiet stretch is a signal worth surfacing: **after N consecutive no-delivery terminal runs, an ongoing trigger auto-deactivates itself, raises a notification, and stays off until an operator re-enables it.** "No-delivery" (run classification, FAR-189) covers both a run that completed but delivered nothing because the backlog was empty, and a run that failed from an infra/sandbox error elevated to `failed`. An idle-stop is **intended** behaviour — a pool that stops delivering should stop, and the deactivation is what prompts the operator to investigate the quiet stretch.

The streak counts terminal runs since the later of the last delivered run and the trigger's streak epoch (`GREATEST(last_delivery_at, streak_epoch)`); the epoch is reset when the trigger is created, re-enabled, or restored, and is backfilled at deploy time so an existing quiet trigger is never deactivated on the first sweep tick. A run that delivered (a valid `pr_url`) resets the streak. `cancelled` and `budget_exceeded` runs are excluded — they neither count nor reset — and a run with a missing or broken classification record stops the walk fail-closed (deactivation never rides on uncertain evidence). The sweep runs as a background system job every ~60s and never inline at run completion; in-flight runs are drained, never cancelled, and a delivery that lands after deactivation does not re-activate the trigger.

Configuration (per-trigger, in `config_json`):
- `max_no_delivery_streak` — the streak threshold. Default 5. The legacy key `max_consecutive_failures` is read as a fallback for one release. Only genuine integer values are accepted; a boolean, float, or out-of-range value falls back to the default, so a mis-typed config can never fire instantly.
- `no_delivery_min_window_hours` — the minimum wall-clock window before a streak can deactivate (the quiet stretch must be at least this old). Default 24h; override the default per-deployment via `MODULO_ONGOING_STREAK_MIN_WINDOW_HOURS` (0 disables the window entirely, for repos where any quiet stretch should stop the pool) or per-trigger via this config key. A per-trigger value overrides the env default.

Re-enabling an auto-deactivated trigger is an operator action (`trigger.update` permission, i.e. the `operator` role): re-activating atomically re-anchors the streak epoch and clears the config-failure Redis counter, so the streak restarts cleanly. A deactivation is recorded as an `ongoing_trigger.auto_deactivated` AuditEvent plus an `auto_deactivated` TriggerEvent, and raises the `trigger_deactivated` notification.

Operational guards:
- **Kill switch** — `MODULO_STREAK_DEACTIVATE_KILL_SWITCH` disables the deactivate + notify side-effect org-wide (run classification still persists; only the deactivation is gated).
- **Per-org per-hour cap** — at most 10 auto-deactivations per org per rolling hour; a burst beyond the cap is deferred to the next sweep tick rather than cascading.
- **Mass-cascade alert** — when an org has deactivated 5+ triggers within 24h (an infra-outage signature), a critical alert fires, once per window.

#### WebhookTrigger Spec
- System-generated `webhook_secret` per trigger instance
- Inbound path: `POST /api/v1/webhooks/<trigger_id>`
- Auth: `X-Modulo-Webhook-Secret` header validated via constant-time HMAC comparison
- **`payload_mapping`**: JSONPath or JMESPath expression mapping raw payload fields to the entry agent's input schema. Required field — no passthrough. Example:
  ```json
  {
    "document_url": "$.repository.html_url",
    "branch": "$.ref",
    "commit_sha": "$.after"
  }
  ```
  If mapping produces a payload that fails schema validation, the trigger event is logged as `schema_validation_failed` and no run is created.
- **Flood protection**: configurable `max_concurrent_runs` per trigger (default: 1). Tracked via a Postgres counter with `SELECT ... FOR UPDATE SKIP LOCKED` on the trigger row — not an in-memory counter (which breaks with multiple server processes). New webhook fires exceeding the limit are queued (configurable queue depth, default: 10) or rejected (configurable).
- **Deduplication**: configurable payload deduplication window (default: 60s). Payload hash stored in `webhook_dedup_hashes` table (`id`, `trigger_id`, `payload_hash`, `expires_at`). Unique constraint on `(trigger_id, payload_hash)`. Duplicate hashes within the window create one run; subsequent fires are logged as `deduplicated`. DB-backed — safe across multiple server processes. **Cleanup job**: runs every 5 minutes; deletes rows where `expires_at < NOW()`. Advisory lock `'webhook_dedup_cleanup'` ensures single-worker execution. Failure of the cleanup job does not block webhook processing — expired rows cause false deduplication at worst (a webhook fires twice in the expired window; the second is treated as unique). Job failure is logged as an OTel error span.

#### TriggerEvent Log
Every trigger activation creates a `TriggerEvent` record:
- `trigger_id`, `trigger_type`
- `raw_payload_hash` (not the payload — never log raw payloads; they may contain secrets)
- `received_at`
- `validation_result`: `passed` | `hmac_failed` | `schema_validation_failed` | `deduplicated` | `concurrency_limit_reached`
- `run_id` (if a run was created)
- `error_detail`

Operators can view the TriggerEvent log and **replay** any logged event (re-fires the trigger with the captured payload hash — requires the original payload to have been stored if the trigger is configured with `retain_payload: true`). **Replay auth (ADR 017)**: replay is a mutating run-creation channel — a principal must hold the `run.trigger` permission (`runner` minimum); an unauthenticated caller must present a valid HMAC signature over the stored payload (same verification as webhook receive).

**`retain_payload` storage**: when `retain_payload: true`, the raw webhook payload is stored in a `webhook_payloads` table (`id`, `trigger_event_id`, `payload_ciphertext`, `created_at`, `expires_at`). The payload is encrypted with Fernet (same mechanism as connector credentials, §6.2). `expires_at` is set to `created_at + 7 days` by default (configurable per trigger, max 90 days). A background cleanup job deletes expired rows nightly. Access to stored payloads is restricted to `operator` and `admin` roles — `runner` role cannot retrieve them. Payloads may contain sensitive data from the calling system (API tokens embedded in GitHub webhooks, repository metadata); operators must acknowledge this when enabling `retain_payload`.

#### Org-wide "Pause All Pipeline Triggers" (Kill-Switch)

An org admin can pause **new trigger-initiated runs** org-wide via
`PUT /api/v1/admin/orgs/{org_id}/triggers/pause` with body `{paused: bool}`
(dependency `org.triggers.pause.manage`, admin minimum, `kill_switch_eligible=False`
so the org authz kill-switch can never lift this gate). The toggle is idempotent
(re-PUTing the current state writes no audit event) and is audited as
`triggers_paused` with payload `{paused: bool}`; the audit write is
**fail-open-with-alert** — the toggle ALWAYS commits, a failed audit write is
loudly logged and never rolls back the toggle.

**What is paused:** any path that creates a NEW trigger-initiated run — webhook,
replay, cron, polling, and agent_signal — is blocked at the single `create_run`
gate (the authority). Cron/polling fire jobs return `{"status":"skipped",
"reason":"triggers_paused"}`. Webhook/replay deliveries to a paused org return
**HTTP 202 `{"status":"paused"}`** with **no `run_id`**, and exactly one
`TriggerEvent` with `validation_result='paused'` is committed (written
in-transaction by the route). If the org row has been HARD-deleted (orphan
trigger), the paused delivery still returns 202 `{"status":"paused"}` but the
`TriggerEvent` write is skipped (an insert would violate the organisations FK →
503). Agent signals log a single `paused` TriggerEvent and skip.

**What is NOT paused (documented escape hatches):** running pipelines continue
against their snapshot to completion; manual runs (`POST /runs`),
`test_trigger`, MCP `trigger_pipeline`, feedback correction, variant runs, and
**scheduled reports** are never paused. `dispatcher_reconcile` re-dispatches
only runs that were enqueued before the pause — any path that would create a NEW
run passes through the `create_run` gate.

**Semantics are SKIP-not-defer:** cron/polling fires that land during a pause
are dropped, not replayed (the scheduler still advances `next_fire_at` so
unpausing never triggers a catch-up storm). Webhook events delivered during a
pause are dropped; recovery is the sender's responsibility (redelivery after
unpause). The scheduled-tick audit is counters + summary only (`cron_skipped_paused`
/ `polling_skipped_paused` in the `fire_due_triggers` summary), never per-trigger
`TriggerEvent` rows.

**Deploy order is migration-before-code** (0069 adds `organisations.triggers_paused`
+ `triggers_paused_at` and widens `ck_trigger_events_validation_result` to the
full 19-value vocabulary). Until the migration lands, the scheduler's batched
pause read (a dedicated read in its own session/transaction) degrades on a
`ProgrammingError` (missing `triggers_paused` column) to treat all orgs as
not-paused (`pause_read='degraded'` — the transient pre-migration window). Any
OTHER read failure (DB down / connection error) RE-RAISES so the tick fails and
the SAQ system cron retries — a pause-read failure is never fabricated into
"paused" for every org. Per-item cron/polling fire jobs mirror this: a
`ProgrammingError` on their org-pause read degrades to not-paused inside a
savepoint (the per-item transaction is never poisoned); any other
`SQLAlchemyError` propagates so the job fails and SAQ retries.

#### TLS for Webhook Triggers in Alpha
Alpha webhook trigger is tested with generic HTTP payloads, not GitHub specifically. GitHub requires HTTPS. Local development uses ngrok or similar tunnel — documented in the developer setup guide. The docker-compose ships a commented-out Caddy config for HTTPS termination. GitHub webhooks are a v1 use case deployed behind real TLS.

### 8.6 Integration Layer (Connectors)

#### ConnectorHub Credential Lifetime
ConnectorHub decrypts connector credentials **once at run-start** during connector initialisation for that run. The decrypted connector instance is held in a run-scoped context object that is discarded at run end. One Fernet decrypt call per connector per run — not per node invocation. The run-scoped context object never enters LangGraph state, checkpoint blobs, OTel span attributes, or logs (§6.13 credential-in-state rule). This balances performance (one decrypt) with the shortest practical credential lifetime.

#### ConnectorType Interface
```python
class ConnectorType(ABC):
    type_id: str
    display_name: str
    capabilities: list[str]  # declared optional capabilities

    def read(self, query: ConnectorQuery) -> ConnectorResult: ...
    def write(self, payload: ConnectorPayload) -> ConnectorResult: ...
    def health_check(self) -> HealthResult: ...
```

Capability declarations: GitHub and GitLab have different feature sets. Connector types declare which optional capabilities they implement (e.g. `create_pr`, `check_runs`, `branch_protection`). Agent definitions declare which optional capabilities they require. Graph validation warns if bound instance doesn't satisfy requirements.

**Prompt portability caveat**: connector type abstraction handles API operations. Agent prompt templates may use platform-specific terminology ("pull request" vs "merge request"). Prompt portability is the user's responsibility — a known limitation of shareable workflows.

#### Alpha Connector Implementations

**`FilesystemConnector`** (`git-host` type):
- Read: read files by path/glob, parse markdown, clone/pull git repo
- Write: write file, `git add`, `git commit`, `git push`
- Capabilities: `read_files`, `write_files`, `git_push`
- **`base_path` (required, admin-configured at ConnectorInstance level)**: all file read and write operations are resolved relative to `base_path`. Path traversal is blocked: resolved path must have `base_path` as a prefix (checked via `os.path.realpath()` on both sides before comparison). Operators cannot configure paths outside `base_path` — this is a security boundary. Failure to enforce this allows a pipeline author to write to arbitrary filesystem paths (e.g. `/etc/cron.d/`, SSH authorized_keys).

**`GitHubConnector`** (`git-host` type):
- Read: read files from GitHub repo via API
- Write: write via GitHub Contents API, commit, push
- PR: create pull request
- Capabilities: `read_files`, `write_files`, `git_push`, `create_pr`
- Auth: Fine-grained PAT (scopes: `contents:read`, `contents:write`, `pull_requests:write`)
- Health check: verifies token validity + required scopes via `X-OAuth-Scopes` response header

A pipeline built against `FilesystemConnector` rebinds to `GitHubConnector` at import time without agent changes. This is the demo proof of the ConnectorType abstraction.

#### Connector Health Check
Before run start: all referenced connector instances are health-checked. Failed check surfaces as a pre-run error with named failure (e.g. `credential_expired`, `endpoint_unreachable`, `missing_scope:create_pr`). Run blocked until resolved.

#### Credential Rotation
"Update credentials" action. Post-rotation health check fires automatically. In-flight runs use credential snapshot from run-start; unaffected.

### 8.7 Run Concurrency Controls

| Control | Scope | Behaviour |
|---|---|---|
| `max_concurrent_runs` | Per pipeline | New run requests blocked (queued or rejected) when limit reached. Default: 5. |
| `max_concurrent_runs` | Per trigger | New trigger fires blocked when limit reached. Default: 1. |
| `max_concurrent_runs` | `ongoing` trigger (FAR-158) | The **target pool size**: the top-up keeps `min(trigger.max_concurrent_runs, pipeline.max_concurrent_runs)` runs in-flight (active or queued; `awaiting_human` NOT counted). Bounded 1..20 by the DB partial CHECK `ck_triggers_ongoing_target_range` and by validation at every write surface. |
| `sandbox_concurrency_limit` | Per organisation | Max concurrently `running` sandbox-agent runs across all pipelines in the org. Default: `null` (unlimited). Runs beyond the cap are demoted back to `pending` with `error_code='org_capacity_limited'` and retried in the background; after the capacity-timeout TTL they terminal-fail with `capacity_timeout`. Stored in `Organisation.settings_json`. |
| `run_concurrency_limit` | Per organisation | Max concurrently executing/claimed runs across ALL pipelines in the org (sandbox-agent and otherwise). Default: `null` (unlimited). Runs dispatched while the org is at this cap are deferred back to `pending` with `error_code='org_capacity_limited'` and retried in the background; after the capacity-timeout TTL they terminal-fail with `capacity_timeout`. Stored in `Organisation.settings_json`. Independent of `sandbox_concurrency_limit`; both org caps share the same `org_capacity_limited` marker on deferred runs. |
| Write lock | Per connector instance + target resource | Advisory lock on (connector_instance_id, target_resource) for write operations. Prevents concurrent runs corrupting shared state (e.g. two runs pushing to the same git branch). |

Write lock is advisory (application-layer, using Postgres `pg_try_advisory_lock`). The `waiting_for_lock` run sub-state was **excised** in the runtime hardening (migrations 0074/0075): it is NOT part of the final run status set, and any legacy rows were backfilled to `pending`. The `lock_wait_timeout_seconds` knob (default: 300, min: 30, max: 3600, stored on the Pipeline entity) remains reserved for the write-lock design. The final run status set is exactly: `pending`, `running`, `awaiting_human`, `claimed`, `complete`, `failed`, `cancelled`, `eval_failed`.

### 8.8 HITL (Human-in-the-Loop)

#### Run Entity
`id`, `organisation_id`, `pipeline_id`, `snapshot_id` (FK to PipelineSnapshot), `trigger_id` (nullable FK — null for manual runs), `trigger_type` (`manual` | `webhook` | `cron` | `polling` | `agent_signal`), `status` (see state machine below), `created_by` (user_id; null for webhook-initiated runs with no authenticated caller), `input_hash` (SHA-256 of the entry input payload; stored for audit without storing the payload itself), `created_at`, `started_at` (nullable — set when run transitions from `pending` to `running`), `completed_at` (nullable — set on terminal state), `cancellation_requested` (boolean, default false — polled by the execution loop to implement graceful cancel), `total_tokens` (nullable integer — accumulated from all LLM calls; null until first LLM call completes), `total_cost_usd` (nullable decimal — null when `cost_tracking: disabled`), `error_code` (nullable — named error code on `failed` terminal state; **non-terminal capacity markers**: when a run is demoted back to `pending` at a concurrency limit, `error_code` carries a non-terminal marker — `pipeline_capacity` (per-pipeline `max_concurrent_runs`) or `org_capacity_limited` (either org cap: `sandbox_concurrency_limit` for sandbox-agent runs or `run_concurrency_limit` for all org runs) — distinct from terminal failure codes such as `never_dispatched`, `worker_lost`, `capacity_timeout`, and `task_failure`. The marker is cleared on admission to `running`; the stale-run sweep exempts reason-marked pending runs and terminal-fails them with `capacity_timeout` only after the TTL elapses.), `langgraph_thread_id` (the checkpoint thread identifier used with `AsyncPostgresSaver`).

#### Run State Machine
```
pending
  → running
      → awaiting_human        (HITL gate reached)
          → claimed            (user has opened the review; atomic DB lock held)
          → running            (after approve — continues)
          → running            (after reject — routes to reject-target node)
      → complete
      → failed
      → cancelled
```
The `waiting_for_lock` sub-state was excised in migrations 0074/0075 (rows backfilled to `pending`); it does not appear in the final status set.

#### Cancellation Mechanics
`POST /api/v1/runs/{id}/cancel` (MCP: `cancel_run` tool). Calling cancel on a terminal-state run returns 409 `run_already_terminal`.

**Graceful cancel for `running` runs**: sets `cancellation_requested = true` on the run record. The LangGraph execution loop checks this flag at each pre-node transition. Implementation: every agent node function is wrapped in a Modulo-provided async decorator (`@cancellable_node`) that checks `run.cancellation_requested` from the DB before invoking the node body. This decorator is applied automatically by the node registration layer — pipeline authors do not write it. On detection, the decorator raises `CancellationRequestedError`, which is caught by the top-level run executor (not by LangGraph's error handling), transitioning the run to `cancelled`. The current node is allowed to complete; cancellation fires before the *next* node. This is polling-based — no external async interrupt is injected into LangGraph mid-node execution.

**Per-node execution timeout**: each node is subject to a `node_timeout_seconds` cap (default: 300, configurable per-pipeline). The `@cancellable_node` decorator wraps the node body in `asyncio.wait_for(node_fn(...), timeout=node_timeout_seconds)`. On `asyncio.TimeoutError`, the run transitions to `failed` with error code `node_timeout`. This ensures cancellation from `running` is eventually actioned even if a node blocks on an unresponsive external call.

**Cancel from `awaiting_human`**: immediately transitions the run to `cancelled`. Any held HITL claim is released: `claimed_by` reset to NULL, `claim_token` invalidated (DB row deleted). No notification is dispatched for the cancelled gate — the run is terminal.

**UI behaviour**: the run detail page shows a "Cancel" button for all non-terminal runs (`pending`, `running`, `awaiting_human`, `claimed`). The runs list page shows a per-row Stop action (two-step inline confirm) for the same non-terminal statuses; terminal rows show no action. On cancel confirmation, the button is disabled and the status shows `cancelling` (pending the next transition check). On the next state push via WebSocket, the status updates to `cancelled`.

#### HITL Gate Definition
Each gate carries:
- `label`: human-readable name
- `description`: context shown to reviewer
- `reject_target`: node ID to route to on rejection. When a `feedback_handler` is set on this gate, `feedback_handler` supersedes `reject_target` — the current run spawns a correction run rather than routing inline. Setting both fields on the same gate is a validation error (`reject_routing_conflict`). See §8.20 for full interaction rules.
- `claim_expiry_minutes`: per-gate configurable; no global default
- `human_only: boolean` (default: `false`): when `true`, MCP `review_hitl` with `action: approve` returns 403. LLM agents can claim and inspect but cannot approve. Only a browser-authenticated human can approve. Enforced at ViewModel command layer (not just middleware).
- `required_team_id` (optional): when set, only members of this team with `runner` or `operator` team role can claim or approve the gate. Enforced at ViewModel layer with a **DB-live membership check** (not JWT claims) — this is a security-critical path where the 15-minute JWT stale window is unacceptable. Gate context exposes `required_team_name` so reviewers and LLM clients understand the restriction.

**`human_only` + `required_team_id` — additive**: when both are set, approval requires both conditions to hold independently: (a) authenticated as a human via browser (not MCP), AND (b) a member of `required_team_id` with `runner`+. Neither condition alone is sufficient. Both are enforced at the ViewModel command layer.

#### HITL Gate Integrity Guard
Removing or weakening an existing HITL gate is a security-sensitive write guarded by the service-layer backstop (`hitl-gate-removal-guard-plan.md` v19 — concrete spec for ADR 017's "Service-layer backstop" section):

- **Gate weakening is governed by the service-layer backstop**: the route layer carries the operator baseline (`pipeline.graph.update` on the graph-write endpoints) for defense-in-depth breadth, and **operator+ privilege is required under the row lock** in `replace_pipeline_graph()` / `rollback_to_snapshot()`. A non-privileged caller that weakens an existing gate is denied before any delete/insert executes (guard-runs-before-delete). There is deliberately no admin-only route gate — operators are "privileged" for weakening by design (equivalent weakening stays reachable via `update_pipeline`, `convert_to_agent`, `revert_to_manual`).
- **Weakening** is any change to an existing `hitl_gate_config` that reduces its effect: `human_only` true→false; `required_team_id` cleared or changed; `condition`/`eval_condition` changed at all (both are evaluated before `human_only` is consulted); or the gated edge's topology key `(source_node_id, target_node_id, edge_type)` removed / its config nulled. Edge creation with no prior row is never weakening. `claim_expiry_minutes` is not weakening: a shorter expiry is stricter (on expiry the claim resets to `awaiting_human` — it never releases the gate).
- **MCP can never weaken gates**: the MCP call site passes the literal `caller_type="mcp"` and the guard hardcodes `is_privileged=False` for MCP callers (no DB query).
- **Denied attempts are audited** as `hitl_gate_removal_denied` (reason-coded: `insufficient-role` / `role-changed-reauth-required` / `role-check-db-error` / `correlation-key-mismatch` / `legacy-snapshot-ambiguous` / `mcp-weakening-not-permitted`); allowed weakening is audited as `hitl_gate_removed`. Both are registered as `scope: admin` notification events.
- **Rollback is fail-closed**: a historical snapshot with missing/`None` gate fields is treated as weakening (`legacy-snapshot-ambiguous`).
- **Non-liftable**: the guard never consults `authz_enforce` — it is an always-on carve-out (ADR 017 Decision 3).

#### HITL Flow
1. LangGraph `interrupt()` fires. State → `awaiting_human`.
2. Outbound notification webhook dispatched (HMAC-signed).
3. First user to POST `/runs/{id}/hitl/{gate_id}/claim` acquires atomic DB lock (`UPDATE ... WHERE claimed_by IS NULL RETURNING id`). Returns a `claim_token` — **alpha**: a cryptographically random opaque string stored in the DB with a 15-minute TTL (no JWT infrastructure required in alpha); **v1**: short-lived JWT scoped to `run_id + gate_id + client_id`, 15-minute expiry, once JWT infrastructure ships with full user management. **Race — claim already held**: if the UPDATE returns zero rows, the server returns 409 `gate_already_claimed` with `claimed_by_display_name` in the body. The UI disables the claim button and shows "Claimed by [name]" — it does not surface an error modal. The gate page polls `GET /runs/{id}/hitl/{gate_id}` every 10 seconds while claimed to surface expiry or release.
4. Claiming user sees: preceding agent input/output, pipeline state, next step's expected input schema.
5. Approve → passes output unchanged; run continues. Reject → routes to reject-target node with required reason. Both require the `claim_token` from step 3.
6. **Modify-then-approve** (v1): user edits output payload before approving.
7. **Deliver manually** (v1): user provides the full output themselves.
8. All HITL actions written to AuditEvent.

**Claim expiry**: per-gate configurable. No global default. When a claim expires, `claimed_by` resets to NULL, `claim_token` is invalidated (DB row deleted or TTL expired), and a new notification dispatches.

**HITL conditions** (v1): require approval only if eval score < threshold, or only on first run.

#### Long-Running Pipeline Retention
Pipelines paused at HITL may persist for days or weeks. LangGraph checkpoints accumulate:
- **Run retention**: configurable TTL after run reaches terminal state (default: 90 days). **Retention job** (`cleanup_old_runs`): processes in batches of 500. Query: runs where `created_at < NOW() - retention_days * interval '1 day'` and `status IN (complete, failed, eval_failed, cancelled, stalled)` (the `TERMINAL_STATUSES` set, §8.32.4). Action: delete the entire `runs` metadata row. LangGraph checkpoint rows (`checkpoints`, `checkpoint_blobs`, `checkpoint_writes`) do NOT cascade on run deletion — the saver schema defines no foreign keys — so the retention job also purges them separately via `batch_delete_langgraph_checkpoints` (same retention window, batch of 500). Job failure is logged to the application logger; does not affect active runs.
- **Analytics facts are EXEMPT from the run purge**: `run_daily_facts` (ADR 020) is NOT deleted by the run-retention job — `run_daily_facts.run_id` has no FK to `runs`, so facts survive the 90-day purge and keep dimensioned analytics history alive (see §8.32). Facts have their own retention: **13 months by default**, config-driven via `analytics_facts_retention_months`, enforced by the analytics maintenance cron's `retention_facts` step using **chunked day-slice deletion** (7-day slices, one bounded DELETE per slice) so each statement stays small.
- **HITL overdue warning**: configurable per-gate. If a run remains in `awaiting_human` beyond N hours, a new notification fires and the UI surfaces a warning badge.
- **Admin purge action**: admins can force-terminate and archive stale runs.

##### Run Payload Retention
When `retain_payload` is enabled on a run, the run's `input_payload` and `outputs_json` columns are retained past the run's lifecycle. The **payload retention job** (`cleanup_retained_payloads`) nulls these columns for runs older than the retention period (default: 30 days) in terminal states. Processes in batches of 500.

##### TriggerEvent Log Cleanup
The `TriggerEvent` log accumulates records for every trigger activation (webhook, cron, polling, agent_signal, ongoing). Two cleanup jobs bound its growth. The **webhook-dedup cleanup job** (`cleanup_old_webhook_events`) deletes events older than 30 days in batches of 1000 (the webhook replay window). The **trigger-events retention job** (`cleanup_old_trigger_events`) is the canonical age-based retention: it deletes events whose `received_at` is older than the retention period (default: 90 days, aligned with the run-retention policy, and comfortably exceeding the replay window so replayable events are never purged) in batches of 500, indexed on `received_at`. Both run on a scheduled loop every 60 minutes and as SAQ system crons (`unique=True`, so overlapping ticks cannot interleave). Failure of either cleanup job does not block trigger processing — stale events cause no functional harm beyond table bloat.

### 8.9 Error Handling

**Per-node retry policy**: `max_retries` (default: 2), `retry_on` (list: `rate_limit`, `timeout`, `schema_validation_failure`, `connector_error`), `backoff` (`linear` | `exponential`, default: `exponential`).

**Pipeline retry policy (auto re-dispatch)**: a pipeline may declare `retry_policy` as `{"on": ["stall" | "timeout" | "failure"], "max_retries": 0-5}`. When a run ends in a configured state and the retry budget remains, the run is reset to `pending` and automatically re-dispatched as a new attempt instead of terminal-failing — `max_retries` counts actual retries (max_retries=1 yields one retry, i.e. two execution attempts). Re-dispatches wait a jittered, capped exponential backoff (base ~45s doubling per attempt, capped at ~5min, bounded by `max_retries`) so a persistent failure does not re-fire back-to-back. The `"failure"` event excludes sandbox-agent hang deaths: a run that terminalizes as `error_code="node_cancelled"` with `error_detail` containing `"likely hung"` is NOT re-dispatched (each re-dispatch would burn a full node timeout with zero recovery probability); transient `node_cancelled` outcomes stay retryable. Known limitation: the `stall` event covers node-idle stalls only (a node returns a stalled output); the executor-level zombie-watchdog stall terminal-fails the run before the retry decision, so it is not retried.

**Dead-session classification (FAR-227)**: when an E2B opencode session dies before producing output, the sandbox wrapper writes a FALLBACK echo `{"status":"failed","summary":"No output from agent - session interrupted"}` to `/home/user/output.json`. This is parseable JSON but is NOT an agent verdict — the agent never spoke. Such a node terminalizes with the retryable `sandbox.no_output_json` code (never `agent.failed`), so a transient session death is auto-re-dispatched under a `{"on": ["failure"]}` retry_policy instead of counting as a non-retryable agent failure. The summary is preserved as the failure detail; a genuine agent-written `agent.failed` verdict (output.json the agent actually wrote) is unchanged and stays non-retryable.

**Idempotency gate for side-effecting sandbox nodes (FAR-228)**: an opt-in `sandbox_agent` node may declare a `delivery_sentinel` — a literal string the agent prints on its own line when the side effect (e.g. an email) has been sent. When a single-sandbox-node run's raw-output marker carries `delivery_done` (the sentinel was observed in a prior attempt's output — including on cancellation), a transient `SandboxNodeFailedError`/cancellation for that node is NOT re-dispatched: the run completes `complete` with `error_code="harness.idempotency_gate"` instead of burning the retry budget and re-sending the side effect. The gate is inert on multi-node graphs and is disabled by the `MODULO_IDEMPOTENCY_GATE_ENABLED` kill-switch. Runs suppressed by the gate are classified `delivered` (reason `email_delivered`) and surface `gate_fired=true` on the run detail API.

**Recovery actions on failed runs**:
- Retry from failed node (same inputs)
- Retry from start
- Resume with manual input (user provides failed node's output; execution continues)

**Error UX**: named error codes map to user-facing messages and a suggested next action. Raw exceptions are never surfaced in the UI — they may contain credentials or internal paths. Examples:

| Error code | User-facing message | Suggested action |
|---|---|---|
| `credential_expired` | "The connector credential has expired." | Link to connector settings |
| `schema_validation_failure` | "The output did not match the expected format." | Link to agent schema config |
| `connector_error` | "Could not reach the connected service." | Link to connector health check |
| `rate_limit` | "The model provider rate limit was reached." | Retry after cooldown |
| `budget_exceeded` | "This run exceeded its token budget." | Link to agent token_budget config |

A "Copy error details" action in the run detail UI assembles a redacted error report (error code, node ID, timestamp, run ID — no credentials, no raw stack traces) for support or GitHub issues.

**Rendered prompt and DOM masking**: the run inspection UI shows the rendered prompt (interpolated from template with actual input values). Rendered prompts may contain sensitive data if the input included file contents from a `FilesystemConnector` (e.g. a config file with embedded secrets). Rendered prompts are therefore subject to the same server-authenticated reveal rule as structured credential fields (§6.17): the prompt body is displayed as `[Prompt hidden — click to reveal]` by default. On reveal click, the client calls `POST /api/v1/runs/{id}/nodes/{node_id}/prompt/reveal` (returns the decrypted prompt body; 30-second TTL on the injected DOM value; then re-masked). This is per-node — revealing one node's prompt does not reveal others. Pipeline authors who want prompts to be visible by default can set `prompt_always_visible: true` on the agent config; this documents their intent that the prompt contains no sensitive data and disables the masking. Connector credential values (API keys, tokens) embedded in prompts are always masked regardless of this flag — detected by the server before returning the reveal response.

**Run inspection UI**: each run's detail view exposes a per-node expandable section showing:
- Input payload received by the node
- Prompt sent to the model (rendered from template with interpolated values — not the raw template)
- Raw model response
- Output payload after schema validation
- Eval results (pass/fail, score, detail) if any evals are configured
- Error detail (named error code + message) if the node failed

Sensitive connector payloads (file contents, API responses) follow the DOM sensitive data rule (§6.17) — masked by default, server-authenticated 30-second reveal. A "Copy as test fixture" action exports the node's input and expected output as a `StubModelBackend` fixture JSON, enabling local reproduction of the exact run state for debugging.

**Agent output and the DOM sensitive data rule**: §6.17 covers structured credential fields (API keys, connector secrets, webhook secrets). Agent-generated output is not automatically masked — it may legitimately contain long text. However, if a pipeline is configured to read sensitive files (e.g. env files, config with embedded secrets), those file contents will appear in the agent's output payload and therefore in the run inspection UI. This is a known gap: Modulo cannot distinguish "output containing credentials" from "output containing normal text." **Required operational guidance** (deployment docs and UI warning on FilesystemConnector): do not point FilesystemConnector at paths containing credentials. Agent output masking is out of scope for v1; it is a v2 data classification feature.

**Vue Flow canvas serialisation**: the pipeline canvas uses Vue Flow. The Vue Flow internal node/edge graph is the source of truth during editing. On save, the ViewModel serialises the canvas state to the Modulo pipeline API format: each Vue Flow node maps to `{agent_id, position: {x, y}, connector_binding}` (position stored for visual layout reproduction); each Vue Flow edge maps to the `PipelineEdge` entity (`{source_node_id, target_node_id, edge_type, hitl_gate_config}`). On load, the API response is the authoritative source — Vue Flow is hydrated from the API, not from localStorage or any client-side store. This ensures the canvas always reflects the server state after a page reload.

### 8.10 Cost Controls

| Control | Scope | Behaviour |
|---|---|---|
| `token_budget` | Per agent | Hard stop → `budget_exceeded` |
| `run_budget` | Per pipeline run | Hard stop |
| `daily_spend_limit` | Per trigger | Pauses trigger for day; admin notified |
| `circuit_breaker` | Per pipeline | Permanently pauses trigger until admin re-enables |

Currency: configurable per organisation (default: USD). `cost_tracking: disabled` per ModelBackend for open-weight / self-hosted models.

#### Token Counting Mechanism
Token usage is captured via the LangGraph→OTel callback handler (§5.6). The `on_llm_end(response: LLMResult)` callback fires after every LLM call and provides `response.llm_output.get('usage')` (OpenAI format) or `response.llm_output.get('token_usage')` (LangChain legacy). The **Cost Controller** is a component that subscribes to this callback alongside the OTel bridge:

1. On `on_llm_end`: extract `prompt_tokens` + `completion_tokens` from usage metadata. Look up the model's per-token cost from `config/model_pricing.yaml` using the `model_id` stored in `PipelineSnapshot.model_backend_pins_json`. Compute incremental cost.
2. Accumulate `total_tokens` and `total_cost_usd` in the run record (incremental DB update — not at run end).
3. After each accumulation: check per-agent `token_budget`. If exceeded → the run transitions to `budget_exceeded` terminal state with error message "This run exceeded its token budget." (enforced in the cost controller's finalization path — the run's accumulated tokens across all of an agent's nodes are summed against the agent's `token_budget`).
4. After each accumulation: check per-run `run_budget`. Same abort path.
5. After each accumulation: check per-trigger `daily_spend_limit`. If exceeded → mark trigger as `daily_limit_reached` (no new runs today); notify admin; this run continues (the limit applies to future runs, not the in-flight run that pushed it over).

For `cost_tracking: disabled` ModelBackends: `on_llm_end` still fires but usage metadata may be absent or unreliable. Token accumulation is skipped; `total_cost_usd` remains null. `token_budget` checking is still applied if the model returns usage data; if usage data is absent, the budget cannot be enforced (documented limitation for self-hosted models).

Pricing tables: local `config/model_pricing.yaml`. Shipped with defaults. User-maintained. UI displays last-updated date.

**Org and team-level admin limits (v1)**: admins may set operational caps independently of SaaS plan limits. These apply to self-hosted and SaaS equally — they are internal controls, not billing controls.

| Limit | Scope | Behaviour |
|---|---|---|
| `org_daily_run_limit` | Org | New runs blocked for remainder of UTC day; admin notified |
| `org_daily_spend_limit` | Org | New runs blocked; admin notified |
| `team_daily_run_limit` | Per team | New runs from team-owned pipelines blocked; team operators notified |

Without team-level limits, a high-volume team running LLM pipelines can exhaust org quota without any governance. These limits ensure admins can operate shared deployments safely.

**Limit enforcement atomicity**: `org_daily_run_limit` and `team_daily_run_limit` are checked and incremented atomically within the run creation transaction using `SELECT ... FOR UPDATE` on an `org_daily_run_counts` table (keyed by `org_id` + UTC date). This prevents two simultaneous run-creation requests both passing the check and exceeding the limit. `org_daily_spend_limit` is checked pre-run against the accumulated spend for the UTC day (from usage events) — read from a materialised counter, updated by the cost tracking callback (§7.10). This check is not transactionally atomic (spend accumulates asynchronously), but over-spend by a single run is accepted as the operational cost of non-blocking execution. Both limits are stored in org `settings_json`. The `Cost Controller` component (§5.6) is the single enforcement point for all spend/run limit checks — they are never checked in multiple places.

#### Sandbox Agent Runtime Cost

`sandbox_agent` (E2B) nodes report a `cost_estimate_usd` on the run, computed from the node's wall-clock time plus the agent's self-reported model estimate:

```
cost_estimate_usd = (wall_clock_seconds / 3600 × E2B_SANDBOX_USD_PER_HOUR)
                    + agent_reported cost_estimate_usd (when present in the agent's output contract)
```

`E2B_SANDBOX_USD_PER_HOUR` (default `0.13` USD/hr) is operator-configurable and reflects the hourly E2B sandbox rate; the default is based on the opencode template = 2 vCPU / 2 GiB at E2B per-second rates (~$0.1332/hr rounded to 0.13), and 0.5 was a conservative placeholder. E2B bills per-second sandbox uptime, so wall-clock time is a faithful cost basis. The agent's own `cost_estimate_usd` field (written to `/home/user/output.json` by dogfood agents) is merged in when present and numeric. The estimate is attached to both the node's artifact output and the run's `output`, and `Run.total_cost_usd` aggregates token cost + sandbox cost across all completed nodes (`execute` and HITL `resume` paths equally).

**Attributability boundary**: GitHub Actions billing is unavailable on the free tier and is not tracked. Fly costs are infrastructure-level (the platform hosting Modulo itself) and are NOT attributed per-run. Only E2B sandbox usage is per-run attributable; sandbox cost is an estimate, not an invoice.

#### Multi-Component Cost Tracking

A run's cost is a sum of named **cost components**, each with a rate/value, a
formula, a source (`calculated` vs `self_reported`), and a declared set of run
parameters it may reference. Components are org-scoped and admin-configurable;
`Run.total_cost_usd` stays the summed total, and the run detail shows a
**breakdown** of component snapshots rather than a single number. Breakdown
values are denormalized onto the run at finalization, so deleting a component
never affects historical runs.

- **Calculated components** use a safe, fixed-grammar formula (four operators:
  `+ - * /`, no arbitrary code) over a whitelist of run parameters (token
  counts, wall-clock hours, rates). Formulas are validated at save time; an
  invalid formula is rejected with a 422.
- **Self-reported components** capture the model cost an agent writes to its
  output (`model_cost_usd`). A self-reported value is validated and clamped at
  the backend extraction boundary before it can affect the run total, and a
  node's model cost is never counted twice — a reporting sandbox node's
  constant-rate estimate is replaced, not added.
- The daily spend ledger is a **report, not a source of truth**: per-key ledger
  strictness is deliberately not guaranteed and the ledger may drift for the
  documented classes (refused, deferred, pre-deploy residue). Enforcement keeps
  today's behaviour; the report keys by run-start day.

The normative reference for this feature is
`docs/design/multi-component-cost-tracking.md`; the operator guide is at
`docs/operations/` (see the config reference for the tunable knobs). This
section states user-facing behaviour only.

### 8.11 Notifications

Outbound webhook on: `hitl_awaiting`, `run_failed`, `budget_exceeded`, `circuit_breaker_tripped`, `claim_expired`, `hitl_overdue`.

All payloads HMAC-SHA256 signed (`X-Modulo-Signature` header). Multiple endpoints configurable per org. V1: native Slack.

**Notification delivery and retry**: outbound webhooks are delivered with at-least-once semantics. On 4xx (except 429) or network error, the delivery is retried up to 3 times with exponential backoff (1s, 5s, 30s). On 429, retry after the `Retry-After` header value (capped at 60s). On 5xx, retry 3 times with the same backoff. After all retries exhausted, the delivery is logged as `failed` in a `notification_delivery_log` table (`id`, `event_type`, `endpoint_id`, `run_id`, `attempt_count`, `last_error`, `failed_at`). Failed deliveries for `hitl_awaiting` events trigger an in-app alert to org admins (surfaced in the header notification bell) — a HITL review may be blocked without notification delivery. Admins can manually retry failed deliveries from the notification delivery log. After 5 consecutive delivery failures to the same endpoint within 24 hours, the endpoint is automatically disabled and the admin is alerted. The notification dispatcher runs in the FastAPI process (async task) with SAQ cron isolation for retry durability.

**Team notification endpoints (v1)**: the Team entity carries `notification_endpoints` (same structure as org-level webhook config). When a HITL gate fires `hitl_awaiting` and the gate has `required_team_id`, the notification dispatches to the team's configured endpoints (if any) and falls back to org-wide endpoints if the team has none configured. This ensures the right group of people is notified rather than spamming the whole org for team-specific review work.

### 8.12 Audit Trail

Immutable `AuditEvent` records. Written in alpha; viewer UI in v1.

| Event | Data |
|---|---|
| `run_started` | pipeline_id, snapshot_id, trigger_type, user_id, input hash |
| `hitl_claimed` | run_id, gate_id, user_id |
| `hitl_approved` | run_id, gate_id, user_id |
| `hitl_rejected` | run_id, gate_id, user_id, reason, reject_target |
| `hitl_output_delivered` (v1) | run_id, gate_id, user_id, output hash |
| `pipeline_changed` | pipeline_id, user_id, change summary |
| `agent_prompt_changed` | agent_id, user_id, old_version, new_version |
| `user_permission_changed` | target_user_id, changed_by, old_role, new_role |
| `connector_credentials_updated` | connector_id, user_id |
| `model_backend_credentials_updated` | backend_id, user_id |
| `schema_version_deprecated` | schema_id, version, user_id |
| `api_key_created` | key_id (not raw key), user_id |
| `api_key_revoked` | key_id, revoked_by |
| `auth_event` | type (login/logout/failed), user_id, ip |
| `team_created` | team_id, team_name, created_by |
| `team_renamed` | team_id, old_name, new_name, changed_by |
| `team_deleted` | team_id, team_name, deleted_by |
| `team_member_added` | team_id, user_id, team_role, added_by |
| `team_member_removed` | team_id, user_id, removed_by |
| `team_member_role_changed` | team_id, user_id, old_role, new_role, changed_by |
| `resource_team_ownership_changed` | resource_type, resource_id, old_team_id, new_team_id, changed_by |
| `team_membership_revoked` | team_id, user_id, revoked_by (immediate session revocation path) |

Append-only at application layer. V2: cryptographic chaining for tamper evidence.

### 8.13 Pipeline Versioning

#### PipelineSnapshot Entity
`id`, `organisation_id`, `pipeline_id`, `snapshot_version` (monotonically increasing integer per pipeline), `created_at`, `created_by` (user_id or null for trigger-initiated runs), `graph_json` (serialised pipeline graph: nodes with agent_id + connector_binding, edges with edge_type + hitl_gate_config — this is the immutable definition), `connector_bindings_json` (list of `{node_id, connector_type, instance_id, instance_name}` — snapshot of binding at run-start, with human-readable name for historical display even if instance is later renamed), `schema_pins_json` (list of `{schema_id, version, abstract_name}` — pinned versions for all referenced schemas), `prompt_pins_json` (list of `{agent_id, prompt_version_hash, prompt_version_at}` — the specific prompt version in use at run-start), `model_backend_pins_json` (list of `{agent_id, model_backend_id, model_id}` — model backend and model ID at run-start; model pricing may change after snapshot, but cost is calculated from `model_pricing.yaml` at run-time using the pinned `model_id`).

- PipelineSnapshot taken at run-start; includes: pipeline definition, all ConnectorBindings, all schema version pins, all prompt version pins
- Run executes against snapshot — live pipeline changes don't affect in-progress runs
- UI warns if pipeline edited while a run is `awaiting_human`
- Snapshot stored by reference (schemas by ID+version; deletion protection is the integrity guarantee)

**Team ownership changes and active runs**: changing a pipeline's `owner_team_id` is blocked while any run is in a non-terminal state (`pending`, `running`, `awaiting_human`, `claimed`; the `waiting_for_lock` sub-state was excised in migrations 0074/0075). The ViewModel returns `pipeline_has_active_runs`. After all runs complete, ownership change is permitted. The UI then warns: "Existing snapshots reference connectors from the previous team. Re-save the pipeline to rebind connectors for the new team." Old snapshots remain valid for historical run records but should not be used to start new runs after rebinding.

### 8.14 Community Library

All platform primitives are first-class library citizens: **schemas**, **workflow bundles**, **agents**, **integrations** (connector type packages).

#### Library Primitive Data Model
Local and community primitives share a single `library_primitives` table with a `source` discriminator:

| Field | Description |
|---|---|
| `id` | UUID |
| `organisation_id` | Org-scoped |
| `source` | `local` — created within the org; `registry` — cached read-only copy from community registry |
| `primitive_type` | `schema` \| `workflow` \| `agent` \| `integration` |
| `name`, `slug`, `description`, `author`, `version`, `tags` | Standard metadata |
| `owner_team_id` | Nullable FK — `local` only; null for `registry` entries |
| `visibility` | `org` \| `team` — `local` only; always `org` for `registry` entries |
| `forked_from` | Nullable — ID of the `registry` entry this was copied from (read-only, immutable) |
| `checksum`, `ed25519_signature` | Registry primitives only; null for local |
| `verified` | Boolean; `registry` entries only; based on publisher tier |
| `download_count`, `average_rating`, `review_count` | Community-facing metrics; null for local entries |
| `content_json` | The primitive's full definition — type-discriminated JSON body (see below) |

**`content_json` by `primitive_type`**:
- `schema`: the full JSON Schema definition object (same structure as `schemas.definition_json`)
- `agent`: `{name, description, model_backend_id, prompt_template, input_schema_id, output_schema_id, connector_bindings, token_budget, retry_policy}` — a self-contained agent config. `model_backend_id` is null for registry entries (bound at copy-to-adapt time)
- `workflow`: the full YAML bundle (as JSON — same structure as the workflow export format in §7.15), including nodes and edges; connector abstract types listed in `requires.connector_types` are left unbound until the CopyToAdaptWizard binding step
- `integration`: `{pip_package, version, capabilities, config_schema}` — the installable package reference; no runtime content

**Copying a registry primitive** (copy-to-adapt): creates a new row with `source: local`, `forked_from: <registry_entry_id>`. The local copy carries an `auto_update` flag (default `TRUE`) and a `version_group_id` linking it to the upstream version family. When the upstream primitive publishes a new version, the copy's `update_available_version_id` is set to signal an available update. The copy owner can apply the update (replacing `content_json` with the new version) or set `auto_update = FALSE` to opt out and diverge independently.

**Rating and trust tiers** apply to `source: registry` entries only. Local primitives are not rated.

#### Library Metadata (summary)
- `name`, `slug`, `description`, `author`, `version`, `tags`
- `download_count`, `average_rating` (1–5, weighted), `review_count`
- `verified` badge (v2 registry)
- `source_url`, `checksum`, `ed25519_signature` (registry primitives only)
- `forked_from` (read-only provenance metadata)

#### Rating System
- One rating per user per primitive
- Self-rating blocked at application layer
- Rating requires at least one prior copy-to-adapt of the primitive (you must have used it)
- 10-minute submission cooldown per user
- Ratings displayed as weighted average with review count
- Reports for abuse: admin review queue

#### Copy-to-Adapt
One-click copy creates a local editable instance. `forked_from` is immutable metadata. By default, copied primitives check for upstream updates (`auto_update = TRUE`); when an update is available, `update_available_version_id` is set on downstream forks. The owner can apply the update or set `auto_update = FALSE` to diverge freely.

The copy-to-adapt flow includes the **ownership picker** (same rules as resource creation in §7.4): the user selects org-wide or a specific team before the copy is created. There is no silent default: community library primitives (`source: registry`) default the picker to `org`; local library entries (`source: local`) default to the same team as the source, if any. The user can override either default. For users in no team, only `Org-wide` is available. For users in a single team, that team is pre-selected but the picker is still shown (matching §7.4 resource creation rules — no silent defaults).

#### Library Primitive Visibility
Local library entries (schemas, agents, workflows created within the org) carry `owner_team_id` (nullable) and `visibility` (`org` | `team`). A team-private library entry is visible only to members of the owning team and admins. Community registry entries (sourced externally) are always `visibility: org` — they are read-only references and do not carry per-org team scope.

When a user copies a team-private library entry, the ownership picker defaults to the same team as the source.

#### ConnectorType Registration
ConnectorType registration is **in-memory at startup**, not DB-backed. Any installed Python package that exposes a `modulo.connectors` entry-point group is discovered via `importlib.metadata.entry_points(group="modulo.connectors")` and registered into an in-memory `ConnectorTypeRegistry`. No database table exists for connector types — only for `ConnectorInstance` (configured instances of a type).

`ConnectorInstance.connector_type_id` (e.g. `git-host`) is a string stored in DB. At runtime, the type implementation is resolved from the in-memory registry by this string.

**If a registered package is uninstalled**: ConnectorInstances referencing the missing type still exist in DB. Pre-run health check fails with `connector_type_unavailable`. New runs using that type are blocked. Existing completed runs are unaffected (snapshots are immutable). Admin must reinstall the package or migrate affected instances to a different type. The admin UI surfaces `connector_type_unavailable` instances with a warning badge.

**Build-time install only**: see §7.14 plugin installation. Runtime `pip install` is explicitly disallowed — ConnectorType is resolved only from packages present in the Python environment at server startup.

#### Integrations as Library Primitives
Connector type implementations are Python packages (e.g. `modulo-connector-gitlab`). A library integration entry links to the pip-installable package, displays capabilities manifest, rating, and download count. One-click install adds the package to the environment and registers the ConnectorType.

#### Trust Tiers (v2)
Two display tiers for community registry primitives:

| Tier | Indicator | Behaviour |
|---|---|---|
| **Verified publisher** | Green badge | Ed25519-signed by Modulo-vetted key; no warning on copy; install without additional confirmation |
| **Community** | Amber badge | Unsigned or self-signed; warning on copy: "This primitive has not been verified by Modulo. Review the prompt template and schema before use." Requires `confirm: true` in copy-to-adapt flow. |

Verified publisher program (v2 roadmap): application process, key issuance, revocation. Provides incentive for quality third-party contributors. Community primitives are still encouraged — the amber badge communicates caution, not prohibition.

#### Battle-Tested Defaults
Library is the primary onboarding path. On first boot, the UI surfaces recommended primitives for the user's declared tool stack. Users start from library primitives and migrate to bespoke over time.

### 8.15 Shareable Workflow Bundles

#### Export Format
Versioned YAML (parsed with `yaml.safe_load()` exclusively):
```yaml
modulo_workflow:
  id: prd-to-tickets
  name: "PRD to Issue Tracker Tickets"
  version: "1.0.0"
  author: "alice@example.com"
  requires:
    connector_types:
      - git-host
      - issue-tracker
    abstract_schemas:
      - document-input
      - issue-ticket
  agents:
    - id: prd-reader
      prompt_template: ...   # shown to user before first run
      input_schema: document-input
      output_schema: structured-prd
    - id: ticket-writer
      prompt_template: ...
      input_schema: structured-prd
      output_schema: issue-ticket
  edges:
    - source: prd-reader
      target: ticket-writer
      edge_type: normal
      hitl_gate_config: null
    - source: ticket-writer
      target: prd-reader
      edge_type: reject
      hitl_gate_config:
        label: "Review generated tickets"
        description: "Approve if tickets are complete and correctly scoped."
        reject_target: ticket-writer
        claim_expiry_minutes: 60
        human_only: false
        required_team_id: null
  schemas:
    - ...                    # embedded for self-containment
```

**`edges:` block is required** when the pipeline has HITL gates. A bundle exported without it silently drops all gate configuration. Import of a bundle with no `edges:` block creates a linear sequential pipeline (all edges are normal, no gates). The importer emits a warning if the `agents:` block implies a non-linear topology that is not explained by the `edges:` block.

#### Abstract Schema Namespacing
`author/name` in v2 registry. Local use: unnamespaced (collision is user's responsibility).

#### Export Stripping
On export, the following fields are stripped: credentials (all), `owner_team_id`, `visibility`. `owner_team_id` is an org-internal reference and meaningless outside the source org. The importing org's team structure does not map to the exporting org's. `visibility` defaults to `org` on import; the importing user is presented the ownership picker before confirming.

#### Import & Binding
1. Parse with `yaml.safe_load()`
2. Verify Ed25519 signature (registry primitives); warn if unverified
3. Display prompt templates to user for review before confirming import
4. **Ownership picker**: select org-wide or team ownership for the imported pipeline
5. Binding UI: map each ConnectorType to a local instance; map each abstract schema to a local schema version; map each agent's model backend requirement to a local ModelBackend instance (agents in the bundle have `model_backend_id: null`; binding is required before import completes)
6. Capability check: hard block if any bound connector instance lacks a required operation (`connector_capability_mismatch`); warn if model backend's model_id differs from the bundle's declared `preferred_model_id` (informational only)
7. Confirm → pipeline created with bindings resolved and selected ownership applied

Missing ConnectorType instance: creates placeholder connector entry (`status: unconfigured`). Pipeline can be saved but not run until configured.

**Schema conflict resolution**: if a bundle's embedded schema has the same `abstract_name` as an existing local schema AND field structure is identical → reuse existing schema (no import). If field structure differs → import as a new independent schema with a disambiguation suffix (e.g. `document-input-imported-1`) and surface a warning: "A schema named `document-input` already exists with a different structure. The imported version was saved as `document-input-imported-1`. You may consolidate these manually." No auto-merge and no silent version bump — the user makes an intentional consolidation decision.

#### Workflow Updates
No automatic updates. V2 registry: "check for updates" compares local checksum to registry. Manual re-import with re-binding. Local customisations are not merged automatically.

#### V2 Format (YAML)

The v2 bundle format moves from ZIP+JSON to a single YAML file with expanded capabilities. The full specification is documented in ADR 015 (`docs/adr/015-bundle-format-v2.md`).

**What's new in v2:**
- **YAML instead of ZIP+JSON** — single file, human-readable, diffable in PRs
- **Triggers** — bundles ship pre-configured webhook, cron, polling, and agent_signal triggers
- **Team ownership** — `owner_team` field resolved by team name on import
- **Visibility** — `org`, `team`, or `private` (defaults to `org` on import)
- **Lifecycle map reference** — bundles declare which lifecycle stage they participate in
- **Composite template refs** — multi-template workflow references
- **Partial bundles** — incomplete pipelines (status: `partial: true`) for assembly
- **Agent runtime config** — `template_id` and `agent_command` travel with agent definitions
- **Ed25519 signature envelope** — optional publisher verification for community library

**Migration path:** v1 ZIP+JSON bundles remain importable. Detection is by file extension (`.zip` → v1, `.yaml`/`.yml` → v2). A conversion script is provided at `docs/ops/bundle-migration-v1-to-v2.md`. v1 import emits a deprecation warning from v2.1 and is removed in v3.0.

**Validation:** v2 bundles are validated against JSON Schema at `docs/schemas/bundle-v2-schema.json`.

### 8.16 Schema Inference (v1)

Schema Inference reduces the primary friction in SDLC onboarding: teams have existing data in their tools (Jira tickets, Linear issues, GitHub PRs, Notion pages) but don't know what Modulo schemas to define because they've never needed to make their data shape explicit.

#### How it works

When a ConnectorInstance is configured and health-checked, an operator can trigger schema inference on a resource type within that connector. Modulo samples recent records (configurable, default: 200), sends them through an LLM analysis step, and produces a draft Modulo schema — field names, types, required/optional, and an inferred `abstract_name` suggestion.

Example: connect to a Jira project → select "Issues" → Modulo samples 200 recent issues → produces a draft schema with fields like `summary` (string, required), `description` (string, optional), `issue_type` (enum: Story/Bug/Task, required), `story_points` (integer, optional), `labels` (string[], optional). Fields that appear in fewer than 10% of sampled records are flagged as rarely-used and excluded from the draft by default.

The draft schema opens in the schema editor for the operator to review, rename fields, adjust types, and publish as a versioned schema. Schema inference produces a starting point — the operator always reviews before publishing.

#### Scope

| Resource | Connector | Notes |
|---|---|---|
| Issues / tickets | `issue-tracker` (Jira, Linear) | Infers field usage frequency; enum detection for `issue_type`, `status`, `priority` |
| Pull requests | `git-host` (GitHub, GitLab) | Infers PR metadata shape; body treated as `string` |
| Documents | `document-store` (Notion, Confluence) | Page structure inferred; block types collapsed to string fields |

Schema inference is read-only — it never writes to the connected system. The LLM prompt used for inference is sandboxed (`SandboxedEnvironment`) and the sampled records are treated as untrusted input (structural separators, no prompt interpolation of raw field values). Sampled data is not stored after inference completes.

#### SDLC onboarding path

Schema inference is the entry point for "onboard your existing SDLC":
1. Connect tools (Jira, GitHub, Notion)
2. Run schema inference on each resource type → get draft schemas in minutes
3. Review and publish schemas
4. Browse the community library filtered by your inferred `abstract_name` values — see which off-the-shelf agents are compatible with your actual data shape
5. Wire agents together into a pipeline against your real schemas

This path turns an existing SDLC into a running Modulo pipeline in a single session.

### 8.17 Eval System (v1)

Evals are automated quality checks on agent output. They run as a post-node step within the LangGraph StateGraph, after node completion and before any HITL gate check. This ordering is required — conditional HITL gating depends on eval results.

#### Eval Definition

| Field | Description |
|---|---|
| `id` | Unique within agent |
| `name` | Human-readable |
| `type` | `llm_judge` \| `regex` \| `json_schema` \| `custom_function` |
| `config` | Type-specific: rubric (llm_judge), pattern (regex), schema ref (json_schema), function path (custom_function) |
| `pass_threshold` | Numeric 0–1 (applies to llm_judge and custom_function that return a score) |
| `failure_behaviour` | `warn` — log result, run continues \| `block` — run transitions to `eval_failed` terminal state |

#### Eval Types

- **`llm_judge`**: sends agent output to a model with a rubric prompt. Returns score 0–1 and reasoning. The eval prompt must treat agent output as untrusted (structural separators; explicit "evaluate only" instruction — see §6.2). Uses a separate, independently configured `model_backend_id` — not the agent's own backend.
- **`regex`**: applies a regex pattern to the output. Pass = match found. No threshold.
- **`json_schema`**: validates output against a JSON Schema definition. Stricter than the agent's output_schema — for catching well-typed but semantically invalid outputs.
- **`custom_function`**: calls a Python function registered via the `modulo.evals` entry-point group. Returns score 0–1.

#### Eval Result Record

`eval_id`, `run_id`, `node_id`, `score` (nullable for non-scored types), `passed: bool`, `detail: str`, `evaluated_at`

Stored per-run per-node. Displayed in run inspection UI (§7.9). Written to AuditEvent on `block` failures.

#### Conditional HITL Gating

A HITL gate's `condition` field (v1) references an eval: `{eval_id: "quality-check", threshold: 0.8, operator: "lt"}`. If the condition is true (score < 0.8), LangGraph `interrupt()` fires. If false, execution continues without pausing. The Eval Engine runs within the StateGraph node transition — before the conditional interrupt check. This ordering is non-negotiable; HITL gating cannot be conditional without it.

#### Guardrails — the input-side seam (FAR-208)

The eval system above is **output-side** (post-node). Guardrails are the **input-side** analogue: deterministic structured-credential boundary data-safety at the ingestion edge. A guardrail is an `EvalDefinition` with `eval_type="guardrail"` (additive to the eval_type vocabulary at the 5 authoritative sites). Shipped core:

| Field | Description |
|---|---|
| `eval_type` | `guardrail` |
| `config_json.interception_point` | `input` (ingestion edge; T1) |
| `config_json.action` | `observe` \| `warn` \| `block` \| `redact` |
| `config_json.redaction` | static field-path policies `{path, mode: transform\|drop\|block}` |
| `config_json.required_capabilities` | optional conformance claim (empty = no claim) |
| `config_json.max_guardrails_per_node` | per-node guardrail binding cap (default 8, `0` = feature off; FAR-223) |
| `config_json.guardrail_timeout_seconds` | per-guardrail hard detection timeout (default 2.0; FAR-223) |
| `detection` | deterministic pure evals ONLY: `regex` \| `json_schema` |

Behaviour:

- **Detection** is deterministic and pure. A guardrail is a *sibling* of the Eval Engine — routing a guardrail through `EvalEngine.evaluate` raises `GuardrailMisroutedError`.
- **Interception** runs at run-creation (webhook trigger, manual, replay) inside `create_run` **before** `runs.input_payload` is persisted — persisted state is post-redaction. Two-phase pass: evaluate ALL bound guardrails against an immutable pre-act copy, then apply redaction masks in deterministic order. Zero-guardrail fast path.
- **Pre-trigger boundary pass (webhook intake, FAR-214)** — webhook intake additionally runs a guardrail pass at the trigger boundary in `TriggerEngine.handle_webhook`, AFTER the event-type/value filters and BEFORE the dedup insert, so the delivery's ack semantics are decided per guardrail BEFORE any run-creation work:
  - **block** → reject-and-retry at the boundary: the delivery is NOT acked-as-accepted — a `guardrail_blocked` TriggerEvent is recorded, the raw payload is stored for replay (the sender/provider can retry after fixing), and the route returns 400. No run is created and no dedup slot is consumed.
  - **redact** → masks are applied at intake so the payload that proceeds to dedup + run creation is POST-redaction (persisted state is post-redaction, consistent with the T1 contract).
  - **warn/observe** → advisory at the boundary (logged, never a raw payload); the delivery proceeds and the advisory evidence is persisted by the run-creation seam.
  - **Replays** re-run the pass DETECTION-ONLY (no block decision, no redaction act), consistent with `create_run`'s `is_replay=True` handling.
  - The boundary pass reuses the T1 engine (`run_interception_pass`, `to_engine_definition`, `EvalEngine`) and the run-creation seam's row-loading semantics — detection and validation are never reimplemented. Mechanism errors FAIL CLOSED when any bound guardrail carries a block/redact action; observe/warn-only guardrails log-and-continue.
  - **Post-guardrail dedup hashing (FAR-214)** — the dedup key is a canonical SHA-256 over the canonical JSON serialization (sorted keys, compact separators) of the POST-guardrail payload, so logically identical payloads dedup regardless of encoding (key order, whitespace, unicode escapes). This CLOSES the raw-body-hash encoding-bypass residual exposure for the dedup key. Pre-guardrail failure events (timestamp, HMAC, event filters) still record the raw-body hash — those rows describe the raw delivery, not dedup.
- **Redaction** is masks-only (fixed mask token, never payload-derived), static field paths with EXACT/ANCHOR matching (substring matching forbidden), and an allowlist of never-touch system fields.
- **Block** is TERMINAL `eval_failed` (error_code `eval_blocked`); the run is never dispatched, never retried, and has no HITL gate. `failure_behaviour='retry'` is forbidden on guardrails (rejected at the API edge and the DB CHECK).
- **Observe/warn** never block; observe-mode results stamp `eval_results.observed` so the guardrail_summary observed bucket counts once.
- **Remediation** is the dedicated guardrail-override endpoint (`POST /runs/{run_id}/guardrail-override`). The generic recover endpoint REFUSES guardrail-blocked runs (`error_code` `eval_blocked`) — the generic path does not re-run the guardrail pass on the supplied input and would resume execution on the blocked payload. `deliver_manual` is unavailable for terminal runs (no HITL gate → 404). The override re-runs the guardrail pass on the operator-supplied input (re-block safe default; a still-violating input is refused and the run stays terminal), persists the post-redaction payload, flips the run to `pending`, sets `is_replay=True` so lifecycle-map journeys increment exactly once, and re-dispatches from run start (the blocked run never executed, so there is no checkpoint to resume). The
endpoint is rate-limited per (org, actor) to ~10 overrides per 60s window (429 when exceeded) so an operator cannot
hammer the re-dispatch path.
- **Conformance re-validation at node start** (shipped): the T1 three-state conformance derivation is now ENFORCED at NODE START against a LIVE manifest, closing the mid-run carve-out (a connector scope or sandbox/environment policy change between run-creation and a node actually executing is re-validated, not trusted). At each work node's start (`agent`, `connector`, `sandbox_agent`), the bound guardrails with a non-empty `required_capabilities` claim are re-derived against the CURRENT capability surface of the node's bound connectors (live `allowed_operations`/status), the run's bound EnvironmentProfile (`capabilities_json`/status), and the bound agent's `required_environment_capabilities`. Behaviour on change:
  - `absent` or `unknown` on a **`block`-action** guardrail → the node is **blocked and routed to HITL** (`awaiting_human`) with a machine-readable reason (guardrail name, required capabilities, derivation state). NEVER a silent abort, NEVER fail-open. The run is not terminal — a human reviews the conformance gate and may approve (override) or reject; on reject the run fails closed. Audit event `guardrail.conformance_blocked_midrun` is emitted (capability names + state only, never raw payloads).
  - `absent` or `unknown` on **`warn`/`observe`** guardrails → advisory only: log + audit event `guardrail.conformance_warned_midrun`, the node continues. Advisory guardrails NEVER block.
  - `present`, or no bound guardrail carrying a conformance claim → continue on the fast path (no live-manifest round-trip when there is no claim).
  - A live-manifest source that is unreadable (missing/deactivated connector or profile, agent row gone, DB read failure) resolves to `unknown` for its capabilities — a `block` guardrail fails CLOSED (unknown blocks). A failure to LOAD the bound guardrails themselves also fails closed (cannot confirm conformance → block with state `unknown`).
  - The re-check is fail-closed for block-action guardrails ONLY; warn/observe guardrails are advisory and never fail-closed on conformance.
  - This ships the NODE-START re-validation described here. Run-creation-time enforcement wiring (deriving conformance at the ingestion edge, before the run is created) is NOT part of this change — the ingestion edge continues to perform the payload guardrail pass only.

The boundary philosophy: the agent definition is the driver's manual; boundary controls are the enforcement. An orchestrator CAN enforce deterministic, auditable, tamper-resistant controls at the orchestration boundary — validating what enters and leaves a run. It must DELEGATE agent-internal behaviour and unmediated external runtimes, which no boundary control can observe. Guardrails are therefore not agent-internal safety; they are the contract between the orchestrator and the agent it runs.

What guardrails do NOT cover (known failure classes, follow-on work): free-text PII embedded in otherwise-valid field values, content-based detection (semantic, not structural), and output-side effects (already specified in the §8.17 output-side gates above). The interior of a Modulo-hosted agent loop (a long-running `sandbox_agent`'s intermediate tool calls) IS now covered PREVENTIVELY by the agent-loop interior interception bridge (FAR-211, below); compensation of already-performed side effects remains the separate run-termination compensation work (FAR-213).

Canary guardrails are **shipped** (FAR-223 PR C): a known-DETECTABLE canary guardrail (a regex that deterministically matches a fixed marker in a fixed payload) is asserted to fire 100% of the time through the real interception seam as a HARD CI gate — it fails CI if the detection engine ever stops firing on a known marker. A known-WEAK evasion canary (an obfuscated/encoded marker that evades a naive regex) is an INFORMATIONAL BAND ONLY, deliberately excluded from CI failure, documenting the free-text/obfuscation non-coverage above rather than gating on it.

The guardrail management UI is **shipped** (FAR-223 PR D): a settings view (`/settings/guardrails`) that lists bound guardrail eval definitions (name, action, detection type, field, bound pipeline), a point-of-decision creation form (field-name only — existing guardrail values are never inspected or displayed) that declares a new regex/json_schema guardrail bound to a pipeline, an observe badge for observe-mode guardrails (and a kill-switch banner downgrading all guardrails to shadow-only when the org kill-switch is ON), and the run-detail guardrail summary card plus the guardrail-override flow (operator-supplied corrected input, re-block safe) for terminal `eval_blocked` runs. Visibility is org-only in T1.

**Guardrail-binding strip enforcement is service-layer (FAR-309 PR A review)**. A node-bound guardrail (`eval_type='guardrail'` row with non-null `node_id`) binds to its node via the interception seam, so REMOVING that node from the graph would silently drop the binding — an end-run around the admin-only guardrail-management path. This is enforced in the SERVICE LAYER, inside `replace_pipeline_graph` (`db/crud/pipeline.py`) and `rollback_to_snapshot` (`db/crud/pipeline_snapshot_versioning.py`), under the row lock and BEFORE any graph mutation — so EVERY operator-level graph-mutation caller inherits it (the `PATCH .../graph` save, `PATCH /{id}` with `graph_json`, snapshot rollback, convert-to-agent / revert-to-manual, and the MCP `update_pipeline_graph`). A NON-ADMIN stripping a guardrail-bound node is denied: REST routes return 403 (a dedicated `GuardrailBindingStripDenied` exception is translated by each route), and the MCP tool returns `guardrail_strip_forbidden`. An admin (`guardrail.manage`) may remove such nodes. The caller's effective admin flag is re-read under the row lock for REST callers (fail-closed on a stale admin claim), matching the ADR 017 HITL guard's live-role semantics. Only guardrail-bound node REMOVAL is protected — a non-admin's unrelated graph changes (edge rewiring, editing other nodes, removing unbound nodes) are unaffected.

**Elevated guardrail-config read (FAR-309 PR A)**. `GET /api/v1/guardrails/config` (standard, viewer-visible) MASKS the deny-rule internals — regex patterns, JSON schemas, and redaction field paths are replaced with `********` while the guardrail topology (ids, names, actions, detection types) stays visible. `GET /api/v1/guardrails/config/elevated` (admin-only, `guardrail.manage`) returns the FULL unmasked config so an operator can author/audit the actual rules; non-admins get 403.

#### Guardrail summary telemetry on run detail (shipped — FAR-223 PR B)

The per-run `guardrail_summary` telemetry is **shipped** (FAR-223 PR B):

- **`guardrail_summary` on run detail** — `GET /api/v1/runs/{run_id}` returns a `guardrail_summary` object (NULL when no guardrails were bound or on pre-migration runs) with buckets: `bound` (guardrail rows bound at run start, INCLUDING skipped pins), `evaluated`, `passed`, `violated`, `observed`, `errored`, `redacted`, `skipped`, `expected_skips`, `unexpected_skips`. The invariant `evaluated + errored + skipped == bound` holds by construction — `errored` absorbs every bound guardrail that produced no clean detection (mechanism errors, pre-pass blocks). `passed`/`violated` follow GUARDRAIL detection semantics (a regex guardrail's `passed=True` means the pattern MATCHED = a violation), not the raw `passed` column. The summary is a point-in-time snapshot persisted on `runs.guardrail_summary_json` at run creation.
- **Unexpected-skip alert** — a skip NOT explained by a soft-deleted pinned guardrail (reason outside `soft_deleted`) pages `guardrail_unexpected_skip` (Notification Log + Error Forwarders), best-effort fail-open.
- **Per-pattern fired-signature regression** — each clean detection emits a structured log `guardrails.fired_signature` with `{guardrail, pattern_hash, fired}`, keying how often each guardrail pattern fires per run/org over time.
- **`eval_results` consumer filter contract** — guardrail results are stored in `eval_results` alongside normal eval results. EVERY consumer must either explicitly include or exclude them; all normal-eval surfaces (run evals reader, dashboard, admin eval dashboard, OKR, regression, executor suite check, quality report, prompt optimizer) EXCLUDE guardrail rows, since a regex guardrail's `passed=True` (matched) would corrupt a normal pass rate.

#### Guardrail T1 remainder (shipped — FAR-223 PR A)

The engine's T1 residual controls are **shipped** (FAR-223 PR A, backend core):

- **Per-node cap enforcement** — a single pipeline node may bind at most `max_guardrails_per_node` guardrail eval definitions (default 8, `0` = feature off). Enforced at graph-save (reject with 422) AND at the `create_run` interception seam (fail-closed mechanism error). The cap is org-configurable via guardrail config-as-code (`GuardrailConfigSet.max_guardrails_per_node`).
- **Per-guardrail hard timeout + bounded payload** — each guardrail's detection is bounded with `asyncio.wait_for` (default `guardrail_timeout_seconds` 2.0) over a bounded payload budget (default 1 MB). A timeout/over-budget is a mechanism error: fail-closed for block/redact guardrails, log-and-continue for observe/warn. Never a raw payload in any log line.
- **Guardrail latency metric** — total guardrail interception wall-clock is recorded on the run (structured log `guardrails.interception_latency_ms`) BEFORE the first node starts, so it is accounted separately and never silently eats the first node's timeout budget.
- **Org kill-switch (downgrade-to-observe-with-alert)** — `organisations.guardrails_kill_switch` (admin endpoint under `/api/v1/admin/orgs/{org}/guardrails/kill-switch`). When ON, every bound guardrail downgrades to `observe` at run start (shadow-only — never block, never redact); never a full disable. Pinned at run start, never re-read mid-run. Enabling fires an audit event AND a paging Notification (`guardrail_kill_switch`). **Read visibility for non-admins (FAR-223 PR D gap)** — a read-only org-scoped endpoint (`GET /api/v1/org/settings/guardrails/kill-switch`) lets any authenticated member of the caller's own org read the kill-switch state (`{enabled, enabled_at}`), so the guardrails UI kill-switch banner shows for all org members, not just admins; toggling remains admin-only.
- **Snapshot & replay pinning** — `pipeline_snapshots.guardrail_pins_json` pins the guardrail set at snapshot creation. Replays evaluate the PINNED set (the ORIGINAL conditions), never the live rows. A pinned guardrail whose live row is soft-deleted is SKIPPED (never a run failure) with a `guardrail.skipped` audit event + enforcement-gap alert (`guardrail_enforcement_gap`).
- **Conformance enforcement wiring at dispatch time** — a block-action guardrail carrying `required_capabilities` that the org cannot satisfy (confirmed-absent OR unknown) blocks the run fail-closed and fires a paging Notification. observe/warn guardrails are advisory and never fail-closed on conformance.

#### Guardrail trust model PR B (shipped — FAR-309)

The guardrail trust-model follow-ups (FAR-223 item 8) are **shipped** (FAR-309 PR B):

- **Run-start snapshot-integrity re-verify (saved fingerprint)** — `pipeline_snapshots.guardrail_pins_fingerprint` (canonical SHA-256 over the serialized `guardrail_pins_json` captured at snapshot creation) is saved alongside the pin set. At replay run-start the seam re-computes the fingerprint of the LOADED pins and compares — a mismatch (tampered or drifted pin set) FAILS CLOSED as a mechanism error: the run is a terminal `eval_failed`/`eval_blocked`, the tampered pins are never evaluated, and the live rows are never silently used. The mismatch is logged and pages a `guardrail_enforcement_gap` alert. Legacy snapshots WITHOUT a saved fingerprint remain trusted (the fingerprint is optional and verified only when present).
- **Two-step soft-delete with audit** — a guardrail eval definition is SOFT-deleted on `DELETE /api/v1/evals/{eval_id}` (`eval_definitions.deleted_at`/`deleted_by` stamped; the row is retained so snapshot pins referencing it resolve to the skipped-with-audit path, never a dangling row). A second admin step (`DELETE ?purge=true`) hard-removes soft-deleted rows. Every soft-delete and purge writes an org-scoped audit event (`eval_definition.soft_deleted` / `eval_definition.purged`). Live binding (`load_pipeline_guardrail_rows`) excludes soft-deleted rows — a soft-deleted guardrail is never evaluated on new runs. Non-guardrail evals keep their existing hard delete.
- **Break-glass invariant (per-scope + org-global)** — a break-glass account (live or denied) cannot act through any guardrail admin surface: the elevated read (`GET /api/v1/guardrails/config/elevated`), propose/apply/reject, and the eval-delete/purge endpoints all carry `deny_break_glass_mint`. The org-global guardrail kill-switch is also break-glass-immune: a break-glass account cannot read or disable `organisations.guardrails_kill_switch` (`GET`/`PUT /api/v1/admin/orgs/{org}/guardrails/kill-switch` carry the deny marker on top of the `kill_switch_eligible=False` role gate) — break-glass recovery can never lift the guardrail safety controls.

#### Run-termination compensation (shipped — T3, FAR-213)

A guardrail block is TERMINAL `eval_failed`/`eval_blocked`, but nodes that executed BEFORE the block may have already performed external side effects (a pushed PR, a flipped Linear status, a sent notification) that now stand. Run-termination compensation (`modulo.core.guardrails.compensation`) inverts those side effects best-effort at terminalization:

- **Connector compensating-callback contract** — `ConnectorBase.compensate(operation, context, error)` (additive; the default returns `not_supported`, so connectors OPT IN). Given the performed operation (resource, write payload, returned entity) and the termination reason, the connector attempts the inverse and reports `CompensationResult(outcome: compensated|not_supported|failed)`. Shipped implementations: GitHub closes a PR the run opened (`pr`); Linear unassigns (`issue_assign`) and archives a created issue (`issue`). Compensation never changes a connector's forward operation behaviour.
- **Best-effort + failure-isolated orchestration** — `compensate_blocked_run` walks the run's executed node outputs, resolves each node's connector binding from the snapshot graph, and invokes the connector's compensating callback via the ConnectorHub. One node's failure never prevents the others and never crashes terminalization (guard-the-guard). Every attempt writes an audit event (`guardrail.compensation_attempted` / `guardrail.compensation_failed`) with a summary-only payload (node_id, resource, outcome, truncated reason — never raw payloads). Security/safety operations still fail closed; compensation of external side effects is fail-open-WITH-log.
- **blocked_partial run summary** — a guardrail-blocked run stores a structured `blocked_partial_summary` (JSONB column, migration 0111) recording: executed nodes (in order), per-node publish status (`published`/`compensated`/`not-compensated`), output references (`{run_id, node_id}` — the record never duplicates raw payloads; the full outputs live in `outputs_json`), and per-attempt compensation outcomes. Exposed on the run detail API (`GET /api/v1/runs/{id}`, `RunResponse.blocked_partial_summary`). The wiring runs AFTER the terminal status write in both the `create_run` guardrail-blocked seam (at the ingestion edge no nodes have executed, so only the summary + summary audit are written there) and the executor's mid-run guardrail-block terminalization (`eval_failed`/`eval_blocked`, FAR-291). In the executor, `_finalize_run_after_stream` — the shared finalization tail for both `execute()` and `resume()` — invokes `compensate_blocked_run` after the terminal status write with the run's executed node outputs and a fresh connector hub for the compensation window, so a run whose earlier nodes performed connector side effects (a pushed PR, a flipped Linear status) gets those compensated when a later node's output is guardrail-blocked. The compensation is best-effort + failure-isolated (guard-the-guard): a compensation failure never crashes terminalization and never touches the claim-token-fenced `finalize_cost` transaction.
- **Dependent-trigger suppression** — dependent triggers (agent_signal children fired on a source node's completion) are suppressed at fire time when the source run is guardrail-blocked (`TriggerEngine.is_guardrail_blocked_run`: terminal `eval_failed` + `error_code` `eval_blocked`). A suppressed fire is audited (`guardrail.dependent_suppressed`, summary-only) and never creates a child run. This is defense-in-depth: the executor only reaches the agent_signal fire path for completing runs, but the fire-time guard is the durable invariant.
- **Webhook ack-after-validate semantics** — the delivery is fully validated (boundary pass + ingestion pass) BEFORE a success ack. Combined with FAR-214's trigger-boundary pass (a block there is reject-and-retry with a `guardrail_blocked` TriggerEvent and HTTP 400, no run created), a delivery whose RUN is created guardrail-blocked (`eval_failed`/`eval_blocked`, e.g. the manual/MCP path or a run-creation-seam block the boundary pass did not reach) is acked with a non-success HTTP 422 and is never dispatched — the run is created only so the failure is visible in the run list, and the run + TriggerEvent rows stay committed.

#### Agent-loop interior interception (shipped — T3, FAR-211)

The sandbox agent loop is the highest-risk unguarded surface: for `sandbox_agent` nodes, the internal LLM loop (tool results re-injected into the model mid-loop) sits between the ingestion-edge guardrail (T1) and any output gate. The sandbox is Modulo-hosted — it is NOT "unmediated external runtime" — so this ships a Modulo-hosted tool_call-dispatch interception bridge INSIDE the loop:

- **Bridge model** — a Modulo-owned interception bridge runs inside the sandbox alongside the external agent. Each tool invocation is reported to the Modulo side BEFORE execution (`direction="before"`) and each tool result BEFORE it re-enters the model context (`direction="after"`). The sandbox-side bridge client (`modulo.core.guardrails.sandbox_bridge`, stdlib-only, written into the sandbox by the node runner) POSTs `{tool_name, args, direction, result_summary}` to a Modulo-side callback server (`modulo.core.guardrails.loop_intercept.LoopInterceptCallbackServer`) hosted by the Modulo sandbox-agent process; the server evaluates the event against the SAME bound guardrail rows + engine as T1 and returns `{action: pass|block|warn|redact, masked_args}`.
- **Scope (which tool calls are intercepted)** — opt-in per node via `loop_intercept.intercepted_tool_patterns` (glob patterns). Default set: connector-mediated writes (`git push*`, `gh pr create*`, `gh issue create*`, `gh repo create*`, `gh api*`), network egress (`curl*`, `wget*`), and deploy/publish/dep-mutation (`fly deploy*`, `flyctl deploy*`, `docker push*`, `npm publish*`, `pip install*`). Read-only/local-only calls (file reads within the workspace, `git status`) pass through without evaluation cost.
- **Action semantics** — `block` on `before` → the tool call is NOT executed (the bridge client refuses it with a `MODULO_BRIDGE_BLOCKED` marker). `warn` → the call proceeds, the violation recorded. `redact` → the payload is masked BEFORE the tool executes / before the result re-enters the model context (`masked_args` replace the args). `block_on_guardrail: false` downgrades block actions to record-only inside the loop. An `after`-direction block is recorded (never refused) — interception is PREVENTIVE, not compensating (compensation is FAR-213).
- **Per-call latency budget** — each interception round-trip runs under `asyncio.wait_for` with `loop_intercept.latency_budget_ms` (default 250). On timeout or bridge error the call is ALLOWED (best-effort fail-open WITH a log + audit `guardrail.loop_bridge_timeout`) — the interior interception must never wedge the agent loop. Detection keeps the T1 per-guardrail hard timeout.
- **T1 interaction** — the T1 ingestion-edge pass (run-creation) is unchanged and still runs first; the interior interception is ADDITIVE and reuses the same rows/engine/actions.
- **Wiring** — best-effort and fail-open: the bridge activates only when the node carries a `loop_intercept` config AND the pipeline has bound guardrails (zero guardrails → inert); a setup failure disables the bridge for that node but never blocks the dispatch. Graph validation rejects a malformed `loop_intercept` config at save-time.
- **Audit** — org-scoped, summary-only audit events (`guardrail.loop_blocked` / `guardrail.loop_warned` / `guardrail.loop_redacted` / `guardrail.loop_bridge_timeout`) carrying tool name, guardrail name, direction, and reason — NEVER raw args/payloads.
- **Known gap** — preventive only: a tool call that already executed cannot be un-executed (compensation is the FAR-213 run-termination compensation). The opencode plugin hook that emits tool-call events mid-loop is the documented future integration point; the shipped slice is test-driven (tests drive the bridge client with a stub agent command), and the command-wrapper mode + event-marker protocol are shipped and tested.

#### Single-node self-correction (shipped — T2b, FAR-210)

Self-correction is a GENUINELY NEW bounded single-node correction path — explicitly NOT `spawn_correction_run` (the whole-pipeline feedback correction, §8.20). It is a bounded, single-node recovery that rewrites a guardrail-violating node input through a RESTRICTED model backend and re-validates the produced output with a DIFFERENT-FAMILY detector. `modulo.core.guardrails.correction`:

- **Correction definition** — a `correction` config block on a guardrail's `config_json` (parsed by `CorrectionDefinition.from_eval_config`): `id` (slug), `guardrail_id`, `model_backend_id` (RESTRICTED backend — validated to have no guardrail-config/vault access), `input_redaction_patterns` (embedded static+regex patterns, NOT vault-backed), `output_schema` (strict JSON Schema), `revalidation_detector_family` (`regex`|`pii`|`llm_judge` — MUST differ from the fired guardrail's detection family; an `llm_judge` re-validation must use a DIFFERENT backend than the correction backend — never two LLM-judges from the same backend), `revalidation_config` (runtime config for the re-validation detector — the `regex` family REQUIRES a non-empty `pattern` + `field` at definition validation), `max_attempts` (single retry budget), `concurrency_cap` (org-wide).
- **redact+correct HARD-BLOCKED** — a correction definition bound to a `redact`-action guardrail is rejected at definition validation (`RedactCorrectBlockedError`): a correction on a redaction guardrail is an exfiltration channel for the exact data redaction protects.
- **Bounded execution** — `run_single_node_correction` pre-redacts the violating input via the embedded pattern set, runs the RESTRICTED backend with the strict output schema, parses + schema-validates the produced output, and re-validates it with the DIFFERENT-FAMILY detector. It never re-runs the pipeline and never touches connector/vault state. A backend/parse failure is `lm_error` (fail-mode/circuit-breaker); a re-validation failure is `still_violating` (→ fresh retry, see "Retry budget"); `dispatch_single_node_correction` maps a budget-exhausted still-violating outcome to `budget_exhausted` (terminal HITL). The `regex` re-validation family FAILS CLOSED on an empty `pattern`/`field` — a regex revalidator with no pattern is never allowed to mark the correction resolved (no inversion of the engine's missing-pattern failure).
- **Convergence check (strictly-worse ordering enforced)** — prior states are fingerprinted (canonical SHA-256 over sorted-key JSON) AND carry a persisted per-state violation metric (the number of distinct bound guardrails the state violates, plus a detail fingerprint of the violated set). A previously-seen input/output state escalates `converged` to HITL immediately — no oscillation burn. A STRICTLY-WORSE state — the current state's violation severity exceeds every recorded prior state's severity (e.g. a corrected output that now violates more bound guardrails than any previously seen state) — ALSO escalates to HITL immediately, without burning the retry budget (FAR-292). Within a retry loop the corrected INPUT is unchanged across attempts, so retries strip the prior input fingerprints AND input violation metrics (a repeated input is not itself oscillation) while a repeated produced OUTPUT (or repeated output violation metric) still converges. An equal-or-better state allows a fresh attempt. Divergent states that never repeat and never worsen are bounded by the single retry budget (`max_attempts`): exhaustion escalates `budget_exhausted` to terminal HITL, so no correction sequence can run unbounded.
- **Retry budget (MAJOR-6)** — a `still_violating` outcome issues a FRESH LM attempt while attempts remain below `max_attempts` (never a re-validation of the recorded output); only budget exhaustion — or a terminal verdict (`converged` / `correction_violated` / `lm_error`) — escalates to HITL. An interrupted correction re-dispatched with the same idempotency key resumes by RE-VALIDATING the recorded produced output (never re-running the LM), and falls through to a fresh attempt when that recorded output is still violating and attempts remain.
- **Continuing-suspicious + redacted-before-persistence** — a corrected output never auto-clears the suspicious signal downstream; it is redacted with the embedded patterns before it is returned/persisted (to `feedback_records.correction_state` / audit, never raw). A corrected output that itself violates a DIFFERENT bound guardrail escalates `correction_violated` (re-runs T1 guardrail detection over the produced output against all bound guardrails except the corrected one) — never silently accepted.
- **Idempotency + resume** — an idempotency key (org + run + node + correction + redacted-input fingerprint) and persisted partial state (`feedback_records.correction_state`, migration 0112) let an interrupted correction RESUME by re-validating the recorded produced output — never re-running the LM. Budget exhausted mid-resume records `correction_interrupted`.
- **Org-wide concurrent-correction cap at claim-time** — `claim_correction_slot` counts in-flight corrections (feedback records in `correcting` status) inside the caller's transaction alongside the record write, mirroring the sandbox-cap claim-time pattern; a dispatch-time count would TOCTOU. The record currently being corrected is EXCLUDED from the count — it is already in `correcting`, so counting it would make the first correction block itself (MAJOR-3). Over-cap admissions raise `CorrectionCapExceededError` (fail-closed) with a `guardrail.correction_cap_blocked` audit event.
- **Terminal HITL on persistent violation** — every non-`resolved` verdict (still-violating, budget-exhausted, converged, lm-error, interrupted, correction-violated) escalates the FeedbackRecord to HITL (`escalated`, `needs_human_review=True`) with a machine-readable reason; `resolved` transitions it to `resolved`. Both status writes are FENCED on the record still being `correcting` (the correction path's expected pre-state, matching `run_post_correction_eval` / `_escalate_record`): an UPDATE that matches no row means the status changed concurrently — e.g. a human escalated or resolved the record while the correction ran — and raises `ConcurrentModificationError` rather than silently reversing the human's decision. Dispatch/resume is gated on a NON-TERMINAL status: a record already `resolved` / `escalated` / `dismissed` raises `InvalidTransitionError` before any LM work.
- **Reject→correction edge** — a HITL gate's `hitl_gate_config` MAY declare a `correction_target` (accepted + persisted through the graph contract, `HitlGateConfig`). The reject→correction DISPATCH seam IS wired: when a HITL gate with a `correction_target` is REJECTED, `node_runner._hitl_gate` invokes `FeedbackManager.dispatch_reject_correction` — the automated single-node correction path — for the blocked node, while the plain `reject_target` kickback remains for gates WITHOUT a `correction_target`. The dispatch is best-effort and failure-isolated: a correction dispatch failure never crashes the reject path (it logs and falls through to the normal reject routing). T1's `recover_node` override stays the break-glass path. Correction runs (`trigger_type='correction'`) are EXCLUDED from the pipeline `retry_policy` re-dispatch — no chained corrections; the single retry budget is owned by the correction path.
- **Orchestration** — `FeedbackManager.run_single_node_correction` is the sibling path: validates the binding, claims the concurrent slot, records the attempt (idempotency key + prior fingerprints), runs the correction, applies the `correction_violated` guardrail re-check, and persists the redacted output + summary-only audit events (`guardrail.correction_attempted|resolved|escalated|violated|interrupted|cap_blocked`).

#### Guardrail config-as-code (shipped — T3, FAR-219)

Versioned, hashed, PR-gated (git-backed) guardrail configuration is **shipped**. An org's guardrail configuration is expressed as a versioned YAML document (`version` + a list of guardrails, each with a stable slug `id`, `name`, `action: observe|warn|block|redact`, `detection: {type: regex|json_schema, pattern/field} | {type: json_schema, schema}`, static `redaction` paths, and `required_capabilities`). It layers a git-style review workflow on top of the engine:

- **Snapshot pinning** — the org's applied config snapshot is pinned in `organisations.guardrail_pins_json` (applied/proposed content hashes, serialized YAML, timestamps, status). Content hashes are stable SHA-256 digests over a canonical serialization (sorted keys, guardrails sorted by stable id), so equivalent YAML layouts hash identically and the hash is reproducible from the live DB rows.
- **Propose → diff → apply** — `POST /api/v1/guardrails/config/propose` validates + hashes a proposal and computes a per-guardrail add/update/remove diff; `POST /apply` (the approve/merge step, operator+) reconciles the live `eval_type='guardrail'` `EvalDefinition` rows bound to the org's pipelines (upsert present ids, delete removed ids — the engine's interception seam consumes these rows unchanged); `POST /reject` discards the proposal. Propose validates against the engine's guardrail rules (regex|json_schema detection only, no `failure_behaviour='retry'` — guardrail rows are always written with `failure_behaviour='warn'` because block semantics are guardrail-owned).
- **Drift detection** — `GET /api/v1/guardrails/config/drift` recomputes the applied hash from the current DB rows and reports `clean`/`drift` when they diverge from the applied pin (e.g. a row edited outside the config layer). Each state-changing step emits an audit event (`guardrail_config.proposed|applied|rejected|drift_detected`) with summary payloads only — never raw config content.
- Two-step delete + break-glass machinery from the T1 engine remains the emergency path.

#### Alpha Note
The Eval Engine component appears in the §6.1 architecture diagram. Eval System is a **v1 feature** — no eval definitions, no eval UI, and no conditional HITL in alpha. Alpha runs do not execute evals.

#### V1 delivery dependency
The Eval System (§8.17) must ship before the Feedback System (§8.20). The Feedback System's `ai_correction` handler, eval gap detection, and proposed eval curation all require a functioning eval suite. The `human` feedback handler mode operates without evals (it is a routing and inbox feature only) and can ship alongside or before the Eval System. V1 Core delivery order: Eval System → Feedback System (`ai_correction` and gap detection). Human feedback inbox can ship earlier as a standalone feature. Additionally, Run Context Propagation (alpha) is a prerequisite for the Feedback System's correction run mechanics — correction runs inherit `run_context` from the original checkpoint. Run Context must be fully implemented and validated in alpha before the Feedback System's correction run feature can ship in v1.

#### Guardrails — phase history

Guardrails are boundary enforcement for agent safety: the same eval primitives as §8.17 extended to the input side, with interception points, transform actions (redaction), and remediation wiring. T1 (the input-side engine, described above under "Guardrails — the input-side seam (FAR-208)"), the T2 pre-trigger boundary pass at webhook intake (above), and T3 (snapshot-pinned, PR-gated config-as-code) are **shipped**. The original T1 planned-capability list is retained for traceability; every listed item is now shipped, with the caveats noted.

T1 planned capability — all **shipped**:
- **Input-side interception** at run creation — evaluated before the run's first node executes.
- **Actions**: `observe` | `warn` | `block` | `redact` (masks-only; redaction replaces matched field values with a placeholder, never logs the raw value).
- **Deterministic detection** — the same `regex` / `json_schema` primitives as §8.17; no LLM judge on the input side.
- **Conformance** for the `block` action — the three-state derivation ships as a pure helper AND the dispatch-time enforcement wiring is shipped (FAR-223 PR A): a block-action guardrail whose required capabilities are confirmed-absent or unknown blocks fail-closed and pages via the Notification channel.
- **Audited override/recovery** — the guardrail-override endpoint re-runs the pass on operator-supplied input; any bypass is itself an auditable event.

The boundary philosophy and the guardrails' non-coverage (known failure classes) are described in the §8.17 "Guardrails — the input-side seam (FAR-208)" section above.

---

### 8.18 Run Context Propagation (alpha)

#### Concept
Every pipeline run carries a `run_context` dict alongside the artifact payload. Where the artifact is the *data* moving through the pipeline (a PRD, a ticket, a test report), `run_context` is *metadata about the run* — configuration and signals that any agent can read and designated agents can write. The two are kept strictly separate in the LangGraph state to prevent agents from accidentally treating config signals as data artifacts.

```
LangGraph state = {
  "artifact":     {...},   # the data moving through the pipeline
  "run_context":  {...},   # config/metadata propagated through all agents
}
```

#### Seeding
`run_context` is seeded at run-start from two sources, merged in order (later wins):
1. **Pipeline defaults**: key-value pairs defined on the pipeline definition itself (e.g. `model_tier: "small"`). These defaults are captured in **PipelineSnapshot** at run-start — alongside ConnectorBindings and schema pins — so a long-running HITL-paused run always resumes with the context defaults that were active when it started, not the current pipeline defaults.
2. **Trigger override**: the triggering event can pass `run_context_overrides` to override defaults for that run only (used for A/B testing and one-off runs)

#### Reading and writing
- **All agents can read** from `run_context` — it is available in the prompt template and in agent configuration resolution at run time
- **Context-setter agents** (a new agent role, opt-in via `role: context_setter` in agent config) may write to `run_context`. Normal agents are read-only. Enforcement runs as a **pre-node guard** in the `@cancellable_node` decorator: after a non-context-setter node executes, any writes it made to the `run_context` slice of LangGraph state are silently discarded and an `audit_warning` event is emitted (`context_write_by_non_setter`, node_id, attempted keys). The run does not fail — the write is ignored. This makes the guard non-breaking while remaining auditable.

> **Implementation note (2026-07):** The current code raises `ContextSetterViolationError` (a hard error) instead of silently discarding writes. The PRD's non-breaking silent-discard approach is deferred to v1. See `backend/src/modulo/core/pipeline_engine/decorator.py:129-142`.
- Writes follow a **write-log with last-write-wins** resolution: every write to a `run_context` key is appended to an ordered log, and the resolved value is always the most recent write. The full write history is visible in run inspection. When two context-setters both write the same key in parallel branches (fan-out, FAR-171), the write that completes last wins; this is deterministic but order-dependent, so parallel context-setter writes to the same key are flagged as a pipeline validation warning at save time (`PARALLEL_RUN_CONTEXT_WRITE`).

#### Canonical use case: complexity-reviewer
A `complexity-reviewer` is a `context_setter` agent placed early in a pipeline. It reads the incoming artifact and writes into `run_context`:
```json
{ "model_tier": "large", "estimated_tokens": 4200, "complexity_reason": "Multi-system dependency change" }
```
Downstream agents consult `run_context.model_tier` to select their ModelBackend. No branching in the pipeline graph — conditional behaviour lives inside the agent's configuration. The complexity-reviewer is a first-class library primitive in v1.

#### Run context in prompt templates
`run_context` fields are available as template variables alongside artifact fields:
```
{{ artifact.summary }} — estimated complexity: {{ run_context.model_tier }}
```
The same `SandboxedEnvironment` rules apply.

#### Run inspection
`run_context` state is shown in the run detail view at each node: what the context contained when the node started, and what (if anything) it wrote. Context writes are shown as diffs.

---

### 8.19 Run Variants & A/B Testing (v1)

#### Concept
A **variant group** is a named set of runs against the same pipeline that differ only in their `run_context_overrides`. The purpose is controlled comparison: run the same input through the same pipeline twice — once with Sonnet, once with Opus — and compare eval scores.

#### Creating a variant group
From the pipeline detail page, an operator selects "Run as variant" and defines two or more variant configurations:

| Variant | run_context_overrides |
|---|---|
| `sonnet-baseline` | `{model_backend_id: "<sonnet-instance-id>"}` |
| `opus-challenger` | `{model_backend_id: "<opus-instance-id>"}` |

Modulo fires one run per variant (sequentially or in parallel, configurable). All runs in a group share the same input payload and the same pipeline snapshot.

#### Comparison view
The variant comparison UI shows runs side by side:
- Eval scores per node, per variant
- Token cost per run
- HITL outcomes (if any gates were reached)
- Per-node output diff (side-by-side artifact comparison)

The comparison view is the primary surface for the "improve" loop: a DevX engineer runs variants, sees which configuration scores better on their eval suite, and promotes the winner as the pipeline default.

#### Eval coverage signal
If the two variants produce different outputs but identical eval scores, the comparison view surfaces a warning: **"Variants diverged but evals did not differentiate — your eval suite may have a coverage gap."** This is a direct prompt to add a new eval case.

**Variant group with HITL**: if one variant reaches `awaiting_human` while others complete, the comparison view shows partial results for completed variants and a "pending HITL" indicator for the blocked variant. The gate appears in the normal HITL review queue. Approving it completes that variant; the comparison view updates. Operators can cancel a variant run to mark it `abandoned` and exclude it from aggregate scores. A group is complete when all variants reach terminal state.

**Pre-eval degraded mode**: if no evals are configured, the comparison view shows token cost and output diffs only, with a banner prompting eval configuration. Variant groups are still useful for cost comparison without evals.

**Variant group run limits**: firing N variants creates N runs, each counted individually against all org/team/trigger limits. Pre-flight check before firing: if the group would breach any active limit, the entire group is rejected (`variant_group_quota_exceeded`) — no partial firing.

**Prompt version comparison**: variant groups are not limited to model backend differences. An operator can also compare prompt versions by creating variants that differ in `run_context_overrides` containing `{prompt_version: "v3"}` vs `{prompt_version: "v4"}`, with agents configured to read `run_context.prompt_version` to select their prompt template. This requires agents to declare multiple prompt template versions and select by the context key — a pattern documented in the library's "prompt versioning" guide.

---

### 8.20 Feedback System (v1)

The Feedback System treats every human rejection as structured signal — not just a routing event. Its purpose is to close the loop between human judgement and agent improvement, and to grow the eval suite automatically over time.

#### FeedbackRecord entity
Every HITL rejection produces a `FeedbackRecord`:

| Field | Description |
|---|---|
| `id` | UUID |
| `run_id`, `gate_id` | The rejected gate |
| `rejected_by` | User ID |
| `rejection_reason` | Human-authored text (required on reject) |
| `rejected_output` | Snapshot of the artifact payload at the gate |
| `producing_node_id` | The agent node whose output was rejected |
| `producing_agent_id` | The agent definition |
| `feedback_status` | `pending` → `routing` → `correcting` → `resolved` \| `escalated` \| `dismissed` |
| `correction_run_id` | The run ID of the correction attempt, if any |
| `created_at` | |

FeedbackRecords are immutable after creation. The correction loop produces new runs; it does not modify the original run.

#### Feedback routing
Each FeedbackRecord carries a `feedback_handler_type` at creation time, set by the caller (typically the endpoint that creates the feedback record). The three handler types control what happens after creation:

- **`human`** (default if unset): the FeedbackRecord is surfaced in a "Feedback inbox" for a human to review, annotate, and optionally trigger a correction run manually
- **`ai_correction`**: an AI feedback agent analyses the FeedbackRecord (rejection reason + rejected output + original prompt + eval scores) and proposes a corrected output, then spawns a new correction run with the correction in `run_context`. If evals pass, the correction is resolved. If evals fail, the record is escalated.
- **`ai_correction_with_human_review`**: same as above, but the proposed correction is shown to a human before the correction run fires

The `feedback_handler_type` is set per-FeedbackRecord at creation, not cascaded from pipeline or gate config. The Pipeline model carries a `default_feedback_handler` column in the database schema, but runtime code does not yet use it — handler type must be explicitly provided when creating feedback. The `hitl_gate_config` JSON column on pipeline edges accepts freeform configuration; there is no typed `feedback_handler` sub-field on the gate config.

#### AI correction agent
The AI correction agent is a library primitive. It receives:
- The original prompt template (rendered, with interpolated values)
- The rejected output
- The rejection reason
- All eval scores from the rejected run

It produces:
- A diagnosis: what likely went wrong
- A correction proposal: a suggested change to the output (not to the prompt — prompt changes are a human decision)
- Optionally: a proposed new eval case that would have caught this failure

The correction proposal is injected into `run_context` as `feedback_correction`, readable by the target node when it re-runs.

#### Correction run mechanics
A correction run is a new pipeline run (new `run_id`) created by `spawn_correction_run()`. It:
- Uses the same PipelineSnapshot as the original run
- Copies `input_payload` from the original run and injects a `_feedback_correction` block containing the rejection reason, rejected output, producing node ID, and a flag marking it as a correction run
- Links to the original run via `parent_run_id`
- Transitions the FeedbackRecord to `correcting` status

Correction runs are fresh runs — they do not inherit checkpoint state from the original run. The `_feedback_correction` block in `input_payload` is promoted to `run_context` by the executor so target nodes can read the correction proposal during re-execution. If evals fail on the correction run output, the FeedbackRecord is escalated to the human feedback inbox. The correction run itself uses the standard run lifecycle — there is no separate `eval_failed` status on FeedbackRecords.

> **Single-node self-correction is a DIFFERENT path** — the §8.17 guardrail correction (FAR-210) is a bounded single-node rewrite of a guardrail-violating INPUT through a restricted model backend with different-family re-validation. It deliberately does NOT use `spawn_correction_run` (this whole-pipeline path) and does not re-execute the pipeline. See §8.17 "Single-node self-correction".

#### Eval suite growth
Every FeedbackRecord is a signal that the eval suite may have a gap. The system surfaces this explicitly:

1. **Gap detection**: after a rejection, the system retrospectively runs the pipeline's eval suite against the rejected output using a standalone `EvalEngine.evaluate(artifact, eval_suite)` call — an eval execution path that operates outside a live LangGraph run, against a frozen artifact snapshot. If no eval scored the output as failing, the FeedbackRecord is tagged `eval_gap`. This standalone eval path is a first-class interface required by both gap detection and future tooling; it is not a side effect of normal run execution.
2. **Proposed eval**: for `eval_gap` records, the AI correction agent (or a dedicated eval-proposal agent) drafts a new eval case: the rejected output as a negative example, the rejection reason as the rubric, and a suggested eval type (`llm_judge`, `regex`, or `json_schema`).
3. **Human curation**: proposed evals land in the "Eval proposals" inbox. A human reviews, edits if needed, and publishes. Published evals are immediately active for future runs of that pipeline.
4. **Library contribution** (v2): curated evals can be contributed back to the community library, attached to the agent or workflow they test.

Over time, the eval suite grows to cover the failure modes the team has actually encountered. The self-correcting loop — rejection → feedback agent → correction run → eval pass → human approval — becomes faster as the eval suite becomes more complete.

#### Feedback inbox UI
The feedback inbox is a first-class UI surface (v1), parallel to the HITL review queue:
- All pending FeedbackRecords across all pipelines
- Filter by status, pipeline, producing agent
- For `human` handler: annotation UI (add notes, trigger correction run)
- For `ai_correction_with_human_review`: correction proposal display with accept/reject
- Eval proposals queue with draft eval editor

The inbox supports three review actions: **mark reviewed** (sets status to `resolved`), **dismiss** (sets status to `dismissed` — a terminal state indicating the feedback was reviewed but does not warrant a correction), and **create correction run** (spawns a new correction run from the `pending` or `routing` state).

---

### 8.21 View Modes (Team)

The UI can show different subsets of features depending on the user's selected **view**. Every Team-licensed deployment ships with two default views — **Simple** and **Advanced** — that an admin can customise. Admins may also create additional named views and assign them to specific users, teams, or roles.

The entire View Modes feature is **Team-gated** (`view_modes` feature flag). Without a valid Team license key, the feature is entirely absent — every feature is always visible, no toggle, no admin configuration.

#### Default UX: Simple/Advanced Toggle

On first team setup, two views are seeded:
- **Simple** — core navigation (pipelines, stages, runs, library, settings), dashboard summary widgets, basic run/trigger views, HITL review queue, run list, basic agent/schema/connector management. Advanced features (evals, variants, schema inference, feedback inbox, cost breakdown, audit viewer, etc.) are hidden.
- **Advanced** — everything visible.

A toggle in the sidebar footer (adjacent to the theme toggle and tier badge) switches between them. The page re-renders in place via Vue's reactive system — no route change, no reload, no flash, no state mutation. The current selection is persisted in `localStorage`.

**When the toggle is hidden**:
- No team license → no toggle, no view system at all, all features visible
- User is assigned a view other than "Simple" or "Advanced" → the toggle is hidden. The assigned view is applied silently.
- User has an enforced view → the toggle is hidden (choice removed)

**Rules**:
- Switching views does not change any application state, config, or in-progress data
- Components hidden by the current view must not render at all (`v-if`, not `v-show`, not CSS `display:none`) — the DOM subtree is removed
- Form inputs hidden by a view switch are unmounted — they do not participate in form state, validation, or submission

#### Custom Views

Beyond the two defaults, an admin can create additional named views for specific audiences. For example: "Read Only" (viewers only see dashboards and run results), "Manager" (aggregate dashboards only), "On-call" (just HITL queue and run alerts).

When a user is assigned a custom view (i.e. a view whose name is neither "Simple" nor "Advanced"), the toggle is hidden — the user sees exactly that view with no ability to switch. This is intentionally simpler than a multi-option selector: the toggle is for the built-in binary; anything else is enforced by assignment.

Admins can rename "Simple" or "Advanced" too — if an admin renames "Simple" to "Developer" and assigns it to the engineering team, those users lose the toggle (because their view is no longer one of the two defaults). This is correct behaviour: if you customise the defaults, you've opted into the assignment model.

#### Data Model

A **View** is an org-scoped entity:

| Field | Type | Description |
|---|---|---|
| `id` | UUID | |
| `organisation_id` | FK | Org-scoped |
| `name` | string | Human-readable label |
| `slug` | string | URL-safe unique key within org |
| `description` | string | Optional explanation of who this view is for |
| `feature_keys` | string[] | Ordered list of feature keys that are visible. Features not in this list are hidden. |
| `created_at` | timestamptz | |

Views are assigned to users, teams, or org roles via a `view_assignments` table:

| Field | Type | Description |
|---|---|---|
| `id` | UUID | |
| `view_id` | FK → views | |
| `assignee_type` | enum | `user` \| `team` \| `org_role` |
| `assignee_id` | string | User ID, team ID, or role name |
| `enforced` | boolean | When true, the toggle is hidden even for Simple/Advanced |

**Resolution algorithm** (evaluated on every page load, returned in `GET /api/v1/me`):

1. Collect all views the user qualifies for (direct assignment + team memberships + org role)
2. If any qualifying assignment has `enforced: true`, the highest-priority enforced view is returned as active. Toggle is hidden.
3. If no enforced view and exactly one qualifying view exists, that view is active. If it's "Simple" or "Advanced", the toggle appears (both defaults are available — see step 5). If it's a custom view, the toggle is hidden.
4. If no enforced view and multiple qualifying views exist, the user's `localStorage` preference determines active. Falls back to creation order.
5. **Toggle visibility**: the toggle is shown only when the user's available views are exactly "Simple" and "Advanced" (or renamed equivalents — the two seeded defaults). If a user has any other combination of views, the toggle is hidden and the resolved view is silently applied.

**Enforcement priority**: per-user > per-team > per-role. Within the same level, the most recently created assignment wins.

#### Admin Configuration: `/settings/view-modes`

`/settings/view-modes` (admin only, team-gated):

| Section | Content |
|---|---|
| View cards | Card per view showing name, description, assigned count. "Simple" and "Advanced" shown first, then custom views. Default views have a subtle badge. |
| Create view | Name + description form. Slug auto-generated from name. New views start with all features disabled (empty feature set). |
| Edit view | Rename, update description, toggle features on/off. Searchable, categorised feature checklist. |
| Delete view | Confirmation dialog. Blocked if it's the last remaining view. |
| Assignments | Per-view panel: add/remove users, teams, org roles. Each assignment has an "Enforce" toggle. Shows current assignees. |
| Preview | Live sidebar preview showing how navigation would look in the selected view |

**Known feature keys** (exhaustive, registered in a central constant):

```
sidebar.pipelines          sidebar.stages          sidebar.runs
sidebar.library            sidebar.settings        sidebar.evals
sidebar.variants           sidebar.diff_rollback   dashboard.summary
dashboard.eval_trend       dashboard.cost_breakdown
pipeline_editor.basic      pipeline_editor.eval_binding
pipeline_editor.variant_group    pipeline_editor.schema_inference
pipeline_editor.feedback_inbox   pipeline_editor.diff_rollback
settings.license           settings.view_modes     settings.teams
settings.sso               settings.mcp            settings.audit
settings.cost_limits       settings.otel           settings.rate_limits
settings.mcp_oauth         settings.plugins        admin.users
admin.audit_viewer         admin.scim
```

#### Backend API

| Endpoint | Method | Auth | Gate | Description |
|---|---|---|---|---|
| `/api/v1/admin/views` | GET/POST | admin | `view_modes` | List all / create |
| `/api/v1/admin/views/{id}` | GET/PUT/DELETE | admin | `view_modes` | Detail / update / delete |
| `/api/v1/admin/views/{id}/assignments` | GET/POST | admin | `view_modes` | List / create assignment |
| `/api/v1/admin/views/assignments/{id}` | DELETE/PATCH | admin | `view_modes` | Remove / toggle enforcement |

The `GET /api/v1/me` response is extended with:

```json
{
  "active_view": {
    "id": "uuid",
    "name": "Simple",
    "slug": "simple",
    "feature_keys": ["sidebar.pipelines", ...],
    "enforced": false
  },
  "available_views": [
    {"name": "Simple", "slug": "simple"},
    {"name": "Advanced", "slug": "advanced"}
  ],
  "view_toggle_visible": true
}
```

`view_toggle_visible` is a computed boolean on the server: true only when `active_view` is non-null and the user's qualifying views are exactly the two default views (or their renamed equivalents). On free tier, `active_view` is null, `available_views` is empty, `view_toggle_visible` is false.

All endpoints return 402 `Payment Required` when `view_modes` feature is not enabled.

#### Pinia Store

```ts
interface ViewModeState {
  activeView: View | null
  availableViews: ViewSummary[]
  toggleVisible: boolean
  loading: boolean
}

interface View {
  id: string; name: string; slug: string
  description: string; featureKeys: string[]; enforced: boolean
}
```

**Methods**: `fetchViews()` hydrates from `GET /api/v1/me`. `featureVisible(key)` returns true when no active view (free tier) or key is in `activeView.featureKeys`. `switchView(slug)` persists to `localStorage` and updates `activeView`.

#### Conditional Rendering Composable

```ts
function useViewMode(featureKey: string): { visible: ComputedRef<boolean> }
```

Returns `true` when `activeView` is null (free tier — all features visible) or featureKey is in `activeView.featureKeys`. Components use `v-if="visible"` — DOM is removed when hidden.

#### Self-Lockout Prevention

An admin cannot remove their own access to view management:

1. **No self-lockout on assignment**: creating or updating an assignment that would strip the requesting user's access to `settings.view_modes` is rejected with `view_self_lockout`.
2. **Last view with view management cannot be deleted**: DELETE returns 409 `last_view_with_view_management`.
3. **Cannot orphan users**: removing `settings.view_modes` from a view's `feature_keys` is blocked if any user depends on that view as their only source of view management access.

#### No State Mutation on Switch

The view toggle is a **display filter only**. It must never change selection, reset form state, clear store state (beyond mounting/unmounting), trigger API calls beyond initial hydration, or modify server resources.

---

### 8.22 SSE Event Bus (Real-Time Frontend Sync)

The SSE Event Bus provides a push-based mechanism for the frontend to detect backend data changes without polling. When any resource is mutated — by the MCP server, a webhook-triggered run, a background job, or a direct API call — the backend publishes an event that all connected frontend sessions receive in real time.

This is a cross-cutting **platform capability**, not a feature the user directly interacts with. It eliminates the need for per-view polling loops and ensures the UI reflects the current backend state without manual refreshes.

#### Transport

- **Endpoint**: `GET /api/v1/events` — Server-Sent Events (text/event-stream)
- **Auth**: Standard Bearer JWT or API key (same as all other endpoints)
- **Auto-reconnect**: Native `EventSource` browser behaviour — the frontend never implements reconnection logic
- **No Redis requirement**: The default `EventBus` implementation is a purely in-memory `asyncio.Queue`-based pub/sub, identical to the existing `RunEventBroker`. When `settings.redis_url` is configured, a `RedisEventBroker` overlay broadcasts across multiple uvicorn workers (already exists as `core/events/redis_broker.py` — just needs wiring in `_lifespan()`). Single-worker deployments (dev, test, simple self-hosted) use the in-memory backend with zero infrastructure.

#### Event Format

Each SSE message has this shape:

```
event: resource_changed
data: {"type":"run","id":"uuid","action":"updated","version":42,"org_id":"uuid"}
```

| Field | Type | Description |
|---|---|---|
| `type` | string | Resource type: `run`, `pipeline`, `agent`, `schema`, `connector`, `model_backend`, `team`, `trigger`, `eval`, `feedback` |
| `id` | string | Resource UUID |
| `action` | string | `created` | `updated` | `deleted` |
| `version` | int | Monotonically increasing per-org version counter (enables client-side gap detection) |
| `org_id` | string | Organisation UUID — clients filter by their own org server-side |

#### Backend Architecture

**EventBus (in-memory)**:
- Module-level singleton scoped to the FastAPI process
- `publish(resource_type, resource_id, action)` → fan-out to all subscriber `asyncio.Queue` instances
- `subscribe()` → returns queue, used by the SSE endpoint per-connection
- **No persistence** — events are fire-and-forget. A frontend that connects mid-stream receives only subsequent events. The existing REST API is always the authoritative state source.

**Publishing**:
- Via **SQLAlchemy `after_insert` / `after_update` / `after_delete` event listeners** on key ORM models (Run, Pipeline, Agent, Schema, ConnectorInstance, ModelBackend, Team, Trigger, EvalDefinition, FeedbackRecord, LibraryPrimitive)
- Listeners are registered in a central module and fire on any mutation, regardless of origin (REST API, MCP, SAQ worker, CLI script)
- Each listener constructs the event and calls `EventBus.publish()`
- `ProgrammingError` is caught gracefully — if the EventBus table/mechanism doesn't exist yet, the listener is a no-op (safe for migrations)

**SSE Endpoint**:
- FastAPI route at `GET /api/v1/events`
- Authenticates via standard `get_current_user` dependency
- Subscribes to `EventBus`, loops on `queue.get()`, yields SSE-formatted messages
- Filters by `org_id` before sending (each subscriber receives only their own org's events)
- Cleans up the subscription on client disconnect or connection error

#### Frontend Integration

**EventSource Composable** (`useEventStream`):
- Connects to `/api/v1/events` with the existing auth token
- Parses incoming `resource_changed` events
- Dispatches to the correct Pinia store or composable via a lightweight registry

**Conflict Detection (`dirtyIds` Pattern)**:
- Each store maintains a `dirtyIds: Set<string>` of locally-edited entities
- When an SSE event arrives for an entity in `dirtyIds`, the event is **silently dropped** — the user's in-progress edit takes priority
- When the user saves or discards, the entity leaves `dirtyIds`
- When an SSE event arrives for a non-dirty entity, the store re-fetches it via its normal `api.GET()` call (single resource, not a full-page refresh)

**Store Integration**:
- The existing `planStore` and `dashboardStore` subscribe to their relevant event types
- Individual view components that manage their own local state via `ref()` can optionally opt in via `useEventStream({ resourceType: 'run', onEvent: ... })`

#### Testing

**Backend**: pytest with `httpx.AsyncClient` — open an SSE connection, publish an event, verify the stream delivers it. Test auth, org filtering, and cleanup on disconnect.

**Frontend**: Vitest with mocked `EventSource` — verify the composable parses events correctly and dispatches to the right store. Test the `dirtyIds` conflict pattern.

**Integration**: No browser needed. A test can use the MCP server or API to create a resource, open an SSE stream, and verify the event arrives.

#### Delivery Dependencies

- No new dependencies (SSE is built-in; `EventBus` uses `asyncio.Queue`)
- The existing `RedisEventBroker` in `core/events/redis_broker.py` provides the multi-worker overlay — it already exists, just needs `configure_registry()` called in `_lifespan()`
- The existing `RunEventBroker` pattern in `core/pipeline_engine/event_broker.py` serves as the reference implementation

#### Flag / Gating

This feature is **free-tier** (no team gate). Real-time sync is a UX baseline, not a premium feature.

---

### 8.23 Remy — In-App AI Assistant

Remy is a floating AI assistant overlay present on every authenticated page. It is driven by user-provided API keys (Anthropic, OpenAI, or any provider supported by the ModelBackend hub) and can drive every page via the existing ViewModel REST API and MCP tool surface.

Remy is not a separate execution engine — it is a chat UI wrapper around the same MCP server and API that already powers the frontend. What makes it distinct is page awareness (it knows what entities are loaded and what actions are available) and skill loading (Markdown skills are injected into the system prompt, inheriting the same skill format from the agent-cli ecosystem).

#### Panel UX

Remy is a floating overlay panel with these states:

| State | Behaviour |
|---|---|
| **Closed** | Remy icon button fixed at bottom-right of viewport. Unread indicator (message count since last focus). |
| **Open (floating)** | Draggable, resizable window with min/max dimensions. Title bar shows conversation name. |
| **Maximised** | Full viewport overlay. Chat takes full height; all header/sidebar chrome is visible underneath via `pointer-events: none`. |
| **Docked** | Side panel at right edge (400px default, resizable). Main content area shrinks to accommodate. Persisted preference. |

Transitions between states are animated and preserve the scroll position of the conversation.

#### Multi-Window and Session Model

Each browser tab/window maintains an independent Remy session via Pinia store scoped to `sessionId` (generated at mount). Sessions are persisted to the backend on every message:

- **Active session per tab**: Each tab has exactly one active `chat_session` loaded. Switching pages within the tab preserves the session — the Pinia store survives route changes.
- **Last-activity winner**: When a user opens a new tab without an existing session, the API returns the session with the most recent `updated_at`. A user can explicitly start a fresh session from the panel.
- **Session list**: The panel has a sessions drawer showing all sessions for the user, ordered by `updated_at`, with message count and last message preview.
- **No cross-tab sync**: Two tabs with the same session open will diverge. This is accepted — the last save wins. A future v2 could add WebSocket-based session sync.

#### Page Awareness

Remy receives structured page context on every navigation. The frontend `useRemyContext()` composable gathers:

- Current route name and params
- Loaded entity IDs and types (from Pinia stores)
- Available actions on the current page (from view model metadata)
- Current search/filter state

This is injected as a system-level context block:

```
You are on the Pipeline Editor page (/pipelines/{id}/editor).
Loaded: pipeline "Code Review Pipeline" (id: abc-123), 6 nodes, 8 edges.
Available: add_node, remove_node, update_agent, run_pipeline, save.
```

Remy uses this context to answer page-specific questions and execute page-specific actions without the user having to describe where they are.

#### Tool Execution

Remy drives the platform through two channels:

| Channel | When | What it can do |
|---|---|---|
| **ViewModel API** (`/api/v1/viewmodel/current`) | Page-aware actions (same as frontend) | Everything the UI can do — CRUD pipelines, agents, schemas, triggers, connectors, run pipelines, review HITL |
| **MCP Server** (`/mcp`) | Structured tool calls | `list_pipelines`, `trigger_pipeline`, `get_run_status`, `review_hitl`, `search_library`, `copy_library_primitive` |

The LLM decides which channel to use based on the user's request. The ViewModel API is preferred for page-specific actions (it mirrors exactly what the UI would do); MCP is preferred for cross-page or background operations.

#### Skill System

Remy loads skills from two tiers:

| Tier | Managed by | Scope |
|---|---|---|
| **Org-level** | Admin via `/admin/remy` | Visible to all authorised Remy users in the org |
| **User-level** | User via panel settings | Visible only to that user |

Skills are Markdown files with frontmatter (agentskills.io format). They are stored in the `remy_skills` table and injected into the Remy system prompt on session start. The skill loader:

1. Queries all org-level and user-level skills
2. Parses frontmatter for `name`, `description`, `triggers`
3. Concatenates them into the system prompt as a "Available skills" block
4. The LLM uses skill instructions to decide how to handle user requests

This means skills written for Claude Code or Codex (e.g. `deploy`, `qa`, `delivery-status`) can be loaded directly into Remy with minimal adaptation — the skill body describes the behaviour, and Remy's tool channel substitutes for the CLI tools the skill originally referenced.

#### Context Window Management

Remy manages the LLM context window automatically:

1. **Token counting**: Each message is token-counted on save (via `tiktoken` or the provider's tokeniser)
2. **Pruning**: When the reconstructed conversation exceeds the model's context window minus a safety margin (20%), Remy prunes the oldest turns first, keeping the system prompt, page context, and most recent messages
3. **Summarization**: If pruning would remove meaningful history (more than half the messages), Remy writes a one-turn summarization of the pruned conversation into the prompt:

```
[Earlier conversation summary: User asked to create a pipeline for PR review.
Remy created "PR Review Pipeline" with 4 nodes. User ran it and checked results.]
```

The summarization is itself a message token-counted and included in the budget.

#### Data Model

Two new tables:

**`chat_sessions`**
| Column | Type | Notes |
|---|---|---|
| `id` | UUID | PK |
| `organisation_id` | UUID | FK → organisations |
| `user_id` | UUID | FK → users |
| `name` | text | Auto-generated from first user message or user-set |
| `provider` | text | Model provider (e.g. `anthropic`, `openai`) |
| `model` | text | Model name (e.g. `claude-sonnet-4-20250514`) |
| `context_window_tokens` | int | Token limit for the model |
| `system_prompt_hash` | text | SHA-256 of resolved system prompt (skills + guidance + page context format) — used to detect prompt changes that should start a new context |
| `created_at` | timestamptz | |
| `updated_at` | timestamptz | |

**`chat_messages`**
| Column | Type | Notes |
|---|---|---|
| `id` | UUID | PK |
| `session_id` | UUID | FK → chat_sessions |
| `role` | text | `user` | `assistant` | `tool_use` | `tool_result` | `summary` |
| `content` | text | Message body |
| `tool_calls_json` | JSONB | Structured tool call arguments (assistant messages) |
| `tool_results_json` | JSONB | Tool call results (tool_result messages) |
| `token_count` | int | Estimated tokens |
| `parent_id` | UUID | Nullable FK → chat_messages (enables branching / re-roll) |
| `created_at` | timestamptz | |

**`remy_skills`**
| Column | Type | Notes |
|---|---|---|
| `id` | UUID | PK |
| `organisation_id` | UUID | FK → organisations (nullable for user-level) |
| `user_id` | UUID | FK → users (nullable for org-level) |
| `name` | text | Skill name from frontmatter |
| `description` | text | Skill description |
| `triggers` | text[] | Trigger keywords |
| `body` | text | Full Markdown skill content |
| `active` | boolean | Soft disable without deletion |
| `created_at` | timestamptz | |
| `updated_at` | timestamptz | |

Only one of `organisation_id` or `user_id` is set — CHECK constraint enforces this.

#### API Endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/v1/remy/sessions` | List user's sessions (latest first) |
| `POST` | `/api/v1/remy/sessions` | Create new session |
| `GET` | `/api/v1/remy/sessions/{id}` | Get session with messages |
| `DELETE` | `/api/v1/remy/sessions/{id}` | Delete session + messages |
| `PATCH` | `/api/v1/remy/sessions/{id}` | Rename session |
| `POST` | `/api/v1/remy/sessions/{id}/messages` | Append message (user or tool) |
| `POST` | `/api/v1/remy/sessions/{id}/stream` | SSE stream of LLM response |

The stream endpoint is the core of Remy — it receives the user's new message, reconstructs the context window, calls the LLM, executes tool calls via ViewModel/MCP, and streams the assistant response + tool results back as SSE events.

**Admin endpoints** (gated behind `remy_admin` feature):

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/v1/admin/remy/config` | Get org-level Remy config |
| `PUT` | `/api/v1/admin/remy/config` | Update system prompt, additional guidance, access list |
| `GET` | `/api/v1/admin/remy/skills` | List org-level skills |
| `POST` | `/api/v1/admin/remy/skills` | Create org-level skill |
| `PUT` | `/api/v1/admin/remy/skills/{id}` | Update skill |
| `DELETE` | `/api/v1/admin/remy/skills/{id}` | Delete skill |

**User endpoints** (self-service):

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/v1/me/remy/skills` | List user's skills |
| `POST` | `/api/v1/me/remy/skills` | Create user-level skill |
| `PUT` | `/api/v1/me/remy/skills/{id}` | Update skill |
| `DELETE` | `/api/v1/me/remy/skills/{id}` | Delete skill |

#### Feature Gating

Remy is gated at two levels:

1. **Feature flag**: `remy` feature flag — admin must enable it for the organisation (or globally) via the existing PlanContext system
2. **Access list**: Admin configures which users, teams, or org roles can use Remy. Users not on the access list see no Remy button — it is fully hidden, not disabled.
3. **API key prerequisite**: Remy is invisible if the user has not configured at least one LLM API key (stored via the existing ModelBackend mechanism or a dedicated `remy_keys` table). A settings prompt appears on first load guiding the user to add a key.

#### Frontend Architecture

- **`RemyPanel.vue`** — root component mounted in `AppLayout.vue`, always rendered (gated by visibility logic). Contains the trigger button + panel shell.
- **`RemyChat.vue`** — chat message list + input area, stream handling, markdown rendering
- **`RemySessionDrawer.vue`** — sessions list sidebar within the panel
- **`RemySkillManager.vue`** — user skill editor within panel settings
- **`useRemyContext()`** — composable that gathers page context on route change
- **`useRemyStream()`** — composable that manages SSE connection for streaming responses

The panel state (open/closed, position, size, docked/maximised/floating) is persisted to `localStorage`. The active session and message list live in a Pinia store (`useRemyStore`) scoped to `sessionId`.

#### Remy-Only Mode (Full-Screen Ingress)

A second Remy ingress at the full-screen route `/remy` renders the **same** sessions, conversations, skills, and context sources as the floating panel. The only behavioural difference: the **UI-driving tool family is excluded** (`navigate`, `click`, `fill`, `extract`, `get_page_interactables`, `go_back`, `wait`, `press`, `get_manifest` — the whole `_UI_TOOLS` set).

- The route is dev-mode-gated exactly like the other Remy routes: `visibility: private_preview` + the `<<: *community` tier anchor in `manifest.yaml` (visible only when `MODULO_DEV_MODE=true`), and it never appears in the sidebar.
- The client sends `exclude_ui_tools: true` on the `/stream` request. The backend then: (1) omits UI tools from the `tools` parameter, (2) omits the text-mode UI-tools block from the system prompt, (3) answers any `get_manifest` call with a `success=False` tool result ("UI driving is not available in this view"), and (4) rejects any UI tool call with the same message before ever yielding a `ui_command_batch`. MCP tool execution is unaffected.
- Remy-only mode sends no `page_context` — there is no page being driven. On return to the panel, the panel's route-change watcher re-hydrates page context automatically.
- The view is a multi-tab interface: one tab per open session (persisted in `localStorage`), with titles derived from the session list, a live indicator on the streaming tab, close affordance per tab, and the same skills/context-sources/sessions panels as the panel.
- Because the UI-driving tools are gone, remy-only mode renders **no permission UI** at all — MCP tools execute un-gated server-side, so there is nothing to approve.

#### Delivery Dependencies

- No new infrastructure dependencies (PostgreSQL, existing auth, existing SSE)
- The ViewModel API must expose all page actions as callable commands — any gap is a Remy gap
- The MCP server must have tool coverage for cross-page operations
- Models do not require LangGraph — Remy is a stateless LLM call pattern, not a pipeline run

#### Flag / Gating

Remy is a **Team-tier** feature (team-gated via `remy` feature flag). The base access control (which users/teams see Remy) is org-level admin configuration, not a separate license gate. The feature flag controls whether Remy exists in the org at all; the access list controls who sees it.

---

### 8.24 (reserved)

---

### 8.25 Error Tracking (Native)

Modulo ships a **built-in error tracking system** that captures backend and frontend errors, deduplicates them, and surfaces them in an error dashboard — no external service required. External integrations (Sentry, DataDog, PagerDuty, Rollbar, OpsGenie, Grafana Loki) are available as Team-tier forwarders for users who want to pipe errors into their existing monitoring.

#### 8.25.1 Frontend Monitor Backend Abstraction

The frontend error capture pipeline (`createErrorTracker()`) is extended to support **plugable monitor backends** — a `MonitorBackend` interface that routes captured errors to multiple destinations in parallel. This allows self-hosters to choose which client-side SDK to use (or none) without code changes.

**MonitorBackend interface** (shipped in `frontend/src/monitor/`):

```typescript
interface MonitorBackend {
  readonly key: string
  init(config: MonitorConfig): boolean | Promise<boolean>
  captureError(event: ErrorEventInput): void
  captureRawError?(error: Error, context?: Record<string, unknown>): void
  captureMessage(message: string, level: 'error' | 'warning' | 'critical'): void
  addBreadcrumb?(breadcrumb: Breadcrumb): void
  setUser?(user: { id: string; email?: string; name?: string; role?: string } | null): void
  setTags?(tags: Record<string, string>): void
  dispose?(): void
}
```

**Shipped backends** (all optional — activated by env var or runtime config):

| Backend | What it does | SDK | Tier |
|---|---|---|---|
| `builtin` | Existing DB-backed pipeline via `POST /api/v1/errors/ingest` | None | Community |
| `sentry` | Client-side Sentry SDK with session replays | `@sentry/vue` | Team |
| `datadog-rum` | Datadog RUM + Logs SDK | `@datadog/browser-rum` | Team |
| `grafana-faro` | Grafana Faro Web SDK | `@grafana/faro-web-sdk` | Team |

**Configuration** — two-layer:

1. **Build-time** via `VITE_MONITOR_BACKEND` env var (defaults to all four)
2. **Runtime** via `MODULO_MONITOR_CONFIG` container env var (no rebuild to switch)

Runtime config is injected into `index.html` as `window.__MODULO_CONFIG__` and takes precedence over build-time. A self-hoster can switch from `builtin` to `builtin,sentry` by setting:

```yaml
environment:
  MODULO_MONITOR_CONFIG: '{"monitorBackends":["builtin","sentry"],"sentry":{"dsn":"https://xxx@o123.ingest.sentry.io/123"}}'
```

**ErrorTracker refactor:** Vue `errorHandler`/`warnHandler` and window-level `onerror`/`onunhandledrejection` become instance methods that dispatch through a `MonitorBackendRegistry`. Each registered backend receives every event in parallel. A backend that implements `captureRawError` gets the raw `Error` object (richer for source-map resolution); other backends get the serialized `ErrorEventInput`. No double-counting.

**CSP:** Backend `SecurityHeadersMiddleware` sets a broad `connect-src` that includes all known monitoring domains (`*.ingest.sentry.io`, `*.datadoghq.com`, etc.). Self-hosters with custom collector URLs add `MODULO_MONITOR_DOMAINS` env var.

**i18n missing-key capture:** Vue's `missing` handler logs `console.warn`, which the existing `warnHandler` (now routed through the registry) captures as a `captureMessage('warning')` event. Rate-limited per-key (1/60s) and globally (100/min per tracker instance) to prevent DB spam.

**Unauthenticated errors:** The builtin backend detects missing auth (`getAccessToken() === null`) and routes to a new `POST /api/v1/errors/ingest/public` endpoint (rate-limited 1/60s per IP, 100/day, 10KB max body, 48-hour TTL).

**Dependencies:** Third-party SDKs are `optionalDependencies` in `package.json`. The default Docker build installs all; `npm ci --omit=optional` excludes them.

**Privacy:** Each shipped backend includes a JSDoc privacy data sheet (domains contacted, cookies set, data fields collected, config knobs, data residency). Backends without completed data sheets are rejected at review.

**Existing bugfixes (shipped as part of this work):**
1. `source` field in frontend error events changed from `config.appName` to hardcoded `'frontend'` (backend validates this field, previous value caused 422)
2. `session_key` → `key` field name in session-key response interface (backend returns `{"key": ...}`, frontend was reading wrong field)
3. Public ingest endpoint for unauthenticated errors

**Tier gating:** The builtin backend is Community tier. Third-party SDK backends (Sentry, Datadog RUM, Grafana Faro) are Team tier — same as existing external forwarders. The MonitorBackend abstraction itself is Community.

**Why native first**: Users deploying Modulo in air-gapped or VPC-isolated environments should not need a Sentry account to know when their platform crashes. The native system is simpler, self-contained, and always available. External integrations layer on top for users who already have an observability stack.

#### Data Model

Two tables, both immutable append-only (same pattern as `audit_events`):

**`error_events`** — raw error occurrences:
- `id`, `org_id`, `fingerprint` (SHA-256 of dedup key)
- `level`: `error` | `warning` | `critical`
- `message`, `stacktrace`, `context_json` (request URL, user agent, user_id, breadcrumbs)
- `source`: `backend` | `frontend` | `saq` | `celery` (legacy)
- `environment`, `version`
- `status`: `new` | `acknowledged` | `resolved` | `archived`
- `created_at`, `resolved_at`

**`error_groups`** — deduplicated aggregates:
- `id`, `org_id`, `fingerprint`, `status`
- `first_seen`, `last_seen`, `count`
- `level_peak` (highest severity seen)
- `sample_event_id` (FK)
- `assigned_to` (nullable user_id)

#### Ingestion Pipeline

| Source | Mechanism |
|---|---|
| Backend unhandled exceptions | `CatchAllMiddleware` — pipes 500s to `ErrorIngestionService` |
| Backend ERROR+ log records | Custom `LogHandler` forwards structured JSON records |
| SAQ job failures | `saq_hooks` after_process captures exception + context (source `saq`) |
| Frontend JS errors | `window.onerror` + `onunhandledrejection` |
| Frontend Vue errors | `app.config.errorHandler` + `warnHandler` |
| Frontend breadcrumbs | Last 50 user actions: click, navigation, API call, route change |

**Fingerprinting**: SHA-256 of `(message + normalized_stacktrace_top_5_frames + source)`. Groups identical root causes into a single `error_group`.

**Frontend SDK**: `createErrorTracker()` singleton with auto-install of error handlers, breadcrumb collector (last 50 actions), batching (flush every 5s/10 errors), retry with backoff (3 retries), rate limiting (10 req/min per session), HMAC-signed transport.

#### API Endpoints

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/v1/errors/ingest` | Ingest single or batch error events (HMAC-signed) |
| `GET` | `/api/v1/errors` | List error groups with pagination/filtering |
| `GET` | `/api/v1/errors/{id}` | Error group detail + event stream |
| `PATCH` | `/api/v1/errors/{id}` | Update status, assignee |
| `GET` | `/api/v1/errors/{id}/events` | Raw events in this group |

#### Dashboard (`/admin/errors`)

- List view: fingerprint preview, level badge, count, first/last seen, status, assignee. Sortable, filterable (level, status, source, environment, date range, free-text search).
- Detail view: full stacktrace (collapsible, syntax-highlighted), context JSON viewer, breadcrumb trail timeline, raw event stream.
- Actions: acknowledge, resolve, archive, assign to user, add internal note.
- Charts: error rate over time (24h/7d/30d, stacked by level), top 10 errors with trend direction.

#### Built-in Alerting

Configurable per-org notification rules:

| Condition | Actions |
|---|---|
| `level = critical AND count > N in 5 min` | In-app bell badge, email (org admins), webhook (Slack format) |
| `level = error AND count > N in 1 hour` | In-app bell badge |
| `level = warning AND count > N in 24 hours` | In-app bell badge |

Per-rule cooldown (default 5 min) prevents alert fatigue. Max 10 rules per org (Community: 3 max, no webhook).

#### Prometheus Metrics

- `modulo_errors_total{level, source, environment}` — counter
- `modulo_error_groups_active{level}` — gauge
- Exported via existing OTel → Prometheus bridge (no new exporter)

#### External Integrations (Team tier)

| Integration | Mechanism | Existing connector? |
|---|---|---|
| **Sentry** | Forward via existing SentryConnector | ✓ |
| **DataDog** | Forward via existing DatadogConnector | ✓ |
| **PagerDuty** | Trigger Events API v2 via existing PagerDutyConnector | ✓ |
| **Rollbar** | POST `/api/1/item/` | New |
| **OpsGenie** | Alert creation API | New |
| **Grafana Loki** | `POST /loki/api/v1/push` | New |

Each forwarder: per-org enable/disable toggle, test-connection button, status indicator. Forwarder failures logged but do not block local ingest (store-local-first architecture).

#### Feature Gating

| Feature | Tier |
|---|---|
| Error ingestion (backend + frontend) | Community |
| Error dashboard list + detail | Team |
| Basic filters (level, status, date) | Community |
| Acknowledge, resolve | Community |
| Built-in notification rules (max 3, no webhook) | Community |
| Advanced filters (source, environment, search, assignee) | Team |
| CSV export | Team |
| Error rate charts | Team |
| Annotations | Team |
| External forwarders (all 6) | Team |
| Extended retention (>30 days) | Team |
| Rules max 10 + webhook action | Team |

> **Page-level gate**: the Error Dashboard page (`/admin/errors`) is gated as a
> whole behind the Team-tier `error_tracking` feature flag (FeatureGate +
> `require_feature("error_tracking")` on the API). Error ingestion always runs
> on Community tier; only the dashboard UI is Team-gated. Finer-grained
> per-capability gating within the page (basic filters, acknowledge/resolve)
> is a future refinement.

---

### 8.26 Navigation Restructure (Frontend UX)

The frontend navigation system was built piecemeal — 56 routes, 44 sidebar links across 7 flat groups, zero breadcrumbs, and no hierarchy or sub-navigation within page domains. This section specifies the restructured navigation to give users clear orientation and contextual movement between pages.

#### 8.26.1 Breadcrumb Component

A `Breadcrumb.vue` component renders a horizontal trail at the top of every page:

```
Dashboard > Admin > Cost Management > Spend Limits
```

Each breadcrumb segment is a `<router-link>` to the parent page, except the current page (plain text). The trail is built from route `meta`:

- `meta.breadcrumb`: display label for the current page
- `meta.parent`: route name of the parent page for breadcrumb chain resolution

The `Breadcrumb.vue` component walks the `parent` chain up to root, rendering each segment. Route definitions are updated with these two fields for all 50 non-auth routes.

#### 8.26.2 Sidebar Groups

The sidebar is a set of four flat groups — no subgroups. It is driven by `frontend/src/manifest.yaml` as the single source of truth for routes, group membership, ordering, and tier/permission gating (§8.28). Each route declares `sidebar_group` + `sidebar_order`; detail and editor pages (`type: detail_page` or without a `sidebar_group`) are excluded from the sidebar.

**Sidebar structure:**

```
BUILD (default expanded)
  Dashboard           /
  Analytics           /analytics         (requires analytics.query permission)
  Pipelines           /pipelines
  Library             /library
  Runs                /runs
  Lifecycle Maps      /lifecycle-maps

MONITOR (default expanded)
  Output Diff         /runs/diff
  Evals               /evals/editor
  Eval Proposals      /evals/proposals
  Variants            /variants/compare
  AB Test Models      /variants/ab-test
  Browser Monitoring  /settings/monitoring
  Error Dashboard     /admin/errors
  Notification Log    /admin/notification-delivery

CONFIGURE (default collapsed)
  Schemas             /schemas
  Parameter Schemas   /admin/parameter-schemas
  Model Backends      /admin/model-backends
  MCP                 /settings/mcp
  Triggers            /settings/triggers
  Connectors          /admin/connectors
  Runtime Config      /settings/runtime-config
  Rate Limits         /settings/rate-limits
  Environment Profiles /environment-profiles

ADMIN (default collapsed)
  Remy Config         /admin/remy          (private_preview — dev mode only)
  Remy Skills         /settings/remy       (private_preview — dev mode only)
  Teams               /settings/teams
  SSO                 /settings/sso
  License             /settings/license
  Users               /admin/users
  Org Settings        /admin/org
  Audit Log           /admin/audit
  Costs               /admin/costs
  Node Categories     /admin/node-categories
  Feature Flags       /admin/feature-flags
  Environments        /admin/environments
  Housekeeping        /admin/housekeeping
  Run Retention       /admin/run-retention
  Saved Views         /admin/views
  Sandbox Concurrency /admin/sandbox-concurrency
  HITL Review         /settings/hitl-review
  Observability       /settings/observability
  Error Forwarders    /settings/error-forwarders
  Email               /settings/email
  Plugins             /admin/plugins
  Feedback Inbox      /feedback/inbox
```

Key structural points:
- Four flat groups — BUILD, MONITOR, CONFIGURE, ADMIN — with no subgroups (`SidebarSubgroup.vue` removed). BUILD and MONITOR default expanded; CONFIGURE and ADMIN default collapsed.
- Remy sits under ADMIN: Remy Config (`/admin/remy`) and Remy Skills (`/settings/remy`), both `private_preview` (dev mode only).
- Settings/configure/admin pages are distributed across groups per the manifest (MCP, Triggers, Runtime Config, Rate Limits, Connectors, Model Backends under CONFIGURE; Teams, SSO, License, HITL Review, Observability, Error Forwarders, Email under ADMIN).
- Detail and editor pages (e.g. `/runs/:id`, `/admin/errors/:id`, `/lifecycle-maps/:id`, schema editor/infer) are not sidebar items.
- The sidebar is driven by `frontend/src/manifest.yaml` as the single source of truth (§8.28): group membership, ordering, tiers, permissions, and dev-mode visibility all come from the manifest.

#### 8.26.3 Secondary Navigation (Page Tabs)

For page domains with 2–5 sibling routes, horizontal tab bars provide in-page context switching:

| Domain | Routes | Tabs |
|---|---|---|
| Cost Management | single sidebar entry `/admin/costs`; tabs route to `/admin/costs/limits`, `/admin/costs/components`, `/admin/costs/controls` | Overview \| Spend Limits \| Cost Components \| Cost Controls |
| Errors | `/admin/errors`, `/admin/errors/:id` | Dashboard \| *(Error Detail — not in tabs, has back link)* |
| Schemas | `/schemas`, `/schemas/editor/:id?`, `/schemas/infer` | Browse \| Editor \| Infer |
| Evaluation | `/evals/editor`, `/evals/proposals`, `/variants/compare`, `/variants/ab-test` | Evals \| Proposals \| Variants \| AB Test |

A reusable `PageTabs.vue` component accepts a `tabs` array of `{ label, to }` highlighting the active tab via route matching.

#### 8.26.4 Back-to-Parent Links

Detail and editor pages not in the sidebar get a prominent back link above the page title:

| View | Link | Target |
|---|---|---|
| PipelineEditorView | `← Back to Library` | `/library` |
| LibraryPipelineWizard | `← Back to Library` | `/library` |
| CompositeEditorView | `← Back to Library` | `/library` |
| RunDetailView | `← Back to Dashboard` | `/` |
| AdminErrorDetailView | `← Back to Error Dashboard` | `/admin/errors` |

The back link uses a left-arrow chevron icon and is rendered via a shared `BackLink.vue` component or inline `<router-link>`.

#### 8.26.5 Feature Gating

| Feature | Tier |
|---|---|
| All navigation components (Breadcrumb, PageTabs, BackLink, SidebarSubgroup) | Community |
| Sidebar restructure (new groups, subgroups, Remy consolidation) | Community |

Navigation is a UI concern, not a feature gate. All changes apply to every tier.

### 8.27 Remy UI Commands (Remy Browser Automation)

Remy can execute UI commands in the user's browser by routing LLM tool calls through the SSE stream to the frontend Vue app. This enables multi-page configuration workflows (navigate → fill → click → verify → go back) without server-side browser automation.

#### 8.27.1 Architecture

```
LLM → tool_calls { navigate, click, fill, extract, ... }
  → Backend SSE stream (agentic loop)
  → event: ui_command_batch
  → Frontend UiCommandExecutor (runs in user's browser)
  → HTTP POST results back
  → Backend feeds results as ToolMessages → next LLM turn
```

The existing SSE stream (`POST /sessions/{id}/stream`) runs multiple LLM turns within a single connection, interleaved with frontend execution pauses. The stream stays alive while the frontend executes commands, yielding `event: ping` every 15s to prevent proxy timeout.

#### 8.27.2 Available Commands

| Command | Description | Default Permission |
|---|---|---|
| `navigate(path)` | Navigate to a Modulo route | auto |
| `click(selector)` | Click an element | auto* |
| `fill(selector, value)` | Type into an input | auto* |
| `select(selector, value)` | Pick a dropdown option | auto* |
| `extract(selector)` | Read visible text from an element | auto |
| `extract_all(selector)` | Read text from all matching elements | auto |
| `get_page_interactables()` | Discover all interactive elements with `data-testid` selectors | auto |
| `wait(ms)` / `wait(selector)` | Pause or wait for an element to appear | auto |
| `go_back()` | Navigate back to previous page | auto |
| `get_url()` | Return current URL and route name | auto |
| `press(key)` | Press a keyboard key | requires approval |

*In `safe` mode (default), write commands are auto-allowed unless the selector matches a destructive keyword pattern (`delete`, `remove`, `destroy`, etc.).

#### 8.27.3 Permission System

Three permission modes configurable in the admin Remy config page:

| Mode | Behaviour |
|---|---|
| `safe` (default) | Read/nav tools auto-allowed. Write tools auto-allowed unless destructive selector detected — forces approval. |
| `full_auto` | All tools auto-allowed. Destructive selectors still prompt for approval. |
| `locked_down` | All write tools require approval. Read/nav tools auto-allowed. |

**Session-level approvals**: Users can approve a tool "for session" — scoped to the current `(tool_name, page_path)` with a 30-minute TTL. Approving `click` on `/admin/pipelines` does not approve `click` on `/admin/users`. Navigate to a different page, and `click` requires re-approval.

**Destructive selector detection**: Substring matching against `delete, remove, destroy, archive, suspend, ban, terminate, revoke, disable, wipe, clear`. Forces approval in any mode.

#### 8.27.4 Selector Strategy

Commands use a `data-testid`-first selector strategy. The `resolveElement()` function tries `[data-testid="..."]` before falling back to raw CSS `querySelector`. The `get_page_interactables()` command lets Remy discover available elements on any page, returning their `data-testid` values for use in subsequent commands.

The codebase already uses `data-testid` extensively across admin views, pipeline editor, and settings pages.

#### 8.27.5 Safety & Visual Feedback

| Mechanism | Description |
|---|---|
| DOM stability wait | `waitForDomStable()` — MutationObserver scoped to `<main>`, waits 200ms of quiet after last mutation, detects loading spinners. Runs automatically after `navigate` and `go_back`. |
| Element highlighting | Before `click`/`fill`/`select`/`press`, the target element gets a 500ms blue outline flash so the user sees what's about to be interacted with. |
| Execution overlay | A semi-transparent banner at the top of the page reads "Remy is performing actions on this page" with a Stop button. |
| Navigation toast | Before each `navigate`/`go_back`, a toast appears: "Navigating to Pipelines…" |
| Stop/abort | User clicks Stop → remaining commands get `cancelled_by_user` → a brief summary is shown (no forced LLM explanation). |
| Multi-tab guard | Commands check `document.visibilityState`. If the tab is hidden, execution pauses up to 60s waiting for the user to return. |
| Extract filtering | `sanitizeExtract()` strips `<script>`, `<style>`, `<input type="hidden">`. Password values are masked. |
| Approval card | Before a gated command executes, an inline card in the chat shows **what** Remy wants to do in plain language: "Click 'Save' button", "Type into Email: 'alice@example.com'". Three buttons: Allow Once, Allow for Session, Deny. |

#### 8.27.6 Agentic Loop (LLM Multi-Turn)

The SSE stream handler runs a `while True` loop:

1. Stream tokens from LLM (may include tool calls)
2. Reconstruct tool calls from accumulated chunks
3. Separate UI tools from MCP tools
4. Execute MCP tools server-side as before
5. For UI tools:
   a. Check permission (config override → mode preset → destructive detection)
   b. If approval needed, yield `permission_request`, await user decision
   c. Yield `ui_command_batch` events to frontend
   d. Await execution results via `asyncio.Event` + `POST /sessions/{id}/ui-command-results`
   e. Store results as `ToolMessage`s in the conversation
6. Feed results into the next LLM call (loop continues)
7. When LLM emits no tool calls, yield `done`

`mcp_api_key` is optional and is required only for MCP calls. If a turn mixes UI and MCP calls without an MCP credential, each MCP call produces a failed `tool_call` result while manifest and browser UI calls continue; all results are fed back to the LLM on the next turn. A missing MCP credential does not abort the SSE stream.

This allows multi-step workflows in a single stream: "Let me check the current config… [extract] → I see X is not set. Let me update it. [navigate → click → fill → click → go_back] → Done!"

#### 8.27.7 Component Support

The `fill` and `select` commands detect and handle common shadcn/vue component types:

| Component | Strategy |
|---|---|
| Native `<input>` / `<textarea>` | Set value via native setter + dispatch `input`/`change` events |
| shadcn `<Select>` | Click trigger → wait for popover → click `[role="option"][data-value="X"]` |
| shadcn `<Combobox>` | Click → type in `<CommandInput>` → select option |
| shadcn `<Switch>` | Click the `[role="switch"]` button |
| `contenteditable` | Set `textContent` + dispatch `input` event |

#### 8.27.8 Non-Tool Models

When the selected LLM provider does not support native tool calling (e.g., local OSS models via Ollama, TGI), UI commands described as text in the system prompt. The LLM can describe what it would do, but cannot emit structured tool calls. The system prompt instructs: "Your provider does not support tool calling. Describe the actions you recommend; the user can guide you. Use the MCP tools you do have for backend operations."

#### 8.27.9 Admin Configuration

A new "Tool Permissions" card on the Remy config page (`/admin/remy`) controls:

- **Permission mode** dropdown: safe | full_auto | locked_down | custom
- **Per-tool override table**: visible when mode is custom. Three options per tool: Auto-allow, Requires Approval, Disabled.
- **Tool descriptions** in plain English, no CSS/jargon.

#### 8.27.10 Frontend Components

| Component/Files | Purpose |
|---|---|
| `useUiCommandExecutor.ts` | Executes all 11 command types in the browser |
| `useRemyStream.ts` | Handles `permission_request`, `ui_command_batch` events |
| `useRemyStore.ts` | Permission state, approval card, `isExecutingUi` lock |
| `RemyChat.vue` | Approval card inline, execution indicator, turn separators |
| `AdminRemyView.vue` | Tool permissions configuration card |
| `RemyPanel.vue` | Reset permissions button |

#### 8.27.11 Backend Endpoints

| Method | Path | Purpose |
|---|---|---|
| POST | `/sessions/{id}/permission-response` | User approves/rejects/approves-for-session a permission request |
| POST | `/sessions/{id}/ui-command-results` | Frontend submits execution results back to the agentic loop |
| POST | `/sessions/{id}/reset-permissions` | Clears all session-level tool approvals |

All endpoints require `Depends(get_current_user)` + `_validate_session_ownership()`.

#### 8.27.12 Feature Gating

| Feature | Tier |
|---|---|
| UI commands (all 11 tools) | Community |
| Tool permissions admin config | Community |
| Permission mode presets | Community |
| Destructive selector detection | Community |

Remy is already a Community-tier feature. The UI commands are an extension of Remy's capability, not a new feature gate.

#### 8.27.13 Open Design Decisions

- **Concurrent batches**: The agentic loop processes one batch of UI commands per LLM turn. If the LLM emits 10 tools, they execute sequentially with a single permission check batch. No concurrent batch support in v1.
- **Multi-worker**: The in-process `asyncio.Event` registry works only with single-worker uvicorn. If horizontal scaling is needed, the registry swaps to Redis pub/sub via the existing `RedisBroker` pattern.
- **Selector discoverability**: The `get_page_interactables()` command requires that elements have `data-testid` attributes. Views without `data-testid` on key interactable elements are invisible to Remy. Tracking issue: add `data-testid` to remaining views as they are encountered.

### 8.28 Core Shared Manifest

A single `frontend/src/manifest.yaml` file serves as the source of truth for page routes, interactive elements, sidebar groups, and their relationships to the product map, i18n, permissions, and feature tiers. Both frontend and backend consume it.

See ADR 008 for the full architecture decision. See `docs/adr/core-manifest-proposal-v2.md` for the complete specification.

#### 8.28.1 File Format

The manifest defines three top-level sections:

- **`routes`** — one entry per route with `name`, `testid`, `breadcrumb`, `parent`, `product_map`, `i18n_key`, `sidebar_group`, `sidebar_order`, `type` (page/list_page/form_page/detail_page), `required_tier`, `required_roles`, `required_permissions`, `pattern` and `dynamic_params` (for dynamic routes), `deprecated`, `page_sections` (for mixed-tier pages).
- **`elements`** — key interactive elements per route with `testid`, `type` (button/select/textarea/table/etc.), `label`, `dynamic_testid` flag. Inclusion criteria: every `<button>` with a `@click` handler, every form element in create/edit views, every table/list with actions.
- **`sidebar_groups`** — group definitions with `label`, `order`, `default_expanded`, `simple_mode`.

#### 8.28.2 Consumption

| Consumer | Mechanism |
|---|---|
| Frontend sidebar nav | `navigation.ts` imports YAML at build time, builds group+item data, applies render-time role/tier filtering, appends runtime items via `getDynamicItemsForGroup()` |
| Frontend breadcrumbs | Global `beforeEach` guard enriches `route.meta` from manifest |
| Frontend feature gating | `<FeatureGate>` props derived from manifest's `required_tier` and `feature_flag` |
| Frontend route guards | `meta.required_roles` and `meta.required_permissions` populated from manifest |
| Backend / Remy | `manifest.py` loads at startup. Remy calls `get_manifest(path?)` to discover page structure before navigating |
| Diagnostics | `GET /api/v1/manifest` returns the full manifest |
| Pre-commit + CI | `validate-manifest.ps1` checks 7 rules (route resolution, testid existence, product map refs, i18n key existence, orphaned elements, circular parents, dynamic route fields) |

#### 8.28.3 Validation Rules

Seven rules enforced by `validate-manifest.ps1`:

| Rule | What it checks |
|---|---|
| 1 | Every `route.name` matches a Vue Router route name |
| 2 | Every static `element.testid` exists in a Vue template (file search, including Vue bindings and template literals; `dynamic_testid: true` entries skipped) |
| 3 | Every `product_map` references an existing file in `docs/product-map/` |
| 4 | Every `i18n_key` exists in `frontend/src/locales/en-US.js` |
| 5 | No orphaned element blocks — every element's parent route exists in `routes` |
| 6 | No circular `parent` chains |
| 7 | Dynamic routes (`type: detail_page`) have required `pattern` and `dynamic_params` fields |

#### 8.28.4 Effect on Remy

Remy gains a new tool: `get_manifest(path?)`. Calling it without arguments returns the full manifest (page structure, elements, sidebar hierarchy). Calling with a specific path returns only that route + its elements. The system prompt instructs: "Before navigating to an unfamiliar page, call `get_manifest()` to learn its structure."

This replaces the trial-and-error pattern (navigate → `get_page_interactables()` → decide) with planned navigation (`get_manifest()` → plan → navigate → interact).

#### 8.28.5 Feature Gating

| Feature | Tier |
|---|---|
| `manifest.yaml` file (creation + validation) | Community |
| `GET /api/v1/manifest` endpoint | Community |
| `get_manifest` Remy tool | Community |
| Sidebar nav migration to manifest | Community |
| Breadcrumb migration to manifest | Community |

The manifest is a developer tooling improvement and Remy capability. No tier gate.

---

### 8.29 Remy Context Sources — Configurable Knowledge Domains

Remy's knowledge of the product is organised into **context sources** — named domains of information that can be independently configured for always-on injection, on-demand retrieval via tool calls, or complete exclusion. This replaces the binary "all skills always injected" model with a graduated system that balances context window budget against Remy's awareness.

#### 8.29.1 Source Modes

Each context source has exactly one mode, configurable at the org level (admin default) and overridable at the user level:

| Mode | Behaviour | Token cost | Example use case |
|---|---|---|---|
| `always_on` | Injected into the system prompt on every session | Per-turn (reconstructed each session) | User profile, current page context |
| `tool` | Exposed as an MCP tool Remy calls on demand | Zero until called | Product docs, FAQ, org config details |
| `off` | Not loaded into system prompt or tool registry | Zero | Unused or deprecated sources |

#### 8.29.2 Default Context Sources

The following sources are pre-defined and seeded on org creation:

| Source key | Contents | Suggested mode | Token estimate |
|---|---|---|---|
| `page_context` | Current route name, params, loaded entity IDs (route.watch → computed → sent in stream request body) | `always_on` | ~100 |
| `user_profile` | User display name, role, org name, plan tier name | `always_on` | ~150 |
| `product_primer` | Auto-generated product overview (§8.30) — key concepts, page navigation, active context summary | `always_on` | ~600–800 |
| `product_docs` | PRD sections, FAQ entries, user-facing docs pages — retrieved via `search_documentation(query)` | `tool` | N/A |
| `integration_status` | Connector health, model backend status, trigger status — via `get_integration_status()` | `tool` | N/A |
| `org_config` | Org settings, feature flags, plan limits, runtime config — via `get_org_config()` | `tool` | N/A |
| `feature_overview` | Feature-to-tier mapping, which features are enabled on the current plan — via `get_available_features()` | `tool` | N/A |

#### 8.29.3 Per-Skill Source Mode

Existing org-level and user-level skills (§8.23.5) gain a `source_mode` field (values: `always_on` | `tool` | `off`, default: `always_on` for backward compatibility).

- Skills with `source_mode = always_on` are injected into the system prompt under `## Organisation Skills` / `## User Skills` (current behaviour).
- Skills with `source_mode = tool` are **not** injected into the prompt. Instead, Remy discovers them via a `get_skill(skill_name)` tool that loads the skill body on demand. The skill's `name` and `description` are listed in a "Skills Available as Tools" section so Remy knows which skills exist and when to call them.
- Skills with `source_mode = off` are excluded entirely — not injected, not listed, no tool.

Migration: existing skills default to `source_mode = always_on`. Admins can bulk-convert large reference skills to `tool` mode via the admin UI.

#### 8.29.4 System Prompt Composition

The system prompt is built in this order:

```
1. [Base admin system prompt]             — from RemyConfig.system_prompt
2. [Additional guidance]                   — from RemyConfig.additional_guidance
3. ## Product Overview                     — always-on primer (§8.30)
4. ## Page Context                         — current page info
5. ## Available Knowledge Tools            — descriptions of tool-mode sources + skills
   - search_documentation — Search product docs and FAQ
   - get_integration_status — Connector and model backend health
   - get_org_config — Org settings and feature flags
   - get_available_features — Feature availability by plan
   - get_skill(name) — Load an organisation or personal skill on demand
6. ## Organisation Skills                  — skills with source_mode = always_on
7. ## User Skills                          — skills with source_mode = always_on
```

The tool descriptions in section 5 are generated dynamically based on which sources and skills are configured as `tool` mode. If a source is `off`, it does not appear. This keeps the prompt tailored to the user's configuration.

#### 8.29.5 MCP Retrieval Tools

Four new MCP tool handlers registered in `mcp_server.py`:

| Tool | Parameters | Returns |
|---|---|---|
| `search_documentation` | `query: str` — free-text search; `section: str \| None` — specific PRD/doc section | Markdown-formatted relevant sections with page titles, up to ~4000 tokens per call |
| `get_integration_status` | None | List of all connectors (name, type, health status, last_ok), model backends (provider, model, configured), trigger counts |
| `get_org_config` | `section: str \| None` — filter to specific config area (e.g. "remy", "rate_limits", "plan") | Org settings relevant to the request, feature flags, plan tier, run limits |
| `get_available_features` | None | Feature-to-tier mapping showing which product features are enabled on the current plan |

**Backend implementation:**

`search_documentation` reads from a documentation index built at startup:
- PRD sections (from `docs/prd.md` — split by `##` / `###` headings, stored with heading path as key)
- FAQ entries (from `frontend/src/faq.yaml` or equivalent)
- User-facing docs pages (from `Development/Website/modulo-website/src/docs/` — indexed at org creation, cached)
- Search: simple case-insensitive keyword match against heading + first paragraph of each section. No vector search in v0.32. Upgraded to embeddings in v1 if retrieval quality demands it.

`get_integration_status` queries live DB:
```sql
SELECT name, connector_type_id, health_status, last_health_check_ok
FROM connector_instances WHERE organisation_id = :org_id
```
plus model backends and trigger counts. Returns a Markdown table.

`get_org_config` reads from `SystemConfig` table for the org, filtered to relevant keys. Never exposes secrets (fernet keys, API keys, database URLs).

`get_available_features` returns the `PlanContext` feature map for the org's current tier.

**Token budget for tool responses:** All retrieval tools return results up to 4000 tokens. Results appear as `ToolMessage`s in the conversation. They are pruned by the context window management (§8.23.7) like any other old message.

#### 8.29.6 Data Model

**`remy_context_sources` table** (new):

| Column | Type | Notes |
|---|---|---|
| `id` | UUID | PK |
| `organisation_id` | UUID | FK → organisations (nullable for built-in defaults) |
| `user_id` | UUID | FK → accounts (nullable for org-level defaults) |
| `source_key` | text | One of the pre-defined keys from §8.29.2 |
| `source_mode` | text | `always_on` \| `tool` \| `off` |
| `created_at` | timestamptz | |
| `updated_at` | timestamptz | |

CHECK constraint: only one of `organisation_id` or `user_id` is set (same pattern as `remy_skills`). Org-level rows set the default for all users; user-level rows override the org default for that individual.

**`remy_skills.source_mode` column** (new, nullable):

| Column | Type | Notes |
|---|---|---|
| `source_mode` | text | `always_on` \| `tool` \| `off`; defaults to `always_on` |

**`RemyConfig.context_sources`** field (new on the existing config model):

```python
class RemyConfig(BaseModel):
    schema_version: int = 3  # bumped from 2
    # ... existing fields ...
    context_sources: dict[str, str] = Field(
        default_factory=lambda: {
            "page_context": "always_on",
            "user_profile": "always_on",
            "product_primer": "always_on",
            "product_docs": "tool",
            "integration_status": "tool",
            "org_config": "tool",
            "feature_overview": "tool",
        }
    )
```

#### 8.29.7 UI: Admin Configuration

The admin Remy page (`/admin/remy`, `AdminRemyView.vue`) gains a **"Knowledge Sources"** section:

```
#### 8.29.7.1 Knowledge Sources

Control what Remy knows and how it loads that knowledge.

Always-on sources are injected into every conversation.
Tool-based sources are available on demand — Remy calls them when needed.
Disabled sources are invisible to Remy.

Source                    Mode          Tokens
───────────────────────────────────────────────────
◉ Product Primer          [always-on]   ~700
◉ Page Context            [always-on]   ~100
◉ User Profile            [always-on]   ~150
○ Product Docs            [tool]        search_documentation()
○ Integration Status      [tool]        get_integration_status()
○ Org Config              [tool]        get_org_config()
○ Feature Overview        [tool]        get_available_features()
```

Each row has a mode dropdown (always_on / tool / off). Org skills listed below with individual source_mode toggles.

**Skill-level overrides:**

```
Skills as Knowledge Sources
Skill                    Current Mode    Override
────────────────────────────────────────────────────
Pipeline Reference Guide  always_on       [tool] ▼
Deployment Checklist      tool            [always_on] ▼
SQL Reference             off             [off] ▼
```

Admins see estimated token count for always-on sources so they can budget context usage.

#### 8.29.8 UI: User Preferences

The Remy panel settings tab gains a **"Knowledge Sources"** subtab (accessible from `RemyPanel.vue`):

```
Knowledge Sources

You can customise what Remy knows about your organisation.
Changes apply to new conversations only.

Source                    Mode
────────────────────────────────
Product Primer            [always-on] ▼    (org default)
Page Context              [always-on] ▼    (org default)
User Profile              [always-on] ▼    (org default)
Product Docs              [tool] ▼         (org default)
...
```

Users can override org defaults for themselves. "Reset to org defaults" button restores admin-configured modes.

#### 8.29.9 Delivery Dependencies

- `remy_context_sources` DB migration must run before the feature deploys
- `remy_skills.source_mode` column migration runs alongside
- `search_documentation` requires the documentation indexer (reads PRD + FAQ + website docs at startup or on first call)
- All four MCP tools must be registered before any source is set to `tool` mode

#### 8.29.10 Flag / Gating

| Component | Tier |
|---|---|
| Context source framework (modes, system prompt filtering) | Community |
| `get_org_config` tool | Community |
| `get_available_features` tool | Community |
| `search_documentation` tool | Team |
| `get_integration_status` tool | Team |
| Per-user source overrides | Team |
| Per-skill source_mode toggles | Team |

The base framework (modes, filtering, prompt composition) is Community so all self-hosters benefit. The retrieval tools that query live org data or documentation indexes are Team-tier to incentivise adoption.

---

### 8.30 Remy Product Primer — Always-On Product Understanding

Remy needs a baseline understanding of what Modulo is and how it works, present in every conversation. The **product primer** is a compact, auto-generated `## Product Overview` section in the system prompt (~600–800 tokens) that covers core concepts, pages, and active context.

#### 8.30.1 Primer Content

The primer is a Markdown block injected before page context:

```
#### 8.30.1.1 Product Overview

### What is Modulo
Governed orchestration for AI-powered SDLC pipelines — automated workflows that
connect tools like GitHub, Linear, Notion, Jira, and Slack. Pipelines are built
from composable AI agents, run with human-in-the-loop gates, evaluated,
and improved continuously.

### Key Concepts
- **Pipeline** — Directed graph of agents (nodes) + edges (data flow, HITL gates)
- **Run** — A single execution of a pipeline with a specific input payload
- **Agent** — An LLM-powered node with a prompt template, schema, and model backend
- **Schema** — Typed JSON structure defining input/output contracts between nodes
- **Connector** — Integration with external tools (GitHub, Linear, Slack, etc.)
- **Trigger** — Event source that starts a pipeline run (webhook, schedule, manual)
- **HITL Gate** — Human-in-the-loop approval point between nodes
- **Eval** — Quality check on agent output (llm_judge, regex, JSON schema, custom)
- **Variant** — A/B test comparing different model backends or prompts on the same input
- **Library** — Reusable primitives: schemas, agents, workflows, pipeline templates
- **Remy** — This AI assistant — embedded in every page, drives the product via tools

### Pages & Navigation
11 sidebar groups: Core (Dashboard, Notifications), Pipelines (Library, Templates,
Stages), Runs & Eval (Output Diff, Evals, Variants, A/B Test), Schemas (Browse,
Editor, Infer), Remy (My Skills, Admin Config), Settings (Teams, SSO, License, MCP,
Triggers, Runtime Config, Rate Limits, HITL Review, Observability, Error Forwarders),
Access Control (Users, Org Settings, Audit Log), Cost Management (Costs — Overview, Spend
Limits, Cost Components, Cost Controls), System (Connectors, Model Backends, Node Categories,
Feature Flags, Environments, Run Retention, Saved Views), Monitoring (Error Dashboard,
Notification Log), Extensions (Plugins, Feedback Inbox).

### Active Context
- **User**: {display_name} ({role}) — {org_name} ({plan_name})
- **Org**: {pipeline_count} pipelines, {connector_count} connectors, {backend_count} model backends
- **Current page**: {route_name} — {page_description}
```

#### 8.30.2 Auto-Generation

The primer is regenerated on a schedule, not handwritten:

```python
# backend/scripts/generate_remy_primer.py
#
# Reads:
#   - docs/prd.md §5 (glossary) — key concepts and definitions
#   - docs/product-map/*.md — feature descriptions and statuses
#   - frontend/src/manifest.yaml — sidebar groups, route names, page descriptions
#   - Live DB counts — pipelines, connectors, model backends
#
# Outputs:
#   - Markdown string written to RemyConfig.product_primer (stored in SystemConfig)
#
# Called:
#   - On deploy (as part of the deploy script)
#   - On housekeeping schedule (nightly)
#   - On demand via `trigger primer regeneration` in admin UI
```

The generator:
1. Reads PRD glossary entries and extracts one-line definitions
2. Reads manifest.yaml sidebar groups and generates the navigation section
3. Queries the live DB for entity counts (pipelines, connectors, model backends)
4. Formats everything into a compact Markdown string
5. Writes it to the org's `RemyConfig.product_primer` field (stored in `SystemConfig`)

**Token budget:** The generator targets 600–800 tokens. If the generated primer exceeds 800 tokens, it truncates from the bottom (navigation section first, then key concepts). The overview section ("What is Modulo") is never truncated.

#### 8.30.3 Admin Override

The admin Remy config page gains a read-only "Product Primer" section showing the current primer text with a "Regenerate" button that calls the generator. Admins can manually edit the primer via the existing system prompt override mechanism if they want custom content.

#### 8.30.4 Delivery Dependencies

- `RemyConfig.product_primer` field added to the config model (part of the context source migration)
- `generate_remy_primer.py` script created and wired into deploy/housekeeping
- The script must handle DB unavailability gracefully (returns the existing primer text)
- The manifest.yaml must exist and be readable (it is loaded at backend startup anyway)

#### 8.30.5 Flag / Gating

| Component | Tier |
|---|---|
| Primer generator script | Community |
| Primer injection in system prompt | Community |
| Admin "Regenerate" button | Community |
| Manual primer editing via admin UI | Team |

The product primer is a core usability improvement for all Remy users. No tier gate on the feature itself.

---

## 9. User Management & Access Control

### 9.1 User Model
`id`, `email`, `display_name`, `org_role`, `auth_provider` (`local` | `oidc` | `saml`), `sso_subject`, `active`, `organisation_id`

### 9.2 Roles

Roles apply at two scopes: **org-level** (baseline for all resources) and **team-level** (override for that team's resources). Team roles are a **subset** of org roles — a team role can only be `operator`, `runner`, or `viewer`; `admin` is org-only.

| Role | Capabilities |
|---|---|
| `admin` | Full access to all resources in the org. Bypasses all team restrictions. Manages users, teams, connectors, model backends, secrets, system config, audit log. |
| `operator` | Create/edit/run pipelines. Approve HITL. Import/export workflows. Cannot manage users or system config. |
| `runner` | Trigger runs on existing accessible pipelines. Claim HITL gates on accessible pipelines (approval requires `operator` or `admin`). Cannot create/edit pipelines or connectors. |
| `viewer` | Read-only. View accessible pipelines, runs, traces. Cannot trigger or approve. |

`admin` is org-only — it cannot be a team role. Team roles are `operator`, `runner`, `viewer`.

### 9.3 Team-Based Access Control

An Organisation can contain multiple **Teams**, each owning a set of pipelines, stages, connectors, and model backends. This allows independent groups (e.g. SDLC team, docs team, data team) to operate their own workflows within a single org without sharing access to one another's resources.

#### Team Entity
`id`, `name`, `description`, `organisation_id`, `created_by`, `created_at`, `notification_endpoints` (list, same structure as org-level webhook config)

#### TeamMembership
`team_id`, `user_id`, `team_role` (`operator` | `runner` | `viewer`), `added_by`, `added_at`

A user may belong to multiple teams with different roles in each.

**Team membership management**: `admin` can manage any team. A team `operator` can add or remove members from their own team only, and can only grant roles up to their own team role (a team `operator` cannot grant another member `operator` if the granting user is only a `runner`). This prevents privilege escalation within team scope. V1: team membership requires email-based invitation acceptance before access is granted.

#### Team Deletion Policy
Team deletion is blocked (`team_has_resources` error) if any resource has `owner_team_id` pointing to the team being deleted. Admin must first reassign or delete all team-owned resources. Reassignment can be done in bulk ("Reassign all to org-wide") before confirming deletion. Team deletion with no owned resources succeeds immediately and is a soft delete (`deleted_at` set; the row is retained for history/analytics). A soft-deleted team is hidden from all lists and lookups, and its name can be reused (name uniqueness applies only to non-deleted teams). `team_deleted` is written to AuditEvent.

#### Effective Access Model

For any resource, a user's effective access is determined by:

1. **Admin**: org `admin` always has full access to every resource. No team restrictions apply.
2. **Org-visibility resources**: pipelines, stages, connectors, and model backends with `visibility: org` are accessible to all org members at their **org role**.
3. **Team-visibility resources**: resources with `visibility: team` are visible only to members of the owning team (plus org admins). A team member's access level = their **team role** in that team.
4. **Multiple team memberships**: a user in two teams has independent access to each team's resources under their respective team roles. Roles from one team do not bleed into another team's resources.
5. **Org role does not override team visibility**: an org-level `operator` cannot see or act on `team`-visibility resources unless they are a member of the owning team (or an admin). This is intentional — team visibility is a privacy boundary, not just a presentation filter.

> **Phase-1 enforcement floor (ADR 017 DECISION 2)**: team-*role* levels described above are **deferred to Phase 3**. As of Phase 1 the REST team gate enforces the RLS-parity formula — `visibility='org' OR owner_team_id IS NULL OR membership (any team role) OR org_role='admin'` — composed with the org-role floor on the same endpoint. Any team membership row qualifies regardless of the stored team role; org `admin` bypasses the team gate entirely; rows with `visibility: org` or `owner_team_id = null` are governed by the org-role floor only. `runs` are org-role-floor-only (they carry `owner_team_id` but no `visibility` column, so a team gate would regress org members who are not on the owning team).

#### Resource Ownership

Each of the following entities carries `owner_team_id` (nullable) and `visibility` (`org` | `team`):
- Pipeline
- Stage
- ConnectorInstance
- ModelBackend

`owner_team_id = null` with `visibility: org` = accessible to all org members (legacy / unowned resources). Admin may reassign ownership.

**Ownership / visibility transitions re-validate the team gate** (Phase-1 enforcement floor). Changing a resource's `visibility` or `owner_team_id` applies the RLS-parity membership-or-admin gate against the NEW values before the write is accepted:
- Downgrading a team-private resource to `visibility: org`, or reassigning/clearing `owner_team_id`, requires membership of the CURRENT owner team (or org `admin`).
- Reassigning `owner_team_id` to a team requires membership of the NEW team (or org `admin`).
- Clearing `owner_team_id` while keeping `visibility: team` is rejected (a team-visible resource must have an owner team).
- Org `admin` bypasses all team-transition gates (RLS parity).

#### Team-Scoped Connectors and Model Backends

A connector instance or model backend with `visibility: team` is only usable within pipelines owned by the same team. Connector bindings at pipeline-save time enforce this: binding a team-private connector to a pipeline owned by a different team is blocked at the ViewModel command layer with a named error (`connector_team_mismatch`).

#### Team-Scoped HITL Gates

A HITL gate may specify `required_team_id`. When set:
- Only members of that team (with `runner` or `operator` team role) can claim the gate
- The MCP `review_hitl` tool enforces this: a token not scoped to a team member returns 403 with `required_team:{team_id}`
- The gate context resource (`modulo://runs/{id}/hitl/{gate_id}`) exposes `required_team_id` and `required_team_name` so LLM clients and human reviewers know which team must approve

This enables the pattern: docs team owns a docs pipeline; all HITL gates on that pipeline require a docs team member to approve; SDLC team cannot see or interact with those gates.

#### Single Owner Limitation
Each resource has exactly one `owner_team_id`. Multiple team ACLs per resource are not supported in this model. Shared infrastructure (e.g. a platform connector used by multiple teams) should use `visibility: org` and rely on org-role RBAC for access control. This is a deliberate simplification — per-resource multi-team ACLs are a v3+ concern if the need is demonstrated. Document this limitation in user-facing docs so teams design their ownership structure intentionally.

#### Team-Scoped API Keys

API keys (§5.2) carry an optional `team_id`. A team-scoped API key is restricted to resources accessible to that team under the role embedded in the key. An org-wide API key (no `team_id`) respects org-level role.

#### Team Management UI (v1)
`/settings/teams` — admin and team operators. Surfaces:
- Team list (name, member count, owned resource count)
- Create team / rename team / delete team (blocked if resources owned)
- Member management: invite by email, set team role, remove member
- Bulk "Reassign all resources to org-wide" action (admin only, required before deletion)
- Team notification endpoint config

User profile panel: "My Teams" — list of teams the user belongs to and their role in each.

#### SSO + JIT Provisioning with Teams

V1 SSO (OIDC/SAML) supports group-to-team mapping via claims. On JIT user provisioning, group membership in the identity provider maps to Modulo team membership. Admins configure the mapping: `{idp_group: "docs-team", modulo_team: "<team_id>", team_role: "operator"}`.

### 9.4 Authentication

| Stage | Mechanism |
|---|---|
| Alpha | HTTP Basic Auth. Default `admin:password`. Overridable via `MODULO_ADMIN_PASSWORD`. Startup warning if default unchanged. Single implicit admin user — team management not active in alpha. **Session lifecycle**: the browser holds no session cookie in alpha — HTTP Basic Auth is stateless; the browser credential prompt is the only "logout" mechanism (closing the tab or clearing browser credentials). A "Sign out" UI action in alpha clears `localStorage` (stored preferences) and redirects to the login page, which re-triggers the Basic Auth challenge. There is no server-side session to invalidate in alpha — that changes in v1 with JWT sessions and token family invalidation. Inactivity timeout: not enforced in alpha (stateless auth). |
| v1 | Local user table (bcrypt), JWT sessions, OIDC, SAML 2.0. JIT provisioning. Teams + team membership. Group-to-team mapping for SSO. API keys (org-wide and team-scoped). |
| v2 | SCIM provisioning with team sync |

**JWT**: 15-min access tokens, 7-day refresh tokens (rotated, family invalidation on theft). WebSocket auth via upgrade-header connection token. JWT payload carries `org_role` and `team_memberships: [{team_id, team_role}]` — the ViewModel resolves effective access from these claims without a DB round-trip on every request.

**JWT stale team membership**: team membership changes (add, remove, role change) take effect at the user's next token refresh — up to 15 minutes after the change. This is a known and documented gap, acceptable for routine membership management. For immediate revocation (departing employee, security incident), admins use the **session revocation** action, which invalidates all active tokens for that user via token family invalidation (§6.10). This forces the next request to fail auth and require re-login with current membership. Document this in the admin UI alongside the "Remove from team" action. **Exception**: `required_team_id` HITL gate enforcement always performs a DB-live membership check — the JWT claims are not trusted for this security-critical path.

**Password change**: Logged-in users can change their own password via `PUT /api/v1/me/password`. The endpoint requires the current password for authorisation, validates the new password against the strength policy, and bcrypt-hashes it before storing. On success, all active JWT token families for the user are blacklisted, forcing re-login with the new password. This prevents a hijacked session from changing the password without the user's knowledge (the attacker would need the current password) and ensures that a compromised password's tokens are immediately invalidated on change.

The My Profile page (`/admin/my-profile`) provides the frontend UI for password change with client-side validation (min length, match confirmation) and error display. Users without a local password (SSO/OIDC/SAML provisioned) cannot use this endpoint.

**Break-glass recovery** — an organisation whose only admin cannot authenticate (forgotten password, SCIM deactivation/demotion of the last admin, role demotion, lost secrets) is recovered via an operator-synthesised, single-use credential consumed through this same login flow. See §7.19. Use-time revalidation TRIMs to outbound webhook dispatch: when the notifier dispatches an event, any webhook endpoint owned by a break-glass account (live or denied) is skipped — fail-closed, per-endpoint — so a webhook created before the deny was active, or forged via a raw DB write, never delivers.

---

## 10. Extensibility and Distribution

### 10.2 Plugin / Extension API
Python entry-point groups (stable public API, own semver):
- `modulo.connectors` — ConnectorType implementations
- `modulo.evals` — custom eval functions
- `modulo.model_backends` — additional model backends
- `modulo.schema_types` — custom schema field types

Must be documented and stable before v1 public release.

**Installation mechanism — v1**: build-time only. Plugins are added to the deployment's `pyproject.toml` (or `requirements.txt`) and the container is rebuilt. Modulo does not shell out to `pip install` at runtime — Docker containers are read-only by default, and runtime pip installs are a supply-chain security risk. The Modulo UI generates the correct `pip install <package>` command and restart instruction for the admin to execute. The library integration entry (§7.14) links to the pip package and shows the install command; "one-click install" means "copy this command" — not silent in-process installation.

**Installation mechanism — v3 (SaaS)**: a dedicated plugin volume approach is required for SaaS (hosted environment cannot rebuild the container per-org). Design deferred to v3. The entry-point API is designed to support both approaches without code changes in the plugin itself.

### 10.3 First-Run Experience
~~On first boot with empty database: pre-loaded demo pipeline + guided walkthrough. Walkable end-to-end in under 5 minutes via the demo mode env var (no external API keys required). Library surfaces recommended primitives post-walkthrough. Required in alpha — an empty state prevents effective adoption.~~

**Removed/descoped (FAR-308).** The pre-loaded demo pipeline and the demo mode env var were removed and are no longer part of the product. The guided first-run walkthrough is no longer a required alpha feature.

### 10.3a Alpha Documentation (Internal, required before alpha is shared)
- `docs/dev-setup.md` — local development environment: prerequisites, env var reference, how to run migrations, how to seed demo data, how to run tests
- `docs/architecture.md` — component overview, data flow, key design decisions, LangGraph integration notes
- `CONTRIBUTING.md` — how to run the test suite, lint rules, pre-commit setup, PR process
These are lightweight but essential for multi-person alpha development. Without them, alpha is only runnable by its author.

### 10.3b Alpha Exit Criteria
Alpha is done when all six conditions are met:
1. ~~Demo pipeline (`prd-to-requirements`) walkable by 3 non-authors without assistance, using the demo mode env var~~ — **removed/descoped (FAR-308)**; no longer an alpha exit requirement
2. All happy-path BDD scenarios green in CI
3. At least one non-demo pipeline built by an internal user and run to completion
4. HITL approve and reject demonstrated by two different named users. Alpha Basic Auth supports multiple users via `MODULO_USERS` env var (format: `username:bcrypt_hash,username2:bcrypt_hash2`); the single `MODULO_ADMIN_PASSWORD` shorthand creates one user named `admin`. This criterion requires at least two entries in `MODULO_USERS` performing the claim and review separately.
5. Connector swap demonstrated: same pipeline run successfully against both `FilesystemConnector` and `GitHubConnector` by rebinding
6. Run Context demonstrated: at least one pipeline uses a context-setter agent (e.g. complexity-reviewer) whose output visibly changes the behaviour of a downstream agent, verified in run inspection

When all six are met, the decision to move to v1 is made explicitly. Alpha does not become v1 by default.

### 10.4 Documentation (Required at Public Launch)
- Quickstart (`docker compose up` → running demo in 10 min)
- Deployment guide (TLS, SECRET_KEY, Postgres, env var reference)
- Connector authoring guide
- Model backend authoring guide
- Schema definition reference
- REST API reference (auto-generated from FastAPI OpenAPI)
- Architecture overview for contributors

### 10.5 Opt-In Telemetry

Telemetry is opt-in and disabled by default. The OTel bridge (`setup_otel`) is configured via `MODULO_TELEMETRY_ENABLED=true` (default: `false`). When enabled, spans are exported to stdout and optionally to an OTLP endpoint if `OTEL_EXPORTER_OTLP_ENDPOINT` is set. No telemetry data leaves the process without explicit operator configuration.

**Anonymous startup ping (deferred):** The original spec envisioned an anonymous startup ping reporting connector type IDs, model backend provider IDs, pipeline count, run count in last 7 days, Modulo version, Python version, and OS type — with no content, no credentials, no user data, and a publicly published payload schema. This ping is **not implemented** and is documented here as a future consideration. The current `MODULO_TELEMETRY_ENABLED` flag controls OTel exporter configuration only.

---

## 11. Tech Stack

### Backend
| Layer | Technology | Notes |
|---|---|---|
| Language | Python 3.12+ | |
| Agent orchestration | LangGraph (pinned exact version) | Upgrades = migration events |
| Observability | OpenTelemetry SDK | Custom LangGraph→OTel callback handler (explicit build work) |
| Connector adapters | LangChain (adapters only) | Not used for core logic |
| API server | FastAPI | Async, OpenAPI, WebSocket |
| Domain DB | SQLAlchemy + Alembic + Postgres RLS | `SET LOCAL` for org context; lint rule enforced |
| Run state DB | LangGraph **Async**PostgresSaver / **Async**SqliteSaver | `asyncpg` driver (Postgres) or `aiosqlite` (SQLite). Sync savers not permitted — they block the event loop. Separate Postgres schema `langgraph.*`; org_id prefix on thread IDs (alpha); subclass for full isolation (v2). **SQLite mode limitations**: `SET LOCAL` RLS, `pg_try_advisory_lock`, and `SELECT FOR UPDATE SKIP LOCKED` are all Postgres-specific and unavailable in SQLite. SQLite mode is for local development only — no RLS enforcement, no advisory locks, no flood protection. Multi-tenant or production use requires Postgres. SQLite mode logs a startup warning: "SQLite mode: security and concurrency features disabled. Use Postgres for any shared deployment." |
| Template rendering | `jinja2.sandbox.SandboxedEnvironment` | Lint rule enforced |
| YAML parsing | `yaml.safe_load()` only | Lint rule enforced |
| Encryption | cryptography (Fernet) | Connector credentials + model backend credentials |
| API keys | SHA-256 hash storage | `mk_<lookup_prefix>_<secret>` format |
| Task queue | SAQ + Redis | Required for run dispatch + cron/polling triggers |
| Auth (v1+) | PyJWT[crypto] (JWT/JWK), passlib (bcrypt), python-saml, authlib (OIDC) | |

### Frontend
| Layer | Technology | Notes |
|---|---|---|
| Framework | Vue 3 (Composition API) | MVVM-native |
| State | Pinia | All stores carry org context |
| Component library | shadcn-vue + Radix Vue | Headless accessible primitives (buttons, dialogs, dropdowns, badges, tooltips, etc.) styled via Tailwind + CSS custom properties. Copy-paste model — components live in `src/components/ui/`. Agent theme `[data-theme]` overrides work cleanly because shadcn-vue uses the same CSS custom property token approach. Never build button/dialog/focus logic from scratch. |
| Graph | Vue Flow | Fractal nesting = breadcrumb drill-down. >80 nodes/canvas degrades. |
| Styling | Tailwind CSS | Standard + agent themes via `[data-theme]` CSS custom property layers |
| Real-time | Native WebSocket + Vue composable | Separate from ViewModel REST |
| Testing | Playwright | pytest-bdd step definitions |

### Testing
| Layer | Technology |
|---|---|
| BDD / acceptance | `pytest-bdd` + Playwright |
| Unit | `pytest` |
| API | `pytest` + `httpx` |
| LLM stub | `StubModelBackend` — implements LangChain `BaseChatModel` (async); returns `AIMessage` from fixture map keyed on normalised input content; `UnexpectedInputError` on unmapped inputs; no special-casing in agent runtime |
| Isolation test | Integration test asserting cross-tenant data isolation across pooled connections |
| Coverage | `pytest-cov` |

---

## 12. Testing Strategy

- Every user-facing feature has a Gherkin scenario before implementation (BDD-first)
- Every interactive element has `data-testid` — no exceptions
- LLM calls use `StubModelBackend` in all automated tests
- Connector operations stubbed in unit/BDD tests; live tests in separate `integration` suite
- `StubModelBackend` built before any feature tests are written
- Cross-tenant isolation test runs in CI against a pooled connection — catches `SET LOCAL` regressions

**Playwright test strategy**: Playwright tests run against the **agent theme** (`?theme=agent`) by default — the stripped, functionality-only theme is the most stable test surface (zero decorative DOM noise). Standard theme is only targeted in tests that explicitly assert visual/aesthetic behaviour (e.g. status badge colours, animation presence). This also validates that the agent theme is complete — if a Playwright test cannot find an element in agent theme, that element is missing ARIA or `data-testid` markup and must be fixed.

**WebSocket test strategy**: Playwright tests that verify real-time run progress use the run's REST endpoints (`GET /api/v1/runs/{id}`) to poll for state transitions rather than asserting on WebSocket events directly. WebSocket reconnection and event replay are tested as dedicated Python integration tests (not Playwright), asserting the ring-buffer replay sequence on a mock broker.

### Feature Test Coverage (Alpha)
```
features/
  organisation/ org_scoping.feature, rls_isolation.feature
  pipelines/ create.feature, run_sequential.feature, validation.feature, concurrency.feature
  connectors/ filesystem.feature, github.feature, swappable_binding.feature, health_check.feature
  model_backends/ configure.feature, rotation.feature, health_check.feature
  triggers/ manual.feature, webhook_hmac.feature, webhook_payload_mapping.feature, flood_protection.feature, trigger_event_log.feature
  agents/ configure.feature, prompt_versioning.feature, schema_assignment.feature
  schemas/ create.feature, version.feature, deletion_protection.feature
  hitl/ claim.feature, approve.feature, reject.feature, human_only_gate.feature, overdue_warning.feature
  errors/ retry.feature, failed_state.feature, recovery.feature
  library/ browse.feature, copy_to_adapt.feature
  workflows/ export.feature, import.feature, binding.feature
  users/ basic_auth.feature, roles.feature, runner_role.feature
  audit/ event_recording.feature
  notifications/ hitl_webhook.feature, failure_webhook.feature, signing.feature
  mcp/ trigger.feature, review_hitl.feature, human_only.feature, library_browse.feature, onboarding.feature

V1 Feature Tests (separate suite, not in alpha CI — these features do not exist in alpha):
  teams/ team_create.feature, team_membership.feature, team_pipeline_visibility.feature, team_hitl_gate.feature, cross_team_isolation.feature, admin_override.feature, team_deletion_blocked.feature, stale_jwt_revocation.feature, view_as_team_non_admin_rejected.feature
  evals/ eval_llm_judge.feature, eval_regex.feature, eval_block.feature, conditional_hitl.feature
  library/ rating.feature (v1 — community ratings require multiple users)
```

---

## 13. Alpha Scope

**Goal**: prove composability, "the remainder," and connector swappability. Every primitive type has two implementations. Demo walkable in under 5 minutes from `docker compose up`.

### Primitive Coverage (2 per type)

| Primitive | A | B |
|---|---|---|
| Connector | `FilesystemConnector` (git-host) | `GitHubConnector` (git-host) |
| Trigger | `manual` | `webhook` |
| Model backend | Anthropic Claude | OpenAI GPT-4o |
| Library schema | `markdown-document` | `structured-requirements` |
| Library agent | `document-loader` | `requirements-extractor` |
| Library workflow | `simplest-workflow` | `prd-to-requirements` | `requirements-to-file` |

### Alpha Feature Checklist

**Infrastructure**
- [ ] Organisation-scoped data model; all tables have `organisation_id`; Postgres RLS with `SET LOCAL`; lint rule banning bare `SET`; isolation integration test
- [ ] Pipeline, Stage, ConnectorInstance, ModelBackend, and LibraryEntry tables include `owner_team_id` (nullable FK, not enforced in alpha — team management is v1) and `visibility` (`org`|`team`, default `org`) columns in initial Alembic migration
- [ ] Agent table includes `evals: JSON` column (nullable, default null) in initial Alembic migration — not surfaced in alpha UI, not executed in alpha; avoids painful v1 migration
- [ ] Pipeline edge entity (`id`, `pipeline_id`, `source_node_id`, `target_node_id`, `edge_type`, `hitl_gate_config JSON`) in initial migration
- [ ] Org-level API key entity (`id`, `lookup_prefix`, `hashed_secret`, `role`, `team_id` nullable, `created_by`) — separate from model backend Fernet credentials; used for MCP bearer token auth
- [ ] Startup sequence: Alembic `upgrade head` → `AsyncPostgresSaver.setup()` → application start; Postgres advisory lock for multi-worker startup
- [ ] Async driver enforcement: `AsyncPostgresSaver`/`AsyncSqliteSaver` + `asyncpg`/`aiosqlite`; no sync DB calls in async path
- [ ] SQLAlchemy models + Alembic migrations for all core entities
- [ ] LangGraph + PostgresSaver/SqliteSaver; thread IDs prefixed `org_id:`; separate `langgraph.*` schema
- [ ] FastAPI ViewModel + WebSocket event bus (separate transport concerns)
- [ ] Basic auth + `MODULO_ADMIN_PASSWORD` override; `SECRET_KEY` enforcement — startup **refuses to start** (not just warns) if default or < 32 bytes; JWT `algorithms=["HS256"]` pinned explicitly
- [ ] Fernet encryption on all connector and model backend credentials
- [ ] Docker-compose: Postgres + API + UI; SQLite fallback
- [ ] `SandboxedEnvironment` lint rule; `yaml.safe_load()` lint rule; credential-in-state lint rule (banning credential field names from LangGraph state dict assignments)
- [ ] Pre-commit hooks enforcing security lint rules
- [ ] API rate limiting middleware: `POST /api/v1/runs`, inbound webhook, MCP tool calls; in-memory fallback in alpha with startup warning

**Model Backend Management**
- [ ] `ModelBackend` entity (provider, model_id, Fernet-encrypted credentials, cost_tracking, currency)
- [ ] ModelBackendHub registration and resolution
- [ ] Health check (test inference call on save)
- [ ] Credential rotation action
- [ ] Anthropic Claude + OpenAI GPT-4o built-in configurations

**Connectors**
- [ ] ConnectorType interface + capabilities list + ConnectorHub
- [ ] ConnectorBinding spec on pipeline nodes (`{type, instance_id}`)
- [ ] `FilesystemConnector`: read + write + git push; `base_path` chroot enforcement (`os.path.realpath` prefix check)
- [ ] `GitHubConnector`: read + write + create PR; scope verification in health check
- [ ] Connector health check (pre-run + on-save)
- [ ] Credential rotation action
- [ ] Advisory write lock (`pg_try_advisory_lock`) on shared resources
- [ ] Connector ACL (`visibility`, `allowed_operations`)

**Triggers**
- [ ] `Trigger` entity + type registry; many-to-one with pipeline
- [ ] `ManualTrigger`: `POST /api/v1/runs`
- [ ] `WebhookTrigger`: HMAC-SHA256 validation of `timestamp + "." + body` (constant-time compare); `X-Modulo-Timestamp` required; ±300s replay window; `payload_mapping` (JSONPath); flood protection (`max_concurrent_runs`); deduplication window; `TriggerEvent` log; replay action
- [ ] Pre-run input validation against entry agent schema

**Pipeline + Agent**
- [ ] Pipeline CRUD with org scoping + `connector_binding` on each node
- [ ] Agent CRUD: prompt template (SandboxedEnvironment); `model_backend_id` reference; `prompt_version_history`; retry policy
- [ ] Schema CRUD: versioning; soft-delete deletion protection
- [ ] PipelineSnapshot: pins pipeline definition + schema version refs + prompt version refs + connector bindings
- [ ] Graph validation: topology; schema compatibility; connector capability; model backend health; pre-run input schema check
- [ ] Sequential pipeline execution via LangGraph StateGraph
- [ ] Per-node retry policy
- [ ] Run state machine (all states including `claimed`; `waiting_for_lock` excised in migrations 0074/0075)
- [ ] Run concurrency controls (`max_concurrent_runs` per pipeline)
- [ ] Error recovery: retry-from-node, retry-from-start
- [ ] Manual (Placeholder) Node: pause run for human-provided output; `output_schema_id` validation on human input before run continues; no `agent_id`, `connector_binding`, or `model_backend_id`; review UI identical to HITL claim flow

**HITL**
- [ ] `interrupt()` → `awaiting_human`
- [ ] Atomic claim (`UPDATE ... WHERE claimed_by IS NULL RETURNING id`); returns `claim_token` — **alpha**: cryptographically random opaque string stored in `hitl_claims.claim_token` with TTL; **v1**: JWT scoped to run+gate+client
- [ ] `human_only: boolean` flag on gate definition; ViewModel enforces 403 on MCP approve when `true`
- [ ] Per-gate configurable claim expiry; background expiry job; claim_token invalidated on expiry
- [ ] Approve → resume; Reject → reject-target with reason; both require `claim_token`
- [ ] HITL overdue warning (configurable threshold; notification dispatch)
- [ ] Run retention policy (90-day default TTL after terminal state)

**Notifications + Audit**
- [ ] Outbound webhook: HMAC-SHA256 signed; multiple endpoints; events: `hitl_awaiting`, `run_failed`, `claim_expired`, `hitl_overdue`
- [ ] AuditEvent writes on all state transitions (no viewer UI in alpha)

**Observability**
- [ ] LangGraph→OTel bridge (standalone blocking dependency — must be built before any OTel span assertions; maps `on_chain_start/end`, `on_llm_start/end`, `on_tool_start/end` to OTel spans with correct parent propagation)
- [ ] Stdout OTel exporter (default); env var config for OTLP/LangSmith
- [ ] OTel spans on all LLM calls and connector operations

**Library**
- [ ] Local library service: CRUD for schemas, agents, workflows
- [ ] 2 built-in schemas, 2 built-in agents, 2 built-in workflows
- [ ] Copy-to-adapt (clone library primitive into org workspace; ownership picker)
- [ ] Rating system deferred to v1 — alpha is single-user internal; ratings require a real user community

**Frontend**
- [ ] Vue 3 + Pinia scaffold; org context in all stores; `planStore` hydrated from `GET /api/v1/license` on page load
- [ ] shadcn-vue component library initialised: `Button`, `Badge`, `Dialog`, `Tooltip`, `DropdownMenu`, `Input`, `Label`, `Separator` installed as baseline primitives before any feature UI is built; components live in `src/components/ui/`
- [ ] Theme system: `data-theme` on root element; `standard` and `agent` themes in alpha; `?theme=<name>` query param override; `localStorage` persistence; admin deployment default
- [ ] Sidebar tier badge: `Community` / `Team` / `License expired` pill in nav footer; reads from `planStore`; links to `/settings/license`
- [ ] `/settings/license` page: current tier card, active feature checklist, license key paste + verify + apply, upgrade CTA on Community tier
- [ ] Team-gated feature pattern: lock icon + `Team` badge + disabled control + tooltip on all gated UI elements
- [ ] Vue Flow pipeline canvas: nodes with ConnectorBinding picker, HITL gate edge type, inline validation errors
- [ ] Agent config UI: prompt editor (sandbox warning), model backend selector, connector type + binding picker, prompt version history viewer
- [ ] Schema editor: field definition, type selection, version history, deletion guard
- [ ] Model backend management UI: register, health check, rotate credentials
- [ ] Connector instance management: create, health check, rotate, ACL config
- [ ] Trigger config UI: manual (one-click) and webhook (path, secret display, payload_mapping config)
- [ ] HITL review UI: full context, claim button, approve/reject, claimed-by indicator, overdue badge
- [ ] Run list + detail: state badge, per-node status, error detail (named code + user-facing message, never raw exception), recovery actions, TriggerEvent log; per-node expandable: input payload, rendered prompt, model response, output payload, eval results (if any), error detail; "Copy as test fixture" action; "Copy error details" redacted report
- [ ] Library browser: list, preview, copy-to-adapt; rating UI
- [ ] Demo pipeline pre-loaded; guided first-run walkthrough
- [ ] Real-time progress via WebSocket

**Remote MCP Server**
- [ ] FastAPI MCP endpoint at `/mcp` (HTTP + SSE, MCP protocol)
- [ ] MCP resources: `modulo://pipelines`, `modulo://runs/{id}`, `modulo://runs/{id}/hitl/{gate_id}`, `modulo://library`, `modulo://schemas`, `modulo://connectors`, `modulo://model-backends`; agent_output resources annotated with `content_type: agent_output`
- [ ] MCP tools: `trigger_pipeline` (fire-and-forget, returns run_id), `get_run_status` (summary-by-default, `detail: true` param), `get_run_output`, `cancel_run`, `review_hitl` (unified claim/approve/reject with claim_token; `destructive: true`), `list_pipelines`, `list_pending_hitl`, `search_library`, `copy_library_primitive` (requires `confirm: true`), `list_trigger_events`
- [ ] All list tools and run status: cursor-based pagination with `next_cursor`
- [ ] API key bearer token auth only (alpha); OAuth 2.0 deferred to v1
- [ ] Dual-layer scope enforcement: token middleware + ViewModel command layer
- [ ] Per-event SSE org context validation (not only at connection establishment)
- [ ] MCP onboarding page `/settings/mcp`: API key generation, config snippets (Claude Desktop, Cursor, generic), registered client list, revoke client
- [ ] `pytest-bdd` scenarios: MCP tool calls for trigger, HITL review_hitl (claim+approve+reject), library browse, human_only gate rejection

**Demo and First-Run**
- [x] ~~Demo mode env var: auto-configures `StubModelBackend` with pre-canned responses for the demo pipeline's exact inputs, and `FilesystemConnector` with a pre-populated `base_path` of sample files. Demo pipeline runs end-to-end with zero external API keys. Real model backends and GitHubConnector remain configurable separately.~~ — **removed/descoped (FAR-308)**
- [ ] Compiled StateGraph cache: in-memory LRU keyed by `snapshot_id`; re-compilation only on first execution of new snapshot
- [ ] Per-run event broker: single `astream_events()` consumer per active run; in-memory pub/sub fan-out in alpha; WebSocket and SSE connections subscribe to broker

**Testing**
- [ ] `StubModelBackend` implements LangChain `BaseChatModel` (async); returns `AIMessage` from fixture map; `UnexpectedInputError` on unmapped; built before any pipeline test
- [ ] `pytest-bdd` + Playwright for all alpha features
- [ ] Cross-tenant RLS isolation integration test
- [ ] Separate `integration` suite for live connector operations

---

## 14. Future Roadmap

### V1 Core (Initial Release)
The minimum viable public release. Ships together.

- Full user management: local user table + JWT (family invalidation) + OIDC + SAML 2.0
- Team management: Team entity, TeamMembership, team-owned resources, team-scoped HITL gates, team-scoped API keys, SSO group-to-team mapping, team notification endpoints, `/settings/teams` UI
- Team cost attribution: `team_id` on all usage events; per-team spend reporting
- Org/team admin spend and run limits (`org_daily_run_limit`, `team_daily_run_limit`)
- Eval System: llm_judge, regex, json_schema, custom_function; warn/block failure behaviour; per-node eval results in run detail
- Schema Inference: LLM-assisted schema draft from connected tool data (samples 200 records); supports issue-tracker, git-host, and document-store connectors; draft opens in schema editor for operator review; SDLC onboarding path (§8.16)
- Run Variants / A/B Testing: variant groups with `run_context_overrides`; all-or-nothing pre-flight quota check; side-by-side eval comparison; eval coverage gap signal; pre-eval degraded mode (cost + output diff); partial completion with HITL; prompt version comparison (§8.19)
- Feedback System: `FeedbackRecord` entity; `human` / `ai_correction` / `ai_correction_with_human_review` handler types; AI correction agent as library primitive; correction run mechanics (new thread pre-seeded from original checkpoint); eval gap detection via standalone `EvalEngine.evaluate()`; feedback inbox UI; eval proposals queue; eval suite growth flywheel. **Delivery dependency**: Eval System ships first; human feedback inbox can ship earlier as standalone (§8.20)
- Complexity-reviewer library primitive (v1 completion of Run Context Propagation: context-setter enforcement validated, complexity-reviewer canonical agent published to library)
- HITL claim_token upgraded to JWT; conditional gates (eval→HITL); modify-then-approve; deliver manually
- Community library UI + workflow import/export; rating system
- Additional connectors: GitLab (`git-host`), Jira (`issue-tracker`), Linear (`issue-tracker`)
- Plugin entry-point API documented and stable (`modulo.connectors`, `modulo.evals`, `modulo.model_backends`)
- Cron trigger
- Cost controls UI; Audit log viewer; Run trace / observability UI
- Organisation deletion / offboarding flow
- Documentation: quickstart, connector authoring, schema reference, API reference, deployment guide, architecture

### V1 Extended (Post-Launch, shipped incrementally)
These are not required for the initial release but should follow shortly after.

- Kick-back edges, conditional transitions
- MCP OAuth 2.0 server via `authlib` (PKCE mandatory; pipeline-scoped scopes)
- AI-assisted schema generation ("describe output → suggested schema")
- Schema union/collection types + migration functions
- OTel exporter config UI
- Self-hosted → SaaS migration CLI (`modulo export-org` / `modulo import-org`)
- HITL pipeline-level permission overrides
- Agent theme (`?mode=agent`) — low priority if MCP covers the headless use case

### V2
- Workflow + schema registry protocol (publish/pull; Ed25519 signed)
- Abstract schema namespacing (`author/name`)
- Integration library with ratings + download counts
- Bundle trust model (verified publishers): application process, key issuance, revocation; green/amber trust tier badges in library UI
- Polling trigger engine
- Full eval dashboard + run comparison
- AI-driven prompt optimisation with A/B testing
- Pipeline versioning UI (diff, rollback)
- SCIM provisioning with team sync
- Audit log cryptographic chaining
- Pluggable SecretsBackend (Vault, AWS Secrets Manager)
- LangGraph PostgresSaver subclass with organisation_id isolation (pre-SaaS requirement)

### V3 (SaaS — conditional on V1/V2 traction)
- modulo-cloud service layer (org lifecycle, SaaS plan enforcement, subdomain routing) — build only if community traction and team license sales justify the operational overhead
- Multi-tenancy active (RLS fully enforced; LangGraph isolation complete)
- Hosted community registry (Modulo-operated)
- Multi-region data residency architecture
- Team / org management UI
- Usage tracking + cost attribution per team (SaaS billing consumer added to event bus)

---

### 8.24 Parameterizable Composite Nodes

**Status**: Pre-development
**PRD ref**: §8.2 Agent Model, §8.4 Pipeline Builder, §8.14 Community Library
**RFC ref**: `docs/proposals/rfc-node-metacategories.md` (§2.5 Composite)

#### Concept

A **parameterizable composite node** is a first-class library primitive that exposes a user-defined internal pipeline as a single, drag-and-drop node with **configurable input ports**. Unlike the existing `parent_node_id` nesting (which is purely visual grouping), a parameterizable composite has:

1. **A defined internal sub-pipeline** — a complete pipeline graph stored as a library primitive
2. **Parameter ports** — named inputs on the composite's boundary that the parent pipeline author sets when placing the node (e.g. `system_prompt`, `model_selection`, `temperature`, `num_llms`, `output_style`)
3. **Boundary schema mapping** — how the parent's artifact payload maps to the sub-pipeline's entry schema, and how the sub-pipeline's exit schema maps back

This enables the use case described in §1: a user builds a "Devil's Advocate" composite — a sub-pipeline with two parallel agent nodes (one arguing for, one against) and a mediator node that synthesises both into balanced prose — saves it to the library, then drops it into any pipeline with different input prompts and output schemas.

#### Data Model

##### CompositeTemplate

A `composite_template` entity stores the reusable sub-pipeline definition:

| Field | Type | Description |
|---|---|---|
| `id` | UUID | PK |
| `organisation_id` | UUID | FK → organisations |
| `name` | string | Human-readable (e.g. "Devil's Advocate Council") |
| `description` | text | What this composite does |
| `sub_pipeline_graph_json` | JSON | The full internal pipeline: nodes (agent_id, position, connector_binding), edges (source, target, edge_type, hitl_gate_config). Same shape as `PipelineSnapshot.graph_json`. |
| `parameter_ports_json` | JSON | Array of `ParameterPort` definitions (see below). |
| `input_schema_id` | UUID, nullable | The entry schema of the sub-pipeline (first node's input_schema). Null until first save. |
| `output_schema_id` | UUID, nullable | The exit schema of the sub-pipeline (last node's output_schema). Null until first save. |
| `created_at`, `updated_at` | timestamps | |
| `account_id` | UUID | FK → accounts (creator) |

The `sub_pipeline_graph_json` references agent IDs by UUID, model backends by UUID, and schemas by UUID — all resolved at run-start from the PipelineSnapshot.

##### ParameterPort

Each port is a named, typed configuration knob exposed on the composite's boundary:

```json
{
  "id": "uuid",
  "name": "system_prompt",
  "label": "System Prompt",
  "description": "The core instruction given to each LLM in the council",
  "type": "string",
  "required": true,
  "default_value": "You are a helpful assistant. Analyze the input and provide thoughtful advice.",
  "target_injection": {
    "mode": "prompt_replace",        // "prompt_replace" | "prompt_append" | "run_context_key" | "node_field"
    "node_id": "uuid",               // which internal node to inject into
    "injection_point": "prompt_template"  // where in the target node
  }
}
```

`type` is a JSON Schema type (`string`, `number`, `boolean`, `select`, `model_backend_ref`, `schema_ref`). `model_backend_ref` and `schema_ref` are special types rendered as pickers (model backend selector, schema picker) instead of text inputs.

Default behaviour when `target_injection.mode = "prompt_replace"`: the composite renders the internal node's prompt template, then replaces the `{{parameter.<name>}}` placeholder in the rendered prompt with the port's value. This is the simplest and most common pattern — the composite author writes placeholders in the internal prompts.

##### CompositeBinding (on PipelineSnapshot)

When a pipeline author places a composite node and configures its ports, a `CompositeBinding` is stored in the PipelineSnapshot:

```json
{
  "composite_template_id": "uuid",
  "parameter_values": {
    "system_prompt": "You are a strict code reviewer...",
    "num_llms": 3,
    "model_backend": "<model_backend_uuid>"
  },
  "input_mapping": {
    "source_schema_id": "<node's input schema UUID>",
    "target_schema_id": "<sub-pipeline entry schema UUID>",
    "field_map": {"title": "prd_title", "body": "prd_body"}
  },
  "output_mapping": {
    "source_schema_id": "<sub-pipeline exit schema UUID>",
    "target_schema_id": "<node's output schema UUID>",
    "field_map": {"advice": "result", "confidence": "score"}
  }
}
```

Field mapping uses JMESPath expressions (same mechanism as webhook `payload_mapping` in §8.5). When `source_schema_id` and `target_schema_id` are identical (compatible schemas), mapping is passthrough — no field_map required.

##### PipelineGraphNode Extension

The existing `PipelineGraphNode` Pydantic model (§2.5 of RFC) gains:

```python
class PipelineGraphNode(BaseModel):
    # ...existing fields...
    composite_ref: uuid.UUID | None = None  # references a CompositeTemplate
    composite_parameter_values: dict[str, Any] | None = None  # port name → value
    composite_input_mapping: FieldMapping | None = None
    composite_output_mapping: FieldMapping | None = None
```

These fields are mutually exclusive with `agent_id` — a node is either a simple agent or a composite reference, never both.

#### Library Integration

##### New Primitive Type

Add `composite` to the `LibraryPrimitive.primitive_type` CHECK constraint:

```sql
CONSTRAINT ck_library_primitives_type CHECK (
    primitive_type IN ('schema', 'workflow', 'agent', 'integration', 'test_fixture', 'pipeline_template', 'composite')
)
```

A `composite` library entry serialises the `CompositeTemplate` definition in `content_json`:

```json
{
  "name": "Devil's Advocate Council",
  "description": "Two LLMs argue opposite positions, a mediator synthesises advice",
  "sub_pipeline_graph_json": {...},
  "parameter_ports_json": [...],
  "input_schema_id": "...",
  "output_schema_id": "..."
}
```

##### Copy-to-Adapt

Copying a composite primitive from the library works like any other primitive: creates a local editable copy with `forked_from` provenance. The local copy is a fully independent `CompositeTemplate`. The CopyToAdaptWizard gains an optional step for parameter port review — showing default values and descriptions before confirming.

##### Versioning

CompositeTemplates are versioned independently (same semver model as schemas). When a new version is published: existing PipelineSnapshots continue using the pinned version (snapshot captures `composite_template_id + version`). Users see an "update available" indicator in the pipeline editor if a newer version exists, with a diff of changed parameter ports.

#### Runtime Execution

##### Graph Expansion

Composite nodes are expanded at **snapshot creation** time, inside `create_snapshot_from_live_graph` (`modulo/db/crud/pipeline_snapshot.py`): the composite's `sub_pipeline_graph_json` is flattened into the parent graph before the snapshot is stored, and the composite node itself is removed. The compiled LangGraph StateGraph therefore contains the composite's internal nodes directly — expansion is in-line, never a separate thread/run. This means:

- The composite's internal nodes execute within the same run, same thread ID, same checkpoint scope
- Sub-node IDs are reassigned to fresh UUIDs; parent edges targeting the composite are rewired to its entry sub-nodes, and edges sourced from the composite are rewired to its exit sub-nodes
- Nested composites are expanded recursively (depth-limited to 5)
- The parent artifact is transformed through `input_mapping` before entering the first composite node
- The composite's output is transformed through `output_mapping` before being presented as the composite node's output
- HITL gates inside the composite appear in the parent run's HITL queue normally
- All internal nodes appear in run inspection at the same level as parent nodes (no separate drill-down in alpha — the phantom flow approach from the run state modelling)

The composite node's `output_schema_json` is propagated to its exit sub-node(s) so output validation fires at runtime. Each expanded composite records a binding in the snapshot's `composite_bindings_json` (template id, version, parameter values, input/output mapping); a long-running composite that pauses at an internal HITL gate resumes with the same pinned values even if the author edits the pipeline.

##### Schema Mapping

Field mapping follows the same pattern as webhook payload mapping (§8.5):

```python
def apply_field_mapping(source: dict, field_map: dict[str, str]) -> dict:
    """Apply JMESPath field mapping. Returns mapped dict."""
    result = {}
    for target_field, source_expression in field_map.items():
        result[target_field] = jmespath.search(source_expression, source)
    return result
```

Passthrough (no field_map, compatible schemas): entire payload passes through unchanged. Partial mapping: unmapped fields in the target are omitted unless the target schema marks them optional. A strict mode (`mapping_must_cover_all_required: bool`) can be set on the binding to reject mappings that leave required fields unmapped.

##### Parameter Injection at Runtime

At expansion time, the expander (`modulo/core/composite_engine/expander.py`):

1. Reads `composite_parameter_values` from the PipelineGraphNode
2. Replaces `{{parameter.<name>}}` placeholders in each sub-node's prompt fields (`prompt`, `prompt_template`, `agent_prompt`) with the port's value — the `prompt_replace` mode, which is the alpha scope. `prompt_append`, `run_context_key`, and `node_field` injection modes are v1 scope.

Parameters are snapshot-pinned: the parameter values at run-start are captured in the PipelineSnapshot's `composite_bindings_json`. A long-running composite that pauses at an internal HITL gate resumes with the same parameter values, even if the parent pipeline author changes them in the editor.

#### Semantic Versioning for Composites

| Version change | What it means |
|---|---|
| **Major** | Breaking change to parameter ports (removed port, changed type, changed required status). Introduces or removes internal nodes. Changes entry/exit schema. |
| **Minor** | New optional parameter port added. New internal path added (no mandatory change to existing pipelines). |
| **Patch** | Internal prompt template tweaks, parameter default value changes, bug fixes. No behavioural change observable from the outside. |

The pipeline editor surfaces an "Update available" badge when the pinned composite version is behind the latest, with a version diff panel showing added/removed/changed parameter ports and a one-click "Update to vN" action. Updating bumps the pin in the snapshot; the pipeline author reviews and confirms.

#### Frontend: Composite Node Rendering

##### Canvas Appearance

A composite node on the Vue Flow canvas has:
- **Double border** (matching the RFC's composite category visual: indigo, thick/double border)
- **Expand/collapse affordance**: clicking drills into a read-only view of the sub-pipeline (same drill-down pattern as existing nesting, per RFC §2.5)
- **Port indicator badge**: shows how many parameters are configured (e.g. "3/5 ports set")

##### Parameter Configuration Panel

When a composite node is selected, the properties sidebar shows a **Parameters** tab with one form field per `ParameterPort`:

| Port type | UI control |
|---|---|
| `string` | Text input (or textarea if `multiline: true`) |
| `number` | Number input |
| `boolean` | Toggle/switch |
| `select` | Dropdown with `options` from the port definition |
| `model_backend_ref` | Model backend picker (same component as agent config) |
| `schema_ref` | Schema picker |

Each field shows the port's label, description, and default value. Required ports are marked with `*`. Validation runs before the pipeline can be saved (required fields filled, types match).

##### Pipeline Library Browser

The library browser gains a "Composites" tab alongside the existing Agents/Schemas/Workflows. Each composite card shows:
- Name, description, author
- Port count + input/output schema names
- Version badge
- "Add to pipeline" button (same flow as agent picker)

#### Schema Mapping UI

When a composite node is placed and the parent pipeline has a preceding node, the properties sidebar shows a **Mapping** tab:

1. **Input mapping**: the preceding node's output schema on the left, the composite's entry schema on the right. The user pairs fields via drag-and-drop or a picker.
2. **Output mapping**: the composite's exit schema on the left, the expected downstream schema on the right. Same pairing UX.

Compatible schemas (identical structure) auto-map with a "Passthrough" badge. Incompatible schemas show warning badges but don't block saving (the user can map manually or adjust schemas).

Schema inference (§8.16) can suggest initial mappings when the composite is first dropped.

#### Composite Output Validation and Retry

##### Concept

Every composite node produces output through its `output_mapping` boundary. This output can be validated against the declared `output_schema_id` plus additional eval definitions (regex, JSON Schema, llm_judge). If validation fails, the composite's **internal sub-pipeline can be re-executed** up to a configurable maximum, enabling self-correcting pattern components like "Approver" (must start with APPROVED/REJECTED), "Booleaner" (must start with TRUE/FALSE), "d20" (must output a valid dice roll), or any user-defined validator gate.

This extends the existing per-node eval system (§8.17) and retry policy (§8.9) to work at the **composite boundary** rather than only at individual agent nodes.

##### Data Model

Extend `CompositeBinding` (stored in `PipelineSnapshot.composite_bindings_json`) with:

```json
{
  "composite_template_id": "uuid",
  "composite_version": "1.0.0",
  "parameter_values": {...},
  "input_mapping": {...},
  "output_mapping": {...},
  "output_validation": {
    "eval_definitions": [
      {
        "id": "uuid",
        "name": "first_word_approved_rejected",
        "type": "regex",
        "config": {
          "pattern": "^(APPROVED|REJECTED)\\b",
          "field": "result"
        },
        "failure_behaviour": "retry"
      },
      {
        "id": "uuid",
        "name": "output_schema_conformant",
        "type": "json_schema",
        "config": {
          "schema_ref": "<schema_id@version>"
        },
        "failure_behaviour": "block"
      }
    ],
    "max_validation_retries": 2
  }
}
```

| Field | Type | Description |
|---|---|---|
| `eval_definitions` | array | List of eval configurations executed against the composite's mapped output. Each follows the same shape as §8.17 eval definitions. |
| `max_validation_retries` | int | How many times to re-run the composite's full internal sub-pipeline on validation failure. Default 0 (no retry). Min 0. Max 5. |

Each eval definition carries a `failure_behaviour`:
- `retry` — if this eval fails, the composite re-executes (up to `max_validation_retries`)
- `block` — if this eval fails, the composite node transitions to `eval_failed` terminal state immediately, regardless of retry budget
- `warn` — log the failure, pass the output through regardless

The `block` behaviour exists for hard schema constraints (e.g. "output must match this JSON Schema for downstream compatibility") where retrying is pointless because the output shape itself is structurally wrong. The `retry` behaviour is for soft or pattern constraints where another LLM generation attempt might succeed.

##### Runtime Execution

After the composite's internal sub-pipeline completes and `output_mapping` transforms the result, before the output is presented to the parent pipeline:

1. **Run output evals**: execute each eval definition in order against the mapped output. Same `EvalEngine` as §8.17 — `_evaluate_regex()`, `_evaluate_json_schema()`, `_evaluate_llm()`.
2. **Check results**: if any eval with `failure_behaviour: block` fails → composite node transitions to `eval_failed`. Run stops.
3. **Retry check**: if any eval with `failure_behaviour: retry` fails → increment retry counter. If `retry_count < max_validation_retries`, **reset the composite's internal sub-pipeline state and re-execute**. If retry budget exhausted → composite node transitions to `eval_failed`.
4. **All pass**: output is promoted to the parent pipeline. Composite node execution is complete.

##### Reset Mechanics

A retry reset means:
1. The composite's internal state (sub-pipeline checkpoint) is rolled back to the state before the composite started execution
2. All parameter values are re-injected identically (they are snapshot-pinned)
3. The input mapping is re-applied from the original parent input
4. The internal sub-pipeline runs from the start again

This is not a correction run (§8.20) — it is a full re-execution with identical inputs. The existing `checkpoint_blobs` cleanup is reused.

##### Graph Validator

The graph validator (§8.4) gains checks:
- `max_validation_retries` must be 0–5 (inclusive)
- Each eval definition must have a valid `type` and `config` that matches that type
- `failure_behaviour` must be one of `retry`, `block`, `warn`
- Regex patterns must be valid Python regexes (compiled at validation time)
- JSON Schema eval config must reference an existing schema

##### Integration with Existing System

| Component | What changes |
|---|---|
| `CompositeBinding` Pydantic model | New `output_validation` field |
| `EvalEngine` | No changes — reused as-is with existing `_evaluate_*` methods |
| `Composite expander` | Post-execution eval step + retry loop around sub-pipeline re-entry |
| `PipelineSnapshot` | `composite_bindings_json` already stores per-node bindings; `output_validation` is part of that |
| `Graph validator` | New checks for validation config validity |
| `PipelineGraphNode` Pydantic | No changes — `output_validation` lives on the binding, not the node definition |
| Frontend `CompositeConfigPanel` | New "Output Validation" tab: add/remove eval configs, set retry count, regex/pattern editor |
| Run inspection | Validation results shown per-composite: eval scores, retry count, final pass/fail |

##### Library Primitive Patterns

With output validation and retry, users can build and save reusable pattern components:

**Approver**: composite with one agent node + output evals enforcing `result` starts with `APPROVED` or `REJECTED`. Retry on regex fail. Drag into any pipeline where you need a governed binary decision.

**Booleaner**: composite enforcing output starts with `TRUE` or `FALSE`. Useful for conditional edges downstream.

**d20**: composite with `output_schema` `{"roll": {"type": "integer", "minimum": 1, "maximum": 20}}`. Retry on schema validation failure (the LLM occasionally hallucinates numbers outside the range).

**Triage**: composite enforcing first word is one of `BUG|FEATURE|INFRA|DOCS`. Retry on mismatch.

All of these are just composites with pre-configured evals. The creator designs the internal prompt, sets the constraints, and publishes to the library. Consumers drag them in, configure the input prompt, and get enforced output contracts for free.

#### Composite Authoring Flow

Creating a composite is a multi-step process available from the library:

1. **Design sub-pipeline**: open the pipeline editor within a composite template scope. Build the internal graph with agents, edges, HITL gates — same full editor.
2. **Define ports**: in a sidebar panel, add parameter ports — name, type, default, required, and the target injection configuration. Each port shows a preview of how it will inject into the internal prompts.
3. **Test**: run the composite with test parameter values and sample input. All internal nodes execute in a temporary run scope.
4. **Publish**: save as library primitive with version, description, tags. Published to the local library immediately; optionally contribute to community registry (v2).

Existing pipelines can be promoted to composites: "Save as composite template" extracts the selected subgraph into a template with auto-detected parameter placeholders.

#### Snapshot and Migration

The PipelineSnapshot entity gains:

```json
{
  "composite_bindings_json": [
    {
      "composite_template_id": "uuid",
      "composite_version": "1.2.0",
      "parameter_values": {...},
      "input_mapping": {...},
      "output_mapping": {...}
    }
  ]
}
```

Existing snapshots without `composite_bindings_json` are handled by backward-compat shim (empty list = no composites). The GraphValidator checks composite version pins at run-start: if the pinned version has been deleted, the run is blocked with `composite_version_deleted`.

#### Scope and Delivery

**Alpha scope**: CompositeTemplate CRUD, ParameterPort definition, library save as `composite` primitive, basic parameter injection (`prompt_replace` mode only), single-passthrough mapping (compatible schemas), canvas rendering as composite node, version pinning in snapshots. No schema mapping UI in alpha — passthrough only. No test-run flow. No community registry.

**V1 Extended scope**: Full schema mapping UI (field-pairing drag-and-drop), `model_backend_ref` and `schema_ref` port types, `run_context_key` and `node_field` injection modes, test-run flow for composite templates, auto-detect parameter placeholders on "Save as composite", version diff panel, update-available indicator. Composite output validation with evals at the composite boundary and configurable retry (§8.24 Composite Output Validation and Retry).

**V2 scope**: Community registry for composites, output-only composites (no input mapping — self-contained generators).

#### Delivery Tasks

The feature decomposes into these delivery tasks:

1. `task-nv30-composite-data-model` (L) — CompositeTemplate entity, ParameterPort model, migration, CRUD service + API routes
2. `task-nv30-composite-library` (M) — `composite` library primitive type, library browser tab, copy-to-adapt composite flow
3. `task-nv30-composite-runtime` (XL) — Graph expansion executor, parameter injection, schema mapping passthrough, snapshot composite_bindings_json, graph validator composite checks
4. `task-nv30-composite-frontend` (L) — Canvas rendering (double border, drill-down), parameter configuration panel, library composable picker
5. `task-nv30-composite-authoring-flow` (L) — Composite template editor sub-pipeline mode, port definition UI, publish flow
6. `task-nv30-composite-schema-mapping` (L) — Schema mapping UI (drag-and-drop field pairing), inference-assisted mapping (v1-extended)
7. `task-nv30-composite-bdd-tests` (L) — BDD feature files for all composite behaviours, step definitions
8. `task-nv31-composite-validation-retry` (M) — Output validation evals at the composite boundary, retry loop on failure, output_validation field on CompositeBinding, graph validator checks for eval config, frontend "Output Validation" tab, library primitive patterns (Approver, Booleaner, d20)

**Phase**: `phase-nv30` (new)

---

### 8.31 Lifecycle Map

A Lifecycle Map is a declarative, multi-diagram model of an organisation's SDLC — or any business process that moves work through governed stages. It is the top-level view of "how does work get from idea to customer?" Each stage in the map is a node; each handoff between stages is a transition edge. Stages can be linked to executable Modulo pipelines (clickable → shows the pipeline graph), marked as external (documented but not managed by Modulo), or left as manual (human process with no automation).

Lifecycle Maps exist because a single pipeline cannot capture an entire SDLC. Pipelines are linear or branching execution graphs — they start, run, and finish. A Lifecycle Map is a **state machine** that describes how a unit of work (a task, a ticket, a PR, a deploy) moves through logically ordered stages over hours, days, or weeks. The pipeline is the *execution* primitive. The Lifecycle Map is the *orchestration* primitive.

From the user's perspective: the Lifecycle Map is the first thing they see when they want to understand or design "how we deliver software." It is the TL;DR of their SDLC — each heading is a clickable stage, each arrow is a documented handoff. It is useful on day 0 with zero Modulo-managed steps. Over time, stages graduate to Modulo-managed pipelines. The goal is not 100% management — it is 100% model fidelity. Some stages will always live in external systems (deploy pipelines in CircleCI, incident response in PagerDuty), and the map documents that honestly.

**Core philosophy**: the Lifecycle Map is a semantic model, not a workflow engine. It says "tasks should flow through these stages in this order" without prescribing *how* each transition happens. Some transitions are tight (pipeline A's output triggers pipeline B via Modulo signals). Others are loose (a label change on a Jira ticket is detected by an external cron; Modulo just documents the relationship). Both belong in the same map.

#### 8.31.1 Map Concepts

| Term | Definition |
|---|---|
| **Lifecycle Map** | A DAG of stage nodes and transition edges describing a logical work lifecycle. Versioned, org-scoped, renderable as an interactive diagram. |
| **Stage Node** | A named step in the lifecycle. Has a `type` (see §8.31.2) and optionally links to a Modulo pipeline or an external system URL. |
| **Transition Edge** | A directed connection between two stage nodes. Carries trigger metadata describing *how* work moves from one stage to the next — even if Modulo doesn't own the trigger. |
| **Trigger Metadata** | A structured annotation on a transition edge: `trigger_type: "pipeline_completed" \| "webhook" \| "cron" \| "manual" \| "external"`, a human-readable description of who/what fires it, and optionally a link to the trigger definition if Modulo-managed. |
| **Graduated Stage** | A stage node that started as `external` or `manual` and was later linked to a Modulo pipeline. The map preserves its history — previous versions show what it looked like before graduation. |
| **Map Version** | A versioned snapshot of the entire map DAG. A version API surface is implemented (list versions, GET a version by number, POST save, PUT update) plus a restore endpoint (`POST /{id}/restore`) that revives a soft-deleted map and frees its pipelines. A save bumps the version counter and publishes the content as the active version; only the active version is served (GET of a non-current version returns 404). Concurrent saves of the same map are serialised on a row lock (`SELECT ... FOR UPDATE`): the active version is last-write-wins and the counter is strictly increasing, so concurrent saves can never produce duplicate version numbers; version-list reads always see one consistent committed snapshot. Immutable per-version snapshot history with browse-back remains a later slice. |

#### 8.31.2 Stage Node Types

| Type | Rendered | Meaning |
|---|---|---|
| `modulo` | Solid border, clickable to pipeline graph | This stage is a Modulo pipeline. Clicking opens the pipeline editor or run history. |
| `external` | Dashed border, clickable to external URL | This stage runs in an external system (CircleCI, GitHub Actions, PagerDuty). The map documents its existence; Modulo does not own execution. |
| `manual` | Dotted border, no click-through | This stage is a human process with no automation or tooling. The map documents it for completeness. |
| `placeholder` | Ghost/faded border | This stage is planned but not yet implemented in any system. Represents a gap in the current process. |

Each stage node carries:
- `name`, `description`, `type`
- `pipeline_id` (if type `modulo`) — links to the pipeline definition
- `external_url` (if type `external`) — links to the external system
- `owner` — which team or person owns this stage
- `required_schema_in` / `schemas_produced` — what artifacts this stage expects and produces (optional, documentary)

#### 8.31.3 Transition Edges

Each edge carries:
- `from_stage_id`, `to_stage_id`
- `trigger_type` — `pipeline_completed` \| `webhook` \| `cron` \| `manual` \| `external`
- `trigger_description` — human-readable explanation: "A human clicks 'Deploy' in CircleCI after soak tests pass"
- `trigger_id` — optional, links to a Modulo Trigger entity if Modulo-managed
- `condition` — optional JMESPath expression describing when this transition fires (e.g. only on green builds)
- `estimated_frequency` — how often this transition typically fires (daily, per-PR, hourly)

#### 8.31.4 Fractal Navigation

A Lifecycle Map is the outermost layer of a fractal hierarchy:

```
Lifecycle Map (the DAG you drew)
  └── Stage Node: "Task Delivery"
        └── Pipeline (clickable → opens pipeline editor)
              └── Agent Node: "Coder"
                    └── Agent config (prompt, schema, model backend)
                          └── Sub-pipeline (if composite node)
```

At every level, the user can "double-click" to drill down. A `modulo` stage node opens its pipeline graph. An agent node opens its config panel. A composite node opens its sub-pipeline. Navigation is breadcrumb-based with viewport state preserved per level (§8.4).

Not every stage needs a Modulo pipeline attached. An `external` stage node with no `pipeline_id` renders as documentation — clicking it opens the `external_url` in a new tab. The map is still useful: it shows the complete lifecycle even when Modulo only owns pieces of it.

#### 8.31.5 Graduation Path

The graduating-track is the core adoption model. A stage starts as `placeholder` (planned), becomes `manual` or `external` (documented), and optionally graduates to `modulo` (managed):

1. **Day 0**: A team creates a Lifecycle Map of their current SDLC. All stages are `external` or `manual`. Transitions are annotated with trigger descriptions but no Modulo trigger IDs. The map is a documentation artifact — useful for onboarding, process review, and gap analysis.
2. **Day 1**: The team replaces one `manual` stage with a Modulo pipeline. The stage node type changes to `modulo` and links to the pipeline. The incoming transition edge gains a `trigger_id`. The map now has one graduated stage.
3. **Day N**: More stages graduate. Some never do — the external deploy pipeline in CircleCI stays `external`. The map remains accurate because `external` is a first-class stage type, not a shortcoming.
4. **Maturity**: Over time, the map becomes a hybrid — some stages Modulo-managed, some external, some manual. The map truthfully reflects the real SDLC at every point in its evolution.

**Why not force 100%:** Forcing all stages into Modulo pipelines creates perverse incentives — teams either stop using the map (because it doesn't reflect reality) or they build shallow pipelines that obscure where the real work happens. Honest stage types keep the map useful forever.

#### 8.31.6 Multiple Maps

Any org member can create a Lifecycle Map. Maps are not limited to "the main SDLC." Teams and individuals create maps for any process they want to document or evolve:

| Map | Audience | Stages |
|---|---|---|
| **Main SDLC** | Entire team | Issue → Groom → Dev → Review → Staging → Prod |
| **Dependency Renovation** | Platform team | Crawl → Triage → PR → Merge → Deploy |
| **Incident Response** | On-call | Alert → Classify → Remediate → Postmortem |
| **Content Publishing** | Marketing | Draft → Review → Approve → Publish |

Maps are independent — a single task might appear in multiple maps (a deploy shows up in both "Main SDLC" and "Infra Change" maps). Maps carry `owner_team_id` and `visibility` (`org` \| `team`), following the same pattern as other Modulo resources.

#### 8.31.7 Visual Editor

The Lifecycle Map editor is an interactive canvas (built on Vue Flow, same as the pipeline editor):

- **Drag-and-drop stage nodes** from a palette
- **Click to configure** each node: type, name, pipeline link, external URL
- **Draw transition edges** between nodes by dragging from one node's output port to another's input
- **Edge properties panel**: trigger type picker, description field, condition expression, trigger link
- **Version selector**: the map detail page renders a version dropdown (vN) that can re-fetch the active version; a time-machine browse-back slider over retained snapshots is a later slice
- **Graduation indicator**: graduated stages show a small badge (shield icon) with the graduation date
- **Map list page**: card grid of all maps in the org, searchable, filterable by team

The editor is available from `/lifecycle-maps` in the sidebar (Core group, or a new "Lifecycle" group if maps reach sufficient adoption).

**Alpha scope**: view-only map rendering from JSON/YAML. Creating and editing maps via JSON editor. No drag-and-drop canvas. Stage node types render with correct border styles. Clicking a `modulo` stage navigates to the pipeline editor. Clicking an `external` stage opens the URL.

**V1 scope**: Full drag-and-drop visual editor. Transition edge drawing. Version history browser. Map list page. Team ownership. Graduation badge.

**V2 scope**: Multiple maps per org. Community map templates ("Start from SDLC template"). Map diff view (side-by-side version comparison). Map export as PNG/SVG for embedding in Confluence/Notion.

#### 8.31.8 Relationship to Existing Primitives

| Existing Concept | How It Relates |
|---|---|
| **Pipeline** (§8.4) | An executable pipeline is attached to a `modulo` stage node. A pipeline may be a stage of **one active map at a time** — the lifecycle-map stage junction enforces this via a save-time uniqueness check backed by the partial unique index `uq_lifecycle_map_stages_active_pipeline`. A pipeline graduating into a new map must first be freed from its previous active map by deleting or un-linking it there; `restore` refuses when a stage pipeline is already registered in another active map. |
| **Trigger** (§8.5) | A transition edge with `trigger_type: "webhook"` or `"cron"` can link to a Modulo Trigger entity. The trigger definition lives separately; the edge just references it. |
| **Manual Node** (§8.4) | A `manual` stage node is the lifecycle-level equivalent of a Manual pipeline node — a human step with no automation. Manual stages document the handoff; Manual pipeline nodes pause execution for human input. They are independent concepts at different fractal levels. |
| **Pipeline Template** (library) | A Pipeline Template can be "slotted into" a stage node as a starting point. "Use template" in the stage config creates a new pipeline from the template and links it. |
| **Composite Node** (§8.24) | A composite node within a pipeline is sub-pipeline execution. A Lifecycle Map stage is cross-pipeline orchestration. They operate at different fractal levels and are not interchangeable. |

#### 8.31.9 Primitive Model

Lifecycle Maps are stored as a new `lifecycle_map` primitive type in the library system (same pattern as `pipeline_template` and `workflow`):

```yaml
primitive_type: lifecycle_map
content_json:
  stages:
    - id: "stage-gen"
      name: "Task Generation"
      description: "Issues, bugs, and feature requests enter here"
      type: modulo  # or external, manual, placeholder
      pipeline_id: "uuid-of-pipeline"         # if type=modulo
      external_url: "https://github.com/..."  # if type=external
      owner: "platform-team"
      position:
        x: 100
        y: 200
  transitions:
    - from: "stage-gen"
      to: "stage-delivery"
      trigger_type: external
      trigger_description: >
        A human adds a 'ready_for_dev' label on the
        GitHub issue, which is detected by a scheduled
        GitHub Actions workflow that fires the delivery
        pipeline via webhook.
      trigger_id: "uuid-of-trigger"  # optional
      condition: "issue.labels contains 'ready_for_dev'"
      estimated_frequency: daily
```

Versioning follows the same pattern as schemas: explicit save creates a new version. Old versions are browsable. The `lifecycle_map` primitive can be exported, imported, and shared via bundles. **Implementation note:** the version API surface is implemented — `GET /{id}/versions` (list), `GET /{id}/versions/{version}` (get; only the current version is served — a non-current version returns 404), `POST /{id}/versions` (save) and `PUT /{id}/versions/{version_id}` (update). A save validates and canonicalises the content, replaces the active `content_json`, and bumps the version counter; the active map state is served as a single version entry. `POST /{id}/restore` revives a soft-deleted map (freed pipelines re-register). Immutable per-version snapshots with browse-back are a later slice. **Export/import (FAR-174, extended FAR-204):** `GET /api/v1/lifecycle-maps/{id}/export` returns the map's version history as a portable envelope (`primitive_type: lifecycle_map`, `format_version: 2`, name/description, `content_json` of the active stages/edges/notes, and a `versions` array of each version's stages/edges/notes + version number + created_at); `POST /api/v1/lifecycle-maps/import` accepts that envelope, validates content with the same rules as an editor save (malformed graph → 422), dedupes the name ("(imported)" suffix on collision), and creates a NEW map that is also registered as a `lifecycle_map` library primitive. Import is backward compatible: a `format_version: 1` payload (no `versions`) imports as a single-version map; a v2 `versions` array is replayed through the version-save path so the chain is recreated deterministically (exported numbers preserved when contiguous 1..N, otherwise re-derived). Lifecycle maps are a first-class library primitive type: they can be listed in the library, `POST /api/v1/libraries/{id}/create-lifecycle-map` copies a primitive into a new map in the org (copy-to-adapt), and `POST /api/v1/libraries/community/contribute` accepts `primitive_type: lifecycle_map` so maps can be contributed to the community library like any other primitive (FAR-204).

#### 8.31.10 The 80% Target

The product target is that 80% of an organisation's SDLC stages are *managed* by Modulo (type `modulo` with a live pipeline), while 100% of stages are *modeled* in the Lifecycle Map. The remaining 20% are honestly annotated as `external` or `manual` — not because we want them there permanently, but because honest annotation is more valuable than forced coverage.

Metrics surfaced in the map UI:
- **Coverage**: "8 of 12 stages graduated (67%)" — a progress indicator, not a shame metric
- **Gap analysis**: stages that have been `placeholder` for >30 days are surfaced as "consider graduating"
- **Velocity**: average time per stage (only for Modulo-managed stages where timing data exists)

#### 8.31.11 Delivery Tasks

1. `task-lm-data-model` (L) — LifecycleMap entity, StageNode model, TransitionEdge model, JSON schema, migration, CRUD service + API routes
2. `task-lm-library-primitive` (M) — `lifecycle_map` primitive type in library system, bundle export/import support, versioning
3. `task-lm-readonly-render` (M) — Vue Flow read-only renderer: stage nodes with type-based border styling, transition arrows, click-to-navigate on `modulo` / `external` stages, version browser
4. `task-lm-visual-editor` (XL) — Full drag-and-drop canvas: stage palette, node config panel, edge drawing, trigger metadata panel, save/publish flow
5. `task-lm-map-list` (S) — Card grid of all maps, searchable, filterable, create-new flow
6. `task-lm-graduation-flow` (M) — "Graduate stage" action: promote manual/external to modulo, link to existing pipeline or create new, version snapshot
7. `task-lm-bdd-tests` (M) — BDD feature files for map CRUD, rendering, navigation, graduation
8. `task-lm-dogfood` (L) — Modulo's own Lifecycle Map defined, stages graduated to Modulo pipelines progressively, used as the primary onboarding example

> The `task-lm-*` IDs above are from the retired delivery tracker. The feature shipped incrementally via Linear FAR-141 (version persistence + stage junction), FAR-142 (journey data model), FAR-143 (advancement / self-report / reconciliation), FAR-144 (journeys API + provenance badges), FAR-145 (unattributed flag).

**Phase**: `phase-lm` (new — independent of alpha/v1 phases; can ship incrementally)

#### 8.31.12 Flag / Gating

| Component | Tier |
|---|---|
| Read-only map rendering (from JSON) | Community |
| Visual map editor | Community |
| Multiple maps per org | Community |
| Map version history browser | Team |
| Team-owned maps | Team |
| Graduation metrics / gap analysis | Team |
| Map templates ("Start from SDLC") | Community (library) |
| Map diff view (version comparison) | Team |

Lifecycle Maps are a core onboarding and documentation feature. The base feature (create, view, navigate stages) is Community-tier. Advanced features (version history, team scoping, analytics) are Team-tier.


#### 8.31.13 Journeys

A **journey** is a unit of work — a task, a ticket, a PR, a deploy — tracked as it moves across a lifecycle map's stages. Journeys are the runtime counterpart of the map's declarative stages: the map models the process; journeys observe real work flowing through it.

**Minting (FAR-142):** journeys are minted org-wide at run create time from a run's `work_item_refs` — a JSONB array of canonical `{kind, ref, source, status?}` pairs stamped on the run (from the trigger payload's `work_item_ref_paths` or a node's self-report). Each canonical `(kind, ref)` pair becomes a `journeys` row via `INSERT ... ON CONFLICT (organisation_id, kind, ref) DO NOTHING`; the row's `canonical_work_item_id` is deterministic (`uuid5(org, kind, ref)`), so there is no mint race. Canonicalisation (`modulo.db.lifecycle_refs`) normalises refs (e.g. GitHub `#123` and `123` land on the same ref).

**Map ownership:** a lifecycle map "owns" journeys whose latest-stage identity points at one of its stages, plus journeys whose canonical `(kind, ref)` has already run through one of the map's stage pipelines.

**Map-scoped reads (FAR-144):** `GET /api/v1/lifecycle-maps/{id}/journeys` lists the map's journeys, keyset-paginated over `(updated_at, id)` with an opaque cursor, optionally narrowed to an exact `kind`/`ref`. Journey detail (`GET /api/v1/lifecycle-maps/{id}/journeys/{kind}/{ref}`) adds run history: per-run status, provenance, and completion date. Both endpoints gate on the `run.list` permission (not `lifecycle_map.list`) and are org-scoped via RLS. Journey runs render as provenance badges on the map canvas.

**Advancement (FAR-143):** on run finalise, an advancement service (`core/lifecycle_map/advancement.py`) advances the journey for every canonical ref the run carried. Latest evidence is compare-and-set: only a strictly-newer evidence timestamp overwrites the row's `updated_at` (equal timestamps keep existing evidence — deterministic first-writer-wins). `run_count` increments for terminal advancing statuses (`complete` / `failed` / `eval_failed`); `awaiting_human` updates evidence without counting; non-advancing runs (`cancelled` / `stalled`, replays, variants) are mint-only. Stage columns are set only when the run's pipeline is a lifecycle-map stage (resolved org-scoped via `lifecycle_map_stages`).

**Self-report parse (FAR-143):** `core/lifecycle_map/self_report.py` extracts work-item refs from merged pipeline output under `work_item_refs` / `modulo.work_item_refs` / `touched_work_items` (recursive walk, node-keyed placement supported). Reported entries are advisory: provenance is forced to `reported`, so a reported claim can confirm or match an existing journey but never mints one. Workflow self-reports may additionally name the completed stage via `stage_id` (the merge queue sends `stage_id: "merge"`, the deploy agent `stage_id: "deploy"`); when the named stage resolves to a stage of the reported map's current version, the journey advances into it even though the stage is external (a GitHub Actions workflow, not a Modulo pipeline) and has no `pipeline_id`. The explicit `stage_id` is used only when pipeline resolution yields no stage — a pipeline-resolved stage still takes precedence.

**Reconciliation (FAR-143):** `core/lifecycle_map/reconcile.py` is a bounded, terminal-only safety net that re-derives journey evidence for runs whose journeys never advanced (post-deploy backlog, raw terminal writers skipping the hook, or a self-report confirm dropping a ref the create-time mint never saw). Drift is MISSING (no journey row) or STALE (row `updated_at` older than the run's evidence timestamp); only drifted refs are re-advanced, and the sweep is idempotent and fails open per run.

**Data model (FAR-142):** `journeys` (migration 0084) + `journey_facts` (migration 0085), anchored on `runs.work_item_refs` / `runs.work_item_id` (migration 0083). Org-scoped with strict RLS (`rls_org_isolation`) plus the `enforce_same_organisation` tenant trigger, mirroring `lifecycle_map_stages`. `latest_terminal_run_id` is deliberately not an FK so journeys survive the 90-day run purge. The `unattributed` flag (FAR-145) marks journeys not referenced by any of the map's stage-pipeline runs.

**Dogfooding:** a production lifecycle map "Farnalabs SDLC (dogfooding)" runs with stages Fix / Review / Merge / Deploy.

---

### 8.32 Analytics

Run analytics over a **retained facts table** (`run_daily_facts`, ADR 020): one row per terminal run, written on every finalize path and backfilled by a daily maintenance cron. Facts are also written as a **compensating write** when the sweep/SAQ terminalizers mark runs failed outside the normal finalize path (`stale_run_recovery_sweep`, `dispatcher_reconcile`, `fail_run_terminal`), so terminal runs never miss a facts row. Facts survive the 90-day run purge, so dimensioned run history outlives the `runs` rows it was derived from.

#### 8.32.1 Rolling-Window Semantics

Dashboards report over **rolling windows**: **Last 1h / 24h / 3d / 7d / 30d / 90d** — each window is the trailing period ending now (timezone-agnostic). Every value is shown with a **period arrow** (delta) that is **period-scoped and same-source/same-window**: a "Last 7d" delta compares the current 7d window against the *previous* 7d window, using the same metric from the same source. Rolling windows eliminate the partial-period bias of calendar-month widgets (a month widget shows 3 days of data on the 3rd).

The facts table keeps both the source run's `created_at` instant (rolling precision for the 24h window) and the UTC attributed day `run_date` (the day-level bucket key).

#### 8.32.2 Query Surface — Typed Params Only

**No query language in this delivery — structured typed params are the surface** (`group_by`, optional dimension, filters, date range ≤ 365d, limit). A syntax/expression query mode is **deferred until a pre-rolled dependency slots in** — it is never hand-rolled ad hoc. The backend is the sole bucketing authority: hour/day/ISO-week bucketing and zero-fill happen server-side; the client only renders. Auto-granularity resolves the bucket by window span (hour for spans ≤ 3 days, day for spans ≤ 90 days, week beyond); an explicit `group_by` passes through unchanged.

#### 8.32.3 Launch-Forward Coverage

Analytics coverage is **launch-forward**: the feature reports "data since \<date\>" (the date the maintenance cron first backfilled), because facts are only available from when the writer started recording — historical runs before the launch date are not reconstructed.

#### 8.32.4 Run-Count Denominators

Three distinct run-count denominators exist and are never conflated:

| Denominator | Source | Meaning |
|---|---|---|
| **Facts count** | `run_daily_facts` | All terminal runs (complete / failed / cancelled / eval_failed / stalled — the `TERMINAL_STATUSES` set) |
| **Ledger count** | `org_daily_run_counts` | Terminal runs with `total_cost_usd > 0` that passed the spend ledger |
| **Summary count** | `runs` | All runs (including pending/running/awaiting_human) |

The analytics dashboard uses the **facts count** for run volume and success rate; the spend ledger's counts are used by cost dashboards; the summary count is used by the live dashboard widgets. "Number of runs" must always state which denominator it uses.

#### 8.32.5 Dimensions and Filters

Optional dimension (`trigger_type`, `status`, `pipeline`, `folder`, `team`, `error_code`; omitted = overall) and filters: `trigger_type`, `status`, `pipeline_id` (repeated — a multi-value "A vs B" filter in a single request), `error_code`, `folder_id`, fixed `date_from`/`date_to` (≤ 365 days), `limit` (≤ 1000). Timezone is fixed UTC.

#### 8.32.6 Flag / Gating

| Component | Tier |
|---|---|
| Analytics page + endpoint | Community (`analytics_page` feature flag, live) |

#### 8.32.7 Dashboard Rolling-Window Toggle

The live dashboard (`/`) has a top-right rolling-window toggle: **Last 24h / Last 3d / Last 7d / Last 30d / Last 90d** (the `days` query param accepts any integer 1–90). When a window is selected, the stat cards (pipelines, total runs, running / awaiting human / failed / idle, eval pass rate, token spend) become **period-scoped** to that window and each card shows a **trend arrow** (▲ up / ▼ down / → flat; green / red / grey; 1dp %) comparing the current window against the immediately-preceding equal-length window.

The toggle also includes an **All time** option that clears the window (no `days` param) and shows the all-time scalars. The selected window persists to `localStorage` (`modulo.dashboard.trendWindow` — `'all'` for all-time, otherwise the day count as a string) and is restored on the next visit; first-time users (nothing stored) default to the **3d** window. Clicking a numbered window refreshes **only** the period-scoped block via the same `summary?days=N` endpoint — the all-time summary (totals, teams, trend, recent runs) is not reloaded and no full-page loading skeleton appears; switching to All time reloads the full summary. When a period window has no data for a metric (current is zero), the card falls back to the all-time value rather than showing 0.

Semantics are **same-source/same-window**: a card's value and its arrow always come from the same metric over the same window — run counts/status/tokens/success/duration from `run_daily_facts`, spend from the `org_daily_run_counts` ledger (org-level row), eval pass rate from `eval_results`. Because facts record only terminal statuses, running / awaiting-human / idle read zero in period mode. The arrow is omitted when there is no baseline (previous window empty/zero) or no current-window data. The `GET /api/v1/dashboard/summary?days=N` endpoint returns the period metrics as an additive `period` block (`{current, previous, delta_pct}` per metric); without `days` the response is the unchanged all-time summary.

#### 8.32.8 Analytics Page UI (frontend)

The `/analytics` page (sidebar group Core, `analytics.query` permission, community tier) is a **structured filter form → typed query params** surface. There is **no query language in this delivery** (see 8.32.2) — the filter form is the surface; syntax/expression mode stays deferred until a pre-rolled dependency slots in.

- **Filter bar**: rolling timespan toggle (1h / 24h / 3d / 7d / 30d / 90d), granularity (hour / day / ISO week; auto resolves by span), optional dimension (trigger_type / status / pipeline / folder / team), and combinable filters (trigger_type, status, pipeline_id, folder_id). A measure picker selects the rendered metric (run count / cost / tokens / avg duration / success rate). The backend returns all metrics per bucket, so the measure is a client-side rendering concern — it is never a query param.
- **Visualisation**: ECharts (via `vue-echarts`, lazy-loaded). Line chart for undimensioned trends by date; bar chart for dimensioned queries (x = dimension key, fallback date). `buildChartOption` is a pure series → ECharts option mapping — the client never buckets. Pre-coverage buckets render as gaps (`null`), never zero-filled as "zero activity".
- **Trend table**: rows are buckets; columns are date/key + measure value + a period arrow (▲/▼/→) against the previous equal-length window. The previous window is fetched as a second query and compared by key (dimensioned) or by offset (undimensioned). `prev=0` and both-zero show no arrow.
- **Empty state**: "No analytics data yet — data since \<date\>" where \<date\> is the earliest bucket with data (launch-forward coverage, 8.32.3).
- **Flag-off state**: the sidebar link is hidden when the feature flag is off (manifest `required_permissions`); if the page is reached anyway and the API returns 402, an "Analytics is not enabled for your workspace" card is shown — never a spinner.

#### 8.32.9 Queryable-Data Layer (FAR-102)

The facts table is enriched with stall-dimension and run-lifecycle columns so the analytics surface can answer "why did runs fail / stall" without reading live `runs`: `error_code` (the stall dimension), `claim_count`, `queue_wait_ms` (started − dispatched), `final_idle_ms` (completed − heartbeat — the stuck-with-no-heartbeat window), `cancellation_requested`, `dispatcher`, `node_count` / `sandbox_agent_node_count` / `max_node_timeout_seconds` (derived from the snapshot `graph_json`, NULL-safe), `parent_run_id`, `snapshot_id` (both NO FKs — facts survive purge), `run_number`, `output_bytes` (serialised `outputs_json` size), and `rate_limited`. All are nullable where the source run may not carry the value. The live writer and the daily backfill both populate them — a backfilled fact never carries NULL where the source provides a value.

- **Per-bucket metrics**: the query surface adds `failure_count`, `stall_count`, `avg_queue_wait_ms`, `avg_final_idle_ms`, and `avg_output_bytes` to every bucket. A **stall** is a run whose `status == "failed"` and `error_code` is in a fixed `STALL_ERROR_CODES` set (`executor_stalled`, `node_timeout`, `TimeoutError` — the no-progress error codes the executor actually emits).
- **Comparison**: a repeated `pipeline_id` query param returns an "A vs B" series in a single request (the builder bounds it via an allowlisted, parameterised `IN` — never string interpolation).
- **Export**: `GET /api/v1/analytics/export` returns raw fact rows (no bucketing, all fact columns) filtered by the same typed params, ordered by `run_date`/`created_at`, paginated (`offset`/`limit`, default 500, max 5000), org-scoped, rate-limited, permission + feature gated. `format=json` (default) or `format=csv` (Content-Disposition attachment).
- **MCP**: the `query_analytics` MCP tool mirrors the REST surface (typed params incl. repeated `pipeline_id`, `error_code`, date range, limit), enforcing the `analytics.query` permission and the `analytics_page` feature gate. Both REST and MCP funnel through the same shared service, so org predicate, rate limit, statement timeout, bucketing, and error semantics stay identical. The MCP result also carries a **`deep_link`** — a relative `/analytics?...` URL pre-filtered with the same resolved params — so the Remy assistant can hand the user a clickable link to the pre-filtered analytics view instead of dumping the raw series. When Remy renders a successful `query_analytics` tool result it shows a **chart card** (reusing the analytics ECharts component with a measure toggle) plus the deep link; the `/analytics` page reads `group_by`/`date_from`/`date_to`/`dimension`/`trigger_type`/`status`/`pipeline_id`/`folder_id` query params on mount to restore the exact same view.

#### 8.32.10 Concurrency / Slot Utilization (FAR-134)

A dedicated `GET /api/v1/analytics/concurrency` endpoint (and the `query_analytics_concurrency` MCP tool) reconstructs "how many runs were running / queued at any instant" from the retained facts — no live `runs` reads. It requires the same `analytics.query` permission and `analytics_page` feature as the bucketed query and funnels through the same shared service (org predicate, rate limit, statement timeout, error semantics).

- **Fact columns**: `run_daily_facts` gains `dispatched_at`, `started_at`, `completed_at` (absolute UTC instants copied from the source run — deliberately NOT FKs, facts survive the run purge) and `total_queue_wait_ms` (`started_at − created_at` — the FULL wait from run creation to execution start, covering capacity-deferral backoff plus the SAQ queue; NULL when either side is missing). The live writer and the daily backfill both populate them; a backfilled fact never carries NULL where the source provides a value.
- **Launch-forward `total_queue_wait_ms`**: populated live for new runs (the live writer records `started_at − created_at` as each run completes) and backfilled per-day for historical runs from the 0076 migration launch date forward (consistent with the launch-forward coverage in 8.32.3). Runs that completed BEFORE the 0076 deploy keep `NULL` — the daily backfill fills `total_queue_wait_ms` only for the days it covers from the launch date onward and never reconstructs pre-launch values.
- **Semantics**: per bucket, `max_active`/`avg_active` are the peak and time-weighted mean of the concurrent-run count computed from the `[started_at, completed_at)` overlap (a run spanning a bucket boundary counts in BOTH buckets; a never-completed run — `completed_at` NULL — counts as active through the bucket's end). `max_queued`/`avg_queued` are the peak and time-weighted mean queue depth: runs with `created_at <= t < started_at`, with never-started runs (`started_at` NULL) counting as queued through the end of the range.
- **`pool_reference`**: the binding concurrency cap for the query scope — the org's `run_concurrency_limit`, or a single filtered pipeline's `max_concurrent_runs` when a `pipeline_id` filter is present.
- **Params**: `group_by` (hour/day/week, with auto-granularity), `trigger_type`, `status`, repeated `pipeline_id`, `folder_id`, `date_from`/`date_to` (≤ 365 days), `limit` (≤ 1000). There is no dimension split — concurrency is an overall-series surface.

---

## 15. Resolved Design Decisions

| Decision | Resolution |
|---|---|
| Agent theme | `?mode=agent` query param |
| Auth default | Basic auth (`admin:password`); JWT + SSO in v1 |
| Run state storage | LangGraph checkpointer (`langgraph.*` Postgres schema); domain in SQLAlchemy (`public.*`) |
| LLM driveability | Remote MCP server — first-class MVVM view. Standards-based (MCP protocol). Alpha: API key bearer token auth. OAuth 2.0 in v1. Replaces stretch goal framing. |
| Theme system | CSS-only, day one. `data-theme` on root. `standard` + `agent` in alpha. One component tree — no duplicate HTML. `?theme=<name>` override. Future themes are additive CSS. |
| Agent theme | CSS theme (`agent`), not a separate render target. Same component tree, DOM, data-testid, ARIA. Zero decorative chrome. Controlled by theme system. |
| Observability | OTel-first; LangSmith optional; custom LangGraph→OTel bridge is explicit build work |
| LangGraph versioning | Pinned exact version; upgrades = migration events with runbooks |
| Nesting depth | Max 3 levels, enforced by validator and UI |
| Generic agents | Experimental; require eval rubric before production promotion |
| BDD framework | `pytest-bdd` + Playwright |
| Jinja2 templates | `SandboxedEnvironment` always; pre-commit lint enforced |
| YAML parsing | `yaml.safe_load()` always; pre-commit lint enforced |
| HITL claim | Atomic `UPDATE ... WHERE claimed_by IS NULL RETURNING id` |
| HITL state | `claimed` is persisted sub-state |
| HITL claim expiry | Per-gate configurable; no global default |
| Schema deletion | Soft-delete; protected while referenced by any snapshot, agent, or library entry |
| PipelineSnapshot | Stores references (ID+version); embedding not used; deletion protection is integrity guarantee |
| Prompt versioning | Independent per-agent prompt version history; snapshot captures specific prompt version |
| Cost controls | Currency-configurable; `cost_tracking: disabled` per ModelBackend for open-weight models |
| Runner role | Added; can trigger runs + approve HITL; cannot create/edit pipelines |
| Outbound webhook signing | HMAC-SHA256 in `X-Modulo-Signature` |
| Inbound webhook auth | Per-trigger HMAC secret; `X-Modulo-Webhook-Secret` header; constant-time comparison |
| Webhook payload mapping | `payload_mapping` JSONPath/JMESPath field on WebhookTrigger (required) |
| Webhook flood protection | Per-trigger `max_concurrent_runs`; deduplication window; `TriggerEvent` log |
| TLS | Not provided by Modulo; reference Caddy config in repo; required before network exposure |
| Webhook + TLS in alpha | ngrok for local dev; GitHub webhooks are v1 use case |
| Trigger cardinality | Many triggers → one pipeline; one trigger → one pipeline; trigger delete does not cancel in-flight runs |
| ConnectorHub resolution | Explicit `connector_binding` on each node; no auto-selection; direct lookup at run time |
| Run concurrency | Per-pipeline and per-trigger `max_concurrent_runs`; advisory write lock on shared resources |
| Model backend management | First-class ModelBackend entity; ModelBackendHub; Fernet-encrypted; health check; rotation; parallel to ConnectorInstance |
| API key hashing | SHA-256 (not bcrypt); `mk_<lookup_prefix>_<secret>` format |
| RLS connection pooling | `SET LOCAL app.organisation_id` inside transactions; lint rule banning bare `SET`; isolation integration test |
| LangGraph checkpoint isolation | `ModuloPostgresSaver` (AsyncPostgresSaver subclass) adds `organisation_id` to all checkpoint tables and enforces it on every read/write; checkpoint JSON encrypted at rest with Fernet |
| modulo-cloud boundary | Zero coupling to core; would inject CloudPlanContext in V3 SaaS; calls core admin API; deferred to V3 |
| Org migration | `modulo export-org` / `modulo import-org` CLI; v1 |
| Org deletion | 30-day soft-delete grace; hard delete after; configurable retention for regulated orgs |
| Data residency | V3 concern; multi-region architecture decided at v3 day one |
| Registry signing | Ed25519 signed manifest; public key shipped with client; client-side verification before import |
| Community library scope | All primitives: schemas, workflows, agents, integrations — unified library |
| Shareable integrations | Pip packages; library links to package; not YAML |
| Alpha naming | "Alpha" not "MVP"; no time estimates; behaviour-driven scope |
| SaaS architecture | Multi-tenant from day one via `organisation_id` + RLS; self-hosted = single org; same codebase |
| Usage events | Internal event bus from day one; OTel exporter in self-hosted; token counting = internal cost controls only (not Modulo billing); flat annual license fee model requires no telemetry aggregation |
| Hosted registry | Modulo-operated; self-hosted pulls over HTTPS; Ed25519 verified |
| MCP auth (alpha) | API key bearer token only in alpha. OAuth 2.0 is weeks of implementation work; not justified for alpha scope. |
| MCP auth (v1) | Full OAuth 2.0 via `authlib` (not hand-rolled); PKCE mandatory; exact redirect_uri; no tokens in query strings |
| MCP HITL tools | Merged `claim_hitl` + `approve_hitl` + `reject_hitl` → `review_hitl(action, claim_token?, reason?)`. Claim returns `claim_token`; approve/reject require it. Reduces API surface and prevents replay across clients. |
| MCP HITL claim_token | Short-lived JWT scoped to `run_id + gate_id + client_id`; 15-min expiry; invalidated on claim expiry |
| HITL human_only flag | `human_only: boolean` on gate definition; alpha requirement. Blocks MCP approve (403); browser auth can still approve. Enforced at ViewModel layer, not just middleware. |
| MCP SSE vs request/response | MCP tools are request/response only. `trigger_pipeline` is fire-and-forget returning `run_id`. Run event streaming is separate SSE channel. These must not be conflated. |
| MCP response size | Summary by default; `detail: true` for full output; cursor-based pagination on all list tools |
| MCP resource content annotation | Agent output resources annotated `content_type: agent_output`; prompt injection warning in resource description |
| MCP write scope | Pipeline creation/editing deferred to v2. Alpha+v1 MCP: read pipelines, trigger runs, HITL review, library browse/copy only. |
| MCP scope enforcement | Dual-layer: token middleware (outer gate) + ViewModel command layer (authoritative). Neither layer alone is sufficient. |
| MCP SSE org context | Validated per event, not only at connection establishment. |
| MCP onboarding | `/settings/mcp` ships in alpha. API key generation, config snippets, registered client list, revoke. Without it, MCP is undiscoverable. |
| Accessibility — standard theme | WCAG 2.1 AA (contrast ratios, focus indicators, reduced motion) |
| Accessibility — agent theme | Exempt from colour contrast ratios. Retains keyboard nav, focus rings, ARIA labels, screen reader semantics. |
| DOM sensitive data rule | Sensitive values are `●●●●●` in DOM; server-authenticated reveal injects value for 30-second display window. CSS visibility is not a security control. |
| Team RBAC model | Teams are a v1 feature. Org role = baseline for org-visibility resources. Team role = effective access for team-visibility resources. Org `operator` cannot see team-private pipelines they are not a member of. Admin bypasses all team restrictions. |
| Team visibility is a privacy boundary | Team-visibility resources do not appear in the UI for non-members. No "N hidden" indicator — total absence. This prevents resource enumeration across teams. |
| Multiple team memberships | A user may be in multiple teams with independent roles per team. Roles from one team do not affect access to another team's resources. |
| Team role set | `operator`, `runner`, `viewer`. `admin` is org-only — not a valid team role. |
| Team-owned resources | Pipeline, Stage, ConnectorInstance, ModelBackend all carry `owner_team_id` (nullable) and `visibility` (`org`\|`team`). `null` + `org` = accessible to all org members. |
| Team connector binding | Team-private connectors can only be bound to pipelines owned by the same team. Enforced at ViewModel layer on pipeline save. |
| Team HITL gates | `required_team_id` on gate definition restricts claim/approve to members of that team. Exposed in gate context resource. |
| Team-scoped API keys | API keys carry optional `team_id`; restricts key to resources accessible to that team at the key's embedded role. |
| SSO team mapping | OIDC/SAML group claims map to Modulo team memberships via admin-configured mapping. Applied at JIT provisioning. |
| JWT team membership | JWT payload includes `team_memberships: [{team_id, team_role}]`. ViewModel resolves effective access from claims without DB round-trip per request. |
| Team management scope | Alpha: single admin, no teams active (columns in schema from day one). V1: full team management. V2: SCIM team sync. |
| Team deletion policy | Blocked if any resource has `owner_team_id` pointing to the team. Admin must reassign or delete resources first. Bulk reassign-to-org-wide provided. |
| Team membership privilege cap | Team operators can only grant roles up to their own team role. Cannot grant `operator` if you are a `runner`. Prevents intra-team escalation. |
| Ownership picker | All resource creation and copy-to-adapt flows require an explicit ownership selection (org-wide or team). No silent defaults. |
| JWT stale membership | 15-minute window is accepted for routine membership changes. Immediate revocation uses session revocation (token family invalidation). DB-live check required for `required_team_id` HITL — JWT claims not trusted on this path. |
| `view_as_team` IDOR prevention | Server-enforced admin check; non-admin requests with this param return 403. Team-private resources return 404 to non-members (not 403) to prevent enumeration. |
| Integration tier classification | Every connector, model backend, and library workflow is classified `Native` \| `Preview` \| `In-Dev`. Native = hardened, prominently surfaced; Preview = functional but segregated behind a secondary UI control; In-Dev = feature-flagged, hidden by default. Once V1 Native integrations are validated by real usage, focus shifts to hardening Native over adding breadth; Preview absorbs new/experimental integrations. See ADR 010. |
| Native library vs. community database | The workflow library splits into a Modulo-maintained Native library (structural, general-purpose patterns, e.g. "LLM as Council") and a separate, clearly-labeled community database of opinionated example pipelines (e.g. "translate to French", "QA Reviewer") contributed by users and not held to Native-maintenance standards. Community database reuses the same tier labeling but is never mixed into the Native library. Launch seeded with a small curated set; not marketed as "community-driven" until genuine external contributions exist. See ADR 010. |
| `human_only` + `required_team_id` | Additive: both conditions must hold. Browser-authenticated AND team member. Enforced independently at ViewModel layer. |
| Stage team ownership | Stage carries `owner_team_id` and `visibility`. A team Stage may only contain same-team pipelines. Cross-team pipeline assignment to a team Stage is a ViewModel error. |
| Pipeline ownership change | Blocked while any run is in non-terminal state. UI warns about stale connector bindings after ownership change. |
| Owner_team_id in alpha schema | Columns added to Pipeline, Stage, ConnectorInstance, ModelBackend, LibraryEntry in initial migration (nullable, default org). Team enforcement is v1 code; columns are alpha schema. |
| Team cost attribution | `team_id` on usage events from v1 (when teams launch), not v3. Retroactive attribution is impossible without it. |
| Bundle export strips team ownership | `owner_team_id` stripped on export. Importing org presented ownership picker before confirming import. |
| Library primitive visibility | Local library entries carry `owner_team_id` and `visibility`. Community registry entries are always org-visible. Copy-to-adapt presents ownership picker; defaults to source entry's team if team-owned. |
| Team notification endpoints | Team entity carries `notification_endpoints`. `hitl_awaiting` for `required_team_id` gates routes to team endpoints, falls back to org-wide. V1. |
| Single owner per resource | One `owner_team_id` per resource. Multi-team ACLs are v3+. Shared infrastructure uses `visibility: org`. Documented limitation. |
| Credential-in-state rule | Decrypted credentials never enter LangGraph state, checkpoint blobs, OTel spans, or log output. In-process only, via transient context object. Lint-enforced. |
| Webhook timestamp requirement | `X-Modulo-Timestamp` header required; included in HMAC input as `timestamp + "." + body`; ±300s replay window enforced server-side. HMAC alone does not prevent post-dedup-window replay. |
| FilesystemConnector base_path | Admin-configured per ConnectorInstance; all paths resolved relative to base_path with realpath prefix check; operators cannot escape the root. |
| Schema validation scope | Schema validation checks types/presence/format only — not a prompt injection sanitisation control. Documented explicitly in §6.2. |
| Eval injection surface | LLM-judge evals treat agent output as untrusted content; structural separators required in eval prompt; "evaluate only" instruction mandatory. |
| API rate limiting | FastAPI middleware on all write endpoints: `POST /api/v1/runs`, webhook inbound, MCP tool calls, HITL review. In-memory alpha fallback with startup warning; Redis required v1. |
| Ed25519 key rotation | Incident response process documented; v2 versioned key manifest for rotation without full release cycle. |
| Checkpoint blob self-hosted gap | Documented: DB-privileged admin bypasses all application-layer controls; restrict Postgres access to service account. Encryption in v2. |
| JWT algorithm pinning | `algorithms=["HS256"]` explicit in decode; `none` and other algorithms rejected. SECRET_KEY ≥ 32 bytes; startup refuses to start if violated. |
| ConnectorInstance visibility unified | `private`/`team-shared` removed; replaced with `owner_team_id` + `visibility: org|team` consistent with all other resource types. User-private connectors not supported — single-person team is the mechanism. |
| Eval System scope | v1 feature. Not in alpha. Architecture diagram is forward-looking. Conditional HITL requires Eval Engine — both must ship together in v1. |
| Error UX | Named error codes map to user-facing messages and suggested actions. Raw exceptions never in UI. "Copy error details" action produces redacted report. |
| Agent/schema pickers | Slide-out panel with search, description, schema summary. Schema compatibility warning on add. Alpha scope. |
| Run inspection UI | Per-node input/output/prompt/response/eval viewer. Sensitive payloads masked per DOM rule. "Copy as test fixture" action. Alpha scope. |
| Bundle import schema conflict | Same abstract_name + same structure → reuse. Same abstract_name + different structure → import with disambiguation suffix + warning. No auto-merge. |
| Bundle import agent/pipeline name conflict | Agent name collision (same name, different prompt/schema): import creates new agent with `(imported)` suffix; user can rename post-import. Pipeline name collision: same rule — suffix, no silent overwrite. Two agents within the same bundle sharing the same name: rejected pre-import with a validation error listing the duplicates. |
| Library trust tiers | Verified publisher (green, v2) vs Community (amber, warn on copy). Verified publisher program is v2 roadmap. |
| Plugin installation | Build-time only through v2. No runtime pip install. UI generates install command. SaaS plugin volume approach deferred to v3. |
| Org/team admin limits | `org_daily_run_limit`, `org_daily_spend_limit`, `team_daily_run_limit` — admin-configurable operational controls, independent of SaaS plan. V1. |
| LangGraph state type | `dict[str, Any]` at LangGraph level. No dynamic TypedDicts. Schema validation is a Modulo pre/post-node layer, not inside LangGraph's type system. Avoids reducer/Annotated/Pydantic footguns. |
| StateGraph caching | In-memory LRU keyed by snapshot_id. Re-compilation on first execution of new snapshot only. |
| WebSocket fan-out | Per-run event broker; one `astream_events()` consumer per active run; N subscriber connections. In-memory alpha; Redis pub/sub for multi-process v1. |
| Async driver mandate | AsyncPostgresSaver + asyncpg; AsyncSqliteSaver + aiosqlite. psycopg2/sqlite3 not permitted in async path — blocks event loop. Hard rule. |
| LangGraph startup sequence | Alembic upgrade head → AsyncPostgresSaver.setup() → app start. Postgres advisory lock for multi-worker. Both operations idempotent. |
| Lifecycle Map vs Pipeline | A Lifecycle Map is a state machine for cross-run task progression. A Pipeline is a single-run execution graph. Maps contain stages, optionally linked to pipelines. Pipelines are not maps. They coexist at different fractal levels. |
| Pipeline edge entity | First-class DB entity with `hitl_gate_config JSON`. HITL gate is an edge property, not a node property. Compiles to conditional edge + interrupt wrapper in LangGraph. |
| ConnectorHub credential lifetime | Decrypt once at run-start into run-scoped context object. Discarded at run end. Never enters LangGraph state. One Fernet decrypt per connector per run. |
| StubModelBackend interface | Implements LangChain BaseChatModel (async). Returns AIMessage from fixture map. No special-casing in agent runtime. Built before any pipeline tests. |
| Webhook flood protection | Postgres-backed via SELECT FOR UPDATE SKIP LOCKED on trigger row. DB-backed deduplication via unique constraint + TTL cleanup. Safe across multiple server processes. |
| claim_token alpha | Cryptographically random opaque string stored in DB with 15-min TTL. JWT-based claim_token deferred to v1 when JWT infrastructure ships with full user management. |
| Rating system scope | Deferred to v1. Alpha is single-user internal — no user community to rate. |
| Demo mode env var | ~~Auto-configures StubModelBackend + FilesystemConnector with pre-canned data. Demo walkable with zero external API keys.~~ — **removed/descoped (FAR-308)** |
| Component library | shadcn-vue + Radix Vue. Headless accessible primitives styled via Tailwind + CSS custom properties. Copy-paste model (`src/components/ui/`). Chosen because it uses the same `[data-theme]` CSS custom property token approach as the Modulo theme system — no theming conflict. Never build button/dialog/focus logic from scratch. |
| Tier badge placement | Sidebar nav footer pill, every authenticated page. Reads `planStore`; links to `/settings/license`. Team-gated features show lock icon + disabled control + tooltip instead of being hidden — passive upgrade funnel. |
| MODULO_LICENSE_KEY | Base64-encoded signed JSON team license payload. Verified against embedded Ed25519 public key on startup. If absent, invalid, or expired: FreeTierPlanContext applies (team features disabled). No outbound network call required. V1. |
| License model | BSL 1.1 — source-available (`Development/Product/LICENSE`). Free for all internal use (personal, commercial, production) except resale or paid hosting. Team tier = gated features only (SSO, RBAC, audit viewer, admin spend limits) — no SLAs, no dedicated support. Each version auto-converts to Apache 2.0 three years after its release date. The semver release process updates the LICENSE file's version and Change Date. Flat annual fee for team license key; no telemetry billing. |
| Alpha exit criteria | 5 explicit criteria (§9.3b). Alpha does not become v1 by default — explicit decision required. |
| V1 split | V1 Core (public launch — must ship together) + V1 Extended (post-launch incremental). Prevents V1 from becoming a never-shipping monolith. |
| Alpha documentation | dev-setup.md, architecture.md, CONTRIBUTING.md required before alpha is shared with a second developer. |
| OTel bridge priority | Standalone blocking dependency. Must be built before any OTel span assertions in tests. Dedicated task, not a by-the-way. |
| Eval JSON column in alpha | `evals: JSON` nullable column on Agent table in initial migration. Not surfaced or executed in alpha. Avoids v1 migration across all existing agent rows. |
| API key entity | Org-level entity (`mk_<lookup>_<secret>`, SHA-256 hash). Separate from Fernet model backend credentials. Listed under Infrastructure, not Model Backend Management. |
