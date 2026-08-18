# Lessons Learned Index

Remaining semantic lessons in `AGENTS.md` after Semgrep-covered entries were removed.

**Legend:** `S` = Semantic code pattern, `W` = Workflow/process, `I` = Infrastructure, `A` = Architecture

| # | Type | Topic | File | Lines |
|---|---|---|---|---|
| 1 | I | Branch-fixer / opencode coder agent (CI step ordering, `repository_dispatch`, `gh run view`) | `Repos/devtools/AGENTS.md` (moved in FAR-287) | - |
| 2 | I | Pre-commit hooks configuration and split stages | `AGENTS.md` | 599-632 |
| 3 | W | Rebasing: merge conflict resolution workflow | `AGENTS.md` | 634-656 |
| 4 | I | Test suites: how to run backend and frontend tests | `AGENTS.md` | 658-668 |
| 5 | I | Frontend worktrees lack node_modules; verification via CI | `AGENTS.md` | 670-675 |
| 6 | W | Systemic patterns: apply as bulk sweeps, not per-feature QA | `AGENTS.md` | 677-682 |
| 7 | W | Test rot: fix recurring patterns once across all files | `AGENTS.md` | 684-688 |
| 8 | W | Parallel Workers: overlapping file footprints need conflict resolution | `AGENTS.md` | 690-695 |
| 9 | S | Health check `finally` block `conn.close()` can override inner `return` | `AGENTS.md` | 696-700 |
| 10 | S | Deployment: return "degraded" not "unavailable" for non-critical checks | `AGENTS.md` | 702-704 |
| 11 | S | vue-i18n: message compiler cannot parse JS ternary expressions | `AGENTS.md` | 706-708 |
| 12 | S | npm install on Windows generates lockfile with platform-specific packages | `AGENTS.md` | 710-712 |
| 13 | S | Database / Multi-backend: tenant filter, JOIN, DML, timezone, dialect names | `AGENTS.md` | 714-720 |
| 14 | S | Locking: advisory lock polling, ownership tracking, shared module state | `AGENTS.md` | 722-726 |
| 15 | W | Frontend / Layout: empty states, enterprise-gated pages | `AGENTS.md` | 728-731 |
| 16 | W | Frontend / API & Errors: avoid full-page redirects on API failure, 401 interceptor | `AGENTS.md` | 733-736 |
| 17 | S | Frontend / Security: sensitive Runtime Config masking, enterprise sidebar links | `AGENTS.md` | 738-741 |
| 18 | W | Frontend / Layout (continued): mobile dropdowns, header sizing, scroll containers | `AGENTS.md` | 743-750 |
| 19 | W | Product Map / improve-architecture: frontmatter, YAML, BDD fields, gap vs partial | `AGENTS.md` | 752-766 |
| 20 | S | Backend / API Schema Migrations: `validation_alias`, `populate_by_name`, column renames | `AGENTS.md` | 768-771 |
| 21 | S | Backend / Models: ORM/column sync, `__table_args__`, optional Pydantic defaults | `AGENTS.md` | 773-777 |
| 22 | S | Ops / Deploy: `flyctl` build-args, Python version hardcodes in `fly.toml` | `AGENTS.md` | 779-783 |
| 23 | S | Frontend / Resilient Rendering: `\|\|` fallback instead of `v-if` for empty API data | `AGENTS.md` | 785-787 |
| 24 | S | Backend / Error Tracking: session.begin() scope, per-forwarder isolation, cooldown keys | `AGENTS.md` | 789-793 |
| 25 | S | Backend / CLI Tools: Click decorator ordering on command function | `AGENTS.md` | 795-797 |
| 26 | S | Backend / Caching & Init Ordering: defensive copies, init flag ordering, exception types | `AGENTS.md` | 799-803 |
| 27 | S | Backend / Dashboard & Aggregations: idle count formula, Redis connection cleanup | `AGENTS.md` | 805-808 |
| 28 | S | Frontend / Internationalization: non-user-facing artifacts, locale sync shape | `AGENTS.md` | 810-813 |
| 29 | W | Frontend / Store & View Patterns: computed dedup, validation level, error handlers | `AGENTS.md` | 815-821 |
| 30 | S | Backend / Async & Concurrency: lazy-init side effects in dual-channel classes | `AGENTS.md` | 823-825 |
| 31 | S | Backend / BDD Feature Tests: path consistency, backgrounds, import strategies | `AGENTS.md` | 827-834 |
| 32 | S | Ops / Deploy Workflow: dirty tree guard, deploy scripts, stash recovery, lifespan error handling, pre-deploy build | `AGENTS.md` | 836-848 |
| 33 | S | entrypoint.sh: migration revision IDs must match Alembic filenames | `AGENTS.md` | 850-852 |
| 34 | W | Fix Workers must be scoped to specific files only | `AGENTS.md` | 854-856 |
| 35 | S | Eval Engine / Error Handling: StrEnum, ReDoS, None coercion, functions config, field paths | `AGENTS.md` | 858-864 |
| 36 | S | Ops / Staging Environment: Fly.io staging machine sizing for E2E tests | `AGENTS.md` | 866-868 |
| 37 | S | Ops / Database (Fly Postgres): unmanaged PG doesn't auto-restart | `AGENTS.md` | 870-872 |
| 38 | A | ADR 003 supersedes ADR 001: Modulo dispatches, doesn't run agents | `AGENTS.md` | 874-894 |
| 39 | S | Fly --strategy immediate desyncs proxy routing table | `AGENTS.md` | 896-916 |
| 40 | S | Playwright E2E: `storageState` for shared login on staging | `AGENTS.md` | 918-941 |
| 41 | W | Never merge PRs directly unless explicitly authorised | `Repos/devtools/AGENTS.md` (moved in FAR-287) | - |
| 42 | W | Manifest: `preview: true` means dev-mode-only, not feature preview | `AGENTS.md` | 956-962 |

## Removed: Semgrep-Covered Lessons

These lessons were removed from `AGENTS.md` because they are now enforced by automated Semgrep rules:

| Removed Lesson | Semgrep Rule | Rule File |
|---|---|---|
| pg_connection_string strips sslmode | `split-on-db-url` | `.semgrep/split-on-db-url.yml` |
| Never embed bare `${err}` in template literals | `bare-err-in-template` | `.semgrep/bare-err-in-template.yml` |
| Translation values: no newlines or HTML entities | `translation-newlines-entities` | `.semgrep/translation-newlines-entities.yml` |
| `asyncio.create_task()` from sync code | `create-task-without-guard` | `.semgrep/create-task-without-guard.yml` |
| Monkey-patching stdlib types | `module-level-mutable-default` | `.semgrep/module-level-mutable-default.yml` |
| Never use `waitForLoadState('networkidle')` | `playwright-no-waitforloadstate` | `.semgrep/playwright-no-waitforloadstate.yml` |
