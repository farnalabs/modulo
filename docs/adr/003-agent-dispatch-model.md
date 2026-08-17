# ADR 003  –  Agent Dispatch Model

**Date**: 2026-07-16
**Status**: Supersedes ADR 001 (the agent execution environment concept is replaced by the dispatch model)

---

## Context

ADR 001 defined the "Agent Execution Environment" model where Modulo agents would run inside sandboxed execution environments (E2B, Docker) with shell access via ShellConnector. The execution path was:

```
Agent definition in Modulo
        ↓
Modulo Agent Runtime / LangGraph
        ↓
E2B/Docker sandbox (ShellConnector)
        ↓
ModelBackend → model inference
        ↓
ConnectorHub → GitHub / filesystem / etc.
```

In this model, Modulo owned the entire agent runtime  –  tool calling, execution loops, shell access, and sandbox lifecycle. This was the wrong strategy.

**Why it was wrong:**

1. **Modulo competes with established agent runtimes.** Claude Code, opencode, Cursor, GitHub Copilot, and others are far better at tool-using execution loops. They have dedicated teams, years of UX refinement, and deep integration with their ecosystems. Modulo cannot and should not match them.

2. **The sandbox is a means, not the product.** Provisioning an E2B sandbox, managing workspace leases, enforcing command allowlists, and tracking shell sessions is infrastructure plumbing. It adds complexity without differentiating Modulo's value proposition.

3. **ShellConnector is a leaky abstraction.** It requires per-connector command allowlists, workspace lease management, runtime provider resolution, and environment profile configuration  –  all of which are incidental complexity for what should be a simple "dispatch and collect" pattern.

4. **Modulo's real value is upstream and downstream.** Modulo excels at pipeline definition, dispatch orchestration, auth and audit, cost tracking, eval gates, and HITL review  –  not at running agent execution loops.

---

## Decision

**Modulo dispatches work to external agent runtimes in sandboxes, then evaluates the output.** Modulo is the SDLC orchestration layer  –  it owns dispatch, auth, audit, cost tracking, eval gates, and HITL  –  not the agent loop itself.

### Architecture

#### New node type: `sandbox_agent`

A new pipeline node type that:

1. **Provisions an E2B sandbox** from a named template (e.g. `claude-code-v1`, `opencode-v1`, `generic-python`)
2. **Writes the prompt + context** into the sandbox as files
3. **Runs the external agent** (e.g. `claude --output-json /home/user/prompt.md`)
4. **Collects structured output** from a well-known path (`/home/user/output.json`)
5. **Tears down the sandbox** regardless of success or failure

#### Sandbox templates

Each sandbox template defines the agent runtime environment:

| Template ID | Agent | Description |
|---|---|---|
| `claude-code-v1` | Claude Code CLI | Includes Claude Code CLI, git, GitHub/Jira CLIs, common dev tools |
| `opencode-v1` | opencode CLI | Includes opencode, git, common dev tools |
| `generic-python` | Any Python agent | Python 3.12 + common SDLC libraries, no specific agent CLI |

Templates are E2B sandbox templates managed outside of Modulo (in E2B's template system). Modulo references them by ID.

#### Output contract

Every sandbox agent execution returns structured JSON:

```json
{
  "status": "completed" | "failed",
  "summary": "Description of what was done",
  "changed_files": ["path/to/file1.py", "path/to/file2.ts"],
  "pr_url": "https://github.com/org/repo/pull/123",
  "exit_code": 0,
  "wall_clock_time_ms": 45000
}
```

- All observable metrics (wall-clock time, exit code, output validity) are captured natively by Modulo  –  no follow-up step required.
- The output schema is validated at the pipeline level, not inside the sandbox.

#### Post-hoc eval

The agent's output is evaluated by a separate Modulo pipeline (e.g. code review, test coverage check, security scan). This keeps evaluation concerns separate from execution concerns and allows different evaluators to be composed independently.

#### Trending

Aggregated metrics per agent template:

- Success rate (outputs with `status: "completed"` / total dispatches)
- Mean wall-clock time
- Mean eval score (when post-hoc eval is configured)
- Failure reasons (timeout, parse error, non-zero exit)

---

## Consequences

### ShellConnector is deprecated

ShellConnector was built for ADR 001's model where Modulo agents would have shell access inside sandboxes. Under the new dispatch model, no Modulo agent runs inside a sandbox  –  the external agent runtime handles all file operations, git commands, and shell execution. ShellConnector will be removed in a future release.

### RuntimeProvider ABC, E2BRuntimeProvider, and WorkspaceLease remain useful

These provide sandbox lifecycle primitives (create sandbox, run command, destroy sandbox) that the `sandbox_agent` node type uses internally. The abstraction is sound  –  only ShellConnector's usage pattern was wrong.

### Modulo is no longer in the "agent runtime" business

Modulo does not compete with Claude Code, opencode, or Cursor. It orchestrates them. The `sandbox_agent` node type is a dispatch client, not an agent runtime. This reframing simplifies the product story and eliminates a class of infrastructure complexity.

### ShellConnector UI deprecation

Existing pipelines that use ShellConnector nodes will continue to execute, but the node type is deprecated in the UI. New pipelines should use `sandbox_agent` for code-generation tasks and `agent` (single-shot LLM call) for non-coding tasks.

### Graph validator changes

The graph validator must allow `sandbox_agent` as a valid node type alongside `agent`, `manual`, and `connector`. The `sandbox_agent` node type does not require `connector_binding` or `model_backend_id`  –  only `agent_prompt`, `template_id`, and optionally `output_schema_json`.

---

## Amendment (2026-08-17) — Tool-call dispatch interception inside the agent loop (FAR-211)

### Context

The original ADR 003 decision scoped Modulo's controls to the orchestration boundary: dispatch, auth, audit, cost, eval gates, and HITL. Guardrails (§8.17) enforce at the ingestion edge (run-creation) and at the webhook intake boundary, but the sandbox agent's INTERNAL LLM loop — tool results re-injected into the model mid-loop — sat between those boundary guardrails and any output gate with no control of its own.

The sandbox is Modulo-hosted (the E2B template, the prompt/context files, the agent command, and the output contract are all Modulo's). It is therefore NOT "unmediated external runtime": it is a covered boundary with no guardrail. The original "must delegate agent-internal behaviour" caveat named unmediated EXTERNAL runtimes; the Modulo-hosted sandbox is not one of them, so this amendment adds a Modulo-owned interception point INSIDE the loop.

### Decision

**A Modulo-owned interception bridge runs INSIDE the sandbox alongside the external agent, wrapping the agent's tool-call dispatch.** Each tool invocation is reported to the Modulo side BEFORE execution, and each tool result BEFORE it re-enters the model context.

Mechanics (first slice, FAR-211 T3):

1. **Sandbox-side bridge client** (`modulo.core.guardrails.sandbox_bridge`) is written INTO the sandbox by the `sandbox_agent` node runner, together with a bridge config (`loop_intercept` on the node's config). It is stdlib-only (Modulo is not installed inside the sandbox).
2. **Modulo-side callback server** (`modulo.core.guardrails.loop_intercept.LoopInterceptCallbackServer`) is hosted by the Modulo sandbox-agent process. The bridge client POSTs `{tool_name, args, direction, result_summary}` to it for each intercepted tool call; the server evaluates the event against the SAME bound guardrail rows as the T1 ingestion edge and returns a decision `{action: pass|block|warn|redact, masked_args, ...}`.
3. **Enforcement** happens client-side, inside the sandbox: `block` → the tool call is refused (the wrapper exits non-zero with a `MODULO_BRIDGE_BLOCKED` marker); `redact` → `masked_args` replace the args before execution; `warn`/`pass` → proceed.
4. **Wiring** is best-effort and additive: the bridge activates only when the node carries a `loop_intercept` config AND the pipeline has bound guardrails (zero guardrails → inert). A bridge setup/evaluation failure NEVER blocks the dispatch or wedges the loop — the call proceeds with a log + audit.

### Which tool calls are intercepted

Interception is opt-in per node via `intercepted_tool_patterns` (glob patterns matched against the tool name). The default set covers the high-risk tool families:

- **Connector-mediated writes** — `git push*`, `gh pr create*`, `gh issue create*`, `gh repo create*`, `gh api*`
- **Network egress** — `curl*`, `wget*`
- **Deployment / publishing / dependency mutation** — `fly deploy*`, `flyctl deploy*`, `docker push*`, `npm publish*`, `pip install*`

Read-only / local-only calls (`git status`, file reads within the workspace, `cat`) are low-risk and pass through without any evaluation cost.

### Per-call latency budget

Each interception round-trip runs under `asyncio.wait_for` with `latency_budget_ms` (default 250ms). **A slow bridge never blocks the agent**: on timeout or bridge error the call is ALLOWED — best-effort fail-open WITH a log + audit (`guardrail.loop_bridge_timeout`). The interior interception must never wedge the agent loop. Guardrail detection itself retains the T1 per-guardrail hard timeout (`guardrail_timeout_seconds`).

### Interaction with the T1 seam

- The T1 ingestion-edge pass (run-creation) is UNCHANGED and still runs first (pre-redaction).
- The interior interception is ADDITIVE: it reuses the SAME guardrail rows, the SAME engine (`EvalEngine` + `_detect_one`), and the SAME action semantics.
- `block` on the `before` direction → the tool call is NOT executed. `warn` → the call proceeds, the violation is recorded. `redact` → the payload is masked before the tool executes / before the result re-enters the model context.
- `block_on_guardrail: false` downgrades block-action guardrails to record-only inside the loop (never refuse).

### Known gap (explicit)

Interception is PREVENTIVE, not compensating: a tool call that ALREADY executed (or a tool result that already re-entered context) cannot be un-executed. An `after`-direction block is recorded, never refused. Compensation of already-performed side effects remains the separate run-termination compensation work (FAR-213, shipped in §8.17).

### Status of this amendment

Shipped (FAR-211 first slice): the bridge client, the Modulo-side evaluation, the callback server, graph validation of the `loop_intercept` config, per-call latency budget with fail-open+audit, org-scoped audit events (`guardrail.loop_blocked` / `guardrail.loop_warned` / `guardrail.loop_redacted` / `guardrail.loop_bridge_timeout`), and the node-runner wiring. Test-driven: the tests drive the bridge client directly with a stub agent command (no real opencode run); the opencode plugin hook that emits tool-call events mid-loop is the documented future integration point (the wrapper mode and event-marker protocol are shipped and tested).

---

## Migration

1. ADR 001 is marked as superseded by this document.
2. ShellConnector is deprecated with a runtime `DeprecationWarning`.
3. The `sandbox_agent` node type is added to `build_graph_from_json` in `graph_cache.py`.
4. Existing ShellConnector pipelines continue to work but will show a deprecation notice.
5. No data migration is required  –  ShellConnector configuration in pipeline snapshots remains readable.
6. The product map entry for `runtime-provider-core` is updated with a deprecation notice.
