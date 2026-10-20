# Lock-point inventory (FAR-1801)

> Audit deliverable for the FAR-41 "concurrency / lock audit" ask. This document is an
> **inventory and assessment only** – it changes no production code. The follow-up fixes
> are listed as proposed tickets at the end and are deliberately *not* created here.

- **Scope:** every row-level lock taken in `backend/src/modulo` (PostgreSQL `SELECT … FOR
  UPDATE` and its SQLAlchemy forms), across both the API/route layer and the background
  engine.
- **Repo-relative paths** throughout. Line numbers are against the worktree tip at time of
  authoring (see `git rev-parse HEAD` in the PR).
- **Placement note:** this repo already carries a single high-level `docs/architecture.md`,
  so this deep-dive reference lives beside it in `docs/architecture/`.

---

## 1. Method (reproducible enumeration)

> **FAR-1825 re-derivation (this branch).** §1's commands were re-run against the
> branch tip, not trusted from the previous snapshot: **58** `.with_for_update(`
> + **3** `with_for_update=` = **61** SQLAlchemy call sites (+2 raw SQL = **63**
> row-lock sites). The +2 since the FAR-1833 cutover are **new sites landed by
> other work**, neither category C: `api/mcp_server.py::_replace_pipeline_graph_txn`
> (A19; self-bounds with `set_mutation_row_lock_timeout` before its first lock) and
> `core/cron_helpers.py::_fail_node_deadline_run` (D15; `session.get(Run,
> with_for_update=True)`, bounded by the reconcile org transaction's bound at
> `cron_helpers.py:8988`). Category C itself is unchanged at **20** sites.

Counts below are **measured**, not estimated. The two enumeration commands are:

```bash
# (1) SQLAlchemy call sites – the lint-safe way to skip doc/comment prose,
#     because prose never writes a leading dot or an `=`:
grep -rn '\.with_for_update(' backend/src/modulo --include='*.py' | wc -l   # 58
grep -rn 'with_for_update='  backend/src/modulo --include='*.py' | wc -l   #  3  (session.get kwargs)
# total SQLAlchemy row-lock call sites: 61

# (2) raw SQL `FOR UPDATE` occurrences. NOTE: the filter is LOOSE – it also
#     matches docstring/prose mentions of `FOR UPDATE`, so do not read the raw
#     hit count as the locking-site count (see the note below the command):
grep -rn 'FOR UPDATE' backend/src/modulo --include='*.py' \
  | grep -E '"[^"]*FOR UPDATE|sql_text\(' | grep -v '^\s*#'
# 5 hits (measured 2026-10-16): 2 LOCKING DML sites (core/runner_capacity.py,
# core/pipeline_engine/node_runner.py), 1 non-locking DDL clause (§4), and
# 2 docstring/prose mentions (db/crud/account.py, core/trigger_engine/__init__.py)
```

Cross-check: a full `grep -rn 'with_for_update' backend/src/modulo --include='*.py'`
returns **69** hits; the 8-hit difference is prose (docstrings/comments), which the
`\.` / `=` patterns in command (1) exclude. The per-site inventory in §2 was generated
by a one-off script that additionally resolves the enclosing `def`/`async def` for each
hit; the raw `file:line` set is exactly reproducible from command (1).

**Headline counts** (re-measured on the FAR-1825 branch; see §10)

| Form | Measured |
|---|---|
| SQLAlchemy `.with_for_update(...)` | 58 |
| SQLAlchemy `session.get(..., with_for_update=True)` | 3 |
| **SQLAlchemy row-lock call sites (total)** | **61** |
| Raw SQL `FOR UPDATE` in locking DML | 2 |
| **Total row-lock sites** | **63** |
| Non-locking `FOR UPDATE` occurrence (RLS policy DDL) | 1 |

> **Count corrections (FAR-1822 + FAR-1832).** The original audit recorded **63**
> `.with_for_update(` sites. FAR-1822 (PR #1842, merged 2026-10-15) removed the
> `cron_helpers` scheduled-report lock (D7) after the audit was written, taking it to
> **62**; **FAR-1832** (Land 1b of FAR-1823) then removed four more – the
> sandbox-concurrency and HITL-window writers (old C2/C3), the product-analytics
> consent read (old C1) and the org-runtime-flags writer (old D14) are now
> single-statement atomic settings patches with no `SELECT … FOR UPDATE` pre-lock –
> giving the **58** `.with_for_update(` sites that main carried before this branch.

> **FAR-1833 cutover (this branch).** The org spend-ceiling gate (former D9,
> `core/cost_controller/finalize.py`) no longer takes an SQLAlchemy row lock – it
> takes a per-org **advisory** lock (`pg_advisory_xact_lock`, not a row lock) and the
> ceiling/cumulative state moved to `organisation_cost_state`. It is therefore removed
> from this *row-lock* inventory: a further **-1**, to the **57** `.with_for_update(`
> sites (**59** SQLAlchemy call sites once the 2 `session.get` kwargs are included,
> **61** total row-lock sites with the 2 raw SQL sites) measured above. See §10.

---

## 2. Categories

Each site is assigned **exactly one** category:

| Tag | Category | Meaning |
|---|---|---|
| **A** | pipeline/trigger serialisation | A lock whose invariant is the single-writer correctness of a **pipeline, trigger, run, pipeline-snapshot or variant-group** row (graph mutation, run status transitions, dispatch/claim fences, snapshot allocation, name uniqueness). |
| **B** | credential rotation | A lock on a **token, key, OAuth code/family, stored secret, one-time setup token, or identity/sequence rotation anchor** – the read-check-write of a credential lifecycle. |
| **C** | entity CRUD | A lock serialising the create/update/delete of an ordinary entity row (**Agent, parameter schema/set, model backend, SSO provider, invitation, node, account preferences, org settings/config, lifecycle map, assistant context source, agent-runner binding**). |
| **D** | operational/other | Everything else: audit chain, cost ledger & daily counters, org deletion, error-group aggregation, runner capacity, HITL claim, runtime-config flags, scheduled reports. |

---

## 3. SQLAlchemy `.with_for_update` inventory (61 sites)

`lock` = the effective PostgreSQL strength (`with_for_update()` → `FOR UPDATE`;
`key_share=True` → `FOR NO KEY UPDATE`; `read=True` → `FOR SHARE`). `txn` = transaction the
lock is taken in. "Over-serialises" is a judgement: **no** means the critical section is the
minimum needed for the stated invariant; **watch** flags a lock that also blocks an
unrelated writer or is held across non-lock work.

### A – pipeline/trigger serialisation (19)

| # | file:line | function | lock | txn | hold / blocking scope | over-serialises |
|---|---|---|---|---|---|---|
| A1 | `api/mcp_server.py:4692` (`bind_connector_to_node`, defined at 4652) | `bind_connector_to_node` | FOR UPDATE | caller txn | Pipeline row locked while the MCP graph/connector-binding write commits. | no – pipeline single-writer. |
| A2 | `api/routes/triggers.py:379` | `_require_team_gate_in_txn` | FOR UPDATE | mutation txn, bounded by `set_mutation_row_lock_timeout` | TOCTOU re-check of the pipeline team gate; blocks pipeline graph writers for the RMW. | no. |
| A3 | `api/routes/pipelines.py:3400` | `_reapply_team_gate_inside_mutation_txn` | FOR UPDATE | mutation txn, bounded | Same team-gate TOCTOU re-check (the route-side twin of A2). | no. |
| A4 | `db/crud/pipeline_snapshot.py:441` | `_acquire_snapshot_allocation_lock` | FOR NO KEY UPDATE (`key_share=True`) | caller txn, bounded by `set_mutation_row_lock_timeout` | Snapshot-allocation slot; deliberately the weakest key-preserving lock so it does not conflict with FK-only writers. Timeout → `SnapshotLockNotAvailableError` (retryable 503). | no. |
| A5 | `db/crud/run.py:2469` | `coalesce_pending_run` | FOR UPDATE **SKIP LOCKED** | caller txn | Non-blocking claim of a pending run to coalesce; never waits. | no. |
| A6 | `db/crud/run.py:3600` | `update_run_status` | FOR UPDATE | caller txn (executor/cron; fenced variant used on the hot path) | Read-then-write of run status; serialises against the park sweep and concurrent terminalisers. | no – but see §7 run-row hotspot. |
| A7 | `db/crud/run.py:3970` | `request_cancellation` | FOR UPDATE | caller txn (**now bounded** – both callers issue `set_mutation_row_lock_timeout` first, FAR-1610) | Sets cancel-wins fields on the run row. Bounding is deliberately the caller's job: `api/routes/runs.py:_cancel_run` and `api/mcp_server.py:_cancel_run_impl` each call `set_mutation_row_lock_timeout` **before** the first lock, so a contended row yields a `55P03` the caller maps to a visible 409 (REST) or a branchable `lock_timeout` result (MCP), never an unbounded wait. | no – gap closed (FAR-1610). |
| A8 | `db/crud/variant_group.py:158` | `increment_run_count` | FOR UPDATE | caller txn | Serialises the run-count increment for an A/B variant group. | no. |
| A9 | `db/crud/variant_group.py:218` | `_lock_variant_group` | FOR UPDATE | caller txn | Shared variant-group lock used by the A/B assignment path. | no. |
| A10 | `db/crud/pipeline_snapshot_versioning.py:281` | `rollback_to_snapshot` | FOR UPDATE | caller txn | Pipeline row held across version rollback (snapshot pointer/version bump). | watch – held across snapshot version work; see §7. |
| A11 | `db/crud/pipeline.py:429` | `check_pipeline_name_available` | FOR UPDATE | caller txn (clone/create), bounded by the clone endpoint's `set_mutation_row_lock_timeout` | Locks the target-name row so two concurrent creates cannot both pass the uniqueness check. | no. |
| A12 | `db/crud/pipeline.py:1415` | `_read_clone_source_snapshot` | **FOR SHARE** (`read=True`) | separate connection, bounded | Shared lock on the clone *source* pipeline; permits concurrent readers, blocks the source graph writer. | no. |
| A13 | `db/crud/pipeline.py:2191` | `replace_pipeline_graph` | FOR UPDATE | caller txn | Pipeline row held across the *entire* graph replace: HITL-gate guard, edge/node snapshot, delete+insert of all edges/nodes, and an inline `pipeline.graph_updated` audit append. | **watch** – long critical section (see §7). |
| A14 | `db/crud/run_node_outputs.py:1014` | `_read_run_markers_fenced_rows` | FOR UPDATE **OF runs** (`of=Run`) | caller txn | Scoped run-row lock (not the joined output rows) for the fenced marker read. | no. |
| A15 | `core/pipeline_engine/node_runner.py:2414` | `_write_raw_output_marker` | FOR UPDATE | caller txn | Run-row lock for the raw-output marker write; the counterpart of A16. | no. |
| A16 | `core/pipeline_engine/recovery.py:431` | `_fetch_locked_run` | FOR UPDATE | caller txn, `include_soft_deleted` | Locks the *pipeline* row to single-flight run recovery; re-reads the run after the lock. | no. |
| A17 | `core/pipeline_engine/recovery.py:712` | `guardrail_override` | FOR UPDATE | caller txn, `include_soft_deleted` | Pipeline lock so a guardrail override cannot race a graph edit. | no. |
| A18 | `core/pipeline_engine/classify.py:1383` | `_reconcile_classify_run` | FOR UPDATE + `populate_existing` | **own** `session.begin()` | Per-run re-read to terminalise/classify exactly once; each DB call bounded by `asyncio.wait_for(_CLASSIFICATION_WRITE_TIMEOUT_SECONDS)`. | no. |
| A19 | `api/mcp_server.py:4170` | `_replace_pipeline_graph_txn` | FOR UPDATE | own `_session` txn, **bounded** by `set_mutation_row_lock_timeout` (FAR-1361) | MCP graph-replace transaction takes the pipeline row lock first (FAR-1845 order); the bound is issued before the first lock. | no. |

> Note: A18 and the raw-SQL site R2 (§4) both lock the `runs` row; A15 and R2 are the two
> halves of the same connector idempotency-gate fence (write vs read).

### B – credential rotation (10)

| # | file:line | function | lock | txn | hold / blocking scope | over-serialises |
|---|---|---|---|---|---|---|
| B1 | `auth/api_key.py:505` | `revoke_api_key` | FOR UPDATE | caller txn | Serialises concurrent revocations of the same key (second waits, sees revoked). | no. |
| B2 | `auth/oauth.py:673` | `consume_authorization_code` | FOR UPDATE | caller txn | Single-use OAuth code exchange; prevents double-consume. | no. |
| B3 | `auth/oauth.py:897` | `_get_token_family` | FOR UPDATE | caller txn | Token-family row lock for refresh-token rotation/reuse detection. | no. |
| B4 | `db/crud/token_family.py:24` | `get_or_create_family` | FOR UPDATE | caller txn | Family create/read RMW. | no. |
| B5 | `db/crud/token_family.py:90` | `advance_sequence` | FOR UPDATE | caller txn | Monotonic sequence advance; serialises concurrent refreshes. | no. |
| B6 | `db/crud/token_family.py:148` | `blacklist_family` | FOR UPDATE | caller txn | Family revocation. | no. |
| B7 | `core/secrets_backend/fernet.py:192` | `set_secret` | FOR UPDATE | **`begin_nested()` savepoint** | Secret upsert RMW; bounded by `asyncio.wait_for(_DB_TIMEOUT)`, retried once. | no. |
| B8 | `core/mcp_setup_handoff/__init__.py:93` | `consume_handoff` | FOR UPDATE | caller txn | One-time MCP setup-token consume (single-use). | no. |
| B9 | `api/routes/api_keys.py:307` | `_enforce_user_key_quota` | FOR UPDATE (`session.get`) | caller txn | Locks the **Account** row to serialise the per-user API-key mint quota (read-count-mint). | **watch** – shares the Account row with the preferences lock C8, so unrelated account writes serialise against key minting. |
| B10 | `api/routes/product_analytics_identity.py:359` | `_lock_rotation_anchor` | FOR UPDATE | caller txn | Locks the per-instance identity row as the rotation critical-section anchor; **fails closed** if the anchor row is missing (a no-row `FOR UPDATE` takes no lock). Rotatable identity/sequence state, not a secret – grouped here as the closest rotation bucket. | no. |

### C – entity CRUD (20)

> **FAR-1832 removed C1, C2 and C3** (analytics-consent read, sandbox-concurrency
> writer, HITL-window writer): each is now a single-statement atomic settings patch
> (`update_org_settings_patch`) with no `SELECT … FOR UPDATE` pre-lock – see §10
> finding 3. Row IDs are kept gap-stable for cross-references.
>
> **FAR-1825 swept this category.** Every C site whose lock a request path can
> reach now issues the transaction-scoped `set_mutation_row_lock_timeout` before
> its first lock (column `txn`). Three site-groups are deliberately left as-is,
> with the reason recorded on the row: C5/C6 are already bounded one frame up by
> the shared org-patch helper, and C10/C11/C21 have no production caller at all.
> The structural guard is
> `backend/tests/unit/api/test_far1825_lock_timeout_sweep.py`.

| # | file:line | function | lock | txn | hold / blocking scope | over-serialises |
|---|---|---|---|---|---|---|
| C4 | `api/routes/model_backends.py:667` | `create_model_backend_endpoint` | FOR UPDATE | mutation txn, **bounded** (FAR-1825: `set_mutation_row_lock_timeout` at the top of the route transaction) | Locks existing same-name `ModelBackend` row for creation uniqueness (409 on clash). | no. |
| C5 | `db/crud/organisation.py:107` | `get_organisation` | FOR UPDATE (opt-in) | caller txn, **bounded** by `_take_settings_patch_lock_wait` in `update_org_json_column_patch` | Generic org row lock used by org-updating callers. Marks itself FAR-1624: intentionally plain `FOR UPDATE`, not `FOR NO KEY UPDATE`. Since FAR-1830/1832 its only caller is the atomic settings patch's **generic-dialect (SQLite/MariaDB) fallback** (`_apply_json_column_patch_generic`) – on Postgres the patch is a single `UPDATE` and never takes this. | **watch** – Organisation row (Postgres never reaches it). |
| C6 | `db/crud/organisation.py:718` (`_apply_json_column_patch_generic`; entered via `db/crud/observability.py::update_otel_config` → shared machinery) | `_apply_json_column_patch_generic` | FOR UPDATE (generic-dialect fallback only) | caller txn, **bounded** by `_take_settings_patch_lock_wait` (the shared patch bounds before it dispatches here) | **FAR-1832 (QA F3):** routes through the shared atomic JSON-column patch (`update_org_json_column_patch`) – the Postgres path is a single-statement atomic `otel_config_json = COALESCE(…) || :patch` write with no pre-lock; the `FOR UPDATE` survives only on the SQLite/MariaDB fallback branch (`_apply_json_column_patch_generic`, which never runs on Postgres). Deliberately `require_live_row=False` (still `include_soft_deleted` on the generic read) – FAR-1025 keeps OTel config writable during the pending-deletion window (unlike `settings_json`, whose writers now refuse soft-deleted orgs). QA F3 also gave this path the same fail-closed tenant guard as the settings writer (it previously trusted `org_id` verbatim – and `organisations` is not RLS-scoped, so that guard is the only tenant control). | no – never reached on Postgres. |
| C7 | `db/crud/parameter_set.py:91` | `update_set` | FOR UPDATE | caller txn, **bounded** (FAR-1825, in the helper before the lock) | Parameter-set version bump RMW. | no. |
| C8 | `db/crud/account.py:137` | `_get_account_locked` | FOR UPDATE (`session.get`) | caller txn, **bounded** (FAR-1825, in the helper before the lock) | The account-preferences serialisation point (documented): all preference writers serialise here. | **watch** – Account row shared with key quota B9. |
| C9 | `db/crud/node_observation.py:42` | `observe_node` | FOR UPDATE | caller txn, **bounded** (FAR-1825, in the helper before the lock) | Idempotent node-observation upsert (insert if absent). | no. |
| C10 | `db/crud/node_composite.py:27` | `set_parent_node` | FOR UPDATE | caller txn, **deliberately unbounded** | Locks the node being re-parented. No production call site (only unit tests import it, verified by the FAR-1825 guard), so no request path can reach it. | no. |
| C11 | `db/crud/node_composite.py:33` | `set_parent_node` | FOR UPDATE | caller txn, **deliberately unbounded** (same reason as C10) | Locks the (new) parent node in the same txn – cycle/self-parent guard. | no. |
| C12 | `db/crud/sso_provider.py:207` | `create_provider` | FOR UPDATE | caller txn, **bounded** (FAR-1825, in the helper before the lock) | SSO-provider name uniqueness per org. | no. |
| C13 | `db/crud/sso_provider.py:323` | `update_provider` | FOR UPDATE | caller txn, **bounded** (FAR-1825, at helper entry, before the conditional name lock) | SSO-provider update RMW (credential-bearing entity). | no. |
| C14 | `db/crud/parameter_schema.py:115` | `update_schema` | FOR UPDATE | caller txn, **bounded** (FAR-1825, in the helper before the lock) | Parameter-schema version bump RMW. | no. |
| C15 | `db/crud/agent.py:168` | `add_prompt_version` | FOR UPDATE | caller txn, **bounded** (FAR-1825, in the helper before the lock) | Agent prompt-version append RMW. | no. |
| C16 | `db/crud/agent.py:240` | `rollback_prompt_version` | FOR UPDATE | caller txn, **bounded** (FAR-1825, in the helper before the lock) | Agent prompt-version rollback. | no. |
| C17 | `db/crud/invitations.py:100` | `get_live_for_email` | FOR UPDATE | caller txn, **bounded** (FAR-1825, in the helper before the lock) | Invitation consume/lookup RMW (documented: caller MUST hold and consume the row). Reached by the pre-auth SSO JIT login; the bound turns a contended row into 55P03 (login failure) rather than an unbounded pre-auth wait. | no. |
| C18 | `db/crud/invitations.py:139` | `get_valid_by_token_hash` | FOR UPDATE | caller txn, **bounded** (FAR-1825, in the helper before the lock) | Invitation consume by token hash (single-use); first lock of the accept-invite transaction. | no. |
| C19 | `db/crud/agent_runner_binding.py:119` | `replace_agent_bindings` | FOR UPDATE | caller txn, **bounded** (FAR-1825, in the helper before the lock) | Serialises concurrent binding saves on the Agent row before delete-all + insert. Both the save route and the workflow-import path are covered. | no. |
| C20 | `core/lifecycle_map/service.py:94` | `get_lifecycle_map_for_update` | FOR UPDATE + `populate_existing` | caller txn, **bounded** (FAR-1825, in the helper before the lock) | Lifecycle-map version bump; prevents two savers both writing version N+1. Covers the update, save-version and update-version routes. | no. |
| C21 | `core/workflow_import_export/__init__.py:160` | `_get_existing_names` | FOR UPDATE (opt-in) | caller txn, **deliberately unbounded** | Locks existing entity names during import for deterministic name reconciliation. The lock is behind `for_update=True`, and **no production caller passes it** (both public wrappers default to `False`; verified by the FAR-1825 guard), so no lock is taken today. Enabling it means sweeping it. | watch – scanning a name set under lock; hold grows with import size. |
| C22 | `core/assistant/context_source_service.py:138` | `_upsert_context_source` | FOR UPDATE | caller txn, **bounded** (FAR-1825, in the helper before the branch) | Org-scoped assistant-context-source upsert. | no. |
| C23 | `core/assistant/context_source_service.py:148` | `_upsert_context_source` | FOR UPDATE | caller txn, **bounded** (same FAR-1825 bound as C22 – one statement precedes both branches) | User-scoped assistant-context-source upsert (per-user variant of C22). | no. |

### D – operational / other (12)

> **D7, D9 and D14 have been removed.** **FAR-1822** removed D7 (the `cron_helpers`
> scheduled-report lock – PR #1842 merged 2026-10-15; the stamp-and-commit path now
> lives in `reports/scheduler.py` with a phase-1 short lock). **FAR-1832** removed D14
> (the org-runtime-flags writer is now an atomic settings patch). **FAR-1833** removes
> D9 (the org spend-ceiling gate moved off the `organisations` row onto
> `organisation_cost_state` under a per-org advisory lock – an advisory lock is not a
> row lock). Row IDs stay gap-stable for cross-references.

| # | file:line | function | lock | txn | hold / blocking scope | over-serialises |
|---|---|---|---|---|---|---|
| D1 | `db/crud/org_deletion.py:271` | `request_org_deletion` | FOR UPDATE | caller txn, `include_soft_deleted` | Org lifecycle FSM (FAR-1624 marker). | watch – Organisation row. |
| D2 | `db/crud/org_deletion.py:327` | `confirm_org_deletion` | FOR UPDATE | caller txn, `include_soft_deleted` | Org lifecycle FSM (FAR-1624 marker). | watch – Organisation row. |
| D3 | `db/crud/org_deletion.py:388` | `cancel_org_deletion` | FOR UPDATE | caller txn, `include_soft_deleted` | Org lifecycle FSM (FAR-1624 marker). | watch – Organisation row. |
| D4 | `db/crud/org_deletion.py:418` | `export_org_data` | FOR UPDATE | caller txn, `include_soft_deleted` | Org lifecycle FSM (FAR-1624 marker). | watch – Organisation row, and an export may run long under it. |
| D5 | `db/crud/error_tracking.py:86` | `upsert_error_group` | FOR UPDATE **OF error_groups** (`of=ErrorGroup`) | caller txn | Error-group aggregation upsert; `of=` avoids locking the nullable side of the `sample_event` outer join. | no. |
| D6 | `db/crud/daily_run_count.py:54` | `upsert_daily_run_count` | FOR UPDATE | caller txn | Daily run-count upsert (non-critical convenience path; `cost_controller` is the serialised one). | no. |
| D8 | `core/reports/scheduler.py:410` | `_fire_scheduled_report` | FOR UPDATE | **own** `session.begin()` | Scheduled-report fire **after FAR-1822 (PR #1842, merged 2026-10-15)**: the lock is now the SHORT phase-1 read+idempotency-snapshot section only – report generation and delivery run outside it (the lock-held-across-generation shape this audit flagged as risk #1 is closed). | no – short critical section (§7 risk #1 closed). |
| D10 | `core/cost_controller/finalize.py:1705` | `_ledger_block` | FOR UPDATE | caller txn | Run-row lock for the duplicate-terminal ledger guard (`ledger_written OR ledger_refused_at`) + savepoint retries. | watch – run-row hotspot. |
| D11 | `core/cost_controller/__init__.py:270` | `get_or_create_daily_count` | FOR UPDATE | caller txn | Daily-count row RMW with `begin_nested()` IntegrityError re-read. | no. |
| D12 | `core/audit_logger/__init__.py:337` | `_get_chain_head_locked` | FOR UPDATE | caller txn | Per-org audit-chain head; prevents forked appends. | watch – held for the append; A13 appends an audit event inside its pipeline lock, stacking two locks (see §7). |
| D13 | `core/hitl_manager/__init__.py:588` | `_assert_team_member_for_claim` | FOR UPDATE | caller txn | Locks the HITL gate row so the team check is serialised with the claim UPDATE. | no. |
| D15 | `core/cron_helpers.py:5846` | `_fail_node_deadline_run` | FOR UPDATE (`session.get`) | caller txn, **bounded** by the reconcile org transaction (`cron_helpers.py:8988`, FAR-1601) | FAR-1999 node-deadline reaper: locks the run row, re-checks every premise on the locked row, then terminal-fails it. Background reconciliation only (no request path). | no – single-run fence. |

> **D9 is retired (FAR-1833).** The org spend-ceiling gate
> (`core/cost_controller/finalize.py::_apply_spend_ceiling_gate`) no longer takes
> an SQLAlchemy row lock: it serialises on the per-org cost-state advisory lock
> (`modulo.core.runner_capacity.lock_org_cost_state`, a `pg_advisory_xact_lock`)
> and reads/writes `organisation_cost_state` instead of the `organisations` row.
> The D-numbering is retained so the other rows keep their §5/§6/§7 references;
> there is no D9 in the post-cutover inventory.

---

## 4. Raw SQL `FOR UPDATE` occurrences

| # | file:line | enclosing function | category | context | assessment |
|---|---|---|---|---|---|
| R1 | `core/runner_capacity.py:500` | `acquire_runner_dispatch_slot` (the `"FOR UPDATE"` fragment of the own-row `SELECT claim_count … FOR UPDATE`) | **D** | `async with session_factory() as session, session.begin()`; **bounded** by a transaction-scoped `SET LOCAL lock_timeout` set unconditionally *before* the lock (FAR-1601), then an optional per-org `pg_advisory_xact_lock`, then a claim-token-fenced marker `UPDATE`. | Well-bounded claim-token fence on the hot `runs` row. The lock is deliberate and the wait is capped in both feature-flag states. **no** over-serialisation. |
| R2 | `core/pipeline_engine/node_runner.py:2780` | `_read_connector_idempotency_gate_state` (raw `SELECT id, idempotency_key FROM runs … FOR UPDATE`) | **A** | `async with session_factory() as session, session.begin()`; **bounded client-side** by `asyncio.wait_for(_read(), _IDEMPOTENCY_GATE_READ_TIMEOUT)` (3 s), failing **open** to `(None, None)` with a WARNING. | Correctly bounded and fail-open so a contended run-row lock never blocks a connector write. The docstring is explicit that the residual concurrent-double-write window is *intentional* (a `write_started` lease would fail-closed and block legitimate recovery). **no** over-serialisation, but note as an accepted semantic risk. |

### Non-locking occurrence (not counted)

| file:line | context |
|---|---|
| `db/migrations/versions/0115_notification_preferences.py:143` | `f"FOR UPDATE USING ({_USER_SCOPE}) WITH CHECK ({_USER_SCOPE})"` – this is the `FOR UPDATE` clause of a PostgreSQL **RLS policy** definition (`CREATE POLICY … FOR UPDATE`), not a row-locking read. Listed for completeness so the grep count is fully reconciled; it takes no row lock at runtime. |

---

## 5. Summary – counts per category

| Category | SQLAlchemy sites | Raw SQL sites | **Total** |
|---|---|---|---|
| A – pipeline/trigger serialisation | 19 | 1 (R2) | **20** |
| B – credential rotation | 10 | 0 | **10** |
| C – entity CRUD | 20 | 0 | **20** |
| D – operational/other | 12 | 1 (R1) | **13** |
| **Total** | **61** | **2** | **63** |

These are the §3 counts as they stand on this branch: the FAR-1801 authoring snapshot
with D7 retired by FAR-1822, C1/C2/C3/D14 removed by FAR-1832, D9 retired by
FAR-1833, and the two post-cutover sites added by other work (A19
`_replace_pipeline_graph_txn`, D15 `_fail_node_deadline_run`). FAR-1825 re-ran
§1's commands against this branch and reads the same **61** SQLAlchemy sites, so
the inventory and the measurement agree; see §9 and §10.

Distinct **locked tables** (by frequency): `runs` (heaviest – A5, A6, A7, A14, A15, A18, R2, D10, R1), `organisations` (C5, C6-generic-fallback, D1–D4), `pipelines` (A1–A4, A10–A13, A16, A17), `accounts` (B9, C8), `token_families` (B4–B6), `ScheduledReport` (D8), plus
single-use rows for credentials/entities.

---

## 6. Lock-strength and bounding notes

- **Most `FOR UPDATE` sites run in a caller-supplied transaction.** Sites that open their
  own transaction inside the helper are A18, D8 and the two raw sites R1/R2;
  B7 takes its lock inside a nested savepoint (`begin_nested`). Every other site inherits
  whatever transaction the caller passes. Caller-transaction locks inherit whatever bound the
  caller set, so a site is only as bounded as its *least* careful caller.
- **The bounding mechanism is `db.crud.row_lock.set_mutation_row_lock_timeout`** – a
  transaction-scoped `SET LOCAL lock_timeout` (Postgres only; a no-op on the SQLite test
  fixtures). It must be called *before* the first lock in a transaction (FAR-1313). Route
  mutation transactions, the two team-gate helpers (A2, A3) and the two
  `request_cancellation` callers (A7) all call it; FAR-1610 closed A7, the hold-out this
  inventory originally flagged. **FAR-1825 closed the category-C sweep:** every C site a
  request path can reach now issues the bound itself (C4 at the route's transaction top,
  C7–C9/C12–C20/C22–C23 at the CRUD helper's first lock), while C5/C6 inherit the shared
  org-patch helper's bound. C10/C11/C21 are deliberately unbounded because no production
  request path reaches them (see §3 C and §10).
- **Weak-strength locks are already used where conflict width matters:** FOR NO KEY UPDATE
  for snapshot allocation (A4), FOR SHARE for the clone
  source read (A12), `SKIP LOCKED` for run coalescing (A5), and scoped `OF <table>` locks
  (A14, D5). These are all deliberate and correct. (The org spend-ceiling gate's former
  FOR NO KEY UPDATE – D9 – is retired; it is now an advisory lock, see §10.)
- **`FOR UPDATE` on a row that does not exist takes no lock** – B10 explicitly fails closed
  for this reason (a missing anchor row must not silently degrade to an unserialised
  rotation).

---

## 7. Top serialisation risks

### 1. Scheduled-report generation runs *under* the row lock (D7, D8) – CLOSED by FAR-1822
Both `core/cron_helpers.py:fire_report_trigger` and `core/reports/scheduler.py:_fire_scheduled_report`
took `FOR UPDATE` on the `ScheduledReport` row and then called `generator(session, org_id, config)`
– the potentially heavy report-generation body – **inside the same transaction**, committing
only after it completed. Consequences: (a) the lock was held for the whole generation window,
not just the RMW; (b) two code paths implemented the same fire-serialisation.

**FAR-1822 ("stamp-and-commit") supersedes this and closes risk #1 – MERGED as PR #1842
(2026-10-15, `c64a99c68`).** It reconciles the two fire paths into one shared core that holds
the `ScheduledReport` row lock only for two short critical sections (the read/snapshot before
generation and the terminal stamp after it) and **releases the lock before generation/delivery
runs**, with a Redis lease fencing concurrent fires. The `cron_helpers` lock (old D7) is gone
entirely; D8 is now the single, short-lock site (`reports/scheduler.py:410`).

### 2. The `organisations` row is a cross-feature mutex – NARROWED by FAR-1830/1832, cost path removed by FAR-1833

*Originally eleven distinct writers locked the same organisation row for unrelated concerns
(sandbox concurrency, HITL window, analytics consent, OTEL config, runtime flags, licence,
retention/email/logo/cost whole-column writers, org deletion/export, generic org update, and
the spend-ceiling gate). Any two serialised even when they shared no data; D9 was forced to
weaken to `FOR NO KEY UPDATE` because plain `FOR UPDATE` deadlocked – a symptom of the
over-broad locking.*

**Status after FAR-1830 (unlocked writers, PR #1850) + FAR-1832 (locked writers):** every
`settings_json` concern now writes through the single-statement atomic patch
(`UPDATE … settings_json = COALESCE(…) || :patch`), which takes the row lock only for its own
statement instead of pre-locking across a read-modify-write. The converted sites – sandbox
concurrency, HITL window, analytics consent ×2, runtime flags, licence set/clear, plus the
already-converted retention/email/logo/cost/run-concurrency writers – no longer appear in the
inventory at all. The product-analytics writers (QA F1) use the patch's nested sub-field form
(`jsonb_set(…, '{product_analytics}', COALESCE(…) || :subpatch)`), still one statement and
still no pre-lock: two writers of DIFFERENT sub-fields of the block both survive without
serialising. What still locks the `organisations` row:

- **D1–D4 (deletion FSM) – intentional and preserved.** Deletion must keep exclusive
  `FOR UPDATE` so a hard delete still blocks concurrent child inserts (`FOR KEY SHARE`); a
  statement-level config patch still conflicts with it, so deletion exclusivity survives
  (proven by `tests/integration/crud/test_org_settings_patch.py`:
  a config patch wait-bounds against an in-flight deletion; a child FK insert still queues).
- **C5 / C6** – generic-dialect (SQLite/MariaDB) fallbacks only; Postgres never reaches them.
- **~~D9 (spend-ceiling gate, `FOR NO KEY UPDATE`)~~ – REMOVED by FAR-1833 (Land 2).** The
  ceiling/cumulative state moved off the org row onto `organisation_cost_state` and the row
  lock became a per-org advisory lock, so D9 is retired from §3/§5. The finalize path is not
  entirely org-row-free – the auto-pause write (`organisations.triggers_paused`) and the
  implicit FK `FOR KEY SHARE` on child inserts remain – but they are now taken in a fixed
  advisory → org-row order that cannot form the old ABBA cycle; see §10.

The mutex is narrower but not gone, and the residual *hold-time* watch item is D4 (org export
can run long under its FSM lock).

### 3. `replace_pipeline_graph` holds the pipeline row across the whole replace + an audit append (A13)
`db/crud/pipeline.py:2191` locks the `Pipeline` row and then performs the HITL-gate guard, a
full before-image snapshot of edges and nodes, delete-all + insert-all of edges/nodes, and an
inline `pipeline.graph_updated` audit event – all before the transaction commits. That one
critical section is the longest pipeline-row hold in the inventory: it blocks graph writers
(A1), the team-gate re-checks (A2, A3), snapshot allocation (A4) and rollback (A10). The
inline audit append (D12: `_get_chain_head_locked`) also stacks a second row lock
(per-org audit-chain head) inside the pipeline lock, so the ordering/lock-count of this
path deserves care.

**Honourable mention – the `runs` row is the hottest contested row.** Run status updates
(A6), cancellation (A7), the marker fence (A14/A15/R2), classification (A18), the cost ledger
(D10) and dispatch capacity (R1) all serialise on one `runs` row per run. Per-run this is
fine (each run is independent), but it means any long-held run-row lock directly stalls a
run's terminal transition. A7 was the one unbounded hold-out here; FAR-1610 bounded it at
both callers, which leaves D10's `_ledger_block` as the remaining candidate to re-check on
any future change to terminalisation ordering.

---

## 8. Proposed follow-up fix tickets (NOT created – text only)

These are proposals for separate delivery; one per problem area. Each is intentionally one
line of scope. Item 1 has since landed (FAR-1610) and is retained only as the record of the
original finding.

1. **~~Bound `request_cancellation`'s row-lock wait at its callers~~ – LANDED (FAR-1610).**
   `api/routes/runs._cancel_run` and `api/mcp_server._cancel_run_impl` now issue
   `set_mutation_row_lock_timeout` before the first lock, and a `55P03` maps to a retryable
   / branchable response, never an unbounded wait. Verified by
   `backend/tests/unit/api/test_far1610_cancel_run_lock_bound.py` and
   `backend/tests/unit/mcp/test_mcp_run_tools.py`. *(Category A.)*
   **Residual – now FIXED by FAR-1828:** the docstrings of `db/crud/run.py` (notably
   `request_cancellation`) described the pre-FAR-1610 gap ("Verified as of FAR-1601 neither
   caller issues it"); the code was correct and only the comments were stale. FAR-1828
   corrected them.
2. **Take scheduled-report generation out from under the row lock** – in both
   `cron_helpers.fire_report_trigger` and `reports/scheduler._fire_scheduled_report`, stamp
   the fire (advance `next_send_at` / set a firing marker) and **commit before** running the
   generator, so the lock covers only the RMW; reconcile the two duplicate implementations.
   *(Category D.)*
   **DELIVERED – FAR-1822 (PR #1842, merged 2026-10-15): see §7 risk #1.**
3. **Narrow the organisation-row mutex to per-concern rows / locks** – move
   `settings_json` sub-configs (sandbox concurrency, OTEL, analytics consent, runtime flags)
   off a single `Organisation` row lock, or give each concern its own row/`key_share`-style
   lock, so unrelated org writers stop serialising; preserve the deletion FSM's exclusive
   hold. *(Categories C + D.)*
   **DELIVERED in three lands – FAR-1830 (unlocked whole-column writers, PR #1850), FAR-1832
   (the locked RMW writers: sandbox/HITL/consent/OTEL/runtime-flags/licence now
   single-statement atomic patches, `FOR UPDATE` pre-locks removed; deletion FSM exclusive
   hold preserved – see §7 risk #2), and FAR-1833 (Land 2: the D9 spend-ceiling half moved
   off the org row onto `organisation_cost_state` under a per-org advisory lock, so D9 is
   retired – see §10).**
4. **Shrink `replace_pipeline_graph`'s critical section** – move the `pipeline.graph_updated`
   audit append and any non-lock work outside the pipeline-row lock (or explicitly document
   why the full-replace must hold it), and assert the lock-ordering against the audit-chain
   head (D12). *(Category A.)*
5. **Sweep the caller-transaction CRUD locks for missing bounds** – audit the entity-CRUD
   sites (C-series) whose callers may hold the transaction across slow work, and add
   `set_mutation_row_lock_timeout` at the transaction top where the first lock is unbounded.
   *(Categories C + B.)*
   **DELIVERED for category C – FAR-1825.** Re-derived the C set from §1 against the
   branch tip, bounded every reachable site (C4 route-level; C7–C9, C12–C20, C22–C23 at the
   CRUD helper; C5/C6 already bounded by the shared org-patch helper), and recorded C10/C11/
   C21 as deliberately unbounded (no production caller). Pinned by
   `backend/tests/unit/api/test_far1825_lock_timeout_sweep.py`. Category B is unchanged by
   this sweep and its residual is the WHOLE B series, not one row: B1–B8 (the
   `auth/**` API-key / OAuth-code / token-family credentials, `db/crud/token_family.py`'s
   three locks and `core/mcp_setup_handoff`'s one-time-token consume) also take
   caller-transaction locks with **no** bound, and B9 (`_enforce_user_key_quota`, the
   Account-row key-mint quota) shares the Account row with C8. A B-series sweep is a
   separate delivery.

---

## 9. Caveats

- This is a **static** audit: it enumerates and reasons about lock sites from source. It does
  **not** instrument runtime lock waits or measure contention empirically; the
  "over-serialises" column is a code-reading judgement, not a profile. A follow-up
  runtime-contention measurement (e.g. `pg_locks`/`pg_stat_activity` sampling under load)
  would confirm which of the "watch" sites actually bite.
- Counts are point-in-time. Re-run the two commands in §1 before relying on them.
- The category assignment is a single-bucket choice for readability; some sites are
  legitimately cross-cutting (e.g. D8 is both operational and "trigger-like"; B10 is a
  rotation anchor rather than a secret). The bucket chosen is noted per site.

---

## 10. Corrections (FAR-1828 + FAR-1832 + FAR-1833)

An audit is a **hypothesis**, not ground truth. This one was **falsified within days of
merging**, so treat the inventory above as a starting point to re-verify – not as a record of
shipped behaviour. The corrections:

- **A7 / §6 / §7 (run-row hotspot)** – the claim that `request_cancellation`'s `SELECT … FOR
  UPDATE` was unbounded "at its callers" was **already false when the audit was written**:
  FAR-1610 (commit `310f9e48e`, 2026-10-11 – four days earlier) has both callers
  (`api/routes/runs._cancel_run`, `api/mcp_server._cancel_run_impl`) issue
  `set_mutation_row_lock_timeout` before the shared-CRUD lock and map a `55P03` to a visible
  error (409 / a branchable `lock_timeout`), rather than an unbounded wait.
- **D7 / D8 / §7 risk #1 / §8 item 2** – the lock-held-across-generation shape is superseded by
  FAR-1822 ("stamp-and-commit"), which reconciles the two fire paths into one shared core and
  runs report generation outside the `ScheduledReport` row lock. **FAR-1822 closes risk #1.**
  (MERGED as PR #1842, `c64a99c68`, 2026-10-15 – the `cron_helpers` lock (old D7) was removed
  outright; D8 retains only the phase-1 short lock.)
- **`backend/src/modulo/db/crud/run.py` docstrings** – four docstrings (`request_cancellation`,
  `update_run_status`, `unpark_parked_run`, `transition_run`) repeated a stale FAR-1601
  "neither caller issues the bound" claim. All four are bounded **at their callers** by
  FAR-1610; the docstrings now say so.

### New findings (surfaced by the FAR-1823 design pass – findings, fixes tracked per land)

1. **Five UNLOCKED `settings_json` read-modify-write writers.** Each reads
   `Organisation.settings_json`, merges its own key, and writes the whole column back through
   the *lockless* `db/crud/organisation.update_organisation`, so each can silently clobber a
   concurrent writer's keys: `admin_update_email_settings` (`api/routes/admin_email.py`),
   `admin_update_org` (`api/routes/admin.py`, `logo_url`), `costs._apply_cost_control_updates`
   (`api/routes/costs.py`), the `run_concurrency_limit` writer (`api/routes/admin.py:4500`), and
   `core/seed_data/demo_data.py`. This was live silent data loss on `main`, not a future risk.
   **FIXED – FAR-1830 (Land 1a, PR #1850):** all five write through the atomic
   `update_org_settings_patch`, and `update_organisation` now REFUSES a `settings_json` key
   (FAR-1832 added the guard) so the dumper shape cannot return.
2. **`confirm_org_deletion` used a mixed lock order.** The handler appended the org-independent
   system-audit event (`api/routes/admin.py`) *before*
   `db.crud.org_deletion.confirm_org_deletion` took the `organisations` row `FOR UPDATE`
   (`db/crud/org_deletion.py:327`) – the one FSM site where an in-transaction write preceded
   the org-row lock (every sibling route locked the org row first). *(Mechanism correction,
   FAR-1832: `system_audit_events` is deliberately NOT hash-chained – see
   `db/models/system_audit_event.py` – so there was no per-org audit-chain-head lock on this
   path; the defect was the lock-order inversion itself against global order R6.)*
   **FIXED – FAR-1832:** the append now runs after the crud call, same transaction (atomicity
   and the fail-closed append property unchanged), pinned by
   `backend/tests/unit/api/test_far1832_lock_order.py`.
3. **Measured counts moved (FAR-1822 + FAR-1832 + FAR-1833).** Re-measuring §1 before this
   branch gives 58 `.with_for_update(` sites (60 SQLAlchemy call sites total once the 2
   `session.get` kwargs are included, 62 total row-lock sites with the 2 raw SQL sites) – the
   audit's original 63 was already stale by one when FAR-1822 (PR #1842) removed the
   `cron_helpers` lock, and FAR-1832 removed four more (old C1, C2, C3, D14 – the locked
   settings writers are now single-statement atomic patches). **FAR-1833 retires D9** (the
   spend-ceiling gate's row lock becomes a per-org advisory lock), giving the **57**
   `.with_for_update(` sites measured on this branch – **59** SQLAlchemy call sites, **61**
   total row-lock sites. §3/§5/§7 carry the post-change shape; removed row IDs are left as
   gaps so cross-references stay stable.

### Correction (FAR-1833) – D9 retired: the org spend-ceiling gate is no longer a row lock

The org spend-ceiling gate (`core/cost_controller/finalize.py::_apply_spend_ceiling_gate`)
took an SQLAlchemy `with_for_update(key_share=True)` (FOR NO KEY UPDATE) on the
`organisations` row (inventory row **D9**). Its coupling with the ceiling set/clear
endpoints' org-row write was the D9 deadlock the audit's §7 risk #2 flagged.

FAR-1833 moved the per-run ceiling, spend ceiling and cumulative total off the
`organisations` row onto the dedicated `organisation_cost_state` row and replaced the
row lock with a per-org **advisory** lock (`modulo.core.runner_capacity.lock_org_cost_state`
→ `pg_advisory_xact_lock`). An advisory lock is transaction-scoped and is NOT a row lock, so
it does not belong in this row-lock inventory; D9 is retired from §3/§5 and the row-lock
counts drop by one. On the finalize path the gate takes NO *cost-column* read or write on the
`organisations` row – it reads the row only for existence, and the ceiling/cumulative values
come from `organisation_cost_state`. Migration `0321` deliberately LEAVES the three physical
`organisations` cost columns in place (the expand half of an expand/contract cutover, so the
rolling deploy never serves old code against a dropped column); a follow-up migration drops
them once this release is live.

**Correction (FAR-1833 P3-6):** the earlier claim that "the finalize path now takes NO lock on
the `organisations` row" was too strong. The finalize path is NOT org-row-free:

- the auto-pause path (`_ceiling_auto_pause_if_org_crossing` → `maybe_auto_pause_org_triggers`)
  still WRITES `organisations.triggers_paused` / `triggers_paused_at` whenever the org's
  "Auto-stop on budget exceeded" toggle is ON and a run crosses the org ceiling; and
- every FK-carrying child-row insert (the ledger/audit rows) takes an implicit **FOR KEY
  SHARE** on the referenced `organisations` tuple.

What FAR-1833 actually enforces is **ordering**, not the absence of org-row access: the cost
advisory is acquired BEFORE any `organisations`-row mutation, on both the finalize path
(advisory first, then the auto-pause write) and the admin ceiling endpoint
(`lock_org_cost_state` precedes `_cost_controls_patch`, which sets `org.daily_spend_limit`).
Both take the pair in the SAME order (advisory → org row), so they cannot form the ABBA cycle
that was D9. The residual coupling is therefore a bounded one-way wait, never a deadlock.

### FAR-1825 – category-C lock-bound sweep (re-derived, not trusted from this doc)

This document's own §9 caveat applies: an inventory is a hypothesis. FAR-1825 therefore
re-ran §1's two commands against the branch tip before changing anything:

| Form | Before sweep (this branch) |
|---|---|
| `.with_for_update(` | **58** |
| `session.get(..., with_for_update=True)` | **3** |
| SQLAlchemy call sites | **61** |
| raw locking `FOR UPDATE` | **2** |
| **total** | **63** |

The +2 since the FAR-1833 snapshot are sites added by unrelated work, both outside
category C: **A19** `api/mcp_server.py::_replace_pipeline_graph_txn` (already self-bounds
with `set_mutation_row_lock_timeout`, FAR-1361) and **D15**
`core/cron_helpers.py::_fail_node_deadline_run` (bounded by the reconcile org transaction
at `cron_helpers.py:8988`). Category C remains exactly **20** sites.

**Disposition of every category-C site** (the `txn` column in §3 carries the per-row note):

- **Bounded by this sweep, at the request transaction's first lock:**
  C4 (route top), C7, C8, C9, C12, C13, C14, C15, C16, C17, C18, C19, C20, C22, C23.
  Each now issues `set_mutation_row_lock_timeout(session)` before its first
  `SELECT … FOR UPDATE`. The helper is transaction-scoped (`SET LOCAL lock_timeout`,
  Postgres-only), so a contended row raises SQLSTATE 55P03 — never an unbounded wait.
  **The mapping that turns that 55P03 into a visible answer differs per surface and is
  recorded in "Mapping truth" below.**
- **Already bounded one frame up:** C5 and C6. Both are reached only through the shared
  org JSON-column patch (`update_org_json_column_patch`), which calls
  `_take_settings_patch_lock_wait` before dispatching to `_apply_json_column_patch_generic`;
  the Postgres path never takes a row lock at all. No second bound was added.
- **Deliberately left unbounded, with reason:** C10/C11 (`set_parent_node`) have **no
  production call site** — only `tests/unit/db/test_node_composite.py` — so no request
  path can reach the lock. C21 (`_get_existing_names`) takes its lock only behind
  `for_update=True`, and neither public wrapper (`get_existing_pipeline_names`,
  `get_existing_agent_names`) passes it, so the lock is never taken today. Enabling either
  path must sweep it in the same change; the FAR-1825 guard fails if that happens
  silently.

**Structural guard.** `backend/tests/unit/api/test_far1825_lock_timeout_sweep.py` is an
**AST** scanner (not a token scanner): it re-derives the C site counts per file, asserts
every swept function issues an awaited `set_mutation_row_lock_timeout(...)` *before* its
first row-lock call (a comment/docstring mention cannot satisfy it), pins the
caller-bounded C5/C6 shape, asserts each ENUMERATED route arm classifies 55P03, and
asserts the no-caller sites stay unreachable (alias-aware). It also carries a
**repo-wide file-set assertion**: the set of files under `backend/src/modulo` that take a
row-lock call must equal the known inventory allowlist, so a NEW file with an unbounded
lock fails CI instead of being invisible. Adding a row-lock call means updating that
allowlist and this inventory in the same change.

The mapping half of that guard is now **mechanically derived, not just enumerated**: it
import-resolves every `api/routes/*.py` function that DIRECTLY calls a swept helper
(including the lifecycle-map and assistant service wrappers that call C20/C22/C23) and
requires `raise_lock_timeout_conflict_if` on any such function that has a local
`except SQLAlchemyError` arm — or, where there is no local arm, the shared
`@handle_db_errors` backstop (which maps 55P03 → 409). A new direct route caller that
forgets the classifier therefore fails CI even though nobody added it to a list. The
guard covers **direct** callers and every enumerated arm; it does **not** enumerate
transitive service-level callers (see the residual table below).

**Mapping truth per surface (after the FAR-1825 pre-PR gate fixes).** A bound only helps
if the 55P03 it produces reaches the client as a conflict rather than an outage:

| Surface | Mapping |
|---|---|
| JSON/API REST route with a local `except SQLAlchemyError` arm | `raise_lock_timeout_conflict_if(exc, <route key>)`, immediately after the session-contract guard (which returns for every error except `InvalidRequestError`), → **409** with the canonical "another change is in progress" detail. Converted: `model_backends.create`, `parameter_schemas.update` (schema + set), `agents` (apply-optimized-prompt, rollback-prompt, replace-bindings), `admin_sso.create/update`, `lifecycle_maps` (import, update, save-version, update-version, graduate), `runs.observe_run_node`, `auth.accept_invite`, `in_app_notifications.update_preferences`, `admin_assistant.set_org_context_source`. |
| REST route WITHOUT a local arm (falls through to `@handle_db_errors`) | The shared classifier maps 55P03 → **409** already, so no local conversion is needed. This is the shape for `me.set_user_context_source` (the user-scoped assistant context-source save, which reaches C22/C23) and for every route whose local arms do not catch `SQLAlchemyError`. |
| MCP tool fronting a swept helper | `_tool_lock_timeout_error` → `{"error": "lock_timeout"}`. It is reachable from (a) the shared mapper `_tool_exception_error`, which every tool's generic `Exception` backstop calls (so e.g. `set_hitl_email_alerts` → C8 classifies via that arm), and (b) the explicit `except SQLAlchemyError` arm of the only two MCP tools whose CRUD helper a request path now bounds: `update_parameter_schema` (C14) and `update_parameter_set` (C7). Other MCP tools' paths take no newly-bounded lock, so they deliberately carry **no** arm — a blanket arm there would be dead code claiming a reachability it does not have. `cancel_run` and `update_pipeline_graph` keep their bespoke `lock_timeout` payloads. |
| Pre-auth accept-invite audit append | The fail-open `invite_consumed` append is already savepoint-contained: `append_audit_event` runs each attempt inside its own `begin_nested()`, so a 55P03 (now reachable via the audit-chain-head lock) rolls back only that attempt and never poisons the caller's transaction; the enrollment still commits and the route answers 200. The helper's OWN outer savepoint is explicit defence-in-depth, keeping that guarantee local rather than dependent on the logger's inner seam. |
| SSO JIT audit append (browser-redirect callback) | Same shape in `auth/sso.py::_audit_invite_consumed_fail_open` (the twin of the accept-invite helper): the logger's per-attempt savepoint contains a 55P03 from the audit-chain-head lock, so the token-issue writes still commit, and the helper's outer savepoint is explicit defence-in-depth. **Documented exception:** the enclosing `api/routes/sso.py::_run_sso_callback_process` arm still maps any 55P03 to the generic `provider_unavailable` redirect — the IdP navigated the **browser** here, so a JSON 409 cannot be delivered and no SSO contention error code exists. Pinned by `test_sso_callback_redirect_surface_is_an_explicit_55p03_exception`. |
| Workflow-import binding replace | `_persist_bindings` wraps `replace_agent_bindings` in a SAVEPOINT, so a contended Agent row rolls back only the bindings and the import continues with its existing warning. |

Known residual (accepted): the repo-wide scan covers SQLAlchemy row-lock shapes only;
raw-SQL `FOR UPDATE` (the two §4 sites) and a dynamic `getattr` call are not discovered.

**Mapping exceptions and residuals (recorded, not silently accepted).** The derived
guard proves the classifier on every **direct** route caller of a swept helper and on
every enumerated arm. Two surfaces are deliberately outside that proof, each pinned by
its own test so it cannot drift:

| Surface | Disposition | Reason |
|---|---|---|
| `api/routes/sso.py::_run_sso_callback_process` (C17 `get_live_for_email`, reached through `auth/sso.py`) | Generic mapping kept: 55P03 → `provider_unavailable` redirect | Browser-redirect surface — the IdP navigates the user here, so a JSON 409 cannot be delivered and no SSO contention error code exists. Pinned by `test_sso_callback_redirect_surface_is_an_explicit_55p03_exception`. |
| `api/routes/library.py::create_lifecycle_map_from_primitive_endpoint` (C20, transitive) | Local `except SQLAlchemyError` → 503 kept | **Transitive** service-level caller: it reaches C20 via `core/lifecycle_map/import_export.py::materialize_map_from_primitive` → `save_map_version` → `get_lifecycle_map_for_update`, so the direct-caller derivation does not see it. The lock is on a row INSERTED IN THE SAME TRANSACTION as the lock acquisition (the copy path creates the map, then saves its first version), so no other transaction can hold it and 55P03 is not practically reachable. Pinned by `test_library_materialise_arm_is_a_recorded_transitive_residual`; transitive service-level callers are an accepted residual of this guard, not a claim of coverage. |

If either arm is converted to a 409 (or the library path becomes practically
reachable), remove its exception entry and re-record it here in the same change — the
pinned tests fail otherwise.

**Out-of-scope findings recorded, not fixed here** (both are category A/B, not C):

1. **A1 `api/mcp_server.py::bind_connector_to_node` is unbounded.** It takes the pipeline
   row `FOR UPDATE` inside the shared `_session` context manager, which sets RLS but no
   `lock_timeout`; the MCP tool path therefore has a user-facing unbounded wait for a
   contended pipeline row. The row's `txn` note says "caller txn" but not "bounded".
   Follow-up: add the bound inside `bind_connector_to_node` (same shape as A19) or accept
   it explicitly.
2. **The whole B series is unbounded**, not only B9. B1–B8 (`auth/api_key.py`,
   `auth/oauth.py`, `db/crud/token_family.py`, `core/mcp_setup_handoff`) and B9
   (`api/routes/api_keys.py::_enforce_user_key_quota`, which shares the Account row with
   C8) all take caller-transaction locks with no `set_mutation_row_lock_timeout` in front
   of them. Category B was out of scope for FAR-1825; a B-series sweep (with the same
   surface-mapping work) is a separate delivery.
