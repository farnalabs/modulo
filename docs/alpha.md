# Alpha - definition, scope and exit

**Status:** settled 2026-08-16, restated 2026-09-21, revised 2026-10-10 (deliverables-based exit; human sign-offs retired). This is the live statement of what "alpha" means for Modulo. It supersedes the alpha sections of the retired monolithic PRD, which described an earlier, internal-only alpha.

## Definition

Alpha means all four of:

1. **The code is public.** Source is published under the Business Source License 1.1 - source-available and world-readable, not OSI open-source.
2. **The product is available to buy.** A paid Team tier can be purchased, and licence enforcement ships with alpha - Ed25519-signed licence keys are verified at startup.
3. **All docs are valid and up to date.**
4. **A user can pull it down and start using it in anger**, via various distribution methods.

**Hosted signup is not offered.** Alpha users self-host Modulo on their own infrastructure. There is no multi-tenant hosted product and no customer-facing evaluation instance; the project's own deployment exists for internal use, not as a service offered to customers.

## How alpha and beta are determined

Alpha and beta are gated purely by deliverables in the code: the scoped Alpha-milestone work has landed, the automated test suites are green in CI, and there are zero open P0 security findings. There are no human-sign-off exit criteria and no go/no-go committee.

## What alpha is not

- Not a hosted SaaS, and not a hosted trial.
- Not a public contributor programme (tracked separately).
- Not the complete planned feature set - depth items continue after alpha.

## Scope - what lands before alpha

- **Distribution:** published container images with unauthenticated pull, a worked `install.sh` path, and the supported install methods documented.
- **Buyable:** licence issuance, Team-tier unlock, invoicing, and the contributor/legal paperwork a public repo needs.
- **Documentation:** the public docs and quickstart valid and current, with no stale org/path/version references.
- **Contribution path:** the public repo is usable by an outside contributor (CI runs for forks, internal references scrubbed).
- **Release engineering:** signed images/artifacts and an SBOM per release.
- **Deployment shape:** a Kubernetes deployment path (Helm chart plus a runtime provider) as the recommended production self-hosting shape. Shipped - the Helm chart for the stack was validated end-to-end on EKS (FAR-1052), the runtime provider that runs agent workspaces as pods ships on main (FAR-1051), and its kind conformance gate is green (FAR-1053), so the combined path may be presented publicly as an existing capability. The scheduled managed-cluster conformance run has not executed yet (its kubeconfig secrets are pending), so no managed-cluster validation is claimed. The public reader-facing guide for the path is https://modulo.run/docs/kubernetes.
- **Governance model:** the evaluation and policy-gate model ships before any customer exposure - it is a conceptual change to how runs are governed, not an incremental feature.

## Exit criteria

1. A new user can self-host from the published artifacts and complete a first real run.
2. A Team tier can be bought end to end.
3. The documentation set is internally consistent and current.
4. No open P0 security findings.

**End condition:** alpha closes when these criteria hold and the scoped Alpha-milestone deliverables are complete - or ~90 days after launch, whichever comes first. Beta begins when the evaluation/policy-gate model lands.

## Commercial decisions (settled)

- **Free vs paid:** paid and enforced - a paid Team tier exists and licence enforcement is live. Licence terms, refunds and the customer-facing ToS/privacy for the paid self-hosted product are an alpha dependency (tracked as FAR-1112).
- **Version:** the release carries the next clean 0.x minor at cut (expected 0.4); the git tag is reconciled with the served version at cut.
- **Launch:** a hard public launch, gated by a fixed go/no-go checklist - exit criteria met; zero open P0s; anonymous `docker pull` succeeds; a real end-to-end Team-tier purchase succeeds; the production deploy SHA equals the release tag; the docs sweep is clean.
