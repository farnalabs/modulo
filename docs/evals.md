# Evals

Modulo evaluates agent/node outputs against **eval definitions**. An eval is a
deterministic (or model-mediated) check attached to a pipeline node. This
document explains the eval types, why the eval system is deliberately
**non-circular**, and how to use **human-authored eval sets** — the trustworthy
correctness path.

## Eval types

| Type | Mechanism | Asserts | Trustworthy for correctness? |
|------|-----------|---------|------------------------------|
| `regex` | Pattern match on an output field | **Shape** (text matches a pattern) | No — shape only |
| `json_schema` | JSON Schema validation | **Shape / structure** of an object | No — shape only |
| `custom_function` | User-supplied callable | Whatever the function checks | Depends on the function |
| `llm_judge` | An LLM grades the output | **Soft** quality/similarity signal | No — circular + injection-prone |
| `human_set` | A registered, versioned, **human-authored** assertion set | **Correctness** (semantic invariants + business rules) | **Yes** |

## Why evals must be non-circular

The eval system is built to avoid **eval circularity** — the trap where an LLM
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
   *instruction* leakage — it cannot guarantee the judge's *correctness*
   assessment is right.

`llm_judge` is useful as a **soft** signal (e.g. flagging candidates for human
review) but must never be the sole gate for a claim of correctness.

### Human-authored eval sets are the trustworthy path
`human_set` runs a **fixed, versioned artifact** — a list of deterministic
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

* `set_name` — the registered set name (see `HUMAN_EVAL_SETS` in
  `core/eval_engine/human_eval_sets.py`). Required.
* `field` — the (dotted) output field the assertions resolve and parse. Optional;
  defaults to `output`.
* `version` — advisory. The registry holds a single active version per name;
  consumers should pin the set name they validated against.

At run time the engine looks up the set and runs **every** assertion. The eval
passes only if all assertions pass; the `detail` lists any failing assertion
names. A `block` behaviour raises `EvalBlockedError` on failure.

### Shipped set: `demo_classification` (`v1`)
A representative agent task: a support-message classification agent emits JSON
`{category, priority, confidence?}`. The human-authored assertions are:

1. `valid_json` — the output field parses as a JSON object.
2. `required_keys` — `category` and `priority` are present.
3. `category_enum` — `category` ∈ {billing, technical, general}.
4. `priority_enum` — `priority` ∈ {low, medium, high}.
5. `consistency` — **business rule**: a billing issue is never `low` priority; a
   technical outage is always `high`.
6. `no_extra_keys` — no hallucinated keys leak into the contract.

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
loudly (it never silently passes) — see `run_human_eval_set`.

## Where evals run

* **Post-node evals** — attached to a node via the `eval_definitions` table;
  executed by `EvalEngine.evaluate` in the pipeline executor.
* **HITL gate evals** — evaluated before an interrupt; see
  `pipeline_engine/node_runner.py`.
* **Feedback evals** — ad-hoc evals via `EvalEngine.standalone_evaluate`
  (§8.20).

`llm_judge` evals require a resolved judge callable (a *different* model backend
than the one under test, where possible). `human_set` evals require no model —
they are pure Python.

## Managed eval input corpus (`EvalDataset` / `EvalCase`) — FAR-375 Phase 2

The original "task-type breadth" gap was never about comparing models — it was
about a **repeatable, managed input corpus** an eval suite can re-run. Phase 2
is the **data layer** for that corpus (FAR-375). It is standalone: it does not
depend on `EvalSuite` (Phase 1) and adds no endpoints, UI, or run-execution
logic (those are Phase 3).

* **`EvalDataset`** — a named, versioned, org- (or team-) scoped header for a
  corpus. One active name per org; soft-delete only (`deleted_at` /
  `deleted_by`).
* **`EvalCase`** — a single repeatable input. `input_payload` is the canonical
  payload store (mirrors `webhook_payloads.raw_payload`); `expected_output` is an
  optional reference answer; `input_hash` is SHA-256 of the payload for dedupe
  and audit. A case references its dataset with `ON DELETE RESTRICT`, so a
  referenced dataset can never be hard-removed.

Key guarantees (enforced by `backend/tests/unit/db/test_eval_dataset.py`):

* **Storage is data-only.** `input_payload` is stored verbatim and returned
  verbatim — even when it contains prompt-injection strings it is never altered
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
  hard-delete/purge is intentionally withheld here — the "referenced by a
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

### Regression notification

Regression postings route through the existing `Notifier` (`eval_regression`
event) — no parallel "sink". Eval notification endpoints share **zero**
subscribers with production error forwarders (a runtime guard asserts this);
posting is idempotent on `suite_run_id`, rate-limited per suite, requires a
baseline before any alert fires, and alerts if the eval channel has no
subscribers (never a silent drop).

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
