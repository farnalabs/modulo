---
id: feat-infra-deployment
prd: 10.3a
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

- **PRD alignment**: PRD §10.3a "Infrastructure" covers general infra checklist items (Docker Compose, startup sequence, auth) but has no dedicated section specifying the deployment metadata endpoint. The previous reference to PRD §10.5 was incorrect — that section covers Opt-In Telemetry only.
- **No BDD tests**: The deployment endpoint has unit tests but no BDD/Gherkin feature files. BDD scenarios should cover: metadata shape, env-var-driven fields, fallback defaults, and uptime monotonicity.
- **No health status aggregation**: The deployment endpoint is separate from `/healthz`. There is no combined endpoint that returns both deployment metadata and health status.
- **No rollback history tracking**: The endpoint returns only current-deployment metadata. No history of previous deployments or rollback state is tracked.
