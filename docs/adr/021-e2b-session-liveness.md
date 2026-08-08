# 021 — E2B sandbox session liveness: stdout redirected to a log file + drain probe

- Status: accepted
- Date: 2026-08-08
- Related: Linear FAR-97, FAR-98

## Context

Long multi-turn `sandbox_agent` nodes running opencode inside E2B sandboxes
stall after ~30-70s of captured tool activity, then sit silent for 10-15+
minutes until the node timeout / idle watchdog kills them. Short sessions
(e.g. the PR Reviewer, ~52s) complete fine. All long improve-* pipelines
(Improve Tests, Improve UX, Improve Architecture) and the Backlog Groomer
pipeline are affected.

Production symptom (2026-08-08): the run fails after ~5 minutes with
**completely empty** stdout/stderr, exit_code -1, the sandbox log tail showing
`Process sendSignal` / `Process ended` (a signal kill), and the node message
"Sandbox agent command produced no output within Ns".

### What the code actually does (ruled out)

The E2B SDK (v2.6.1) `commands.run(background=True, on_stdout=..., on_stderr=...)`
path already streams process output incrementally: `AsyncCommandHandle._handle_events`
drains the RPC stream continuously into `self._stdout` and dispatches each chunk to
the `on_stdout`/`on_stderr` callbacks. The harness is therefore **not** "reading
output only once at the end". The naive "Modulo never drains the pipe" theory does
not hold.

### Root cause

Two compounding facts:

1. **The sandbox-side pipe can still fill.** The process's stdout is captured by
   E2B's `envd` agent through a pipe and streamed over the RPC connection. A long
   opencode session emits ~1KB NDJSON per tool event; a ~64KB pipe buffer fills
   after ~30-70 events — matching the observed "30-70s of tool activity then
   silence". When the pipe fills, the process blocks on write and appears silent,
   even though it is alive and working.

2. **The idle watchdog's liveness signal is the RPC stream.** `_activity["last"]`
   was updated only by the `on_stdout`/`on_stderr` callbacks. When the RPC stream
   stalls (pipe backpressure, silent stream drop), the callbacks stop firing and
   the idle watchdog (300s) treats the alive-but-blocked process as stalled,
   SIGKILLs it (`Process sendSignal` / `Process ended`), and the node fails with
   empty output — exactly the production symptom. The kill-before-output-read
   path then discards whatever had been streamed.

## Decision

Redirect the agent command's stdout/stderr to a **regular file inside the
sandbox** and drain that file periodically:

- The command is wrapped as `( <agent_command> ) > /home/user/agent.log 2>&1`.
  The subshell preserves the command's exit code for the SDK's `wait()`.
  The process's stdout is now a regular file, so a pipe can never fill and the
  process can never block on write — the pipe-buffer mechanism is eliminated at
  the source.
- A **drain probe** runs on every idle-watchdog tick (every `_SANDBOX_TAIL_INTERVAL`
  = 5s): it stats the log file (`files.get_info`), and when it has grown, reads
  it and streams the newly-appended content to the run event broker (live output,
  FAR-98) and accumulates it for the node artifact.
- The idle watchdog's liveness signal now comes from the **drain probe
  succeeding** (the sandbox connection is responsive), not from streamed output.
  A live-but-silent agent (long LLM turn, quiet tool) is never killed for being
  quiet — it gets the full node timeout budget. The watchdog now fires only on a
  genuine stall: the sandbox connection unresponsive for `idle_timeout` seconds.
- `agent_stdout` in the artifact is the drained log content, falling back to the
  SDK's captured stdout for legacy/non-redirected paths. This also fixes the
  empty-output symptom on timeouts: whatever was produced before the stall is now
  surfaced instead of dropped.

`exec_command` on the `RuntimeProvider`/E2B provider path is unchanged: it is a
separate foreground path (used by the environments "hello" probe), not the
`sandbox_agent` node, and the SDK streams its output internally.

## Consequences

Positive:

- A long session's process can never block on a full stdout pipe.
- Live-but-silent agents are not falsely killed; they complete within the node
  timeout.
- Genuine stalls (sandbox connection dead) still fail fast within `idle_timeout`.
- Partial output survives a timeout/failure and is surfaced in the artifact.
- `output.json` reading and timeout/kill-before-output-read semantics are
  unchanged.

Negative:

- stdout and stderr are merged into one file (`2>&1`), so the artifact's
  `agent_stdout` carries combined output and `agent_stderr` is empty for
  redirected commands. Acceptable for the summary/audit surface; can be split
  into a second file later if needed.
- A silent-but-connected agent that is genuinely making no progress now burns the
  full node timeout instead of failing at 300s. This is the deliberate trade-off:
  false kills (the observed bug) are worse than delayed true-stall detection, and
  the node timeout still bounds everything.
- The drain probe adds one `get_info` RPC every 5s (negligible) and a full file
  read only when the file grew (bounded by `_MAX_ARTIFACT_LOG` truncation at the
  artifact stage; the in-memory accumulation is proportional to session output).

The FAR-98 live-streaming (`_stream_chunk` publishing) is preserved: the drain
probe publishes newly-appended content, and the `on_stdout`/`on_stderr` callbacks
remain wired for the (now rare) non-redirected output.
