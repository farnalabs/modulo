"""PR Fix Agent Pipeline

Triggered when a review from the PR Reviewer identifies fixable issues.
Clones the PR branch, applies automated fixes via `opencode qa-iterate`,
commits the fixes back to the PR branch, and notifies the PR.

Environment Variables:
    GITHUB_TOKEN                 — GitHub PAT for cloning and pushing fixes
    GITHUB_REPO                  — Repository full name (e.g. "farnalabs/modulo")
    GITHUB_PR_NUMBER             — Pull request number to fix
    APP_MODULO_OPENCODE_API_KEY  — API key for the opencode CLI

Output:
    Writes status, summary, wall_clock_ms, commit_sha, and comment_url to output.json
"""

import os
import subprocess
import tempfile
import time
from pathlib import Path

from _common import check_env, exit_completed, exit_failed, get_git_url, run_git, safe_clone, setup_opencode_auth


def check_env_extra():
    missing = [k for k in ["GITHUB_REPO", "GITHUB_PR_NUMBER"] if k not in os.environ]
    if missing:
        exit_failed(f"Missing required env vars: {', '.join(missing)}")
    return os.environ["GITHUB_REPO"], os.environ["GITHUB_PR_NUMBER"]


def clone_and_checkout_pr(token, repo_path, pr_number, repo_full_name):
    ref_spec = f"+refs/pull/{pr_number}/head:refs/remotes/origin/pr/{pr_number}"
    run_git(["fetch", "origin", ref_spec], cwd=repo_path, timeout=60)
    r = run_git(["checkout", f"pr/{pr_number}"], cwd=repo_path)
    if r.returncode != 0:
        exit_failed(f"Checkout PR ref failed: {r.stderr[:200]}")
    return repo_path


def run_fix_iteration(repo_path):
    result = subprocess.run(
        ["opencode", "qa-iterate"],
        cwd=repo_path,
        capture_output=True,
        text=True,
        timeout=900,
    )
    return {"returncode": result.returncode, "stdout": result.stdout, "stderr": result.stderr}


def get_branch_name(repo_path):
    r = run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=repo_path)
    if r.returncode != 0:
        exit_failed("Failed to get branch name")
    return r.stdout.strip()


def commit_and_push_fixes(repo_path, branch_name, repo_full_name, token):
    run_git(["add", "-A"], cwd=repo_path)
    r = run_git(["diff", "--cached", "--quiet"], cwd=repo_path)
    if r.returncode == 0:
        return ""

    run_git(["commit", "-m", "fix: automated PR fix agent patches"], cwd=repo_path)
    r = run_git(["rev-parse", "HEAD"], cwd=repo_path)
    commit_sha = r.stdout.strip()

    owner, repo = repo_full_name.split("/")
    run_git(["remote", "set-url", "origin", get_git_url(token, owner, repo)], cwd=repo_path)
    r = run_git(["push", "origin", branch_name], cwd=repo_path, timeout=120)
    if r.returncode != 0:
        exit_failed(f"Push failed: {r.stderr[:200]}")

    return commit_sha


def post_fix_comment(repo_full_name, pr_number, commit_sha, token):
    body = (
        "## Automated Fixes Applied\n\n"
        f"Fix commit: `{commit_sha}`\n\n"
        "These fixes were applied automatically by the PR Fix Agent.\n"
        "Please review before merging."
    )
    r = subprocess.run(
        ["gh", "pr", "comment", pr_number, "--repo", repo_full_name, "--body", body],
        env={**os.environ, "GH_TOKEN": token},
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        exit_failed(f"Post fix comment failed: {r.stderr[:200]}")
    return r.stdout.strip()


def main():
    token, api_key = check_env()
    model = setup_opencode_auth(api_key)
    github_repo, pr_number = check_env_extra()

    with tempfile.TemporaryDirectory(prefix="pr-fix-agent-") as tmpdir:
        owner, repo = github_repo.split("/")
        repo_path = safe_clone(token, get_git_url(token, owner, repo), Path(tmpdir) / "repo")
        repo_path = clone_and_checkout_pr(token, repo_path, pr_number, github_repo)

        branch_name = get_branch_name(repo_path)

        start = time.time()
        fix_result = run_fix_iteration(repo_path)
        wall_clock_ms = int((time.time() - start) * 1000)

        if fix_result["returncode"] != 0:
            post_fix_comment(
                github_repo,
                pr_number,
                f"Fix agent encountered errors:\n```\n{fix_result['stderr'][:1000]}\n```",
                token,
            )
            exit_failed(
                "qa-iterate returned non-zero exit code",
                extra={"stderr": fix_result["stderr"], "wall_clock_ms": wall_clock_ms},
            )

        commit_sha = commit_and_push_fixes(repo_path, branch_name, github_repo, token)

        if not commit_sha:
            exit_completed(
                summary="No fixes needed — PR is already clean",
                extra={"pr_number": pr_number, "wall_clock_ms": wall_clock_ms},
            )

        comment_url = post_fix_comment(github_repo, pr_number, commit_sha, token)

        exit_completed(
            summary=f"Fixed and pushed to PR #{pr_number}",
            extra={
                "commit_sha": commit_sha,
                "branch": branch_name,
                "comment_url": comment_url,
                "wall_clock_ms": wall_clock_ms,
                "usage": {"model": model, "wall_clock_ms": wall_clock_ms},
            },
        )


if __name__ == "__main__":
    main()
