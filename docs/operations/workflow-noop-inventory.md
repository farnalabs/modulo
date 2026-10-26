# Workflow no-op inventory (FAR-2157)

A workflow **acts** (deploy, fix, review, merge, dispatch, provision, notify). It
has a defect when a run concludes **success** while performing none of that work
and leaving **no machine-readable signal** that it did nothing, so a consumer
reads the green run as "the action happened".

This inventory classifies every workflow in `.github/workflows/` against that
definition. It exists because FAR-2152 proved the class with four "successful"
deploy runs that shipped nothing (2026-10-25), and FAR-528 before it (a
throttled deploy green for two days).

**A legitimate no-op is not a defect.** A docs-only change skipping the code
jobs, an empty merge queue, a nightly canary with no release to verify — these
are fine when nothing consumes the run as evidence that the work happened.

## The signalling convention: a named step, never a job output

The durable machine-readable outcome is a **step that runs only on the no-op
path**. The Actions jobs API (`GET /repos/{owner}/{repo}/actions/runs/{id}/jobs`)
returns each job's `steps` with `name` and `conclusion`, but **not** the job's
`outputs` — verified against the live API on 2026-10-26 (the job object's keys
contained `steps` and no `outputs`). So:

* a job output is only visible **inside** the run (to `needs.<job>.outputs`);
* a consumer looking at run history (a workflow, the app, a person) can only
  read **step presence/conclusion** — or the job/run conclusion.

Both existing precedent and every fix here follow that: `deploy_run_attempts.py`
reads the "Deploy to app.modulo.run" **job** conclusion, the k8s-conformance
staleness job reads the "Run the runtime-provider conformance suite" **step**
conclusion, and the FAR-2157 markers are dedicated steps. A future edit that
replaces a marker with a job output silently disarms the signal — the guard suite
`backend/tests/architecture/test_workflow_noop_signal.py` fails if it happens.

## Inventory

| Workflow | Substantive work | Path to success without it | Machine-readable outcome | Summary says so | Consumers of the conclusion | Classification |
|---|---|---|---|---|---|---|
| `audit-chain-monitor.yml` | Verify the audit chain + 24h continuity | No admin credentials → every check skips | **Added:** `authenticated=false` + NOT RUN marker step | **Added:** NOT RUN summary | Operator / run history | **Masking — fixed** |
| `backup-restore-nightly.yml` | Backup → restore → downgrade → upgrade gate | None (every step runs) | Gate fails loudly | n/a | Operator | Legitimate |
| `bdd.yml` | BDD/E2E suite | Docs/frontend-only PR → suite steps skip (`Detect changed paths`) | Step-level skip, documented | Log | PR checks (none required) | Legitimate no-op (nothing to run) |
| `branch-fixer.yml` | Dispatch the Branch Fixer | None — always dispatches; a failed dispatch exits 1 | Dispatch result + fail | n/a | PR Housekeeper (re-drives from PR state) | Legitimate |
| `bundle-release.yml` | Version parity + build the single-install bundle | None | Fails loud | n/a | `release-unified.yml` (`workflow_call`) | Legitimate |
| `changelog-classify.yml` | Classify the PR's changelog label | Job always exits 0 | **Check run** with the real verdict (neutral/success/failure) | Check title/summary | Merge queue `checks_pass` | Legitimate (verdict is a check) |
| `ci.yml` | Lint/test/build gates | Path-gated jobs skip their steps for docs-only/frontend-only PRs | Step-level skip, documented; self-report is fail-open with a warning | Log | Merge queue CI gate, Branch Fixer | Legitimate no-op |
| `codescene-analysis.yml` | CodeScene delta analysis | None (missing token fails) | Fails loud | n/a | Operator | Legitimate |
| `dast-auth.yml` | Authenticated DAST scan | None (missing creds exit 1) | Fails loud | n/a | Operator | Legitimate |
| `dast.yml` | Baseline DAST scan | Never fails on findings **by design** | ZAP report artefact | Summary | Enforcement is deploy.yml's `dast-gate` | Legitimate (documented report-only) |
| `deploy-staleness-check.yml` | Prod staleness + consecutive-failure escalation | Every source unreadable → `healthy` empty, verdict step skipped | **Added:** UNKNOWN-tick marker step | **Added:** NOT MEASURED summary | Its own next tick (transition email state vector) | **Masking — fixed** |
| `deploy-watchdog.yml` | Fly-level self-heal / re-dispatch | Same UNKNOWN verdict shape | **Added:** UNKNOWN-tick marker step; explicit verdicts elsewhere | **Added:** NOT MEASURED summary | Its own next tick | **Masking — fixed** |
| `deploy.yml` | Staging + prod deploy | Throttle / already-in-progress → success, nothing shipped | `deployed` output + `NO DEPLOY` markers (FAR-2152 / PR #2405) | Yes (`NO DEPLOY — …`) | deploy-staleness-check, deploy-watchdog, weekly report | **Masking — fixed in #2405 (not redone)** |
| `determination-eval.yml` | Score determination fixtures | None | Fails loud | Log | Operator | Legitimate |
| `duplicate-delivery-guard.yml` | Warn on duplicate open PR titles | Guard not on the base commit yet (self-bootstrap) | `::warning::`, documented as expected | Log | PR checks | Legitimate (short-lived self-bootstrap) |
| `fast-lane-post-merge-audit.yml` | Audit that fast-lane merges got a review | No fast-lane merges in the window | Step log | Log | Operator | Legitimate no-op |
| `fast-lane-post-merge.yml` | Dispatch the post-merge review | Not a fast-lane merge (most pushes) | `::notice::` / `::warning::` | Log | `fast-lane-post-merge-audit.yml` measures the **review comment**, not the run | Legitimate (consumer measures the artefact) |
| `fast-lane-suspension.yml` | Record a suspension marker from a comment | Comment lacks the marker (trigger filter) | Log | Log | Operator | Legitimate (trigger-filtered) |
| `flyctl-version-drift.yml` | Detect a stale flyctl pin | Pin current and no open issue | Fails loud on unknown; "nothing to do" in summary | Yes | Operator | Legitimate |
| `issue-notify.yml` | Email on a new external issue | Issue is internal / from a bot | Log; send failure exits 1 | Log | Operator | Legitimate (no notification needed) |
| `k8s-conformance.yml` | Kind + managed conformance gate | Path-filtered kind leg skips the suite | **Staleness job reads the suite STEP conclusion** (FAR-1620) | Yes | The workflow's own staleness job | Legitimate (consumer measures the artefact — exemplar) |
| `main-integration.yml` | Integration tests on main | None | Fails loud | n/a | Operator | Legitimate |
| `merge-queue-watchdog.yml` | Detect/cancel stuck queue runs, re-arm | Healthy queue → no action | Emits alert outputs; **section 5 measures the last merged PR** | Yes | Operator / Linear | Legitimate (consumer measures merges) |
| `merge-queue.yml` | Merge approved + green PRs | No approved PRs, or approved but none mergeable → merges nothing | **Added:** `NO MERGE` summary on both no-op paths (step presence on the no-op path) | **Added** | merge-queue-watchdog (already measures merged PRs) | **Masking (legibility) — fixed** |
| `mutation-testing.yml` | Weekly mutation baseline | None | Fails loud | Report | Operator | Legitimate |
| `native-gate0.yml` | Offline bundle boot proof on PRs | None — asserts every precondition | Fails loud | Log | PR checks | Legitimate (exemplar) |
| `native-release-smoke.yml` | Install + smoke a published bundle | (a) No published `bundle-v*` release → canary skip; (b) a `workflow_dispatch` with NEITHER `canary_version` NOR `rollback_version` → no verification job is eligible, so only housekeeping runs | (a) `::notice::`, documented "honestly not applicable"; (b) **Added:** `dispatch-guard` job fails with an `::error::` naming both inputs | (a) Log; (b) error annotation | (a) `canary-housekeeping` deliberately treats it as not-applicable; (b) operator | (a) Legitimate (documented); (b) **Masking — fixed (FAR-2161)** |
| `pr-conflict-reconcile.yml` | Dispatch the fixer for conflicted PRs | `gh pr list` fails → fail-open skip | `api_failed` output + **Added:** NOTHING RECONCILED summary | **Added** | Operator | **Masking (minor) — fixed** |
| `pr-review.yml` | Dispatch the PR reviewer | Draft / dedup / superseded / manual-delivery / fork → no dispatch | `ready=false` output + `::notice::` | Log | PR Housekeeper re-drives from the **review artefact** | Legitimate (consumer measures the artefact) |
| `publish-images.yml` | Publish container images to GHCR | None | Fails loud | n/a | Operator | Legitimate |
| `rc-validate.yml` | Pen-test an RC against prod | None (missing secrets exit 1) | Fails loud | n/a | Operator | Legitimate |
| `release-unified.yml` | Cut / recut / promote / dry-run | None — each mode has its jobs | Fails loud on every guard | Yes | Operator | Legitimate |
| `release.yml` | (retired entry point) | n/a | **Fails loudly**, naming the replacement | Log | Operator | Legitimate (deliberately red) |
| `runner-ci-backstop.yml` | Weekly forced harness run | None | Fails loud | n/a | Operator | Legitimate |
| `runner-ci-harness.yml` | Bundled-runner harness | Path filter skips when the surface is untouched | Skip documented; the weekly backstop forces it | Log | `runner-ci-backstop.yml` | Legitimate |
| `schemathesis-nightly.yml` | API fuzz | None | Fails loud | Report | Operator | Legitimate |
| `signing-canary.yml` | Sign + verify a release artefact | Dispatch from a non-main branch skips (documented) | Log | Log | Operator | Legitimate (manual-only edge) |
| `tier1b-nightly.yml` | Heavy Tier 1b suite | None | Fails loud | n/a | Operator | Legitimate |
| `web-watchdog.yml` | Restart a wedged app machine | Machine healthy / state unknown → no action | **`no-op: …` in the job summary on every path** | Yes | Operator | Legitimate (exemplar) |
| `workflow-lint.yml` | actionlint over the workflow tree | None — refuses to run without shellcheck | Fails loud when it would otherwise be blind | Log | PR checks | Legitimate (exemplar) |

Notes:

* **`housekeeping`** (named as a candidate) is not a GitHub workflow in this
  repo — it is the app-side housekeeping pipeline plus the local skill. Nothing
  to classify here; the workflow-lint/housekeeping distinction is not a workflow
  no-op.
* **`deploy.yml`, `deploy-watchdog.yml` (throttled-run handling),
  `deploy_health.py` and the deploy consumers** are fixed by PR #2405
  (FAR-2152). This sweep did not redo them; it added only the *UNKNOWN-tick*
  state-vector marker, which is a different defect (a tick that measured
  nothing, not a deploy that was throttled).
* **`pr-review.yml` / `fast-lane-post-merge.yml` / `k8s-conformance.yml`**
  were the ticket's strongest candidates and each was *verified*, not assumed:
  their no-ops are covered by a consumer that measures the artefact (the review
  comment, the suite step conclusion, the merged PR), so no marker was added.

## Fixes and their guards

| Case | Fix | Guard |
|---|---|---|
| Deploy-health UNKNOWN tick read back as HEALTHY | Marker step in both escalation workflows; `parse_previous_verdict` returns UNKNOWN; the transition email walks back to the last **measured** verdict | `test_unknown_tick_marker_name_matches_the_parser_constant`, `test_unknown_tick_marker_is_a_summary_and_never_fails_the_run`, `test_deploy_health_consumer_walks_back_past_unmeasured_ticks`, `tests/unit/scripts/test_deploy_health.py` (marker mapping) |
| Merge queue pass that merged nothing was silent | `NO MERGE` summary on both no-op paths | `test_merge_queue_no_approved_prs_pass_is_marked`, `test_merge_queue_nothing_mergeable_pass_is_marked` |
| Audit-chain monitor blind cycle was green silence | `authenticated` output + NOT RUN marker step + summary | `test_audit_chain_monitor_missing_credentials_is_marked_not_run` |
| Conflict reconciler blind cycle was silent | NOTHING RECONCILED summary on the API-failure path | `test_pr_conflict_reconcile_api_failure_is_marked` |
| `native-release-smoke.yml` green run with no target | `dispatch-guard` job rejects an argument-free manual dispatch with an `::error::` naming `canary_version` / `rollback_version`; push and schedule paths untouched | `test_native_release_smoke_rejects_an_argument_free_dispatch`, `test_native_release_smoke_dispatch_paths_keep_their_own_gates` |
