"""Merge Fixer Pipeline

Triggered when PR merge attempt(s) fail due to conflicts. Clones each PR
branch, rebases onto the target branch, resolves conflicts automatically
using the `merge` skill, commits the resolved merge, and pushes back.

Supports both single-PR mode (GITHUB_PR_NUMBER) and batch mode (CONFLICT_PRS).

Environment Variables:
    GITHUB_TOKEN                 — GitHub PAT for cloning and pushing
    GITHUB_REPO                  — Repository full name (e.g. "farnalabs/modulo")
    GITHUB_PR_NUMBER             — Single PR number (legacy, fallback if CONFLICT_PRS unset)
    CONFLICT_PRS                 — Space-separated list of PR numbers (takes priority)
    TARGET_BRANCH                — Branch to merge into (default: "main")
    APP_MODULO_OPENCODE_API_KEY  — API key for the opencode CLI

Output:
    Writes status, summary, wall_clock_ms, and per-PR results to output.json
"""

import os
import subprocess
import tempfile
import time
from pathlib import Path

from _common import check_env, exit_completed, exit_failed, get_git_url, run_git, safe_clone, setup_opencode_auth


def get_prs():
    """Return list of PR numbers to process."""
    conflict_prs = os.environ.get("CONFLICT_PRS", "").strip()
    if conflict_prs:
        return conflict_prs.split()
    pr_number = os.environ.get("GITHUB_PR_NUMBER", "").strip()
    if pr_number:
        return [pr_number]
    exit_failed("Neither CONFLICT_PRS nor GITHUB_PR_NUMBER set")


def check_env_extra():
    missing = [k for k in ["GITHUB_REPO"] if k not in os.environ]
    if missing:
        exit_failed(f"Missing required env vars: {', '.join(missing)}")
    return os.environ["GITHUB_REPO"], os.environ.get("TARGET_BRANCH", "main")


def get_head_branch(repo_path, pr_number, token):
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


def get_branch_name(repo_path):
    r = run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=repo_path)
    if r.returncode != 0:
        exit_failed("Failed to get branch name")
    return r.stdout.strip()


def attempt_rebase(repo_path, target_branch="main"):
    result = subprocess.run(
        ["git", "rebase", target_branch],
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


def process_pr(pr_number, repo_path, repo_full_name, token, target_branch):
    """Fix a single PR: rebase onto target, resolve conflicts, push back."""
    run_git(["fetch", "origin"], cwd=repo_path, timeout=60)
    head_branch = get_head_branch(repo_path, pr_number, token)
    r = run_git(["checkout", head_branch], cwd=repo_path)
    if r.returncode != 0:
        return {"pr": pr_number, "status": "failed", "error": f"Checkout {head_branch} failed"}

    branch_name = get_branch_name(repo_path)
    start = time.time()
    conflict_signals, rebase_ok = attempt_rebase(repo_path, target_branch)
    wall_clock_ms = int((time.time() - start) * 1000)

    if rebase_ok:
        commit_sha = commit_and_push(repo_path, branch_name, repo_full_name, token, rebase_was_started=False)
        return {
            "pr": pr_number,
            "status": "completed",
            "summary": f"Rebased cleanly onto {target_branch}",
            "commit_sha": commit_sha,
            "branch": branch_name,
            "conflict_files": [],
            "wall_clock_ms": wall_clock_ms,
        }

    conflict_files = [line.split(":")[-1].strip() for line in conflict_signals if "both modified:" in line]
    print(f"PR #{pr_number}: resolving conflicts in: {conflict_files}")

    merge_result = run_merge_skill(repo_path, conflict_files)
    if merge_result["returncode"] != 0:
        return {
            "pr": pr_number,
            "status": "failed",
            "summary": "Merge skill could not resolve all conflicts",
            "conflict_files": conflict_files,
            "stderr": merge_result["stderr"],
            "wall_clock_ms": wall_clock_ms,
        }

    commit_sha = commit_and_push(repo_path, branch_name, repo_full_name, token, rebase_was_started=True)

    return {
        "pr": pr_number,
        "status": "completed",
        "summary": f"Merge conflicts resolved in PR #{pr_number}",
        "commit_sha": commit_sha,
        "branch": branch_name,
        "conflict_files": conflict_files,
        "wall_clock_ms": wall_clock_ms,
    }


def main():
    token, api_key = check_env()
    model = setup_opencode_auth(api_key)
    github_repo, target_branch = check_env_extra()
    pr_numbers = get_prs()

    with tempfile.TemporaryDirectory(prefix="merge-fixer-") as tmpdir:
        owner, repo = github_repo.split("/")
        repo_path = safe_clone(token, get_git_url(token, owner, repo), Path(tmpdir) / "repo")

        results = [process_pr(pr, repo_path, github_repo, token, target_branch) for pr in pr_numbers]

        completed = [r for r in results if r["status"] == "completed"]
        failed = [r for r in results if r["status"] == "failed"]

        total_ms = int(sum(r.get("wall_clock_ms", 0) for r in results))
        commit_shas = [r.get("commit_sha", "") for r in completed if r.get("commit_sha")]

        if failed:
            exit_failed(
                f"{len(failed)} PR(s) failed: {', '.join(r['pr'] for r in failed)}",
                extra={
                    "results": results,
                    "target_branch": target_branch,
                    "wall_clock_ms": total_ms,
                    "usage": {"model": model, "wall_clock_ms": total_ms},
                },
            )

        exit_completed(
            summary=f"All {len(pr_numbers)} PR(s) fixed and pushed",
            extra={
                "results": results,
                "target_branch": target_branch,
                "commit_shas": commit_shas,
                "wall_clock_ms": total_ms,
                "usage": {"model": model, "wall_clock_ms": total_ms},
            },
        )


if __name__ == "__main__":
    main()
