# ADR 011 — Remy Context Sources: Configurable Knowledge Domains with Progressive Disclosure

**Date**: 2026-07-04  
**Status**: Active

---

## Context

Remy injects all active org-level and user-level skills into the system prompt unconditionally (§8.23). This works for small skill sets but does not scale:

1. **Token waste** — A 10-skill org with 500 tokens each burns 5,000 tokens per session just on skills, every turn
2. **No product knowledge** — Remy has no built-in understanding of what Modulo is, its concepts, pages, features, docs, or the user's current configuration
3. **No user control** — Users cannot choose what Remy knows. An admin who wants Remy to be lean ("don't load integration status, it never changes") has no mechanism
4. **No progressive disclosure** — Remy cannot retrieve knowledge on demand when it discovers it needs it

Three approaches were considered:

1. **RAG pipeline** — Embed all docs, skills, and config into a vector store. Remy retrieves relevant chunks on each query.
2. **Static system prompt expansion** — Hand-write a large system prompt with all product knowledge embedded. Update it manually.
3. **Context Sources with progressive disclosure** — Organise knowledge into named domains, each configurable as always-on, tool-based, or off. Auto-generate a product primer for always-on context. Provide MCP retrieval tools for on-demand knowledge.

## Decision

Use **Context Sources with progressive disclosure** (option 3).

### Why Not RAG

1. **Infrastructure cost** — Embeddings require a vector DB (pgvector, Qdrant, etc.), an embedding model API call per document, and chunking strategy tuning. Context sources need none of these.
2. **Retrieval quality uncertainty** — Vector search on documentation is non-deterministic. Remy might get an irrelevant chunk, miss the right one, or hallucinate from partial context. Explicit tool calls are deterministic and auditable.
3. **Explainability** — When Remy calls `get_documentation("deploy pipeline")`, the user sees the tool call in the event stream. With RAG, the knowledge appears as if by magic — opaque.
4. **Existing infra fits** — Remy already has an agentic loop with MCP tool calling. Adding retrieval tools is natural. No new infrastructure needed.
5. **User control** — RAG makes it hard to say "don't load this knowledge at all". With context sources, each domain has an explicit off switch.

### Why Not Static System Prompt

1. **Stale** — A hand-written prompt drifts from the actual product (new routes, renamed features, changed plan structures)
2. **No dynamic context** — Cannot include live counts (pipelines, connectors) or the user's current plan tier
3. **No user control** — Every user gets the same knowledge, whether they want it or not

### Why Tool-Based Retrieval Instead of Always-On

| Consideration | Always-on | Tool-based |
|---|---|---|
| Token cost per turn | High (proportional to knowledge size) | Zero until a tool is called |
| Latency | Zero (already in prompt) | One tool call round-trip |
| Remy awareness | Implicit — Remy "just knows" | Explicit — Remy must decide to call |
| Context pressure | Reduces space for conversation | No pressure until retrieval |
| Freshness | Stale until next session rebuild | Always fresh (queries live DB) |

The product primer (~700 tokens always-on) covers the baseline. Everything else moves to tools. This gives Remy enough context for competent conversation while keeping the per-turn budget for actual dialogue.

## What This Means for Code

| Concern | Approach |
|---|---|
| Source configuration | `RemyConfig.context_sources: dict[str, str]` field (source_key → mode) |
| Skill source mode | `remy_skills.source_mode` column: `always_on` \| `tool` \| `off` (default `always_on`) |
| System prompt composition | `SkillLoader.build_system_prompt()` filters by mode, injects primer, appends tool descriptions |
| Retrieval tools | 4 new MCP tools: `get_documentation`, `get_integration_status`, `get_org_config`, `get_available_features` |
| Documentation indexer | Startup or lazy process that chunks `docs/prd.md` + FAQ + website docs by heading |
| Primer generator | `backend/scripts/generate_remy_primer.py` — reads PRD glossary + manifest + live DB counts |
| Admin UI | "Knowledge Sources" section in `AdminRemyView.vue` with mode dropdowns + per-skill toggles |
| User UI | "Knowledge Sources" subtab in Remy panel settings with user-level overrides |
| Org-level defaults | Stored in `SystemConfig` as part of `RemyConfig` (existing mechanism) |
| User-level overrides | New `remy_context_sources` table, one row per (user_id, source_key) |
| Migration path | Existing skills default to `source_mode = always_on`. Built-in sources have hardcoded defaults. |

## When to Revisit

- An org reports that tool-based retrieval is too slow for their workflow (latency of two round-trips — "which tool do I need" + tool call). Move their sources to always-on.
- The documentation corpus grows beyond ~100 sections. Upgrade `get_documentation` from keyword search to embeddings-based retrieval (pgvector).
- A user reports that Remy consistently fails to call the right retrieval tool. Improve tool descriptions or add automatic context injection based on query intent detection.
- Multi-worker deployment requires the in-memory knowledge index to be shared (Redis cache or startup-loaded from DB).

## Related Documents

- PRD §8.23 — Remy In-App AI Assistant
- PRD §8.27 — Remy UI Commands
- PRD §8.28 — Core Shared Manifest
- PRD §8.29 — Remy Context Sources (this decision)
- PRD §8.30 — Remy Product Primer
- ADR 007 — Remy UI Commands: Frontend-Mediated Browser Automation
- ADR 008 — Core Shared Manifest
- `backend/src/modulo/core/remy/skill_loader.py` — System prompt builder
- `backend/src/modulo/core/remy/config_service.py` — RemyConfig model
- `backend/src/modulo/api/mcp_server.py` — MCP tool handlers (where retrieval tools land)
- `backend/src/modulo/api/routes/remy.py` — SSE stream handler (system prompt assembly entry point)
- `frontend/src/views/AdminRemyView.vue` — Admin config page
- `frontend/src/components/remy/RemyPanel.vue` — User-facing panel
- `frontend/src/components/remy/RemySkillManager.vue` — User skill editor
