# Pipeline Scripts

Reference copies of the `agent_command` scripts stored inline in each pipeline graph on `app.modulo.run`.

## Purpose

The 8 dogfooding pipelines on `app.modulo.run` execute their core logic via an `agent_command` field embedded directly in each pipeline's graph definition (within the agent node's configuration). This field is stored as a JSON string in the database — **not in version control**. That means:

- **No audit trail** — you can't `git blame` a pipeline script
- **No rollback** — reverting to a previous version requires manual copy/paste from the app UI
- **No review process** — changes bypass PRs entirely

These reference copies fix that by providing a version-controlled source of truth.

## Versioning Pattern

1. **Canonical source** — `backend/pipeline-scripts/*.py` in `main` is the canonical version
2. **Deployment to app.modulo.run** — When a script changes here, a maintainer must copy the contents into the corresponding pipeline's agent node configuration via the app UI (or via the MCP API's `update_pipeline_graph` tool)
3. **Version alignment** — The file's git hash (`git log -1 --format=%H -- <file>`) and the `last_modified` timestamp serve as the audit record
4. **CI check** — A future enhancement could add a CI step that warns if the pipeline graph on the server has a different script hash than the file here

## Deploying Updates

To update a pipeline script on `app.modulo.run`:

1. Make your changes to the `.py` file here on a branch
2. Get the changes reviewed and merged to `main` via `gate.ps1`
3. After merging, open the pipeline on `app.modulo.run`, find the agent node, and paste the updated script into the `agent_command` field
4. Verify: trigger a test run and check the output

**Alternative (future):** Use `modulo_update_pipeline_graph` to set the `agent_command` programmatically from the file content. This requires an MCP tool update to expose the agent node's `agent_command` field in `update_pipeline_graph`.

## Pipeline Scripts Overview

| File | Pipeline Name | Trigger | Purpose |
|---|---|---|---|
| `codebase-improver.py` | Codebase Improver | Manual | Runs `improve-codebase` skill on a target path |
| `pr-reviewer.py` | PR Reviewer | Pull Request | Multi-lens QA review of PR changes |
| `pr-fix-agent.py` | PR Fix Agent | PR Reviewer output | Auto-fix issues found by PR Reviewer |
| `merge-fixer.py` | Merge Fixer | Failed merge attempt | Auto-resolve merge conflicts via `merge` skill |
| `merge-queue.py` | Merge Queue | Scheduled (cron) | Orchestrate sequential PR merging via `gate.ps1` |
| `issue-triage.py` | Issue Triage | New issue / label | Classify and label GitHub issues |

## Convention

- All scripts follow the same scaffold pattern and must remain syntactically valid Python
- All scripts write their result to `/home/user/output.json` — the pipeline graph's output validation schema consumes this file
- Environment variables for each pipeline are documented in the script's module docstring and the pipeline's configuration on `app.modulo.run`
- Shared utilities (env checks, git operations, opencode invocation, output writing) live in `_common.py` — all scripts import from it via `from _common import *`

### Cost Tracking

Every pipeline run captures `wall_clock_ms` (wall-clock time of the opencode call)
and estimates `cost_estimate_usd` based on ~100 tokens/sec at DeepSeek V4 Flash pricing
($1.50/1M tokens blended). The cost estimate is included in the output JSON.
