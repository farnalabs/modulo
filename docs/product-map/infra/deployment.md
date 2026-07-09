---
id: feat-infra-deployment
prd: 10.5
code:
  - backend/src/modulo/api/routes/deployment.py
bdd: []
unit-tests: []
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
