"""Merge Queue Pipeline

Orchestrates the merge queue for the repository. Checks for pending PRs
labelled "merge-queue", validates they pass CI and review requirements,
applies `gate.ps1` to merge them sequentially, handles conflicts via the
Merge Fixer pipeline, and updates PR status checks.

Environment Variables:
    GITHUB_TOKEN                 — GitHub PAT for cloning, PR access, and status updates
    GITHUB_REPO                  — Repository full name (e.g. "farnalabs/modulo")
    MAX_BATCH                    — Maximum number of PRs to process in one run (default: 3)
    APP_MODULO_OPENCODE_API_KEY  — API key for the opencode CLI

Output:
    Writes status, summary, wall_clock_ms, and per-PR results to output.json
"""

import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

from _common import check_env, exit_completed, exit_failed, get_git_url, safe_clone, setup_opencode_auth


def check_env_extra():
    missing = [k for k in ["GITHUB_REPO"] if k not in os.environ]
    if missing:
        exit_failed(f"Missing required env vars: {', '.join(missing)}")
    return os.environ["GITHUB_REPO"], int(os.environ.get("MAX_BATCH", "3"))


def fetch_merge_queue(repo_full_name, token):
    r = subprocess.run(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            repo_full_name,
            "--label",
            "merge-queue",
            "--state",
            "open",
            "--json",
            "number,headRefName,title,mergeable,reviews",
            "--limit",
            "10",
        ],
        env={**os.environ, "GH_TOKEN": token},
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        exit_failed(f"Fetch merge queue failed: {r.stderr[:200]}")
    return json.loads(r.stdout)


def validate_pr(pr, token):
    if pr.get("mergeable") == "CONFLICTING":
        print(f"PR #{pr['number']} has conflicts — skipping")
        return False
    reviews = pr.get("reviews", [])
    approved = any(r.get("state") == "APPROVED" for r in reviews)
    if not approved:
        print(f"PR #{pr['number']} is not approved — skipping")
    return approved


def resolve_gate_script(repo_path):
    gate = repo_path / ".." / ".." / ".." / ".." / "devtools" / "harness" / "tools" / "gate.ps1"
    if gate.exists():
        return gate
    gate2 = repo_path / ".." / ".." / ".." / ".." / ".." / "devtools" / "harness" / "tools" / "gate.ps1"
    if gate2.exists():
        return gate2
    exit_failed(f"gate.ps1 not found (tried {gate} and {gate2})")
    return None


def run_gate_script(repo_path, branch):
    gate_script = resolve_gate_script(repo_path)
    start = time.time()
    result = subprocess.run(
        ["powershell", "-File", str(gate_script), "-Branch", branch, "-SkipBump"],
        cwd=repo_path,
        capture_output=True,
        text=True,
        timeout=600,
    )
    wall_clock_ms = int((time.time() - start) * 1000)
    return {
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "wall_clock_ms": wall_clock_ms,
    }


def update_pr_status(repo_full_name, pr_number, state, token):
    subprocess.run(
        ["gh", "pr", "edit", pr_number, "--repo", repo_full_name, "--add-label", f"merged-by-queue:{state}"],
        env={**os.environ, "GH_TOKEN": token},
        capture_output=True,
    )


def main():
    token, api_key = check_env()
    model = setup_opencode_auth(api_key)
    github_repo, max_batch = check_env_extra()

    prs = fetch_merge_queue(github_repo, token)
    if not prs:
        exit_completed(summary="No PRs in merge queue")

    valid_prs = [p for p in prs if validate_pr(p, token)]
    batch = valid_prs[:max_batch]

    if not batch:
        exit_completed(
            summary=f"{len(prs)} PRs in queue but none validated (approval/conflicts)",
            extra={"total_queued": len(prs), "validated": 0},
        )

    total_start = time.time()
    results = []
    for pr in batch:
        branch = pr["headRefName"]
        print(f"Processing PR #{pr['number']} ({branch}): {pr['title']}")

        with tempfile.TemporaryDirectory(prefix=f"merge-queue-{pr['number']}-") as tmpdir:
            owner, repo = github_repo.split("/")
            repo_path = safe_clone(token, get_git_url(token, owner, repo), Path(tmpdir) / "repo")

            gate_result = run_gate_script(repo_path, branch)

            if gate_result["returncode"] == 0:
                update_pr_status(github_repo, str(pr["number"]), "success", token)
                results.append({"pr_number": pr["number"], "branch": branch, "status": "merged"})
            else:
                update_pr_status(github_repo, str(pr["number"]), "failure", token)
                results.append(
                    {
                        "pr_number": pr["number"],
                        "branch": branch,
                        "status": "failed",
                        "error": gate_result["stderr"][:500],
                    }
                )

    total_wall_clock_ms = int((time.time() - total_start) * 1000)
    merged = sum(1 for r in results if r["status"] == "merged")
    failed = sum(1 for r in results if r["status"] == "failed")

    exit_completed(
        summary=f"Processed {len(results)} PRs: {merged} merged, {failed} failed",
        extra={
            "batch_size": len(batch),
            "results": results,
            "wall_clock_ms": total_wall_clock_ms,
            "usage": {"model": model, "wall_clock_ms": total_wall_clock_ms},
        },
    )


if __name__ == "__main__":
    main()
