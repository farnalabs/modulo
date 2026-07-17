#!/usr/bin/env python3
"""Modulo PR Review Agent — uses OpenAI-compatible API for LLM-based PR review.

Calls any OpenAI-compatible chat completions API (configured via env vars).
Reads prompt from /home/user/prompt.md, writes structured output to output.json.

Env vars (set by modulo-wrap.sh from Fly secrets):
  OPENAI_API_KEY      — API key for the LLM provider (required)
  OPENAI_BASE_URL     — Base URL (default: https://opencode.ai/zen/go/v1)
  APP_MODULO_OPENCODE_API_KEY — fallback if OPENAI_API_KEY not set
  GITHUB_TOKEN        — for cloning private repos (passed by node_runner)
"""
import json, os, sys, re, urllib.request, urllib.error, textwrap

PROMPT_FILE = os.environ.get("MODULO_PROMPT_FILE", "/home/user/prompt.md")
OUTPUT_FILE = os.environ.get("MODULO_OUTPUT_FILE", "/home/user/output.json")

def get_api_config():
    """Get API key and base URL from environment."""
    api_key = os.environ.get("OPENAI_API_KEY", "") or os.environ.get("APP_MODULO_OPENCODE_API_KEY", "")
    base_url = os.environ.get("OPENAI_BASE_URL", "https://opencode.ai/zen/go/v1").rstrip("/")
    model = os.environ.get("OPENAI_MODEL", "opencode-go")
    return api_key, base_url, model

def extract_pr_url(prompt):
    m = re.search(r"github\.com/([^/]+)/([^/]+)/pull/(\d+)", prompt)
    if m:
        return f"https://github.com/{m.group(1)}/{m.group(2)}/pull/{m.group(3)}"
    return prompt.strip()

def call_llm(prompt, api_key, base_url, model):
    """Call an OpenAI-compatible chat completions API."""
    url = f"{base_url}/chat/completions"
    
    system_prompt = textwrap.dedent("""\
        You are a senior code reviewer. Review the pull request at the URL provided.
        Analyze for: security issues, error handling, code style, maintainability.
        Return valid JSON with keys: verdict (APPROVE|REQUEST_CHANGES|COMMENT),
        summary (string), issues_found (array of {severity, lens, file, line, description}).""")

    payload = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt[:12000]}
        ],
        "max_tokens": 2000,
        "temperature": 0.3,
    }).encode()

    req = urllib.request.Request(
        url, data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:500]
        return {"error": f"HTTP {e.code}: {body}"}
    except Exception as e:
        return {"error": str(e)}

def format_output(api_response, pr_url):
    content = ""
    if "choices" in api_response:
        content = api_response["choices"][0].get("message", {}).get("content", "")
    elif "error" in api_response:
        return {"status": "completed",
                "summary": f"LLM unavailable: {api_response['error'][:200]}",
                "issues_found": [], "pr_url": pr_url}

    try:
        parsed = json.loads(content)
        return {"status": "completed", "summary": parsed.get("summary", ""),
                "verdict": parsed.get("verdict", "COMMENT"),
                "issues_found": parsed.get("issues_found", []), "pr_url": pr_url}
    except json.JSONDecodeError:
        return {"status": "completed", "summary": content[:1000],
                "issues_found": [], "pr_url": pr_url}

def main():
    if not os.path.exists(PROMPT_FILE):
        with open(OUTPUT_FILE, "w") as f:
            json.dump({"status": "failed", "summary": "No prompt file"}, f)
        sys.exit(1)

    with open(PROMPT_FILE) as f:
        prompt = f.read()

    pr_url = extract_pr_url(prompt)
    api_key, base_url, model = get_api_config()

    if not api_key:
        result = {"status": "completed",
                  "summary": f"No LLM API key configured. Set OPENAI_API_KEY or APP_MODULO_OPENCODE_API_KEY.",
                  "issues_found": [], "pr_url": pr_url}
    else:
        api_response = call_llm(prompt, api_key, base_url, model)
        result = format_output(api_response, pr_url)

    with open(OUTPUT_FILE, "w") as f:
        json.dump(result, f, indent=2)
    print(f"Review written to {OUTPUT_FILE}", flush=True)
    sys.exit(0)

if __name__ == "__main__":
    main()
