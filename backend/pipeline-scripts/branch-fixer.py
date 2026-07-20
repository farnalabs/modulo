"""Branch Fixer Pipeline - Agent-driven."""
import os, subprocess, tempfile, time
from pathlib import Path
from _common import check_env, exit_completed, exit_failed, get_git_url, run_git, safe_clone, setup_opencode_auth

PROMPT_FILE = ".agents/skills/branch-fixer/BRANCH_FIXER_PROMPT.md"

def check_env_extra():
    missing = [k for k in ["GITHUB_REPO"] if k not in os.environ]
    if missing:
        exit_failed("Missing required env vars")
    branch_name = os.environ.get("BRANCH_NAME", "").strip()
    if not branch_name:
        exit_failed("BRANCH_NAME is required")
    return os.environ["GITHUB_REPO"], branch_name

def resolve_branch_ref(repo_path, branch_name, token):
    r = subprocess.run(["git", "fetch", "origin", branch_name], cwd=repo_path, capture_output=True, text=True, timeout=30)
    if r.returncode == 0:
        return branch_name
    if branch_name.isdigit():
        r = subprocess.run(["git", "fetch", "origin", "+refs/pull/" + branch_name + "/head:refs/remotes/origin/pr/" + branch_name], cwd=repo_path, capture_output=True, text=True, timeout=30)
        if r.returncode == 0:
            return "pr/" + branch_name
    exit_failed("Could not fetch branch: " + branch_name)

def call_agent(repo_path):
    prompt_path = Path(repo_path) / PROMPT_FILE
    if not prompt_path.exists():
        print("Agent prompt not found, falling back to qa-iterate")
        return subprocess.run(["opencode", "qa-iterate"], cwd=repo_path, capture_output=True, text=True, timeout=900)
    desc = os.environ.get("FAILURE_DESCRIPTION", "Unknown failure")
    pr = os.environ.get("GITHUB_PR_NUMBER", "N/A")
    branch = os.environ.get("BRANCH_NAME", "unknown")
    context = "Diagnose failures on branch: " + branch + ". PR: " + pr + ". Failure description: " + desc
    return subprocess.run(["opencode", "run", "--agent", "coder", context], cwd=repo_path, capture_output=True, text=True, timeout=900)

def main():
    token, api_key = check_env()
    model = setup_opencode_auth(api_key)
    github_repo, branch_name = check_env_extra()
    pr_number = os.environ.get("GITHUB_PR_NUMBER", "").strip()
    with tempfile.TemporaryDirectory(prefix="branch-fixer-") as tmpdir:
        owner, repo_part = github_repo.split("/")
        repo_path = safe_clone(token, get_git_url(token, owner, repo_part), Path(tmpdir) / "repo")
        local_ref = resolve_branch_ref(repo_path, branch_name, token)
        run_git(["checkout", local_ref], cwd=repo_path)
        start = time.time()
        result = call_agent(repo_path)
        elapsed = int((time.time() - start) * 1000)
        if result.returncode != 0:
            exit_failed("Agent failed", extra={"stderr": result.stderr[:1000], "wall_clock_ms": elapsed})
        run_git(["add", "-A"], cwd=repo_path)
        if run_git(["diff", "--cached", "--quiet"], cwd=repo_path).returncode != 0:
            run_git(["commit", "-m", "fix: automated branch fix agent patches"], cwd=repo_path)
            r2 = run_git(["rev-parse", "HEAD"], cwd=repo_path)
            sha = r2.stdout.strip()
            run_git(["remote", "set-url", "origin", get_git_url(token, owner, repo_part)], cwd=repo_path)
            run_git(["push", "origin", local_ref + ":" + local_ref], cwd=repo_path, timeout=120)
            if pr_number:
                subprocess.run(["gh", "pr", "comment", pr_number, "--repo", github_repo, "--body", "Branch fixer applied: " + sha], env={**os.environ, "GH_TOKEN": token})
            exit_completed("Fixed " + branch_name, extra={"commit_sha": sha, "wall_clock_ms": elapsed, "agent_output": result.stdout[:2000]})
        else:
            exit_completed("No fixes needed on " + branch_name, extra={"wall_clock_ms": elapsed})

if __name__ == "__main__":
    main()