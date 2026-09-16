#!/usr/bin/env python3
"""Detect open PRs whose base branch is about to be deleted and retarget them.

When a merged PR's head branch is deleted, GitHub auto-closes any open PR
whose base branch matches the deleted branch (FAR-917: PR #597 merged ->
feat/far-856-org-login deleted -> PR #602 silently closed).

This script:
1. Finds all open PRs whose base matches any branch in the given list.
2. Attempts to retarget each to the specified ref (default: main).
3. Posts an explanatory comment on each retargeted PR.
4. Outputs:
   - protected_branches: space-separated branches that MUST NOT be deleted
     (child PR exists but could not be retargeted).
   - retargeted_prs: space-separated PR numbers that were retargeted.
   - unprotected_count: number of child PRs successfully retargeted.
"""

import json
import subprocess
import sys
import textwrap


def gh(*args: str) -> str:
    """Run a gh CLI command and return stdout."""
    result = subprocess.run(
        ["gh", *args],
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: retarget-stacked-prs.py <branch1> [branch2 ...] [target_ref]")
        print("  target_ref defaults to 'main'")
        sys.exit(1)

    args = sys.argv[1:]
    target_ref = "main"

    # If the last argument looks like a ref (not a branch name from the list),
    # treat it as the target. In practice, the workflow always passes
    # "branch1 branch2 main" — branches come first, target last.
    # Heuristic: if there are multiple args and the last one is "main", use it.
    if len(args) >= 2 and args[-1] in ("main", "origin/main"):
        target_ref = args[-1]
        branches_to_check = args[:-1]
    else:
        branches_to_check = args

    if not branches_to_check:
        print("No branches to check.")
        return

    # Query GitHub for all open PRs with their base refs
    open_prs_raw = gh(
        "pr", "list",
        "--state", "open",
        "--json", "number,title,baseRefName,headRefName",
        "--limit", "100",
    )
    if not open_prs_raw:
        print("No open PRs found.")
        return

    open_prs = json.loads(open_prs_raw)
    branch_set = set(branches_to_check)

    protected_branches = set()
    retargeted_prs = []

    for pr in open_prs:
        base = pr["baseRefName"]
        if base not in branch_set:
            continue

        pr_num = pr["number"]
        pr_title = pr["title"]
        head_ref = pr["headRefName"]

        print(f"  PR #{pr_num} ({pr_title}) has base={base} -- attempting retarget to {target_ref}")

        # Attempt retarget via gh pr edit
        edit_result = subprocess.run(
            ["gh", "pr", "edit", str(pr_num), "--base", target_ref],
            capture_output=True,
            text=True,
        )

        if edit_result.returncode == 0:
            # Post explanatory comment
            comment = (
                f"This PR's base branch (`{base}`) was merged and would have been "
                f"deleted, which would have auto-closed this PR.\n\n"
                f"The merge queue automatically retargeted this PR to `{target_ref}` "
                f"to prevent silent closure (FAR-917).\n\n"
                f"If this PR depends on changes that were only in `{base}` and are "
                f"now on `{target_ref}`, you may need to rebase or update it."
            )
            subprocess.run(
                ["gh", "pr", "comment", str(pr_num), "--body", comment],
                capture_output=True,
                text=True,
            )
            print(f"    Retargeted PR #{pr_num} to {target_ref}")
            retargeted_prs.append(pr_num)
        else:
            # Retarget failed -- protect the branch, post explanation on the child
            print(f"    Retarget FAILED for PR #{pr_num}: {edit_result.stderr.strip()}")
            protected_branches.add(base)

            comment = (
                f"**⚠️ Stacked PR protection (FAR-917)**\n\n"
                f"This PR's base branch (`{base}`) was merged and is scheduled for "
                f"deletion. The merge queue attempted to retarget this PR to "
                f"`{target_ref}` but was unable to do so.\n\n"
                f"The base branch has been **kept in place** to prevent this PR from "
                f"being silently closed.\n\n"
                f"**What to do:** Rebase this PR onto `{target_ref}` and update its "
                f"base via `gh pr edit {pr_num} --base {target_ref}`. Once done, the "
                f"base branch can be safely deleted."
            )
            subprocess.run(
                ["gh", "pr", "comment", str(pr_num), "--body", comment],
                capture_output=True,
                text=True,
            )

    # Output results for the workflow
    protected_str = " ".join(sorted(protected_branches))
    retargeted_str = " ".join(str(n) for n in retargeted_prs)

    # Write to GITHUB_OUTPUT if available
    github_output = sys.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a") as f:
            f.write(f"protected_branches={protected_str}\n")
            f.write(f"retargeted_prs={retargeted_str}\n")
            f.write(f"unprotected_count={len(retargeted_prs)}\n")

    # Human-readable summary
    if protected_branches:
        print(f"\nProtected branches (NOT deleted): {protected_str}")
    if retargeted_prs:
        print(f"Retargeted PRs: {retargeted_str}")
    if not protected_branches and not retargeted_prs:
        print("\nNo stacked PRs found -- all branches safe to delete.")


if __name__ == "__main__":
    main()
