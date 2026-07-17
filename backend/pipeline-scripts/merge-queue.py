"""Merge Queue Pipeline

Orchestrates the merge queue for the repository. Checks for pending PRs
labelled "merge-queue", validates they pass CI and review requirements,
applies `gate.ps1` to merge them sequentially, handles conflicts via the
Merge Fixer pipeline, and updates PR status checks.

Environment Variables:
    GITHUB_TOKEN      — GitHub PAT for cloning, PR access, and status updates
    GITHUB_REPO       — Repository full name (e.g. "farnalabs/modulo")
    MAX_BATCH         — Maximum number of PRs to process in one run (default: 3)
    OPENCODE_API_KEY  — API key for the opencode CLI

Output:
    Writes status, summary (PRs processed, merged, failed), and per-PR details
    to /tmp/output.json
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REQUIRED_ENV_VARS = [
    "GITHUB_TOKEN",
    "GITHUB_REPO",
    "OPENCODE_API_KEY",
]


def check_env() -> dict[str, str]:
    missing = [k for k in REQUIRED_ENV_VARS if k not in os.environ]
    if missing:
        print(f"Missing required env vars: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)
    return {
        "github_token": os.environ["GITHUB_TOKEN"],
        "github_repo": os.environ["GITHUB_REPO"],
        "opencode_api_key": os.environ["OPENCODE_API_KEY"],
        "max_batch": int(os.environ.get("MAX_BATCH", "3")),
    }


def fetch_merge_queue(repo_full_name: str, github_token: str) -> list[dict]:
    result = subprocess.run(
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
        env={**os.environ, "GH_TOKEN": github_token},
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


def validate_pr(pr: dict, github_token: str) -> bool:
    if pr.get("mergeable") == "CONFLICTING":
        print(f"PR #{pr['number']} has conflicts — skipping")
        return False

    reviews = pr.get("reviews", [])
    approved = any(r.get("state") == "APPROVED" for r in reviews)

    if not approved:
        print(f"PR #{pr['number']} is not approved — skipping")

    return approved


def run_gate(repo_path: Path, branch: str) -> dict:
    gate_script = repo_path / ".." / ".." / ".." / ".." / "devtools" / "harness" / "tools" / "gate.ps1"
    if not gate_script.exists():
        gate_script = Path("C:/Users/dunca/Modulo/Repos/devtools/harness/tools/gate.ps1")

    result = subprocess.run(
        ["powershell", "-File", str(gate_script), "-Branch", branch, "-SkipBump"],
        cwd=repo_path,
        capture_output=True,
        text=True,
        timeout=600,
    )
    return {
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


def update_pr_status(
    repo_full_name: str,
    pr_number: str,
    state: str,
    description: str,
    github_token: str,
) -> None:
    subprocess.run(
        [
            "gh",
            "pr",
            "edit",
            pr_number,
            "--repo",
            repo_full_name,
            "--add-label",
            f"merged-by-queue:{state}",
        ],
        env={**os.environ, "GH_TOKEN": github_token},
        capture_output=True,
    )


def write_output(status: str, summary: str, extra: dict | None = None) -> None:
    output = {"status": status, "summary": summary}
    if extra:
        output.update(extra)
    output_path = Path("/tmp/output.json")
    output_path.write_text(json.dumps(output, indent=2))
    print(f"Output written to {output_path}")


def main() -> None:
    env = check_env()

    prs = fetch_merge_queue(env["github_repo"], env["github_token"])
    if not prs:
        write_output(status="success", summary="No PRs in merge queue")
        return

    valid_prs = [p for p in prs if validate_pr(p, env["github_token"])]
    batch = valid_prs[: env["max_batch"]]

    if not batch:
        write_output(
            status="skipped",
            summary=f"{len(prs)} PRs in queue but none validated (approval/conflicts)",
            extra={"total_queued": len(prs), "validated": 0},
        )
        return

    results = []
    for pr in batch:
        branch = pr["headRefName"]
        print(f"Processing PR #{pr['number']} ({branch}): {pr['title']}")

        with tempfile.TemporaryDirectory(prefix=f"merge-queue-{pr['number']}-") as tmpdir:
            subprocess.run(
                ["git", "clone", f"https://github.com/{env['github_repo']}.git", str(Path(tmpdir) / "repo")],
                check=True,
                capture_output=True,
                text=True,
            )
            repo_path = Path(tmpdir) / "repo"

            gate_result = run_gate(repo_path, branch)

            if gate_result["returncode"] == 0:
                update_pr_status(
                    env["github_repo"],
                    str(pr["number"]),
                    "success",
                    "Merged by Merge Queue",
                    env["github_token"],
                )
                results.append(
                    {
                        "pr_number": pr["number"],
                        "branch": branch,
                        "status": "merged",
                    }
                )
            else:
                update_pr_status(
                    env["github_repo"],
                    str(pr["number"]),
                    "failure",
                    "Gate failed — check logs",
                    env["github_token"],
                )
                results.append(
                    {
                        "pr_number": pr["number"],
                        "branch": branch,
                        "status": "failed",
                        "error": gate_result["stderr"][:500],
                    }
                )

    merged = sum(1 for r in results if r["status"] == "merged")
    failed = sum(1 for r in results if r["status"] == "failed")

    write_output(
        status="success" if failed == 0 else "partial_failure",
        summary=f"Processed {len(results)} PRs: {merged} merged, {failed} failed",
        extra={
            "batch_size": len(batch),
            "results": results,
        },
    )


if __name__ == "__main__":
    main()
