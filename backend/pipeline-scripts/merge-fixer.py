"""Merge Fixer Pipeline

Triggered when a PR merge attempt fails due to conflicts. Clones the PR
branch, rebases onto main, resolves conflicts automatically using the
`merge` skill (which reconciles both sides' changes intelligently),
commits the resolved merge, and pushes back to the PR branch.

Environment Variables:
    GITHUB_TOKEN      — GitHub PAT for cloning and pushing
    GITHUB_REPO       — Repository full name (e.g. "farnalabs/modulo")
    GITHUB_PR_NUMBER  — Pull request number with merge conflicts
    OPENCODE_API_KEY  — API key for the opencode CLI

Output:
    Writes status, summary, conflict_files, and pr_url to /tmp/output.json
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
    "GITHUB_PR_NUMBER",
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
        "pr_number": os.environ["GITHUB_PR_NUMBER"],
        "opencode_api_key": os.environ["OPENCODE_API_KEY"],
    }


def clone_and_checkout_pr(repo_url: str, work_dir: Path, pr_number: str, github_token: str) -> Path:
    clone_path = work_dir / "repo"
    subprocess.run(
        ["git", "clone", repo_url, str(clone_path)],
        check=True,
        capture_output=True,
        text=True,
    )
    remote_url = f"https://x-access-token:{github_token}@github.com/{os.environ['GITHUB_REPO']}.git"
    subprocess.run(
        ["git", "remote", "set-url", "origin", remote_url],
        cwd=clone_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "fetch", "origin"],
        cwd=clone_path,
        check=True,
        capture_output=True,
    )
    head_branch = subprocess.run(
        ["gh", "pr", "view", pr_number, "--json", "headRefName", "--jq", ".headRefName"],
        cwd=clone_path,
        env={**os.environ, "GH_TOKEN": github_token},
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    subprocess.run(
        ["git", "checkout", head_branch],
        cwd=clone_path,
        check=True,
        capture_output=True,
    )
    return clone_path


def attempt_rebase(repo_path: Path) -> list[str]:
    result = subprocess.run(
        ["git", "rebase", "main"],
        cwd=repo_path,
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return []

    conflict_lines = [line for line in result.stdout.splitlines() if "both modified:" in line or "CONFLICT" in line]
    return conflict_lines


def run_merge_skill(repo_path: Path, conflict_files: list[str]) -> dict:
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
    return {
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


def commit_and_push(repo_path: Path, branch_name: str) -> str:
    subprocess.run(
        ["git", "add", "-A"],
        cwd=repo_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "rebase", "--continue"],
        cwd=repo_path,
        check=True,
        capture_output=True,
    )

    subprocess.run(
        ["git", "push", "origin", branch_name, "--force-with-lease"],
        cwd=repo_path,
        check=True,
        capture_output=True,
    )

    commit_result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_path,
        capture_output=True,
        text=True,
        check=True,
    )
    return commit_result.stdout.strip()


def write_output(status: str, summary: str, extra: dict | None = None) -> None:
    output = {"status": status, "summary": summary}
    if extra:
        output.update(extra)
    output_path = Path("/tmp/output.json")
    output_path.write_text(json.dumps(output, indent=2))
    print(f"Output written to {output_path}")


def main() -> None:
    env = check_env()

    with tempfile.TemporaryDirectory(prefix="merge-fixer-") as tmpdir:
        work_dir = Path(tmpdir)
        repo_url = f"https://github.com/{env['github_repo']}.git"
        repo_path = clone_and_checkout_pr(
            repo_url,
            work_dir,
            env["pr_number"],
            env["github_token"],
        )

        branch_name = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

        conflict_signals = attempt_rebase(repo_path)

        if not conflict_signals:
            commit_sha = commit_and_push(repo_path, branch_name)
            write_output(
                status="success",
                summary="Rebased cleanly with no conflicts",
                extra={
                    "commit_sha": commit_sha,
                    "branch": branch_name,
                    "conflict_files": [],
                },
            )
            return

        conflict_files = [line.split(":")[-1].strip() for line in conflict_signals if "both modified:" in line]
        print(f"Resolving conflicts in: {conflict_files}")

        merge_result = run_merge_skill(repo_path, conflict_files)

        if merge_result["returncode"] != 0:
            write_output(
                status="failed",
                summary="Merge skill could not resolve all conflicts",
                extra={"conflict_files": conflict_files, "stderr": merge_result["stderr"]},
            )
            sys.exit(1)

        commit_sha = commit_and_push(repo_path, branch_name)

        write_output(
            status="success",
            summary=f"Merge conflicts resolved in PR #{env['pr_number']}",
            extra={
                "commit_sha": commit_sha,
                "branch": branch_name,
                "conflict_files": conflict_files,
            },
        )


if __name__ == "__main__":
    main()
