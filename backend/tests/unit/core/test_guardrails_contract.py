"""Contract test — the five authoritative eval_type sites agree (FAR-208 item 1).

The allowed eval_type vocabulary lives in exactly five places:

  (a) ``EvalType`` StrEnum (modulo.core.eval_engine) — the full universe,
      now including ``guardrail``.
  (b) the three Pydantic request regexes in ``modulo.api.routes.evals`` —
      create / update accept the FULL universe (including ``guardrail``);
      from-run deliberately EXCLUDES ``guardrail`` (a deny-rule cannot be
      pre-populated from run output — a stub would be fail-open).
  (c) ``graph_validator`` composite ``valid_types`` — composite-eligible
      subset; guardrail is NOT composite-eligible (like custom_function).
  (d) ``composite_binding.EvalDefinitionConfig.type`` Literal — same
      composite-eligible subset; guardrail deliberately absent.
  (e) DB CHECK ``ck_eval_definitions_type`` (ORM model + migration 0110) —
      must include ``guardrail``.

Sites (a), (b create/update), (e) must agree on the FULL universe. Sites (c),
(d) must agree on the composite-eligible subset and MUST NOT contain
``guardrail``.
"""

import re

from modulo.core.composite_engine.composite_binding import EvalDefinitionConfig
from modulo.core.eval_engine import EvalType
from modulo.core.graph_validator import GraphValidator
from modulo.db.models.eval_definition import EvalDefinition as EvalDefinitionModel

FULL_UNIVERSE = {"llm_judge", "regex", "json_schema", "custom_function", "guardrail", "human_set"}
COMPOSITE_ELIGIBLE = {"regex", "json_schema", "llm_judge"}


def test_site_a_evaltype_full_universe():
    assert set(EvalType) == FULL_UNIVERSE


def test_site_b_evals_regexes_accept_guardrail():
    import modulo.api.routes.evals as evals

    create_pattern = evals.CreateEvalRequest.model_fields["eval_type"].metadata[0].pattern
    update_pattern = evals.UpdateEvalRequest.model_fields["eval_type"].metadata[0].pattern
    for pattern in (create_pattern, update_pattern):
        assert re.match(f"^{pattern}$", "guardrail"), pattern
    # The full universe must be expressible through the create/update API edge.
    for value in FULL_UNIVERSE:
        assert re.match(f"^{create_pattern}$", value), value


def test_site_b_from_run_regex_excludes_guardrail():
    # ``guardrail`` is deliberately absent from the from-run vocabulary: the
    # from-run endpoint pre-populates a definition from run OUTPUT, and a
    # guardrail deny-rule (regex pattern / json_schema) cannot be derived from a
    # sample. A stub config would be silently-inert (fail-open) for a
    # data-safety control, so the API edge rejects it outright.
    import modulo.api.routes.evals as evals

    from_run_pattern = evals.CreateEvalFromRunRequest.model_fields["eval_type"].metadata[0].pattern
    assert not re.match(f"^{from_run_pattern}$", "guardrail"), from_run_pattern
    # The pre-populatable subset remains expressible.
    for value in ("llm_judge", "regex", "json_schema", "custom_function"):
        assert re.match(f"^{from_run_pattern}$", value), value


def test_site_c_graph_validator_composite_valid_types_exclude_guardrail():
    # Behavioural check: the graph validator rejects a guardrail-typed eval in
    # composite output_validation (guardrail is NOT composite-eligible).
    from modulo.core.graph_validator._types import ValidationResult

    result = ValidationResult()
    GraphValidator()._check_output_validation(
        "node-1",
        {
            "eval_definitions": [
                {"id": "gr", "name": "guardrail-eval", "type": "guardrail", "config": {}, "failure_behaviour": "block"}
            ]
        },
        result,
    )
    codes = [issue.code for issue in result.issues]
    assert "COMPOSITE_VALIDATION_INVALID_TYPE" in codes
    assert any("guardrail" in issue.message for issue in result.issues)
    # And regex/json_schema/llm_judge pass validation (no invalid-type error).
    clean = ValidationResult()
    GraphValidator()._check_output_validation(
        "node-1",
        {
            "eval_definitions": [
                {"id": "r", "name": "regex-eval", "type": "regex", "config": {"field": "x", "pattern": "y"}},
                {"id": "s", "name": "schema-eval", "type": "json_schema", "config": {"schema": {}}},
                {"id": "l", "name": "judge-eval", "type": "llm_judge", "config": {}},
            ]
        },
        clean,
    )
    assert "COMPOSITE_VALIDATION_INVALID_TYPE" not in {issue.code for issue in clean.issues}


def test_site_d_composite_binding_literal_excludes_guardrail():
    allowed = EvalDefinitionConfig.model_fields["type"].annotation.__args__
    assert set(allowed) == COMPOSITE_ELIGIBLE
    assert "guardrail" not in allowed
    assert "custom_function" not in allowed  # pre-existing drift, documented


def test_site_e_orm_and_migration_check_include_guardrail():
    orm_check = next(
        c for c in EvalDefinitionModel.__table_args__ if getattr(c, "name", None) == "ck_eval_definitions_type"
    )
    assert "guardrail" in orm_check.sqltext.text

    import pathlib

    migration_dir = pathlib.Path(__file__).resolve().parents[3] / "src" / "modulo" / "db" / "migrations" / "versions"
    migration_file = migration_dir / "0110_schema_pipeline_runtime.py"
    assert migration_file.exists(), "migration 0110_schema_pipeline_runtime.py missing"
    content = migration_file.read_text(encoding="utf-8")
    assert "'guardrail'" in content
    assert "ck_eval_definitions_type" in content
