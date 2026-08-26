# Evals

Modulo evaluates agent/node outputs against **eval definitions**. An eval is a
deterministic (or model-mediated) check attached to a pipeline node. This
document explains the eval types, why the eval system is deliberately
**non-circular**, and how to use **human-authored eval sets** – the trustworthy
correctness path.

## Eval types

| Type | Mechanism | Asserts | Trustworthy for correctness? |
|------|-----------|---------|------------------------------|
| `regex` | Pattern match on an output field | **Shape** (text matches a pattern) | No – shape only |
| `json_schema` | JSON Schema validation | **Shape / structure** of an object | No – shape only |
| `custom_function` | User-supplied callable | Whatever the function checks | Depends on the function |
| `llm_judge` | An LLM grades the output | **Soft** quality/similarity signal | No – circular + injection-prone |
| `human_set` | A registered, versioned, **human-authored** assertion set | **Correctness** (semantic invariants + business rules) | **Yes** |

## Why evals must be non-circular

The eval system is built to avoid **eval circularity** – the trap where an LLM
grades an LLM and a passing score means nothing.

### Deterministic guardrails only catch shape, not correctness
`regex` and `json_schema` confirm an output is *well-formed*. They cannot tell a
**correct** answer from a confidently-wrong one. A classification agent can emit
perfectly valid JSON with `category: "billing", priority: "low"` and pass a
`json_schema` check even when a human would flag that combination as wrong. Shape
checks are necessary but never sufficient.

### `llm_judge` is a soft signal, vulnerable to injection
`llm_judge` asks a model to score another model's output. Two problems:

1. **It is circular.** LLM-judging-LLM inherits the same failure modes it is
   meant to catch. A passing `llm_judge` score is a *weak, approximate* signal,
   not evidence of correctness.
2. **It is injection-prone.** An injection payload embedded in the agent output
   can instruct the judge to return a passing score. The engine mitigates
   instruction leakage with structural delimiters and a guard instruction
   (`_GUARD_INSTRUCTION` in `eval_engine/__init__.py`), but that only neutralises
   *instruction* leakage – it cannot guarantee the judge's *correctness*
   assessment is right.

`llm_judge` is useful as a **soft** signal (e.g. flagging candidates for human
review) but must never be the sole gate for a claim of correctness.

### Human-authored eval sets are the trustworthy path
`human_set` runs a **fixed, versioned artifact** – a list of deterministic
assertion functions written and reviewed by a person. They are not
model-mediated, so they cannot be talked into a false pass, and they assert
*correctness properties* (business rules, consistency, semantic invariants) that
shape checks cannot express. This is the path to use when you need to trust an
eval result.

## Using a human-authored eval set

A human-authored set is selected at eval time by name. Create an eval definition
with:

```json
{
  "pipeline_id": "<pipeline-uuid>",
  "node_id": "<node-uuid>",
  "name": "demo classification correctness",
  "eval_type": "human_set",
  "config_json": {
    "set_name": "demo_classification",
    "field": "output"
  },
  "failure_behaviour": "block"
}
```

* `set_name` – the registered set name (see `HUMAN_EVAL_SETS` in
  `core/eval_engine/human_eval_sets.py`). Required.
* `field` – the (dotted) output field the assertions resolve and parse. Optional;
  defaults to `output`.
* `version` – advisory. The registry holds a single active version per name;
  consumers should pin the set name they validated against.

At run time the engine looks up the set and runs **every** assertion. The eval
passes only if all assertions pass; the `detail` lists any failing assertion
names. A `block` behaviour raises `EvalBlockedError` on failure.

### Shipped set: `demo_classification` (`v1`)
A representative agent task: a support-message classification agent emits JSON
`{category, priority, confidence?}`. The human-authored assertions are:

1. `valid_json` – the output field parses as a JSON object.
2. `required_keys` – `category` and `priority` are present.
3. `category_enum` – `category` ∈ {billing, technical, general}.
4. `priority_enum` – `priority` ∈ {low, medium, high}.
5. `consistency` – **business rule**: a billing issue is never `low` priority; a
   technical outage is always `high`.
6. `no_extra_keys` – no hallucinated keys leak into the contract.

Assertions 1–4 are things `json_schema` *could* express; assertion 5 (the
consistency rule) and the overall composition are what make the set a
**correctness** check rather than a shape check, and what no `llm_judge` can be
trusted to enforce.

## Authoring a new human eval set

1. Write each assertion as a pure function
   `(output: dict, config: dict) -> dict` returning
   `{"passed": bool, "score": float | None, "detail": str}`.
2. Register it via `register_human_eval_set(HumanEvalSet(...))` in
   `core/eval_engine/human_eval_sets.py`.
3. **Bump the version** whenever an assertion's semantics change. Consumers pin a
   version, so an edit never silently changes a contract.

Keep assertions deterministic and side-effect free. A broken assertion fails
loudly (it never silently passes) – see `run_human_eval_set`.

## Where evals run

* **Post-node evals** – attached to a node via the `eval_definitions` table;
  executed by `EvalEngine.evaluate` in the pipeline executor.
* **HITL gate evals** – evaluated before an interrupt; see
  `pipeline_engine/node_runner.py`.
* **Feedback evals** – ad-hoc evals via `EvalEngine.standalone_evaluate`
  (§8.20).

`llm_judge` evals require a resolved judge callable (a *different* model backend
than the one under test, where possible). `human_set` evals require no model –
they are pure Python.

## Managed eval input corpus (`EvalDataset` / `EvalCase`) – FAR-375 Phase 2

The original "task-type breadth" gap was never about comparing models – it was
about a **repeatable, managed input corpus** an eval suite can re-run. Phase 2
is the **data layer** for that corpus (FAR-375). It is standalone: it does not
depend on `EvalSuite` (Phase 1) and adds no endpoints, UI, or run-execution
logic (those are Phase 3).

* **`EvalDataset`** – a named, versioned, org- (or team-) scoped header for a
  corpus. One active name per org; soft-delete only (`deleted_at` /
  `deleted_by`).
* **`EvalCase`** – a single repeatable input. `input_payload` is the canonical
  payload store (mirrors `webhook_payloads.raw_payload`); `expected_output` is an
  optional reference answer; `input_hash` is SHA-256 of the payload for dedupe
  and audit. A case references its dataset with `ON DELETE RESTRICT`, so a
  referenced dataset can never be hard-removed.

Key guarantees (enforced by `backend/tests/unit/db/test_eval_dataset.py`):

* **Storage is data-only.** `input_payload` is stored verbatim and returned
  verbatim – even when it contains prompt-injection strings it is never altered
  or executed. Phase 3 owns the boundary enforcement (LLM-judge + SUT paths)
  that prevents the payload from becoming instructions; Phase 2 guarantees only
  *storage-as-data*.
* **Decoupled from Run retention.** The corpus stores its own payload, so
  repeatable re-runs survive Run pruning.
* **Soft-delete + org-scoped RLS.** Both tables carry `ENABLE` + `FORCE ROW
  LEVEL SECURITY` with a `rls_org_isolation` policy, owned by `modulo_migrate`.
* **Empty dataset is a no-op at run time.** `validate_dataset_has_cases` returns
  a count of active cases so Phase 3 can refuse to run an empty dataset.
* **Housekeeping.** `purge_soft_deleted_eval_cases` hard-deletes soft-deleted
  cases past a retention cutoff (cases have no dependents). Dataset
  hard-delete/purge is intentionally withheld here – the "referenced by a
  SuiteRun" guard lands in Phase 3.

> Note: this document is the source of truth for the eval data layer while
> `docs/prd.md` is being revised; any PRD section describing an "eval dataset"
> concept must match the entities and guarantees above.

## Tracking an eval over time (`SuiteRun`) — FAR-376 Phase 3

Phase 3 closes the flywheel: take an `EvalSuite` (Phase 1) onto a repeatable
`EvalDataset` (Phase 2), run it against a pinned Model Backend on a snapshot of
the exact inputs + definition config, persist every per-case outcome, and —
when a same-tuple baseline already exists — **detect a pass-rate regression**.

### `SuiteRun` — one execution, three immutable snapshots

A `SuiteRun` records one execution of a suite against a dataset:

* **`dataset_version`** — snapshot of dataset membership at creation. A content
  change bumps the version, which produces a NEW baseline tuple rather than
  corrupting a prior run's comparison.
* **`definition_checksum`** — SHA-256 of every eval-definition's config snapshot.
  A changed config or changed membership also produces a new tuple.
* **`scenario_signature`** — canonical hash of the run's scenario inputs; `NULL`
  is the explicit "scenarios unused" sentinel.

These are captured **at creation and never live-looked-up**. The immutable
`baseline_tuple` is the comparison key: `(suite_id, dataset_id, dataset_version,
eval_definition_ids, definition_checksum, model_backend_id, scenario_signature)`.

### Version scoping — FAR-382

Each `EvalDefinition` and `EvalSuite` carries an integer `version` (default `1`,
non-null from cutover). Every definition create stamps `version=1`; every edit
snapshots the pre-edit config into `pre_version_raw` (JSON) and bumps `version`
by one, so a rubric change (e.g. a v1 -> v2 edit to an eval's `config_json`) is
an **explicitly version-scoped event** rather than a silent mutation.

* **`EvalResult.eval_definition_version`** — a snapshot of the eval-definition
  `version` that scored the result, captured at write time. A version bump after
  a result was scored never retroactively changes what that result represents.
* **NULL-version lookup (latest-at-time)** — a result recorded before
  versioning was cut over carries `eval_definition_version = NULL`. When a query
  needs an eval definition but no version is pinned, it resolves to the
  definition's **current (latest)** `version` (see
  `resolve_eval_definition_version`); a pinned version is returned unchanged.
* Complement, not replacement: `definition_checksum` remains the config
  fingerprint that produces a new `baseline_tuple` on a config change, while
  `version` is the human-explicit signal of that change. The two together ensure
  a rubric change is never mistaken for a regression.

`CalibrationLabel` / calibration-vs-human-labels is deliberately **out of scope**
for this versioning work.

### State machine

```
pending -> running -> completed | partial | failed
  |--cancelled (pending or running)
```

* `partial` (some cases errored) is distinct from `failed` (orchestration error).
* Transitions are guarded by an **optimistic-lock `version` column**: two
  concurrent workers cannot both land `completed` — the second is rejected.

### Baseline resolution

The baseline is the **latest same-tuple `completed` run strictly prior** to the
current run, with a deterministic `(created_at, id)` tiebreak. It never queries
the live dataset/definition; it is a pure tuple match on prior `SuiteRun` rows.

* First run (no completed same-tuple prior) → **comparison skipped** + warning,
  no regression flag.
* `partial`/`failed`/`cancelled` prior runs are never baselines.
* Cross-org runs are never selected (org-scoped, and never by the org's RLS).
* An operator may pin a canonical baseline (`baseline_locked`).

### Comparison — REUSES the existing engine, never a parallel store

Per-case outcomes are persisted into the existing `eval_results` table (via
`suite_run_id`) and the pass-rate comparison delegates to `detect_regressions`
with `group_by="suite_id"` plus an explicit `baseline` scope. **The default
`detect_regressions(session, org_id, days=7, ...)` signature is unchanged.**

* Pass rates are aggregated **per `eval_type` only** — raw `score` is never
  averaged across differing eval types (type-incorrect).
* Regression fires on configurable absolute (and optional relative) drop
  thresholds.
* A `partial` run excludes its errored cases from the denominator (recorded as
  `excluded_case_count`) and does not raise a regression alert unless explicitly
  configured.

### Regression notification — the Alerting layer (FAR-379)

Regression postings route through the existing `Notifier` (`eval_regression`
event) — no parallel "sink". Eval notification endpoints share **zero**
subscribers with production error forwarders (a runtime guard asserts this);
posting is idempotent on `suite_run_id`, rate-limited per suite, requires a
baseline before any alert fires, and alerts if the eval channel has no
subscribers (never a silent drop).

Detection is Phase 3's job. The **alerting** layer (FAR-379) owns *when* and
*how often* it pages, and is configured per suite via
`PUT /api/v1/evals/suites/{suite_id}/alerting`:

| Field | Meaning |
|---|---|
| `baseline_window` | Rolling N-run baseline window used when resolving the comparison baseline. `NULL` = alerting dormant until an explicit baseline exists. |
| `minimum_delta` | Minimum pass-rate drop (fraction 0..1) the observed drop must exceed before an alert fires. `NULL` = defer entirely to the Phase 3 `regressed` flag. |
| `cooldown` | Silence window (minutes) between regression alerts for a suite — a single sustained regression does not page on every run. `NULL` = no time-based rate limit (idempotency on `suite_run_id` still applies). |

The decision function is `maybe_alert_eval_regression` in
`core/eval_engine/suite_run.py`. Its guards fire in order: an **explicit
baseline is required** (a first run never alerts — `regressed` is `None`), a
non-regressed run is skipped, a `partial` run never alerts, an alert for a
given run is sent at most once (idempotent on `suite_run_id` via `notified_at`),
the drop must meet `minimum_delta`, and the suite's `cooldown` window rate-limits
a persistent regression. Only then does the runtime isolation guard run — it
*fails loudly* (raises) if the eval channel has no eval-scoped subscribers or
leaks to a production error forwarder, never a silent drop. All config is
org-scoped and overwritten-able (`NULL` clears a field).

### Spend & tenancy

Execution routes through the org Model Backend with the `daily_spend_limit`
**and** a separate per-suite cumulative cost ceiling (two independent counters).
The per-suite ledger is a row-locked increment before each judge call so a
read-check-write race cannot overshoot. `suite_runs` carries `ENABLE` + `FORCE
ROW LEVEL SECURITY` + `rls_org_isolation` (owned by `modulo_migrate`), so the
`OrgScoped` mixin alone is not the isolation boundary.

### Feature flag

The SuiteRun / comparison ENDPOINTS and UI are gated behind the existing
`eval_maturity` flag (`eval_maturity_enabled()`), fail-closed to the legacy
suite path. The data layer is always present; only the new comparison surface is
gated.

## Leaderboard / rollup read-model (FAR-378)

The READ side of the eval flywheel is a pure read-model over the now-structured
`SuiteRun` / `eval_results` data. It is **live aggregation** over the existing
tables — there is no parallel materialised table, no new "scores over time"
store — so it can never diverge from what the writer actually persisted.

Two endpoints:

* `GET /api/v1/evals/leaderboard?group_by=pipeline|node|agent` — per-axis
  leaderboard ranked by aggregate pass-rate, optional `eval_id` filter (the
  cross-pipeline rollup), plus `pipeline_id` / `node_id` / `model_backend_id`
  and `days` filters.
* `GET /api/v1/evals/{eval_id}/timeseries` — day-bucketed pass-rate series for
  one eval, with a window `summary` and a `pipelines` cross-pipeline rollup.

### The pass-rate-only rule (non-circular)

Leaderboards and time-series rank and aggregate on **pass-rate only** — the
`passed` boolean, never the raw `score` column. A raw score is not comparable
across differing `eval_type` (an `llm_judge` 0.8 and a `regex` 0.6 measure
different things), so every aggregation **partitions by `eval_type`** and rolls
up by *counting passes*, never by *averaging scores*. A mixed-`eval_type` axis
is never ranked on a raw score:

* each leaderboard entry carries a per-`eval_type` `by_type` breakdown
  (`pass_rate` / `passed` / `total` / `run_count`); the entry-level
  `pass_rate` is `passed/total` across the boolean partitions, never a score
  mean;
* the timeseries buckets `eval_type` separately too.

### Isolation and determinism

Every query injects the explicit `organisation_id = :org` predicate —
`modulo_app` is BYPASSRLS, so the predicate is the ONLY isolation control
(`set_rls_org` remains defense-in-depth). Suite-run outcomes count only when the
run is terminal (`completed`/`partial`), so the read-model is deterministic
across two calls; legacy pipeline-path rows (no `suite_run_id`) are always
included. Guardrail rows are excluded (the standard eval consumer contract).

## The coverage-gap signal (FAR-381)

The complements to the pass-rate read-models are **static coverage** (does a
variant have any eval at all — `coverage-gaps` on a variant group) and the
dynamic **coverage-gap signal** described here. It answers the PRD §8.19
problem: *"variants diverged but evals did not differentiate."* That is not a
dashboard convenience — it is a signal that the **eval SUITE is insufficient**
and must be routed to eval-quality improvement.

Lineage read (pure read-model, no new table, no migration):

```
VariantGroup ──> Runs (variant_group_id, batch_id; each has variant_config_snapshot
                    {variant_id, variant_name} and outputs_json)
                     │
                     └──> EvalResult (run_id, eval_id, score, passed)
```

Endpoint: `GET /api/v1/eval-coverage-gap?variant_group_id=…&batch_id=…&min_runs=3&threshold=0.15`

It returns a per-eval verdict list:

```json
{
  "status": "complete",
  "variant_divergence": 0.4,
  "evals": [
    {
      "eval_id": "…", "eval_name": "…",
      "variant_divergence": 0.4,
      "eval_score_spread": 0.01,
      "has_gap": true,
      "reason": "Variants diverged but this eval could not differentiate them; route to eval-quality improvement.",
      "recommended_action": "improve_evals"
    }
  ]
}
```

### The metrics

* **Variant divergence** — how spread apart the variant *outputs* are. Measured
  as `1 − mean pairwise normalized edit distance` over the canonical JSON of
  each terminal variant run's `outputs_json`. Outputs are often unstructured, so
  a structure-independent string-similarity gradient is the stable default:
  `0.0` = identical outputs, near `1.0` = maximally different.
* **Eval differentiation** — the population standard deviation of that eval's
  per-variant metric within one `eval_id` (`score` when present, else `passed`
  as `1.0/0.0`). Grouping by `eval_id` keeps a single scale per comparison, so
  an absolute std is meaningful. A near-zero std means the eval scored every
  variant ~the same — it did not differentiate them.

### Statistical significance

A signal is emitted only when at least `min_runs` (default `3`) **terminal**
runs carry eval data. Below that the result is `status: "insufficient_data"`
with an empty `evals` list and **no signal** — this is what suppresses
llm-judge high-variance false positives. Runs are terminal-only, so the
read-model is deterministic across two calls.

### The gap condition (configurable threshold)

```
has_gap = variant_divergence >= divergence_threshold      (default 0.15)
          AND eval_differentiation < differentiation_threshold  (default 0.05)
```

The `divergence_threshold` is configurable per suite via the endpoint `threshold`
query param (an operator dial, `0.0`–`1.0`). The `differentiation_threshold` is
an eval-quality calibration kept as a module default (not surfaced on the
endpoint).

### Recommended-action routing

* `has_gap=true` → `recommended_action: "improve_evals"` — the evals could not
  tell the variants apart even though the variants genuinely differ. The problem
  is the eval **suite**, so this routes to eval-quality improvement, not to a
  flag.
* `has_gap=false` → `recommended_action: "ok"` — either the variants did not
  diverge, or the evals differentiated them adequately.

### Isolation

Every query injects the explicit `organisation_id = :org` predicate (BYPASSRLS
⇒ the predicate is the ONLY isolation control). A cross-org batch or variant
group resolves to no org-owned runs and returns `status: "insufficient_data"`
rather than leaking another org's data.
