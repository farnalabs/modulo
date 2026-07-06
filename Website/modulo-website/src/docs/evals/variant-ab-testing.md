---
title: Variant A/B Testing
---

# Variant A/B Testing

Weighted A/B testing for pipeline variants — compare model backends side by side with weighted selection, eval coverage analysis, and prompt version comparison.

- Route: `/variants/ab-test`
- API: `POST /api/v1/variant-groups` — create variant group
- API: `GET /api/v1/variant-groups` — list variant groups
- API: `GET /api/v1/variant-groups/{id}` — get variant group
- API: `PUT /api/v1/variant-groups/{id}` — update variant group
- API: `DELETE /api/v1/variant-groups/{id}` — delete variant group
- API: `POST /api/v1/variant-groups/{id}/run` — trigger weighted variant run
- API: `GET /api/v1/variant-groups/{id}/coverage-gaps` — eval coverage analysis
- API: `GET /api/v1/variant-groups/{id}/prompt-diffs` — prompt version comparison
- API: `GET /api/v1/runs/{id}` — get run details
- API: `GET /api/v1/runs/{id}/io` — get run node outputs
- API: `GET /api/v1/runs/{id}/evals` — get run eval results
- PRD: §8.19

Supports weighted random selection, single-variant short-circuit, degraded evals mode,
and concurrent run quota enforcement. Variants are stored as JSON on the VariantGroup model.

See the [PRD §8.19](../../prd.md#819-variant-ab-testing) for the full specification.
