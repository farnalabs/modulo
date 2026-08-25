# Progressive Autonomy: Measurement Framework & Initial Study

> **FAR-395** – Evidence-based progressive autonomy is Modulo's key wedge against
> incumbents (GitHub Actions, GitLab CI). Those tools gate *human review* on/off;
> they do not *record* the autonomy decision per change, nor learn from outcomes.
> This document defines how we measure progressive autonomy and publishes an
> honest first longitudinal view.

**Status:** Framework v1 (specification + initial instrumentation). Data view is
**illustrative** where real instrumentation does not yet exist – flagged
explicitly below.

---

## 1. The claim we are making

Modulo lets each pipeline choose a default **autonomy level** for its HITL gates:

| Level | HITL gate behaviour |
|---|---|
| `manual_approval` | Gate halts; a human must review and approve/reject. |
| `notify_on_complete` | Gate is auto-approved at runtime but emits a notification for observability. |
| `fully_autonomous` | Gate is skipped entirely; no halt, no notification. |

The *progressive* claim: autonomy should **rise** for change classes that
consistently ship clean and **fall** after a defect escapes human review. A
team that runs `manual_approval` for a pipeline, sees a streak of clean merges,
and promotes it to `notify_on_complete` – then back to `manual_approval` if a
bad change slips through – is practising *evidence-based* progressive autonomy.

**Why this is defensible vs incumbents:** GitHub/GitLab expose "require review"
as a static branch-protection boolean. They produce no cross-agent evidence
record of *what autonomy was granted, to whom, and what happened next*. Modulo's
audit chain (`audit_events`, SHA-256 linked per org) is already the substrate
for that evidence record. This document turns it into a measurement instrument.

---

## 2. What is recorded today (as-built)

Discovered by reading the codebase at implementation time:

| Signal | Recorded? | Where |
|---|---|---|
| Pipeline autonomy **level *configured*** | ✅ | `pipeline.autonomy_level_changed` audit event (`api/routes/pipelines.py:_maybe_audit_autonomy_change`) |
| HITL gate **decisions** (claimed/approved/rejected/expired) | ✅ | `hitl_gate_*` events; `core/hitl_manager` |
| HITL gate **eval result** (LLM-judge before interrupt) | ✅ | `hitl_gate.eval_result` telemetry |
| **Per-run effective autonomy** actually applied | ❌ *new* | **Now emitted**: `run.autonomy_level_applied` (`core/run_context/autonomy_telemetry.py`) |
| **Gate-fire** (human path taken vs bypassed) per run | ❌ *new* | **Now emitted** as `gate_outcome` in the same event |
| **Defect-escape** (autonomous change later reverted/fixed) | ❌ | Not yet – see §6 |
| Autonomy level in the analytics fact table | ❌ | `run_daily_facts` has no autonomy column |

The two gaps closed by this change (`run.autonomy_level_applied`) are the
minimum required to *measure* the claim. Everything else in §6 is a roadmap
item, not yet instrumented.

---

## 3. Measurement framework

### 3.1 Primary events (the evidence record)

`run.autonomy_level_applied` – emitted once per HITL gate evaluation at runtime:

```json
{
  "event_type": "run.autonomy_level_applied",
  "resource_type": "run",
  "resource_id": "<run_id>",
  "payload_json": {
    "gate_id": "<gate_id>",
    "autonomy_level": "manual_approval | notify_on_complete | fully_autonomous",
    "gate_outcome": "skipped | auto_approved | fired",
    "pipeline_id": "<pipeline_id>",
    "human_only": false
  }
}
```

`gate_outcome` is the discriminating field:
- `skipped` → gate bypassed (fully_autonomous)
- `auto_approved` → notify_on_complete
- `fired` → human path taken / interrupt raised (manual_approval, or
  human_only override)

The event joins to `run_daily_facts` on `run_id` (and to `audit_events` for the
config-change history on `pipeline_id`), giving us a per-run, per-gate,
per-autonomy-level evidence stream.

### 3.2 Derived metrics

| Metric | Definition | Question it answers |
|---|---|---|
| **Autonomy grant rate** | fraction of gate-evaluations with `gate_outcome ∈ {skipped, auto_approved}` | How much human review are we actually removing? |
| **Autonomy by change class** | group the above by pipeline / folder / node type | Which change classes are safe to run autonomous? |
| **Gate-fire rate** | fraction of evaluations with `gate_outcome = fired` | How often does a human actually get pulled in? |
| **Defect-escape rate (autonomous)** | autonomous changes later reverted/fixed ÷ autonomous changes shipped | When we grant autonomy, how often do we regret it? |
| **Defect-escape rate (human-only)** | same denominator for `manual_approval` changes | Is human review actually catching more than it misses? |
| **Autonomy delta per pipeline** | `(autonomy grant rate now) − (grant rate N periods ago)` | Is autonomy *progressing* as promised? |
| **Promotion/demotion events** | `pipeline.autonomy_level_changed` events over time | Are teams actually practising progressive autonomy? |

The wedge metric is the **defect-escape comparison**: *autonomous vs
human-only*, same change class, same period. If autonomous escape rate is
within tolerance of human-only, the progressive-autonomy claim is defensible.

### 3.3 Data model summary

- **Event stream** (real-time, append-only, tamper-evident): `audit_events`
  carrying `run.autonomy_level_applied` + existing `pipeline.autonomy_level_changed`.
- **Dimensioned history** (survives 90-day run purge, ADR 020): `run_daily_facts`.
  Recommended next-step column: `autonomy_level` (nullable, sourced from the
  event stream at fact-write time) so the analytics query surface can bucket by
  autonomy directly without re-joining the audit chain.
- **Outcome join** (roadmap): a `defect_escape` flag derivable from
  reverted/fixed runs correlated to their originating autonomous run.

---

## 4. Initial longitudinal view (HONEST)

> ⚠️ **Data-availability disclaimer.** At implementation time the
> `run.autonomy_level_applied` event did **not** exist, so no real
> per-run autonomy history has been collected. The figures below are
> **illustrative placeholders** showing the *shape* of the report, computed
> from the *current* static config signals we *do* have (pipeline
> `default_autonomy_level` distribution). They are **not** measured outcome
> data and must not be quoted as evidence.

### 4.1 What we can state today (real, static)

From the autonomy-resolution model (`core/run_context/autonomy.py`) and the
config-change audit event, the *configured* autonomy distribution across an
org is observable *now* via `pipeline.autonomy_level_changed`. The *applied*
distribution requires the new event and a collection window.

### 4.2 Illustrative longitudinal table (PLACEHOLDER – not real data)

| Period | Autonomy grant rate | Gate-fire rate | Autonomous defect-escape | Human-only defect-escape |
|---|---|---|---|---|
| W1 | _placeholder_ | _placeholder_ | _placeholder_ | _placeholder_ |
| W2 | _placeholder_ | _placeholder_ | _placeholder_ | _placeholder_ |
| W3 | _placeholder_ | _placeholder_ | _placeholder_ | _placeholder_ |

*These cells will be populated once ≥1 collection window of
`run.autonomy_level_applied` events exists. The collection is fail-open and
zero-dependency, so backlog filling begins the moment this ships to an
environment with live runs.*

### 4.3 What "good" looks like (targets, not measurements)

- Autonomy grant rate **trends up** over time as change classes prove out.
- Autonomous defect-escape stays **within tolerance** (target ≤ 1.5×) of
  human-only defect-escape for the same class.
- Promotion events (`manual → notify → autonomous`) **outnumber** demotions.

---

## 5. The defensible wedge (argument)

1. **Incumbents gate; they don't record.** GitHub/GitLab branch protection is a
   static boolean. There is no event saying "this change was auto-merged under
   elevated autonomy and here is what happened." Modulo's `audit_events` chain
   *is* that record, now extended to per-run autonomy.
2. **Cross-agent evidence is the moat.** Autonomy decisions in Modulo are made
   by agents (context-setter recommends, pipeline defaults, hitl-gate
   eval-before-interrupt). The evidence record spans *every* agent touchpoint,
   not a single CI job – so the learning signal is richer than a human-review
   log could ever be.
3. **Progressive autonomy is measurable, therefore governable.** Because each
   grant is an event and each outcome is joinable, a customer can *prove* to
   their auditor that autonomy only rises on evidence and falls on failure.
   That is a compliance story incumbents cannot tell.
4. **Fail-open by design.** Instrumentation never blocks a run
   (`autonomy_telemetry` is wrapped in try/except, logged, dropped). Measuring
   autonomy cannot become a production risk – which is itself a trust argument.

---

## 6. Roadmap (not yet instrumented)

| Item | Size | Notes |
|---|---|---|
| `autonomy_level` column on `run_daily_facts` | S | Enables direct analytics bucketing without audit-chain joins. |
| Defect-escape derivation | M | Correlate reverted/fixed runs to originating autonomous run. |
| Promotion/demotion dashboard | M | Surface `pipeline.autonomy_level_changed` over time per pipeline. |
| Autonomy-by-change-class report | M | Group grant/escape rates by pipeline/folder/node type. |
| Per-run telemetry in node output | XS | Echo effective autonomy into `node_telemetry_json` for in-run visibility. |

---

## 7. Implementation notes (this change)

- **New module:** `backend/src/modulo/core/run_context/autonomy_telemetry.py`
  – `emit_autonomy_telemetry()` (fail-open, session-factory driven).
- **Wired at:** `core/pipeline_engine/node_runner.py:make_hitl_gate_fn._hitl_gate`
  – emits `skipped` / `auto_approved` (when the gate is bypassed) and `fired`
  (when the human path is taken).
- **Registered:** `run.autonomy_level_applied` added to
  `core/product_analytics/metrics_constants.VALID_EVENT_TYPES` so it flows
  through product-analytics ingest.
- **Tests:** `backend/tests/unit/core/run_context/test_autonomy_telemetry.py`.
