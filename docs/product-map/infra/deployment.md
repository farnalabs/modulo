---
id: feat-infra-deployment
prd: 10
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
- [ ] Health status aggregation (currently separate from /healthz)
- [ ] Rollback history tracking

## Known Gaps

- **PRD alignment**: PRD §10 "Extensibility and Distribution" is the broadest applicable section. There is no PRD section dedicated to the deployment metadata endpoint — it is an internal infra concern documented in the product map for completeness.
- **BDD coverage partial**: 7 BDD scenarios exist in `backend/tests/bdd/features/deployment/metadata.feature` covering metadata shape, version, uptime, environment defaults, build metadata fields, and env var fallbacks. Missing: CI job URL field validation, non-development environment override, and error responses (missing auth, internal error).
- **No health status aggregation**: The deployment endpoint is separate from `/healthz`. There is no combined endpoint that returns both deployment metadata and health status.
- **No rollback history tracking**: The endpoint returns only current-deployment metadata. No history of previous deployments or rollback state is tracked.
