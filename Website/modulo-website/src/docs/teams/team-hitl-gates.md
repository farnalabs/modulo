---
title: Team-Scoped HITL Gates
---

# Team-Scoped HITL Gates

Restrict HITL gate claim/approve to members of a specific team using `required_team_id`.

- Route: `/admin/hitl`
- API: `POST /api/v1/runs/{run_id}/hitl/{gate_id}/claim` — claim gate
- API: `POST /api/v1/runs/{run_id}/hitl/{gate_id}/approve` — approve gate
- API: `POST /api/v1/runs/{run_id}/hitl/{gate_id}/reject` — reject gate
- API: `POST /api/v1/runs/{run_id}/hitl/{gate_id}/deliver-manual` — manual delivery
- API: `GET /api/v1/hitl/pending` — list pending gates across org
- MCP: `list_pending_hitl` — agent-accessible pending gate list
- MCP: `review_hitl` — claim/approve/reject gate via agent
- PRD: §8.8, §9.3

See the [PRD §8.8](../../prd.md#88-feature-gating) and [PRD §9.3](../../prd.md#93-team-scoped-hitl-gates) for the full specification.
