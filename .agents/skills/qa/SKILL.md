---
name: qa
description: >-
  Runs a multi-lens quality review on a given path/package, surfacing issues as
  critical, major, or minor. Each lens fires a subagent; the parent validates
  findings and addresses valid ones.
disable-model-invocation: true
agents:
  openai:
    policy:
      allow_implicit_invocation: false
---

# QA Review Skill

## When to use

Invoke this skill when asked to review code quality, perform a code review, audit a package, or when a delivery tracking item says "QA: review <path>". Also use it proactively when touching unfamiliar packages to understand their quality posture before making changes.

## Input

```
qa <target-path> [--lens correctness,bugs,maintainability,solid,dry,simplify,deps]
qa session                        [--lens ...]   # QA work delivered in the current conversation session
qa in-flight                      [--lens ...]   # QA uncommitted working-tree changes
qa branch [<branch>]              [--lens ...]   # QA commits on a branch not yet on main (default: HEAD)

# session, in-flight, and branch modes auto-discover the file set to review.
```

Default lenses (all):
- `correctness` — logic errors, edge cases, input validation, type safety
- `bugs` — runtime errors, exception handling, resource leaks, concurrency issues
- `maintainability` — naming, cohesion, coupling, testability, documentation gaps
- `solid` — single responsibility, open/closed, Liskov, interface segregation, dependency inversion
- `dry` — duplication of logic, data, or structure across the target
- `simplify` — unnecessary complexity, over-engineering, flattening opportunities
- `deps` — hand-rolled logic that a well-known library already solves

## Output

A structured QA report. Each finding has the form:

```
## <lens-name> — <severity>

| Severity | File:Line | Finding | Suggestion |
|----------|-----------|---------|------------|
| critical/major/minor | path:line | description | concrete fix |
```

Severity definitions:
- **critical** — causes incorrect behaviour, data loss, or crashes in normal usage
- **major** — makes the code harder to maintain, extend, or understand; likely to cause bugs
- **minor** — style, naming, or trivial improvements with low risk

## Target discovery (session / in-flight / branch modes)

When no explicit path is given, the agent discovers which files to review:

### `qa session`
1. Scan the conversation history for every file that was created or modified by the agent (use tool calls — Read, Write, Edit — to build the list).
2. Deduplicate by real path; include only files that still exist on disk.
3. Report the discovered set to the user before proceeding: "Reviewing N files from this session: [paths]"
4. Feed those files into the standard lens pipeline.

### `qa in-flight`
1. Run `git status --porcelain` in the repo root.
2. Collect all files with status `M` (modified in working tree), `A` (added), `??` (untracked).
3. Exclude generated/lock files (`uv.lock`, `package-lock.json`, `*.pyc`, `.pytest_cache/`, etc.) and binary files.
4. If the set is empty, report "No uncommitted changes to review."
5. Report the discovered set to the user: "Reviewing N in-flight files: [paths]"
6. Feed those files into the standard lens pipeline.

### `qa branch [<branch>]`
1. Default branch is `HEAD` (current branch).
2. Run `git log --oneline main..<branch>` to find commits not on main.
3. If the branch is already merged or has no new commits, report "No unreviewed commits on this branch."
4. Collect all files changed across those commits: `git diff --name-only main...<branch>`.
5. Report: "Reviewing N files across M commits on branch `<branch>`: [paths]"
6. Feed those files into the standard lens pipeline (reading the current working tree versions).

## Process

### 1. Parent agent reads the target

The invoking agent must first read all files in the target set to build context. For session/in-flight/branch modes, the file list is discovered first (see above), then read in full.

### 2. Spawn subagents (one per lens)

Launch one `general` subagent per enabled lens. Each receives:
- The full source of the target files
- The lens definition (what to look for)
- Instructions to return findings as a JSON array of `{severity, file, line, finding, suggestion}`

All subagents run in parallel.

### 3. Parent validates findings

For each subagent result, the parent:
1. Re-reads the relevant file/line to confirm the finding
2. Dismisses false positives (mark as `invalid` in the output)
3. For valid findings: determines what action to take and applies the fix
4. If uncertain about a finding, leaves it as `flagged` for human review

### 4. Apply fixes

Valid findings with concrete, correct suggestions are applied to the codebase immediately (edit tool). The report captures what was changed.

### 5. Output summary

After all lenses are processed, emit a final summary:

```
# QA Review: <target-path-or-mode>

## Summary
- Critical: X (Y fixed, Z invalid, W flagged)
- Major:    X (Y fixed, Z invalid, W flagged)
- Minor:    X (Y fixed, Z invalid, W flagged)

## Details
<concatenated lens findings with resolution status>
```

### 6. Lessons learned (automatic codification)

After the summary is emitted, evaluate whether any fixed findings represent recurring patterns that should be codified as project guidance:

1. Collect all findings marked as `fixed` (severity, file, finding, suggestion)
2. Load the `lessons-learned` skill (`.agents/skills/lessons-learned/SKILL.md`) and invoke it with the target path + list of valid findings
3. The skill determines placement (deepest ancestor with AGENTS.md), deduplicates, and appends to the appropriate file
4. Include the lessons-learned output in the final report

If no valid findings were fixed (all were invalid or flagged), skip this step entirely — nothing to learn.

## Sequencing for delivery tracker

When run as a delivery-tracker item, QA reviews are sequenced one after another (not in parallel). Each review is a separate tracker item. After finishing one, the agent moves to the next.

## Directory junction (Windows)

```powershell
# Run from repo root to expose skill to Claude Code
mklink /J .claude\skills\qa .agents\skills\qa
```

## Examples

```
# Explicit path — review a specific package
Task: QA review backend/src/modulo/auth/

# Session mode — review everything built in the current conversation
Task: QA session

# In-flight mode — review uncommitted changes in the working tree
Task: QA in-flight

# Branch mode — review a feature branch's delta against main
Task: QA branch agent/task-foo
Task: QA branch HEAD
```

Examples of the discovery modes:

**`qa session`**: The agent scans its own tool-use history, finds 6 files were written (BrandMark.vue, AppLogo.vue, SidebarLayout.vue, etc.), reports them, then runs all 7 lenses on those 6 files.

**`qa in-flight`**: The agent runs `git status`, finds 3 modified and 2 untracked files, excludes `uv.lock` as generated, reviews the remaining 4 files across all lenses.

**`qa branch feature/foo`**: The agent runs `git log main..feature/foo`, finds 3 commits touching 8 files, runs `git diff --name-only main...feature/foo`, reads both old and new versions where helpful, reviews all 8 files.
