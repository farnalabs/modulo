# ADR 004 — Agent as a Self-Contained Bundle

**Date**: 2026-07-16
**Status**: Accepted

---

## Context

ADR 003 established that Modulo dispatches work to external agent runtimes in E2B sandboxes rather than running agents itself. The `sandbox_agent` node type was delivered as the first implementation.

However, the current design has the `template_id` and `agent_command` fields on the **pipeline node** (`node_def`), not on the **Agent entity** itself. This means:

1. **An Agent is not self-contained.** You can define an Agent (prompt + schemas) but the runtime details (what image to use, what command to run) live elsewhere.
2. **Library copies are broken.** If you copy an Agent from the library, the runtime config doesn't travel with it — the copy is non-functional until manually reconfigured.
3. **There's no onboarding path.** A new user has no "default" agent to start from. They must understand E2B templates, agent commands, and sandbox config before their first pipeline run.

The user workflow we want to enable is:

> "Tell Modulo where to execute agents (E2B API key). It seeds a working example. Run it. Copy it. Tweak the prompt. Swap the schemas. Swap the image. Done."

---

## Decision

An Agent becomes a self-contained bundle of six fields:

| Field | Source | Description |
|---|---|---|
| `name` | Agent entity | Human-readable label |
| `prompt_template` | Agent entity | Jinja2 template rendered against run context |
| `input_schema_id` | Agent entity (optional) | Schema for what the agent expects as input |
| `output_schema_id` | Agent entity (optional) | Schema for what the agent returns |
| `template_id` | Agent entity (optional) | E2B sandbox template / Docker image ref |
| `agent_command` | Agent entity (optional) | Shell command to execute inside the sandbox |

All six fields travel together on copy, export, and import. Schemas remain independent, versioned primitives in the schema registry — they are *referenced by ID*, not embedded.

---

## Architecture

### Agent entity changes

Add two optional fields to the Agent ORM model:

```python
class Agent(Base):
    # Existing fields...
    template_id: Mapped[str | None]      # E2B template ID or Docker image ref
    agent_command: Mapped[str | None]    # Default: "opencode --output-json /home/user/prompt.md"
```

Both fields are nullable (`None` = agent is a non-sandbox, single-shot LLM agent). When both are set, the Agent can be used with a `sandbox_agent` pipeline node.

### Pipeline node resolution

The `sandbox_agent` node's `make_node_fn` reads the Agent entity at compile time:

```
node_def.agent_id → Agent entity → {
    prompt_template, template_id, agent_command,
    input_schema_id, output_schema_id
}
```

If `node_def` explicitly provides `template_id` or `agent_command`, those override the Agent-level values (allows per-node overrides without creating a new Agent version).

### Schema sharing

Schemas are already independent, versioned primitives. Two agents can reference the same `output_schema_id` — the exact same object, not a copy. When Schema v3 is bumped to v4:
- New snapshots pin v4
- Old snapshots still validate against v3
- Both agents automatically pick up the new version on their next pipeline save

### Library copy

The existing library copy primitive already clones Agent entities. With `template_id` and `agent_command` on the Agent, a copied Agent is immediately functional — no manual reconfiguration needed.

---

## Published reference images

We will publish a family of Docker images to Docker Hub under the `modulo/` org:

| Image | Agent | Description |
|---|---|---|
| `modulo/example-agent:latest` | opencode | General-purpose coding agent. Reads `/home/user/prompt.md`, writes `/home/user/output.json`. Includes opencode CLI, git, Node.js, Python. |
| `modulo/example-agent:minimal` | opencode (slim) | Same but minimal footprint. No git, no Python. Just Node.js + opencode. |

The reference image includes `modulo-wrap`, a small entrypoint wrapper that:

1. Runs the configured agent command
2. Captures exit code and wall-clock time
3. Reads the agent's `output.json`
4. Augments it with `_telemetry` (wall-clock time, exit_code, sandbox_id, template_version)
5. Writes the augmented output to `output.json`
6. Exits with the agent's exit code

Users are free to ignore `modulo-wrap` and write their own entrypoint. The contract is the same: write structured JSON to `/home/user/output.json`.

### When to build these

The images will be built and published as a follow-up delivery (not part of this ADR). They are referenced here as the target workflow — the Agent entity changes in this ADR are designed to support this workflow without yet shipping the images.

---

## User workflow

### Onboarding

1. User sets `MODULO_E2B_API_KEY` in their Modulo environment
2. Modulo seeds two library primitives:
   - **"Getting Started Agent"** — references no image (single-shot LLM), translates English to French
   - **"Getting Started Pipeline"** — one-node pipeline with the above agent + input/output schemas
3. User clicks "Run" — pipeline executes, agent translates, output shown in run inspection
4. User sees it works end-to-end

### Adapt for real work

5. User copies "Getting Started Agent" → "My Code Reviewer"
6. Changes the prompt: `"Review this PR diff: {{ input.diff }}"`
7. Changes `template_id` to their own E2B image
8. Changes `output_schema_id` to a custom schema they defined: `ReviewResult`
9. Done — the agent now runs their custom image with their prompt and returns structured data matching their schema

### Escape hatch

10. User hits a wall — the agent can't measure something their image doesn't expose
11. They build their own Docker image, push it to their registry, update `template_id`
12. No pipeline changes needed — the Agent definition is the interface

---

## Consequences

### Positive

- **Agents are truly portable** — copy, export, import, all config travels together
- **Schemas are genuinely shared** — no copies, no drift, no duplication
- **Onboarding is linear** — API key → example → run → copy → adapt → own
- **Existing agents unchanged** — `template_id: null` means single-shot LLM, backward compatible
- **Pipeline snapshots pin schema versions** — shared schema bumps don't break old runs

### Migration

1. Add `template_id` and `agent_command` columns to the `agents` table (nullable, no default)
2. Update the Agent CRUD API to accept the new fields
3. Update `make_sandbox_agent_fn` to resolve from Agent entity (fall back to `node_def` for backward compatibility)
4. Update the library service to include the new fields in Agent copy/export
5. Build and publish `modulo/example-agent` images (follow-up)
6. Seed "Getting Started Agent" and "Getting Started Pipeline" in the library (follow-up)

### Open questions

- Should `agent_command` have a system default (e.g. `"opencode --output-json /home/user/prompt.md"`) when both `template_id` is set but `agent_command` is null? Current thinking: yes — this makes the two-field combination "use the default command with this image."
- Should `template_id` accept Docker Hub image refs directly (e.g. `modulo/example-agent`), or only E2B template IDs? Current thinking: both — E2B resolves image refs to templates transparently.

---

## Status of ADR 001

ADR 001 is superseded by ADR 003. ADR 004 builds on ADR 003's dispatch model and does not conflict with it. The RuntimeProvider ABC, E2BRuntimeProvider, and WorkspaceLease entities remain useful as internal sandbox lifecycle primitives.
