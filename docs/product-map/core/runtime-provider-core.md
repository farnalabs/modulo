---
id: feat-core-runtime-provider-core
prd: 6
adr: [ADR 044 (agent-dispatch-model), ADR 040 (runtime-provider-errors)]
delivery-tasks: []
code:
  - backend/src/modulo/core/runtime_provider/
  - backend/src/modulo/db/models/environment_profile.py
  - backend/src/modulo/db/crud/environment_profile.py
  - backend/src/modulo/db/crud/run.py
  - backend/src/modulo/api/routes/environment_profiles.py
  - backend/src/modulo/core/graph_validator/__init__.py
  - backend/src/modulo/connectors/shell/__init__.py
  - frontend/src/views/environment-profiles/
  - frontend/src/stores/environmentProfiles.ts
unit-tests:
  - backend/tests/unit/core/runtime_provider/test_abc.py
  - backend/tests/unit/core/runtime_provider/test_hub.py
  - backend/tests/unit/core/runtime_provider/test_default_hub.py
  - backend/tests/unit/core/runtime_provider/test_provider_type_coverage.py
  - backend/tests/unit/core/runtime_provider/test_provider_close.py
  - backend/tests/unit/core/runtime_provider/test_error_family.py
  - backend/tests/unit/core/runtime_provider/test_e2b.py
  - backend/tests/unit/core/runtime_provider/test_e2b_conformance_slice2.py
  - backend/tests/unit/core/runtime_provider/test_e2b_read_log_tail.py
  - backend/tests/unit/core/runtime_provider/test_e2b_apply_isolation.py
  - backend/tests/unit/core/runtime_provider/test_file_io_primitives.py
  - backend/tests/unit/core/runtime_provider/test_local.py
  - backend/tests/unit/core/runtime_provider/test_workspace_network_validation.py
  - backend/tests/unit/core/runtime_provider/test_docker_endpoint_tls.py
  - backend/tests/unit/core/runtime_provider/test_far1128_cap_environments_dispatch.py
  - backend/tests/unit/runtime_provider/test_docker_provider.py
  - backend/tests/unit/pipeline_engine/test_e2b_isolation_provider.py
  - backend/tests/unit/pipeline_engine/test_sandbox_policy.py
  - backend/tests/unit/core/bundled_runner/test_runner_dispatch_node.py
  - backend/tests/unit/graph_validator/test_environment_capabilities.py
  - backend/tests/unit/api/test_environment_profiles_routes.py
  - backend/tests/unit/db/test_run_one_pr_per_run.py
bdd:
  - backend/tests/bdd/features/environments/environment_profiles.feature
  - backend/tests/bdd/features/runtime_providers/provider_matrix.feature
  - backend/tests/bdd/features/runtime_providers/file_io.feature
  - backend/tests/bdd/features/workflows/binding.feature
depends-on: []
status: covered
---

# Runtime Provider Core

Provider abstraction that executes `sandbox_agent` nodes and manages workspaces
(ADR 044 – Agent Dispatch Model). Runtime providers (`local`, `runner_docker` with
`docker`/`local_docker` aliases, `e2b`) expose the same capability surface, gated
per-environment via environment profiles and validated at graph-validation time.
`ShellConnector` (the legacy command connector) is deprecated since ADR 044 and maps
onto the same runtime-provider surface; its product-map entry carries the ADR 044
deprecation notice. The WorkspaceLease scaffolding was removed in FAR-587 (ADR 029)
– workspace state lives in `runs.sandbox_dispatch_state`.

## Behaviours

- [x] Runtime provider base contract (`base.py`): lifecycle, health, execution, teardown
- [x] Provider registry/hub resolves the configured provider deterministically
      (explicit provider_type/hint match; `ProviderNotConfiguredError` otherwise)
- [x] Built-in providers: `local`, `runner_docker` (aliases `docker`, `local_docker`), `e2b`
- [x] Environment profiles CRUD (`/api/v1/environment-profiles`): list, create, get,
      update, delete, restore, and `POST /{id}/test` (SSE sandbox connectivity check) –
      input-validated, org-scoped, gated on the `environment_profiles` feature
- [x] Graph validator rejects pipelines whose nodes need a capability the profile lacks
      (`test_environment_capabilities`)
- [x] `sandbox_agent` node dispatch, crash-resume, and output-handling contracts
      (run model fields: node retry/resume markers)
- [x] ShellConnector is deprecated (ADR 044, 2026-07-16) with a runtime
      `DeprecationWarning` and doc notice; existing ShellConnector pipelines continue
      running, and the node type is marked deprecated in the UI – new pipelines should
      use `sandbox_agent`
- [x] Platform-provider matrix is BDD-exercised against the REAL runtime-provider
      seams network-free and DB-free (`provider_matrix.feature`):
      `build_hub` registers `local` unconditionally and gates `e2b` /
      the docker family on their documented env signals (an unrelated
      `MODULO_RUNNER_*` var never registers Docker – FAR-996); the hub resolves
      deterministically (hint wins, docker-family aliases share one provider,
      known-but-unregistered types raise `ProviderNotConfiguredError` naming the
      remediation env var, unknown types raise `UnknownProviderTypeError` naming
      the valid vocabulary, a missing type is unresolvable); and the factory
      `initialise` loads docker-family configs under their config name, skips e2b
      without an api_key, and rejects unknown types
- [x] ADR 040 additive `RuntimeProviderError` family (FAR-1050 slice 1): typed
      members (`ProviderCapabilityUnsupportedError`, `WorkspaceGoneError`,
      `StreamingUnsupportedError`, `ArtifactTooLargeError`, `RateLimitedError`,
      `SdkMissingError`, `ProvisionTimeoutError`, `UnknownRefError`,
      `BackendUnreachableError`) with an explicit dual hierarchy – the
      pre-existing `ProviderNotConfiguredError` / `UnknownProviderTypeError`
      config tree is deliberately NOT re-parented under the new base, so each
      dispatch catch-site stays reconciled
- [x] `exec_command_stream` (D4 primitive) with a kill handle: async decoded
      chunks, `done` on every stream end, `exit_code` stays `None` until the END
      of a healthy stream (never a fabricated zero exit on a mid-stream
      engine/proxy drop), and the ABC default raises the typed
      `StreamingUnsupportedError` – never a raw `NotImplementedError` (ADR 040
      error-honesty carve-out)
- [x] `destroy_workspace_by_ref` (ADR 040 substrate-level destroy): idempotent on
      already-destroyed / foreign refs (no-op success), confirmed-gone `True` /
      unconfirmed `False` best-effort (never raises), overridable via
      `AsyncSandbox.connect` on E2B, typed `ProviderCapabilityUnsupportedError`
      default
- [x] `read_log_tail` (FAR-1050 R1): bounded tail read (`max_bytes` newest-end
      cap, 4k raw fallback, `b""` on invalid ref / fetch failure – never raises),
      with E2B's legacy `_fetch_sandbox_log_tail` content parity pinned
- [x] `apply_isolation` + the frozen `IsolationPolicy` carrier (FAR-1050 R3): the
      single owner of the three in-sandbox controls – git-credential scoping,
      the selected-mode egress allowlist, and the read-only seal – with
      flag-gated parity to the legacy `sandbox_policy.apply_sandbox_policy`
      (enforcement-critical-raise vs egress-best-effort split) and a typed
      `ProviderCapabilityUnsupportedError` refusal on non-overriding providers;
      the carrier also carries `single_pr_per_run` (FAR-1273), the single
      carrier for the one-PR-per-run guard trigger
- [x] Run-scoped one-PR-per-run `gh` guard (FAR-1264; trigger made explicit by
      FAR-1273): when the node carries `single_pr_per_run: true`, the engine
      threads the flag on the typed `IsolationPolicy` (the single carrier) to
      `apply_sandbox_policy`, which installs a `gh` shim that permits exactly
      ONE `gh pr create` per sandbox run for the `gh` binaries it managed to
      guard (a bounded, best-effort defence-in-depth layer behind the
      prompt-level one-PR-per-run rule, FAR-1254 – explicitly not a guarantee
      on its own); every other `gh`
      invocation passes through untouched, and
      a create that FAILS (non-zero exit) releases its claim so a transient
      failure does not burn the run's only attempt. **Coverage is bounded, not
      absolute:** the guard intercepts only `gh pr create` resolved through the
      sandbox PATH at install time (and absolute paths to those same binaries);
      `gh api` PR creation, a `gh` copy outside the PATH, shell aliases/functions,
      and a `gh` installed into the PATH AFTER the install are NOT intercepted.
      **Tier coverage (FAR-1315) – honest boundary:** `e2b` installs the guard
      through `apply_isolation` → `apply_sandbox_policy` and is the ONE tier
      where the platform guard is actually in force today. `runner_docker`
      (the Bundled Runner) runs the SAME install at dispatch through the
      provider `exec_command` primitive
      (`sandbox_policy.install_gh_pr_guard_via_exec`) – but the shipped
      first-party runner image (`deploy/docker/runner-opencode.Dockerfile`)
      ships NO `gh` and runs `ReadonlyRootfs: true` as uid 1001 with no
      writable PATH dir, so on that image the install always resolves to
      `absent`/`failed` and the tier is prompt-level-guarded only: the
      exec-install machinery is BEST-EFFORT and is **not effective on the
      shipped image today**, becoming effective only on an image that provides
      a `gh` in a PATH directory the workspace user can write. Every
      absence/failure there (no `gh` on PATH, non-writable PATH directory, exec
      failure, failed claim pre-plant) is surfaced by a LOUD
      `runner_dispatch.gh_pr_guard_unavailable` warning naming the tier and
      status, never silently; `local` / `local_docker` are
      dispatch-unbound and never execute sandbox nodes, so no dispatchable tier
      is left uncovered-but-unmentioned. A missing `gh` or a failed install
      degrades to the prompt-level guard (both are logged). The install is
      BEST-EFFORT – a failure is logged and the
      run degrades to the prompt-level guard, never wedges the dispatch (unlike
      the enforcement-critical steps); the gate
      `_should_apply_sandbox_policy(..., single_pr_per_run=...)` runs the
      policy step for flag-only nodes, and a flag-only invocation failure is
      swallowed at the T7 call site while enforcement-control nodes keep the
      fail-closed tier refusal. The pre-FAR-1273 trigger (any non-empty
      `delivery_sentinel` threading through
      `WorkspaceSpec.workspace_metadata[DELIVERY_SENTINEL_SPEC_KEY]`) is gone:
      `delivery_sentinel` keeps only its FAR-228 idempotency meaning and a
      sentinel-only node now gets NO guard, and the metadata constant itself was
      deleted. **Migration required:** existing sentinel-only pipelines (the
      live Prompt-to-PR nodes among them) must be migrated to the explicit flag
      – set `single_pr_per_run: true` on each such node; until then no shim is
      installed for them, and every dispatch logs the
      `sandbox_agent.single_pr_per_run_flag_missing` warning (node id +
      pipeline id) so the disarm stays observable. Unit-covered in
      `tests/unit/pipeline_engine/test_sandbox_policy.py` (shim executed
      end-to-end under `sh`: first create passes, a failed first create
      releases its claim for a retry, second refused; install idempotency
      re-writes a stale run scope) and
      `tests/unit/pipeline_engine/test_e2b_isolation_provider.py` (gating
      predicate, the sentinel-only-gets-no-guard regression, and call-site
      routing/best-effort), with the e2b call site's own policy-flag read
      pinned in `tests/unit/core/runtime_provider/test_e2b_apply_isolation.py`
- [x] The one-PR claim is RUN-scoped across MULTIPLE flagged nodes (FAR-1315):
      the FAR-1264 marker lives inside ONE sandbox, so two flagged nodes in one
      run previously allowed one PR per node (one claimable marker each). The
      engine now keeps a process-local run CLAIM LEDGER keyed by the run id
      (`sandbox_policy.acquire_run_pr_guard` / `settle_run_pr_guard`,
      lock-guarded): the FIRST flagged node of a run installs the live guard; a
      later or concurrent flagged node is DENIED and installs a PRE-PLANTED
      refusal (its marker directory already exists, so its first
      `gh pr create` is refused without calling gh), with the denial logged
      loudly (`sandbox_policy.gh_guard_run_claim_denied` – claim status, scope,
      owner). **Spend evidence (re-gate hardened):** the dispatch `finally`
      settles the node's slot from platform-observed evidence, in this order –
      (1) the HARVESTED claim RECEIPT, a file the shim writes inside the marker
      dir only when `gh pr create` exited 0 (`harvest_gh_pr_claim_bounded`,
      one bounded, cancellation-safe exec probe run after the node while the
      sandbox/container is still alive: the probe task is always
      awaited-or-cancelled, never left running against a container the teardown
      is about to destroy); (2) ONLY when the receipt is a definitive
      `False` – the probe ran against a LIVE install
      (`guard_install_status` ∈ `installed`/`pre_planted`, threaded from the
      install step itself) – is it decisive: then the agent-authored `pr_url`
      and the stdout claim sentinel are BOTH ignored and the hold is RELEASED
      (a definitive "the live shim ran, no create succeeded" outranks every
      agent-authored signal); (3) when the receipt is UNKNOWN – the harvest
      could not run (sandbox already destroyed, exec failed, cancelled), or
      the probe ran against a path NO SHIM EVER WROTE (`absent`/`failed`/
      unthreaded install, e.g. the shipped runner image has no `gh`, so the
      probe answers ABSENT against a non-existent path) – the settle falls
      back to a URL-valid `pr_url` **corroborated by the platform's own
      capture** (the same URL must also appear in the captured transcript –
      raw `output.json` text alone, validated for URL syntax only, never
      spends) and then to the stdout claim SENTINEL as the final fallback.
      Receipt confirmed ABSENT against a LIVE shim → `pr_url` and the sentinel
      are IGNORED and the owner's hold is RELEASED – reading the shim
      (`cat $(command -v gh)`, a fixed literal) can therefore never spend the
      run, while a node that never created a PR never burns the run's attempt.
      `guard_owner` (the node id) rides the typed `IsolationPolicy` as the
      ledger owner, so a denied node can never release another node's hold,
      while a node retry re-claims its own slot. **Cancellation-safe
      teardown:** the harvest is the first await in the dispatch `finally`, so
      an `asyncio.CancelledError` landing there is RECORDED (receipt →
      unknown), the teardown runs UNCONDITIONALLY (E2B: bridge close, streaming
      writer cleanup, `destroy_workspace_by_ref`, provider `close`,
      fenced dispatch-marker clear; runner: `destroy_workspace`, `close`,
      hub `aclose`, marker clear), and the cancellation is re-raised only
      afterwards – a cancel during the harvest can no longer leak a sandbox,
      strand a dispatch marker or leave the ledger hold settling-less.
      **Run-id canonicalisation:** the marker/receipt PATH and the ledger key
      both canonicalise a UUID-shaped run scope first (braced / uppercase /
      `urn:uuid:` / hex-less forms all map to ONE path), so the install side
      (which threads `str(spec.run_id)`) and the harvest/settle side (which
      threads the raw run id) can never disagree about which path to probe.
      **What is shared across nodes is the LEDGER, never the sandbox
      filesystem** – the marker itself stays per-sandbox. **Bounded (and what
      is still residual):**
      - the ledger is process-local (all nodes of a run execute in the engine
        process; a run resumed in a DIFFERENT process starts with an empty
        ledger) and capped at `_MAX_RUN_PR_GUARD_CLAIMS` (512) run scopes,
        evicting the oldest entries – an evicted run degrades to "entry
        forgotten" (its later flagged nodes can claim again), never affects a
        run still in flight under the cap;
      - a failure before output capture releases the slot rather than
        stranding it, and a CONCURRENT second flagged node is conservatively
        denied while the slot is held even if the holder never claims;
      - **SCOPE CALL (deliberately stated, not implied): adversarial evasion
        is OUT OF SCOPE.** The marker/receipt live under `/tmp` and the shim
        (mode 755, PATH-resolvable) prints its exact path, so a DELIBERATELY
        malicious agent can pre-create the marker/receipt (false SPEND: later
        flagged nodes are refused and the run delivers nothing) or DELETE it
        (false RELEASE). **Marker/receipt deletion now also neutralises the
        sentinel arm**: the harvest answers ABSENT against the deleted path
        and, when the install was live, that definitive ABSENT suppresses the
        stdout sentinel and `pr_url` as well – so a hostile deletion both
        releases this node's hold and silences the fallback channel that would
        otherwise have spent the run. No heuristic is designed to defeat a
        hostile agent; hardening against deliberate evasion is deliberately
        not attempted here and stays tracked separately. The ACCIDENTAL model
        is what must stay correct, and is what the tests pin: a normal create
        → receipt `True` → spent; a truncated stream → harvested receipt →
        spent; merely reading the shim → no create → receipt `False` (live
        install) → released, never spent;
      - KNOWN RESIDUAL: when the receipt harvest cannot run AND the sentinel
        was itself lost from the captured stream (a node that created the PR
        and then emitted more than the 512 KB drain window before its sandbox
        died), the settle sees no evidence and releases – a later flagged node
        could then open a second PR. The receipt/corroborated-`pr_url` arms
        exist precisely to shrink this window to "sandbox gone AND no
        `output.json`+transcript delivery";
      - KNOWN RESIDUAL: in the same harvest-unavailable window, a node that
        merely PRINTED the sentinel (by reading the shim) can still spend the
        run (fail-closed: later flagged nodes are refused, zero PRs). Both
        residuals are the sentinel-fallback channel only; both are documented
        here deliberately rather than silently wrong. Unit-covered in
      `tests/unit/pipeline_engine/test_sandbox_policy.py` (ledger state
      machine + bound, the two-node pre-planted refusal through the real
      `apply_sandbox_policy` path, the shim sentinel AND success receipt
      executed under `sh`, the settle's receipt/`pr_url`/sentinel decision
      table INCLUDING the re-gate cases – definitive `receipt=False`
      outranking an agent-authored `pr_url`, an uncorroborated `pr_url` never
      spending, and an absent/failed install leaving the receipt meaningless –
      the bounded+cancellation-safe `harvest_gh_pr_claim_bounded` wrapper
      (timeout drains the probe; cancellation re-raises only after the probe
      is drained), the run-id canonicalisation across every UUID form, the
      real-probe harvest parse, a real-shell install reporting `absent` on a
      gh-less PATH – the shipped runner image's shape – and the
      exec installer's installed/pre_planted/absent/failed outcomes),
      `tests/unit/pipeline_engine/test_e2b_isolation_provider.py`
      (the REAL `guard_owner` wiring at the `_sandbox_agent_impl` call site,
      plus the end-to-end observation channel per spend arm: shim-produced
      stdout → dispatch capture → REAL settle spends; harvested receipt spends
      a sentinel-less stream; a shim-read transcript with a confirmed-absent
      receipt from a LIVE install does NOT spend; a failing flagged node
      releases its hold; a URL-shaped `pr_url` never outranks a definitive
      `receipt=False`; an `absent` install leaves the receipt meaningless so
      the sentinel still spends; and a cancellation landing DURING the harvest
      still destroys the sandbox, closes the provider and clears the dispatch
      marker before the CancelledError propagates), and
      `tests/unit/core/bundled_runner/test_runner_dispatch_node.py`
      (runner_docker flagged install + hold release, the same three spend-arm
      channels through the REAL settle including the FAILURE-path release, the
      loud tier-named absence warning, the unflagged control, and the
      runner-tier twins of the `pr_url`-precedence, absent-install and
      cancel-during-harvest findings), with
      `guard_owner` crossing `apply_isolation` into the ledger pinned in
      `tests/unit/core/runtime_provider/test_e2b_apply_isolation.py`
- [x] One-PR-per-run: POST-RUN detection + an admin alert outside the sandbox
      (FAR-1274) – this DETECTS and alerts after the fact; it does **NOT
      prevent** a second PR. It is a stopgap until a preventive,
      platform-mediated PR-create path exists (outstanding, XL/design).
      Every terminal write that funnels through `db.crud.run` (the
      `update_run_status` ORM + fenced writers and `request_cancellation`) runs
      `_enforce_one_pr_per_run`, which is **armed ONLY for runs whose frozen
      pipeline snapshot declares the FAR-1273 `single_pr_per_run` node flag** –
      multi-PR-by-design pipelines (and runs whose snapshot cannot be read)
      stay silent, so the detector honours each run's own declared contract.
      For an armed run it re-scans the run's **platform-captured**
      delivery evidence – the stored blobs' strings, i.e. the persisted
      transcript (`agent_stdout` / `agent_stderr` / `sandbox_log_tail`, marker
      `raw_output`) plus the agent-declared `pr_url` fields – for distinct
      GitHub pull-request URLs: URLs are **normalised before dedup**
      (scheme-insensitive, lowercase host/path, trailing punctuation stripped –
      variants of one PR never count twice), `gh pr list --json` listing lines
      (other open PRs a pre-check echoes) are skipped, and both collection
      (≤50 URLs) and the rendered list (first 10 + "and N more") are bounded so
      the alert body and log line stay bounded. Two or more distinct URLs
      breach the contract and are recorded LOUDLY: an `error`-level,
      admin-scoped in-app notification (category `run.duplicate_pr_delivery`,
      deep-linked to the run; admin-scoped means READABLE BY ORG ADMINS ONLY –
      the visibility clause requires a live `admin` membership) written in a
      **SAVEPOINT of the same transaction** as the terminal status – a failed
      alert rolls back only itself, never the terminal status, and a
      rolled-back terminalization leaves no phantom alert – plus an
      `error`-level `delivery_contract.duplicate_pr` log line. The alert is
      idempotent per run (a re-terminalization does not stack a second row).
      Detection deliberately does **not** use the delivery sentinel: FAR-1254's
      second `gh pr create` did not re-echo it, so sentinel counting (and the
      boolean `delivery_done` stamp) cannot see the second PR, while `gh`'s own
      stdout echo of the created URL is captured by the platform and needs no
      agent cooperation. **Bounded, not absolute** (extends the FAR-1264
      bound above): detection reads only RETAINED evidence, so a PR whose URL
      never reached captured output or a declared field (created outside the
      sandbox, transcript truncated past the retention cap without its
      `stdout_artifact`, output suppressed) is not detected; conversely a run
      that merely *references* two PR URLs in its output is flagged for
      review (the alert is worded as a suspected breach, not a verdict); a
      `gh pr list --jq`-style listing flattened to bare URL lines still counts;
      and terminalizers that write `status` via raw SQL (the cron/SAQ failure
      sweeps) bypass this hook exactly as they bypass the FAR-189 inline
      classify hook. The in-sandbox FAR-1264 `gh` shim stays as **defence in
      depth, bounded**. Unit-covered in
      `tests/unit/db/test_run_one_pr_per_run.py` (the FAR-1254 shape caught
      from the transcript alone; single-PR happy path silent; multi-PR-by-design
      pipeline without the flag silent; unreadable snapshot fails safe to
      silent; `gh pr list` pre-check noise silent; URL-variant dedup; collection
      and rendered-list bounds; the production write shape – blobs carried by
      `update_run_status` itself, nothing pre-seeded; DB-level alert INSERT
      failure still commits the terminal status; same-transaction rollback drops
      the alert; re-terminalization idempotency; a failed blob read never blocks
      the terminal write) and `tests/unit/db/test_notification_preferences.py`
      (admin-scope rows readable by org admins only)
- [x] File-I/O primitives (FAR-1050 R2a): `read_file` / `write_file` /
      `list_files` / `get_info` (+ the frozen `WorkspaceFileInfo` value object)
      on the ABC – exec-based binary-safe defaults (base64 over the text exec
      channel, shlex-quoted paths, `mkdir -p` parent creation, sorted full
      child paths, `stat` parsing, 30s per-command bound, typed
      `RuntimeProviderError` on a non-zero exit) with E2B native `sandbox.files`
      SDK overrides, all unit-covered and BDD-exercised against the REAL
      `LocalRuntimeProvider` exec backend (`file_io.feature`)

## Known Gaps

- **E2B provider is V3-deferred / environment-dependent** – runs only where the E2B
  integration is configured.

## QA History

- 2026-09-25: **product-map walk** – walked the FAR-1050 runtime-provider
  primitive series into this entry: the ADR 040 `RuntimeProviderError` family
  (slice 1), `exec_command_stream` + `destroy_workspace_by_ref` (slice 2),
  `read_log_tail` (R1), `apply_isolation` + `IsolationPolicy` (R3), and the
  file-I/O primitives `read_file` / `write_file` / `list_files` / `get_info` +
  `WorkspaceFileInfo` (R2a) were shipped without behaviour-tracker lines or
  citation updates; added the behaviour lines, the missing unit-test citations,
  the ADR 040 reference, and new executing BDD coverage
  (`runtime_providers/file_io.feature`, steps in
  `features/runtime_providers/test_file_io_steps.py`) driving the REAL
  `LocalRuntimeProvider` exec-based defaults. Status: covered.
- 2026-09-22: **product-map walk** – closed the "No BDD coverage for the
  platform-provider matrix" gap (`provider_matrix.feature`, steps in
  `features/runtime_providers/test_provider_matrix_steps.py`), driving the REAL
  `build_hub` / `RuntimeProviderHub.resolve` / factory `initialise` seams
  network-free and DB-free (real `LocalRuntimeProvider` / `DockerRuntimeProvider`
  / `E2BRuntimeProvider(api_key=...)` constructors, which open no connections):
  the env-gated registration matrix (local always; e2b / docker family gated on
  their documented signals; unrelated `MODULO_RUNNER_*` never registers Docker,
  FAR-996), the deterministic resolve matrix (hint-wins, docker-family aliases →
  one provider, known-but-unregistered → `ProviderNotConfiguredError` naming the
  remediation env var, unknown → `UnknownProviderTypeError` naming the valid
  vocabulary, missing type → unresolvable), and the config-driven `initialise`
  (docker-family aliases under a config name, e2b skipped without an api_key,
  unknown config types rejected). 13 scenarios execute in CI.
- 2026-09-02: **FAR-551** – collapsed the duplicate `/admin/environments` UI +
  `environments.py` router into `/environment-profiles`; ported the `/test`
  connectivity check; added the missing API feature-gate.
- 2026-08-25: **product-map review pass** – restored this entry as part of
  rebuilding the `docs/product-map/` feature graph. This entry is the one ADR 044
  requires to carry the ShellConnector deprecation notice
  (ADR 044 (agent-dispatch-model)). Re-verified the runtime_provider package
  layout, environment-profile CRUD routes, workspace-lease model, and ShellConnector
  deprecation notice against the current tree. Status: covered.
