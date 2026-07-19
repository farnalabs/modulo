"""Shared scaffold for Modulo dogfooding pipeline scripts.

All pipeline scripts should import and use this instead of duplicating code.
"""

import json
import os

__all__ = [
    "call_opencode",
    "check_env",
    "exit_completed",
    "exit_failed",
    "extract_code_block",
    "get_git_url",
    "gh_api",
    "git_askpass_script",
    "run_git",
    "safe_clone",
    "setup_opencode_auth",
    "validate_output",
    "write_output",
]
import re
import subprocess
import time
import urllib.request


# === Environment ===
def check_env():
    """Validate required env vars. Returns (token, api_key) or exits."""
    token = os.environ.get("GITHUB_TOKEN", "")
    api_key = os.environ.get("APP_MODULO_OPENCODE_API_KEY", "")
    if not token or not api_key:
        exit_failed("Missing GITHUB_TOKEN or APP_MODULO_OPENCODE_API_KEY")
    return token, api_key


# === Opencode Auth ===
def setup_opencode_auth(api_key):
    """Write auth.json for opencode CLI. Returns model name string."""
    auth_dir = os.path.expanduser(os.path.join("~", ".local", "share", "opencode"))
    os.makedirs(auth_dir, exist_ok=True)
    with open(os.path.join(auth_dir, "auth.json"), "w") as f:
        json.dump({"opencode": {"type": "api", "key": api_key}, "opencode-go": {"type": "api", "key": api_key}}, f)
    return "opencode-go/deepseek-v4-flash" if api_key else "opencode/deepseek-v4-flash-free"


# === Git Operations (token-safe) ===
def git_askpass_script(token, path="/tmp/git-askpass.sh"):
    """Write a GIT_ASKPASS script to avoid embedding token in git URLs."""
    with open(path, "w") as f:
        f.write("#!/bin/sh\n")
        f.write('echo "' + token + '"\n')
    os.chmod(path, 0o700)
    return path


def safe_clone(token, repo_url, dest, timeout=90):
    """Clone a repo without exposing token in URLs or logs."""
    askpass = git_askpass_script(token)
    env = os.environ.copy()
    env["GIT_ASKPASS"] = askpass
    safe_url = repo_url.replace(token, "PLACEHOLDER")
    r = subprocess.run(["git", "clone", safe_url, dest], capture_output=True, text=True, timeout=timeout, env=env)
    if r.returncode != 0:
        exit_failed("Clone failed: " + r.stderr[:200])
    return dest


def run_git(cmd, cwd=None, capture=True, timeout=60, env=None):
    """Run git command with optional timeout."""
    return subprocess.run(cmd, capture_output=True, text=True, cwd=cwd, timeout=timeout, env=env)


def get_git_url(token, owner="farnalabs", repo="modulo"):
    """Get a safe git URL (without embedded token)."""
    return "https://x-access-token@github.com/" + owner + "/" + repo + ".git"


# === Opencode Invocation ===
def call_opencode(prompt, model, timeout=180):
    """Run opencode with a prompt, parse NDJSON, return collected text. Returns ('full_text', wall_clock_ms)."""
    start = time.time()
    r = subprocess.run(
        ["opencode", "run", "--format", "json", "-m", model, prompt], capture_output=True, text=True, timeout=timeout
    )
    elapsed = int((time.time() - start) * 1000)
    full = ""
    for line in (r.stdout or r.stderr).strip().split(chr(10)):
        try:
            e = json.loads(line)
            if e.get("type") == "text":
                full += e["part"].get("text", "")
        except Exception:
            pass
    return full, elapsed


def extract_code_block(text):
    """Extract the LAST ```...``` code block from text."""
    if not text:
        return None
    blocks = list(re.finditer(r"```\w*\n(.+?)\n```", text, re.DOTALL))
    if not blocks:
        return None
    return blocks[-1].group(1).strip() + chr(10)


# === GitHub API ===
def gh_api(owner, repo, path, token, method="GET", data=None):
    """Call GitHub REST API. Returns parsed JSON."""
    url = "https://api.github.com/repos/" + owner + "/" + repo + path
    headers = {"Authorization": "bearer " + token, "Content-Type": "application/json"}
    if isinstance(data, str):
        data = data.encode()
    elif data is not None:
        data = json.dumps(data).encode()
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    return json.loads(urllib.request.urlopen(req, timeout=15).read())


# === Output ===
def validate_output(output, schema):
    """Validate output against a JSON schema (basic check)."""
    if not isinstance(output, dict):
        return False
    return all(req in output for req in schema.get("required", []))


def write_output(output, path="/home/user/output.json"):
    """Write validated output with cost estimate."""
    if "cost_estimate_usd" not in output and "wall_clock_ms" in output:
        est_tokens = output["wall_clock_ms"] / 1000 * 100
        output["cost_estimate_usd"] = round(est_tokens * 0.0000015, 6)
    with open(path, "w") as f:
        json.dump(output, f)


def exit_failed(msg, extra=None, path="/home/user/output.json"):
    """Exit with failure status and message."""
    out = {"status": "failed", "summary": msg}
    if extra:
        out["output_json"] = extra
    write_output(out, path)
    exit(1)


def exit_completed(summary, extra=None, path="/home/user/output.json"):
    """Exit with completed status."""
    out = {"status": "completed", "summary": summary}
    if extra:
        out["output_json"] = extra
    write_output(out, path)
    exit(0)
