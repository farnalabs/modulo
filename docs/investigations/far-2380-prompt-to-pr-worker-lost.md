# FAR-2380 - four Prompt-to-PR runs lost to a production worker deploy

**Status:** investigation complete, no code change made
**Date:** 2026-11-04
**Runs:** `399922c1-4e59-4593-8a00-e7788ac9bd60`, `7f2372be-2d0c-4131-a4e5-35cffa297e18`,
`1c53683d-aa32-4fe0-997e-171797e33c09`, `b9cae5f1-d60e-4774-b13d-712400e2cec6`
**Pipeline:** Prompt to PR (`4e918522-b723-4641-9e61-53774028f160`)

## Verdict

This was **not four independent harness failures**. It was **one routine production deploy** that
replaced the single SAQ runs worker while all four runs were mid-sandbox. The FAR-1627
worker-shutdown handoff then terminal-failed each run with `harness.worker_lost` because it
observed durable sandbox activity, which it treats as unsafe to re-execute.

The handoff behaved exactly as designed and as documented in `docs/architecture.md`. There is no
crash, no stale heartbeat, no node-deadline watchdog firing, and no evidence of a model-provider
fault. The trigger is infrastructure/operations (a deploy), and the run-level behaviour is a
deliberate safety trade-off, not a defect in the handoff.

The incident does expose two real product gaps, both secondary to the loss itself:

1. the production deploy path kills **every** in-flight sandbox run (not just these four), and
   deploys were frequent enough in this window that long runs are highly likely to collide;
2. a run that had already delivered a merged PR is recorded as `failed` with
   `delivery_done: false`, and the retained-triage `pr_url` on all four runs points at an
   unrelated PR - so the run record actively under-reports and mis-attributes delivery.

## Impact

| Run | Ticket | Duration | Retained stdout | Sandbox-derived (run total) | Salvaged model self-report | Outcome |
|---|---|---|---|---|---|---|
| `399922c1` | FAR-2364 | 28m 21s | 1,311,463 chars | $0.0614 | $0.1365 | PR **#2835** created 12:00:00Z, merged 14:47Z |
| `7f2372be` | FAR-2365 | 28m 07s | 885,378 chars | $0.0609 | $0.0721 | PR **#2836** created 12:01:20Z, merged 14:00Z |
| `1c53683d` | FAR-2366 | 27m 52s | 599,216 chars | $0.0604 | $0.0877 | no PR, no branch pushed |
| `b9cae5f1` | FAR-2367 | 27m 39s | 1,152,828 chars | $0.0599 | $0.0955 | no PR, no branch pushed |

The four run totals sum to **$0.242653** (sandbox-derived occupancy, matching the analytics
`harness.worker_lost` bucket for the hour). The model-token figure in the breakdown is
`self_reported` and salvaged from retained output; it is not part of `runs.total_cost_usd`.

So: two of the four runs had already delivered and their PRs later merged; the other two lost
their sandbox work. All four are recorded as `failed` / `harness.worker_lost`.

`gh pr list --search "FAR-2366 in:title"` and `"FAR-2367 in:title"` return no PRs.
`git ls-remote origin 'refs/heads/prompt-pr-1793793*'` and `'refs/heads/prompt-pr-1793792*'` return
no branches at all (the two merged PRs' branches were deleted on merge, and the two lost runs never
pushed), so neither lost run ever pushed.

## Evidence

### 1. Identical terminalisation within 2.1 seconds

All four runs were dispatched by SAQ within 45 seconds of each other and were terminalised within
**2.1 seconds** of each other, ~28 minutes into their execution:

| Run | `started_at` (UTC) | `completed_at` (UTC) | `error_code` |
|---|---|---|---|
| `399922c1` | 11:33:11.924 | 12:01:33.071 | `harness.worker_lost` |
| `7f2372be` | 11:33:27.055 | 12:01:33.836 | `harness.worker_lost` |
| `1c53683d` | 11:33:42.215 | 12:01:34.533 | `harness.worker_lost` |
| `b9cae5f1` | 11:33:55.758 | 12:01:35.147 | `harness.worker_lost` |

Every row carries the same `error_detail`: *"Worker executing this run was shut down (deploy or
restart); the run was terminal-failed instead of handed back for re-dispatch because a re-run is
not safe (FAR-1627). Reason: durable sandbox activity was observed; terminalisation is safer than
re-execution."*

That 2-second spread is the loop in `worker_handoff._terminalise_org`, which processes one
shutdown hook's registry entries sequentially. It is not the signature of four independent faults.

Also from the run rows: `claim_count = 1`, `run_attempt_count = 1`, `dispatch_phase =
first_node_dispatched`, `dispatcher = saq`. No retry occurred; the run-detail attempt count is 1.

### 2. The deploy timeline matches to the second

The deploy is GitHub Actions run **41817770086** (`Deploy: Staging + Prod`, `workflow_dispatch`,
head `4fb7fec07232d921e39b865fbb0694fd14704f62`, "docs: position Farna Control as a self-hosted software
factory (#2827)"):

| Step | Started (UTC) | Completed (UTC) |
|---|---|---|
| Deploy to app.modulo.run (job) | 11:58:50 | 12:03:39 |
| Deploy to app-modulo (step) | **12:00:44** | 12:03:27 |
| Pre-deploy migrations | 11:59:59 | 12:00:44 |
| Prod smoke tests | 12:03:41 | 12:04:46 |

Fly machine events corroborate the worker replacement:

```
misty-bird-7418 (worker)  image app-modulo:deploy-4fb7fec07232
  launched 12:00:50.063Z / 12:00:50.082Z
  stopped  12:00:53.186Z           state: stopped
summer-frost-9104 (worker) image app-modulo:deploy-4fb7fec07232
  launched 12:00:50Z, started 12:03:16.632Z   state: started, worker_health passing
```

Both worker machines were re-created at 12:00:50 by the deploy. One was stopped three seconds
later; the serving worker's replacement did not report `started` until 12:03:16, so
12:00:50-12:03:16 is the version hand-over window. The runs were terminalised at 12:01:33, i.e.
**49 seconds after the production deploy step began and 43 seconds after the hand-over began**,
while the new version was still booting. No other deploy workflow overlapped that window.

The 43-second gap is also consistent with the code: SAQ's `_SHUTDOWN_GRACE_PERIOD_S = 30` from the
SIGTERM at 12:00:50 puts the start of cancellation at ~12:01:20, and the shutdown callback (the
handoff) runs after the cancelled task set unwinds - 13 seconds later at 12:01:33.

### 3. Why the handoff terminalised instead of re-dispatching

`worker_handoff._handback_decision` (backend/src/modulo/core/worker_handoff.py:427) applies the
durable-evidence gate **before** the zero-node, idempotency and retry-policy arms:

```python
if getattr(row, "observed_activity", False):
    return _HandbackDecision(
        may_requeue=False,
        node_start_safe=False,
        reason="durable sandbox activity was observed; terminalisation is safer than re-execution",
    )
```

`observed_activity_expr` (backend/src/modulo/db/crud/run_node_outputs.py:1129) is `EXISTS` over
`run_node_outputs` for any engine-observed evidence: output stream lengths, retained tails,
artifact pointers, non-empty raw-output markers/artifacts/schema-enforcement. It deliberately
excludes a self-reported agent return and a lifecycle-only telemetry envelope.

These four runs all had a live E2B sandbox and captured stdout (599k–1.3M chars), and the retained
sandbox log tails show `Sandbox created`, `Process with pid 1543 started`, then file/CPU activity.
The gate therefore returned false for `may_requeue`, and `_terminalise_entry` failed the run in
place via `fail_run_terminal` + `record_worker_lost_derived_cost`.

This is the documented contract in `docs/architecture.md` ("Worker-shutdown handoff (FAR-1627)",
lines 371-395) and in the module docstring: re-running after observed/derived activity can repeat
side effects and discard evidence, so the run is failed in place rather than put on the un-gated
re-dispatch leg.

### 4. The same event hit other pipelines

The run-detail evidence for Improve Public Docs (`1a5b5a33-4a5d-41e1-b642-bcc42138fded`) shows
run `3c76ee2c-9003-415e-a70e-4e3f1e1def31`: started 11:50:05Z, completed **12:01:36.283Z**, also
`harness.worker_lost` with the same "durable sandbox activity" detail - three seconds after the
four Prompt-to-PR runs. That is the same worker-shutdown event.

Analytics (`modulo_query_analytics`, `error_code=harness.worker_lost`, hourly buckets for
2026-11-04) also records in the 11:00-12:00 window: Branch Fixer 3, Improve Database 1,
Improve Public Docs 1, PR Reviewer Agent 1, plus PR Housekeeper 1 and PR Reviewer Agent 1 in the
12:00 bucket. The hourly bucket is the run's start hour, so those counts span the two deploy workflows that
overlapped the window (the workflow created at 10:08 completed at 11:08, and the workflow created
at 10:58 completed at 12:04) - not a single event - but the pattern is the same: a deploy kills
whatever sandbox work is executing on the prod worker.

The same error code has a recurring daily history: 18 runs on 2026-10-21, 7 on 2026-10-22,
2 on 2026-10-20, 4 on 2026-11-03, 15 on 2026-11-04 (daily analytics, last 30 days). This is an
ongoing operational class, not a one-off.

### 5. Delivery evidence is under-reported on the two runs that delivered

- `399922c1` opened PR #2835 at 12:00:00Z (head `prompt-pr-1793792035`, the branch named in its
  raw output), merged at 14:47Z.
- `7f2372be` opened PR #2836 at 12:01:20Z (head `prompt-pr-1793792059`), merged at 14:00Z.

Both runs nevertheless report `delivery_done: false` in their retained deadline-triage entries.
The marker field is sentinel-only (`_source_contains_delivery_sentinel` requires the configured
`PR_CREATED` line to appear as a full line in the drained stdout). Since the PRs demonstrably
existed before the kill, the sentinel was either not present in the drained tail at cancel time or
not captured; the run record therefore classifies delivered work as undelivered.

### 6. Secondary defect: the triage `pr_url` is contaminated on all four runs

Every one of the four retained triage entries reports:

```
"pr_url": "https://github.com/farnalabs/farna-control/pull/686"
```

PR #686 is unrelated (it is the September relocation of lessons into the workspace AGENTS.md). The
only occurrence of that URL in the repository is `AGENTS.md:63`:

```
> ([PR #686](https://github.com/farnalabs/farna-control/pull/686)):
```

`node_runner._extract_pr_url` takes the **first** URL-shaped token in the retained stdout
(`_PR_URL_PATTERN.search`, node_runner.py:2240), and a Prompt-to-PR run reads the repo `AGENTS.md`
early - the captured opening steps of `b9cae5f1` show a `cat AGENTS.md` call. The documentation at
`api/routes/runs.py:408-413` already calls this heuristic
"not anchored to `gh pr create`" and non-gating, but in practice it is misleading: for `7f2372be`
the tail even contains `"url":"https://github.com/farnalabs/farna-control/pull/2836"`, yet the projected
value is #686.

This does not gate delivery, retry or cost, but it undermines the run-detail surface an operator
uses to answer "did this run deliver?".

### 7. The two lost runs had not followed the delivery-first instruction

`1c53683d` and `b9cae5f1` were still in the qa-iterate/coverage phase at ~27 minutes without
having pushed a branch or opened a PR, despite the node prompt's step-8 instruction (FAR-1914:
open the PR as soon as the first push succeeds, then keep working on the same branch). Their raw
tails at 12:00-12:01 show skill reading and coverage analysis, not delivery. By contrast, the two
runs that did deliver had opened PRs 93 and 13 seconds before the kill.

This is an agent-compliance observation, not the cause of the loss - the deploy would have killed
all four either way - but it materially changed the outcome from "4 delivered, 4 recorded failed"
to "2 delivered, 2 lost".

## What this is not

- **Not a hard crash / OOM / SIGKILL-only event.** The handoff ran, which means the SAQ shutdown
  path completed; the machine was stopped as part of a planned deploy, not lost.
- **Not a stale-heartbeat sweep.** Those write `worker_lost` (legacy spelling) or
  `harness.heartbeat_stale` after the 30-minute TTL. These rows carry the dotted
  `harness.worker_lost` from `worker_handoff`, written within a minute of the shutdown.
- **Not a node-deadline watchdog kill.** `node.deadline_exceeded` is a different code; the node
  budget is 3300s and these runs died at ~28 minutes.
- **Not a provider or free-model problem.** No `agent.failed` / `sandbox.no_output_json`; the
  agents were producing output continuously up to the kill (stdout 0.6–1.3 MB, live step records
  in the retained tail) and two of them created PRs.

## Recommended remediation

Ranked by durability per unit of risk. None was implemented here: the first is a
deployment-policy change, and the second changes delivery evidence semantics; neither is safe to
land as a drive-by under an investigation ticket.

### 1. Primary - stop a production deploy from rolling the worker while sandbox runs are in flight

This is the only option that preserves the work rather than reporting its loss more accurately.
The deploy already owns the decision point (the `Deploy to app-modulo` step), so the check belongs
there:

- before `fly deploy`, query Farna Control for `runs.status = 'running'` with a sandbox dispatch
  (`dispatch_phase = 'first_node_dispatched'` and sandbox activity), or expose a small read-only
  "in-flight sandbox runs" endpoint / MCP read;
- if any exist, **wait a bounded window** (e.g. 10 minutes, polling every 30s) before continuing;
- then deploy anyway with a loud annotation, or require an explicit `force_deploy` input, so
  hotfixes are never blocked indefinitely;
- skip the wait entirely for staging (staging deployments do not host the prod runs).

Trade-off: every deploy can be delayed by up to the wait window while long sandbox work runs.
Given the observed deploy cadence (multiple prod deploys per hour in this window) and Prompt-to-PR
node budgets of 55 minutes, a long unbounded wait would be unacceptable; the bounded
wait-plus-override shape is the compromise.

The alternative - a second always-on worker and a blue-green replace - does **not** fix this on its
own: Fly still sends SIGTERM to the old worker, and the handoff still terminalises observed-activity
runs after the drain window. It only removes the ~2.5-minute no-worker gap.

### 2. Complement - make the run record tell the truth about delivery

Two independent changes, both small and testable:

- **Preserve delivery evidence across a shutdown kill.** When the cancellation drain observes the
  configured `PR_CREATED` sentinel (or a PR URL anchored to a `gh pr create` tool result), set
  `delivery_done` and record the URL; the `1c53683d`/`b9cae5f1` outcome stays `failed`, but
  `399922c1`/`7f2372be` become "failed after delivery" rather than plain failures.
- **Anchor `pr_url` extraction.** Restrict `_extract_pr_url` to a PR URL that follows the delivery
  sentinel or appears in the output of a `gh pr create` command, or at minimum take the last
  (most recent) match rather than the first. Today's first-match rule will always pick up the
  `AGENTS.md` link in this repo. This is diagnostic-only and must remain non-gating, but it is
  currently wrong on 4/4 observed runs.

Suggested regression coverage: a unit test that feeds a raw-output stream containing the
`AGENTS.md` `pull/686` link followed by a real `gh pr create` URL and asserts the real URL is
selected; and a worker-handoff test asserting that a sentinel observed before cancellation
produces `delivery_done=True` on the triage marker.

### 3. Operational - do not dispatch long Prompt-to-PR batches into a deploy window

The four runs were dispatched at 11:33, while deploy run 41817770086 was already in flight
(its job started at 11:09, the staging deploy ran 11:39-11:45 and staging E2E until 11:58); the
production rollout began at 12:00:44. Nothing in the run input or the Prompt-to-PR graph carries
deploy state, so the dispatching agent had no in-product signal. Until (1) exists, check
`gh run list --workflow deploy.yml` before dispatching a long batch, or run the batch against a
non-deploying environment.

The two losing tickets (FAR-2366, FAR-2367) need a plain re-dispatch; there is no recoverable
sandbox state.

## Why not simply re-dispatch on worker loss

The zero-node and idempotency arms of the handback already support re-dispatch, but the
observed-activity gate wins first. Re-executing a sandbox node starts a fresh E2B sandbox and
repeats all model work and side effects; the Prompt-to-PR prompt's one-branch/one-PR guard would
limit duplicate PRs, but the cost and the risk of a second branch are real. Loosening that gate
would be a product policy decision, not a bug fix. If it is ever revisited, the natural scope is
`idempotent: true` graphs plus `single_pr_per_run`, bounded by the retry budget - and it should be
opt-in per pipeline, not the default.

## Reproduction / verification commands

Runtime evidence (Farna Control MCP):

```
modulo_get_run_status(<run_id>, detail=True)     # all four: status, error_detail, deadline_triage
modulo_get_pipeline_graph("4e918522-…")          # node id, timeout_seconds=3300, sandbox_agent
modulo_list_runs(pipeline_id="4e918522-…")       # dispatch/claim/attempt counts, neighbours
modulo_query_analytics(error_code="harness.worker_lost", group_by="day"|"hour")
modulo_list_runs(pipeline_id="1a5b5a33-…")       # Improve Public Docs 12:01:36 confirmation
```

Deploy / repository evidence:

```
gh run list --workflow deploy.yml --limit 60 --json …
gh api repos/farnalabs/modulo/actions/runs/41817770086/jobs
flyctl machines list -a app-modulo
flyctl machine status 811d136c5e1dd8 -a app-modulo
flyctl machine status 683e6e9b529418 -a app-modulo
gh pr list --state all --search "FAR-2364 in:title"
gh pr list --state all --search "FAR-2365 in:title"
gh pr list --state all --search "FAR-2366 in:title"   # []
gh pr list --state all --search "FAR-2367 in:title"   # []
git ls-remote origin 'refs/heads/prompt-pr-1793792*'  # no lost-run branches
```

Code trace:

```
backend/src/modulo/core/worker_handoff.py:427        # _handback_decision durable-evidence gate
backend/src/modulo/core/worker_handoff.py:629        # _terminalise_entry (fail in place + cost)
backend/src/modulo/db/crud/run_node_outputs.py:1129  # observed_activity_expr
backend/src/modulo/core/saq_worker.py:2504           # _shutdown_handoff_hook
docs/architecture.md:371-395                         # FAR-1627 contract
fly.toml:71-94                                       # kill_signal/kill_timeout=180 (FAR-2004)
```

## Limitations of this investigation

- The worker's process logs were not available (`flyctl logs -a app-modulo --no-tail` returned no
  matching lines), and the audit log requires an admin-scope caller while this session is operator
  scope. The deploy timeline and Fly machine event log are therefore the strongest shutdown
  evidence available, and they line up to within a few seconds.
- The full retained raw output is not exposed by the run-detail API (only bounded head/tail
  slices and the marker's `pr_url`/`delivery_done`), so the exact moment the `PR_CREATED` sentinel
  was or was not captured cannot be observed directly. The statement that delivery evidence was
  under-reported rests on the PR timestamps versus `delivery_done: false`.
- The analytics facts table reports `facts_freshness_hours: 16.7`; the run rows themselves were
  read live and are the primary evidence for the four runs.
