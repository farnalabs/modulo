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
