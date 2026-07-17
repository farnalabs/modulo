"""PR Reviewer Pipeline

Triggered on pull_request events. Clones the PR's head branch, reads the
diff against main, invokes opencode's multi-lens QA skill on the changed
files, and posts review comments back to the PR.

Environment Variables:
    GITHUB_TOKEN      — GitHub PAT for cloning, PR access, and posting reviews
    GITHUB_REPO       — Repository full name (e.g. "farnalabs/modulo")
    GITHUB_PR_NUMBER  — Pull request number to review
    OPENCODE_API_KEY  — API key for the opencode CLI

Output:
    Writes status, summary (number of findings, categories), and review_url
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


def clone_repo(repo_url: str, work_dir: Path) -> Path:
    clone_path = work_dir / "repo"
    subprocess.run(
        ["git", "clone", repo_url, str(clone_path)],
        check=True,
        capture_output=True,
        text=True,
    )
    return clone_path


def fetch_pr_head(repo_path: Path, pr_number: str, github_token: str) -> None:
    repo_full = os.environ["GITHUB_REPO"]
    ref_spec = f"+refs/pull/{pr_number}/head:refs/remotes/origin/pr/{pr_number}"
    remote_url = f"https://x-access-token:{github_token}@github.com/{repo_full}.git"
    subprocess.run(
        ["git", "remote", "set-url", "origin", remote_url],
        cwd=repo_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "fetch", "origin", ref_spec],
        cwd=repo_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "checkout", f"pr/{pr_number}"],
        cwd=repo_path,
        check=True,
        capture_output=True,
    )


def get_changed_files(repo_path: Path) -> list[str]:
    result = subprocess.run(
        ["git", "diff", "--name-only", "origin/main...HEAD"],
        cwd=repo_path,
        capture_output=True,
        text=True,
        check=True,
    )
    return [f.strip() for f in result.stdout.splitlines() if f.strip()]


def run_qa(repo_path: Path, changed_files: list[str]) -> dict:
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
    return {
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


def post_review_comment(
    repo_full_name: str,
    pr_number: str,
    qa_output: str,
    github_token: str,
) -> str:
    body = (
        f"## Automated Code Review\n\nFindings from multi-lens QA on changed files:\n\n```\n{qa_output[:3000]}\n```\n"
    )
    result = subprocess.run(
        [
            "gh",
            "pr",
            "comment",
            pr_number,
            "--repo",
            repo_full_name,
            "--body",
            body,
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

    with tempfile.TemporaryDirectory(prefix="pr-reviewer-") as tmpdir:
        work_dir = Path(tmpdir)
        repo_url = f"https://github.com/{env['github_repo']}.git"
        repo_path = clone_repo(repo_url, work_dir)

        fetch_pr_head(repo_path, env["pr_number"], env["github_token"])
        changed_files = get_changed_files(repo_path)

        if not changed_files:
            write_output(
                status="skipped",
                summary="No changed files detected for review",
            )
            return

        print(f"Reviewing {len(changed_files)} changed files")
        result = run_qa(repo_path, changed_files)

        comment_url = post_review_comment(
            env["github_repo"],
            env["pr_number"],
            result["stdout"],
            env["github_token"],
        )

        write_output(
            status="success" if result["returncode"] == 0 else "issues_found",
            summary=f"Reviewed {len(changed_files)} files in PR #{env['pr_number']}",
            extra={
                "changed_files_count": len(changed_files),
                "review_comment_url": comment_url,
                "files": changed_files,
            },
        )


if __name__ == "__main__":
    main()
