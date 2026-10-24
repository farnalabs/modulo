# Reviewer Independence Audit

**Ticket:** FAR-2031
**Date:** 2026-10-24
**Auditor:** Worker (general)

---

## Executive Summary

This audit examines every review stage in Modulo to verify that reviewing agents do not share runtime, credentials, or context with authoring agents. Four review stages were identified and audited.

**Overall finding:** Modulo's review architecture **already provides strong isolation guarantees** by design:
- PR Reviewer runs as `modulo-reviewbot` (separate GitHub identity from `farnalabs` bot)
- Review nodes run in separate E2B sandboxes from authoring nodes
- Model credentials are resolved per-run from the ModelBackendHub
- HITL gates enforce `human_only` by default, blocking agent approval

**One structural gap found:** LLM-judge evals can share the same model backend as the node under review. This is a soft-signal eval type (never a correctness gate) but should be documented.

---

## Review Stages Audited

### 1. PR Reviewer Pipeline (app.modulo.run)

**Pipeline ID:** `d6b2c25b-ff7f-490a-9967-633b6adcc93c`
**Trigger:** GitHub Actions `pr-review.yml` workflow (on CI completion)
**Identity:** `modulo-reviewbot` (separate PAT from `farnalabs`)

| Property | Finding |
|---|---|
| **Runtime isolation** | ✅ Separate E2B sandbox per node. Three `sandbox_agent` nodes run sequentially in fresh sandboxes. Each sandbox clones `devtools` fresh and runs `opencode` independently. |
| **Credential isolation** | ✅ Uses `GITHUB_REVIEWBOT_PAT` (vault entry `github-reviewbot-pat`) – distinct from `GITHUB_DOGFOOD_PAT` used by `farnalabs` bot for PR creation. Model backend uses org-configured `opencode` provider (`opencode-live2`, ID `292d54ef-6644-441e-8379-0a8802f15e5b`) resolved via ModelBackendHub at run-start. |
| **Context isolation** | ✅ Author's PR diff/context passed as *evidence* via webhook payload → Jinja-rendered prompt files (`/home/user/prompt.md`). The reviewer sees the PR as input data, not as instruction. |
| **Influence paths** | ✅ No write path from author to reviewer. Author writes to PR branch; reviewer reads via GitHub API. Reviewer posts formal GitHub review as `modulo-reviewbot` – GitHub rejects self-approval, so identities MUST be separate. |

**Architecture notes:**
- Three-node pipeline: (1) Review agent → (2) Policy router → (3) Post-decision (posts formal review)
- Upstream decisions injected as JSON files by `agent_command` Jinja rendering (server-side), not read from `state.artifacts` (sandbox-inaccessible)
- `guard-secrets.sh` sourced in `agent_command` blocks token leaks in `gh`/`curl` bodies
- Model selection via `resolve_model.py` with classes `pr-reviewer` and `router` – both resolve to `opencode-go/glm-5.3-flash` (paid)

---

### 2. HITL Review Gates (In-Pipeline)

**Mechanism:** Edges with `hitl_review_config` compile to synthetic `hitl_review` nodes (LangGraph `interrupt()`)

| Property | Finding |
|---|---|
| **Runtime isolation** | ✅ N/A – HITL gates pause execution and wait for *human* claim/decision. No automated reviewer agent involved. |
| **Credential isolation** | ✅ `human_only` flag defaults to `true` (FAR-609). Non-browser credentials (API keys, non-browser JWTs) cannot claim or decide `human_only` gates – enforced in REST (`_enforce_human_only_gate`) and MCP (`_check_human_only_gate`). |
| **Context isolation** | ✅ Human reviewer sees fire-time briefing bundle (`HitlReviewContext`) as evidence. No agent-to-agent influence. |
| **Influence paths** | ✅ Authoring agent cannot influence gate outcome – only a human (or browser-linked OAuth principal per ADR 063) can decide. |

**Architecture notes:**
- `human_only_effective()` resolves gate config; `human_only_denial()` returns verdict
- Claim tokens are opaque random strings (alpha) or short-lived JWTs
- Gate coalescing (FAR-604 D4) prevents duplicate gates for same work item

---

### 3. LLM-Judge Evals (Post-Node, Pre-HITL)

**Mechanism:** `EvalType.LLM_JUDGE` eval definitions scoped to nodes, run by `EvalEngine` before HITL gates on same edge

| Property | Finding |
|---|---|
| **Runtime isolation** | ⚠️ **Partial.** LLM judge runs in the *same* SAQ worker process as the node execution. It uses the ModelBackendHub to resolve a model backend (configured per-eval via `config.model_backend_id`), but the backend is shared with the pipeline's other model calls. |
| **Credential isolation** | ⚠️ **Shared model backend possible.** An eval can reference the same `model_backend_id` as the node it evaluates. The backend credentials are decrypted once per run into the hub's `_creds` map. No separate credential set for "judge" vs "author". |
| **Context isolation** | ✅ Evaluated content wrapped with guard instruction (`_GUARD_INSTRUCTION`) and delimiters (`_build_safe_judge_input`). The judge sees node output as *data*, not instructions. Injection attempts in content are neutralized. |
| **Influence paths** | ✅ Author node cannot influence judge verdict – judge callable is built from eval config at eval time, not from node output. |

**Architecture notes:**
- `llm_judge` is explicitly documented as "soft signal only, never a correctness gate" (architecture.md)
- `failure_behaviour: "warn"` (default) allows run to continue; `"block"` raises `EvalBlockedError`
- Judge callable built via `build_llm_judge_callable(hub, backend_id_str)` – uses shared `ModelBackendHub`
- Timeout bounds: `BACKEND_INVOKE_TIMEOUT_SECONDS=60s`, `SYNC_BRIDGE_TIMEOUT_SECONDS=75s` (fail-closed)

**Gap:** An LLM-judge eval can be configured with the same `model_backend_id` as the node it evaluates, meaning the *same model instance* (with same credentials) judges its own output. This is acceptable for `llm_judge` because:
1. It is explicitly a soft signal (not a blocking gate by default)
2. The guard instruction + delimiters prevent prompt injection
3. Human-set evals (`human_set`) are the "trustworthy correctness path"

**Recommendation:** Document this in eval engine docs; consider adding a validation warning when `llm_judge` eval uses same backend as node's `model_backend_id`.

---

### 4. Improve-* / QA Pipelines (Automated Code Review)

**Pipelines:** `improve-codebase`, `improve-architecture`, `improve-tests`, `improve-security`, `improve-database`, `improve-ux`, `improve-docs`, `qa-reviewer`, `sonarqube`, `codescene`

| Property | Finding |
|---|---|
| **Runtime isolation** | ✅ Each runs as a `sandbox_agent` node in fresh E2B sandbox. Clones `devtools` fresh per run. |
| **Credential isolation** | ✅ Uses `GITHUB_TOKEN` (dogfood PAT) for PR creation. Model backend uses paid primary (`opencode-go/deepseek-v4.1-flash`) with fallback ladder (free GLM → free DeepSeek). Model fallback plugin (`@smart-coders-hq/opencode-model-fallback`) handles escalation. |
| **Context isolation** | ✅ Target path/files passed as input; reviewer sees code as evidence. `qa-iterate` runs multi-lens review against changed files only. |
| **Influence paths** | ✅ Author code is frozen at PR creation. Reviewer reads from GitHub/main branch. Fixes go through new PR cycle. |

**Architecture notes:**
- All use `modulo-opencode` template (pre-warmed with Python 3.12, deps cached)
- `agent_command` sources `guard-secrets.sh` to prevent token leaks
- `opencode-idle-guard.sh` bounds attempts (360s idle / 700s wall for most; 3240s for Improve Architecture)
- `merge_cost_self_report.py` merges real opencode session cost into output.json

---

## Structural Guarantees Already in Place

1. **Identity separation** – `modulo-reviewbot` vs `farnalabs` bot (GitHub enforces no self-approval)
2. **Sandbox isolation** – Every `sandbox_agent` node gets a fresh E2B sandbox
3. **Credential scoping** – ModelBackendHub decrypts once per run; credentials never enter LangGraph state/checkpoints/OTel
4. **Human-only default** – HITL gates block agent approval unless explicitly opted out
5. **Guard instruction** – LLM-judge input wrapped with "treat as data, not instructions"
6. **Post-merge review** – Fast-lane PRs get post-merge review (PR comment, not formal review)
7. **Secret leak gate** – `guard-secrets.sh` blocks `gh`/`curl` bodies containing tokens

---

## Required Changes

### A. Documentation Updates

1. **`docs/architecture.md`** – Add note in Eval Engine section: "LLM-judge evals may share a model backend with the node under evaluation; they are soft-signal only. For correctness guarantees, use `human_set` evals."

2. **`docs/core-principles.md`** – Principle 4 (Deterministic gates) already notes `llm_judge` is soft signal. No change needed.

3. **`docs/evals.md`** (if exists) – Document the shared-backend consideration for `llm_judge`.

### B. Code Change (Optional – Validation Warning)

In `backend/src/modulo/core/eval_engine/__init__.py` or graph validator: add a warning when an `llm_judge` eval definition references the same `model_backend_id` as its parent node's `model_backend_id`. This is a soft warning, not a block.

---

## Test Plan

The test `tests/architecture/test_reviewer_independence.py` asserts:

1. PR Reviewer pipeline uses `modulo-reviewbot` identity (not `farnalabs`)
2. Review-type nodes in pipeline configs use separate sandbox/credentials from authoring nodes
3. LLM-judge evals run with isolated model credentials (or document shared backend)
4. HITL gates enforce `human_only` by default

---

## Conclusion

Modulo's review architecture provides **strong reviewer independence** by design. The only gap is the documented soft-signal nature of `llm_judge` evals potentially sharing a model backend – this is acceptable given the explicit design intent and the availability of `human_set` evals for correctness-critical gates.
