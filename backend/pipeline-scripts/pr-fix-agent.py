"""PR Fix Agent Pipeline — thin wrapper around branch-fixer

Resolves PR number to branch name, then delegates to branch-fixer.py
by re-executing with BRANCH_NAME set.
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

from _common import check_env, exit_failed, get_git_url, safe_clone


def get_pr_head_branch(repo_path, pr_number, token):
    r = subprocess.run(
        ["gh", "pr", "view", pr_number, "--json", "headRefName", "--jq", ".headRefName"],
        cwd=repo_path,
        env={**os.environ, "GH_TOKEN": token},
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        exit_failed(f"Failed to get PR #{pr_number} head branch: {r.stderr[:200]}")
    return r.stdout.strip()


def main():
    token, _api_key = check_env()
    pr_number = os.environ.get("GITHUB_PR_NUMBER", "").strip()
    github_repo = os.environ.get("GITHUB_REPO", "")
    if not pr_number or not github_repo:
        exit_failed("GITHUB_PR_NUMBER and GITHUB_REPO are required")

    with tempfile.TemporaryDirectory(prefix="pr-fix-agent-") as tmpdir:
        owner, repo = github_repo.split("/")
        repo_path = safe_clone(token, get_git_url(token, owner, repo), Path(tmpdir) / "repo")
        head_branch = get_pr_head_branch(repo_path, pr_number, token)

    # Re-execute branch-fixer.py with BRANCH_NAME set
    script_dir = os.path.dirname(os.path.abspath(__file__))
    branch_fixer_path = os.path.join(script_dir, "branch-fixer.py")
    env = os.environ.copy()
    env["BRANCH_NAME"] = head_branch
    env["GITHUB_PR_NUMBER"] = pr_number

    r = subprocess.run(
        [sys.executable, branch_fixer_path],
        capture_output=True,
        text=True,
        timeout=900,
        env=env,
    )
    print(r.stdout)
    if r.returncode != 0:
        exit_failed(f"branch-fixer failed for PR #{pr_number}: {r.stderr[:500]}")

    # branch-fixer writes its own output.json
    sys.exit(0)


if __name__ == "__main__":
    main()
