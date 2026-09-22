# Alpha - definition, scope and exit

**Status:** settled 2026-08-16, restated 2026-09-21. This is the live statement of what "alpha" means for Modulo. It supersedes the alpha sections of the retired monolithic PRD, which described an earlier, internal-only alpha.

## Definition

Alpha means all four of:

1. **The code is public.** Source is published under the Business Source License 1.1 - source-available and world-readable, not OSI open-source.
2. **The product is available to buy.** A paid Team tier can be purchased.
3. **All docs are valid and up to date.**
4. **A user can pull it down and start using it in anger**, via various distribution methods.

**Hosted signup is not offered.** Alpha users self-host Modulo on their own infrastructure. There is no multi-tenant hosted product and no customer-facing evaluation instance; the project's own deployment exists for internal use, not as a service offered to customers.

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
- **Deployment shape:** a Kubernetes deployment path (Helm chart plus a runtime provider) as the recommended production self-hosting shape. In progress - not yet available, and not to be presented publicly as an existing capability until it ships.
- **Governance model:** the evaluation and policy-gate model ships before any customer exposure - it is a conceptual change to how runs are governed, not an incremental feature.
- **Exit gate:** the exit criteria below are demonstrably met with named human sign-offs, not merely green CI.

## Exit criteria

1. A new user can self-host from the published artifacts and complete a first real run.
2. A Team tier can be bought end to end.
3. The documentation set is internally consistent and current.
4. The named sign-offs are recorded as evidence: an end-to-end walkthrough by non-authors; HITL review by multiple named users; a non-demo pipeline built and run to completion; a connector swap; and a run-context demonstration.
5. No open P0 security findings.

## Open commercial questions

Still to be settled:

- the milestone version number carried by the release;
- the success metrics, and the end condition that closes alpha;
- the go/no-go and announcement process;
- whether the self-hosted alpha user pays, and whether licence enforcement ships with alpha.
