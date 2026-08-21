---
id: feat-infra-deployment
prd: 10, 11
delivery-tasks: []
code:
  - backend/src/modulo/api/routes/deployment.py
bdd:
  - backend/tests/bdd/features/deployment/metadata.feature
unit-tests:
  - backend/tests/unit/api/test_deployment_endpoint.py
depends-on: []
status: partial
---

# Deployment Metadata

Public endpoint returning build and runtime metadata for the current deployment: version, uptime, Python version, hostname, environment, git commit SHA/branch/timestamp/message, and build timestamp. Values are injected via Docker build args in CI/CD.

## Behaviours

- [x] GET deployment metadata as JSON
- [x] Version from `modulo.version.get_version()`
- [x] Uptime tracking
- [x] Git metadata from build args (empty fallback)
- [x] CI job URL from env var
- [ ] Health status aggregation — no combined deployment+health endpoint; PRD §10/§11 do not require one (internal infra concern, see Known Gaps)
- [ ] Rollback history tracking — no deployment history is persisted; PRD §10/§11 do not require it (internal infra concern, see Known Gaps)

## Known Gaps

- **PRD alignment**: PRD §10 "Extensibility and Distribution" is the broadest applicable section. There is no PRD section dedicated to the deployment metadata endpoint — it is an internal infra concern documented in the product map for completeness.
- **Health status aggregation (NOT PRD-required)**: The deployment endpoint is separate from `/healthz`. There is no combined endpoint that returns both deployment metadata and health status. `GET /api/v1/deployment` intentionally returns metadata only; readiness lives in `GET /healthz/ready` (`backend/src/modulo/api/routes/health.py`) and is consumed by the deployment orchestrator (Fly bluegreen, `fly.toml` `http_service.checks`). Building a combined endpoint is out of PRD scope — left as a deliberate non-gap rather than invented feature work.
- **Rollback history tracking (NOT PRD-required)**: The endpoint returns only current-deployment metadata. No history of previous deployments or rollback state is tracked. PRD §10/§11 specify no rollback-history requirement — out of scope.
- **BDD coverage partial**: 7 BDD scenarios exist in `backend/tests/bdd/features/deployment/metadata.feature` covering metadata shape, version, uptime, environment defaults, build metadata fields, and env var fallbacks. Missing: CI job URL field validation, non-development environment override, and error responses (missing auth, internal error).

## QA History

- 2026-08-15: Coverage-verification sweep. Confirmed all 5 checked behaviours are implemented and BDD/unit covered (`test_deployment_endpoint.py` + `metadata.feature`). Verified the 2 unchecked behaviours are NOT PRD-required: health status aggregation and rollback history tracking are internal infra aspirations absent from PRD §10/§11, so they remain unchecked Known Gaps rather than implementation work. Status: partial (5/7, both unchecked items documented as out-of-PRD-scope).
