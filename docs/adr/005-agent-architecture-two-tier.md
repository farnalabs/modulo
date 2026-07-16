# ADR 005 — Agent Architecture: Two-Tier Orchestration + Execution

**Date**: 2026-07-16
**Status**: Accepted

---

## Context

Since ADR 003 established the Agent Dispatch Model, we've been iterating on how Modulo should interact with external agent runtimes. The initial implementation used hand-written Python scripts running inside E2B sandboxes. While functional, this approach has several problems:

1. **Python scripts duplicate agent runtime capabilities.** Modulo shouldn't write its own code review logic — that's what opencode/Claude Code exist for.
2. **Script delivery is fragile.** Base64 encoding in JSON, multi-layer escaping (PowerShell → Python → JSON → API → PostgreSQL → Python → Shell → E2B), and template rebuilds create a brittle pipeline.
3. **Skills define the contract, scripts implement it.** The `pr-review` skill should be the source of truth. The implementation should be the agent runtime (opencode) executing that skill.

## Research Findings

### How the ecosystem builds AI agents (2026)

We surveyed the agent-building landscape. The dominant pattern across LangChain, CrewAI, Semantic Kernel, and E2B's own examples is a **two-tier architecture**:

```
Tier 1: Orchestration Layer
  - State machine / DAG (LangGraph, Temporal, Prefect)
  - Tool routing, memory management, HITL gates
  - Cost tracking, eval, observability
  - Owned by: Modulo

Tier 2: Agent Runtime Layer  
  - The actual LLM + tool-calling loop
  - Reads files, writes code, runs shell commands
  - Iterates based on feedback
  - Owned by: opencode, Claude Code, Cursor, etc.
```

| Team/Product | Orchestration | Agent Runtime | Sandbox |
|---|---|---|---|
| Klarna | Internal DAG | Custom LLM router | Kubernetes |
| Replit | Internal agent loop | Codey (custom) | gVisor containers |
| LangChain | LangGraph | AgentExecutor | Docker/E2B |
| E2B examples | Custom Python | Any LLM SDK | E2B sandbox |
| **Modulo** | **LangGraph (Pipeline Engine)** | **opencode/lildax** | **E2B sandbox** |

### How agent runtimes are invoked

All major agent runtimes follow a server-based model:

| Runtime | Invocation Pattern |
|---|---|
| Claude Code | `claude` (interactive) → no stable headless mode |
| opencode v2 (`lildax`) | `lildax serve` → `lildax api POST /chat/completions` → `lildax service stop` |
| Continue.dev | VS Code extension + API server |
| Cursor | Proprietary, no headless API |

**No major coding agent provides a one-shot headless CLI.** They all use a daemon/server architecture. The `lildax serve` → API → stop pattern is the standard approach.

### E2B template ecosystem

E2B provides:
- Base templates: `cloudflare/sandbox`, `base` (Debian + Python + Node)
- SDK-based template building (v2 API with `TemplateBuilder`)
- No pre-built "opencode-in-a-box" template exists publicly

---

## Decision

### Architecture: Two-Tier Dispatch

Modulo adopts the two-tier architecture:

```
Tier 1 (Modulo Pipeline Engine):
  LangGraph state machine
  → Auth, audit, cost tracking, HITL, eval
  → Routes to sandbox_agent nodes

Tier 2 (E2B Sandbox + Agent Runtime):
  Pre-built template with opencode CLI
  → lildax serve → prompt → lildax api → result → lildax service stop
  → Returns structured output to Modulo
```

### Template Strategy

We maintain a family of E2B templates under the `modulo/` namespace:

| Template | Contents | Use Case |
|---|---|---|
| `modulo-agent` | git + jq + python3 + modulo-wrap.sh | Basic ops, Python scripts |
| `modulo-agent-lildax` | All of above + `@opencode-ai/cli` (lildax) | Code review, code gen, any opencode-powered task |

The `modulo-agent-lildax` template is the recommended default for new sandbox_agent nodes. The base `modulo-agent` remains for lightweight tasks.

### Skill-Driven Reviews

The `pr-review` skill at `.agents/skills/pr-review/SKILL.md` is the single source of truth for what a PR review entails. The lildax agent invocation reads this skill and executes it. The Python script implementation is a fallback for environments where lildax is unavailable.

### Model Configuration

lildax requires a model backend API key. For Modulo's use:
- The `APP_MODULO_OPENCODE_API_KEY` can be passed to the sandbox via `env_vars`
- lildax reads `OPENCODE_API_KEY` or model-specific env vars for configuration
- Future: Modulo could expose a model backend proxy that sandboxes can call, avoiding the need to distribute raw API keys

---

## Consequences

### Positive

- **Modulo stops duplicating agent runtime capabilities.** opencode handles tool-using loops; Modulo handles orchestration.
- **Skills are the source of truth.** The `pr-review` skill defines the review; the implementation is an execution detail.
- **Template rebuilds are infrequent.** Script changes don't require template rebuilds.
- **The architecture matches ecosystem standards.** Two-tier orchestration+execution is the proven pattern.

### Negative

- **Sandbox startup overhead.** `lildax serve` takes ~3 seconds to start, adding latency to each dispatch.
- **Template management burden.** Two templates to maintain instead of one.
- **Model key distribution.** API keys must be passed to sandboxes, increasing the blast radius of a key leak.

### Migration

1. Build `modulo-agent-lildax` template with `@opencode-ai/cli` pre-installed
2. Update the `pr-review` skill with lildax-specific invocation instructions
3. Create a new sandbox_agent test pipeline that uses lildax
4. Keep the Python script review as a fallback implementation
5. Ship both templates; document which to use when

### Open Questions

- How does lildax authenticate with model providers in a sandbox? Does it read env vars, config files, or CLI flags?
- Can lildax reuse an existing Modulo model backend connection instead of requiring its own API key?
- Should we build a light-weight proxy in Modulo that sandboxes can call for model inference?

---

## References

- ADR 003: Agent Dispatch Model
- ADR 004: Agent as Self-Contained Bundle
- E2B Template SDK: https://e2b.dev/docs/template/
- opencode CLI: `@opencode-ai/cli` v1.18.3
- LangGraph: https://langchain-ai.github.io/langgraph/
