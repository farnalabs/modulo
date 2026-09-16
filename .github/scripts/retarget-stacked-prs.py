#!/usr/bin/env python3
"""Detect open PRs whose base branch is about to be deleted and retarget them.

When a merged PR's head branch is deleted, GitHub auto-closes any open PR
whose base branch matches the deleted branch (FAR-917: PR #597 merged ->
feat/far-856-org-login deleted -> PR #602 silently closed).

This script:
1. Finds all open PRs whose base matches any branch in the given list.
2. Attempts to retarget each to the specified ref (default: main).
3. Posts an explanatory comment on each retargeted PR.
4. Outputs (ALWAYS written, on every path):
   - protected_branches: space-separated branches that MUST NOT be deleted
     (child PR exists but could not be retargeted).
   - retargeted_prs: space-separated PR numbers that were retargeted.

FAIL-SAFE CONTRACT (FAR-917 review): if the open-PR query fails for ANY reason
(auth, rate limit, network, blank/unparseable response) the script cannot know
which branches have child PRs, so it protects EVERY slated branch and prints a
loud annotation. A failed query must NEVER be read as "no open PRs": that
fail-open path deletes a base branch and silently closes its child PR, which is
exactly the incident this guard exists to prevent. The previous `gh()` helper
discarded the return code, so a failure was indistinguishable from an empty
result; `gh_checked` now propagates it.

Unit tests: backend/tests/unit/scripts/test_retarget_stacked_prs.py.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

DEFAULT_TARGET_REF = "main"


class GhError(RuntimeError):
    """A `gh` invocation exited non-zero. Never swallowed."""

    def __init__(self, args, returncode: int, stderr: str):
        self.cmd = tuple(args)
        self.returncode = returncode
        self.stderr = (stderr or "").strip()
        detail = f": {self.stderr}" if self.stderr else ""
        super().__init__(f"`gh {' '.join(args)}` failed (exit {returncode}){detail}")


def _run_gh(*args: str):
    """Run a gh CLI command, returning (returncode, stdout, stderr)."""
    proc = subprocess.run(["gh", *args], capture_output=True, text=True, check=False)
    return proc.returncode, proc.stdout.strip(), (proc.stderr or "").strip()


def gh_checked(run_gh, *args: str) -> str:
    """Run gh and return stdout, raising GhError on any non-zero exit.

    Propagating the return code is the whole point: the previous helper
    discarded it, so a failed query looked identical to an empty result and the
    guard failed OPEN.
    """
    rc, out, err = run_gh(*args)
    if rc != 0:
        raise GhError(args, rc, err)
    return out


def parse_concatenated(text: str) -> list:
    """Parse `gh api --paginate` output (one JSON document per page, concatenated).

    `gh api --paginate` emits each page as a separate top-level JSON array with
    no separator (e.g. ``[page1][page2]``); a naive ``json.load`` would see only
    the first page. Walk the text with ``raw_decode`` and flatten every page.
    """
    decoder = json.JSONDecoder()
    items: list = []
    idx, n = 0, len(text)
    while idx < n:
        while idx < n and text[idx] in " \t\r\n":
            idx += 1
        if idx >= n:
            break
        obj, end = decoder.raw_decode(text, idx)
        if isinstance(obj, list):
            items.extend(obj)
        elif isinstance(obj, dict):
            items.append(obj)
        idx = end
    return items


def fetch_open_prs(run_gh) -> list[dict]:
    """Return EVERY open PR, fully paginated (never truncated at 100).

    Uses ``gh api --paginate`` rather than ``gh pr list --limit 100`` so a repo
    with more than 100 open PRs cannot hide a stacked child PR past the cutoff.
    A blank response is treated as unverifiable (an empty page is ``[]``), so
    the caller protects every branch rather than assuming "no open PRs".
    """
    raw = gh_checked(
        run_gh,
        "api",
        "--paginate",
        "repos/{owner}/{repo}/pulls?state=open&per_page=100",
    )
    if not raw.strip():
        raise ValueError("gh api returned no output for the open-PR query")
    prs: list[dict] = []
    for pr in parse_concatenated(raw):
        base = (pr.get("base") or {}).get("ref")
        head = (pr.get("head") or {}).get("ref")
        number = pr.get("number")
        if base is None or head is None or number is None:
            continue
        prs.append(
            {
                "number": number,
                "baseRefName": base,
                "headRefName": head,
                "title": pr.get("title") or "",
            }
        )
    return prs


def retarget_pr(run_gh, pr_num: int, target_ref: str):
    """Attempt to retarget a PR. Returns (ok, detail); never raises on failure."""
    rc, out, err = run_gh("pr", "edit", str(pr_num), "--base", target_ref)
    return rc == 0, (err or out).strip()


def comment_pr(run_gh, pr_num: int, body: str) -> bool:
    """Post a comment. Best-effort: a comment failure never changes protection."""
    rc, _out, _err = run_gh("pr", "comment", str(pr_num), "--body", body)
    return rc == 0


def is_empty_diff(run_gh, head_ref: str, target_ref: str) -> bool:
    """Best-effort: True when `head_ref` adds no commits over `target_ref`.

    Advisory only -- used to enrich the retarget comment. Any error returns
    False (the normal retarget path), so this can never fail the guard.
    """
    try:
        ahead = gh_checked(
            run_gh,
            "api",
            f"repos/{{owner}}/{{repo}}/compare/{target_ref}...{head_ref}",
            "--jq",
            ".ahead_by",
        )
        return ahead.strip() == "0"
    except (GhError, OSError):
        return False


def _retarget_comment(base: str, target_ref: str, empty_diff: bool) -> str:
    body = (
        f"This PR's base branch (`{base}`) was merged and would have been "
        f"deleted, which would have auto-closed this PR.\n\n"
        f"The merge queue automatically retargeted this PR to `{target_ref}` "
        f"to prevent silent closure (FAR-917).\n\n"
        f"If this PR depends on changes that were only in `{base}` and are "
        f"now on `{target_ref}`, you may need to rebase or update it."
    )
    if empty_diff:
        body += (
            f"\n\n**Note:** this PR adds no commits over `{target_ref}` -- its "
            f"changes appear to already be on `{target_ref}`. If it is superseded, "
            f"close it; otherwise rebase it onto `{target_ref}`."
        )
    return body


def _protection_comment(base: str, target_ref: str, pr_num: int) -> str:
    return (
        f"**Stacked PR protection (FAR-917)**\n\n"
        f"This PR's base branch (`{base}`) was merged and is scheduled for "
        f"deletion. The merge queue attempted to retarget this PR to "
        f"`{target_ref}` but was unable to do so.\n\n"
        f"The base branch has been **kept in place** to prevent this PR from "
        f"being silently closed.\n\n"
        f"**What to do:** Rebase this PR onto `{target_ref}` and update its "
        f"base via `gh pr edit {pr_num} --base {target_ref}`. Once done, the "
        f"base branch can be safely deleted."
    )


def decide(branches_to_check, open_prs, target_ref, run_gh):
    """Retarget every open child PR; protect any base we could not retarget.

    Returns (protected_branches, retargeted_prs).
    """
    branch_set = set(branches_to_check)
    protected: set[str] = set()
    retargeted: list[int] = []

    for pr in open_prs:
        base = pr["baseRefName"]
        if base not in branch_set:
            continue

        pr_num = pr["number"]
        head_ref = pr["headRefName"]
        print(f"  PR #{pr_num} ({pr.get('title', '')}) has base={base} -- attempting retarget to {target_ref}")

        ok, detail = retarget_pr(run_gh, pr_num, target_ref)
        if ok:
            empty = is_empty_diff(run_gh, head_ref, target_ref)
            comment_pr(run_gh, pr_num, _retarget_comment(base, target_ref, empty))
            print(f"    Retargeted PR #{pr_num} to {target_ref}")
            retargeted.append(pr_num)
        else:
            # Retarget failed -- protect the branch so the delete sweep skips it.
            protected.add(base)
            print(f"    Retarget FAILED for PR #{pr_num}: {detail}")
            comment_pr(run_gh, pr_num, _protection_comment(base, target_ref, pr_num))

    return protected, retargeted


def parse_args(argv):
    """Return (branches_to_check, target_ref) from a full argv (argv[0] = prog)."""
    args = [a for a in argv[1:] if a]
    target_ref = DEFAULT_TARGET_REF
    # The workflow appends the target ref LAST (branch1 ... branchN main).
    if len(args) >= 2 and args[-1] in ("main", "origin/main"):
        target_ref = args[-1]
        args = args[:-1]
    return args, target_ref


def write_outputs(github_output, protected, retargeted) -> None:
    """Write the step outputs. Called on EVERY path so downstream never sees ''."""
    if not github_output:
        return
    protected_str = " ".join(sorted(protected))
    retargeted_str = " ".join(str(n) for n in retargeted)
    with Path(github_output).open("a", encoding="utf-8") as fh:
        fh.write(f"protected_branches={protected_str}\n")
        fh.write(f"retargeted_prs={retargeted_str}\n")


def run(argv, run_gh=_run_gh, github_output=None) -> int:
    """Guard logic. Returns an exit code; all failure paths fail SAFE."""
    branches_to_check, target_ref = parse_args(argv)

    if not branches_to_check:
        print("No branches to check.")
        write_outputs(github_output, set(), [])
        return 0

    try:
        open_prs = fetch_open_prs(run_gh)
    except (GhError, ValueError, json.JSONDecodeError, OSError) as exc:
        # FAIL SAFE: we could not verify which branches have child PRs, so we
        # protect EVERY slated branch -- the delete sweep skips them all. We
        # deliberately exit 0: the delete step is gated on `if: success()`, and
        # the post-merge CI/deploy dispatch must still run. Protection, not a
        # failed step, is what prevents the silent closure.
        print(
            "::error title=FAR-917 stacked-PR guard::could not query open PRs "
            f"({exc}) - protecting all {len(branches_to_check)} slated branch(es)"
        )
        write_outputs(github_output, set(branches_to_check), [])
        return 0

    protected, retargeted = decide(branches_to_check, open_prs, target_ref, run_gh)
    write_outputs(github_output, protected, retargeted)

    if protected:
        print(f"\nProtected branches (NOT deleted): {' '.join(sorted(protected))}")
    if retargeted:
        print(f"Retargeted PRs: {' '.join(str(n) for n in retargeted)}")
    if not protected and not retargeted:
        print("\nNo stacked PRs found -- all branches safe to delete.")
    return 0


def main(argv) -> int:
    return run(argv, run_gh=_run_gh, github_output=os.environ.get("GITHUB_OUTPUT"))


if __name__ == "__main__":
    sys.exit(main(sys.argv))
