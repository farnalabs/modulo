# ADR 003 — Agent Dispatch Model

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

In this model, Modulo owned the entire agent runtime — tool calling, execution loops, shell access, and sandbox lifecycle. This was the wrong strategy.

**Why it was wrong:**

1. **Modulo competes with established agent runtimes.** Claude Code, opencode, Cursor, GitHub Copilot, and others are far better at tool-using execution loops. They have dedicated teams, years of UX refinement, and deep integration with their ecosystems. Modulo cannot and should not match them.

2. **The sandbox is a means, not the product.** Provisioning an E2B sandbox, managing workspace leases, enforcing command allowlists, and tracking shell sessions is infrastructure plumbing. It adds complexity without differentiating Modulo's value proposition.

3. **ShellConnector is a leaky abstraction.** It requires per-connector command allowlists, workspace lease management, runtime provider resolution, and environment profile configuration — all of which are incidental complexity for what should be a simple "dispatch and collect" pattern.

4. **Modulo's real value is upstream and downstream.** Modulo excels at pipeline definition, dispatch orchestration, auth and audit, cost tracking, eval gates, and HITL review — not at running agent execution loops.

---

## Decision

**Modulo dispatches work to external agent runtimes in sandboxes, then evaluates the output.** Modulo is the SDLC orchestration layer — it owns dispatch, auth, audit, cost tracking, eval gates, and HITL — not the agent loop itself.

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

- All observable metrics (wall-clock time, exit code, output validity) are captured natively by Modulo — no follow-up step required.
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

ShellConnector was built for ADR 001's model where Modulo agents would have shell access inside sandboxes. Under the new dispatch model, no Modulo agent runs inside a sandbox — the external agent runtime handles all file operations, git commands, and shell execution. ShellConnector will be removed in a future release.

### RuntimeProvider ABC, E2BRuntimeProvider, and WorkspaceLease remain useful

These provide sandbox lifecycle primitives (create sandbox, run command, destroy sandbox) that the `sandbox_agent` node type uses internally. The abstraction is sound — only ShellConnector's usage pattern was wrong.

### Modulo is no longer in the "agent runtime" business

Modulo does not compete with Claude Code, opencode, or Cursor. It orchestrates them. The `sandbox_agent` node type is a dispatch client, not an agent runtime. This reframing simplifies the product story and eliminates a class of infrastructure complexity.

### ShellConnector UI deprecation

Existing pipelines that use ShellConnector nodes will continue to execute, but the node type is deprecated in the UI. New pipelines should use `sandbox_agent` for code-generation tasks and `agent` (single-shot LLM call) for non-coding tasks.

### Graph validator changes

The graph validator must allow `sandbox_agent` as a valid node type alongside `agent`, `manual`, and `connector`. The `sandbox_agent` node type does not require `connector_binding` or `model_backend_id` — only `agent_prompt`, `template_id`, and optionally `output_schema_json`.

---

## Migration

1. ADR 001 is marked as superseded by this document.
2. ShellConnector is deprecated with a runtime `DeprecationWarning`.
3. The `sandbox_agent` node type is added to `build_graph_from_json` in `graph_cache.py`.
4. Existing ShellConnector pipelines continue to work but will show a deprecation notice.
5. No data migration is required — ShellConnector configuration in pipeline snapshots remains readable.
6. The product map entry for `runtime-provider-core` is updated with a deprecation notice.
