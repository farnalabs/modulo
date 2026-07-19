"""PR Reviewer Pipeline

Triggered on pull_request events. Clones the PR's head branch, reads the
diff against main, invokes opencode's multi-lens QA skill on the changed
files, and posts review comments back to the PR.

Environment Variables:
    GITHUB_TOKEN                 — GitHub PAT for cloning, PR access, and posting reviews
    GITHUB_REPO                  — Repository full name (e.g. "farnalabs/modulo")
    GITHUB_PR_NUMBER             — Pull request number to review
    APP_MODULO_OPENCODE_API_KEY  — API key for the opencode CLI

Output:
    Writes status, summary, wall_clock_ms, and review_comment_url to output.json
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


def fetch_pr_head(repo_path, pr_number, token, repo_full_name):
    ref_spec = f"+refs/pull/{pr_number}/head:refs/remotes/origin/pr/{pr_number}"
    owner, repo = repo_full_name.split("/")
    run_git(["remote", "set-url", "origin", get_git_url(token, owner, repo)], cwd=repo_path)
    r = run_git(["fetch", "origin", ref_spec], cwd=repo_path, timeout=60)
    if r.returncode != 0:
        exit_failed(f"Fetch PR head failed: {r.stderr[:200]}")
    r = run_git(["checkout", f"pr/{pr_number}"], cwd=repo_path)
    if r.returncode != 0:
        exit_failed(f"Checkout PR ref failed: {r.stderr[:200]}")


def get_changed_files(repo_path):
    r = run_git(["diff", "--name-only", "origin/main...HEAD"], cwd=repo_path)
    if r.returncode != 0:
        exit_failed(f"Diff failed: {r.stderr[:200]}")
    return [f.strip() for f in r.stdout.splitlines() if f.strip()]


def run_qa(repo_path, changed_files):
    if not changed_files:
        return {"returncode": 0, "stdout": "No changed files to review.", "stderr": ""}
    paths = " ".join(changed_files)
    result = subprocess.run(
        ["opencode", "qa", paths],
        cwd=repo_path,
        capture_output=True,
        text=True,
        timeout=600,
    )
    return {"returncode": result.returncode, "stdout": result.stdout, "stderr": result.stderr}


def post_review_comment(repo_full_name, pr_number, qa_output, token):
    body = (
        f"## Automated Code Review\n\nFindings from multi-lens QA on changed files:\n\n```\n{qa_output[:3000]}\n```\n"
    )
    r = subprocess.run(
        ["gh", "pr", "comment", pr_number, "--repo", repo_full_name, "--body", body],
        env={**os.environ, "GH_TOKEN": token},
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        exit_failed(f"Post review comment failed: {r.stderr[:200]}")
    return r.stdout.strip()


def main():
    token, api_key = check_env()
    model = setup_opencode_auth(api_key)
    github_repo, pr_number = check_env_extra()

    with tempfile.TemporaryDirectory(prefix="pr-reviewer-") as tmpdir:
        owner, repo = github_repo.split("/")
        repo_path = safe_clone(token, get_git_url(token, owner, repo), Path(tmpdir) / "repo")

        fetch_pr_head(repo_path, pr_number, token, github_repo)
        changed_files = get_changed_files(repo_path)

        if not changed_files:
            exit_completed(summary="No changed files detected for review")

        start = time.time()
        result = run_qa(repo_path, changed_files)
        wall_clock_ms = int((time.time() - start) * 1000)

        comment_url = post_review_comment(github_repo, pr_number, result["stdout"], token)
        exit_completed(
            summary=f"Reviewed {len(changed_files)} files in PR #{pr_number}",
            extra={
                "changed_files_count": len(changed_files),
                "review_comment_url": comment_url,
                "files": changed_files,
                "wall_clock_ms": wall_clock_ms,
                "usage": {"model": model, "wall_clock_ms": wall_clock_ms},
            },
        )


if __name__ == "__main__":
    main()
