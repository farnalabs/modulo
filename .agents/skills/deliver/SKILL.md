---
name: deliver
description: >
  Autonomous delivery sprint for Modulo. Reads the delivery plan,
  resolves any stuck PRs, then picks the next pending task whose
  dependencies are completed. Implements, tests, reviews, and verifies
  the task on a branch, raises a PR with "ready for review" label
  for auto-merge, and loops indefinitely. Invoke with /deliver from
  project root.
disable-model-invocation: true
---

# deliver

Autonomous delivery sprint. The workflow:

1. **Load state** — read `Development/harness/delivery/delivery-plan.json` + `Development/Product/AGENTS.md`
2. **Resolve stuck PRs** — check all open PRs for merge conflicts (`DIRTY` status), fix them by merging main in, then continue
3. **Find next task** — first `pending` task whose entire `dependsOn` array is `completed`
4. **Start task** — `task.ps1 start <id>`, create a branch `deliver/<task-id>-<slug>`
5. **Spiral 4 sub-agents** via the Task tool (implementer → tester → reviewer → verifier)
6. **Verifier commits and pushes** the branch, creates a PR with `--label "ready for review"`, runs `task.ps1 complete`
7. **Post-delivery QA** — if the task has a `qaTarget`, run QA on it
8. **Loop back to step 2** — keep running indefinitely, checking for stuck PRs and new tasks

## Location

- Skill: `.agents/skills/deliver/SKILL.md`
- Conductor: `.agents/agents/conductor/CONDUCTOR.md`
- Delivery plan: `Development/harness/delivery/delivery-plan.json`
- Task script: `Development/harness/tools/task.ps1`
- Codebase guidance: `Development/Product/AGENTS.md`
- CI auto-merge: `.github/workflows/automerge.yml`

## Constraints

- Always use `task.ps1 start` before implementing, `task.ps1 complete` after verification
- Each task gets its own branch — never commit feature work to main
- PRs must have the `ready for review` label to trigger auto-merge
- If a sub-agent fails, block with `task.ps1 block <id>` and sleep 5 min before retrying
- The conduit never exits — it keeps looping, sleeping 5 min when no work is available
