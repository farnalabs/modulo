---
title: Workflow Import / Export
---

# Workflow Import / Export

Bundle-based pipeline portability — export a pipeline as a `.modulo.zip` bundle, import it into another organisation with schema/connector/model backend binding.

- API: `POST /api/v1/libraries/export/{pipeline_id}` — export pipeline as `.modulo.zip`
- API: `POST /api/v1/libraries/import/upload-zip` — upload and analyse bundle
- API: `POST /api/v1/libraries/import/analyse` — analyse raw bundle JSON
- API: `POST /api/v1/libraries/import/confirm` — confirm and materialise import
- PRD: §8.15

The export strips org-private data (`owner_team_id`, credentials, ciphertexts). The import wizard resolves connector types, abstract schemas, and model backends to local equivalents, with name conflict disambiguation.

See the [PRD §8.15](../../prd.md#815-workflow-importexport) for the full specification.
