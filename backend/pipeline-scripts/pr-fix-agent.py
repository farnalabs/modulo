"""PR Fix Agent Pipeline

Triggered when a review from the PR Reviewer identifies fixable issues.
Clones the PR branch, applies automated fixes via `opencode qa-iterate`,
commits the fixes back to the PR branch, and notifies the PR.

Environment Variables:
    GITHUB_TOKEN      — GitHub PAT for cloning and pushing fixes
    GITHUB_REPO       — Repository full name (e.g. "farnalabs/modulo")
    GITHUB_PR_NUMBER  — Pull request number to fix
    OPENCODE_API_KEY  — API key for the opencode CLI

Output:
    Writes status, summary (files fixed, commit SHA), and pr_url
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
        ["git", "fetch", "origin", f"+refs/pull/{pr_number}/head:refs/remotes/origin/pr/{pr_number}"],
        cwd=clone_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "checkout", f"pr/{pr_number}"],
        cwd=clone_path,
        check=True,
        capture_output=True,
    )
    return clone_path


def run_fix_iteration(repo_path: Path) -> dict:
    result = subprocess.run(
        ["opencode", "qa-iterate"],
        cwd=repo_path,
        capture_output=True,
        text=True,
        timeout=900,
    )
    return {
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


def get_branch_name(repo_path: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=repo_path,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def commit_and_push_fixes(repo_path: Path, branch_name: str) -> str:
    subprocess.run(
        ["git", "add", "-A"],
        cwd=repo_path,
        check=True,
        capture_output=True,
    )
    result = subprocess.run(
        ["git", "diff", "--cached", "--quiet"],
        cwd=repo_path,
        capture_output=True,
    )
    if result.returncode == 0:
        return ""

    subprocess.run(
        ["git", "commit", "-m", "fix: automated PR fix agent patches"],
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
    commit_sha = commit_result.stdout.strip()

    subprocess.run(
        ["git", "push", "origin", branch_name],
        cwd=repo_path,
        check=True,
        capture_output=True,
    )

    return commit_sha


def post_fix_comment(
    repo_full_name: str,
    pr_number: str,
    commit_sha: str,
    github_token: str,
) -> str:
    body = (
        "## Automated Fixes Applied\n\n"
        f"Fix commit: `{commit_sha}`\n\n"
        "These fixes were applied automatically by the PR Fix Agent.\n"
        "Please review before merging."
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

    with tempfile.TemporaryDirectory(prefix="pr-fix-agent-") as tmpdir:
        work_dir = Path(tmpdir)
        repo_url = f"https://github.com/{env['github_repo']}.git"
        repo_path = clone_and_checkout_pr(
            repo_url,
            work_dir,
            env["pr_number"],
            env["github_token"],
        )

        branch_name = get_branch_name(repo_path)
        fix_result = run_fix_iteration(repo_path)

        if fix_result["returncode"] != 0:
            post_fix_comment(
                env["github_repo"],
                env["pr_number"],
                f"Fix agent encountered errors:\n```\n{fix_result['stderr'][:1000]}\n```",
                env["github_token"],
            )
            write_output(
                status="failed",
                summary="qa-iterate returned non-zero exit code",
                extra={"stderr": fix_result["stderr"]},
            )
            sys.exit(1)

        commit_sha = commit_and_push_fixes(repo_path, branch_name)

        if not commit_sha:
            write_output(
                status="success",
                summary="No fixes needed — PR is already clean",
                extra={"pr_number": env["pr_number"]},
            )
            return

        comment_url = post_fix_comment(
            env["github_repo"],
            env["pr_number"],
            commit_sha,
            env["github_token"],
        )

        write_output(
            status="success",
            summary=f"Fixed and pushed to PR #{env['pr_number']}",
            extra={
                "commit_sha": commit_sha,
                "branch": branch_name,
                "comment_url": comment_url,
            },
        )


if __name__ == "__main__":
    main()
