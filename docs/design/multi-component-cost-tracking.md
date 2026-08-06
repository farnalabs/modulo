# Multi-Component Per-Run Cost Tracking — Distilled Specification

**Status:** Implementable reference (derived from plan-v32, an editorial pass).
**Owner:** Modulo product
**Target PRD section:** 8.10 (Cost Controls) + new sub-section
**Feature flag:** reuse `admin_cost_breakdown` (tier `team`)
**Related:** ADR 019 (cost formula engine); migration `0066_cost_components`

This document is the single implementable reference for the cost-tracking
feature. The plan body it was derived from is historical. Section numbering
($0–$11) is preserved verbatim; every non-home section is pointer-only.

---

## 0. Problem statement

Today `Run.total_cost_usd` is a single `Numeric(14,6)` from two hardcoded
formulas: `executor._compute_token_costs` (LLM tokens for `agent` nodes) and
`node_runner._compute_sandbox_cost` (E2B sandbox = `(elapsed/3600) ×
E2B_SANDBOX_USD_PER_HOUR` + agent `cost_estimate_usd`). No breakdown is
visible, nothing is configurable, and the agent-reported model cost is a
heuristic rather than opencode's real session cost.

**Goals**

1. A run's cost is a sum of named cost components, each with a rate/value, a
   formula, a source (`calculated` vs `self_reported`), and a declared set of
   run parameters it may reference.
2. Formulas are validated — a safe expression engine (whitelist of params and
   operators). No arbitrary Python, no `eval`/`exec`, no third-party evaluator.
3. Admins can view/configure components and their rates/formulas; run detail
   shows the breakdown, not just a total.
4. `Run.total_cost_usd` stays the summed total; the existing hardcoded E2B
   formula becomes one configured component with the same default.
5. Dogfood agents report real opencode model cost (`model_cost_usd` +
   `token_usage`) instead of the heuristic. PR C (devtools) lands first so real
   model cost is on the wire from day one. The no-dip conditions are normative
   in §9.3; this section does not restate them.
6. A node's model cost is never counted twice. A node with positive
   `model_cost_usd` (≥ floor) that is a sandbox node (from the node-type map,
   `sandbox_by_map`) with an enabled consuming `self_reported` component
   contributes to that component; its constant-rate estimate is replaced, not
   added. `llm_tokens` covers only nodes without a report (incl. orphaned
   self-reports and non-sandbox nodes). The per-node `cost_usd` is computed by
   `build_telemetry` — the SINGLE classification authority — and carried back
   into the union. The `llm_tokens` money estimate never trusts agent-supplied
   token counts — it is server-measured; sandbox-by-map estimated nodes
   contribute 0 tokens (their estimate is wall-clock money via
   `sandbox_infra`, not tokens × rate). Agent `token_usage` is display-only
   context in output.json; it is not folded into the run record.
7. Honest transition statement. On deploy, run totals will shift for runs that
   carried `cost_estimate_usd`, toward accuracy. The empty-set legacy fallback
   is byte-identical for runs that did NOT carry `cost_estimate_usd`; for runs
   that did, it shifts by exactly that term. Canonical in §9.3.

**Non-goals (out of scope for v1)**

- Per-org LLM pricing tables — `llm_tokens` keeps today's constant rates.
- Per-pipeline or per-node component overrides. Components are org-scoped.
- A formula param reading arbitrary node outputs. `reported` covers
  self-reported values only.
- GH Actions billing (free tier) and Fly infra (not per-run attributable).
- Fraud/underreport detection — trusted-until-cap, no evasion signal. The PRD
  states the ledger is a REPORT, not a source of truth, and that per-key ledger
  strictness is deliberately not guaranteed. The risk is made MEASURABLE, not
  blocked, by the observability-only suspect-report signal (§1.6) and the
  probe (§4.7).
- The reconciliation apparatus. No background reconciliation job, no
  `cost_reconciliation_runs` table, no snapshot-consistent CTE, no skip
  taxonomy, no `deploy_ts` tracking, no lock-step ledger purge. The probe is a
  canary, not an audit; its mechanism count is BUDGETED and must NOT grow in
  v1.
- The enforcement-window switch. Enforcement keeps today's `created_at`
  behavior (zero regression); the report keys by `started_at`; the views
  differ near midnight — bounded, documentable divergence.
- The dated removal of the empty-set legacy fallback is a DEFERRED follow-up
  task: the fallback STAYS as the rollout blast shield. The fallback de-trusts
  `cost_estimate_usd` — its total is computed from server-verified wall-clock
  only, so it is attacker-safe even before the scheduled removal.
- `agent_signal` is a TRIGGER type, not a cost consumer. The plan touches NONE
  of its firing.

---

## 1. Data model

### 1.1 Decision: dedicated `cost_components` table (not settings_json)

The table is additive, org-scoped, and matches every other config entity. Cost
breakdown values are denormalized onto the run at finalization, so deleting a
component never affects historical runs. The dedicated table wins over
`organisation.settings_json` on RLS org-scoping, structured validation,
per-component audit, enumeration, and migration effort.

### 1.2 `cost_components` table

New model: `backend/src/modulo/db/models/cost_component.py` — `class
CostComponent(SoftDeleteMixin, OrgScoped)` (SoftDeleteMixin FIRST, house
pattern).

| Column | Type | Notes |
|---|---|---|
| `name` | `String(64) NOT NULL` | slug |
| `display_name` | `String(128) NOT NULL` | |
| `kind` | `String(20) NOT NULL` | `calculated` \| `self_reported` |
| `rate_usd` | `Numeric(18,6) NULL` | null => not rate-based / env fallback |
| `rate_fallback` | `String(32) NULL` | e.g. `e2b_rate` |
| `formula` | `String(256) NULL` | required iff `calculated`; NULL for `self_reported` |
| `report_key` | `String(64) NULL` | required iff `self_reported` |
| `enabled` | `Boolean NOT NULL DEFAULT true` | |
| `sort_order` | `Integer NOT NULL DEFAULT 0` | |

`formula` is NULLABLE — `NULL` for `self_reported`, required non-null for
`calculated`. `cost_components` is a BRAND-NEW table (migration 0066), so
`formula` is simply created nullable. The engine evaluates `reported` for the
self_reported kind; the stored formula string is dropped for that kind. Seeds
set `formula = NULL` on `model_tokens`.

`constants_json` is dropped; token/pricing constants are built-in read-only
params (§2.2). `SoftDeleteMixin` adds `deleted_at`; DELETE marks `deleted_at`,
never removes. Seeds skip soft-deleted names. `rate_fallback` is an explicit
env fallback source (§3.3); `rate_usd=NULL` is validated, not magic name
coupling. `rate_fallback` stays a SINGLE fallback name.

**Settings-knob vs constants authority pinned:** `constants.py` holds ONLY
defaults; runtime reads flow through `get_settings()`. A knob's env override
moves the boundary EVERYWHERE (asserted by test 8g, §2.5). Magic constants
live in ONE module + Settings aliases: `cost_controller/breakdown/constants.py`
— `MAX_FORMULA_LENGTH = 256`, `MAX_FORMULA_DEPTH = 8`,
`MAX_SELF_REPORTED_USD = Decimal("10000.0")`,
`MAX_REPORTABLE_USD_MIN = Decimal("0.000001")`, `MAX_RATE_USD =
Decimal("100000.0")`, `MAX_COMPONENTS_PER_ORG = 50`, `MAX_NAME_LENGTH = 64`,
`MAX_DISPLAY_NAME_LENGTH = 128`, `MAX_BREAKDOWN_BASIS_SIZE = 2048`,
`NODE_TYPE_SANDBOX_AGENT = "sandbox_agent"` (the pinned node-type literal),
`PLAUSIBLE_NODE_COUNT = 100`, and `MAX_REPORTABLE_BAND_USD` — the SINGLE
canonical name for the ABOVE-BAND clamp ceiling (the TOP OF THE SANITY BAND,
the trust boundary for self-reported model cost; used in BOTH backend
`constants.py` AND devtools `_common.py`; the dual name `BAND_ABOVE_CEILING`
is dropped). CI asserts `MAX_REPORTABLE_BAND_USD`'s VALUE in both repos.
`MAX_FOLDED_TOKENS_PER_NODE` is deleted (no folded-token cap).

Three operator-tunable Settings knobs with env aliases:
`MODULO_MAX_SELF_REPORTED_USD`, `MODULO_MAX_REPORTABLE_USD_MIN`,
`MODULO_MAX_RATE_USD`. The knobs carry ge-bounds symmetric with the
anti-abuse floor, AND `MAX_SELF_REPORTED_USD` gains an EFFECTIVE CEILING (a
ceilingless knob means `1e9` silently disables the load-bearing per-node
clamp):

- `MODULO_MAX_REPORTABLE_USD_MIN` — `ge=0.000001` (a sub-floor knob would
  silently disable the floor).
- `MODULO_MAX_SELF_REPORTED_USD` — `ge=0.000001`, and the WRITE-PATH effective
  value is `min(knob, 99999999.999999)` — the column-cap min-cap; a `1e9` env
  value cannot silently disable the per-node clamp.
- `MODULO_MAX_RATE_USD` — `ge=0`, effective value `min(knob,
  999999999999.999999)` (the Numeric(18,6) column cap).
- `e2b_sandbox_usd_per_hour` ALREADY carries `ge=0` in `settings.py` — do NOT
  re-add (documentation-only).

The band ceiling is a fourth knob `MODULO_MAX_REPORTABLE_BAND_USD` (default
`50.0`, `ge=0.000001`).

**Env-knob typing:** the dynamic rate validator reads the knob as a Decimal
(`Decimal(str(get_settings().modulo_max_rate_usd))`), never a raw float/Decimal
`min()` mismatch. Tested. A 0/negative env value for any knob fails at Settings
LOAD, never silently accepted.

**Blast stated:** Settings fail-at-load IS a boot outage — the failure is
DELIBERATE (fail-fast; a bad knob value must be unrepresentable, and on the
single-instance dogfood deploy a bad env value blocks boot). A boot-time
self-test validates the knobs + the ordering invariant + the floor-vs-clamp
guard + the knob-below-band guard and prints a CLEAR recovery message (the
offending knob + the valid range) before exiting.

**Knob ordering enforced at Settings LOAD, not log-only:**

- `MODULO_MAX_REPORTABLE_USD_MIN >= MODULO_MAX_SELF_REPORTED_USD` RAISES at
  Settings load (a high floor silently disables self-reporting with no
  canary). No boot-time log-only fallback.
- Floor-vs-band guard — boot-FATAL: `MAX_REPORTABLE_USD_MIN >=
  MAX_REPORTABLE_BAND_USD` RAISES at Settings load (a floor at or above the
  band ceiling omits every plausible report). The comparison is
  DECIMAL-TYPED (coerced with `Decimal(str(...))`).
- Knob-below-band guard — boot-FATAL: `MAX_REPORTABLE_BAND_USD >
  MAX_SELF_REPORTED_USD` RAISES (the `out_of_band_high` marker can never fire
  if no report can exceed the band). The normal order (band ≤ clamp) passes.

The FLOOR and the guards are checked in the SETTINGS LOAD path, so EVERY
process that constructs Settings — including the SAQ system worker, which
never runs the FastAPI boot self-test — fails fast identically; the boot
self-test remains as the operator-facing surface.

Unique constraints are partial unique indexes (compose with soft delete):
`uq_cost_components_org_name_active` on `(organisation_id, name)` WHERE
`deleted_at IS NULL`; `uq_cost_components_org_report_key_self` on
`(organisation_id, report_key)` WHERE `kind = 'self_reported' AND deleted_at
IS NULL`. Postgres-only; on SQLite (dev backend) enforcement is delegated to
cross-field validation. The uniform 409 pre-check runs BEFORE insert on ALL
backends with an org filter: `SELECT 1 FROM cost_components WHERE
((report_key = :k AND kind = 'self_reported') OR name = :n) AND organisation_id
= :org_id AND deleted_at IS NULL` — the explicit parens pin the intended
precedence, and the `organisation_id` clause is the cross-tenant rule (raw
`text()` otherwise bypasses RLS). The partial unique index backstop returns 409
on a genuine race. No 503 claim scoped: `IntegrityError` → 409; only
non-Integrity `SQLAlchemyError` → 503. Both 409s tested.

`name` slug: `^[a-z][a-z0-9_]{1,63}$`. Reserved `name`s: `reported`, `rate`,
`cost_estimate_usd`, `model_cost_usd`. Reserved `report_key`s: `reported`,
`rate`, `cost_estimate_usd`. `model_cost_usd` is NOT reserved as a report_key
(seed contract key).

`report_key` v1 constraint: only `model_cost_usd` can EVER be satisfied in v1.
A self_reported component with any other `report_key` is permanently
`missing_self_report: true` — state, do not block. Cap-slot trap stated: such
a component consumes one of the `MAX_COMPONENTS_PER_ORG` (50) cap slots for
the org's lifetime; the UI warns via the per-component "not reported" chip.
Component CRUD emits audit events on create/update/soft-delete.

Python 3.13: use `Mapped[...]` annotations (bare ones are ignored/rejected).

### 1.3 `Run.cost_breakdown` JSON column

Add to `Run` (`backend/src/modulo/db/models/run.py`):

```python
cost_breakdown: Mapped[Optional[list[dict[str, Any]]]] = mapped_column(JSON)
```

**Shape** (list of component snapshots; amounts are **strings** so Decimal
precision survives JSON round-trips):

```json
[
  {
    "component": "llm_tokens",
    "display_name": "LLM Tokens",
    "source": "calculated",
    "amount_usd": "0.000800",
    "formula_applied": "tokens_input * input_token_rate + tokens_output * output_token_rate",
    "rate_usd": null,
    "basis": {
      "tokens_input": 50,
      "tokens_output": 10,
      "input_token_rate": "0.000010",
      "output_token_rate": "0.000030",
      "nodes_estimated": 1
    }
  },
  {
    "component": "sandbox_infra",
    "display_name": "Sandbox Infrastructure",
    "source": "calculated",
    "amount_usd": "0.133200",
    "formula_applied": "rate * wall_clock_hours",
    "rate_usd": "0.133200",
    "basis": { "wall_clock_hours": 1.0 }
  },
  {
    "component": "model_tokens",
    "display_name": "Model cost (self-reported)",
    "source": "self_reported",
    "amount_usd": "0.040000",
    "formula_applied": "reported",
    "rate_usd": null,
    "basis": {
      "reported": 0.04,
      "raw_reported": 0.0412,
      "node_count": 1
    },
    "missing_self_report": false
  }
]
```

**Total-level clamp — marker defined ONCE here.** When the summed total exceeds
`Decimal("99999999.999999")` (Numeric(14,6) capacity), the total is clamped to
the max and the breakdown is PREFIXED with a synthetic marker entry:

```json
{ "total_clamped": true, "amount_usd": "0.000000" }
```

Every component entry's serialized `amount_usd` STRING is ALSO clamped to the
flat ceiling (`"99999999.999999"`, never `"1E+40"`). The marker is ALSO emitted
on the empty-set legacy-fallback write when the fallback total is flat-clamped.
Detectable via JSON containment `cost_breakdown::jsonb @> '[{"total_clamped":
true}]'` — no new column.

The marker is NOT a component: run-detail renders a "total clamped to column
capacity" banner instead of a row, detected BEFORE the zero-amount-row omission
filter; the probe skips marker-bearing runs; `amount_usd 0.000000` keeps naive
Σ unaffected. This is the ONE documented exception to `total == Σ`. Breakdown
consumers MUST skip non-component entries.

Field meanings:

- `component` — slug (stable for aggregation). `source` — `calculated` |
  `self_reported`. `formula_applied` — the formula at run time.
- `amount_usd` — string (Decimal 6dp, string-clamped).
- `basis` — actual substituted param values; the observational record of the
  formula/rate applied. Bounded by `MAX_BREAKDOWN_BASIS_SIZE` per entry; on
  overflow the largest multi-value member (`raw_reported`/per-node map) is
  TRUNCATED deterministically (newest N + `"node_count": N`, logged once).
  `basis.raw_reported` serialized display is clamped to a sane magnitude (1e6)
  for UI rendering — the raw value stays in the basis for audit, but the UI
  money line cannot render 1e300. The same display clamp is applied on the
  node-output surface (`model_cost_display_usd`) and the union serialization
  surface (`RunResponse.node_token_usage` / MCP `get_run_status`).
- Per-node clamp record: `"raw_reported"` (pre-clamp) + `"clamped"` +
  `"out_of_band_high"`. `basis.raw_reported = union.model_cost_raw_usd`;
  `basis.clamped = union.model_cost_clamped` (ANY clamp — band OR per-node —
  true when `clamped != raw`); `basis.out_of_band_high =
  union.model_cost_out_of_band_high`. The two flags are NOT mutually exclusive:
  a band-clamped value has `clamped=true AND out_of_band_high=true`; a
  per-node-clamped value (only reachable if the band were configured above the
  per-node cap, which the knob-below-band guard forbids in v1) has
  `clamped=true, out_of_band_high=false`.
- `missing_self_report` is PER-COMPONENT, scoped to ELIGIBLE nodes (sandbox by
  map + classifiable); `true` renders a "not reported" chip + increments
  `modulo_cost_components_missing_self_report_total{component}`; ABSENT when
  no eligible nodes. Growth is a Watcher baseline line + a SOFT signal.
- Single eval-error code: any eval failure (incl. negative computed amount,
  DivisionByZero, non-finite) → entry with `amount_usd: "0.000000"` +
  `"error": "eval_error"`, metric `modulo_cost_components_eval_errors_total`.
  The per-entry warning LOGS THE EXCEPTION CLASS. A NON-FINITE summed total is
  ALSO an `eval_error` (guard before the flat clamp — §2.4).
- Amounts below `0.000001` quantize to `"0.000000"` with a positive basis; the
  UI shows the basis.
- `segments` does not exist: resume recomputes (§4.4).

### 1.4 Live component reads at finalization — no run-start snapshot

No `Run.cost_component_snapshot` column. At finalization the executor reads
live enabled, non-deleted rows from `cost_components` (ordered by `sort_order`,
then `name`) INSIDE the same finalization transaction as the build + write
(§4.2). The breakdown `basis` records the formula/rate actually applied — the
audit trail a snapshot was meant to provide.

Documented, accepted consequences (PRD + module docstring): editing a
rate/formula mid-run re-prices at finalization; disabling/deleting a calculated
component mid-run drops that cost class; disabling/deleting a consuming
`self_reported` component does NOT drop the cost (orphan-routing → ESTIMATED);
a true commit-during-finalize race is accepted — cost is observational, the
stored `basis` records whatever was read.

### 1.5 Consistency invariant — single write path

**`total_cost_usd` ≡ `Σ component.amount_usd` with ONE documented exception.**
One function computes both and they are written together:

```python
# modulo/core/cost_controller/breakdown/aggregate.py
def build_cost_breakdown(
    telemetry: RunCostTelemetry,
    components: list[CostComponentConfig],  # LIVE enabled rows, read in-transaction
) -> tuple[list[dict[str, Any]], Decimal]:  # (breakdown, total)
```

- Amounts quantized to 6dp (`Decimal("0.000001")`). `total = sum(amounts)` in
  Decimal, quantized. NaN guard: `if not total.is_finite(): → eval_error entry
  + metric + zero total` BEFORE the flat clamp — never NaN into the DB.
- `update_run_status` gains a `cost_breakdown` parameter; `execute()` and
  `resume()` pass the tuple. No other path writes `total_cost_usd` for run
  finalization — a grep-asserted test keeps this alive in CI.
- The invariant holds below the corner; in the corner the total is clamped and
  flagged (marker, §1.3). The clamp stack is the LOAD-BEARING set, documented
  in ONE place (§4.5 — normative); this section is a POINTER, not a
  restatement.
- Eval-time traps collapsed. `build_cost_breakdown` runs under
  `decimal.localcontext()` with ONLY `DivisionByZero` trapped. Any eval-time
  failure — the trap, a missing param, a non-finite result, or a negative
  computed amount — surfaces as a generic `"error": "eval_error"` entry +
  metric, never a crash.
- Legacy fallback — TWO triggers, BOTH documented. The fallback applies (a)
  when the enabled component set is EMPTY, OR (b) on ANY cost-path exception
  inside the never-fail envelope (below). The fallback writes `llm_tokens` +
  one `sandbox_infra` entry, `total = token_cost + legacy_sandbox_cost`. The
  fallback DE-TRUSTS agent-supplied `cost_estimate_usd`.
  `legacy_sandbox_cost` is computed from SERVER-VERIFIED wall-clock ONLY —
  `elapsed/3600 × E2B_SANDBOX_USD_PER_HOUR` — dropping the old
  `+ cost_estimate_usd` term. A hostile legacy `cost_estimate_usd` can no
  longer inflate the fallback total. The fallback is attacker-safe. The
  legacy-fallback write is FLAT-CLAMPED too and emits the marker (same shared
  signal).
- The never-fail envelope covers the whole cost path. The component load +
  `build_cost_breakdown` + `update_run_status` cost fields + spend-ledger write
  are inside one `try/except Exception` (logged via `_log.exception()` +
  `cost_component_finalize_failed`). On ANY exception the executor degrades to
  the legacy byte-identical total and increments
  `modulo_cost_components_fallback_legacy_total`. Cost accounting can never
  fail a run — by construction. On a cost-path exception, roll back the aborted
  session (savepoint) BEFORE the fallback reuses it. Last resort: if even the
  fallback raises (residual `Numeric` DataError), persist a zero total + log.
  Scoped to COST-ONLY exceptions: a deadlock/DB abort aborts the finalization
  transaction and surfaces as a run failure — acceptable, NOT swallowed. The
  ledger block is handled by bounded retry then the reduced
  terminalize-without-ledger corner (§4.2), NOT by failing the run.

### 1.6 Self-report tri-state + node-type gating + honest backward-compat

The telemetry builder classifies every node into two disjoint sets, driven by
live enabled components:

- **Self-reported nodes:** positive, finite, numeric `model_cost_usd` ≥ floor
  AND matching `report_key` on an enabled consuming `self_reported` component
  AND a sandbox node by map (`sandbox_by_map`, from the run's frozen
  NODE-TYPE MAP — agent/connector nodes are NEVER self-reporting regardless of
  output keys; a map-absent node is NEVER self-report-eligible). Contribute to
  `reported[report_key]`, EXCLUDED from `llm_tokens` sums. The effective
  reporting floor is the magnitude-band floor 1e-4 ($0.0001) —
  `read_opencode_cost` omits reports below it. `MAX_REPORTABLE_USD_MIN`
  (1e-6) remains the node_runner extraction floor for reports that DO arrive
  (defense in depth). Band-floor vs extraction-floor asymmetry is stated: a
  report in (1e-6, 1e-4) is accepted by node_runner but omitted by
  `read_opencode_cost` — a stated asymmetry, NOT a mismatch to "fix".
- **Estimated nodes:** every other node (no key, explicit-0 omitted, invalid,
  sub-floor, non-sandbox, or orphaned consuming component). Contribute tokens
  to `tokens_input`/`tokens_output`, counted by `missing_self_report`. The
  token sums are SERVER-MEASURED ONLY: LLM-token nodes contribute their
  server-measured token entry; sandbox-by-map estimated nodes contribute 0
  tokens. Agent-supplied `token_usage` is NOT folded into the union.

A node contributes to exactly one of `llm_tokens` / `model_tokens`.
`total = llm_tokens + model_tokens + sandbox_infra + ...` — never an
estimate-plus-real double count.

**The sandbox signal is SPLIT — wall-clock vs self-report are decoupled.** Two
flags on the enriched union entry:

- `is_sandbox_for_wallclock` = `node_type_map.get(node_id) == 'sandbox_agent'`
  OR (`node_type_map.get(node_id) is None AND wall_clock_time_ms present`) —
  used ONLY for wall-clock summing (`sandbox_infra`).
- `sandbox_by_map` = `node_type_map.get(node_id) == 'sandbox_agent'` — a
  separate map-derived flag used ONLY for self-report classification.

`build_telemetry` classifies a node as self-reporting iff `sandbox_by_map` is
true (and it carries `model_cost_usd`). A map-absent node is never
self-report-eligible regardless of its output keys. The enrichment ASSERTS map
completeness — any executed node id absent → structured log
`cost_components_missing_node_type`. The `'sandbox_agent'` literal is a PINNED
constant (`NODE_TYPE_SANDBOX_AGENT` in `constants.py`), grep-asserted against
the graph node-type source.

**The node-type map is FROZEN at run start.** The map is captured ONCE per run
from the snapshot's `graph_json` at run start, and passed into `finalize_cost`
at every pause and resume — never re-read from a mutable store at resume. A
mid-run graph edit cannot change `sandbox_by_map` mid-run. Pre-migration
resume map policy: a run paused BEFORE the deploy has no run-start-frozen map;
derive the map at the FIRST post-deploy finalization and freeze it there, with
a documented classification drift for that one-time class. `graph_json`
completeness is confirmed at run start: nodes materialized AFTER start would be
permanently map-absent → self-report silently disabled for them → the
`cost_components_missing_node_type` completeness log fires (LOGGED, never a
crash). This policy is stated in §9.3 as an accepted transition artifact.

**No underreport signal — trusted until the cap.** A report ≥ floor is trusted
up to the caps; fraud detection is out of scope (§0). A self-reporting node
whose `model_cost_usd < 0.1 × estimated_node_cost` — where
`estimated_node_cost` is derived from the node's RECORDED token usage at the
documented constant rates — emits a structured
`cost_components_suspect_report` log carrying the RATIO. NEVER blocks, never
changes the total, never triggers rollback. The ratio is SKIPPED when
`estimated_node_cost <= 0` (a self-reporting node with zero recorded tokens
produces no ratio — the band clamp + per-node clamp + daily clamp bound it). If
token rates become configurable, the ratio basis silently drifts — a doc line
next to the denominator guard flags the assumption.

**Orphaned self-reports never vanish:** a reporting node whose consuming
component is soft-deleted/disabled is classified ESTIMATED + flagged with a
structured `cost_components_orphan_report` log. The orphan-node's per-node
`cost_usd` is ESTIMATED-consistent: `build_telemetry` is the SINGLE
classification authority and computes the per-node `cost_usd` — an
orphan-report node's `cost_usd` is token-derived, NEVER its `model_cost_usd`.

**Wall-clock is independent of the split:** `wall_clock_hours` is summed over
ALL completed sandbox nodes (by `is_sandbox_for_wallclock`), unconditionally.
`wall_clock_time_ms` is written SERVER-SIDE by `make_sandbox_agent_fn` AFTER
the agent output; the enrichment trusts ONLY that value.

**Honest backward-compat:** the empty-set legacy fallback is byte-identical
for runs that did NOT carry `cost_estimate_usd`; for runs that did, it shifts
by exactly that term. Pre-migration historical runs are never recomputed. On
deploy, totals for runs that carried `cost_estimate_usd` WILL shift, toward
accuracy, when devtools C is re-deployed. The no-dip conditions are NORMATIVE
in §9.3.

---

## 2. Safe formula engine

New package: `backend/src/modulo/core/cost_controller/breakdown/`:

- `formula.py` — tokenizer, recursive-descent parser, evaluator,
  `CostFormulaError`.
- `params.py` — param registry + `RunCostTelemetry` + telemetry builder.
- `aggregate.py` — `build_cost_breakdown` + recompute helpers.
- `constants.py` — shared limits/constants (defaults only; runtime via
  `get_settings()`).
- `metrics.py` — the metric counters (single owning module); `modulo_`-prefixed.

### 2.1 Grammar

```
expr     := term (('+' | '-') term)*
term     := factor (('*' | '/') factor)*
factor   := '-' factor | primary
primary  := NUMBER | IDENT | '(' expr ')'
NUMBER   := [0-9]+('.'[0-9]+)? | '.'[0-9]+
IDENT    := [A-Za-z_][A-Za-z0-9_]*
```

Allowed operators: `+ - * /`, unary minus, parentheses. NO functions. The
5-function whitelist is removed. Rejected: `**`, comparisons, assignment,
attribute access, subscripting, string literals, semicolons, any out-of-grammar
token, any non-whitelist identifier. The grammar must NOT grow in v1 — a v1
grammar grow is a security-review event, never an implementation-time
convenience.

Unary minus is parse-legal; a negative eval RESULT is an `eval_error`.
Intermediate subexpressions may be negative; only the final value is checked.
Unary-minus chains are bounded by `MAX_FORMULA_DEPTH` like any other nesting.

### 2.2 Identifier whitelist (global param registry)

All values are `Decimal` (ints too). Convert with `Decimal(str(value))`, NEVER
`Decimal(float(...))`.

| Identifier | Type | Meaning | Source | v1 consumer |
|---|---|---|---|---|
| `rate` | Decimal | `rate_usd`; if null → `rate_fallback` (§3.3) | component config (live) | `sandbox_infra` |
| `e2b_rate` | Decimal | `Settings.e2b_sandbox_usd_per_hour` | settings | `sandbox_infra` fallback |
| `input_token_rate` / `output_token_rate` | Decimal | default token rates (today's constants) — built-in, read-only | engine | `llm_tokens` |
| `wall_clock_hours` | Decimal | Σ sandbox elapsed over ALL completed sandbox nodes (by `is_sandbox_for_wallclock`) — unconditional | telemetry | `sandbox_infra` |
| `tokens_input` / `tokens_output` / `tokens_estimated` | int | Σ over estimated nodes — SERVER-MEASURED ONLY | telemetry | `llm_tokens` |
| `node_count` / `nodes_estimated` | int | counts | telemetry | `llm_tokens` basis + operator formulas |
| `reported` | Decimal | Σ of `report_key` across self-reporting nodes — self_reported kind only | agent output | `model_tokens` |

The four dead params are CUT: `minutes_per_hour`, `wall_clock_seconds`,
`wall_clock_minutes`, `nodes_reported` — absent from the registry. The internal
seconds field is `wall_clock_elapsed_s` — it is NEVER a registry identifier.
`wall_clock_hours` remains the SOLE wall-clock identifier;
`tokens_input`/`tokens_output` remain the token identifiers;
`tokens_estimated`, `node_count`, `nodes_estimated` remain as documented
operator formula inputs. `tokens_input_reported` / `tokens_output_reported`
are DELETED (the folded-token change is cut). `tokens_estimated` =
estimated-node tokens ONLY (formula input); `Run.total_tokens` = the
SERVER-MEASURED total across LLM-token nodes (a persisted run column, reverted
to today's semantics — NOT the formula input). A grep-asserted test pins the
two are distinct.

Whitelist enforcement:

- One `validate_formula(formula, allowed_idents)` at save time and eval time
  (no separate enforcement split).
- `calculated`: allowed set = everything except `reported`.
- `self_reported`: the formula is IMPLICIT `reported` — the engine always
  evaluates `reported`; the `formula` column is NULL for this kind (the stored
  string is dropped; `validate_formula` is not called on it).
- Engine constants and `reported` are read-only.
- Reserved names rejected as component names / report_keys (§1.2).

### 2.3 Implementation approach and security rationale

Hand-rolled tokenizer + recursive-descent parser (~100 LOC, zero deps).
Reject `eval()`/`exec()` and `asteval`. `eval()` with restricted
globals/locals is escapable via introspection; our tokenizer cannot even
produce `.`, `[`, `__`, or a call — zero escape surface. Codified in
`docs/adr/019-cost-formula-engine.md`.

### 2.4 Validation rules (single validate function)

**Save-time (syntactic + identifier validation):**

1. `len(formula) <= MAX_FORMULA_LENGTH` (256) — skipped for `self_reported`
   (formula NULL).
2. Must tokenize; unknown characters (incl. non-ASCII homoglyphs) →
   `CostFormulaError("unexpected_character")`. ASCII whitespace only — NBSP is
   a character error, no silent juxtaposition.
3. Balanced parentheses; no empty subexpression (`()`).
4. Identifiers in the allowed set; unknown → error listing the set (fail-closed).
5. Nesting depth ≤ `MAX_FORMULA_DEPTH` (8). Unary-minus chains count toward
   the depth.
6. No out-of-position tokens (e.g. `1 2`, `() 3`).
7. `rate` referenced → `rate_usd` OR a registered `rate_fallback` required.
8. `rate_usd` bound is a DYNAMIC validator: a `model_validator` reads
   `get_settings().modulo_max_rate_usd` — the env knob moves the write-path
   boundary. The validator's cap is `min(get_settings().modulo_max_rate_usd,
   999999999999.999999)` — the Numeric(18,6) column cap is the hard upper
   bound. The knob is read as a Decimal (`Decimal(str(...))`), never a raw
   float `min()`. Tested with an env value above the column cap. In a
   NON-REQUEST context where Settings is unavailable, the validator RAISES a
   ValueError with a CLEAR message — mapped by FastAPI to a 422 (never an
   implicit 500-by-default, never a 503-by-default). The knob cannot be read,
   so the write is refused, never silently passed.
9. `rate_fallback` REGISTRY validation: a `rate_fallback` NOT in the
   registered set — currently exactly `{"e2b_rate"}` — is rejected with a 422
   listing the valid names (fail-closed). `rate_fallback` stays a SINGLE
   fallback name.

**Eval-time (runtime):**

1. One generic catch-all. Any eval failure — DivisionByZero trap, missing
   param, non-finite result, `OverflowError`, or negative computed amount —
   surfaces as a generic `"error": "eval_error"` entry +
   `modulo_cost_components_eval_errors_total{component}`. No taxonomy. The
   catch-all logs the exception CLASS. A NON-FINITE summed total is ALSO an
   `eval_error` + metric + zero total, BEFORE the flat clamp — never a clamped
   NaN.
2. Magnitude handling — FLAT CLAMP, never scaling (mechanics normative in
   §4.5; this is a pointer): the summed total over the column ceiling is
   clamped flat + the `total_clamped` marker (§1.3),
   `modulo_cost_components_clamped_total{kind="total_flat_clamp"}` ONCE. The
   fallback write clamps with the SAME marker. Each entry's serialized string
   is independently clamped. `total ≡ Σ` holds below the corner.

**Runtime failure policy:** an errored component contributes `0.00` +
`eval_error` + metric; the run still completes. A malformed formula is
rejected at save time.

### 2.5 Unit-test plan for the engine (`backend/tests/unit/core/cost_controller/`)

Correctness: `rate * wall_clock_hours` → expected; the token formula →
expected; precedence (`2 + 3 * 4` → 14, `(2 + 3) * 4` → 20); unary minus
parse-legal + negative RESULT = `eval_error` (`2 - -3` → 5); the four dead
params are REJECTED as unknown identifiers AND grep-asserted absent from the
registry + `wall_clock_seconds` absent from the dataclass field names;
identifier validation (`wall_clock_hours` allowed for `sandbox_infra`,
rejected for `self_reported`; `reported` in a calculated formula → 422); a
257-char formula → rejected; Decimal end-to-end (`0.1 + 0.2` → `0.3` at 6dp);
the union→breakdown float→Decimal boundary (`Decimal(str(value))` +
quantize ROUND_HALF_UP; a banker's-rounding fixture pins a `.0000005`-boundary
value); raw-float path asserted absent; `1/0` → eval-time generic `eval_error`
entry; `0/0` → NaN → generic `eval_error` entry + metric; FLAT-CLAMP boundary
by VALUE (just below → true sum, no marker; exactly at → the ceiling, no
marker; just above → the ceiling, marker FIRST, metric once, amounts
unchanged; two max components → ceiling + marker, never a DataError); a single
component alone over the max → flat-clamped; residual-overflow fallback
(DataError → envelope degrades to legacy fallback, then zero + log); fallback
DE-TRUSTS `cost_estimate_usd` + fallback flat-clamp + MARKER; knob authority +
single near-ceiling check (see §1.2 for the near-ceiling check); Settings
ge-bounds (each knob below its ge-bound fails at load; the ordering + the
floor-vs-clamp + the knob-below-band guards raise at load); knob ceiling test
(`1e9` for `MODULO_MAX_SELF_REPORTED_USD` is min-capped on the write path);
column-cap validator test (`MODULO_MAX_RATE_USD` above the Numeric(18,6) cap →
a `rate_usd` at the env value is rejected 422); rename test (`tokens_estimated`
NOT `tokens_total`; `tokens_input_reported`/`tokens_output_reported`/
`minutes_per_hour`/`wall_clock_seconds`/`wall_clock_minutes`/`nodes_reported`
asserted absent from the registry, grep); `MAX_FORMULA_DEPTH` (9 deep →
rejected, exactly 8 → accepted; a 256-char unary-minus chain is bounded by the
depth check and rejected when it exceeds it).

Attack strings (all raise `CostFormulaError`, never execute):
`__import__('os').system('id')`; `()[().__class__.__bases__[0].__subclasses__()]`;
`open('/etc/passwd')` / `open`; `1 if True else 2`; `a or b`, `not a`,
`a == b`, `a < b`; `abs(-1).__class__`; `pow(2,3)`, `eval(1)`, `exec("x")`,
`round(2.5)`; `rate / wall_clock_hours` with zero hours → eval-time
`eval_error`; empty / whitespace-only formula → rejected; reserved names as
`name`/`report_key` → 422; non-ASCII/homoglyph (full-width asterisk U+2217,
NBSP) → `unexpected_character`.

NaN/Inf and string-clamp tests: a >28-digit coefficient → non-finite sum →
`eval_error` + metric + ZERO total (never NaN/clamped NaN). A single component
over the ceiling → string `"99999999.999999"` (never `"1E+40"`), total
flat-clamped with marker. The raw_reported display clamp is asserted — a
`raw_reported` of 1e300 renders at 1e6 in the serialized display while the
basis keeps the raw value. The node-output-surface display clamp is asserted
via `model_cost_display_usd`. The union-surface display clamp is asserted too.

---

## 3. Cost component registry / config

### 3.1 CRUD API

New route file: `backend/src/modulo/api/routes/cost_components.py` (prefix
`/api/v1/admin/costs/components`, tags `["admin", "costs"]`), following
`costs.py` conventions: `@handle_db_errors(...)`, `set_rls_org` inside
`session.begin()`, `require_permission("cost.manage")`,
`require_feature("admin_cost_breakdown")` on the CRUD surface — the write
routes AND the GET list. Each mutation emits an audit event.

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/admin/costs/components` | List (org), soft-deleted excluded |
| POST | `/api/v1/admin/costs/components` | Create (validate formula + kind/report_key + env fallback + reserved names + org cap) |
| PUT | `/api/v1/admin/costs/components/{component_id}` | Partial update (same validation) |
| DELETE | `/api/v1/admin/costs/components/{component_id}` | Soft delete; last enabled `calculated` is undeletable |

**There is NO `validate-formula` endpoint.** It would duplicate the save-time
422 validation. The save-time validation is the sole validation path; the
frontend's inline UI validation round-trips through the save endpoint's 422.

Pydantic request/response models (length bounds from `constants.py`):

- `name`: `str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")`
- `display_name`: `min_length=1, max_length=MAX_DISPLAY_NAME_LENGTH`
- `kind`: `CostComponentKind`
- `rate_usd`: `Decimal | None = Field(None, ge=0)` with the DYNAMIC upper
  bound (`min(knob, 999999999999.999999)`)
- `rate_fallback`: `max_length=32`
- `formula`: `max_length=MAX_FORMULA_LENGTH` (NULL for self_reported)
- `report_key`: `max_length=64`
- `enabled`: `bool = True`; `sort_order`: `int = 0`

`CostComponentUpdate` supports PARTIAL update via `exclude_unset=True`.
Explicit `rate_usd: None` clears the field back to NULL (env fallback) — do
NOT use `exclude_none`.

Cross-field validation (on both Create and Update):

- `kind == self_reported` → `report_key` required; `rate_fallback` must be
  `None`; `formula` MUST be `None`/absent (implicit `reported`).
- `kind == calculated` → `formula` required; `report_key` `None`; formula may
  not reference `reported`.
- Formula references `rate` → `rate_usd` OR registered `rate_fallback`
  required; `rate_fallback` registered and `None` when `rate_usd` set.
- `rate_fallback` is the only valid fallback reference AND must be in the
  registered fallback set `{"e2b_rate"}` — an unregistered name → 422 listing
  the valid names.
- Reserved `name`/`report_key` → 422 (§1.2).
- Duplicate existence check — uniform org-scoped 409 pre-check (§1.2); the
  partial unique index backstops races → 409.
- Per-org cap: `>= MAX_COMPONENTS_PER_ORG` (50) → 422.
- disable-of-last-calculated guard: the last enabled `calculated` component is
  undeletable AND cannot be disabled (PUT `enabled: false` → 422);
  kind-change guard: `calculated → self_reported` on it → 422.
- Last-of-kind guard extended to `self_reported`: an operator cannot
  soft-delete (or disable) the last enabled `self_reported` component without a
  destructive-action confirm warning (reports orphan → ESTIMATED until a
  component with the report_key is recreated). ALLOWED with confirmation
  (unlike `calculated`, which is hard-blocked).
- Daily-spend-limit bound — APP-SIDE 422 ONLY. The DB CHECK constraints are
  dropped (the numeric columns already cap storage). The early-422 bound sites:
  THREE org/team budget sites + FIVE trigger sites, each with their Numeric
  column bound (`99999999.999999` for the spend sites, `99999999.9999` for the
  trigger sites). All EIGHT early-422 sites are retained and tested.

Error mapping: invalid formula → `422` with a structured `CostFormulaError`
code; DB errors via `handle_db_errors` (`IntegrityError` → 409; other → 503).

### 3.2 Seed / default components

Seeds applied to EVERY org at startup AND on org creation. The seeder calls
`set_rls_org` inside each `session.begin()`, wraps each org in `try/except`.
Idempotent: insert only when no active row has the `name`; soft-deleted names
are skipped. ORG ENUMERATION runs in SYSTEM CONTEXT with NO `set_rls_org`
(exactly like the probe's org enumeration). If the enumeration were subject to
RLS it would return zero orgs and SILENTLY skip seeding. A negative test
mirrors the probe's fixture — an RLS-scoped enumeration returns no orgs while
the system-context enumeration seeds every org. The SYSTEM-CONTEXT mechanism
is the app role's owner-bypasses-RLS connection (the app role owns
`organisations`), so a plain query sees all rows. `cost_components`' own
RLS-owner gate (the app role must NOT own it) is a SEPARATE, distinct
ownership rule.

| name | kind | formula | rate_usd | rate_fallback | report_key |
|---|---|---|---|---|---|
| `llm_tokens` | `calculated` | `tokens_input * input_token_rate + tokens_output * output_token_rate` | `NULL` | — | — |
| `sandbox_infra` | `calculated` | `rate * wall_clock_hours` | `NULL` | `e2b_rate` | — |
| `model_tokens` | `self_reported` | `NULL` (implicit `reported`) | `NULL` | — | `model_cost_usd` |

`sort_order` 10 / 20 / 30. If the `model_tokens` seed is soft-deleted, the org
loses self-report routing (reports orphan → ESTIMATED) until a component with
the `model_cost_usd` report_key is manually recreated. Exactly TWO seeds carry
a real formula (`llm_tokens` and `sandbox_infra`, both `calculated`);
`model_tokens` is `self_reported` with an IMPLICIT formula. The v1 surface is
2 formulas + 1 implicit — a third formula-carrying seed or a formula on
`self_reported` is a v1 scope break.

### 3.3 Env fallback contract

- Keep `Settings.e2b_sandbox_usd_per_hour` + `E2B_SANDBOX_USD_PER_HOUR`. A
  component may declare `rate_fallback="e2b_rate"`; the engine resolves
  `e2b_rate` from `get_settings().e2b_sandbox_usd_per_hour` when `rate_usd` is
  null. An explicit `rate_usd` wins (`rate_fallback` cleared to NULL).
  `e2b_rate` is also a named param. `e2b_rate` is the ONLY registered fallback
  name.
- The `e2b_rate` value participates in the SINGLE first-finalization
  near-ceiling check (§1.2). `e2b_rate` is operator-maintained; a periodic E2B
  pricing drift CHECK/ALERT is a dated follow-up.
- Deprecation: mark `E2B_SANDBOX_USD_PER_HOUR` deprecated in the config
  reference; do NOT remove in this delivery.
- `node_runner._E2B_SANDBOX_USD_PER_HOUR`/`_compute_sandbox_cost` stay for the
  legacy fallback path (§1.5); routed through `get_settings()` at RUNTIME (a
  real code change — the constant is an import-time read today).
  `_compute_sandbox_cost`'s fallback TOTAL no longer adds the agent
  `cost_estimate_usd` term — the server-verified wall-clock total is the whole
  fallback; the `cost_estimate_usd` artifact stays only as a legacy UI display
  field.

---

## 4. Runtime wiring

### 4.1 node_runner: emit structured per-node cost signals

**Sandbox↔node cardinality — stated ONCE here.**
`make_sandbox_agent_fn` creates a FRESH E2B sandbox PER sandbox_agent NODE
INVOCATION — each `sandbox_agent` node gets its own `AsyncSandbox` and
therefore its own `opencode.db`. Each node's `read_opencode_cost` reads THAT
node's OWN sandbox DB, so per-node model cost is correct with NO N×
double-count. If a future change shares one sandbox across nodes, the reuse
predicate AND this per-node-DB cardinality argument MUST be revisited — stated
now so the guard is not silently inherited.

**Extraction boundary — `_extract_reported_cost` (node_runner, the SINGLE
extraction authority).** Returns `(raw, clamped, was_clamped,
out_of_band_high)` ONLY for a POSITIVE finite numeric `model_cost_usd` (> 0).
`None` for absent key, non-dict, non-numeric, NaN/Inf, negative, zero, or bool
(bool rejected explicitly). `None` => the key is NOT written. Any
`0 < val < MAX_REPORTABLE_USD_MIN` is treated as NOT reported → `None` (closes
the spend-evasion hole where a 1e-9 report suppressed the estimate).

RAW SOURCE PRECEDENCE: the raw input is read from `model_cost_raw_usd` WHEN
PRESENT (the producer's pre-clamp value — devtools writes it), falling back to
`model_cost_usd` for legacy producers.

CLAMP ORDER (pinned): the value is clamped at the per-node cap
(`_effective_self_reported_cap()`, min-capped at the column cap) AND at the
BAND CEILING. THE BAND CLAMP DOMINATES because band < per-node cap:
`min(min(raw, cap), band) == min(min(raw, band), cap)` — the final value is
IDENTICAL regardless of which clamp runs first. The band clamp is enforced HERE
— the SINGLE extraction authority — so ANY producer (a customer/third-party
sandbox pipeline script, a fork of the dogfood scripts, an agent writing
`model_cost_usd` directly) is bounded at the band ceiling BEFORE the per-node
clamp. A $6000 misread (or hostile value) cannot flow 100× into the
enforcement SUM. `was_clamped = True` iff `clamped != raw` — ANY clamp (band OR
per-node); `out_of_band_high = True` iff `raw > the band ceiling`. The
magnitude-based structured log `cost_components_out_of_band_high` fires
REGARDLESS of token counts.

SCHEMA-DRIFT FLAG READ AT THE TOP: this function FIRST checks the
devtools-emitted `schema_drift` producer-wire key (the FATAL minimal dict
`{"schema_drift": true}` forwarded by write_output) and returns None (no
report) when it is truthy — a drifted-schema node reports NO cost. The COUNTER
INCREMENT does NOT happen here (the provenance gate is evaluated in
`_enrich_union` — §4.2 — where the frozen map and `sandbox_by_map` are
available; it fires only on `is_terminal` finalizations, gated on
`pin_failed == false` AND the node being sandbox-by-map).

**`_effective_self_reported_cap()` — devtools default STATED:**
`read_opencode_cost` (devtools, which cannot read the backend Settings) uses
the CONSTANTS default via `_effective_self_reported_cap()`; the backend
node_runner's clamp is AUTHORITATIVE — the executor re-applies the
Settings-knob clamp (effective value min-capped at the column cap) when it
extracts `model_cost_usd` from the node output, so a devtools-side default
drift can never bypass the knob.

**Node-output fields.** Add to the node `output` only when not None:
`"model_cost_usd": <clamped>`, `"model_cost_raw_usd": <raw>`,
`"model_cost_display_usd": <clamped-at-1e6>` (the SEPARATE clamped display
field the UI/money formatter renders), and `"model_cost_clamped"` +
`"model_cost_out_of_band_high"` written UNCONDITIONALLY (a boolean — `true`
when clamped/out-of-band, `false` written EXPLICITLY otherwise) (extraction
OVERWRITES both flags from its own computation on every report, so a legacy or
hostile marker already on the node output can never survive). When None the
keys are ABSENT — `0.0` is NEVER written as a report. Keep `wall_clock_time_ms`
and legacy `cost_estimate_usd` untouched (existing consumers).
`wall_clock_time_ms` is written SERVER-SIDE AFTER the agent output; enrichment
trusts ONLY that value (negative test). The raw value never reaches the money
formatter through the node-output surface (the display field is clamped; the
raw node-output surface is audit context, stated in the operator guide — a
hostile raw value on that surface is a display concern, not a money path).

node_runner ALSO extracts the pin-failure signal — SERVER-SIDE via the E2B
filesystem API (reads the sandbox-local flag file at the same time it reads
the node output), NEVER from an agent-supplied `output.json` key. It emits
`pin_failed: true` into the node output dict as a SERVER-side-derived key —
overwriting any producer-supplied value. A `pin_failed` key an agent places in
output.json is NEVER trusted. `read_opencode_cost` NEVER invokes the opencode
binary.

The opencode DB path is resolved from the SANDBOX's OWN env (`XDG_DATA_HOME`/
`HOME`), never hardcoded — a fresh E2B sandbox may not have the default
`$HOME` layout.

**Clamp semantics pinned:** per-node clamp at `MAX_SELF_REPORTED_USD = 10000`
(effective value min-capped at the column cap); the BAND CEILING clamp is
applied HERE at the extraction boundary — node_runner is the SINGLE extraction
authority, so the trust boundary is UNIVERSAL. A legitimately-long real session
that genuinely bills >$50 is ALSO clamped at $50 — under-reported by design,
now universally. NO aggregate re-clamp at this layer; `build_cost_breakdown`
has no policy ceiling either (§1.5).

### 4.2 executor: finalize against LIVE components, cumulative write-back, union enrichment, ledger_written guard

**Finalization block shape (execute() and resume() share ONE helper):**

```python
async def finalize_cost(
    session, run_id, segment_node_token_usage, segment_completed_node_outputs,
    live_components, node_type_map, *, is_terminal,
) -> None:
    # ONE session.begin(): component read + build + run write + ledger.
    # Cumulative union; segment-wins on node-id collision (replaced, never summed).
    # Terminal-path FOR UPDATE retained (load-bearing for duplicate dispatch).
    # ACTIVE-TRANSACTION CONTRACT: finalize_cost runs INSIDE the caller's
    #   existing session.begin(). NO nested begin() here; the ONLY nesting
    #   allowed is the ledger block's begin_nested() savepoints (non-abort retries).
    merged_usage = _merge(stored=run.node_token_usage, segment=segment_node_token_usage, segment_wins=True)
    merged_outputs = _merge(stored=run.outputs_json, segment=segment_completed_node_outputs, segment_wins=True)
    enriched = _enrich_union(merged_usage, merged_outputs, node_type_map, is_terminal)
    telemetry, per_node_cost = build_telemetry(enriched, live_components)
    breakdown, total = build_cost_breakdown(telemetry, live_components)
    enriched = _write_back_node_cost(enriched, per_node_cost)   # single authority
    total_tokens = _derive_total_tokens(enriched)            # SERVER-MEASURED
    await update_run_status(
        session, run.id, run.status,   # status already determined by caller
        total_cost_usd=total, cost_breakdown=breakdown,
        node_token_usage=enriched,      # persist the ENRICHED union, never segment-only
        outputs_json=merged_outputs,    # full outputs — unchanged write for run-detail
        total_tokens=total_tokens,      # derive + persist Run.total_tokens
    )
    # Ledger block — terminal only, guarded by ledger_written under FOR UPDATE,
    # bounded retry (begin_nested savepoints for NON-ABORT errors) + the REDUCED
    # terminalize-without-ledger corner. NO SWEEP.
    if is_terminal and total is not None and total > 0:
        locked = (await session.execute(
            select(Run).where(Run.id == run_id).with_for_update()
        )).scalar_one()
        if locked.ledger_written or locked.ledger_refused_at is not None:
            _log.warning("cost_ledger.duplicate_terminal", run_id=run_id)   # skip
            return
        ok, reason = await _record_ledger_with_retry(
            session, run=run, total=total, attempts=3,
        )  # begin_nested() savepoints for NON-ABORT errors only
        if not ok and reason == "daily_limit_exceeded":
            locked.ledger_refused_at = utcnow()
            _log.info("cost_ledger.limit_reached", run_id=run_id)
            return
        if not ok:
            # REDUCED terminalize-without-ledger escape, write_failure ONLY:
            # persist the FULL finalization field set in a FRESH transaction,
            # set NOTHING ELSE, leave ledger_written = false, log
            # cost_ledger.finalize_deferred + increment
            # modulo_cost_ledger_finalize_deferred_total{reason="write_failure"}.
            return
        locked.ledger_written = True   # same transaction/update
```

**Limit-refused vs write-failure, pinned ONCE here.** `check_and_record_spend`
returns `(False, "daily_limit_exceeded")` for expected enforcement and
`(False, <other>)`/raised error for genuine failure:

- `(False, "daily_limit_exceeded")` → NO escape, NO finalize-deferred
  increment, NO error log. Run keeps `ledger_written = false`, full breakdown
  on the row, `ledger_refused_at` set to `utcnow()` +
  `modulo_cost_ledger_limit_refused_total{team}` incremented + the refused
  amount written to the org/team day rows' `refused_spend_usd`. Refusal is
  PERMANENT — nothing clears `ledger_refused_at`.
- `(False, <other>)`/raised error → the reduced escape (full field-set persist,
  log `cost_ledger.finalize_deferred`, increment `{reason: "write_failure"}`),
  3 retries via `begin_nested` (non-abort), fresh tx for whole-tx aborts. This
  class keeps `ledger_written = false` AND `ledger_refused_at IS NULL`.
- Distinguishable at one surface: `ledger_refused_at IS NOT NULL` (refused) vs
  `ledger_written = false AND ledger_refused_at IS NULL` (write-failure).

**Refusal ordering pinned.** The ledger block checks BOTH the org and team
daily limits BEFORE writing either row. Only if BOTH pass are the org row then
the team row written (org first). A team-limit refusal after an org-limit pass
writes NEITHER spend row. On a refusal, the refused amount IS written to BOTH
rows' `refused_spend_usd` (org + team). The refusal decision semantics — the
SUM predicates, the exclusion predicates, the no-limit short-circuit, and the
permanent-refusal rule — are NORMATIVE in §4.6.

**THE UNION ENRICHMENT STEP — stated once here.** The union is newly
constructed in `finalize_cost`. `_enrich_union(merged_usage, merged_outputs,
node_type_map, is_terminal)` folds per-node cost summaries from the
completed-node output dicts into the union BEFORE build_telemetry, setting per
completed node: `wall_clock_time_ms` (server-side value);
`model_cost_usd` (re-clamped through `_clamp_reported`, §4.5);
`model_cost_raw_usd`; `model_cost_clamped` and `model_cost_out_of_band_high`
(folded from the node-output dict written by extraction — AUTHORITATIVE, never
recomputed from the clamped value; `_clamp_reported`'s own flags are the
fallback only for legacy producers and the pre-migration stored-union-only
class); the SPLIT sandbox signals `is_sandbox_for_wallclock` and
`sandbox_by_map`; `cost_usd` NOT set here — build_telemetry computes it.

THE SCHEMA-DRIFT INCREMENT happens HERE in `_enrich_union`, because
`node_type_map` (the frozen map) is in scope AND because the increment is
TERMINAL-ONLY (fires only when `is_terminal` is true). For each node whose
output dict carries the devtools-emitted `schema_drift: true` flag, increment
`modulo_cost_opencode_schema_drift_total` GATED on `pin_failed == false` (the
server-side-derived flag) AND on `node_type_map.get(node_id) ==
NODE_TYPE_SANDBOX_AGENT` (the provenance gate — a non-sandbox node carrying a
spoofed `schema_drift` key is IGNORED; a sandbox node's `schema_drift` flag is
trusted by design — the impact is bounded by the estimate + band clamp + the
terminal-only dedup).

**The per-node `cost_usd` (single authority):** computed INSIDE
`build_telemetry` and written back into the union via
`_write_back_node_cost(enriched, per_node_cost)` — union `cost_usd` and the
breakdown ALWAYS agree. A self-reporting sandbox node reports `cost_usd =
model_cost_usd` (FIRST), token-derived second, 0 third. The orphan-node
`cost_usd` is token-derived, NEVER its `model_cost_usd`.

`is_sandbox_for_wallclock` and `sandbox_by_map` are BOTH derived from the
node-type map, NOT from `wall_clock_time_ms` presence alone. Wall-clock
authority: ONLY the server-side value. `outputs_json` is a telemetry input
ONLY through this enrichment fold, never read directly by the builder. The
read is DEFENSIVE: a node whose output is missing from BOTH stored
`outputs_json` and the current segment is tolerated (the stored-union
re-clamp path).

**node-type map DEFAULT + completeness, stated once here.** A node absent from
`node_type_map` is: sandbox for wall-clock summing
(`is_sandbox_for_wallclock` → fail-safe toward real sandbox time); non-sandbox
for self-report classification (`sandbox_by_map` false → never trusted). The
enrichment ASSERTS completeness — any executed node id absent → a structured
`cost_components_missing_node_type` log. A WRONG-TYPED map produces no
absent-id signal — the enrichment ALSO logs a RATIO of executed-node types vs
the map, so a systemic map-drift is observable, not silent.

**Cumulative write-back invariant — stated once, applies everywhere:**
`run.node_token_usage` and `run.outputs_json` are ALWAYS the cumulative union
of all completed node segments, from the first finalization onward. The
fallback path persists the UN-ENRICHED merged set, so the invariant survives a
cost-path exception too. Merge precedence pinned: on node-id collision the
SEGMENT wins (replaced, never summed). `node_token_usage` holds the
newly-constructed consumer-shape per-node cost summary: `id`,
`wall_clock_time_ms`, `model_cost_usd` (stored as a FLOAT — JSON-serializable;
hostile NaN/Inf are rejected at extraction), `model_cost_raw_usd`,
`model_cost_clamped`, `model_cost_out_of_band_high`, `input_tokens`,
`output_tokens`, `total_tokens`, `cost_usd`, `is_sandbox_for_wallclock`,
`sandbox_by_map`. NO cumulative-set cap; the run row already tolerates
unbounded JSON via `outputs_json`. Log-only size guardrail. A structured
warning fires when the union JSON exceeds a documented threshold (e.g. > 8 MB)
— log-only, not a cap.

**Terminal-status set — CANONICAL definition pinned ONCE here.** The terminal
status set used by the operator query, the probe sample predicate, and the
migration comment is exactly `('complete', 'failed', 'cancelled',
'eval_failed')`. SQL-literal copies in the operator query (§8) and the probe
sample predicate (§4.7) are REQUIRED and grep-asserted against this
definition.

**The ledger guard is the explicit `ledger_written` boolean, read UNDER
`FOR UPDATE`, NEVER based on prior status.** `runs.ledger_written boolean NOT
NULL server_default 'false'` (no backfill). `ledger_written == true` means "a
ledger row has been written for this run's final spend." `ledger_refused_at`
set when the terminal write is refused by a daily limit; NULL otherwise. NO
backfill. The FOR UPDATE is held during `check_and_record_spend` only.

**Pre-deploy paused runs:** their pause-time ledger row (OLD code) is NOT
flagged (no backfill); on resume + terminalize a second row is added → a
documented ONE-TIME overcount. Both shapes are pre-deploy-scoped residue,
documented as a report artifact in §9.3; there is no reconciliation machinery
to skip them.

**Terminal reachability — the funnel-through-one-block is a DESIGN TARGET
with THREE KNOWN HOLES TO CLOSE.** The verified callers are the finalization
call sites inside `execute()`/`resume()` and the HITL approve/resume handler.
THREE direct terminal writes exist TODAY that do NOT pass through any
cost/finalize block: (1) `_stream_graph`'s `RunCancelledError` handler — PR A
changes it to return the accumulated sets (4th element); (2) the
capacity-timeout `"failed"` terminal write in `_stream_graph`; (3)
`request_cancellation` (`db/crud/run.py`) — sets `status='cancelled'` +
`completed_at` directly. All three must be ROUTED THROUGH `finalize_cost` in
PR A. Budget-enforcement (`RunawayGuard` → `RunawayRunError` →
`final_status="failed"`) and `node_timeout`/`budget_exceeded` funnel through
the SAME `finalize_cost` block. Eval pipeline on an ALREADY-COMPLETE run →
guard skips → no double row.

**The accumulation mechanism + the signature changes, stated together.** The
accumulated `node_token_usage` and `completed_node_outputs` live in
`_stream_graph`'s local scope. Two signature changes ship in PR A:
(1) `_handle_graph_interrupt` (the awaiting_human path) changes its SIGNATURE
to accept the accumulated dicts and returns them in the `awaiting_human`
4-tuple (by-reference `completed_node_outputs`); (2) the `RunCancelledError`
handler returns the accumulated sets: `return ("cancelled", None, None,
node_token_usage or None)` — `completed_node_outputs` rides BY-REFERENCE into
the caller's dict (no 5th element). The empty-accumulator case (`{}` → `None`)
is asserted. `_stream_graph` logs a STRUCTURED warning distinguishing "zero
nodes completed" (legitimate empty accumulator) from "accumulation broken".

**`request_cancellation` streamed-cancelled finalize — DATA SOURCE PINNED:**
when a STREAMED run is cancelled via `request_cancellation` (a SEPARATE
request process), the cancel path RE-READS the STORED cumulative sets
(`run.outputs_json` + `run.node_token_usage`) and passes THOSE to
`finalize_cost`. A streamed run that HAS PAUSED at least once has stored sets
→ partial breakdown + ONE ledger row. A NEVER-PAUSED in-flight run cancelled
cross-process finalizes from EMPTY (total 0, no breakdown, no ledger row, and
NO counter signal); the in-process `RunCancelledError` handler covers the
never-paused class when the executor is ALIVE. The never-paused cross-process
forfeiture is STATED and its audit-trail claim is CORRECTED: a never-paused
streamed run cancelled cross-process FORFEITS its accrued cost — the accrued
spend is gone from the run's total, the breakdown, AND the ledger, and no
counter fires. It is surfaced ONLY via the
`cost_components_partial_spend_lost` DIAGNOSTIC LOG — run_id ONLY (no segment
count, no amount). A log, not a counter, not a mechanism.

**Concurrent cancel-vs-executor-finalize race:** the breakdown/
`update_run_status` write on the cancelled path is UNGUARDED — a concurrent
cancel and an executor finalize can race with last-committed-wins on the run
row. The ledger guard prevents DOUBLE rows but not a stale pair. PR A routes
the cancel path through `finalize_cost` with the SAME active-transaction
contract; the residual race is bounded (one of the two commits wins
atomically) and observable via `cost_ledger.duplicate_terminal` if the ledger
block double-fires.

**Session lifecycle:** the block runs in a short-lived session; component read
+ build + `update_run_status` + ledger write in ONE `session.begin()`;
`set_rls_org` inside it. Roll back the aborted session before the fallback
reuse.

**`run_date` = `started_at`, pinned to UTC:** the ledger key date is
`run.started_at.astimezone(UTC).date()`; `started_at IS NULL` (never-started)
→ skip the ledger write. Documented behavior change: ledger-row keying moves
from terminal-day (now-date) to run-start-day (`started_at` UTC date).

**Pre-component-read terminal transition:** fails with `total_cost_usd = 0`,
`cost_breakdown = NULL`, no ledger write. State + test.

### 4.3 `RunCostTelemetry` builder (`cost_controller/breakdown/params.py`)

```python
@dataclass
class RunCostTelemetry:
    wall_clock_elapsed_s: Decimal        # internal; the FORMULA-visible id is wall_clock_hours
    tokens_input: int                    # SERVER-MEASURED ONLY
    tokens_output: int
    tokens_estimated: int
    node_count: int
    nodes_estimated: int
    reported: dict[str, Decimal]         # report_key -> summed value, CONSUMING component only
    clamped_nodes: list[str]
    raw_reported: dict[str, float]
    orphan_report_nodes: list[str]
    missing_report_keys: set[str]
    suspect_report_nodes: list[tuple[str, float]]
    per_node_cost: dict[str, Decimal]    # node_id -> cost_usd, the SINGLE authority
```

`build_telemetry(node_token_usage, components) -> tuple[RunCostTelemetry,
dict[str, Decimal]]`. A node is self-reporting iff: (1) positive
`model_cost_usd` ≥ floor; (2) sandbox by MAP (`sandbox_by_map`); (3) an enabled
consuming `self_reported` component's `report_key` matches. `reported[report_key]`
populated only for consuming keys. A sandbox node with no consuming component
is ESTIMATED + `orphan_report_nodes` + `cost_components_orphan_report` log. A
map-absent node is ESTIMATED + the completeness log fires.
`per_node_cost` is computed by THIS builder (self-report FIRST, then
token-derived, then 0). The basis derivation is pinned: `basis.clamped =
union.model_cost_clamped` (ANY clamp); `basis.out_of_band_high =
union.model_cost_out_of_band_high` — derived from the union flags, never
recomputed.

Token sums are SERVER-MEASURED ONLY. `tokens_input` / `tokens_output` /
`tokens_estimated` sum the SERVER token entries across estimated nodes.
`Run.total_tokens` is derived from the server token entries only.
`tokens_input_reported`/`tokens_output_reported` are DELETED.

NO underreport signal. Suspect-report observability, CALIBRATED: a
self-reporting node with `model_cost_usd < 0.1 × estimated_node_cost` is
recorded in `suspect_report_nodes`; `estimated_node_cost` is derived from the
node's RECORDED token usage at the documented constant rates. The report is
USED unchanged. v1 emits a structured `cost_components_suspect_report` log
carrying the RATIO. NEVER blocks. Denominator guard: when
`estimated_node_cost <= 0` (a self-reporting node with zero recorded tokens),
the ratio is SKIPPED. Basis-drift note: if token rates become configurable,
the ratio basis silently drifts — flagged in the operator guide.

`missing_report_keys` = enabled report_keys present in ZERO eligible node
outputs; ABSENT when no eligible sandbox nodes. `wall_clock_elapsed_s` (never
a registry identifier) = Σ SERVER-side `wall_clock_time_ms / 1000` over ALL
completed sandbox nodes (by `is_sandbox_for_wallclock`), unconditionally;
`wall_clock_hours` = `wall_clock_elapsed_s / 3600`. Eval-time invariant: every
consumed self-report key has ≥ 1 enabled consuming component; else warn +
orphan log. `outputs_json` is NOT read directly by the builder.

### 4.4 HITL resume parity — cumulative union + two-state recompute

1. Read the stored cumulative set at resume's finalization start (NULL →
   pre-migration → union from the resumed segment).
2. Merge: `merged = stored ∪ segment`; on node-id collision SEGMENT wins.
3. Recompute from LIVE components, OVERWRITE `total_cost_usd` +
   `cost_breakdown`, persist merged sets. `total == Σ` by construction.
4. Two-state rule:

   | Stored `cost_breakdown` | Action |
   |---|---|
   | present | Full recompute over the cumulative merged set, live components; overwrite all. |
   | NULL | Build the breakdown from the cumulative merged set; overwrite. No synthetic entry. |

   No third state, no `legacy_carryover`.

5. NO separate `FOR UPDATE` on the resume read. Legitimate resumes are
   serialized by the run's `status` transition; recompute is idempotent. A
   duplicate resume dispatch → the same total (pinned with a test).

**outputs_json recovery path on resume recompute.** When the stored union is
NULL or a node is missing, read the node's pre-pause wall-clock/model-cost from
the STORED `outputs_json`. PR A FIRST VERIFIES that a pre-migration paused run
HAS stored `outputs_json`: grep the pre-change executor pause path. (a) YES
(likely): keep the recovery path. (b) NO: downgrade §9.3's artifact-1 claim to
honest undercount for that tiny class. When the recovery finds NOTHING for an
expected node, a `cost_components_recovery_missed` log fires — the miss is
visible, not silent.

Accepted one-time transition artifact — token undercount: pre-deploy paused
runs never persisted pre-gate tokens → `llm_tokens` undercounts ONCE for that
class.

**Deadlock scope:** a DB-level abort in the finalization tx aborts it and
surfaces as a run failure — the never-fail guarantee is scoped to COST-ONLY
exceptions. A deadlock in the ledger write goes straight to the fresh-tx
escape.

### 4.5 Self-reported validation & invariant-preserving clamps

- node_runner validates positive/finite and clamps per node to
  `MAX_SELF_REPORTED_USD` (effective value min-capped at the column cap) AND to
  the band ceiling (§4.1 — the extraction boundary is the single enforcement
  point), writing `model_cost_raw_usd` (raw) + `model_cost_display_usd`
  (clamped) + `model_cost_out_of_band_high`; sub-floor values are not reports;
  `model_cost_clamped` asserted in unit tests.
- Magnitude = FLAT total clamp — `min(total, 99999999.999999)` + marker,
  preceded by the non-finite guard. The legacy fallback engages ONLY on an
  empty enabled set or a cost-path exception (§1.5) and is itself flat-clamped
  + marked (the fallback total is SERVER-VERIFIED wall-clock ONLY).
- `total_cost_usd` is `Decimal` end-to-end, quantized once `ROUND_HALF_UP`,
  under the DivisionByZero-only `localcontext`; `Decimal(str(value))`.
- The whole block is wrapped in the never-fail envelope (§1.5).

**The clamp stack is the LOAD-BEARING set — ONE normative table HERE; §1.5/§2.4
reference it, they do not restate it.** One shared helper for every ceiling
clamp:

```python
def _clamp_to_ceiling(value: Decimal, ceiling: Decimal, kind: str) -> Decimal:
    """Clamp a value to a ceiling; the CALLER decides whether a metric/log is
    warranted. NaN/Inf never reach this helper (the non-finite guard runs first)."""
```

**Re-clamp at enrichment — ONE shared helper:**

```python
def _clamp_reported(value: Decimal | float) -> tuple[Decimal, bool, bool]:
    """Re-clamp a folded self-reported model cost at enrichment (defense-in-depth):
    returns (clamped_value, was_clamped_any, out_of_band_high). `was_clamped_any`
    is TRUE for ANY clamp (band OR per-node — clamped != raw). `out_of_band_high`
    is True iff raw > the band ceiling. NOT the flag authority on the live path.
    Re-validates the stored value as if it were a fresh report: bool /
    non-numeric / NaN/Inf -> treated as ABSENT (route to estimate); a stored
    value below the floor -> skipped; then clamp (band + per-node). INPUT PIN:
    the enrichment calls
    _clamp_reported(model_cost_raw_usd if model_cost_raw_usd is not None else
    model_cost_usd) — the RAW value when the node output carries it."""
```

**Anti-abuse layering — ONE consolidated table:**

| Layer | Where | Ceiling | What it catches |
|---|---|---|---|
| Absolute floor | node_runner / `build_telemetry` | `MAX_REPORTABLE_USD_MIN` (1e-6, ge-bound); EFFECTIVE reporting floor is the band floor 1e-4 | degenerate sub-floor reports |
| Per-node clamp | node_runner | `MAX_SELF_REPORTED_USD` (10k, ge-bound, effective value min-capped at the column cap) | an absurd single-node report (agent input is UNTRUSTED) — the HOSTILE-INFLATION case |
| ABOVE-BAND clamp | `_extract_reported_cost` in node_runner — the SINGLE extraction authority (the devtools reader applies the same clamp via the shared constant, now redundant but harmless) | `MAX_REPORTABLE_BAND_USD` = 50.0 — the TOP OF THE SANITY BAND | a DRIFT-charged or HOSTILE self-reported value from ANY producer — clamped at the band ceiling with `min()`, never replace; a $6000 misread cannot flow 100× into the enforcement SUM; carries the `model_cost_out_of_band_high` marker + the `cost_components_out_of_band_high` magnitude log (fires regardless of token counts); a legitimately-long real session that genuinely bills >$50 is ALSO clamped at $50 |
| Non-finite guard | `build_cost_breakdown` (before flat clamp) | finite check | NaN/Inf → eval_error + zero |
| Total flat clamp | `build_cost_breakdown` | 99999999.999999 | residual summed total over the column capacity; marker-flagged once |
| Per-entry string clamp | serialization | `"99999999.999999"` per string | a runaway amount breaking frontend money formatting / the Σ smoke |
| raw_reported display clamp | serialization | 1e6 | `basis.raw_reported` renders sanely in the UI; raw value stays in the basis |
| Node-output display clamp | node-output serialization | 1e6 on `model_cost_display_usd` | `model_cost_raw_usd` in the node output dict cannot reach the UI/money formatter as 1e300 |
| Union serialization display clamp | RunResponse + MCP `get_run_status` + MCP run-list line serialization | 1e6 | `model_cost_raw_usd` in the union cannot reach the UI/money formatter through the union surface |
| Legacy-fallback flat clamp | empty-set / cost-path fallback write | 99999999.999999 | a hostile legacy wall-clock total; the fallback DE-TRUSTS `cost_estimate_usd`; also emits the marker |
| Daily ledger clamp | `check_and_record_spend` | 99999999.999999 on `total_spend_usd` | a day's ledger row over capacity; sets the per-row `clamped` boolean |
| Refused-amount accumulation clamp | `refused_spend_usd` upsert | 99999999.999999 on `refused_spend_usd` | a heavy-refusal day's accumulation overflow → clamped + warn (a DataError here would convert "refused" into a write-failure escape) |

Each layer is invariant-preserving; the single documented exception is the
total flat clamp corner (§1.5). The per-node clamp is a DELIBERATE SHIPPED
FEATURE — DECIDED, not re-litigated. The band ceiling at $50 makes the per-node
$10,000 clamp MATHEMATICALLY DEAD in v1 (band < per-node cap, and the
knob-below-band guard forces the effective cap ≥ the band, so no value can
reach the per-node cap without first being clamped at the band), but it is
DEFENSE-IN-DEPTH for a future band rise — documented as such. The raw
`cost_estimate_usd`/`model_cost_raw_usd` values reach API/webhook/MCP consumers
of the node OUTPUT dict at full magnitude — the display clamps protect the UI
money formatter; the raw node-output surface is audit context.

**Re-clamp at enrichment — defense-in-depth:** `_enrich_union` re-applies
`_clamp_reported` to the folded `model_cost_usd`, so the union stores the
CLAMPED value and `build_telemetry` / `build_cost_breakdown` see only clamped
values. The union's `model_cost_usd` is OVERWRITTEN/POPPED for every completed
node per the PINNED ONE-mechanism rule: iterate the union's node ids;
(1) output PRESENT + carries `model_cost_usd` → OVERWRITE with the re-clamped
fold; (2) output PRESENT but LACKS `model_cost_usd` →
`union[node].pop("model_cost_usd")` AND pop the sibling flags
(`model_cost_raw_usd`, `model_cost_clamped`, `model_cost_out_of_band_high`) —
the node is estimated; (3) output ABSENT from both stored outputs_json and the
current segment → the stored-union value is re-clamped through `_clamp_reported`
and the folded flags derive from the re-clamped fold (fallback authority). This
closes the RESUME-OF-STORED-UNCLAMPED path: a `model_cost_usd` stored BEFORE
PR A deployed (a third-party script or an agent direct-write) was trusted at
full magnitude on the resume path → $6000 into `reported` → the enforcement SUM
→ permanent refusal. The extraction boundary (§4.1) remains the PRIMARY trust
boundary; the enrichment re-clamp is the cheap second layer.

### 4.6 Cost controller / spend recording — terminal-only, guarded, converged

**The real schema.** The ledger is `OrgDailyRunCount` — unique
`(organisation_id, team_id, run_date)`, no run_id column, `total_spend_usd
Numeric(14,6)`, `run_count Integer`. The table gains `clamped boolean NOT NULL
server_default 'false'` — set by `check_and_record_spend` on the daily clamp —
and `refused_spend_usd Numeric(14,6) NOT NULL server_default '0'` — the refused
amount for the key, set at refusal, surviving the run purge. The ORM model
`db/models/daily_run_count.py` adds `clamped: Mapped[bool]` and
`refused_spend_usd: Mapped[Decimal]` in the SAME PR. The unique constraint is
NULLS NOT DISTINCT — two concurrent first-of-day terminals for the same org
(team_id NULL) can no longer BOTH insert org rows. The migration drops the
plain unique CONSTRAINT by name `uq_org_daily_run_counts_org_team_date` via
`op.drop_constraint(...)` and recreates it as a NULLS NOT DISTINCT unique index
named `uq_org_daily_run_counts` (§9.1). The write path uses the EXISTING
`get_or_create_daily_count` (SELECT FOR UPDATE + `begin_nested` +
IntegrityError re-read) for the org row (`team_id=None`) and the team row
(`team_id=run.owner_team_id`); on a NULLS NOT DISTINCT collision the second
insert raises IntegrityError → the re-read path returns the existing row →
UPDATE. The ORM `OrgDailyRunCount.__table_args__` mirrors the SAME named NULLS
NOT DISTINCT unique-index construct so `test_migrated_schema_matches_orm_metadata`
passes.

**NULL-owner double-write guard:** the team-row write is guarded on
`owner_team_id IS NOT NULL` — a NULL-`owner_team_id` run writes ONLY the org
row. The refused-amount write follows the same guard: a NULL-team run's refusal
writes `refused_spend_usd` on the org row only.

**Ledger two-row lock order pinned:** ORG row written (locked) BEFORE the TEAM
row; no sweep. BOTH LIMITS checked BEFORE either row is written; a team-limit
refusal after an org-limit pass writes NEITHER spend row (the refused AMOUNT is
written to BOTH rows' `refused_spend_usd`). check-vs-create ordering pinned:
the limit checks run BEFORE `get_or_create_daily_count` is invoked — a
first-run-of-the-day refusal does NOT create a spend row with a 0 amount; it
DOES create/update a row carrying the refused amount in `refused_spend_usd` (a
"refused row", total_spend_usd 0, run_count 0). The mutation order is
org-then-team.

**`check_and_record_spend` — signature + return semantics:**

```python
async def check_and_record_spend(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    cost_usd: Decimal | None,
    team_id: uuid.UUID | None = None,
    run_id: uuid.UUID | None = None,      # for structured logs / metric tags
    run_date: date | None = None,          # executor passes run START date (UTC)
) -> tuple[bool, str | None]:
```

Return semantics pinned: `(bool ok, str | None reason)`. `ok=false` = a limit
would be exceeded and NO spend row was written; `reason` is a stable machine
code (`"daily_limit_exceeded"`). Consumers log on false
(`cost_ledger.limit_reached`) but do NOT fail the run.

**THE REFUSAL-WINDOW RULE — normative HERE.** The terminal limit check is keyed
to the CREATED-AT day — the SAME window the enforcement SUM uses. The created-at
day start (`created_at >= :day_start`) is computed by a SINGLE shared helper
(`_created_at_day_start(now)`, UTC) used by BOTH the enforcement readers
(cron_helpers/polling) AND the refusal SUM — grep-asserted (one definition,
both call sites). The no-limit short-circuit is PINNED:
`if org_limit is None: refuse_org = False` and
`if team_limit is None: refuse_team = False` — a NULL limit means unlimited,
matching current enforcement semantics. The enforcement readers' NULL
comparison is grep-asserted. The limit fetch preserves NULL (no
`coalesce(..., 0)` on the fetch path — grep-asserted ABSENT). The refusal
decision: `refuse_org = existing_org + cost > org_limit` (skipped when
`org_limit is None`); `refuse_team = existing_team + cost > team_limit`
(evaluated only when the run has `owner_team_id` AND `team_limit is not None`;
skipped for NULL-owner runs). Both refusals are PERMANENT. The current run is
counted EXACTLY ONCE per predicate — `finalize_cost` calls
`update_run_status(... total_cost_usd=total)` in the SAME transaction BEFORE the
ledger block, so a plain `SUM(total_cost_usd)` over the created-at day would see
the current run's just-written total; adding `cost_usd` on top DOUBLE-COUNTED
the common same-day case. Both SUM queries are pinned (ORG + TEAM), served by
ONE index (`ix_runs_refusal`):

```sql
-- ORG limit (SHORT-CIRCUITED when org_limit IS NULL — the SUM is NOT executed)
SELECT COALESCE(SUM(total_cost_usd), 0) FROM runs
WHERE organisation_id = :org
  AND created_at >= :day_start           -- created-at day (the enforcement window)
  AND id != :current_run_id              -- exclude the current run

-- TEAM limit (evaluated only when owner_team_id IS NOT NULL AND team_limit IS NOT NULL)
SELECT COALESCE(SUM(total_cost_usd), 0) FROM runs
WHERE organisation_id = :org
  AND owner_team_id = :team
  AND created_at >= :day_start
  AND id != :current_run_id
```

The SUM queries are SHORT-CIRCUITED when the limit is NULL: `refuse_org = False`
when `org_limit is None` (the DEFAULT) — and the ORG SUM is NOT EXECUTED at all
in that case. Then `cost_usd` is added UNCONDITIONALLY to each SUM. This counts
the current run EXACTLY ONCE per predicate (the explicit add) and excludes it
from each SUM. The refusal decision uses the SAME created-at window and org/team
scoped SUM as enforcement; for the CROSS-MIDNIGHT class (created yesterday,
terminalizing today) the explicit add puts the run against TODAY's limit while
enforcement shows it in YESTERDAY's bucket — discoverable via
`ledger_refused_at` + the report line. The org scope is the same
trigger-independent org scope the enforcement readers use.

The refusal SUM is INDEX-SERVED by `ix_runs_refusal (organisation_id,
created_at)` — a single index whose leading-org column + the `created_at` range
column serves BOTH predicates: the ORG SUM as a TRUE RANGE over today, and the
TEAM SUM as the SAME range with a RESIDUAL `owner_team_id = :team` filter. The
EXPLAIN gate asserts RANGE SELECTIVITY for BOTH predicates.

**Limit-check-then-clamp ordering:** the limit checks run FIRST — BOTH org and
team. If either existing + `cost` exceeds its limit → `(False,
"daily_limit_exceeded")` WITHOUT writing either spend row AND the refused amount
is written to the day rows' `refused_spend_usd`. The column clamp applies
AFTER, only to the stored spend row: `existing_daily + cost > 99999999.999999`
→ store at the max, warn, increment `modulo_cost_ledger_clamped_total`, set
`clamped = true`. The daily-ledger clamp is reachable when the started-at-day
row exceeds the column ceiling EVEN WITH a limit configured (the refusal checks
the created-at window; the clamp protects the started-at row). Once clamped,
the unclamped total is NOT recoverable. The refused-amount accumulation is
clamped the same way: `existing_refused + refused_amount > 99999999.999999` →
store at the max, warn, increment `modulo_cost_ledger_refused_clamped_total`.
daily-limit TOCTOU restated HONESTLY: between the limit check and the terminal
write a concurrent burst can push the day over the limit; the ONE-burst
overshoot is ACCEPTED — the burst can be a SUM of a COHORT of concurrent
paused-run partials terminalizing together. Bounded (each run's `cost_usd` is
clamped), and self-corrects on the next check.

`team_id` from `run.owner_team_id` (NULL-guarded); `run_date` =
`run.started_at.astimezone(UTC).date()`. `(False, "daily_limit_exceeded")` is
EXPECTED enforcement — NOT a ledger failure.

**Refused amount persistence — stated once here.** On a refusal, the refused
amount (`cost_usd`) is added to `refused_spend_usd` on the org row (`team_id IS
NULL`) and, when `owner_team_id IS NOT NULL`, the team row, for the run's
`run_date` (`started_at` date). The column is a plain additive upsert on the
existing row key (no special ordering — it uses the same
`get_or_create_daily_count` machinery; a refusal creates a refused-only row when
none exists), with the accumulation clamp. This makes refused visibility
PERMANENT: the refused amount lives on the ledger row, which is NEVER purged —
it survives the ~90-day run purge. The refused query (`SUM(refused_spend_usd)`
over the report period) reads the LEDGER column, NOT the runs table. The run row
keeps `ledger_refused_at` (the "was refused" marker + the operator-query
discriminator). Refused dual-write non-additivity: org-level
`refused_total_usd` must NOT be summed with the team lines (the org row is the
org-wide figure; the team rows are the breakdown — adding org + team
double-counts). The report line's date attribution uses the STARTED-AT date
(the decision used created-at) — stated explicitly.

**Call sites that change — verified exactly two, both in the executor:** the
`execute` and `resume` call sites, both called whenever `total_cost_usd_val is
not None` — even at the pause (the double-count source) and with no
team_id/date. Both REMOVED, replaced by the single `finalize_cost` ledger block
(§4.2). `upsert_daily_run_count` is DEAD / OUT-OF-CONTRACT — not called by any
live cost/ledger path; the refused-amount write must NOT be routed through it.

**Ledger semantics:**

- One call, full FINAL total, `run_count += 1` once, at terminal. No delta
  math, no per-run key.
- At the pause: run-row fields updated (partial, cumulative); ledger not
  touched. On resume: total recomputed; at terminal the ledger records the
  final total once (unless `ledger_written` or `ledger_refused_at`).
- duplicate-terminal guard: `ledger_written` (or `ledger_refused_at` set)
  under `FOR UPDATE`; true → skip + log. Failure semantics: one tx; retried
  (non-abort savepoints), then the reduced escape — write-failure ONLY, never a
  `daily_limit_exceeded` refusal; Zero/None total → no row.
- Daily ledger clamp: after the limit check, `existing_daily + cost >
  99999999.999999` → clamp + warn + metric + `clamped = true`. The limit check
  uses the TRUE new spend.
- Enforcement SUM — UNCHANGED except ONE shared-helper code edit. The
  enforcement readers (`cron_helpers` and `polling`) READ `SUM(Run.total_cost_usd)`
  directly and compare to the limit — there is NO clamp in either file. The
  readers are TRIGGER-SCOPED (key their limit evaluations by `trigger_id`); the
  refusal SUM in this section is ORG-WIDE (`organisation_id` scope) — the two
  surfaces are DIFFERENT SCOPES. The shared created-at day-start helper IS a
  code edit to BOTH enforcement readers — nothing else. `SUM` over Postgres
  Numeric is UNBOUNDED — no overflow risk; the enforcement SUM reads the
  BAND-CLAMPED `total_cost_usd`. Enforcement additionally under-counts ONLY in
  the flat-clamp corner (a marker-bearing run's `total_cost_usd` IS the clamped
  value). The enforcement window is NOT switched: enforcement keeps today's
  `created_at` window, while the report keys by `started_at`. The two views
  differ near midnight by one bucket. Enforcement-SUM population includes
  PAUSED partial totals AND the reduced-escape (`ledger_written = false`) AND
  limit-refused (`ledger_refused_at` set) runs AND the streamed-cancelled run's
  PARTIAL spend (for runs WITH A PRIOR PAUSE — which NOW ENTERS THE SUM); a
  NEVER-PAUSED in-flight run cancelled cross-process finalizes from EMPTY and
  contributes 0 to the SUM. Enforcement never depends on the ledger. The
  capacity-timeout / RunawayGuard / node_timeout / budget_exceeded partial
  spends join the population too.
- `run_count` semantics — terminal-only: counts only terminalized runs with a
  ledger row. Consumers: `get_cost_report`, the Daily Watcher run-count
  anomaly, the dashboard cards, the Quality report. The Watcher baseline notes
  the post-deploy run-count shift is EXPECTED — the baseline covers HITL 2→1,
  $0 1→0, and the cancelled-class 0→1 (SCOPED: streamed cancelled runs WITH A
  PRIOR PAUSE now write a ledger row + increment `run_count`; a NEVER-PAUSED
  in-flight run cancelled cross-process finalizes from EMPTY and stays 0). The
  capacity-timeout / RunawayGuard / node_timeout / budget_exceeded partials
  join the 0→1 class.
- Documented behavior change: a HITL-paused run is not reflected in the daily
  ledger until it resolves. Ledger-row keying moves from terminal-day (now-date)
  to run-start-day (`started_at` UTC date). org-first write order. `run_count`
  semantics.
- Terminal-state writes: `failed`/`cancelled`/`eval_failed` record their
  partial spend (run_count 1). The pre-component-read failure writes NOTHING.
- p95 finalization latency is a documented OPERATOR-GUIDANCE target with a
  generous bound (< 1 s as guidance, measured and reported), NOT a CI-failing
  assertion. The p95 guidance explicitly includes the created-at SUM latency —
  the index-served SUM predicate is part of the measured finalization cost (for
  LIMIT-CONFIGURED orgs; a no-limit org — the default — runs no SUM). The
  refusal-SUM scaling ceiling is NAMED: the SUM is O(day-runs) per
  terminalization for a limit-configured org — index-served but LINEAR in the
  org's day-run count; named next to the p95 in the operator guide; a revisit
  is DATED if any limit-configured org grows past the measured bound.
- The ledger has no `run_id` column — no rewiring of
  `get_cost_report`/`get_anomalies`/dashboard beyond the enumerated changes.
- Ledger-consumer set reconciled: `get_cost_report`, the dashboard cards, the
  Quality report, AND `get_anomalies` AND the CSV EXPORT endpoint ALL read
  `OrgDailyRunCount` — the clamped-day/refused-day handling applies to ALL of
  them, not just `get_anomalies`. Heavy-refusal days are ALSO excluded from the
  RATIO/baseline calculations: the exclusion predicate is `refused_spend_usd /
  (total_spend_usd + refused_spend_usd) > 0.5` — the threshold PINNED at 0.5,
  with a zero-guard (`if total_spend_usd + refused_spend_usd == 0: skip`).

### 4.7 The probe — the verification vehicle (replaces the reconciliation)

The probe is a CURRENT, SMALL deliverable in PR A — the lightweight successor
to the cut reconciliation. It is NOT a reconciliation — it is a canary for
systemic drift, not a per-key equality audit. Its mechanism count is BUDGETED
and must NOT grow in v1.

**What it does.** A scheduled job on the SAQ system-worker cadence, every 5
minutes, `retries=0` (pinned). The probe CronJob is declared with
`unique=True`. Org enumeration runs OUTSIDE RLS (NO `set_rls_org`), exactly
like the `probe_state` KV access — pinned + a fixture; `set_rls_org` applies
ONLY to the per-org runs/ledger queries the probe runs. It iterates orgs —
each org in its own `try/except` so ONE org's RLS/DB failure cannot abort the
whole sample — and, for each org, samples the N=50 most recent terminal runs
(status per the canonical terminal-status set, §4.2, ordered by `started_at`
desc, `cost_breakdown IS NOT NULL`), served by a new `(organisation_id,
started_at)` index (`ix_runs_probe`; a plain blocking CREATE INDEX on the
hottest table — flagged for production sizing). For each sampled run it
compares `total_cost_usd` to Σ component.amount_usd (Decimal, over the
breakdown's non-marker component entries). Marker-bearing runs (`total_clamped`
JSON containment) are SKIPPED with a counter. No table is written. The sample
query is EXPLAIN-asserted. The EXPLAIN gate asserts INDEX USE + ORDERING for
the `(organisation_id, started_at)` index, NOT "no sequential scan". The EXPLAIN
gate runs under `SET enable_seqscan=off` scope (so it asserts CAPABILITY, not
planner whim) wrapped in a try/finally so `RESET enable_seqscan` runs even when
the EXPLAIN raises.

Per-run exception isolation: in addition to the per-org `try/except`, the org
loop wraps EACH sampled run in its own `try/except` — one malformed
`amount_usd` string drops only that run from the sample. Per-org
statement/query timeout: each org's runs/ledger queries run under a per-org
statement/query timeout, so ONE stalled org's query cannot block the whole
cadence — the org is logged + skipped, and the heartbeat does not advance for
that org.

**The consecutive-cadence probe state is PERSISTED, KEYED, SINGLE-INSTANCE, and
ADJACENCY-AWARE.** The `probe_state` JSON blob in `system_config`:

- (d) Keying (pinned): a COMPOSITE KEY `probe_state:<org_id>` on the GLOBAL
  `system_config` table (a single `(key, value)` KV row per org). RLS clarity:
  the global `system_config` table has NO RLS — `set_rls_org` applies ONLY to
  the runs/ledger queries. The blob VALUE is stored via `json.dumps` into the
  existing `system_config.value` column (the default is the house KV-blob
  pattern).
- (a) Blob shape: `{last_cadence_mismatch_runs: [run-ids], last_cadence_at:
  <iso ts>}`.
- (b) Temporal adjacency: a persisted mismatch list whose `last_cadence_at` is
  older than ~2× the cadence does NOT count toward the next cadence's
  "consecutive" — an outage gap RESETS the consecutive count. Implemented by
  comparing `now - last_cadence_at > 2 × cadence` before consulting the
  persisted state. The adjacency-vs-staleness line is CONSISTENT: a probe that
  misses 1–2 cadences is NOT yet stale (still within 3× cadence), but any
  in-progress 2-consecutive chain IS reset by the adjacency rule.
- (c) Single-instance: the probe is pinned as SINGLE-INSTANCE — the SAQ
  system-worker is deployed single-instance AND the `probe_state`
  read-modify-write uses a POSTGRES ADVISORY LOCK (`pg_advisory_xact_lock` on a
  deterministic key derived from the `probe_state:<org_id>` key) held for the
  read-modify-write of the blob — the same discipline the sibling system-worker
  jobs use. Rolling-deploy overlap: two instances process cadences
  SEQUENTIALLY (the advisory lock serializes the read-modify-write) but can
  DOUBLE-ADVANCE `probe_state` — ACCEPTED (bounded, makes the trigger MORE
  sensitive briefly; the lock prevents state corruption).
- (e) Run-ids are DIAGNOSTIC ONLY (pinned): stored for operator inspection;
  NEVER compared across cadences. ONLY the per-cadence COUNT matters for the
  2-distinct rule.
- Key accumulation: `probe_state:<org_id>` keys for DELETED orgs accumulate
  (the cleanup is a dated follow-up). Write-rate ceiling: ONE write per org per
  5-min cadence — it scales with org count; noted as a scaling ceiling. Serial
  per-cadence wall-clock budget is documented for many-org installs. At
  ~150–200 orgs the serial cadence exceeds the 5-min cadence and the probe's
  OWN heartbeat pages while it is STILL RUNNING — the operator guide names the
  org-count so the page is not chased as a probe bug.
- When a cadence passes CLEAN, the org's blob is RESET (mismatch list cleared,
  `last_cadence_at` advanced). The rule survives deploys/restarts.

**The org-row existence WATCH — DECIDED shipped.** For each sampled run with
`ledger_written = true AND ledger_refused_at IS NULL`, the probe asserts the
org ledger row EXISTS for the run's date with ONE batched query per org:

```sql
SELECT organisation_id, run_date, total_spend_usd, clamped
FROM org_daily_run_counts
WHERE organisation_id = :org AND team_id IS NULL AND run_date IN (:dates)
```

- Assertion: the org row must EXIST for every sampled run's date (team-carrying
  AND null-team runs alike), and `org_row.total_spend_usd >= Σ sampled-run
  totals for that date` (sufficiency; skipping `clamped=true` dates).
- Date filter + per-date grouping in Python: groups the sampled runs BY DATE
  (`run.started_at.astimezone(UTC).date()` — the SAME key the executor writes),
  then compares each date's Σ sampled totals against the returned row.
- Clamped-day skip: the per-date grouping skips a date when the ORG row is
  clamped (the stored boolean, §4.6) — a clamped day is a known anomaly, not a
  missing-row signal.
- This is a WATCH signal, NOT a hard-gate trigger input: absence or
  insufficiency increments `modulo_cost_probe_missing_ledger_row_total` + a
  Watcher baseline line. It is NOT subject to a 2-cadence hard rule and does
  NOT feed the rollback trigger.
- DELETED: the team-row query, the `probe_double_insert_detected_total`
  counter, and the 2-cadence missing-row memory. The claimed detection scope is
  restated honestly: the probe detects (a) `total == Σ` on run rows (HARD
  trigger input) and (b) org-row absence/sufficiency (WATCH signal).
  PROPORTIONAL UNDER-RECORD and a SAME-DAY WRONG-`run_date` are PARTIAL
  coverage — the named-gap is stated honestly.

Name collision disambiguated: the probe's "clamped-run skip" and "clamped-day
skip" are DISTINCT from the DELETED reconciliation skip taxonomy of the same
names. Clamped-run sample-minimum counting, pinned: marker-bearing (clamped-
skip) runs DO count toward the ≥5-sample minimum; they ARE excluded from the
org-row WATCH assertion.

**Signals — semantics pinned HERE:**

- `modulo_cost_probe_mismatch_runs_total` — number of sampled runs where
  `total != Σ`.
- `modulo_cost_probe_total_eq_mismatch_total` — 1 when a sample flagged ≥1
  mismatching run (sample-level).
- `modulo_cost_probe_clamped_skip_total` — clamped (marker) runs skipped.
- `modulo_cost_probe_missing_ledger_row_total` — sampled runs whose org ledger
  row is absent (or insufficient), per the org-row WATCH semantics. A WATCH
  counter — NOT a hard-gate trigger input.
- `modulo_cost_probe_last_success_ts` — a heartbeat: a log timestamp + a gauge
  recording the last successful sample. The Daily Watcher treats the probe as
  STALE after 3× the cadence (15 min). The heartbeat is the LAST SUCCESSFUL
  SAMPLE. It advances on ANY cadence where the probe ran ≥1 org successfully —
  INCLUDING a 0-eligible-run org. It does NOT advance on an ALL-ORGS-FAILED
  cadence or on a ZERO-ORG install (the staleness alert is gated on ≥1 org
  existing). An OUTER-LOOP bug aborts the whole job → the heartbeat stays flat
  → the stale alert fires. The stale alert is DEDUPED and ACKABLE. There is NO
  sub-threshold band — every mismatch is counted.

One structured log per sample: `cost_probe_sample` with org, sample size,
mismatches, clamped-skips, missing-ledger-rows, and the heartbeat timestamp. No
retry semantics.

**What it catches and what it deliberately does not.** It catches systemic
drift — a live change that breaks `total == Σ` across many runs (HARD trigger),
OR a systemic org-ledger-write failure (row absent / under-suffering across
sampled keys, per the org-row WATCH assertion). It does NOT guarantee
per-`(org, team, date)` strictness: the ledger may drift for the accepted
classes (refused, deferred, two-row overcount, pre-deploy residue) and the
probe samples rather than exhaustively checks. Per-key strictness is
deliberately not guaranteed — accepted, the ledger is a report (§9.3).

**Rollback trigger backing — THE CANONICAL TRIGGER — ONE rule, normative HERE.**
The automated rollback state machine is a DELIBERATE SHIPPED FEATURE — DECIDED,
not re-litigated; a future cut is a NAMED accepted gap in §9.3, not a silent
removal. The rollback fires when ANY of these holds:

1. (a) The probe rule: a cadence samples ≥5 runs (per org) AND ≥2 DISTINCT
   mismatching runs appear in ≥2 CONSECUTIVE cadences.
2. (b) Duplicate-terminal flood (threshold PINNED): a burst of
   `cost_ledger.duplicate_terminal` logs — more than 5 DISTINCT runs raising
   `duplicate_terminal` within 10 minutes. A single run's duplicate log does not
   fire. The BULK-REDISPATCH false-fire source is NAMED: a deploy/restart of
   the SAQ worker, a queue-retry burst, or a PRE-DEPLOY COHORT of paused runs
   re-dispatched together can each raise a burst WITHOUT a ledger-write
   regression. The flood trigger carries a POST-DEPLOY/WORKER-RESTART COOLDOWN
   — a `duplicate_terminal_suppressed_until` timestamp on the worker (set on
   worker start / a version-change marker) SUPPRESSES the flood trigger for the
   first 15 minutes after a worker restart or version change. The cooldown's
   STORAGE + EVALUATION are PINNED: the timestamp is written to `system_config`
   (a plain global table, NO RLS — the same no-RLS discipline as `probe_state`);
   the flood detector reads it and SKIPS the flood trigger while
   `now < suppressed_until` — the cooldown suppresses the TRIGGER only, never
   the log or the counter. The write is IN-MEMORY-PLUS-PERSISTED; a
   pre-existing persisted value from a crashed worker EXPIRES by wall-clock
   comparison. A SOFT Watcher line fires when the flood counter RISES DURING a
   suppression window.
3. (c) Spend-limit-misfire ticket (a WATCH signal, NOT a hard-gate input): a
   human-confirmed spend-limit-misfire incident is an operator page surface for
   the Watcher baseline, NOT a machine-gate rollback input.

The org-row WATCH signal (`missing_ledger_row`) is NOT a hard-gate input — it
has no 2-cadence rule and no threshold beyond its counter + Watcher line.

The single-probe-rule sub-rule itself pins: the 2-distinct requirement is
WITHIN EACH cadence — ≥2 DISTINCT mismatching runs per cadence. This ONE rule
excludes a single deterministic flaky node; catches a systematic bug (≥2
distinct affected runs persist across 2 consecutive cadences); states the
quiet-org limitation explicitly (an org with <5 sampled runs per cadence NEVER
triggers the rollback — counter/log only). It is the ONLY post-deploy
verification vehicle — it replaces the reconciliation smoke in the Daily
Watcher baseline. A mismatch is never silent: the counters + log fire
regardless of threshold.

**Scope honesty.** The probe samples only RECENT runs with a breakdown; it does
not touch pre-migration history (`cost_breakdown IS NULL` runs are not
recomputed and are excluded by the predicate). The probe runs unconditionally;
a sample passing is the steady-state expectation. The heartbeat turns a
silently dead probe into a stale alert.

---

## 5. Agent self-report contract (dogfood)

File: `Repos/devtools/dogfood/pipeline-scripts/_common.py`.

### 5.1 Read opencode's REAL session cost — exception-safe

Each E2B sandbox is fresh per NODE (one sandbox per sandbox_agent node
invocation), so the opencode user-data DB (`opencode.db`) contains only THIS
node's session(s). Query read-only, WAL-safe after the run.
`read_opencode_cost` is the ONLY cost source. Every sqlite op (open + query) is
wrapped in `try/except sqlite3.Error` with `busy_timeout`; any failure → `None`
(fail-soft).

The reader SUMS ALL sessions — correct ONLY because each E2B sandbox is created
fresh per NODE INVOCATION. If the sandbox cardinality ever changes, this
argument and the reuse predicate MUST be revisited.

REUSE PREDICATE (epoch-UTC + ±5min skew): MIN/MAX(started_at), normalized to
ONE epoch base (seconds / ms / ISO asserted by a UNIT smoke). If any session
timestamp is OUTSIDE [run_start_epoch - 300, now_epoch + 300], treat the DB as
REUSED: log cost_components_reuse_detected + omit the report, unless
COST_SANDBOX_REUSE_ABORT=true (hard abort; DEFAULTS FALSE). run_start comes
from the sandbox-side /tmp/run-start flag (same clock domain as sessions);
uniformly-shifted clocks are NOT flagged; a non-uniform NTP step can push a
legit session out → one-time estimate undercount, fail-safe; run_start=None
(crash path) SKIPS the predicate (fail-open; pin_failed is the backstop).
WITHIN-WINDOW REUSE IS AN ACCEPTED BOUNDED OVERCOUNT: two back-to-back runs in
the SAME sandbox window (±300s) sum two sessions into one report; the per-node
clamp bounds the overcount. There is NO runtime detection if the sandbox
cardinality silently changes (doc-flag only).

IN-BAND SHAPE ASSERTION: the queried column NAMES and VALUE TYPES (cost/tokens
INTEGER|REAL, started_at INTEGER|REAL|ISO) are verified at read time; drift →
cost_components_schema_drift + the FATAL MINIMAL DICT `{"schema_drift": true}`
(NOT bare None: the fatal class must page). The assertion is REAL CODE. DB
birth-time is diagnostic-only. NEVER invokes the opencode binary.

SCHEMA-DRIFT WIRE SIGNAL: on a FATAL in-band drift (a renamed/missing column,
or a wrong VALUE TYPE), the reader returns the MINIMAL result dict
`{"schema_drift": true}` — NO cost keys, and NOT bare None. The transport is
PINNED: the devtools reader is the sandbox-side detection point; the BACKEND
counter increment lives in `_enrich_union` (§4.2), which reads the node
output's `schema_drift` key and increments
`modulo_cost_opencode_schema_drift_total` gated on `pin_failed == false` AND
on the node being sandbox-by-map. The golden-reference diff stays CI-side
(§5.3). A transient sqlite error (busy/locked/corrupt-file) returns None —
fail-soft, distinguishable from the fatal-drift class.

MAGNITUDE SANITY BAND (direction-SPLIT): a summed session cost outside the
plausible per-run band [$0.0001, $50] is handled BY DIRECTION:

- ABOVE $50 → `cost_components_schema_drift` (log, direction=
  "cost_out_of_band_high", with the cost context) + the report is KEPT but
  CLAMPED AT THE BAND CEILING: cost = min(raw, MAX_REPORTABLE_BAND_USD). The
  above-band value is CLAMPED AT $50 (bounded) and the report carries the
  `model_cost_out_of_band_high": true` marker. THE RAW VALUE IS PRESERVED ON
  THE WIRE: the pre-clamp value rides as `model_cost_raw_usd` on the result
  dict, so node_runner's `_extract_reported_cost` derives the flags from the
  TRUE raw and the audit value survives end-to-end. This reader-side clamp is
  now REDUNDANT but HARMLESS — node_runner's `_extract_reported_cost` is the
  SINGLE enforcement point and re-applies the SAME constant at the backend
  extraction boundary. A legitimately-long real session that GENUINELY bills
  >$50 is ALSO clamped at $50.
- BELOW $0.0001 → the EXISTING sub-floor path: report omitted (the estimate
  applies) + a direction="cost_out_of_band_low" log with cost context — this
  is the normal cheap-session corner, NOT a canary by itself.

The band is a CANARY, not a hard None for the high side. COST-UNIT DRIFT
WITHIN THE BAND (a within-band drift that keeps the summed value inside
[$0.0001, $50]) is NOT caught by the band: the suspect-report RATIO is the only
cross-check and it is SKIPPED for sandbox nodes with no recorded tokens —
stated; the golden-reference schema check + the dated E2B pricing/unit drift
follow-up are the backstops.

EFFECTIVE SELF-REPORT FLOOR: the BAND FLOOR $0.0001 (1e-4) is the EFFECTIVE
reporting floor — reports below it are omitted here; the MAX_REPORTABLE_USD_MIN
1e-6 floor at node_runner is the extraction-level lower bound. The asymmetry is
a STATED asymmetry.

`db_path` resolves from THIS process env (XDG_DATA_HOME else HOME +
/opencode/opencode.db) — shared by the template-cache exclusion and boot-step
cleanup.

### 5.2 write_output forwarding contract

`write_output` forwards the model-cost fields verbatim: `model_cost_usd`
(clamped), `model_cost_raw_usd` (pre-clamp), `model_cost_out_of_band_high`
(when above-band). The 2-way preference (agent's written value vs computed) and
explicit-0 handling are pinned. On a FATAL schema drift the reader emits the
FATAL minimal dict `{"schema_drift": true}` (NO cost keys, NO token_usage) and
`write_output` forwards it onto the node output — so a renamed/missing column
PAGES the backend counter instead of silently dropping the model cost. A pin
fallback legitimately drifts the schema and is already signaled by
`pin_failed`, so the schema-drift counter must not double-page that class.

The `pin_failed` flag-file transport: a shell WRAPPER in the `agent_command`
boot step installs opencode, records `pin_failed` into
`/tmp/opencode-pin-failed`, then executes the real command (wrapper EXIT trap
covers both success and crash paths). The backend reads the flag file VIA THE
E2B FILESYSTEM API at the SAME time it reads the node output (the sandbox
object is in scope in `_sandbox_agent`), and emits `pin_failed: true` into the
node output dict as a SERVER-side-derived key — overwriting any producer value.
The anti-spoof claim: "the `pin_failed` flag is server-derived via the E2B
filesystem API, never trusted from the producer wire". The template-cache
exclusion excludes BOTH the opencode user-data/opencode.db path AND
`/tmp/opencode-pin-failed`, and the boot step cleans them — a stale cached DB
would flag every run as reused and silently drop model cost.

### 5.3 Golden-reference diff (CI-side)

The golden-schema diff stays CI-side: a golden reference of the opencode DB
schema is diffed against the production-deployed opencode version; a drift is
CI-failing. The confirmed golden version must EQUAL the production-deployed
version; the comparison SOFTENS to a logged skip during the bypass window. The
path smoke is PR C's HARD MERGE GATE, re-run against the MERGE CANDIDATE
commit; if the live smoke cannot run for > 3 days due to the auth-key/sandbox-
infra prerequisite, the gate SOFTENS to the per-PR static check + the fail-loud
scheduled smoke, LOGGED, AND an EXPLICIT OPERATOR ACK.

### 5.4 Assertions

The per-node-cap assertion on the reader is DOWNGRADED:
`_effective_self_reported_cap()` EXISTS on the reader but is SUBSUMED BY THE
BAND CLAMP — the reader clamps any above-band value at $50, so the per-node cap
is never the binding constraint on the reader; the assertion is reworded to
assert the constant exists, not that it is load-bearing there. `MAX_REPORTABLE_BAND_USD`
devtools default 50.0 is the SAME shared constant the backend extraction uses.

---

## 6. API + frontend (PR B)

This section is delivered in PR B. It is retained here so the reference is
complete; it is not normative for the backend data-model/engine PRs (A1/A2).

### 6.1 Cost report + consumer handling

- `get_cost_report`, the dashboard cards, the Quality report, `get_anomalies`,
  AND the CSV EXPORT endpoint all read `OrgDailyRunCount`. Clamped dates are
  skipped from the ratio columns; refused-day spend treated as absent/0 for the
  SPEND figures with the refused amount read from `refused_spend_usd`; the
  annotations rendered. Heavy-refusal days are excluded from the
  RATIO/baseline calculations (§4.6 predicate).
- Refused visibility: the refused query (`SUM(refused_spend_usd)` over the
  report period) reads the LEDGER column, NOT the runs table — the 90-day
  refusal-visibility horizon is GONE. Refused dual-write non-additivity: org
  + team must NOT be summed.
- `refused_total_usd` reads the `refused_spend_usd` ledger column;
  `clamped_total_usd` is NOT additive with `total_spend_usd`.

### 6.2 Run detail + admin views

Run-detail shows the breakdown (component snapshots), not just a total. The
per-node column is relabelled "Model cost" with the Σ ≠ total caveat + the
single-authority `cost_usd`. `AdminCostBreakdownView.vue` renders the
annotations; `CostComponentsView.vue` (manifest `sidebar_order: 4`) is the
components CRUD UI with the dead-report_key cap-slot chip. The frontend
`TERMINAL_STATUSES` copy is reconciled to the canonical set (§4.2) to include
`eval_failed`. The `node_token_usage` serialization truncation bound + the
union display clamp + the `model_cost_display_usd` render ship in PR B.

---

## 7. Testing strategy

### 7.1 Unit tests (backend)

- The engine test plan in §2.5 (grammar, validation, param registry,
  attack strings, clamp boundaries).
- The extraction clamp tests (§4.1): band clamp at the boundary (raw 6000 →
  clamped at the band with `was_clamped=true`, `out_of_band_high=true`; exactly
  at the band → not out-of-band; just below → ok), non-finite/NaN/Inf → None,
  bool/negative/zero → None, sub-floor → not reported, raw-source precedence,
  schema-drift flag → None, order-independence of the band vs per-node clamps.
- Settings knobs (§1.2/§2.5): ge-bounds, the ordering invariant, the
  floor-vs-clamp guard (floor exactly at the band → fatal; $49.99 → passes;
  the DECIMAL-TYPED coercion), the knob-below-band guard, the min-capped
  effective values, the Settings-load raise in every process.
- Seeds idempotency (§3.2): system-context enumeration (an RLS-scoped
  enumeration returns no orgs while the system-context enumeration seeds every
  org — negative test), per-org `set_rls_org`, soft-deleted names skipped.
- CRUD validation (§3.1): reserved names/report_keys → 422, the
  rate_fallback registry → 422, formula cross-field validation → 422, the
  org-scoped 409 (both the pre-check AND the IntegrityError race backstop), the
  org cap → 422, the last-calculated guards → 422.
- The once-and-reference grep-asserts: the token→section scan over the
  COMMITTED distilled spec; the terminal-status set grep; the
  `tokens_estimated` vs `Run.total_tokens` distinction; the dead-param absence
  greps; the enforcement-reader NULL comparison + no-`coalesce` fetch greps;
  the `upsert_daily_run_count` no-callers grep; the
  `_clamp_reported`/`_extract_reported_cost`/`_clamp_to_ceiling` code-equivalent
  greps (join at A2 — a grep whose target does not exist yet is a SKIP with a
  logged note, never a CI failure).
- A `Settings()`-load-raise inside the never-fail envelope → legacy fallback +
  the labelled signals, never a crash (fixture).

### 7.2 Integration tests

- Migration round-trip: alembic upgrade heads + the five pinned columns
  asserted + the NULLS NOT DISTINCT index + the dropped constraint + the
  MIGRATE-role owner + RLS enabled + `test_migrated_schema_matches_orm_metadata`
  parity. The round-trip provisions the NOLOGIN role + the superuser URL
  against testcontainers and re-runs the superuser privilege smoke.
- The concurrent-first-of-day fixture (two concurrent NULL-team terminals can
  no longer both insert org rows).
- The refusal-boundary fixtures: the org fixture ("day one-cost below the limit
  accepts; at the limit with the current run included, refuses EXACTLY at the
  configured limit, never at half"), the team fixture ("a team at its limit
  refuses exactly at the team limit; an org-passing/team-failing run refuses
  and writes NEITHER row"), and the no-limit fixture ("an org with NO configured
  limit accepts every terminal run; the team check is skipped when
  `team_limit IS NULL`"). The no-limit org runs NO SUM.
- The duplicate-dispatch / `ledger_written` FOR UPDATE fixture; the
  refuse-then-raise re-dispatch fixture (permanent refusal, no catch-up); the
  NULL-team double-write guard fixture; the refused-amount upsert + clamp
  fixture (incl. a clamped-duplicate merge fixture); the p95 latency measured
  (operator-guidance, not CI-failing).
- The cross-tenant isolation test extends to `cost_components` (RLS).

### 7.3 BDD

Feature files for the components CRUD surface (org-scoped 409s, the
last-calculated guards, the reserved-key 422s) and the enforcement refusal
boundaries. Delivered with PR B.

### 7.4 CI / governance

The token→section scan runs from A1 over the committed distilled spec; the
code-equivalent greps join at A2 (scoped to "fire only when the target
exists"). The upgrade-heads assert scans every shell script under `deploy/`
with the pattern `\bupgrade\s+(?!heads\b)\S+` — `upgrade heads` (plural, the
VERIFIED deploy form) is allowed exactly; any non-plural `upgrade X` form incl.
the literal revision-id pin is REJECTED, with a POST-MATCH allowlist (comment
lines / named prose tokens only). The deploy smoke asserts it.

---

## 8. Docs / operator guide

Reference-not-restate (§8 is the doc-authoring home). The PRD 8.10 section and
the operator guide MAY state the thresholds (the band ceiling, the trigger, the
no-dip conditions) as user-facing prose — they are the OPERATOR-FACING surface,
not the once-and-reference home. They must REFERENCE this spec path
(`docs/design/multi-component-cost-tracking.md`) and may not INVENT new
thresholds.

Operator guide must state:

- The ledger is a REPORT, not a source of truth; per-key ledger strictness is
  deliberately not guaranteed — the ledger may drift. The probe is the canary.
- Refusal is PERMANENT — nothing clears `ledger_refused_at`; refused spend does
  not catch up after a limit raise; the counter + the refused-day report line +
  the `refused_spend_usd` column are the visibility. Operators should NOT
  expect catch-up.
- The refused/`finalize_deferred` operator query (`ledger_written = false`
  terminal runs, with `ledger_refused_at` in the SELECT). The paused-then-
  cancelled class runs NO finalize — enumerable ONLY via the operator query.
- The never-paused cross-process forfeiture class (`cost_components_partial_spend_lost`
  log, run_id only) is NOT in the operator query.
- A deployment-window flag for migration 0066 (two blocking CREATE INDEX + the
  constraint drop in one transaction; budget a maintenance window at
  production scale). `ix_runs_probe`/`ix_runs_refusal` are plain blocking
  CREATE INDEX on the hottest table.
- The raw `cost_estimate_usd`/`model_cost_raw_usd` node-output surface is
  AUDIT CONTEXT — every NEW consumer of that surface is audited in code review
  so a new money formatter cannot read the raw surface un-clamped.
- Lowering `MODULO_MAX_RATE_USD` does NOT affect existing components — the knob
  moves the write-path boundary only; existing rows are still evaluated at
  finalization at their stored rate.
- Floors at/above ~$1 silently disable NORMAL reporting (real sessions bill
  $0.004–$0.04) via the SOFT `missing_self_report{component}` line ONLY — no
  boot failure, no hard signal. A flat report is not chased as a bug.
- The duplicate-terminal flood false-fire sources (deploy-restart / queue-retry
  / pre-deploy cohort / stale_run_recovery_sweep re-drive) and the 15-minute
  cooldown + the SOFT line on counter rise during suppression.
- The serial-cadence self-fire org-count for the probe; the write-rate ceiling;
  the p95 finalization-latency guidance (index-served SUM, no-limit org runs no
  SUM); the refusal-SUM scaling ceiling.
- The behaviour-changes list (deploy-day): totals shift for runs that carried
  `cost_estimate_usd` (honest transition); ledger-row keying moves to
  run-start-day; org-first write order; `run_count` semantics (incl. the
  cancelled-class 0→1 scoped to runs WITH A PRIOR PAUSE); the pre-deploy
  overcount; the ~1-week post-deploy anomaly re-check; no post-deploy
  suppression (deploy-day spikes fire, expected/dismissible).

---

## 9. Rollout / migration

### 9.1 Migration — no data loss

New migration `backend/src/modulo/db/migrations/versions/0066_cost_components.py`
(down_revision `0065_reconcile_staging_schema`). The REAL tree: the live head is
`0065_reconcile_staging_schema` (down_revision `0064_merge_heads_0037`) — the
staging-schema-drift reconciliation migration merged on main (PR #618).
`0064_merge_heads_0037` is itself a MERGE migration (down_revision
("0037_add_scheduled_reports_created_by", "0037_break_glass_enforcement")) that
merged the two live 0037_* heads. `0063_merge_all_heads.py` carries the
revision id "0036_merge_all_heads" but is NOT the head. 0066 is a NORMAL
migration off the ACTUAL head, deployed via the EXISTING `upgrade heads`
(plural) — NO pin. 0066 was originally authored as 0065 off
`0064_merge_heads_0037`; after #618 landed on main first it was renumbered to
0066 with down_revision `0065_reconcile_staging_schema`. 0066 off
`0065_reconcile_staging_schema` replaces it as a head — the tree STAYS at 1
head. The migration docstring states the REAL tree + the step-0 command.

**Step-0 head assertion — a REAL, WRAPPER-PINNED check** (runs BEFORE writing
0066, and POST-authoring):

```powershell
$alembic_heads = uv run python -m alembic heads
if ($LASTEXITCODE -ne 0) { throw 'uv/alembic command failed (not a head mismatch)' }
$head_lines = @($alembic_heads | Where-Object { $_ -match '^\S+ \(head\)' })
if (($head_lines | Select-String '0065_reconcile_staging_schema').Count -eq 0) { throw 'wrong migration head' }
if ($head_lines.Count -ne 1) { throw 'migration tree is not single-head' }
```

The wrapper (1) checks `$LASTEXITCODE` FIRST so a uv/alembic COMMAND failure is
distinguished from a WRONG-HEAD failure, and (2) asserts the head output is
EXACTLY ONE line over HEAD-MARKER LINES ONLY (a two-head tree fails). PRE
asserts `0065_reconcile_staging_schema` (the current head); POST asserts
`0066_cost_components` (the new sole head). `check-migration-heads.ps1` still
runs for its duplicate-prefix + multi-head-warning role.

**Migration DDL maintenance-window flag:** 0065 runs in ONE migration and holds
the ACCESS EXCLUSIVE lock for TWO blocking CREATE INDEX (`ix_runs_probe`,
`ix_runs_refusal`) + the `op.drop_constraint` + the NULLS NOT DISTINCT
re-create — terminal finalizations stall for the build duration. Fine at
dogfood scale; budget a MAINTENANCE WINDOW at production scale.

The migration steps (upgrade):

0. Step-0 head assertion (above) + `SET search_path` pinned at the top of 0066.
1. **NULLS NOT DISTINCT org-row pre-flight — the FIRST step of 0066, BEFORE
   any `add_column` AND before `create_table`** (a failing pre-flight leaves
   NOTHING behind):

   ```sql
   SELECT organisation_id, run_date, COUNT(*) FROM org_daily_run_counts
   WHERE team_id IS NULL
   GROUP BY organisation_id, team_id, run_date HAVING COUNT(*) > 1;
   ```

   If ANY duplicates exist, FAIL LOUDLY with the counts and the remediation
   (merge/delete the duplicate org rows first, then re-run). The merge policy:
   SUM `total_spend_usd`, SUM `run_count`, keep the LATEST `clamped`, with
   clamped-OR semantics (`clamped = true` if ANY source row was clamped), AND
   the merged total is CLAMPED to the column ceiling before insert. Ship a
   `scripts/` helper (`backend/scripts/merge_org_daily_run_count_dupes.py`)
   that mirrors the pre-flight query and performs the merge under org-scoped
   RLS discipline (`set_rls_org` per org in its own `session.begin()`, never a
   superuser session). Production pre-flight GATE: run the pre-flight against
   production BEFORE PR A; PR A does not ship until the production pre-flight
   returns zero duplicates (or the documented merge has been applied).

   The constraint-name catalog assertion:
   `SELECT conname FROM pg_constraint WHERE conrelid =
   'public.org_daily_run_counts'::regclass AND contype = 'u';` — the relation
   is SCHEMA-QUALIFIED and the gate PINs `SET search_path`. The authoritative
   copy of the `uq_org_daily_run_counts_org_team_date`-present check lives IN
   THE MIGRATION, immediately before `op.drop_constraint`, as an in-migration
   guard (a `SELECT conname FROM pg_constraint ... WHERE conname = ...` that
   RAISES on mismatch).

   The RLS-owner assertion (pre-flight): `SELECT to_regclass('public.cost_components')`
   — NULL pre-migration (expected, branch a: "expected pre-migration, skip with
   a log"); when NOT NULL (branch b) assert the app role is NOT the owner
   (`pg_has_role(relowner, :app_role, 'USAGE')` is FALSE) — org confinement can
   SILENTLY VANISH if migrations run as the app role. Both queries use
   `to_regclass` on the SCHEMA-QUALIFIED name.

2. **`op.drop_constraint("uq_org_daily_run_counts_org_team_date",
   "org_daily_run_counts", type_="unique")`** — the EXISTING unique is a
   CONSTRAINT, and `op.drop_index` on it FAILS on Postgres. Then recreate the
   unique index as NULLS NOT DISTINCT named `uq_org_daily_run_counts`:

   ```python
   op.create_index(
       "uq_org_daily_run_counts",
       "org_daily_run_counts",
       ["organisation_id", "team_id", "run_date"],
       unique=True,
       postgresql_nulls_not_distinct=True,
   )
   ```

   (PG15+, the repo pins PG16.) The ORM `OrgDailyRunCount.__table_args__` uses
   the SAME named index construct so `test_migrated_schema_matches_orm_metadata`
   passes.

3. **`SET ROLE modulo_migrate` BEFORE `op.create_table("cost_components", ...)`,
   `RESET ROLE` AFTER** (the migration connects via `DATABASE_ADMIN_URL` per
   env.py:52-55; `modulo_migrate` is a NOLOGIN role — a NOLOGIN role IS
   activatable via SET ROLE by a superuser). `op.create_table("cost_components",
   ...)` — columns per §1.2 (`formula` NULLABLE), kind check constraint +
   partial unique indexes + `ix_cost_components_org_enabled_sort`. The
   SUPERUSER ASSUMPTION is stated explicitly next to env.py:52-55:
   `SET ROLE modulo_migrate`, `CREATE POLICY`, and `GRANT` ALL require
   superuser (or membership) — the whole 0066 run depends on `DATABASE_ADMIN_URL`
   actually being superuser- or owner-privileged. This is TESTED by the
   pre-flight smoke.

   **MIGRATE-role deploy-wiring (first-class PR A deliverable):** for
   `modulo_migrate` to CREATE the table it needs CREATE on the public schema
   (PG15+ does not grant CREATE to PUBLIC by default) AND REFERENCES on
   `organisations` (the new table's org FK references it). `bootstrap_role.py`
   grants both on every boot (the REFERENCES grant guarded by `to_regclass`
   because the pre-alembic bootstrap runs on a fresh DB where `organisations`
   does not exist yet), and 0066 re-applies both idempotently right before
   `SET ROLE` so a fresh-DB migration works regardless of bootstrap state.

   **The POST-CREATE ownership assertion sits between `create_table` and the
   RLS-enable step:** after create_table, 0066 asserts the created table's
   owner is `modulo_migrate`, not the app role
   (`SELECT relowner::regrole::text FROM pg_class WHERE oid =
   to_regclass('public.cost_components')` — a hard failure inside the migration
   if it ran as the app role). Running 0066 as the superuser violates the
   "never superuser" rule and FAILS the assertion; running as the app role
   fails it too (the app role must NOT own `cost_components`).

4. The FIVE pinned migration-surface columns:
   `runs.cost_breakdown` (JSON), `runs.ledger_written` (Boolean NOT NULL
   server_default 'false', NO backfill), `runs.ledger_refused_at`
   (DateTime(timezone=True) NULL), `org_daily_run_counts.clamped` (Boolean NOT
   NULL server_default 'false'), `org_daily_run_counts.refused_spend_usd`
   (Numeric(14,6) NOT NULL server_default '0'). ORM columns in the SAME PR.
   NO `ix_runs_deferred`. NO `cost_component_snapshot` column. NO
   `ledger_deferred` column, NO sweep index. NO reconciliation indexes.

5. **`op.create_index("ix_runs_probe", "runs", ["organisation_id",
   "started_at"])`** — serves the probe's sample query. plain `CREATE INDEX`
   pinned (BLOCKING, non-CONCURRENTLY — the maintenance-window flag).

6. **`op.create_index("ix_runs_refusal", "runs", ["organisation_id",
   "created_at"])`** — serves the refusal-window SUM predicates (§4.6), created
   UNCONDITIONALLY (v1 M6: no `IF EXISTS`, no conditional branch; parity-safe
   with the unconditional drop in downgrade). plain `CREATE INDEX` pinned.

7. **Enable RLS** (the `0008_rls_pipeline_folders` pattern), Postgres-only:
   `ALTER TABLE cost_components ENABLE ROW LEVEL SECURITY;` + `CREATE POLICY
   rls_org_isolation ON cost_components USING (organisation_id =
   nullif(current_setting('app.organisation_id', true), '')::uuid)`.

8. **Direct TABLE grant — PINNED:** `GRANT SELECT, INSERT, UPDATE, DELETE ON
   cost_components TO PUBLIC` — POSTGRES-ONLY (RLS is the confinement). The
   PUBLIC grant is role-agnostic by design; a CI assertion proves every write
   path calls `set_rls_org`. RLS-owner precondition: `cost_components` RLS
   confinement depends on the app role NOT owning the table.

9. NO `runs.total_cost_usd` widen. Stays `Numeric(14,6)`; ORM mirror
   identical. `Run.total_tokens` ALREADY EXISTS — no migration/ORM change.
   Overflow unreachable (per-node clamp + band + dynamic `rate_usd` bound +
   non-finite guard + flat clamp + the daily ledger clamp + the refused-amount
   accumulation clamp). NO `deploy_ts` recording, NO `cost_reconciliation_runs`
   table. NO daily-spend-limit DB CHECK constraints.

**Multi-backend + role handling:** SQLite — the `add_column`s run through
`render_as_batch`; the NULLS NOT DISTINCT unique index, the Postgres-only run
indexes, RLS, and the GRANT are skipped on non-Postgres (enforcement via
cross-field validation). MariaDB DEPRECATED, dropped from the parametrized
suites. Postgres — no `FORCE ROW LEVEL SECURITY` in the migration (owner
bypasses RLS); the integration testcontainers DO force RLS (add
`cost_components` to the FORCE list).

**`downgrade()`** — batch-aware: drop RLS policy/RLS, drop `cost_components`
(with indexes), drop `cost_breakdown` + `ledger_written` + `ledger_refused_at`,
drop `org_daily_run_counts.clamped`, drop
`org_daily_run_counts.refused_spend_usd`, recreate the plain (non-NULLS-NOT-
DISTINCT) `org_daily_run_counts` unique constraint
`uq_org_daily_run_counts_org_team_date` and drop the `uq_org_daily_run_counts`
NULLS NOT DISTINCT index, drop `ix_runs_probe`, drop `ix_runs_refusal`
UNCONDITIONALLY (parity-safe). Verify ROUND-TRIP. Partial-apply note: a re-run
after remediation must handle partially-applied `add_column`s (or downgrade
first) — re-entrancy covers ONLY the five add_columns; `create_table` /
`op.drop_constraint` / the CREATE INDEX steps / the RLS enable/policies / the
GRANT are NOT idempotent. The deploy runbook documents the ACTUAL recovery for
the drop_constraint→index window — ONE pinned sequence: assess the partial
state; complete the remainder of 0066's DDL MANUALLY in one transaction (with
the conditional constraint drop FIRST); `alembic stamp 0066`; verify
`alembic heads` reports `0066_cost_components` as the sole head and
`test_migrated_schema_matches_orm_metadata` passes.

**No data backfill of any kind.** Historical runs keep `cost_breakdown = NULL`
(total unchanged). Pre-migration runs are never recomputed.

**Report history is preserved — the ledger is NEVER purged.** The run-retention
cron continues to purge RUNS only; `org_daily_run_counts` ledger rows are NOT
purged. The manual purge endpoints also purge runs only. `refused_spend_usd`
rides the ledger row, so refused visibility survives the run purge — the
refused-day report line covers the FULL period. Ledger accumulation is bounded
in SIZE (one row per `(org, team, date)`) and documented as indefinite; an
archival/rollup task is a dated follow-up beyond a 5-year horizon. The lock-step
ledger purge and the `purged` skip no longer exist.

### 9.2 Feature flag

Reuse `admin_cost_breakdown` (tier `team`); the description is updated in BOTH
`feature_flags.py` and `core/seed_data/catalog.py`. The whole components surface
(GET list + write routes) is gated with `require_feature("admin_cost_breakdown")`.

### 9.3 Rollout / backward-compat window + rollback triggers

**The no-dip conditions — normative HERE (canonical).** PR C (devtools) lands
FIRST so dogfood runs report real cost from day one of PR A's deploy. The two
conditions: (1) the pin resolves, AND (2) C is re-deployed to production. If
either fails, the documented dip reappears. There is NO alert guarantee for the
C-redeploy gap — the `missing_self_report{component}` line is CONTEXT, the
enforcement-SUM / probe are the CONTROL, and the dip is BOUNDED only in the
ABOVE-BAND direction (the band ceiling caps the magnitude). A cheap NON-CI
process gate covers the C-redeploy gap: the deploy runbook verifies C is LIVE
in production BEFORE A's deploy (a one-line script asserting `model_cost_raw_usd`
appears in a recent run's node output; fallbacks: assert the scheduled smoke's
recorded production version string is current, or run a synthetic sandbox run;
if neither is available, the runbook step is logged as SKIPPED, not silently
passed).

**Honest transition statement (canonical).** On deploy, run totals WILL shift
for runs that carried `cost_estimate_usd`, toward accuracy. The empty-set
legacy fallback is byte-identical for runs that did NOT carry
`cost_estimate_usd`; for runs that did, it shifts by exactly that term. The
fallback DE-TRUSTS `cost_estimate_usd` (server-verified wall-clock only), so
the fallback is attacker-safe. Pre-migration historical runs are never
recomputed.

**Ledger-is-a-report statement (canonical).** The ledger is a REPORT, not a
source of truth. Per-key ledger strictness is deliberately not guaranteed — the
ledger may drift for the accepted classes (refused, deferred, two-row overcount,
pre-deploy residue). The probe is the canary, not an audit.

**Accepted one-time transition artifacts (canonical list):**
- The cancelled-class 0→1 `run_count` shift (streamed cancelled runs WITH A
  PRIOR PAUSE now write a ledger row + increment `run_count`); the
  capacity-timeout partials join it; a NEVER-PAUSED in-flight run cancelled
  cross-process stays 0 (no row, no counter, `cost_components_partial_spend_lost`
  log).
- The pre-deploy paused-run two-row overcount (pause-time row + terminal row;
  one row with doubled spend when same-day, two rows when spanning midnight).
- The pre-migration-resume map policy (a pre-deploy paused run's nodes are
  classified per the post-deploy graph at first finalization).
- The token undercount for pre-deploy paused runs (pre-gate tokens never
  persisted → `llm_tokens` undercounts ONCE).
- The streamed-cancelled partial spend now enters the enforcement SUM and, if
  it pushes the day over, the refusal is permanent.
- The ~1-week post-deploy anomaly re-check confirms the deploy-day spike
  normalized and no post-deploy drift is masked.
- The deployment-day spikes fire (expected/dismissible) — no post-deploy
  suppression.
- The pre-deploy paused runs are NOT backfilled (`ledger_written`/breakdown).

**Post-deploy verification (canonical).** The automated Daily Watcher smoke
reads the PROBE — the ONLY post-deploy verification vehicle; there is NO
reconciliation smoke. The Watcher baseline covers: the `missing_self_report`
SOFT line (the control is the SOFT-controlled enforcement-SUM / probe signals);
the enforcement-SUM streamed-cancelled partial-spend entry + the budget chain
(cancelled spend counts against the daily limit, permanent refusal); the
band-clamped-total enforcement consequence; the `limit_refused` vs
spend-decline separation (watch signal); the enforcement-window NON-switch
divergence + the refusal-window = enforcement-window keying; the two cancelled
classes; clamped-day annotation. The baseline note is documentation, not a
mute (pages may still fire).

**Rollback triggers.** The rollback state machine is the canonical §4.7 rule
(the probe rule with its sampled-runs minimum, its within-each-cadence
distinct-mismatch requirement, and its consecutive-cadence persistence;
deterministic-flaky excluded; quiet-orgs never trigger; the
clamped-runs-count-toward-minimum rule; the persisted `probe_state` + the
temporal-adjacency reset) + the duplicate-terminal flood with the
post-deploy/worker-restart cooldown. The org-row WATCH + the spend-limit-misfire
ticket are WATCH signals, not hard-gate inputs. This section references the
canonical trigger; it does not restate its numeric tokens.

### 9.4 Delivery order + verification

Delivery order is C → A → B; PR A is the CRITICAL PATH. PR A is split into A1
and A2 (the DEFAULT delivery shape — the 4-PR chain C → A1 → A2 → B). Each half
is its own PR; each is merged manually once green + approved, branching each
subsequent PR off updated main.

1. **PR C (devtools dogfood) — LANDS FIRST:** `_common.py` real self-report via
   `read_opencode_cost` ONLY; the THREE-FIELD wire (`model_cost_usd` clamped +
   `model_cost_raw_usd` pre-clamp + `model_cost_out_of_band_high` when
   above-band), and `write_output` forwards all three verbatim; the pinned
   `round(...,6)` banker's-rounding behavior; the canonical binary-PATH pin
   (install to a sandbox toolchain path, NOT `-g`; version-check the exact
   resolved path; the `pin_failed` flag-file transport written on both success
   and failure paths); the template-layer cache excludes the opencode.db path
   AND `/tmp/opencode-pin-failed`; the npm package-name pre-flight check; the
   in-band column-name schema-shape + VALUE-TYPE assertion; the direction-split
   magnitude band; `_effective_self_reported_cap()` uses the constants default;
   the `_SandboxReuseError` contract; version + shape CI smoke (per-PR static +
   scheduled live, fail-loud on a missing auth key); the epoch-UTC reuse
   predicate with ±5min skew; the within-window-reuse accepted-bounded-overcount
   note; the production version record + compare (log). The smoke pinning the
   resolved `_opencode_db_path()` is a HARD MERGE GATE, re-run against the
   MERGE CANDIDATE commit, with a time-boxed 3-day bypass + an EXPLICIT
   OPERATOR ACK. Conductor merges manually after CI/build (devtools has no
   autonomous pipeline). Backward-compatible with the current backend.

2. **PR A1 (backend data-model + engine) — off updated main, started by running
   the step-0 head assertion FIRST:** migration 0066 off `0065_reconcile_staging_schema`
   (step-0 wrapper-pinned); role-safe + multi-backend; `ledger_written` no
   backfill; `ledger_refused_at`; NO `ledger_deferred`/sweep index;
   `org_daily_run_counts.clamped` + `refused_spend_usd` (ORM columns in the
   same PR); the NULLS NOT DISTINCT unique index + the duplicate pre-flight as
   the FIRST step + the `scripts/` merge helper + the PRODUCTION PRE-FLIGHT
   GATE with the constraint-name catalog assertion + the RLS-owner gate via
   `to_regclass('public.cost_components')` + the post-create ownership assertion
   inside 0066 (owner `modulo_migrate`) + the MIGRATE-role deploy-wiring
   (`SET ROLE modulo_migrate` around `create_table`) + the SUPERUSER PRIVILEGE
   SMOKE (pre-flight gate step; mirrors 0066's actual execution order) +
   `SET search_path` pinned + the ORM `__table_args__` parity; `formula`
   NULLABLE for `self_reported`; the daily-spend-limit app-side 422 bounds only;
   NO `ix_runs_deferred`; `ix_runs_probe` + `ix_runs_refusal` (blocking CREATE
   INDEX, maintenance-window flag); NO reconciliation indexes, NO
   `cost_reconciliation_runs`, NO `deploy_ts`, NO lock-step ledger purge; NO pin
   — `upgrade heads`, single-head tree, upgrade-heads CI assertion; the
   `cost_controller/breakdown` package (formula engine + param registry +
   telemetry builder + aggregate + clamp helpers + metrics); the three new
   Settings knobs (min-capped effective values + Decimal typing + the
   ordering-at-load invariant + the boot self-test incl. the BOOT-FATAL
   floor-vs-clamp guard) + the band-ceiling knob; node_runner tri-state + clamp
   + floor + the BAND ceiling clamp at the extraction boundary
   (`_extract_reported_cost`) + the `model_cost_out_of_band_high` marker + the
   `cost_components_out_of_band_high` magnitude log + `model_cost_raw_usd` +
   the separate `model_cost_display_usd` clamped display field + the
   `_clamp_reported` re-clamp at enrichment (defense-in-depth, wired by A2) +
   server-side wall-clock authority + `pin_failed` server-side flag extraction;
   seeds (system-context enumeration + per-org `set_rls_org`, idempotent,
   soft-deleted names skipped); CRUD for cost_components
   (`db/crud/cost_component.py` + `api/routes/cost_components.py`) with
   reserved-key validation, org-scoped 409, org cap, and the save-time 422
   validation as the ONLY validation path; the ORM model parity (Run +
   OrgDailyRunCount + CostComponent); the once-and-reference token→section
   grep-assert CI scanning the COMMITTED distilled spec
   (`docs/design/multi-component-cost-tracking.md` — committed in THIS PR, CLEAN:
   zero version markers, pointer-only prose, §0–§11 numbering preserved
   verbatim); PRD + config-ref + ADR 019. The A1/A2 machine-check window: the
   code-equivalent greps are scoped to "fire only when the target exists" — a
   grep whose target (executor/ledger/probe) does not exist yet (pre-A2) is a
   SKIP with a logged note, never a CI failure.

3. **PR A2 (executor + ledger + probe) — off updated main:** the finalization
   block (§4.2) with the cumulative write-back, the enriched union, the
   accumulated-set signature + return workstream (the THREE direct terminal
   writes routed through `finalize_cost`), the terminal-only ledger with the
   `ledger_written` FOR UPDATE guard + bounded retry + the reduced
   terminalize-without-ledger escape, the limit-refused vs write-failure split
   + the refusal-window SUM semantics (§4.6) + the shared created-at day-start
   helper + the enforcement-reader NULL comparison + the check-both-limits-then-
   write ordering + the NULL-owner team-write guard + the refused-amount
   persistence, the near-ceiling check, the legacy-fallback DE-TRUSTS +
   flat-clamp-with-marker, the `run_ws.py` divergence reconciliation + the
   `stale_run_recovery_sweep` RLS fix, the `eval_failed` direct-write preserves
   the terminal field set, and THE PROBE (§4.7). The code-equivalent greps join
   here (their targets now exist) and the greps harden.

4. **PR B (API + frontend) — off updated main:** the §6 surface (run detail
   breakdown, the admin components view + annotations renderer, the cost-report
   consumer handling, the MCP sanitize, the flag description in both sources,
   the view fixes).

5. Dated/owned follow-ups — the §11 register is the SINGLE source:
   remove the empty-set legacy fallback + the `cost_estimate_usd` artifact
   (2026-11-30, ADR 019 analysis by 2026-10-31 — BOTH revisited when PR A
   ships; the fallback-removal date must be re-derived from the ACTUAL deploy
   date); remove the `E2B_SANDBOX_USD_PER_HOUR` env fallback (2026-12-31); E2B
   pricing drift check (2026-10-15); calibrate the 0.1× suspect-report threshold
   (1-2 weeks post-C); PR-C smoke bypass re-assert (3 days); the
   `probe_state:<org_id>` keys for deleted orgs cleanup (2026-12-31); the
   union-amplification decision window (2026-12-31); `cost_breakdown`
   unboundedness on BULK RunResponse surfaces (2026-12-31); display-clamp
   consolidation (2026-12-31); the never-paused pause-time partial-total
   persistence (2026-12-31); ledger archival/rollup (2031-01-01);
   `get_anomalies` re-check (~7 days post-deploy); the reuse predicate as a
   follow-up CUT candidate (noted, not changed now); the meta/governance
   consolidation (push TYPE-LEVEL asserts over grep asserts).

---

## 10. Effort estimate (T-shirt sizes)

The honest headline is 1×L+ + 3×M+ + 4×M — DERIVED FROM THE 8-ROW TABLE. The
two alternative framings are DELETED (they disagreed with the table's own
arithmetic). The table has EIGHT rows: 1×L (the executor, L+ expected-to-leak),
3×M+ (data model/migration, integration+BDD, docs), and 4×M (engine, probe,
dogfood, frontend).

| Workstream | Size | Scope (one-line pointer — the body is normative) |
|---|---|---|
| Cost formula engine + aggregate + unit tests | **M** | §2.1–§2.5 + §4.5 |
| Data model + migration + RLS + seeds + CRUD | **M+** | §1.1–§1.2 + §3.1–§3.3 + §9.1 (the MIGRATE-role wiring + the SUPERUSER PRIVILEGE SMOKE + the round-trip NOLOGIN-role provisioning make it heavier than a plain M) |
| Executor enrichment/ledger block | **L** | §1.5–§1.6 + §4.2–§4.6 |
| The probe | **M** | §4.7 |
| Dogfood self-report (`_common.py`) | **M** | §5.1–§5.4 |
| Frontend (run-detail + admin view + manifest + i18n) | **M** | §6.2 |
| Integration + BDD tests | **M+** | §7.2–§7.3 |
| Docs (PRD, config-ref, API ref, operator guide, product-map, ADR 019, TESTING.md, devtools note) | **M+** | §8 |

**Total size: 1×L+ + 3×M+ + 4×M**, with the executor SPLIT — the executor L
bucket is the expected overrun surface; integration+BDD is the secondary overrun
risk. The A1/A2 default split (C → A1 → A2 → B) stays; PR A (A1/A2) is the
CRITICAL PATH. No hard gate between PRs — the CONTROL is the SOFT-CONTROLLED
enforcement-SUM / probe signals; C is backward-compatible and lands first. The
one hard gate that DOES exist: PR C's path smoke. p95 finalization latency is a
documented operator-guidance target — measured and reported with a generous
bound, NOT a CI-failing assertion.

---

## 11. DECISION REGISTER (one page)

The register is a PURE DECISION / OWNER / WINDOW table — the anti-abuse
layering and the dated follow-ups stay normative ONLY in the body (§4.5, §9.4).
Every row is a pointer: body section + one-line rationale; the body is the sole
normative text. Every entry has ONE owner and a stated removal window. The
register is capped at 15 rows, ONE LINE per row.

| Decision | Owner | Removal window | Pointer (body = normative) |
|---|---|---|---|
| Extraction authority + band ceiling + unconditional flags — `_extract_reported_cost` (node_runner) is the single extraction authority; the band clamp at the backend boundary, `min()` never replace, universal (any producer); both flags written unconditionally; the raw rides via `model_cost_raw_usd`; the `schema_drift` wire key is read here (returns None on the flag) and the counter is incremented in `_enrich_union` (the frozen-map layer) gated on `pin_failed == false` AND sandbox-by-map provenance AND terminal-only | product | permanent (v1) | §4.1/§4.2/§4.5/§5.1/§5.3 |
| Raw audit value + display clamps — `basis.raw_reported` = the raw for audit; the display clamps (raw_reported, node-output `model_cost_display_usd`, union, MCP run-list) keep the money formatter safe; the `RunResponse` breakdown serializer is the PINNED home of the raw_reported 1e6 display clamp; the raw webhook/MCP surface is audit context | product | permanent (v1) | §1.3/§4.1/§6.1 |
| Stored-union ONE rule + enrichment re-clamp — output PRESENT+carrying → OVERWRITE; PRESENT-but-lacking → pop the value + sibling flags; ABSENT → re-clamp-keep with fallback flags; `_clamp_reported` is defense-in-depth with the explicit-None input | product | permanent (v1) | §4.2/§4.5/§7.2 |
| Clamp stack + anti-abuse layering — the ONE normative table (§4.5): floor, per-node, band, non-finite, flat total + marker, per-entry string, display, daily-ledger + `clamped`, refused-amount accumulation clamp; `total == sum` with the single marker exception | product | permanent (v1) | §1.3/§4.5 |
| Settings knobs + boot guards — the knobs (ge-bounds, min-capped effective values, Decimal typing); ordering-at-load; boot self-test with the BOOT-FATAL floor guard + knob-below-band guard; the SINGLE first-finalization near-ceiling check (24h constant, per-instance, recalibrated to also fire at 10x the default knob); the `rate_fallback` registry | product | permanent (v1) | §1.2/§2.4/§2.5 |
| Ledger + refusal semantics — terminal-only, `ledger_written`/`ledger_refused_at` under FOR UPDATE; refusal PERMANENT + `refused_spend_usd` column (permanent visibility); refusal-window SUM (created-at, current-run-excluded + explicit add, NULL-limit short-circuit, `ix_runs_refusal` index-served, EXPLAIN RANGE-SELECTIVITY gate); run_count semantics; daily-ledger clamp + `clamped`; the `get_cost_report`/dashboard/Quality/`get_anomalies`/CSV-export consumers | product | permanent (v1) | §4.6/§6.1/§9.3 |
| Cancellation classes + terminal handlers — all terminal handler return shapes pinned (accumulated sets, 4-tuple, by-reference outputs, empty-accumulator normalization); the THREE direct terminal writes routed through `finalize_cost`; the cancelled-class 0→1 scoped to runs WITH A PRIOR PAUSE; never-paused cross-process forfeiture surfaced ONLY via the `cost_components_partial_spend_lost` diagnostic log; the paused-then-cancelled class is operator-query-only | product | permanent (v1) | §4.2/§9.3 |
| Probe + WATCH + heartbeat + trigger — canary not audit, mechanism budget fixed; the FIVE signals; the org-row WATCH is a WATCH counter not a trigger input; `probe_state:<org_id>` persistence (no RLS, advisory lock, temporal adjacency, single-instance, diagnostic run-ids); heartbeat/staleness ackable; the CANONICAL trigger (the probe rule + the duplicate-terminal flood as hard-gate inputs, with the cooldown + SOFT-line-on-rise as the mitigation; the org-row WATCH + the spend-limit-misfire ticket as WATCH signals; quiet-orgs never trigger); the duplicate-flood false-fire sources named; the probe EXPLAIN gates run under `SET enable_seqscan=off` in a try/finally | product | permanent (v1) | §4.7/§9.3 |
| Migration + tree + step-0 + gates — the REAL tree (head `0065_reconcile_staging_schema`); `0066_cost_components` off it; PR-A step 0 = the WRAPPER-PINNED assert (checks `$LASTEXITCODE` first + asserts EXACTLY ONE head line with the revision-id-pattern filter; pre asserts `0065_reconcile_staging_schema`, post asserts `0066_cost_components`); the upgrade-heads assert (the broadened negative pattern with the POST-MATCH allowlist exemption over every shell script under `deploy/`); the NULLS NOT DISTINCT unique + duplicate pre-flight FIRST + merge helper + constraint-name catalog gate (authoritative copy IN the migration) + RLS-owner `to_regclass` gate + `SET ROLE modulo_migrate` around `create_table` via `DATABASE_ADMIN_URL` + the SUPERUSER PRIVILEGE SMOKE + the partial-apply recovery (manual DDL with the conditional constraint drop FIRST + `alembic stamp 0065`) + the maintenance-window flag | product + infra | permanent (v1) | §9.1/§7.2/§9.4 |
| Seeding — the org ENUMERATION runs in system context with NO `set_rls_org` (a negative test mirrors the probe's fixture); `set_rls_org` per seed transaction; idempotent, soft-deleted names skipped | product | permanent (v1) | §3.2/§7.1 |
| Self-report contract (devtools) — `read_opencode_cost` (fail-soft, band floor 1e-4, the direction-split band, the reuse predicate, the REAL in-band shape assertion writing the `schema_drift` wire flag, plus the drift-vs-transient discriminator); `write_output` 2-way preference + explicit-0 + the drift dict carries NO token_usage; the canonical-path pin + `pin_failed` flag-file transport read SERVER-SIDE via the E2B filesystem API (never the producer wire) + the template-cache exclusion of BOTH the opencode.db path AND `/tmp/opencode-pin-failed`; the golden-schema diff (CI-side, OWNED + DATED for the opencode release cadence); the path smoke = PR-C hard gate | product | permanent (v1) | §5.1–§5.4 |
| Effort + delivery shape — 1×L+ + 3×M+ + 4×M (derived from the 8-row table; the alternative framings are DELETED); A1/A2 is the DEFAULT delivery shape — the 4-PR chain C → A1 → A2 → B | product | permanent (v1) | §10/§9.4 |
| Review target + spec — the closing review runs against the DISTILLED-SPEC DRAFT; the plan body is history after it; the distilled spec is derived via a NAMED EDITORIAL TASK (not a mechanical copy) with the human gate; the human gate re-reads the clamp stack + refusal SQL + trigger + terminal set IN THE DERIVED SPEC; the spec-clean pin (zero markers, pointer-only, §0–§11 preserved); the first-CI fix-up budget | product | permanent (v1) | §1.2/§9.4 |
| Machine-check + register self-description — once-and-reference MACHINE-CHECKED: the grep-assert scans the COMMITTED distilled spec + the code (ONE reconciled list) + the shipped PRD/operator-guide; the governance machine is KEPT (its value is post-commit over the committed spec + code equivalents); the §11 register is self-clean by construction; the PLAN-side scan is a MANUAL gate until the spec commits; the meta/governance push toward type-level asserts + grep consolidation is a dated follow-up | product | permanent (v1) | §1.2/§7.1/§7.4 |
| Dated/owned follow-ups (SINGLE source, §9.4 point 5) — fallback removal 2026-11-30 (ADR 019 analysis by 2026-10-31; BOTH revisited when PR A ships); `E2B_SANDBOX_USD_PER_HOUR` fallback 2026-12-31; E2B pricing drift check 2026-10-15; suspect-report 0.1× calibration 1-2 weeks post-C; PR-C smoke bypass re-assert 3 days; probe_state deleted-org cleanup 2026-12-31; union-amplification decision 2026-12-31; `cost_breakdown` unboundedness on BULK RunResponse surfaces 2026-12-31; display-clamp consolidation 2026-12-31; ledger archival/rollup 2031-01-01; `get_anomalies` re-check ~7 days post-deploy; golden re-confirm per pin bump (owned); pause-time partial-total persistence 2026-12-31 | product + ops + infra | dated (windows as stated) | §9.4/§11 |
