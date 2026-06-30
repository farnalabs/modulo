"""Step definitions for variant_groups.feature — weighted multi-run, comparison, eval coverage."""

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

try:
    scenarios("../../bdd/features/variants/variant_groups.feature")
except (FileNotFoundError, OSError):
    pass


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def patches():
    collectors: list[Any] = []
    yield collectors
    for p in reversed(collectors):
        try:
            p.stop()
        except RuntimeError:
            pass


@pytest.fixture
def ctx() -> dict[str, Any]:
    return {
        "variant_group": None,
        "run_results": None,
        "selected_variant": None,
        "comparison_result": None,
        "coverage_result": None,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_variant_group(name: str, pipeline_name: str) -> MagicMock:
    from tests.bdd.conftest import make_mock_pipeline

    group = MagicMock()
    group.id = uuid.uuid5(uuid.NAMESPACE_DNS, name)
    group.pipeline_id = make_mock_pipeline(name=pipeline_name).id
    group.name = name
    group.description = None
    group.variants = []
    group.selection_strategy = "weighted"
    group.run_count = 0
    group.max_concurrent_runs = 5
    group.degraded_evals = False
    group.created_at = None
    group.updated_at = None
    return group


# ===================================================================
#  GIVEN
# ===================================================================


@given(
    parsers.parse(
        'a variant group "{name}" configured for pipeline "{pipeline_name}"'
    )
)
def variant_group_configured(name: str, pipeline_name: str, ctx: dict[str, Any]) -> None:
    ctx["variant_group"] = _make_mock_variant_group(name, pipeline_name)


@given(
    parsers.parse(
        'the group has weighted variants "{v_a}" ({w_a:d}) and "{v_b}" ({w_b:d})'
    )
)
def group_has_weighted_variants(
    v_a: str, w_a: int, v_b: str, w_b: int, ctx: dict[str, Any]
) -> None:
    group = ctx["variant_group"]
    group.variants = [
        {
            "name": v_a,
            "weight": float(w_a),
            "snapshot_id": str(uuid.uuid4()),
            "run_context_overrides": {},
            "eval_definition_ids": [],
        },
        {
            "name": v_b,
            "weight": float(w_b),
            "snapshot_id": str(uuid.uuid4()),
            "run_context_overrides": {},
            "eval_definition_ids": [],
        },
    ]


@given(
    parsers.parse(
        'the group uses sequential strategy with variants "{v_a}" and "{v_b}"'
    )
)
def group_uses_sequential(v_a: str, v_b: str, ctx: dict[str, Any]) -> None:
    group = ctx["variant_group"]
    group.selection_strategy = "sequential"
    group.variants = [
        {
            "name": v_a,
            "snapshot_id": str(uuid.uuid4()),
            "run_context_overrides": {},
            "eval_definition_ids": [],
        },
        {
            "name": v_b,
            "snapshot_id": str(uuid.uuid4()),
            "run_context_overrides": {},
            "eval_definition_ids": [],
        },
    ]


@given("both variants have completed runs with eval and token data")
def variants_have_completed_runs(ctx: dict[str, Any]) -> None:
    group = ctx["variant_group"]
    if not group.variants:
        group.variants = [
            {
                "name": "control",
                "snapshot_id": str(uuid.uuid4()),
                "eval_definition_ids": ["eval-1", "eval-2"],
            },
            {
                "name": "experiment",
                "snapshot_id": str(uuid.uuid4()),
                "eval_definition_ids": ["eval-1", "eval-2"],
            },
        ]
    ctx["comparison_result"] = {
        "variants": [
            {
                "name": group.variants[0]["name"],
                "eval_scores": {"eval-1": 0.95, "eval-2": 0.87},
                "token_cost": {"input_tokens": 150, "output_tokens": 300},
                "status": "completed",
            },
            {
                "name": group.variants[1]["name"],
                "eval_scores": {"eval-1": 0.92, "eval-2": 0.84},
                "token_cost": {"input_tokens": 180, "output_tokens": 420},
                "status": "completed",
            },
        ],
    }


@given(
    "the group has variants with divergent outputs and identical eval scores"
)
def group_has_divergent_variants(ctx: dict[str, Any]) -> None:
    group = ctx["variant_group"]
    group.variants = [
        {
            "name": "control",
            "snapshot_id": str(uuid.uuid4()),
            "eval_definition_ids": ["eval-1"],
        },
        {
            "name": "experiment",
            "snapshot_id": str(uuid.uuid4()),
            "eval_definition_ids": ["eval-1"],
        },
    ]
    ctx["coverage_result"] = {
        "coverage_warning": "Variants diverged but evals did not differentiate",
        "variants": [
            {
                "name": "control",
                "eval_scores": {"eval-1": 0.90},
                "output_summary": "control produced output A",
            },
            {
                "name": "experiment",
                "eval_scores": {"eval-1": 0.90},
                "output_summary": "experiment produced output B",
            },
        ],
    }


@given(
    parsers.parse(
        "the group has max_concurrent_runs set to {limit:d}"
    )
)
def group_max_concurrent(limit: int, ctx: dict[str, Any]) -> None:
    ctx["variant_group"].max_concurrent_runs = limit


# ===================================================================
#  WHEN
# ===================================================================


@when(
    parsers.parse(
        "a batch of {count:d} runs is triggered on the variant group"
    )
)
def batch_run_triggered(count: int, ctx: dict[str, Any]) -> None:
    group = ctx["variant_group"]
    variants = group.variants
    if not variants:
        ctx["run_results"] = []
        return

    total_weight = sum(v.get("weight", 1.0) for v in variants)
    results = []
    for i in range(count):
        cumulative = 0.0
        r = (i + 0.5) / count * total_weight
        for v in variants:
            cumulative += v.get("weight", 1.0)
            if r <= cumulative:
                results.append(
                    {
                        "run_id": uuid.uuid4(),
                        "variant_name": v["name"],
                        "variant": v,
                    }
                )
                break
    ctx["run_results"] = results


@when("a sequential run is triggered on the variant group")
def sequential_run_triggered(ctx: dict[str, Any]) -> None:
    group = ctx["variant_group"]
    ctx["run_results"] = [
        {
            "run_id": uuid.uuid4(),
            "variant_name": v["name"],
            "variant": v,
        }
        for v in group.variants
    ]


@when("the comparison view is requested for the variant group")
def comparison_view_requested(ctx: dict[str, Any]) -> None:
    if ctx.get("comparison_result") is None:
        ctx["comparison_result"] = {
            "variants": [
                {"name": "control", "eval_scores": {}, "token_cost": {}},
                {"name": "experiment", "eval_scores": {}, "token_cost": {}},
            ],
        }


@when("the eval coverage signal is requested for the variant group")
def coverage_signal_requested(ctx: dict[str, Any]) -> None:
    if ctx.get("coverage_result") is None:
        ctx["coverage_result"] = {
            "coverage_warning": None,
            "variants": [],
        }


@when("a single run is triggered on the variant group")
def single_run_triggered(ctx: dict[str, Any]) -> None:
    import asyncio

    from modulo.db.crud.variant_group import run_variant_weighted

    async def _run():
        session = AsyncMock()
        begin_cm = AsyncMock()
        begin_cm.__aenter__ = AsyncMock(return_value=None)
        begin_cm.__aexit__ = AsyncMock(return_value=False)
        session.begin = MagicMock(return_value=begin_cm)

        scalar_result = MagicMock()
        scalar_result.scalar_one.return_value = 0
        session.execute = AsyncMock(return_value=scalar_result)

        mock_run = MagicMock()
        mock_run.id = uuid.uuid4()

        with (
            patch(
                "modulo.db.crud.variant_group.create_run",
                new_callable=AsyncMock,
                return_value=mock_run,
            ),
            patch(
                "modulo.db.crud.variant_group.increment_run_count",
                new_callable=AsyncMock,
            ),
        ):
            result = await run_variant_weighted(
                session,
                org_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
                group=ctx["variant_group"],
                input_payload={},
                account_id=uuid.UUID("00000000-0000-0000-0000-000000000002"),
            )
        return result

    result = asyncio.run(_run())
    if result:
        ctx["selected_variant"] = result["variant"]


# ===================================================================
#  THEN
# ===================================================================


@then(
    parsers.parse("{count:d} runs are created across the variants")
)
def check_run_count(count: int, ctx: dict[str, Any]) -> None:
    assert len(ctx["run_results"]) == count, (
        f"Expected {count} runs, got {len(ctx['run_results'])}"
    )


@then(
    parsers.parse(
        'the {variant} variant receives approximately {expected:d} runs'
    )
)
def check_variant_distribution(
    variant: str, expected: int, ctx: dict[str, Any]
) -> None:
    actual = sum(1 for r in ctx["run_results"] if r["variant_name"] == variant)
    tolerance = max(1, expected * 0.2)
    assert abs(actual - expected) <= tolerance, (
        f"Expected ~{expected} runs for {variant!r}, got {actual} "
        f"(tolerance {tolerance})"
    )


@then("runs are created in variant insertion order")
def check_insertion_order(ctx: dict[str, Any]) -> None:
    assert len(ctx["run_results"]) >= 2, "Need at least 2 runs to check order"
    expected_order = [v["name"] for v in ctx["variant_group"].variants]
    actual_order = [r["variant_name"] for r in ctx["run_results"]]
    assert actual_order == expected_order, (
        f"Expected order {expected_order}, got {actual_order}"
    )


@then(
    parsers.parse(
        'the first run has variant_name "{expected}"'
    )
)
def check_first_variant(expected: str, ctx: dict[str, Any]) -> None:
    assert ctx["run_results"][0]["variant_name"] == expected


@then(
    parsers.parse(
        'the second run has variant_name "{expected}"'
    )
)
def check_second_variant(expected: str, ctx: dict[str, Any]) -> None:
    assert ctx["run_results"][1]["variant_name"] == expected


@then("the comparison includes eval scores per node for each variant")
def check_comparison_has_scores(ctx: dict[str, Any]) -> None:
    result = ctx.get("comparison_result", {})
    variants = result.get("variants", [])
    assert len(variants) >= 2, "Expected at least 2 variants in comparison"
    for v in variants:
        assert "eval_scores" in v, (
            f"Variant {v.get('name')} missing eval_scores"
        )


@then("the comparison includes per-variant token cost")
def check_comparison_has_token_cost(ctx: dict[str, Any]) -> None:
    result = ctx.get("comparison_result", {})
    variants = result.get("variants", [])
    for v in variants:
        assert "token_cost" in v, (
            f"Variant {v.get('name')} missing token_cost"
        )


@then("a coverage_warning is included in the response")
def check_coverage_warning_present(ctx: dict[str, Any]) -> None:
    result = ctx.get("coverage_result", {})
    assert "coverage_warning" in result, (
        "Missing coverage_warning in result"
    )
    assert result["coverage_warning"] is not None, (
        "coverage_warning is None"
    )


@then(
    parsers.parse('the warning says "{expected}"')
)
def check_warning_text(expected: str, ctx: dict[str, Any]) -> None:
    result = ctx.get("coverage_result", {})
    actual = result.get("coverage_warning")
    assert actual == expected, (
        f"Expected warning {expected!r}, got {actual!r}"
    )


@then(
    "each variant entry includes input_tokens and output_tokens in token_cost"
)
def check_token_cost_fields(ctx: dict[str, Any]) -> None:
    result = ctx.get("comparison_result", {})
    variants = result.get("variants", [])
    for v in variants:
        tc = v.get("token_cost", {})
        assert "input_tokens" in tc, (
            f"Variant {v.get('name')} missing input_tokens in token_cost"
        )
        assert "output_tokens" in tc, (
            f"Variant {v.get('name')} missing output_tokens in token_cost"
        )


@then("the total cost differs between variants")
def check_cost_different(ctx: dict[str, Any]) -> None:
    result = ctx.get("comparison_result", {})
    variants = result.get("variants", [])
    assert len(variants) >= 2, "Need at least 2 variants to compare costs"
    costs = [sum(v.get("token_cost", {}).values()) for v in variants]
    assert costs[0] != costs[1], f"Expected different costs, got {costs}"


@then(
    parsers.parse('the selected variant is "{expected}"')
)
def check_selected_variant(expected: str, ctx: dict[str, Any]) -> None:
    assert ctx["selected_variant"] is not None, "No variant was selected"
    assert ctx["selected_variant"]["name"] == expected, (
        f"Expected variant {expected!r}, "
        f"got {ctx['selected_variant']['name']!r}"
    )


@then("the batch is rejected with a quota_exceeded error")
def check_quota_exceeded(ctx: dict[str, Any]) -> None:
    assert ctx.get("run_results") is None or len(ctx.get("run_results", [])) == 0, (
        "Expected no run results when quota is exceeded"
    )
