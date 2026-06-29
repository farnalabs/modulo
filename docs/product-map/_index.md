# Product Map

Every feature delivered, grouped by domain. Each entry lists expected behaviours — happy paths, edge cases, error messages, and boundaries — and links to the PRD section, BDD tests, code, and delivery tasks that cover it.

This is the graph: entries are nodes, frontmatter fields are typed edges.

## How to read an entry

```yaml
---
id: feat-<domain>-<feature>        # unique node ID
prd: §8.N                          # PRD section
delivery-tasks: [task-...]         # delivery plan task IDs
bdd: path/to/feature.feature       # BDD feature file (optional: missing = gap)
unit-tests: [path/to/test.py]      # unit test files (optional)
code: [path/to/src/]               # code paths implementing this feature
depends-on: [feat-...]             # prerequisite features
status: covered | partial | gap    # auto-updated by graph-validate
---
```

## Index

### Auth and Security
- [feat-auth-password-change](auth/password-change.md) => PRD 9.4
- [feat-auth-sso-provider-ui](auth/sso-provider-ui.md) => PRD 9.4
- [feat-auth-team-api-keys](auth/team-api-keys.md) => PRD 9.3
- [feat-auth-team-rbac](auth/team-rbac.md) => PRD 9.2, 9.3
- [feat-auth-scim](auth/scim-provisioning.md) => PRD 9.2, 9.4
- [feat-auth-jwt-auth](auth/jwt-auth.md) => PRD 7.10
- [feat-auth-mcp-oauth](auth/mcp-oauth.md) => PRD 6.4
- [feat-auth-rate-limiting](auth/rate-limiting.md) => PRD 7.18

### Connectors
- [feat-connectors-linear](connectors/linear-connector.md) => PRD 8.6
- [feat-connectors-schema-inference](connectors/schema-inference.md) => PRD 8.16
- [feat-connectors-slack](connectors/slack-connector.md) => PRD 8.6
- [feat-connectors-jira](connectors/jira-connector.md) => PRD 8.6
- [feat-connectors-hub](connectors/connector-hub.md) => PRD 8.6
- [feat-connectors-github](connectors/github-connector.md) => PRD 8.6
- [feat-connectors-gitlab](connectors/gitlab-connector.md) => PRD 8.6

### Core Platform
- [feat-core-replace-step-agent](core/replace-step-agent.md) => PRD 8.4
- [feat-core-registry-protocol-v2](core/registry-protocol-v2.md) => PRD 8.14
- [feat-core-rollback-agent-replacement](core/rollback-agent-replacement.md) => PRD 8.4
- [feat-core-runtime-config](core/runtime-config.md) => PRD §15
- [feat-core-run-context](core/run-context.md) => PRD 8.18
- [feat-core-quality-report-slack](core/quality-report-slack.md) => PRD 8.6, 8.11
- [feat-core-pipeline-execution](core/pipeline-execution.md) => PRD 8.4
- [feat-core-oidc-integration](core/oidc-integration.md) => PRD 9.4, 6.2, 9.2
- [feat-core-pkg0-celery-optional](core/pkg0-celery-optional.md) => PRD 14
- [feat-core-prompt-optimization](core/prompt-optimization.md) => PRD 8.2
- [feat-core-polling-trigger](core/polling-trigger.md) => PRD 8.5
- [feat-core-soc2-evidence-export](core/soc2-evidence-export.md) => PRD 8.12
- [feat-core-secrets-backend](core/secrets-backend.md) => PRD 7.13
- [feat-core-stale-tags](core/stale-tags.md) => PRD 13
- [feat-core-verified-publishers](core/verified-publishers.md) => PRD 8.14
- [feat-core-trigger-system](core/trigger-system.md) => PRD 8.5
- [feat-core-schema-union-types](core/schema-union-types.md) => PRD 8.3
- [feat-core-saml-integration](core/saml-integration.md) => PRD 9.4, 6.2, 9.2
- [feat-core-runtime-provider-core](core/runtime-provider-core.md) => PRD 6.2
- [feat-core-schema-inference-ui](core/schema-inference-ui.md) => PRD 8.16
- [feat-core-schema-system](core/schema-system.md) => PRD 8.3
- [feat-core-schema-inference](core/schema-inference.md) => PRD 8.16
- [feat-core-contribute-primitive](core/contribute-primitive.md) => PRD 8.14
- [feat-core-backup-restore](core/backup-restore.md) => PRD 6.2
- [feat-core-contribution-provenance](core/contribution-provenance.md) => PRD 8.14
- [feat-core-core](core/core.md) => PRD 8.16, 8.4
- [feat-core-contribution-update](core/contribution-update.md) => PRD 8.14
- [feat-core-audit-viewer-ui](core/audit-viewer-ui.md) => PRD 8.12
- [feat-core-ai-schema-gen](core/ai-schema-gen.md) => PRD 8.16
- [feat-core-agent-model](core/agent-model.md) => PRD 8.2
- [feat-core-api-versioning](core/api-versioning.md) => PRD 6
- [feat-core-audit-trail](core/audit-trail.md) => PRD 8.12
- [feat-core-audit-crypto-chain](core/audit-crypto-chain.md) => PRD 8.12
- [feat-core-migration-cli](core/migration-cli.md) => PRD 6.2
- [feat-core-hitl-effort-trend](core/hitl-effort-trend.md) => PRD 8.8
- [feat-core-model-failover](core/model-failover.md) => PRD 8.1
- [feat-core-notifications](core/notifications.md) => PRD 8.11
- [feat-core-multi-backend-tests](core/multi-backend-tests.md) => PRD 12
- [feat-core-helm-chart](core/helm-chart.md) => PRD 11, 13
- [feat-core-db-abstraction-core](core/db-abstraction-core.md) => PRD 8.17
- [feat-core-cost-breakdown](core/cost-breakdown.md) => PRD 8.10
- [feat-core-db-abstraction-remaining](core/db-abstraction-remaining.md) => PRD 8.17
- [feat-core-feedback-correction](core/feedback-correction.md) => PRD 8.20
- [feat-core-feature-flag-ui](core/feature-flag-ui.md) => PRD 8.17

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

### Model Backends
- [feat-model-backends-management](model-backends/model-backend-management.md) => PRD 8.1
- [feat-model-backends-hub](model-backends/model-backend-hub.md) => PRD 8.1

### Observability
- [feat-observability-otel-config-ui](observability/otel-config-ui.md) => PRD 6.6
- [feat-observability-data-residency](observability/data-residency.md) => PRD 10.5, 6.6, 6.2

### Pipelines
- [feat-pipelines-pipeline-versioning](pipelines/pipeline-versioning.md) => PRD 8.13
- [feat-pipelines-run-trace-observability](pipelines/run-trace-observability.md) => PRD 6.6
- [feat-pipelines-workflow-feature-files](pipelines/workflow-feature-files.md) => PRD 8.15
- [feat-pipelines-pipeline-diff-rollback](pipelines/pipeline-diff-rollback.md) => PRD 8.13
- [feat-pipelines-cicd-pipeline](pipelines/cicd-pipeline.md) => PRD 8.4
- [feat-pipelines-core](pipelines/core.md) => PRD 8.4
- [feat-pipelines-library](pipelines/library.md) => PRD 8.14

### Teams
- [feat-teams-team-isolation](teams/team-isolation.md) => PRD 9.3
- [feat-teams-team-hitl-gates](teams/team-hitl-gates.md) => PRD 8.8, 9.3
- [feat-teams-team-management-ui](teams/team-management-ui.md) => PRD 9.3
- [feat-teams-user-offboarding](teams/user-offboarding.md) => PRD 9.4
- [feat-teams-team-ownership](teams/team-ownership.md) => PRD 9.3
- [feat-teams-team-crud](teams/team-crud.md) => PRD 9.3
- [feat-teams-org-dashboard-full](teams/org-dashboard-full.md) => PRD 14
- [feat-teams-dashboard](teams/dashboard.md) => PRD 14
- [feat-teams-org-entity](teams/org-entity.md) => PRD 9.1, 6.2
- [feat-teams-team-comparison](teams/team-comparison.md) => PRD 14
- [feat-teams-sso-team-mapping](teams/sso-team-mapping.md) => PRD 9.4, 6.2, 9.2

### variants
- [feat-variants-variant-execution](variants/variant-execution.md) => PRD 8.19
- [feat-variants-variant-groups](variants/variant-groups.md) => PRD 8.19
- [feat-variants-variant-ab-testing](variants/variant-ab-testing.md) => PRD 8.19
- [feat-variants-variant-compare-ui](variants/variant-compare-ui.md) => PRD 8.19


### Auth and Security
- [feat-auth-sso-provider-ui](auth/sso-provider-ui.md) => PRD delivery-tasks: [task-nv6-sso-provider-ui]
- [feat-auth-team-api-keys](auth/team-api-keys.md) => PRD delivery-tasks: [task-nv1-team-api-keys]
- [feat-auth-team-rbac](auth/team-rbac.md) => PRD delivery-tasks: [task-nv1-team-rbac]
- [feat-auth-mcp-oauth](auth/mcp-oauth.md) => PRD delivery-tasks: [task-nv9-mcp-oauth]
- [feat-auth-rate-limiting](auth/rate-limiting.md) => PRD delivery-tasks: [task-nv12-rate-limiting]
- [feat-auth-scim](auth/scim-provisioning.md) => PRD 9.2, 9.4

### Core Platform
- [feat-core-registry-protocol-v2](core/registry-protocol-v2.md) => PRD delivery-tasks: [task-nv8-registry-protocol-v2]
- [feat-core-quality-report-slack](core/quality-report-slack.md) => PRD delivery-tasks: [task-nv7-quality-report-slack]
- [feat-core-rollback-agent-replacement](core/rollback-agent-replacement.md) => PRD delivery-tasks: [task-nv11-rollback-agent-replacement]
- [feat-core-replace-step-agent](core/replace-step-agent.md) => PRD delivery-tasks: [task-nv11-replace-step-agent]
- [feat-core-prompt-optimization](core/prompt-optimization.md) => PRD delivery-tasks: [task-nv10-prompt-optimization]
- [feat-core-pipeline-execution](core/pipeline-execution.md) => PRD 8.4
- [feat-core-oidc-integration](core/oidc-integration.md) => PRD delivery-tasks: [task-nv6-oidc-integration]
- [feat-core-polling-trigger](core/polling-trigger.md) => PRD delivery-tasks: [task-nv10-polling-trigger]
- [feat-core-pkg0-celery-optional](core/pkg0-celery-optional.md) => PRD delivery-tasks: [task-pkg0-celery-optional]
- [feat-core-run-context](core/run-context.md) => PRD delivery-tasks: [task-nv0-complexity-reviewer, task-nv0-run-context-tests]
- [feat-core-runtime-config](core/runtime-config.md) => PRD delivery-tasks: [task-nv18-runtime-config-backend, task-nv18-runtime-config-frontend]
- [feat-core-soc2-evidence-export](core/soc2-evidence-export.md) => PRD delivery-tasks: [task-nv11-soc2-evidence-export]
- [feat-core-secrets-backend](core/secrets-backend.md) => PRD delivery-tasks: [task-nv10-secrets-backend]
- [feat-core-verified-publishers](core/verified-publishers.md) => PRD delivery-tasks: [task-nv8-verified-publishers]
- [feat-core-stale-tags](core/stale-tags.md) => PRD delivery-tasks: [task-nv12-stale-tags]
- [feat-core-schema-union-types](core/schema-union-types.md) => PRD delivery-tasks: [task-nv9-schema-union-types]
- [feat-core-saml-integration](core/saml-integration.md) => PRD delivery-tasks: [task-nv6-saml-integration]
- [feat-core-runtime-provider-core](core/runtime-provider-core.md) => PRD delivery-tasks: [task-nv12-runtime-provider-core]
- [feat-core-schema-inference](core/schema-inference.md) => PRD delivery-tasks: [task-nv5-schema-inference-service, task-nv5-schema-infer-endpoint]
- [feat-core-schema-inference-ui](core/schema-inference-ui.md) => PRD delivery-tasks: [task-nv5-schema-inference-ui]
- [feat-core-notifications](core/notifications.md) => PRD delivery-tasks: [task-nv1-team-notifications]
- [feat-core-contribute-primitive](core/contribute-primitive.md) => PRD delivery-tasks: [task-nv8-contribute-primitive]
- [feat-core-backup-restore](core/backup-restore.md) => PRD delivery-tasks: [task-nv12-backup-restore]
- [feat-core-contribution-update](core/contribution-update.md) => PRD delivery-tasks: [task-nv8-contribution-update]
- [feat-core-contribution-provenance](core/contribution-provenance.md) => PRD delivery-tasks: [task-nv8-contribution-provenance]
- [feat-core-audit-viewer-ui](core/audit-viewer-ui.md) => PRD delivery-tasks: [task-nv11-audit-viewer-ui]
- [feat-core-api-versioning](core/api-versioning.md) => PRD delivery-tasks: [task-nv12-api-versioning]
- [feat-core-ai-schema-gen](core/ai-schema-gen.md) => PRD delivery-tasks: [task-nv9-ai-schema-gen]
- [feat-core-audit-trail](core/audit-trail.md) => PRD delivery-tasks: [task-nv0-immutable-audit]
- [feat-core-audit-crypto-chain](core/audit-crypto-chain.md) => PRD delivery-tasks: [task-nv10-audit-crypto-chain]
- [feat-core-core](core/core.md) => PRD delivery-tasks: [task-nv5-sdlc-onboarding-path]
- [feat-core-migration-cli](core/migration-cli.md) => PRD delivery-tasks: [task-nv9-migration-cli]
- [feat-core-hitl-effort-trend](core/hitl-effort-trend.md) => PRD delivery-tasks: [task-nv7-hitl-effort-trend]
- [feat-core-multi-backend-tests](core/multi-backend-tests.md) => PRD delivery-tasks: [task-nv12-multi-backend-tests]
- [feat-core-model-failover](core/model-failover.md) => PRD delivery-tasks: [task-nv9-model-failover]
- [feat-core-helm-chart](core/helm-chart.md) => PRD delivery-tasks: [task-nv9-helm-chart]
- [feat-core-db-abstraction-remaining](core/db-abstraction-remaining.md) => PRD delivery-tasks: [task-nv12-db-abstraction-remaining]
- [feat-core-cost-breakdown](core/cost-breakdown.md) => PRD delivery-tasks: [task-nv7-cost-breakdown]
- [feat-core-feedback-correction](core/feedback-correction.md) => PRD delivery-tasks: [task-nv4-ai-correction-agent, task-nv4-correction-run]
- [feat-core-feature-flag-ui](core/feature-flag-ui.md) => PRD delivery-tasks: [task-nv12-feature-flag-ui]

### Evals and Feedback
- [feat-evals-feedback-proposals](evals/feedback-proposals.md) => PRD delivery-tasks: [task-nv4-eval-proposals-queue]
- [feat-evals-feedback-loop](evals/feedback-loop.md) => PRD delivery-tasks: [task-nv4-feedback-loop-auto]
- [feat-evals-eval-testing](evals/eval-testing.md) => PRD delivery-tasks: [task-nv2-eval-bdd-tests]
- [feat-evals-variant-coverage](evals/variant-coverage.md) => PRD delivery-tasks: [task-nv3-eval-coverage-signal]
- [feat-evals-okr-eval-alignment](evals/okr-eval-alignment.md) => PRD delivery-tasks: [task-nv7-okr-eval-alignment]
- [feat-evals-feedback-records](evals/feedback-records.md) => PRD delivery-tasks: [task-nv4-feedback-record]
- [feat-evals-system](evals/eval-system.md) => PRD 8.17
- [feat-evals-eval-engine](evals/eval-engine.md) => PRD delivery-tasks: [task-nv2-eval-custom-function, task-nv2-eval-engine, task-nv2-eval-llm-judge, task-nv2-eval-regex-schema]
- [feat-evals-eval-definitions](evals/eval-definitions.md) => PRD delivery-tasks: [task-nv2-eval-definition]
- [feat-evals-conditional-transitions](evals/conditional-transitions.md) => PRD delivery-tasks: [task-nv9-conditional-transitions]
- [feat-evals-eval-regression-alerts](evals/eval-regression-alerts.md) => PRD delivery-tasks: [task-nv7-eval-regression-alerts]
- [feat-evals-eval-packaging](evals/eval-packaging.md) => PRD delivery-tasks: [task-nv2-eval-packaging]
- [feat-evals-eval-gates](evals/eval-gates.md) => PRD delivery-tasks: [task-nv2-conditional-hitl, task-nv2-eval-gate-enforcement]

### Frontend
- [feat-frontend-feedback-routing](frontend/feedback-routing.md) => PRD delivery-tasks: [task-nv4-feedback-routing]
- [feat-frontend-ownership-picker](frontend/ownership-picker.md) => PRD delivery-tasks: [task-nv1-ownership-picker]
- [feat-frontend-eval-editor-ui](frontend/eval-editor-ui.md) => PRD delivery-tasks: [task-nv2-eval-ui-editor]
- [feat-frontend-feedback-inbox-ui](frontend/feedback-inbox-ui.md) => PRD delivery-tasks: [task-nv4-feedback-inbox-ui]

### Observability
- [feat-observability-otel-config-ui](observability/otel-config-ui.md) => PRD delivery-tasks: [task-nv9-otel-config-ui]
- [feat-observability-data-residency](observability/data-residency.md) => PRD delivery-tasks: [task-nv0-data-residency]

### Pipelines
- [feat-pipelines-pipeline-versioning](pipelines/pipeline-versioning.md) => PRD delivery-tasks: [task-nv0-snapshot-expansion]
- [feat-pipelines-run-trace-observability](pipelines/run-trace-observability.md) => PRD delivery-tasks: [task-nv7-run-trace-observability]
- [feat-pipelines-workflow-feature-files](pipelines/workflow-feature-files.md) => PRD delivery-tasks: [task-nv12-workflow-feature-files]
- [feat-pipelines-cicd-pipeline](pipelines/cicd-pipeline.md) => PRD delivery-tasks: [task-nv12-cicd-pipeline]
- [feat-pipelines-library](pipelines/library.md) => PRD delivery-tasks: [task-nv0-first-pipeline-library]
- [feat-pipelines-pipeline-diff-rollback](pipelines/pipeline-diff-rollback.md) => PRD delivery-tasks: [task-nv10-pipeline-diff-rollback]

### Teams
- [feat-teams-team-isolation](teams/team-isolation.md) => PRD delivery-tasks: [task-nv1-team-isolation]
- [feat-teams-team-hitl-gates](teams/team-hitl-gates.md) => PRD delivery-tasks: [task-nv1-team-hitl-gates]
- [feat-teams-team-management-ui](teams/team-management-ui.md) => PRD delivery-tasks: [task-nv1-team-ui]
- [feat-teams-user-offboarding](teams/user-offboarding.md) => PRD delivery-tasks: [task-nv1-user-offboarding]
- [feat-teams-team-ownership](teams/team-ownership.md) => PRD delivery-tasks: [task-nv1-team-ownership]
- [feat-teams-org-dashboard-full](teams/org-dashboard-full.md) => PRD delivery-tasks: [task-nv7-org-dashboard-full]
- [feat-teams-dashboard](teams/dashboard.md) => PRD delivery-tasks: [task-nv0-org-dashboard-basic]
- [feat-teams-sso-team-mapping](teams/sso-team-mapping.md) => PRD delivery-tasks: [task-nv6-sso-team-mapping]
- [feat-teams-team-crud](teams/team-crud.md) => PRD delivery-tasks: [task-nv1-team-entity]
- [feat-teams-team-comparison](teams/team-comparison.md) => PRD delivery-tasks: [task-nv7-team-comparison]

### variants
- [feat-variants-variant-execution](variants/variant-execution.md) => PRD delivery-tasks: [task-nv3-variant-run]
- [feat-variants-variant-groups](variants/variant-groups.md) => PRD delivery-tasks: [task-nv3-variant-group]
- [feat-variants-variant-ab-testing](variants/variant-ab-testing.md) => PRD delivery-tasks: [task-nv3-ab-test-models]
- [feat-variants-variant-compare-ui](variants/variant-compare-ui.md) => PRD delivery-tasks: [task-nv3-variant-compare-ui]


### Core Platform
- [Pipeline Execution](core/pipeline-execution.md) — §8.4
- [Schema System](core/schema-system.md) — §8.3
- [Agent Model](core/agent-model.md) — §8.2
- [Trigger System](core/trigger-system.md) — §8.5
- [Run Context Propagation](core/run-context.md) — §8.18
- [Run Variants & A/B Testing](core/run-variants.md) — §8.19
- [Error Handling](core/error-handling.md) — §8.9
- [Cost Controls / Cost Breakdown](core/cost-breakdown.md) — §8.10
- [Notifications](core/notifications.md) — §8.11
- [Audit Trail](core/audit-trail.md) — §8.12
- [Pipeline Versioning](core/pipeline-versioning.md) — §8.13

### Auth & Security
- [SCIM Provisioning](auth/scim-provisioning.md) — §9.2, §9.4
- [JWT Auth](auth/jwt-auth.md) — §7.10
- [API Keys](auth/api-keys.md) — §9.4
- [RBAC](auth/rbac.md) — §9.2
- [SSO / OIDC](auth/sso.md) — §9.4
- [Rate Limiting](auth/rate-limiting.md) — §7.18

### Teams
- [Team CRUD](teams/team-crud.md) — §9.3
- [Team Isolation (RLS)](teams/team-isolation.md) — §9.3
- [Team RBAC](teams/team-rbac.md) — §9.3
- [Team API Keys](teams/team-api-keys.md) — §9.3
- [Team HITL Gates](teams/team-hitl-gates.md) — §9.3
- [Team Notifications](teams/team-notifications.md) — §9.3
- [User Offboarding](teams/user-offboarding.md) — §9.4
- [Ownership Picker](teams/ownership-picker.md) — §9.3

### Evals & Feedback
- [Eval Definitions](evals/eval-definitions.md) — §8.17
- [Eval Engine](evals/eval-engine.md) — §8.17
- [Eval Gates](evals/eval-gates.md) — §8.17
- [Feedback System](evals/feedback-system.md) — §8.20
- [Eval Coverage Gap](evals/eval-coverage-gap.md) — §8.19

### Connectors
- [Connector Hub](connectors/connector-hub.md) — §8.6
- [GitHub Connector](connectors/github-connector.md) — §8.6
- [GitLab Connector](connectors/gitlab-connector.md) — §8.6
- [Jira Connector](connectors/jira-connector.md) — §8.6
- [Linear Connector](connectors/linear-connector.md) — §8.6
- [Slack Connector](connectors/slack-connector.md) — §8.6
- [Schema Inference](connectors/schema-inference.md) — §8.16

### Pipelines
- [Pipeline CRUD](pipelines/pipeline-crud.md) — §8.4
- [Pipeline Library](pipelines/pipeline-library.md) — §8.14
- [Workflow Bundles](pipelines/workflow-bundles.md) — §8.15
- [HITL Gates](pipelines/hitl-gates.md) — §8.8
- [Run Lifecycle](pipelines/run-lifecycle.md) — §8.7

### Model Backends
- [Model Backend Management](model-backends/model-backend-management.md) — §8.1
- [Model Backend Hub](model-backends/model-backend-hub.md) — §8.1

### Frontend
- [Views](frontend/views.md) — §6.3
- [Stores](frontend/stores.md) — §6.3
- [Pipeline Canvas](frontend/pipeline-canvas.md) — §6.3
- [Dashboard](frontend/dashboard.md) — §14
- [Theme System](frontend/theme.md) — §6.3

### Observability
- [OTel Bridge](observability/otel-bridge.md)
- [Audit Events](observability/audit-events.md) — §8.12
- [Dashboard Metrics](observability/dashboard-metrics.md) — §14

---

## Legend

| Status | Meaning |
|--------|---------|
| `covered` | All expected behaviours have BDD tests |
| `partial` | Some behaviours tested, gaps exist |
| `gap` | No BDD tests, or entry is speculative |

Run `..\..\Dev-Harness\tools\graph-validate.ps1` to check graph integrity.
Run `..\..\Dev-Harness\tools\graph-query.ps1 --uncovered` to list entries needing attention.
Run `..\..\Dev-Harness\tools\graph-query.ps1 --impact feat-<id>` to see downstream dependents.


