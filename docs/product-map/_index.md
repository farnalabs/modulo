# Product Map

Every feature delivered, grouped by domain. Each entry lists expected behaviours — happy paths, edge cases, error messages, and boundaries — and links to the PRD section, BDD tests, code, and delivery tasks that cover it.

This is the graph: entries are nodes, frontmatter fields are typed edges.

## How to read an entry

```yaml
---
id: feat-<domain>-<feature>        # unique node ID
prd: 8.N                           # PRD section
delivery-tasks: [task-...]         # delivery plan task IDs
bdd: [path/to/feature.feature]     # BDD feature file(s) (optional: missing = gap)
unit-tests: [path/to/test.py]      # unit test files (optional)
code: [path/to/src/]               # code paths implementing this feature
depends-on: [feat-...]             # prerequisite features
status: covered | partial | gap    # auto-updated by graph-validate
---

## Index

### Auth and Security
- [feat-auth-sso-provider-ui](auth/sso-provider-ui.md) => PRD 9.4
- [feat-auth-scim-provisioning](auth/scim-provisioning.md) => PRD 9.2, 9.4
- [feat-auth-team-rbac](auth/team-rbac.md) => PRD 9.2, 9.3
- [feat-auth-team-api-keys](auth/team-api-keys.md) => PRD 9.3
- [feat-auth-mcp-oauth](auth/mcp-oauth.md) => PRD 6.4
- [feat-auth-jwt-auth](auth/jwt-auth.md) => PRD 7.10
- [feat-auth-rate-limiting](auth/rate-limiting.md) => PRD 7.18
- [feat-auth-password-change](auth/password-change.md) => PRD 9.4

### Connectors
- [feat-connectors-linear](connectors/linear-connector.md) => PRD 8.6
- [feat-connectors-jira](connectors/jira-connector.md) => PRD 8.6
- [feat-connectors-slack](connectors/slack-connector.md) => PRD 8.6
- [feat-connectors-schema-inference](connectors/schema-inference.md) => PRD 8.16
- [feat-connectors-github](connectors/github-connector.md) => PRD 8.6
- [feat-connectors-hub](connectors/connector-hub.md) => PRD 8.6
- [feat-integration-tiering](connectors/integration-tiering.md) => PRD 8.6
- [feat-connectors-gitlab](connectors/gitlab-connector.md) => PRD 8.6

### Core Platform
- [feat-core-rollback-agent-replacement](core/rollback-agent-replacement.md) => PRD 8.4
- [feat-core-replace-step-agent](core/replace-step-agent.md) => PRD 8.4
- [feat-core-registry-protocol-v2](core/registry-protocol-v2.md) => PRD 8.14
- [feat-core-run-context](core/run-context.md) => PRD 8.18
- [feat-core-runtime-provider-core](core/runtime-provider-core.md) => PRD 6
- [feat-core-runtime-config](core/runtime-config.md) => PRD 6
- [feat-core-run-retention](core/run-retention.md) => (no dedicated PRD section)
- [feat-core-lifecycle-maps](core/lifecycle-maps.md) => PRD 8.31
- [feat-core-pipeline-execution](core/pipeline-execution.md) => PRD 8.4
- [feat-core-oidc-integration](core/oidc-integration.md) => PRD 9.4, 6.2, 9.2
- [feat-core-notifications](core/notifications.md) => PRD 8.11
- [feat-core-pkg0-celery-optional](core/pkg0-celery-optional.md) => PRD 8.5
- [feat-core-quality-report-slack](core/quality-report-slack.md) => PRD 8.6, 8.11
- [feat-core-prompt-optimization](core/prompt-optimization.md) => PRD 8.2
- [feat-core-polling-trigger](core/polling-trigger.md) => PRD 8.5
- [feat-core-tier-catalog](core/tier-catalog.md) => PRD 6
- [feat-core-system-config](core/system-config.md) => PRD 6
- [feat-core-soc2-evidence-export](core/soc2-evidence-export.md) => PRD 8.12
- [feat-core-trigger-system](core/trigger-system.md) => PRD 8.5
- [feat-core-viewmodel-current](core/viewmodel-current.md) => PRD 6
- [feat-core-view-modes](core/view-modes.md) => PRD 8.21
- [feat-core-verified-publishers](core/verified-publishers.md) => PRD 8.14
- [feat-core-schema-inference](core/schema-inference.md) => PRD 8.16
- [feat-core-schema-inference-ui](core/schema-inference-ui.md) => PRD 8.16
- [feat-core-saml-integration](core/saml-integration.md) => PRD 9.4, 6.2, 9.2
- [feat-core-schema-system](core/schema-system.md) => PRD 8.3
- [feat-core-shared-manifest](core/shared-manifest.md) => PRD 8.28
- [feat-core-secrets-backend](core/secrets-backend.md) => PRD 7.13
- [feat-core-schema-diff](core/schema-diff.md) => PRD 8.3
- [feat-core-schema-versioning](core/schema-versioning.md) => PRD 8.3
- [feat-core-schema-deletion](core/schema-deletion.md) => PRD 8.3
- [feat-core-schema-import-export](core/schema-import-export.md) => PRD 8.3
- [feat-core-schema-union-types](core/schema-union-types.md) => PRD 8.3
- [feat-core-contribution-provenance](core/contribution-provenance.md) => PRD 8.14
- [feat-core-contribute-primitive](core/contribute-primitive.md) => PRD 8.14
- [feat-core-backup-restore](core/backup-restore.md) => PRD 6.2
- [feat-core-contribution-update](core/contribution-update.md) => PRD 8.14
- [feat-core-db-abstraction-core](core/db-abstraction-core.md) => PRD 6.1, 6.2
- [feat-core-cost-breakdown](core/cost-breakdown.md) => PRD 8.10
- [feat-core-core](core/core.md) => PRD 8.16, 8.4
- [feat-core-alpha-exit-verification](core/alpha-exit-verification.md) => PRD 10.3b
- [feat-core-ai-schema-gen](core/ai-schema-gen.md) => PRD 8.16
- [feat-core-agent-model](core/agent-model.md) => PRD 8.2
- [feat-core-api-versioning](core/api-versioning.md) => PRD 6
- [feat-core-audit-viewer-ui](core/audit-viewer-ui.md) => PRD 8.12
- [feat-core-audit-trail](core/audit-trail.md) => PRD 8.12
- [feat-core-audit-crypto-chain](core/audit-crypto-chain.md) => PRD 8.12
- [feat-core-migration-cli](core/migration-cli.md) => PRD 6.2
- [feat-core-langgraph-runtime](core/langgraph-runtime.md) => PRD 6.5
- [feat-core-in-app-notifications](core/in-app-notifications.md) => PRD 8.11
- [feat-core-model-failover](core/model-failover.md) => PRD 8.1
- [feat-core-node-categories](core/node-categories.md) => PRD 8
- [feat-core-navigation-restructure](core/navigation-restructure.md) => PRD 8.26
- [feat-core-multi-backend-tests](core/multi-backend-tests.md) => PRD 6.2, 12
- [feat-core-email-config](core/email-config.md) => PRD 8.11
- [feat-core-determination](core/determination.md) => PRD 8.16
- [feat-core-db-abstraction-remaining](core/db-abstraction-remaining.md) => PRD 6.1, 6.2
- [feat-core-feature-flag-ui](core/feature-flag-ui.md) => PRD 6.2,6.3
- [feat-core-hitl-effort-trend](core/hitl-effort-trend.md) => PRD 8.8
- [feat-core-helm-chart](core/helm-chart.md) => PRD 11
- [feat-core-feedback-correction](core/feedback-correction.md) => PRD 8.20

### Evals and Feedback
- [feat-evals-feedback-proposals](evals/feedback-proposals.md) => PRD 8.20
- [feat-evals-feedback-loop](evals/feedback-loop.md) => PRD 8.20
- [feat-evals-eval-testing](evals/eval-testing.md) => PRD 8.17
- [feat-evals-feedback-records](evals/feedback-records.md) => PRD 8.20
- [feat-evals-variant-coverage](evals/variant-coverage.md) => PRD 8.19
- [feat-evals-okr-eval-alignment](evals/okr-eval-alignment.md) => PRD 8.17
- [feat-evals-feedback-routing](evals/feedback-routing.md) => PRD 8.20
- [feat-evals-eval-engine](evals/eval-engine.md) => PRD 8.17
- [feat-evals-eval-definitions](evals/eval-definitions.md) => PRD 8.17
- [feat-evals-conditional-transitions](evals/conditional-transitions.md) => PRD 8.17
- [feat-evals-eval-gates](evals/eval-gates.md) => PRD 8.17
- [feat-evals-system](evals/eval-system.md) => PRD 8.17
- [feat-evals-eval-regression-alerts](evals/eval-regression-alerts.md) => PRD 8.17
- [feat-evals-eval-packaging](evals/eval-packaging.md) => PRD 8.17

### Frontend
- [feat-frontend-feedback-routing](frontend/feedback-routing.md) => PRD 8.20
- [feat-frontend-ownership-picker](frontend/ownership-picker.md) => PRD 9.3
- [feat-frontend-eval-editor-ui](frontend/eval-editor-ui.md) => PRD 8.17
- [feat-frontend-feedback-inbox-ui](frontend/feedback-inbox-ui.md) => PRD 8.20

### Infrastructure
- [feat-infra-security](infra/security-controls.md) => PRD 7
- [feat-infra-sse-event-bus](infra/sse-event-bus.md) => PRD 8.22
- [feat-infra-health](infra/health-checks.md) => (no PRD section — internal infra concern)
- [feat-infra-deployment](infra/deployment.md) => PRD 10.3a
- [feat-infra-extensibility](infra/extensibility.md) => PRD 10

### Library
- [feat-library-schemas](library/library-schemas.md) => PRD 8.3
- [feat-community-library](library/community-library.md) => PRD 8.14
- [feat-library-auto-update](library/auto-update.md) => PRD 8.14

### Model Backends
- [feat-model-backends-management](model-backends/model-backend-management.md) => PRD 8.1
- [feat-model-backends-hub](model-backends/model-backend-hub.md) => PRD 8.1

### Observability
- [feat-observability-monitoring-config](observability/monitoring-config.md) => PRD 8.25.1
- [feat-observability-otel-config-ui](observability/otel-config-ui.md) => PRD 6.6
- [feat-observability-error-tracking](observability/error-tracking.md) => PRD 8.25
- [feat-observability-data-residency](observability/data-residency.md) => PRD 10.5, 6.6, 6.2
- [feat-observability-error-forwarders](observability/error-forwarders.md) => PRD 8

### Pipelines
- [feat-pipelines-run-concurrency](pipelines/run-concurrency.md) => PRD 8.7
- [feat-pipelines-prompt-reveal](pipelines/prompt-reveal.md) => PRD 8.9
- [feat-pipelines-pipeline-versioning](pipelines/pipeline-versioning.md) => PRD 8.13
- [feat-pipelines-workflow-feature-files](pipelines/workflow-feature-files.md) => PRD 8.15
- [feat-pipelines-run-websocket](pipelines/run-websocket.md) => PRD 8.1
- [feat-pipelines-run-trace-observability](pipelines/run-trace-observability.md) => PRD 6.6
- [feat-pipelines-core](pipelines/core.md) => PRD 8.4
- [feat-pipelines-composite-templates](pipelines/composite-templates.md) => PRD 8.24
- [feat-pipelines-cicd-pipeline](pipelines/cicd-pipeline.md) => PRD 8.4
- [feat-pipelines-pipeline-diff-rollback](pipelines/pipeline-diff-rollback.md) => PRD 8.13
- [feat-pipelines-library](pipelines/library.md) => PRD 8.14
- [feat-pipelines-hitl-gates](pipelines/hitl-gates.md) => PRD 8.8

### Remy
- [feat-remy-context-sources](remy/remy-context-sources.md) => PRD 8.29, 8.30
- [feat-remy-assistant](remy/remy-assistant.md) => PRD 8.23, 8.27

### Teams
- [feat-teams-team-management-ui](teams/team-management-ui.md) => PRD 9.3
- [feat-teams-team-isolation](teams/team-isolation.md) => PRD 9.3
- [feat-teams-team-hitl-gates](teams/team-hitl-gates.md) => PRD 8.8, 9.3
- [feat-teams-user-offboarding](teams/user-offboarding.md) => PRD 9.4
- [feat-teams-user-management](teams/user-management.md) => PRD 9
- [feat-teams-team-ownership](teams/team-ownership.md) => PRD 9.3
- [feat-teams-org-entity](teams/org-entity.md) => PRD 9.1, 6.2
- [feat-teams-org-dashboard-full](teams/org-dashboard-full.md) => PRD 8
- [feat-teams-dashboard](teams/dashboard.md) => PRD 8
- [feat-teams-team-crud](teams/team-crud.md) => PRD 9.3
- [feat-teams-team-comparison](teams/team-comparison.md) => PRD 8
- [feat-teams-sso-team-mapping](teams/sso-team-mapping.md) => PRD 9.4, 6.2, 9.2

### Run Variants
- [feat-variants-variant-execution](variants/variant-execution.md) => PRD 8.19
- [feat-variants-variant-groups](variants/variant-groups.md) => PRD 8.19
- [feat-variants-variant-ab-testing](variants/variant-ab-testing.md) => PRD 8.19
- [feat-variants-variant-compare-ui](variants/variant-compare-ui.md) => PRD 8.19

## Legend

| Status | Meaning |
|--------|---------|
| `covered` | All expected behaviours have BDD tests |
| `partial` | Some behaviours tested, gaps exist |
| `gap` | No BDD tests, or entry is speculative |

Run `..\..\harness\tools\graph-validate.ps1` to check graph integrity.
Run `..\..\harness\tools\graph-query.ps1 --uncovered` to list entries needing attention.
Run `..\..\harness\tools\graph-query.ps1 --impact feat-<id>` to see downstream dependents.

### Admin
- [feat-admin-housekeeping](admin/housekeeping.md) => PRD TBD
