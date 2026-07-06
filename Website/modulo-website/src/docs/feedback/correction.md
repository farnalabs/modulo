---
title: Feedback Correction
---

# Feedback Correction

Spawning and evaluation of correction runs for rejected pipeline outputs in the Feedback System.

- Route: `/admin/feedback`
- API: `POST /api/v1/runs/{run_id}/feedback` — create feedback record
- API: `GET /api/v1/feedback` — list feedback records
- API: `GET /api/v1/feedback/{record_id}` — get feedback record
- API: `PATCH /api/v1/feedback/{record_id}/status` — update status
- API: `POST /api/v1/feedback/{record_id}/detect-gap` — run eval gap detection
- API: `GET /api/v1/feedback/inbox` — feedback inbox
- API: `GET /api/v1/feedback/inbox/{record_id}` — inbox item detail
- API: `POST /api/v1/feedback/inbox/{record_id}/review` — review + optional correction run
- API: `GET /api/v1/feedback/proposals` — eval proposals queue
- PRD: §8.20

Supports three handler types: `human`, `ai_correction`, `ai_correction_with_human_review`.

See the [PRD §8.20](../../prd.md#820-feedback-system) for the full specification.
