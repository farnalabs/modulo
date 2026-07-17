"""Codebase Improver Pipeline

Runs the `improve-codebase` skill against a target path in the repository.
Picks the next unassigned section from the linear queue, validates the
environment, calls opencode to apply improvements, commits the result,
pushes to a branch, and creates a pull request.

Environment Variables:
    GITHUB_TOKEN            — GitHub PAT for cloning and PR creation
    GITHUB_REPO             — Repository full name (e.g. "farnalabs/modulo")
    APP_MODULO_OPENCODE_API_KEY — API key for the opencode CLI
    TARGET_PATH             — Subdirectory to improve within the repo (optional)

Output:
    Writes status, summary, wall_clock_ms, and pull_request_url to output.json
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
    return os.environ["GITHUB_REPO"], os.environ.get("TARGET_PATH", ".")


def run_improve_codebase(repo_path, target_path):
    return subprocess.run(
        ["opencode", "improve-codebase", target_path],
        cwd=repo_path,
        capture_output=True,
        text=True,
        timeout=600,
    )


def commit_and_push(repo_path, branch_name, repo_full_name, token):
    run_git(["checkout", "-b", branch_name], cwd=repo_path)
    if run_git(["checkout", "-b", branch_name], cwd=repo_path).returncode != 0:
        run_git(["checkout", branch_name], cwd=repo_path)
    run_git(["add", "-A"], cwd=repo_path)
    r = run_git(["diff", "--cached", "--quiet"], cwd=repo_path)
    if r.returncode != 0:
        run_git(["commit", "-m", "feat: codebase improvement sweep"], cwd=repo_path)
    owner, repo = repo_full_name.split("/")
    run_git(["remote", "set-url", "origin", get_git_url(token, owner, repo)], cwd=repo_path)
    r = run_git(["push", "origin", branch_name], cwd=repo_path)
    if r.returncode != 0:
        exit_failed(f"Push failed: {r.stderr[:200]}")


def create_pr(repo_full_name, branch_name, token):
    r = subprocess.run(
        [
            "gh",
            "pr",
            "create",
            "--repo",
            repo_full_name,
            "--base",
            "main",
            "--head",
            branch_name,
            "--title",
            "Codebase Improvement Sweep",
            "--body",
            f"Automated codebase improvement via the improve-codebase skill.\nBranch: {branch_name}\n",
        ],
        env={**os.environ, "GH_TOKEN": token},
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        exit_failed(f"PR creation failed: {r.stderr[:200]}")
    return r.stdout.strip()


def main():
    token, api_key = check_env()
    model = setup_opencode_auth(api_key)
    github_repo, target_path = check_env_extra()

    with tempfile.TemporaryDirectory(prefix="codebase-improver-") as tmpdir:
        owner, repo = github_repo.split("/")
        repo_path = safe_clone(token, get_git_url(token, owner, repo), Path(tmpdir) / "repo")

        start = time.time()
        result = run_improve_codebase(repo_path, target_path)
        wall_clock_ms = int((time.time() - start) * 1000)

        if result["returncode"] != 0:
            exit_failed(
                "opencode improve-codebase returned non-zero exit code",
                extra={"stderr": result["stderr"], "wall_clock_ms": wall_clock_ms},
            )

        branch_name = f"improve-codebase/{target_path.replace('/', '-')}"
        commit_and_push(repo_path, branch_name, github_repo, token)
        pr_url = create_pr(github_repo, branch_name, token)

        exit_completed(
            summary=f"Codebase improvement completed for {target_path}",
            extra={
                "pull_request_url": pr_url,
                "branch": branch_name,
                "wall_clock_ms": wall_clock_ms,
                "usage": {"model": model, "wall_clock_ms": wall_clock_ms},
            },
        )


if __name__ == "__main__":
    main()
