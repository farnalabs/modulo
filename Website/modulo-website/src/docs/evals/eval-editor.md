---
title: Eval Editor
---

# Eval Editor

CRUD UI for pipeline eval definitions — create, edit, and delete evals scoped to a pipeline and optionally a node.

- Route: `/evals/editor`
- API: `GET /api/v1/evals` — list eval definitions
- API: `POST /api/v1/evals` — create eval definition (admin only)
- API: `PUT /api/v1/evals/{id}` — update eval definition (admin only)
- API: `DELETE /api/v1/evals/{id}` — delete eval definition (admin only)
- PRD: §8.17

Supports four eval types: `llm_judge`, `regex`, `json_schema`, `custom_function`.

See the [PRD §8.17](../../prd.md#817-eval-system) for the full specification.
