# Persona: Duncan — Solo Developer / Indie Hacker

| Attribute | Value |
|---|---|
| **Role** | Solo developer building and operating a SaaS product |
| **Org size** | 1 person |
| **Technical level** | Full-stack, comfortable with self-hosting and DevOps |
| **Location** | Remote, UK |
| **SDLC maturity** | Informal — personal kanban, GitHub, Linear, manual QA on staging |
| **Budget authority** | Spends his own money; price-sensitive but values time savings |

## Goals

- Deliver features with minimal manual effort: PRD → tickets → code → review → deploy, governed by configurable autonomy
- Rotate between AI providers (Claude, GPT-4o, Gemini, local Ollama) to optimise cost, latency, and output quality per task
- Grow his pipeline complexity incrementally as his service matures — start simple, add steps later
- Keep full control: self-hosted, no telemetry leaving his infra, no SaaS dependency
- Reproduce his workflow across projects with minimal reconfiguration

## Pain points / triggers

- Spends 40%+ of his week on non-coding SDLC overhead: writing tickets, grooming, reviewing, releasing
- Has multiple AI subscriptions but no unified way to route tasks to the right model
- Wants agentic delivery but every "solution" is SaaS or requires a team
- Past attempts at AI automation produced inconsistent results — no versioning or rollback

## Key scenarios that must work

1. **`@persona-duncan`** `features/pipelines/run_sequential.feature` — Solo trigger-and-wait pipeline with no multi-user setup
2. **`@persona-duncan`** `features/model_backends/rotation.feature` — Route different nodes to different providers within one pipeline
3. **`@persona-duncan`** `features/model_backends/health_check.feature` — Dead backend auto-skipped; fallback to live provider
4. **`@persona-duncan`** `features/pipelines/create.feature` — Clone a personal pipeline template for a new project
5. **`@persona-duncan`** `features/errors/recovery.feature` — Failed run can be resumed from checkpoint; no work lost
6. **`@persona-duncan`** `features/workflows/export.feature` — Export pipeline as YAML bundle, import on another machine
7. **`@persona-duncan`** `features/eval/eval_run.feature` — Evaulate agent outputs automatically; flag regressions before deploy
8. **`@persona-duncan`** `features/hitl/approve.feature` — Brief pause-and-approve on the deploy gate (the only HITL he keeps)
9. **`@persona-duncan`** `features/observability/otel_traces.feature` — Glance at OTel trace to see where a run spent tokens

## Anti-scenarios (must NOT be required)

- Setting up SSO, team management, or org hierarchy
- Approval workflows requiring >1 human
- Entering a credit card to use the software
- A blank canvas as the starting point — library workflows or templates must be one-click

## What success looks like

Duncan pushes to `main`, Modulo picks up the webhook, runs his PRD→tickets→code→review→deploy pipeline across Claude (planning), GPT-4o (coding), and Ollama (review), posts a summary to his Linear, and deploys to staging — all while he's in a meeting. He checks the run trace once, sees green evals, and ships to prod.
