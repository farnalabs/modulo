# Sandbox Agent Pipeline Configuration Guide

Everything we learned (the hard way) about configuring `sandbox_agent` nodes in Modulo pipelines.

---

## 1. Pipeline Node Configuration Checklist

| Field | Required | Value |
|---|---|---|
| `agent_prompt` | **REQUIRED** | Non-empty Jinja2 template string |
| `template_id` | **REQUIRED** | `"opencode"` (maps to E2B sandbox) |
| `agent_command` | **REQUIRED** | Shell command to run inside the sandbox |
| `timeout_seconds` | Optional | 1200 (20 min) |
| `env_vars` | Optional | Extra env vars; prefer system-resolved over hardcoded |

### `agent_prompt`
- Must be a non-empty string. Empty prompts cause the agent to hang indefinitely or error silently.
- Supports Jinja2 template variables resolved at pipeline run time (see [System Env Vars](#5-system-env-vars-available-inside-sandbox)).

### `template_id`
- Must be `"opencode"` exactly. This selects the E2B sandbox runtime with opencode pre-configured.
- Any other value will not provision an E2B sandbox.

### `agent_command`
- The shell command executed inside the E2B sandbox.
- Must install opencode, configure auth, and run the agent.
- See [Working `agent_command` template](#7-working-agent_command-template).

### `timeout_seconds`
- Default: 1200 (20 minutes).
- Set higher for long-running tasks (large PR reviews, full test suites).
- The E2B platform may also enforce its own timeout; `asyncio.wait_for` in the runner is the belt-and-suspenders.

### `env_vars`
- Use for environment-specific overrides only.
- **Never hardcode secrets** — let the system resolve them from the host environment.
- If you must add vars here, ensure they complement rather than override system-resolved vars (see [Forbidden Patterns](#3-forbidden-patterns)).

---

## 2. Agent Prompt Best Practices

### Keep focused and narrow
- A prompt like "Run the full test suite and report results" will timeout or exhaust the E2B sandbox.
- Scope each prompt to a single, well-defined task: "Review PR #1234 for SQL injection vulnerabilities" or "Run `pytest tests/unit/api/` and report failures."

### Include explicit git operations if pushing
- The sandbox starts empty — clone the repo explicitly:
  ```
  git clone https://x-access-token:$GITHUB_TOKEN@github.com/farnalabs/modulo.git /tmp/repo
  ```
- If the agent should push changes, include branch creation and push steps in the prompt.

### Specify output JSON format
- Always tell the agent what JSON structure to output. Example:
  ```
  Output JSON: {"summary": string, "changed_files": string[], "pr_url": string}
  ```
- Without an explicit format, opencode returns unstructured text that `node_runner.py` cannot parse.

### Test with a short timeout first
- Before running a 20-min pipeline, test with `timeout_seconds: 60` and a trivial prompt to verify connectivity, auth, and output parsing.
- Increment timeout only after confirming the basic flow works.

---

## 3. Forbidden Patterns

### Empty `agent_prompt`
- An empty string causes the opencode CLI to hang indefinitely (reads empty stdin) or error silently with no output.
- The pipeline node will stall until the global timeout kills it.
- **Fix:** Always provide a non-empty prompt template. Validate in the API layer before saving.

### Hardcoded `GITHUB_TOKEN` in `env_vars`
- Hardcoding tokens in pipeline config means:
  - Tokens can't be rotated without updating every pipeline.
  - Pipelines checked into git leak credentials.
  - Tokens expire; the pipeline silently fails.
- **Fix:** Rely on system env var resolution. The host runner injects `GITHUB_TOKEN` from the secure env chain.

### `env_vars` overriding system-resolved vars
- `node_runner.py` applies `**env_vars_extra` before system-resolved environment variables, meaning explicit `env_vars` overwrite the system values.
- If a pipeline sets `GITHUB_TOKEN` in `env_vars` (even to an empty string), it shadows the system-resolved token.
- **Fix:** `node_runner.py` must put `**env_vars_extra` **after** system vars so system vars take precedence. Pipeline config should leave secrets out of `env_vars` entirely.

### Missing timeout on E2B API calls
- Calls to `sandbox.commands.run()`, `sandbox.files.read()`, and `sandbox.files.write()` without a timeout can hang indefinitely if the E2B platform stalls.
- The SDK's `request_timeout` parameter may be ignored by the platform.
- **Fix:** Always wrap E2B calls in `asyncio.wait_for()` with a sensible timeout.

---

## 4. Known E2B Issues

### SDK `request_timeout` may be ignored
The E2B Python SDK accepts a `request_timeout` parameter, but the upstream platform does not reliably enforce it. Calls can hang past the specified timeout.

**Fix:** Always wrap E2B calls in `asyncio.wait_for()`:

```python
async def run_with_timeout(coro, timeout=300):
    return await asyncio.wait_for(coro, timeout=timeout)

# Usage
output = await run_with_timeout(sandbox.commands.run("opencode run ..."), timeout=600)
```

### `sandbox.kill()` in `finally` blocks blocks CancelledError
When `asyncio.wait_for` raises `TimeoutError` or `CancelledError`, the `finally` block runs `sandbox.kill()` — but `kill()` itself is a network call that can block, preventing the cancellation from propagating.

**Fix:** Wrap `sandbox.kill()` in `asyncio.wait_for()` too:

```python
finally:
    try:
        await asyncio.wait_for(sandbox.kill(), timeout=10)
    except (TimeoutError, CancelledError):
        pass  # kill timed out, sandbox will be reaped by platform
```

### `sandbox.files.read()` and `sandbox.commands.run()` need both SDK timeout AND `asyncio.wait_for`
The SDK `request_timeout` is a client-side timeout for the HTTP request itself. The `asyncio.wait_for` is a deadline for the entire operation. Both are needed because:

1. SDK timeout prevents the HTTP client from hanging forever.
2. `asyncio.wait_for` prevents the application from stalling if the SDK timeout is ineffective.

```python
await asyncio.wait_for(
    sandbox.commands.run(cmd, request_timeout=300),
    timeout=600
)
```

---

## 5. System Env Vars Available Inside Sandbox

These are resolved from the host environment and injected by `node_runner.py`:

| Variable | Source | Purpose |
|---|---|---|
| `APP_MODULO_OPENCODE_API_KEY` | `.env` / vault | Authenticates opencode CLI with the Modulo API |
| `GITHUB_TOKEN` | Env chain (see below) | Git clone/push auth for `farnalabs/modulo` |
| `MODULO_RUN_ID` | Pipeline run context | Current run ID for logging/tracing |
| `MODULO_PIPELINE_ID` | Pipeline definition | Current pipeline ID |
| `MODULO_ORG_ID` | Org context | Current organisation ID |
| `MODULO_INPUT_PAYLOAD` | Trigger payload | JSON input payload from the trigger event |

### GITHUB_TOKEN resolution chain

The host runner resolves `GITHUB_TOKEN` in this priority order (first non-empty wins):

1. `GITHUB_DOGFOOD_PAT_ALL` — full-scope dogfood PAT
2. `GITHUB_DOGFOOD_PAT_WR` — write-scope dogfood PAT
3. `GITHUB_TOKEN` — generic fallback

This chain means tokens can be rotated in the host environment without touching any pipeline configuration.

---

## 6. Stale Run Cleanup

### Default timeout
- Runs that haven't progressed in **30+ minutes** are considered stale and killed.
- Controlled by the cleanup scheduler that queries runs with `status = 'running'` and `updated_at < now() - interval '30 minutes'`.

### Cleanup query
```sql
UPDATE pipeline_runs
SET status = 'failed', error = 'Run timed out after 30 minutes of inactivity'
WHERE status = 'running'
  AND updated_at < NOW() - INTERVAL '30 minutes';
```

### The `outputs_json::jsonb` cast
When comparing or querying `outputs_json`, always cast to `jsonb`:

```sql
-- Correct
WHERE outputs_json::jsonb IS NOT NULL

-- Wrong (type mismatch errors)
WHERE outputs_json IS NOT NULL
```

The column stores JSON as text; the `::jsonb` cast ensures proper type comparison and avoids PostgreSQL type coercion surprises.

---

## 7. Working `agent_command` template

```bash
npm install -g opencode-ai@1.18.8 2>&1 | tail -1 \
  && mkdir -p ~/.local/share/opencode \
  && printf '{"opencode":{"type":"api","key":"%s"},"opencode-go":{"type":"api","key":"%s"}}' \
       "$APP_MODULO_OPENCODE_API_KEY" "$APP_MODULO_OPENCODE_API_KEY" \
       > ~/.local/share/opencode/auth.json \
  && opencode run --model opencode-go/deepseek-v4-flash --auto --format json < /home/user/prompt.md \
  ; echo '{"summary":"No output from agent","changed_files":[],"pr_url":""}' > /home/user/output.json
```

### What each part does

1. **Install opencode** — `npm install -g opencode-ai@1.18.8` with stderr redirected to stdout and tailed to one line (avoids log noise).
2. **Create auth dir** — `mkdir -p ~/.local/share/opencode`.
3. **Write auth.json** — Uses `printf` with env var substitution to create the credential file. Both `opencode` and `opencode-go` keys are set to the same API key.
4. **Run opencode** — `opencode run` with the deepseek model, auto-confirm, JSON output format, reading the prompt from `/home/user/prompt.md`.
5. **Fallback output** — `echo` after `;` (not `&&`) ensures a fallback JSON is written even if the opencode command fails, so `node_runner.py` always has something to parse.

---

## 8. Working `agent_prompt` template

```
Check the farnalabs/modulo codebase for [specific task].
Work in /tmp/repo. Clone first:
  git clone https://x-access-token:$GITHUB_TOKEN@github.com/farnalabs/modulo.git /tmp/repo

Output JSON: {"summary": string, "changed_files": string[], "pr_url": string}
```

### Replace `[specific task]` with:
- "changes between commit A and commit B"
- "SQL injection vulnerabilities in routes that accept user input"
- "files that reference deprecated `pkg_resources`"
- "whether PR #5678 contains any hardcoded credentials"

### Notes
- The `$GITHUB_TOKEN` is resolved inside the sandbox from the system env var (see [Section 5](#5-system-env-vars-available-inside-sandbox)).
- The output format **must** match exactly what `node_runner.py` expects or the run will fail with a parse error.
- The prompt is read by opencode from `/home/user/prompt.md` (as specified in `agent_command`).

---

## 9. Troubleshooting

### "No output from agent" in run results
The fallback `echo` in `agent_command` wrote the default JSON. This means opencode either:
- Exited with a non-zero code (check agent prompt validity)
- Timed out (increase `timeout_seconds`)
- Couldn't authenticate (check `APP_MODULO_OPENCODE_API_KEY` is set in the host env)

### Run hangs until global timeout
- **Most likely:** Empty `agent_prompt` — opencode reads empty stdin and waits.
- **Also possible:** E2B API call hung without `asyncio.wait_for` wrapper.
- Check the `agent_prompt` field in the pipeline definition. If it's empty or whitespace-only, that's the cause.

### "401 Unauthorized" during git clone
- `GITHUB_TOKEN` is empty or expired in the host environment.
- Check the env chain: `GITHUB_DOGFOOD_PAT_ALL` -> `GITHUB_DOGFOOD_PAT_WR` -> `GITHUB_TOKEN`.
- Verify tokens are still valid in the vault.

### JSON parse error on run output
- The agent prompt doesn't specify an explicit output format, or the agent returned unstructured text.
- Add `Output JSON: {"summary": string, "changed_files": string[], "pr_url": string}` to the prompt.
- Verify `node_runner.py`'s parsing regex matches the exact key names.

### `sandbox.kill()` never returns
- Known E2B issue: `kill()` can hang if the platform is under load.
- Fix confirmed working: wrap in `asyncio.wait_for(kill(), timeout=10)` with `try/except`.
- The sandbox will be garbage-collected by the E2B platform eventually even if `kill()` fails.

### Pipeline run says "completed" but output is empty
The node's `outputs_json` field is NULL or empty object. Possible causes:
- `agent_command` didn't write to `/home/user/output.json` (the expected path).
- `outputs_json` comparison in SQL uses `=` instead of `::jsonb` cast, causing type mismatch.
- The cleanup scheduler marked it complete before the agent finished (stale run detection too aggressive).
