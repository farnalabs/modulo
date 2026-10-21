# FAR-1883 – one ticket delivered by two PRs, and the migration-prefix collision it caused

**Status:** investigation record; no product code changed. Date: 2026-10-20.
Times are UTC. Every claim cites a git object, PR, workflow run or Linear record read directly.

## Summary
FAR-1883 was delivered through two pull requests that both targeted `main`. PR #2017 carried the feature. PR #2029 was opened against `main` from a branch cut from PR #2017's head, so its diff re-carried the whole feature (44 files, +1406/−300 against its merge base), not only the "leftovers" it added. Both branches also carried the same 111-line migration, which claimed numeric prefix `0321` while `main` had meanwhile taken `0321` for a different migration. The merge queue's combined-graph gate then saw two Alembic heads, aborted the batch, and ten approved PRs waited for it.

**Nothing was lost.** After #2017 merged, #2029 was force-updated to a single commit rebased on `main`; its patch content is identical to the original leftovers commit and every hunk of it is present on `main` today.

## Timeline
| Time (UTC) | Event | Evidence |
|---|---|---|
| 2026-10-18 20:39:55 | `a39325615` authors the feature and `0321_pipeline_queue_timeout.py`; `c8822d501` (21:29) and `f13581086` (22:36) follow on `feat/far-1883-queue-timeout` | `git show --stat` |
| 2026-10-18 23:05:55 | PR #2017 opened against `main` (initial head `a39325615`) | `gh pr view 2017` |
| 2026-10-19 10:07:07 | `bd902b406` "docs+ui leftovers" committed with parent `f13581086` | `git log -1 --format='%ci %P' bd902b406` |
| 2026-10-19 10:10:05 | PR #2029 opened against `main` from `chore/far1883-leftovers` | `gh pr view 2029` |
| 2026-10-20 02:37:19 | `3efcd874f` adds `queue_timeout_seconds=None` to a test helper on the leftovers branch | `git show 3efcd874f` |
| 2026-10-20 03:25:53 | The same one-line change lands independently on #2017 as `f409fcf6e` | `gh pr view 2017 --json commits` |
| 2026-10-20 04:24:37 | First failing merge-queue run of the window | `gh run list --workflow merge-queue.yml` |
| 2026-10-20 04:30:41 | PR #1907 merges `0321_organisation_cost_state`, so `main` now holds prefix `0321` | `gh pr view 1907` |
| 2026-10-20 06:39–09:04 | Merge queue reports `Migration heads check failed` on #2029 (four recorded ticks) | PR #2029 comments |
| 2026-10-20 09:42:03 | `2ec97214c` renumbers #2017's migration to `0322_pipeline_queue_timeout` | `git show --stat 2ec97214c` |
| 2026-10-20 09:55:31–09:56:51 | The blocked batch drains: #1996, #1997, #2011, #2069, #2070, #2074, #2076, #2077, #2080, #2082 merge | `gh pr list --state merged` |
| 2026-10-20 11:01:26 | PR #2017 merges as `76013cfb2` | `gh pr view 2017` |
| 2026-10-20 11:28:52 | PR #2029 head force-pushed to `59aee6c2b`, parented on `76013cfb2` | PR #2029 timeline |
| 2026-10-20 13:07:29 | PR #2029 merges as `385322d30` (8 files, +54/−13) | `gh pr view 2029` |

## The two branches were stacked, not independent
`git merge-base bd902b406 2ec97214c` is `f13581086` – the leftovers commit's **direct parent** is a commit on PR #2017's branch. `chore/far1883-leftovers` therefore never branched from `main`; it inherited the three feature commits (`a39325615`, `c8822d501`, `f13581086`) and the migration file from the still-open feature branch. Diffing the original head against its merge base with `main` (`5966c342f`) shows 44 changed files / +1406 / −300, the migration included.

Commits unique to each branch: **#2017** – `f409fcf6e`, `d31f00c0f`, the merge commits and the renumber `2ec97214c`. **#2029** – `bd902b406` (the leftovers change) and `3efcd874f` (the test helper, superseded by the identical `f409fcf6e` on #2017).

Linear had **one** ticket, FAR-1883, not two (`linear_get_ticket`). Its comments contain no second assignment and its relations are only dependencies (`FAR-1343`, `FAR-1344`, `FAR-1882`, `FAR-1342`). There is no evidence of two writers in one worktree or branch: the two deliveries used distinct branches and distinct PRs, and the second was stacked on the first's tip. The duplicate is two branches for one ticket, not a same-tree collision.

## The migration collision
The migration was authored **once** (`a39325615`) and inherited by the leftovers branch; its content is byte-identical at `a39325615`, `f13581086` and `3efcd874f` (same SHA-256). It was never re-authored. At `main`'s `0320_evidence_redaction_rules` tip, the branch's `0321_pipeline_queue_timeout` was a valid single head. After PR #1907 merged `0321_organisation_cost_state` at 04:30:41, `main` had a different migration holding numeric prefix `0321`, and both files descend from `0320` – two heads in the combined graph. The merge queue validates exactly that before merging (`.github/workflows/merge-queue.yml`, "Alembic migration graph has $HEAD_COUNT heads"), so the batch was abandoned rather than merged.

## What was and was not lost
**Not lost.** `git diff f13581086 bd902b406` and `git diff 76013cfb2 59aee6c2b` are the same patch: same eight files, +54/−13, differing only in blob index lines and hunk offsets (the rebase context). `385322d30` has the same diffstat, and all eight files' leftovers hunks are on `main` today – notably `isQueueExpiredRun` and `status_queue_expired` in `frontend/src/utils/runUtils.ts` and `frontend/src/locales/en-US.js`, and the `queue_expired` reason in `docs/product-map/build/runs.md`. The test-helper line `queue_timeout_seconds=None` is on `main` via `f409fcf6e`. No behaviour from either branch is absent from `main`.

## Root cause and the mechanical guard
Root cause: a second PR was opened against `main` for a ticket that already had an open PR, from a branch stacked on that PR's head. Its diff therefore re-asserted the whole feature, including a migration whose numeric prefix had since been taken on `main`.

Prevention, in order of directness:
1. **Duplicate-delivery guard at PR open (recommended).** A `pull_request` check that fails or comments when two OPEN PRs targeting the same base carry the same `FAR-<n>` in their titles. At 2026-10-19 10:10 this would have flagged #2029 against #2017, which had been open since 2026-10-18 23:05. Scope it to PRs targeting `main` so a genuine stacked PR (base = the other PR's branch) and a `[partial]` chain remain legal.
2. **Queue-side: exclude, don't abort.** `fix/merge-queue-skip-poison-pr` (`9b76ddda3`, plus `a2814c955`) checks each approved PR's new migration prefixes before the squash-merge, excludes a colliding PR and posts the reason, so one malformed PR can no longer poison the batch. It is committed on that branch; it was not on `main` when this audit was taken.
3. **Pre-PR migration check.** The `check-migration-heads` hook proves a single linear head *within* the branch; it cannot see `main` moving under an open PR. Any migration-bearing branch should be rebased on current `main` (or `alembic heads` run against a merge with `origin/main`) before the queue validates it.
