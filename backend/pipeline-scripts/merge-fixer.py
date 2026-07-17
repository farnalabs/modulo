"""Merge Fixer Pipeline

Triggered when a PR merge attempt fails due to conflicts. Clones the PR
branch, rebases onto main, resolves conflicts automatically using the
`merge` skill (which reconciles both sides' changes intelligently),
commits the resolved merge, and pushes back to the PR branch.

Environment Variables:
    GITHUB_TOKEN                 — GitHub PAT for cloning and pushing
    GITHUB_REPO                  — Repository full name (e.g. "farnalabs/modulo")
    GITHUB_PR_NUMBER             — Pull request number with merge conflicts
    APP_MODULO_OPENCODE_API_KEY  — API key for the opencode CLI

Output:
    Writes status, summary, wall_clock_ms, conflict_files, and commit_sha to output.json
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


def get_head_branch(repo_path, pr_number, token):
    r = subprocess.run(
        ["gh", "pr", "view", pr_number, "--json", "headRefName", "--jq", ".headRefName"],
        cwd=repo_path,
        env={**os.environ, "GH_TOKEN": token},
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        exit_failed(f"Failed to get PR head branch: {r.stderr[:200]}")
    return r.stdout.strip()


def get_branch_name(repo_path):
    r = run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=repo_path)
    if r.returncode != 0:
        exit_failed("Failed to get branch name")
    return r.stdout.strip()


def attempt_rebase(repo_path):
    result = subprocess.run(
        ["git", "rebase", "main"],
        cwd=repo_path,
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return [], True

    conflict_lines = [line for line in result.stdout.splitlines() if "both modified:" in line or "CONFLICT" in line]
    return conflict_lines, False


def run_merge_skill(repo_path, conflict_files):
    if not conflict_files:
        return {"returncode": 0, "stdout": "No conflicts to resolve.", "stderr": ""}
    paths = " ".join(conflict_files)
    result = subprocess.run(
        ["opencode", "merge", paths],
        cwd=repo_path,
        capture_output=True,
        text=True,
        timeout=600,
    )
    return {"returncode": result.returncode, "stdout": result.stdout, "stderr": result.stderr}


def commit_and_push(repo_path, branch_name, repo_full_name, token, rebase_was_started):
    run_git(["add", "-A"], cwd=repo_path)

    if rebase_was_started:
        r = run_git(["rebase", "--continue"], cwd=repo_path)
        if r.returncode != 0:
            exit_failed(f"Rebase continue failed: {r.stderr[:200]}")
    else:
        r = run_git(["diff", "--cached", "--quiet"], cwd=repo_path)
        if r.returncode != 0:
            run_git(["commit", "-m", "fix: merge conflict resolution"], cwd=repo_path)

    owner, repo = repo_full_name.split("/")
    run_git(["remote", "set-url", "origin", get_git_url(token, owner, repo)], cwd=repo_path)
    r = run_git(["push", "origin", branch_name, "--force-with-lease"], cwd=repo_path, timeout=120)
    if r.returncode != 0:
        exit_failed(f"Push failed: {r.stderr[:200]}")

    r = run_git(["rev-parse", "HEAD"], cwd=repo_path)
    return r.stdout.strip()


def main():
    token, api_key = check_env()
    model = setup_opencode_auth(api_key)
    github_repo, pr_number = check_env_extra()

    with tempfile.TemporaryDirectory(prefix="merge-fixer-") as tmpdir:
        owner, repo = github_repo.split("/")
        repo_path = safe_clone(token, get_git_url(token, owner, repo), Path(tmpdir) / "repo")

        run_git(["fetch", "origin"], cwd=repo_path, timeout=60)
        head_branch = get_head_branch(repo_path, pr_number, token)
        r = run_git(["checkout", head_branch], cwd=repo_path)
        if r.returncode != 0:
            exit_failed(f"Checkout {head_branch} failed: {r.stderr[:200]}")

        branch_name = get_branch_name(repo_path)

        start = time.time()
        conflict_signals, rebase_ok = attempt_rebase(repo_path)
        wall_clock_ms = int((time.time() - start) * 1000)

        if rebase_ok:
            commit_sha = commit_and_push(repo_path, branch_name, github_repo, token, rebase_was_started=False)
            exit_completed(
                summary="Rebased cleanly with no conflicts",
                extra={
                    "commit_sha": commit_sha,
                    "branch": branch_name,
                    "conflict_files": [],
                    "wall_clock_ms": wall_clock_ms,
                },
            )
            return

        conflict_files = [line.split(":")[-1].strip() for line in conflict_signals if "both modified:" in line]
        print(f"Resolving conflicts in: {conflict_files}")

        merge_result = run_merge_skill(repo_path, conflict_files)

        if merge_result["returncode"] != 0:
            exit_failed(
                "Merge skill could not resolve all conflicts",
                extra={
                    "conflict_files": conflict_files,
                    "stderr": merge_result["stderr"],
                    "wall_clock_ms": wall_clock_ms,
                },
            )

        commit_sha = commit_and_push(repo_path, branch_name, github_repo, token, rebase_was_started=True)

        exit_completed(
            summary=f"Merge conflicts resolved in PR #{pr_number}",
            extra={
                "commit_sha": commit_sha,
                "branch": branch_name,
                "conflict_files": conflict_files,
                "wall_clock_ms": wall_clock_ms,
                "usage": {"model": model, "wall_clock_ms": wall_clock_ms},
            },
        )


if __name__ == "__main__":
    main()
