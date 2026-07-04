# ADR 010 — Integration Tier Classification (Native / Preview / In-Dev)

**Date**: 2026-07-04
**Status**: Accepted

---

## Context

Modulo is maintained by a solo/small team while aiming to support a broad set
of connectors, model backends, and library workflows. Unbounded breadth
creates a maintenance burden that a small team cannot sustain at production
quality, but restricting the product to only a handful of hardened
integrations limits reach. This is the classic breadth-vs-depth tension for
a small maintaining team.

At the same time, users need an honest signal about how mature a given
integration or workflow actually is. Presenting every connector, model
backend, and library workflow identically — regardless of how battle-tested
it is — misleads users into assuming uniform quality and risks trust when a
less-mature integration misbehaves in production.

**What already exists informally:** the product has an ad-hoc notion of
maturity for some integrations (e.g. GitHub, GitLab, Jira/Linear are the
existing V1-scoped, actively-maintained connectors per the PRD), but there is
no formal schema field, enum, or UI treatment that captures this distinction
today. No prior ADR or PRD section defines a `tier` concept for connectors,
model backends, or workflows. This ADR introduces that concept formally and
generalizes it uniformly across all three integration surfaces.

The team also maintains a curated library of "off-the-shelf SDLC modules"
(see PRD library/registry sections) and has discussed extending this with a
community-populated database of components and example pipelines. Mixing
Modulo-maintained structural workflows with opinionated, narrower
community-contributed examples in the same UI surface would create the same
trust-signaling problem at the workflow level.

## Decision

### 1. Three-tier classification for every integration surface

Every connector, model backend, and library workflow is classified into
exactly one of three tiers:

| Tier | Meaning | UI treatment |
|---|---|---|
| **Native** | Robust, fully tested, feature-rich. Actively maintained and hardened by Modulo. | Prominently surfaced as the primary/default options. |
| **Preview** | Functional but not yet production-hardened. | Segregated behind a secondary control (e.g. a "more options" / overflow dropdown) so users opt in with clear expectations rather than assuming Native-level quality. |
| **In-Dev** | Feature-flagged, actively being built. | Not visible to users by default. |

This applies uniformly to:
- **Connectors** (e.g. GitHub, GitLab, Jira/Linear as Native; an Artifactory
  connector as an example of a Preview-tier candidate)
- **Model backends**
- **Library workflows**

This generalizes the informal maturity distinction that already exists
between actively-maintained connectors (GitHub, GitLab, Jira/Linear) and any
newer or experimental integrations, by giving it an explicit metadata field
and consistent UI treatment rather than leaving it implicit in which
connectors happen to get engineering attention.

**Sequencing implication:** once V1/MVP Native integrations and workflows are
validated through real usage (not just built), development focus shifts to
hardening the Native set — robustness, no regressions, responsiveness to
feature requests — rather than continuing to add integration breadth. The
Preview tier absorbs new and experimental integrations during and after that
shift, giving the team a place to keep shipping breadth without diluting the
Native tier's quality bar.

### 2. Native library vs. community database (workflow-level split)

The existing curated/native workflow library is split into two distinct,
clearly-labeled UI sections:

- **Native library** — structural, general-purpose, verified workflow
  patterns that Modulo itself designs and maintains (example: an "LLM as
  Council" pattern where multiple models/agents deliberate before producing
  an output).
- **Community database** — opinionated, narrower example pipelines
  contributed by users, shown to demonstrate what's possible but explicitly
  NOT held to Native-maintenance standards (examples: a "translate to
  French" pipeline, a "QA Reviewer" pipeline).

The community database reuses the same Native/Preview/In-Dev-style tiering
visually, but as a separate, clearly-labeled section — never mixed into the
Native library — so users cannot mistake a community-contributed example for
a Modulo-hardened workflow.

**Go-to-market sequencing note (non-technical):** the community database's
value as a differentiator depends on genuine external contributions. It
should launch seeded with a small curated set and should NOT be marketed as
"community-driven" until real external contributions exist. This affects
when and how the feature is surfaced publicly, even though it does not
change the technical shape of the feature.

## Consequences

**Positive:**
- Honest signaling: users can distinguish "Modulo stands behind this" from
  "this exists and works, use with judgment" from "not ready yet."
- Protects trust in the Native tier by preventing an unvetted or
  experimental integration from degrading the perceived quality of the
  product as a whole.
- Contains maintenance burden: the team can keep shipping breadth (Preview)
  without committing full hardening effort to every new integration
  immediately.
- Gives the community database a safe home that doesn't compete with or
  dilute the Native library's credibility.

**Negative:**
- Added metadata and UI complexity: every connector, model backend, and
  workflow registration now needs a `tier` field, and the UI needs
  tier-aware rendering (primary surface vs. secondary/overflow vs. hidden).
- Requires an explicit promotion/demotion process between tiers (Preview →
  Native, and potentially Native → Preview if a previously-hardened
  integration regresses or becomes unmaintained), which does not exist yet.

## Alternatives considered

### No tiering — uniform list

Present all connectors, model backends, and workflows identically regardless
of maturity.

Rejected because this either forces an excessive quality bar before adding
anything (limiting breadth unacceptably for a small team) or dilutes user
trust by presenting unvetted integrations with the same confidence as
hardened ones.

### Fully open marketplace with no native/community split

Let any contributed connector, model backend, or workflow appear in the same
marketplace-style listing without a maintained/community distinction.

Rejected because it optimizes for module count over trust, which conflicts
with Modulo's existing intent to keep the library and registry
trust-oriented rather than quantity-oriented. A fully open, undifferentiated
marketplace also makes it impossible for users to know which entries Modulo
will support versus merely host.

## Open questions

- Exact schema field name and enum values for the tier field (e.g.
  `tier: "native" | "preview" | "in_dev"` vs. some other naming) — to be
  decided at implementation time.
- The concrete promotion/demotion process from Preview → Native (and any
  Native → Preview demotion path) — criteria, who approves it, and whether
  it needs to be recorded anywhere (changelog, product map).
- Whether In-Dev items should be visible to admins behind a debug/internal
  flag for early testing, or strictly invisible to everyone until promoted
  to Preview.
