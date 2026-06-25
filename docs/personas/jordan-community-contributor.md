# Persona: Jordan — Community Contributor / Library Author

| Attribute | Value |
|---|---|
| **Role** | Indie developer / open-source contributor |
| **Org size** | 1 (solo); collaborates with 2–3 other OSS maintainers |
| **Technical level** | Expert full-stack; knows the Modulo internals from contributing |
| **Industry** | OSS ecosystem |
| **Budget authority** | None — Community edition user |

## Goals

- Build and share reusable SDLC modules (agents, schemas, workflows, evals) with the Modulo community
- Contribute fixture data and eval suites that improve the library's quality baseline
- Get attribution and reputation through the community library (downloads, ratings, verified publisher status)
- Use Modulo's primitives to automate his own OSS maintenance: release notes, changelog, dependency bumps, CI triage
- Port his workflows between personal projects and OSS repos with minimal friction

## Pain points / triggers

- Maintains 4 OSS libraries; spends weekends on release management — wants to automate it
- Built custom automation scripts that break whenever a dependency or API changes
- Wants to share his PRD→issues workflow with the community but has no publishing pipeline
- Sees the same CI failures across projects and wants a reusable triage agent
- Frustrated with "marketplace" platforms that take a cut or require SaaS signup to publish

## Key scenarios that must work

1. **`@persona-jordan`** `features/library/browse.feature` — Browse community library by primitive type and category
2. **`@persona-jordan`** `features/library/copy_to_adapt.feature` — Fork a library workflow; customise for his OSS conventions
3. **`@persona-jordan`** `features/library/contribute.feature` — Submit a new workflow/agent/eval to the community library
4. **`@persona-jordan`** `features/library/contribution_review.feature` — Contribution goes through automated quality checks and human review
5. **`@persona-jordan`** `features/library/ratings.feature` — Rate and review library primitives; see download counts
6. **`@persona-jordan`** `features/workflows/export.feature` — Export pipeline as YAML bundle to share with contributors
7. **`@persona-jordan`** `features/workflows/import.feature` — Import someone else's pipeline YAML; resolve schema conflicts
8. **`@persona-jordan`** `features/agents/prompt_versioning.feature` — Version prompts so his contributed agent has provenance
9. **`@persona-jordan`** `features/eval/eval_suite_crud.feature` — Package evals alongside his contributed agent as a complete primitive
10. **`@persona-jordan`** `features/triggers/polling.feature` — Poll GitHub for new releases; auto-run his changelog pipeline
11. **`@persona-jordan`** `features/pipelines/run_lifecycle.feature` — Check run status of his OSS maintenance pipelines from CI

## Anti-scenarios (must NOT require)

- An enterprise licence, SSO, or team setup to publish library contributions
- A SaaS account, credit card, or telemetry opt-in to share work with the community
- Manual approval from Modulo maintainers for every contribution (automated validation first)
- Forking a library primitive losing the "forked_from" provenance metadata

## What success looks like

Jordan builds a `release-notes-from-changelog` agent, packages it with an eval suite and a `markdown-release` schema, and submits it to the community. It passes automated quality checks, gets reviewed by a Modulo maintainer, and is published. Three weeks later, 47 downloads and 4.2 stars. He forks the official `issue-to-pr` workflow for his OSS project, tweaks the prompts, and CI is now automatically generating PR descriptions from Linear issues. He opens a PR to contribute his improved prompts back.
