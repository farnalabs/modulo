"""Branch Fixer Pipeline

Triggered when CI or tests fail on a branch. Clones the branch, runs
automated fixes via `opencode qa-iterate`, commits fixes back to the
branch, and notifies if it's a PR branch.

Supports both PR branches (via BRANCH_NAME derived from PR number)
and shared branches like "merge-queue" (via explicit BRANCH_NAME).

Environment Variables:
    GITHUB_TOKEN                 — GitHub PAT for cloning and pushing
    GITHUB_REPO                  — Repository full name (e.g. "farnalabs/modulo")
    BRANCH_NAME                  — Branch to fix (e.g. "merge-queue" or "feature/foo")
    GITHUB_PR_NUMBER             — PR number (optional — if set, posts a PR comment)
    APP_MODULO_OPENCODE_API_KEY  — API key for the opencode CLI

Output:
    Writes status, summary, wall_clock_ms, commit_sha to output.json
"""

import os
import subprocess
import tempfile
import time
from pathlib import Path

from _common import check_env, exit_completed, exit_failed, get_git_url, run_git, safe_clone, setup_opencode_auth


def check_env_extra():
    missing = [k for k in ["GITHUB_REPO"] if k not in os.environ]
    if missing:
        exit_failed(f"Missing required env vars: {', '.join(missing)}")

    branch_name = os.environ.get("BRANCH_NAME", "").strip()
    if not branch_name:
        exit_failed("BRANCH_NAME is required")

    return os.environ["GITHUB_REPO"], branch_name


def resolve_branch_ref(repo_path, branch_name, token):
    """Try to fetch the branch. Returns the local ref name to checkout."""
    r = subprocess.run(
        ["git", "fetch", "origin", branch_name],
        cwd=repo_path,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if r.returncode == 0:
        return branch_name

    # Maybe it's a PR ref
    if branch_name.isdigit():
        r = subprocess.run(
            ["git", "fetch", "origin", f"+refs/pull/{branch_name}/head:refs/remotes/origin/pr/{branch_name}"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if r.returncode == 0:
            return f"pr/{branch_name}"

    exit_failed(f"Could not fetch branch '{branch_name}': {r.stderr[:200]}")


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


def commit_and_push_fixes(repo_path, local_ref, repo_full_name, token):
    run_git(["add", "-A"], cwd=repo_path)
    r = run_git(["diff", "--cached", "--quiet"], cwd=repo_path)
    if r.returncode == 0:
        return ""

    run_git(["commit", "-m", "fix: automated branch fix agent patches"], cwd=repo_path)
    r = run_git(["rev-parse", "HEAD"], cwd=repo_path)
    commit_sha = r.stdout.strip()

    owner, repo = repo_full_name.split("/")
    run_git(["remote", "set-url", "origin", get_git_url(token, owner, repo)], cwd=repo_path)
    run_git(["push", "origin", f"{local_ref}:{local_ref}"], cwd=repo_path, timeout=120)

    return commit_sha


def post_fix_comment(repo_full_name, pr_number, commit_sha, token):
    if not pr_number:
        return ""
    body = (
        "## Automated Fixes Applied\n\n"
        f"Fix commit: `{commit_sha}`\n\n"
        "These fixes were applied automatically by the Branch Fixer.\n"
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
    github_repo, branch_name = check_env_extra()
    pr_number = os.environ.get("GITHUB_PR_NUMBER", "").strip()

    with tempfile.TemporaryDirectory(prefix="branch-fixer-") as tmpdir:
        owner, repo = github_repo.split("/")
        repo_path = safe_clone(token, get_git_url(token, owner, repo), Path(tmpdir) / "repo")

        local_ref = resolve_branch_ref(repo_path, branch_name, token)

        r = run_git(["checkout", local_ref], cwd=repo_path)
        if r.returncode != 0:
            exit_failed(f"Checkout {local_ref} failed: {r.stderr[:200]}")

        actual_branch = get_branch_name(repo_path)

        start = time.time()
        fix_result = run_fix_iteration(repo_path)
        wall_clock_ms = int((time.time() - start) * 1000)

        if fix_result["returncode"] != 0:
            exit_failed(
                "qa-iterate returned non-zero exit code",
                extra={
                    "branch": actual_branch,
                    "stderr": fix_result["stderr"][:1000],
                    "wall_clock_ms": wall_clock_ms,
                },
            )

        commit_sha = commit_and_push_fixes(repo_path, branch_name, github_repo, token)

        if not commit_sha:
            exit_completed(
                summary=f"No fixes needed on branch '{branch_name}'",
                extra={"branch": actual_branch, "wall_clock_ms": wall_clock_ms},
            )

        comment_url = post_fix_comment(github_repo, pr_number, commit_sha, token) if pr_number else ""

        extra = {
            "commit_sha": commit_sha,
            "branch": actual_branch,
            "wall_clock_ms": wall_clock_ms,
            "usage": {"model": model, "wall_clock_ms": wall_clock_ms},
        }
        if comment_url:
            extra["comment_url"] = comment_url

        exit_completed(
            summary=f"Fixed and pushed to branch '{branch_name}'",
            extra=extra,
        )


if __name__ == "__main__":
    main()
