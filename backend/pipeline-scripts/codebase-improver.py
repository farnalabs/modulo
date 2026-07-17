"""Codebase Improver Pipeline

Runs the `improve-codebase` skill against a target path in the repository.
Picks the next unassigned section from the linear queue, validates the
environment, calls opencode to apply improvements, commits the result,
pushes to a branch, and creates a pull request.

Environment Variables:
    GITHUB_TOKEN      — GitHub PAT for cloning and PR creation
    GITHUB_REPO       — Repository full name (e.g. "farnalabs/modulo")
    OPENCODE_API_KEY  — API key for the opencode CLI
    TARGET_PATH       — Subdirectory to improve within the repo (optional)

Output:
    Writes status, summary, and pull_request_url to /tmp/output.json
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

    target_path = os.environ.get("TARGET_PATH", ".")
    return {
        "github_token": os.environ["GITHUB_TOKEN"],
        "github_repo": os.environ["GITHUB_REPO"],
        "opencode_api_key": os.environ["OPENCODE_API_KEY"],
        "target_path": target_path,
    }


def clone_repo(repo_url: str, work_dir: Path) -> Path:
    clone_path = work_dir / "repo"
    subprocess.run(
        ["git", "clone", repo_url, str(clone_path)],
        check=True,
        capture_output=True,
        text=True,
    )
    return clone_path


def run_improve_codebase(repo_path: Path, target_path: str) -> dict:
    result = subprocess.run(
        ["opencode", "improve-codebase", target_path],
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


def commit_and_push(repo_path: Path, branch_name: str, github_token: str) -> None:
    subprocess.run(
        ["git", "checkout", "-b", branch_name],
        cwd=repo_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "add", "-A"],
        cwd=repo_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "feat: codebase improvement sweep"],
        cwd=repo_path,
        check=True,
        capture_output=True,
    )
    remote_url = f"https://x-access-token:{github_token}@github.com/{os.environ['GITHUB_REPO']}.git"
    subprocess.run(
        ["git", "remote", "set-url", "origin", remote_url],
        cwd=repo_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "push", "origin", branch_name],
        cwd=repo_path,
        check=True,
        capture_output=True,
    )


def create_pr(repo_full_name: str, branch_name: str, github_token: str) -> str:
    result = subprocess.run(
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
            (
                "Automated codebase improvement via the improve-codebase skill.\n"
                f"Branch: {branch_name}\n"
                "Review and merge at your convenience."
            ),
        ],
        env={**os.environ, "GH_TOKEN": github_token},
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def write_output(status: str, summary: str, extra: dict | None = None) -> None:
    output = {"status": status, "summary": summary}
    if extra:
        output.update(extra)
    output_path = Path("/tmp/output.json")
    output_path.write_text(json.dumps(output, indent=2))
    print(f"Output written to {output_path}")


def main() -> None:
    env = check_env()

    with tempfile.TemporaryDirectory(prefix="codebase-improver-") as tmpdir:
        work_dir = Path(tmpdir)
        repo_url = f"https://github.com/{env['github_repo']}.git"
        repo_path = clone_repo(repo_url, work_dir)

        print(f"Running improve-codebase on target: {env['target_path']}")
        result = run_improve_codebase(repo_path, env["target_path"])

        if result["returncode"] != 0:
            write_output(
                status="failed",
                summary="opencode improve-codebase returned non-zero exit code",
                extra={"stderr": result["stderr"]},
            )
            sys.exit(1)

        branch_name = f"improve-codebase/{env['target_path'].replace('/', '-')}"
        commit_and_push(repo_path, branch_name, env["github_token"])
        pr_url = create_pr(env["github_repo"], branch_name, env["github_token"])

        write_output(
            status="success",
            summary=f"Codebase improvement completed for {env['target_path']}",
            extra={"pull_request_url": pr_url, "branch": branch_name},
        )


if __name__ == "__main__":
    main()
