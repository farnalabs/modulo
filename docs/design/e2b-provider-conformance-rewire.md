# Design: E2B Provider-Conformance Rewire - inventory and flag-gated plan (FAR-1050 deliverable 1)

**Status:** Inventory + plan only. This document changes no production behaviour; it is the precondition slice for the dispatch rewire.
**Governs:** FAR-1050 deliverable 1 - bringing the E2B managed-sandbox path onto the frozen `RuntimeProvider` contract (ADR 040). FAR-1050 slice 1 shipped the additive `RuntimeProviderError` family; slice 2 shipped `destroy_workspace_by_ref` + `exec_command_stream` parity on `E2BRuntimeProvider` (PR #923 lineage). The remaining work is the dispatch rewire: `_sandbox_agent_impl` calls the E2B SDK directly and bypasses the ABC.
**Related:** ADR 040 `Repos/devtools/adr/040-runtime-provider-contract-and-tier-conformance.md` (conformance clause, sanctioned-site requirement, A21 gating, "its own release with its own flag and revert point"); ADR 051 `Repos/devtools/adr/051-managed-sandbox-adapter-pattern.md` (FAR-1050 deliverable 2, the T2a adapter pattern - note the brief's "ADR 042 managed-sandbox-adapter-pattern" resolves to ADR 051; ADR 042 is the governed-run contract).

## 0. Methodology (reproducible)

Run from the worktree root. `rg` was unavailable on this machine, so `Select-String` was used; portable `grep` equivalents are given so the inventory can be re-derived anywhere. Base commit: `f086e7220`.

```powershell
# 1. node_runner full E2B reference enumeration (do not sample)
Select-String -Path backend\src\modulo\core\pipeline_engine\node_runner.py -Pattern "e2b|E2B|E2b"
# -> 104 matches across 96 lines (brief's ~92; measured, not sampled)

# 2. All bound forms across product code (imports, annotations, hostname)
Get-ChildItem -Recurse -Filter *.py backend\src | ForEach-Object {
  Select-String -Path $_.FullName -Pattern "from e2b|import e2b\b|AsyncSandbox|api\.e2b\.app" }

# 3. A21 bound form
Get-ChildItem -Recurse -Filter *.py backend\src | ForEach-Object {
  Select-String -Path $_.FullName -Pattern "apply_sandbox_policy" }

# 4. per-file E2B occurrence counts (cluster discovery, outside runtime_provider)
Get-ChildItem -Recurse -Filter *.py backend\src | ForEach-Object {
  $c = (Select-String -Path $_.FullName -Pattern "e2b|E2B" -AllMatches |
        ForEach-Object { $_.Matches.Count } | Measure-Object -Sum).Sum; if ($c) { "$c`t$($_.FullName)" } }

# 5. duck-typed SDK-object usage (no import, but operates on an AsyncSandbox)
Select-String -Path backend\src\modulo\core\pipeline_engine\*.py -Pattern "sandbox\.commands\.run|sandbox\.files\.|sandbox\.kill"
```

Portable equivalents: `grep -rnE "from e2b|import e2b\b|AsyncSandbox|api\.e2b\.app" backend/src --include="*.py"`, `grep -rn "apply_sandbox_policy" backend/src --include="*.py"`, `grep -cE "e2b|E2B" <file>`.

Bound forms per ADR 040: imports (incl. aliased), `from e2b.exceptions`, `AsyncSandbox` annotations (incl. quoted annotations and `ast.Constant` strings - docstrings), `api.e2b.app` hostnames (separate CI ban), and the A21 `apply_sandbox_policy` call-site form. String-literal provider tokens (`"e2b"`) and comments/docstrings are deliberately outside the bound forms (ADR 040) - but a site that gates behaviour on E2B semantics is still inventoried below even when its only token is a string.

**Classification rule.** **sanctioned** = ADR 040's four-category enumeration names it (direct-path sites are enumerated as the *current* scanner exemption and leave at retirement; the other three are structural or named-as-staying), OR it is structural (provider module, hub registration), OR the contract genuinely does not model the capability (E2B billing rate, E2B-template wrapper semantics, save-time substrate-cap validation, provider-type vocabulary). **to-be-migrated** = the frozen ABC models the capability and a rewire slice below moves the call site through it.

**Scope of this document (and non-goals).** Inventory + plan only: no modification of `node_runner.py`, `runtime_provider/**`, or any production behaviour; no CI workflow, scanner, or semgrep rule added or changed; no new third-party dependencies. Every inventory entry below is a grep hit cited in section 0 - nothing is inferred from prose alone. The ADR 040 sanctioned list is amended only by ADR amendment; this document *enumerates* it at file/line granularity, it does not widen it. Expanding the list beyond the sites below remains an ADR-amending decision.

## 1. Inventory - every direct E2B usage site outside `core/runtime_provider/e2b.py`

Twenty-five usage sites: **15 sanctioned, 10 to-be-migrated.** (Plus the excluded provider module itself, `core/runtime_provider/e2b.py` - sanctioned by definition, not enumerated.)

### 1.1 `backend/src/modulo/core/pipeline_engine/node_runner.py` (104 matches / 96 lines, grouped into 12 sites)

| ID | Lines | Symbol / cluster | What it does | Class | Reason |
|---|---|---|---|---|---|
| T1 | 76-77, 7309-7310, 6165, 7483 | `from e2b import AsyncSandbox` (TYPE_CHECKING + runtime), `from e2b.exceptions import RateLimitException, SandboxException`, annotations `sandbox: "AsyncSandbox \| None"` / `sandbox: AsyncSandbox \| None` | The SDK type and exception vocabulary the whole dispatch body is written against | to-be-migrated | Bound-form imports/annotations; post-rewire the dispatch holds a provider-neutral handle (`ExecProcess` / provider ref), not an `AsyncSandbox` |
| T2 | 710-724, 7896-8003 (create at 7924; `RateLimitException` catch 7962; `api_key=` 7934; watchdog raise 8000-8003) | create loop inside `_sandbox_agent_impl` | Provisions the sandbox via `AsyncSandbox.create` with rate-limit backoff, provisioning watchdog, int-timeout + egress boolean + metadata | to-be-migrated | The core dispatch bypass; ABC `create_workspace(spec)` models it (slice R4) |
| T3 | 8497-8508, 5461-5522 | `sandbox.commands.run(..., background=True, on_stdout/on_stderr, envs, timeout)`; `_wait_command_with_idle_watchdog` | Starts the agent command as a background stream; idle watchdog polls `handle.wait()` in shielded slices (built on E2B's connect-timeout-only semantics) | to-be-migrated | ABC `exec_command_stream` (already conformance-shipped, slice 2) models it; watchdog re-expressed over `ExecProcess.done/chunks/kill` |
| T4 | 8021, 8063, 8102, 8111, 8443, 8447-8452, 8468 (writes); 6289, 6311, 6396, 6420, 8721, 8819 (reads/probes) | `sandbox.files.write/read/get_info/list` | Context files, schema contract, input.json, prompt.md, loop-intercept bridge files; log-drain + watch-log probes; stall log re-read; `output.json` read | to-be-migrated | ABC file I/O (`write_file`/`read_file`/`list_files`/`get_info`, ADR 040) models it (slice R2) |
| T5 | 6586 (in `kill_sandbox_for_budget` 6561-6609), 8679, 9531 (finally 9526-9531) | `sandbox.kill(request_timeout=...)` | Budget kill (resource-cap + wall-clock), stall/timeout kill-before-output-read, finally teardown kill | to-be-migrated | ABC `destroy_workspace` / `destroy_workspace_by_ref` model it (slice R4) |
| T6 | 1034-1071 (hostname 1037/1052), 1074-1110; callers 8671, 8884, 9090, 9410 | `_fetch_sandbox_log_tail`, `_combine_log_entries` | HTTP GET `https://api.e2b.app/sandboxes/{id}/logs` (urllib, `X-API-KEY`), bounded tail; fetched before/at failure paths - the only place the kill reason lives | to-be-migrated | Bound-form hostname (CI ban targets it); ADR 040 explicitly arrows it to `read_log_tail` (slice R1) |
| T7 | 6992-7002, 8184-8206 (import 8190, await 8199) | `_should_apply_sandbox_policy`, engine-side `apply_sandbox_policy(sandbox, ...)` invocation | Runs git-credential scope / egress allowlist / read-only seal inside the sandbox before the agent command | to-be-migrated | ADR 040 arrows it to `apply_isolation` (slice R3); this is the A21-guard site |
| T8 | 7025-7057, 9771-9784, key use 7934 | `_script_enforcement_requires_remote`, `_resolved_e2b_key_for_enforcement` | Engine checks raw E2B credential presence to (a) fail script-enforcement closed and (b) pass `api_key=` to create | to-be-migrated | The credential belongs to the provider instance post-rewire; the refusal becomes a typed provider-capability error, not an engine key probe |
| T9 | 6810-6832, 9374-9383 (plus class defs 426-445 - taxonomy, see S11) | `_format_sandbox_provider_error(_exc, SandboxException)` | `isinstance` against the E2B exception type; appends provider response body to the run-output error message (FAR-511) | to-be-migrated | Post-rewire the provider raises typed `RuntimeProviderError` members; isinstance against an SDK type disappears |
| A1 | 7342-7344, 7367, 7386-7387, 7890, 7893 | `RUNNER_PROVIDER_E2B` attribution, `validate_e2b_dispatch_timeout` call, `resolve_egress(tier="e2b")` | Stamps the dispatch marker's provider field, loud timeout-cap check, canonical egress resolution tier | sanctioned | Provider-type vocabulary + save/dispatch-time cap parity - no SDK binding; the tokens stay as long as the `e2b` tier exists (attribution should read `provider.provider_id` after the flip - noted, not a scanner concern) |
| A2 | 5622-5650, caller 9142 | `_is_sandbox_session_lost_echo` | Detects the E2B sandbox *wrapper template's* fallback echo in `output.json` and routes it retryable instead of `agent.failed` | sanctioned | E2B-template semantic; the contract models no primitive for "template wrapper artefact detection" |
| A3 | 974-1031; callers 9048, 9159, 9411 (family continues in other files - see S6) | `_E2B_SANDBOX_USD_PER_HOUR`, `_e2b_rate_runtime`, `_compute_sandbox_cost` | Wall-clock x E2B hourly rate, merged with agent self-reported cost | sanctioned | E2B billing semantics; contract models no cost rate (ADR 040 records the non-E2B-at-E2B-rate defect as an open ticket, not a migration) |

### 1.2 All other product files

| ID | File / lines | Symbol | What it does | Class | Reason |
|---|---|---|---|---|---|
| T10 | `core/pipeline_engine/workspace_input_orchestration.py` 585-704 (`sandbox.commands.run` at 608, 631, 650, 696; `sandbox: Any`); called from `node_runner.py` 8126-8130 with the live handle | `provision_workspace_inputs_in_sandbox` | Host-side-resolved managed inputs: credential setup + clone commands executed inside the workspace via the SDK handle dispatch hands it | to-be-migrated | Duck-typed (`sandbox: Any`, no import - invisible to a bound-form scanner) but semantically E2B: it operates on the `AsyncSandbox` created at T2; ABC `exec_command` models it (slice R4, alongside the dispatch handle swap) |
| S1 | `core/runtime_provider/hub.py` 165-176; `core/runtime_provider/__init__.py` 474-481 | `RuntimeProviderHub.initialise` case `"e2b"`; `build_hub()` e2b registration | Registers `E2BRuntimeProvider` (env-gated on `MODULO_E2B_API_KEY` via `get_e2b_api_key`) | sanctioned | Hub registration - structural; ADR 040's own example of a sanctioned site |
| S2 | `db/crud/org_deletion.py` 78-132 (import 89, kill 126) | `_abort_org_live_sandboxes` | Best-effort `AsyncSandbox.kill` of every live `runs.sandbox_id` before hard org delete; never blocks the delete | sanctioned | ADR 040 explicitly enumerates "the org-deletion ... kill sites"; cross-context kill where no dispatch/hub handle exists |
| S3 | `core/pipeline_execution.py` 1236-1261 (import 1254, connect/kill 1256-1257) | `_kill_sandbox_best_effort` | Run-watchdog kill of the recorded sandbox by id; never raises | sanctioned | Same kill-site class ADR 040 enumerates ("org-deletion / evidence kill sites"); `destroy_workspace_by_ref` provides a future path but retiring this exemption is an ADR-amending decision, not a PR decision |
| S4 | `core/pipeline_engine/evidence.py` 450-495, wiring 498-536 | `_e2b_run_command`, `_e2b_list_files` | Evidence probes: `AsyncSandbox.connect` + `commands.run` / `files.list` on the run's live sandbox, bounded, `close()` in finally | sanctioned | ADR 040 enumerates the evidence sites; probes run from eval/evidence contexts with only a persisted sandbox id |
| S5 | `core/pipeline_engine/sandbox_policy.py` 311-395 (docstring Constant 357; `sandbox.commands.run` 365) | `apply_sandbox_policy` module internals | The enforcement scripts themselves (git scope -> egress iptables -> read-only seal; critical steps raise, egress best-effort) | sanctioned | The A21 guard's protected host module (rule allows call sites *here* and in `e2b.py`); its engine-side *caller* is T7. Module folds into `E2BRuntimeProvider.apply_isolation` at slice R3/R6 |
| S6 | cross-file: `settings.py` 631-635; `core/cost_controller/finalize.py` 210-233, 930-939, 1047; `breakdown/params.py` 57-73, 500-508; `core/seed_data/cost_components.py` 60; `evidence.py` 646-659 | `e2b_sandbox_usd_per_hour`, `_e2b_rate`, `rate_fallback="e2b_rate"`, `_estimate_probe_cost_usd` | The E2B hourly-rate family used to cost sandbox wall-clock (plus the probe) | sanctioned | Same family as A3; E2B billing rate, contract models no cost mechanism (ADR 040 open defect covers non-E2B reuse, not this site) |
| S7 | `core/graph_validator/__init__.py` 88, 609-629, 2850 | `_check_sandbox_timeout_e2b_cap`, known-good E2B template list | Save-time rejection of `timeout_seconds > 3300` (E2B 1-hour cap); template allowlist | sanctioned | Save-time product validation of an E2B substrate constraint; no SDK binding; dispatch-time parity lives at A1 |
| S8 | `core/bundled_runner/runner_dispatch.py` 8 (docstring Constant), 56, 78, 151-152, 195-206 | `resolve_sandbox_dispatch_route` e2b arm, `validate_e2b_dispatch_timeout`, `_E2B_MAX_TIMEOUT_SECONDS` | Dispatch-route resolution returning `provider_type="e2b"`; loud timeout validation | sanctioned | Route vocabulary + cap validation, no SDK call; the module docstring's `AsyncSandbox.create` mention is an `ast.Constant` the scanner will see - reword it in slice R6 when the scanner activates (both files stay sanctioned hosts) |
| S9 | `api/routes/environment_profiles.py` 474-478 (e2b tuple 475) | `_PROVIDER_TIER_SOURCES` | Registration-adjacent registry mapping provider class path -> egress tier | sanctioned | Same class as S1 (provider registration surface); string module path is outside bound forms |
| S10 | `core/runtime_config/key_bridge.py` 104-116; `store.py` 94-97 | `get_e2b_api_key`, `MODULO_E2B_API_KEY` key config | Runtime-override-over-env credential bridge shared by hub registration, the enforcement gate, and `e2b.py` (FAR-1159/1171) | sanctioned | Credential infrastructure for registration + provider; survives the rewire (the provider keeps consuming it; only T8's engine-side probe goes away) |
| S11 | `core/pipeline_engine/error_codes.py` 136-140, 370, 690-697 (+ node_runner class defs 426-445) | `SANDBOX_TIMEOUT_EXCEEDS_E2B_CAP`, rate-limit guidance, `"RateLimitException"` string-prefix map, `SandboxRateLimitedError`/`SandboxQueueTimeoutError` | Error taxonomy keyed to E2B failure modes; string prefixes in `classify.py` style maps | sanctioned | String tokens / class taxonomy - ADR 040 is additive-only (no re-parenting); the *raise sites* migrate inside T2/T9, the vocabulary stays |
| S12 | `core/pipeline_engine/egress.py` 37; `core/runner_capacity.py` 138, 147, 170, 484; `db/models/environment_profile.py` 14; `core/runner_bindings.py` 99, 125; `core/pipeline_engine/sandbox_mode.py` 164-183 | `"e2b"` tier capability map, `RUNNER_PROVIDER_E2B`, `PROVIDER_TYPES`, remediation strings, capability derivation `tier="e2b"` | Provider-type vocabulary consumed by egress resolution, capacity markers, the CHECK constraint, and binding refusals | sanctioned | String-literal provider tokens - explicitly outside ADR 040's bound forms; permanent while the `e2b` type exists |

### 1.3 Prose/token mentions - not usage sites (outside bound forms; no behaviour)

Comments/docstrings/string descriptions only, no classification. Enumerated so the inventory is exhaustive against grep (a re-run of command 4 must land entirely inside 1.1-1.3):

| File | Lines | Nature |
|---|---|---|
| `core/pipeline_engine/executor.py` | 1093, 1238-1244, 1504, 1752, 1947, 2138-2139, 2596, 2630, 2986, 3093-3096, 3882, 3985, 4049, 4283 | D8 e2b-skip carve-out notes, fallback-echo notes, E2B+DB provider prose |
| `core/bundled_runner/runner_dispatch.py` | 9, 19-20, 129-130, 148, 343, 515-524, 589, 646, 1158-1164, 1212, 1228, 1262, 1358-1361, 1429 | parity/annotation comments (behaviour-bearing lines are S8) |
| `core/pipeline_engine/sandbox_mode.py` | 41, 324, 345, 380 | capability/validation comments (behaviour-bearing tier derivation is S12) |
| `core/pipeline_engine/sandbox_policy.py` | 7, 12, 67 | module-docstring prose (behaviour is S5) |
| `core/pipeline_engine/sandbox_errors.py` | 3 | "no heavy imports (langgraph, e2b, ...)" docstring |
| `core/pipeline_engine/node_runner.py` | 427, 438, 539, 553, 565, 694-695, 753, 5956, 7609, 7663, 7673, 7875-7876, 7905, 8035, 8358, 8668, 8880, 8888, 9060, 9072, 9112, 9379, 9764, 9918, 9925, 9947 | remaining comment/docstring lines (behaviour-bearing clusters are T1-T9, A1-A3) |
| `core/saq_worker.py` | 79 | concurrency-comment prose |
| `core/schema_registry/contract.py` | 8-10 | "E2B-DECOUPLED" docstring |
| `connectors/shell/__init__.py` | 120 | exit-code-optional comment |
| `core/guardrails/sandbox_bridge.py` / `loop_intercept.py` | 3 / 822 | "runs inside the E2B sandbox" docstrings |
| `db/crud/run.py` | 3042, 3181 | capacity-window comments |
| `db/models/run.py` | 300, 314 | dispatch-marker shape + `sandbox_id` column comments |
| `db/crud/variant_group.py` | 243 | model-name comment |
| `db/crud/org_deletion.py` | 53, 79, 83, 120, 131, 258-259 | kill-site prose (behaviour is S2) |
| `core/pipeline_execution.py` | 299, 370, 2194 | fence prose (behaviour is S3) |
| `api/mcp_server.py` / `api/routes/pipelines.py` / `api/routes/runners.py` | 7270 / 787 / 125 | tool-description and comment strings |
| `settings.py` | 459 | comment (behaviour-bearing rate field is S6) |
| migrations `0003`, `0110`, `0178` | - | historical CHECK-vocabulary text |

Two docstring `AsyncSandbox` mentions (S8 line 8; S5 line 357) are `ast.Constant` strings and sit on sanctioned hosts - they are the only Constant-form hits outside `e2b.py` besides node_runner's live annotations (T1).

## 2. The flag and revert point

- **Name:** `MODULO_E2B_VIA_PROVIDER`, **default OFF**. Settings field follows the established pattern: `modulo_e2b_via_provider: bool = Field(default=False, alias="MODULO_E2B_VIA_PROVIDER")` in `backend/src/modulo/settings.py` (same shape as `modulo_workspace_inputs_enabled`).
- **What it gates:** every to-be-migrated site above, at its call site. Each migrated site becomes `if flag: <ABC path via hub-resolved E2BRuntimeProvider> else: <existing direct path>`. With the flag OFF (the default), the ABC path is unreachable and behaviour is byte-for-byte the legacy direct path - the legacy code stays in-tree, unmodified, as the fallback.

  ```python
  # shape of every gated site (R1 shown; R2-R4 identical shape):
  if get_settings().modulo_e2b_via_provider:
      tail = await provider.read_log_tail(sandbox_id, max_bytes=6000)   # ABC path
  else:
      tail = await _fetch_sandbox_log_tail(sandbox_id)                  # legacy direct path
  ```

  The flag is read per call site via `get_settings()` (runtime read, patchable in tests, same pattern as `modulo_workspace_inputs_enabled`) - never captured once at import, so a runtime flip does not require stale-module gymnastics beyond a settings refresh.
- **Revert:** unset / set `MODULO_E2B_VIA_PROVIDER=false` and restart. Every gated site falls back to the direct path in one flip; no data migration, no marker/schema change (marker schema tolerates unknown fields per ADR 040). The legacy path is deleted only in the post-soak retirement slice (R6), never before. Because each slice is gated at the *call site*, reverting slice R3 does not un-ship R1/R2 - the flag is shared, but the legacy branches of already-migrated sites remain independently reachable code until R6; a slice-level rollback in the window before R6 is therefore a code revert of that slice's gate only, not a flag change.
- **Observability (ADR 040 "Flag and revert observability"):** the flag state and the resolved `provider_id` are stamped onto the dispatch marker and node telemetry on both paths, rendered in the run inspector, so any run can be attributed to legacy-vs-provider execution. Promotion budget: >=14 consecutive green days on the provider path before the default flips; <=3 flag flips per 30 days (ADRs' N/M budget, instantiated here as 3).

## 3. Ordered rewire slices (each independently shippable and revertible)

Each slice ships with the flag present and OFF by default (except R5, which flips the default); reverting any slice = flag OFF, and no slice deletes legacy code. Ordering is dependency-driven: primitives land before their call sites move (R1-R3 are dispatch-agnostic capability pairs), the dispatch itself moves once all three primitives exist (R4), the default flips only after the parity soak (R5), and deletion + scanner activation happen last (R6). Each row states what moves, what stays, what unit tests with a fake provider can prove without a live E2B sandbox, and what only CI or a real sandbox can cover.

| # | Slice | Moves | Stays | Verification without a live sandbox | Only CI / real-sandbox can cover |
|---|---|---|---|---|---|
| R1 | **`read_log_tail` primitive + log-probe rewire** (smallest) | Add `read_log_tail(ref, *, max_bytes) -> bytes` to the ABC + `E2BRuntimeProvider` impl (the `api.e2b.app` HTTP call moves *into* `e2b.py`); gate T6's four callers behind the flag | `_combine_log_entries` parsing, pre-kill fetch ordering, key fallback, "never raises / empty on no key" | Unit: `FakeRuntimeProvider.read_log_tail` returns a fixed tail; assert flag-OFF callers still hit urllib, flag-ON callers hit the provider; content-parity test legacy-urllib-fixture vs primitive over the same payload; hostname absent from `node_runner.py` under flag-ON path | Post-destroy retention window (tail readable after kill within window); live-endpoint content parity for a real sandbox id |
| R2 | **File-I/O primitives + file call-site rewire** | Add `read_file`/`write_file`/`list_files`/`get_info` to the ABC (exec-based default bodies per ADR 040) + optional native E2B overrides; gate T4's 13 sites (and T6-adjacent probes) behind the flag | Context/schema/prompt/bridge write paths, drain windowing logic, `output.json` read semantics (timeout threading) | Unit with fake provider: bytes-in/bytes-out round-trip, write-before-command ordering, drain-window equivalence on a scripted log; no `sandbox.files` call reachable in `node_runner.py` when flag ON (assert via monkeypatched handle) | Binary (`.b64`) context file round-trip; seal interaction (writes happen before `apply_isolation` seal on both paths) |
| R3 | **`apply_isolation` + engine-invocation rewire (A21 precursor)** | Add `apply_isolation(spec, policy)` to the ABC; `E2BRuntimeProvider.apply_isolation` wraps the existing `sandbox_policy` scripts (same order: git -> egress -> seal; same user=root; same raise-vs-best-effort split); gate T7 behind the flag | `_should_apply_sandbox_policy` predicate, allowlist pre-resolution, the scripts themselves | Unit: parity test - flag-ON `apply_isolation` and flag-OFF `apply_sandbox_policy` emit the same script sequence, users, and enforce/best-effort flags for a fixed policy; refusal maps to terminal named code (typed error catchability test); **node_runner still imports `apply_sandbox_policy` on the flag-OFF path - A21 must NOT activate yet** | In-sandbox effect: seal actually blocks writes, iptables actually denies, git helper actually scoped (existing sandbox-policy tests + a real-sandbox strip) |
| R4 | **Dispatch rewire - create / stream / kill (flag OFF default)** | When flag ON: `_sandbox_agent_impl`'s legacy branch resolves the E2B provider via `build_hub()` (per-dispatch fresh hub, mirroring the Docker route) and uses `create_workspace(spec)` (T2), `exec_command_stream` (T3 + watchdog re-expressed over `ExecProcess`), `destroy_workspace` (T5), typed-error translation (T8/T9: `RateLimitedError` -> existing rate-limit taxonomy, no re-parenting), provider-owned credential (T8) | The entire legacy direct branch verbatim as the flag-OFF path; dispatch marker fencing; bindings/MWI pre-claim blocks; cost stamping | Unit with `FakeRuntimeProvider`: full `_sandbox_agent_impl` run flag-ON asserting the ABC call sequence, marker written before create, cancellation propagates (CancelledError not swallowed), no zero-exit fabrication on stream error, retry classification unchanged; flag-OFF run asserts zero provider calls | Streaming parity transcript (ADR 040 flip-gate observable): captured legacy-path stream for a fixed workload replayed against the provider path - coalesced output bytes, ordered stdout/stderr segments, failure markers, scripted idle window; rate-limit backoff under a real 429 |
| R5 | **The dispatch flip** | Flip `MODULO_E2B_VIA_PROVIDER` default to ON (settings default or deploy config); soak | Legacy branch still in-tree as the fallback (flag OFF) | Unit: default-value test; marker/telemetry carries flag+provider on both paths | >=14 consecutive green days on the provider path with the parity transcript green; hard date 2026-12-15 triggers recorded escalation only - never a force-flip past red parity (ADR 040) |
| R6 | **Legacy retirement + A21 activation** (post-soak, its own release) | Delete the direct-path branch, the node_runner e2b imports (T1), `_fetch_sandbox_log_tail` (T6), the engine-side `apply_sandbox_policy` import (T7), the flag-OFF arms of T2-T5/T8-T10; activate the A21 guard and the `api.e2b.app` hostname ban; amend the ADR 040 sanctioned list (drop the direct-path / log-probe / sandbox-policy categories; remaining sanctioned: A1-A3 + S1-S12) | Sanctioned sites untouched; buffered non-stream exec route survives (ADR 040 deletion scope: only the legacy *streaming* path dies) | Unit: full node_runner suite flag-irrelevant (flag removed or pinned ON); architecture test asserting no `from e2b` outside `e2b.py`+sanctioned hosts | Scanner green in CI with the anti-vacuity fixture; a real-sandbox smoke of create/stream/kill on the provider path |

Slices R1-R3 are deliberately capability-shaped and dispatch-agnostic: each proves one primitive pair (legacy site <-> ABC method) under the flag before the dispatch itself moves in R4.

## 4. A21-guard note

ADR 040: the A21-guard bound form (banning `apply_sandbox_policy` / `sandbox_policy` call sites outside `sandbox_policy.py` and `core/runtime_provider/e2b.py`) **activates with slice R6 - the legacy-retirement slice - and no earlier.**

Why: the guard scans source text for the import/call bound form, and `node_runner.py` imports `apply_sandbox_policy` today (line 8190). Slices R3-R5 gate the *call* behind the flag but leave the import and the flag-OFF call site in the file - a flag-guarded import is still an import node, so the rule would fire red on every commit from R3 through R5. The rule first becomes green at the moment R6 physically removes the engine-side invocation, i.e. when that path provably routes through `apply_isolation` (the ADR's own invariant: "the engine-side invocation is retired for a path only when that path provably routes through the primitive"). Activating it before then would make CI red; that is exactly the failure ADR 040 records as the reason the guard is end-state.

## 5. Risks - where the rewire changes behaviour, and the test that proves preservation

| Risk | What can change | Proof it is preserved |
|---|---|---|
| **Streaming semantics** | T3's `on_stdout/on_stderr` callback stream becomes `ExecProcess.chunks`; chunk ordering, exit-vs-error distinction, and the no-fabricated-zero rule could drift | R4 unit: fake-provider stream test asserting ordered chunks, `exit_code is None` + `error set` on proxy drop, `done` fires on early consumer close; ADR 040 parity transcript (coalesced bytes, ordered segments, failure markers, scripted idle window) - binding, blocks R5 |
| **Cancellation** | Legacy sites re-raise `CancelledError` everywhere (create, kill, reads, `finally` teardown); a provider wrapper that catches broadly would swallow it; the shielded-slice watchdog (5461-5522) cancels only the shield, never the stream task | R4 unit: CancelledError injected at each provider call, assert propagation (not conversion to `ExecResult(-1)`); watchdog test: slice timeout does not cancel the underlying wait (the `cancelling()==0` regression test from FAR-97/98 stays green) |
| **Sandbox-policy invocation** | T7's step order (git writes before read-only seal), enforcement-critical raise vs egress best-effort, and terminal refusal could be reordered or softened inside `apply_isolation` | R3 parity test (same scripts/users/enforce flags for a fixed policy - fails if order changes); refusal test asserting terminal named code, never retry-loop; existing `sandbox_policy` unit suite runs unchanged against both paths |
| **Log probe** | T6's contract: bounded tail, `""` on no key / any error, fetched *before* kill while the sandbox is live, post-destroy retention | R1 unit: no-key -> `""`, provider exception -> `""` (never raises); ordering assertion that the tail fetch precedes destroy in the flag-ON dispatch trace; content parity over a recorded endpoint payload |
| **Org-deletion / evidence kill sites (S2-S4)** | These are **not migrated**; risk is collateral - a scanner or sweep treating them as violations, or the flag accidentally routing them | Footprint review of R1-R6: S2/S3/S4 lines untouched; they never read the flag (assert in unit: kill paths work with flag ON and OFF); their existing never-blocks-delete / never-raises tests stay green |
| **Retry classification** | T2's `RateLimitException` backoff -> `SandboxQueueTimeoutError` could be reclassified as terminal when translated through `RateLimitedError` (ADR 040: "retry classification does not move") | R4 mapping test: provider `RateLimitedError`/`ProvisionTimeoutError` map to the existing retryable codes; post-claim faults stay terminal (fencing-lease stage-split tests unchanged); no new `harness.unknown` outcomes on flag-ON rate limits |
| **Cost stamping** | Flag-ON path could skip `_compute_sandbox_cost` elapsed-time capture or stamp a different provider into the rate family | Unit: flag ON/OFF produce identical `cost_estimate_usd` for fixed elapsed+output fixtures (A3/S6 untouched by construction - they are sanctioned) |
| **Marker/telemetry attribution** | Wrong provider or missing flag field would make legacy-vs-provider runs indistinguishable (defeats revert observability) | R4/R5 unit: dispatch marker and node telemetry carry `provider` and flag state on both paths; unknown-field tolerance on read asserted (ADR 040 marker schema versioning) |
| **Duck-typed helper drift (T10 workspace inputs)** | `workspace_input_orchestration.provision_workspace_inputs_in_sandbox` takes `sandbox: Any` and calls `sandbox.commands.run` four times (608, 631, 650, 696) with no import - it silently receives whatever handle dispatch hands it; flag-ON dispatch could pass a provider handle the helper cannot use | R4 unit: flag-ON run with workspace inputs configured asserts the helper receives the ABC-mediated handle and its four commands round-trip through the fake provider; flag-OFF run asserts the helper still receives the legacy handle (zero behaviour change) |
| **Evidence/org kill divergence (S2-S4 stay direct)** | Because kills stay direct while dispatch goes through the provider, the provider's in-process `_sandboxes` map and the raw-SDK kill sites can disagree about liveness after the flip | No new test needed for the sites themselves (sanctioned, untouched - footprint review covers it); ADR 040's two-phase `destroy_intent`/`confirmed` marker (a later slice, caller-side) is the designed reconciliation - recorded here so the rewire does not claim kill-path unification it does not deliver |

## 6. Out of scope for this document

- The actual code for R1-R6 (each is its own delivery ticket/slice; this doc is the precondition, not the implementation).
- CI scanner / semgrep rule authoring (including the A21 rule and the `api.e2b.app` hostname ban) - activation slice recorded in section 4, wiring happens in R6's delivery, not here.
- The parity-transcript comparator implementation (ADR 040 rehomes it to the delivery ticket; section 3 states what it must observe).
- Amending ADR 040's sanctioned list - section 1 is the enumeration it promises; shrinking it at R6 is an ADR edit performed by the retirement slice, not a PR-time change.
- Park-destroy, the two-phase destroy marker, identity-scoped enumeration, and the per-provider cost-rate mechanism - open ADR 040 / ADR 051 points, none of which this rewire resolves.
