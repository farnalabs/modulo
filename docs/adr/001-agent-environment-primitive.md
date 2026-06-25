# ADR 001 — Agent Execution Environment as a V1 Primitive

**Date**: 2026-06-23  
**Status**: Active — delivering in phase-7a, before the rest of v1

---

## Context

Modulo defines agents as reusable, versioned logical units containing prompts, schemas, model bindings, connector requirements, evals, retry policy, and token budget. What it does not currently define is the compute environment in which a tool-using agent operates.

The current execution path is:

```
Agent definition in Modulo
        ↓
Modulo Agent Runtime / LangGraph
        ↓
ModelBackend → model inference provider (Claude, GPT-4o, etc.)
        ↓
ConnectorHub → GitHub / filesystem / etc.
```

Every agent "run" today is: call an LLM via ModelBackend, read/write via ConnectorHub. The execution happens on the Modulo server itself inside LangGraph. There is no isolated environment, no shell, no ability to run arbitrary code.

This is sufficient for a large class of SDLC work: requirements extraction, ticket generation, code review comments, PR description generation, document summarisation. All of these are prompt-in / structured-output-out patterns and they work with the existing stack.

**The gap appears the moment an agent needs to apply a code change and verify it.** Running tests, invoking a linter, building the project, applying a patch — all of these require a shell in an environment with the repository checked out. Without that, coding agents can generate code but cannot verify it worked.

---

## The Forcing Function: Modulo Builds Modulo

The primary goal driving this decision is **dogfooding**: using Modulo to build and improve Modulo itself.

For that to work, Modulo needs agents that can:

1. Read a ticket or spec — LLM call, works today ✓
2. Generate code changes — LLM call, works today ✓
3. Apply the changes and run the tests — **requires cloud execution, blocked** ✗
4. Create a pull request — GitHubConnector, works today ✓

Step 3 is the blocker. Without it, the "builds itself" pipeline can produce code but cannot close the loop. Every generated change requires a human to manually verify before merging. That defeats the purpose.

There is no workaround within the current architecture. This must be implemented.

---

## Decision

Deliver the agent execution environment as a first-class primitive in **phase-7a**, before SSO, team management, eval system, and the rest of v1. It is the prerequisite for the dogfooding goal, and the dogfooding goal is the fastest path to proving the platform works.

The implementation is a single RuntimeProvider backed by **E2B** as the first concrete provider. E2B gives sandboxed cloud containers with full shell access, git, and configurable tool images — exactly what a coding agent needs. The abstraction layer (described below) ensures other providers can be swapped in later without changing agent definitions.

---

## Architecture

The pattern mirrors ConnectorHub and ModelBackendHub: Modulo owns the contract; the deployment supplies the implementation.

### Three concepts

**EnvironmentProfile** — a reusable, versioned environment template stored in Modulo:
- Container image or E2B sandbox template (pinned by digest/id)
- CPU, memory, disk, timeout
- Declared capabilities: `["git", "python>=3.12", "shell", "network:github.com"]`
- Workspace initialisation strategy (git clone, worktree, blank)
- Network / egress policy
- Mount and cache policy
- Secret references — never decrypted values
- Persistence policy: ephemeral (default), retained-for-inspection, or cache-reusable

Example profiles: `python-dev`, `node-monorepo`, `modulo-itself`.

**RuntimeProvider** — the infrastructure implementation that realises a profile:
- **E2B** (first implementation — sandboxed cloud containers)
- Local Docker (second implementation, for self-hosted without cloud dependency)
- AWS ECS / Fargate / CodeBuild (future)
- Kubernetes (future)

Registered in a `RuntimeProviderHub`, parallel to ConnectorHub and ModelBackendHub.

**WorkspaceLease** — a per-run realisation of an EnvironmentProfile:
- Concrete sandbox/container ID issued by the provider
- Checked-out repository at the correct ref
- Lifecycle and expiry (max wall-clock time)
- Resource usage (CPU/memory observed)
- Output artifact references (files, stdout/stderr)
- Execution and audit metadata

The profile is reusable; the workspace is not — a fresh lease is created per run. Warm pools are a provider-level implementation detail; they must not leak mutable workspace state between runs or organisations.

### Binding model

An Agent declares requirements as a capability list. A pipeline node binds an EnvironmentProfile that satisfies them, at pipeline-save time — same pattern as `connector_binding`:

```yaml
# Agent definition
required_environment_capabilities:
  - git
  - python>=3.12
  - shell
  - network:github.com

# Pipeline node (set at save time, pinned in PipelineSnapshot)
environment_profile_id: <uuid>
```

The graph validator checks that the bound profile declares all capabilities the agent requires — hard block if missing, same as ConnectorType capability enforcement.

### ShellConnector

A new ConnectorType (`shell`) that executes commands inside the active WorkspaceLease for a run. Operations:
- `run_command(command: str, cwd: str | None, env: dict | None, timeout_seconds: int) → {stdout, stderr, exit_code}`
- `write_file(path: str, content: str) → None`
- `read_file(path: str) → str`
- `list_files(path: str) → list[str]`

ConnectorHub provides the ShellConnector with the run's active WorkspaceLease. Agents use it like any other connector — bound at pipeline-save, resolved at run time. No shell access outside a WorkspaceLease; the ShellConnector 403s if no lease is active for the run.

### PipelineSnapshot pinning

EnvironmentProfile version must be pinned into PipelineSnapshot alongside prompt version, schema version, connector bindings, and model backend. This is mandatory for correction runs and variant comparisons to be reproducible — a run must be replayable against the exact same environment image.

### Security constraints

- Decrypted credentials never enter the WorkspaceLease metadata, shell environment, LangGraph state, OTel spans, or logs — same rule as ConnectorHub.
- ShellConnector enforces a command allowlist per profile (configurable; default: deny-all except explicitly permitted commands).
- Network egress from the sandbox is controlled by the EnvironmentProfile's egress policy.
- E2B sandbox IDs are run-scoped and discarded at lease expiry; they are never reused across runs or orgs.

---

## What Must NOT Happen

- Environment configuration (image digest, AWS details, resource limits) must not live directly on the Agent entity. Coupling logic to infrastructure breaks reusability.
- The ShellConnector must not be usable outside a WorkspaceLease — it is not a general-purpose server shell.
- Command output (stdout/stderr) follows the same sensitive data rules as connector payloads: masked by default in run inspection, server-authenticated 30-second reveal.

---

## Delivery Scope (phase-7a)

1. `task-runtime-provider-core` — EnvironmentProfile entity, RuntimeProvider ABC, WorkspaceLease entity. RuntimeProviderHub. Alembic migration. Graph validator extension.
2. `task-e2b-runtime-provider` — E2BRuntimeProvider implementation. Sandbox lifecycle (create, exec, destroy). WorkspaceLease persistence. Integration test with live E2B API.
3. `task-shell-connector` — ShellConnector implementing ConnectorType `shell`. run_command, write_file, read_file, list_files. Command allowlist enforcement. Integration test inside WorkspaceLease.
4. `task-runtime-provider-ui` — EnvironmentProfile management UI (create, configure, test). Pipeline node binding picker for environment profiles. Run inspection: workspace lifecycle events.
5. `task-modulo-dogfood-pipeline` — The "Modulo builds Modulo" pipeline: reads a GitHub issue, generates code changes via LLM, applies via ShellConnector, runs `pytest`, creates a PR if tests pass. Delivered as a library workflow. HITL gate before PR creation.

---

## Consequences

- SSO, team management, eval system, and the rest of v1 are deprioritised until after phase-7a ships.
- The dogfooding pipeline becomes the most demanding integration test the platform has — it exercises ModelBackend, ConnectorHub (GitHub), ShellConnector, RuntimeProviderHub, HITL, and the full run lifecycle end-to-end.
- Once the dogfooding pipeline is running, future Modulo development can itself be driven through Modulo. That feedback loop is the point.
