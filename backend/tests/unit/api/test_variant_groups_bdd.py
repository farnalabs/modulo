"""Unit tests for variant group BDD-backed features — batch run, comparison, coverage signal."""

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.db.crud.variant_group import get_coverage_gaps, pick_variant_weighted

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_session_mock() -> AsyncMock:
    session = AsyncMock()
    session.in_transaction.return_value = True
    session.execute = AsyncMock()
    begin_ctx = AsyncMock()
    begin_ctx.__aenter__ = AsyncMock(return_value=session)
    begin_ctx.__aexit__ = AsyncMock(return_value=None)
    session.begin = MagicMock(return_value=begin_ctx)
    return session


def make_mock_principal(**kwargs: Any) -> MagicMock:
    p = MagicMock()
    p.organisation_id = kwargs.get("org_id", uuid.uuid4())
    p.account_id = kwargs.get("user_id", uuid.uuid4())
    p.username = kwargs.get("username", "test_user")
    p.org_role = kwargs.get("org_role", "admin")
    return p


# ===================================================================
#  Zero-weight variant selection (existing behaviour)
# ===================================================================


class TestZeroWeightVariantSelection:
    """A zero-weight variant is never selected when positive-weight variants exist."""

    def test_zero_weight_not_selected_with_positive_weights(self) -> None:
        variants = [
            {"name": "control", "weight": 100.0, "snapshot_id": str(uuid.uuid4())},
            {"name": "disabled", "weight": 0.0, "snapshot_id": str(uuid.uuid4())},
        ]
        for _ in range(1000):
            result = pick_variant_weighted(variants)
            assert result is not None
            assert result["name"] == "control", f"Zero-weight variant selected: {result}"

    def test_multiple_positive_weights_exclude_zero(self) -> None:
        variants = [
            {"name": "a", "weight": 50.0, "snapshot_id": str(uuid.uuid4())},
            {"name": "b", "weight": 50.0, "snapshot_id": str(uuid.uuid4())},
            {"name": "disabled", "weight": 0.0, "snapshot_id": str(uuid.uuid4())},
        ]
        for _ in range(2000):
            result = pick_variant_weighted(variants)
            assert result is not None
            assert result["name"] != "disabled"


# ===================================================================
#  Weighted variant distribution (existing behaviour)
# ===================================================================


class TestWeightedDistribution:
    """Distribution characteristics of pick_variant_weighted."""

    def test_highly_skewed_weights_produce_expected_bias(self) -> None:
        variants = [
            {"name": "control", "weight": 99.0},
            {"name": "variant_a", "weight": 1.0},
        ]
        control_count = 0
        trials = 5000
        for _ in range(trials):
            result = pick_variant_weighted(variants)
            assert result is not None
            if result["name"] == "control":
                control_count += 1
        assert control_count > trials * 0.85


# ===================================================================
#  Coverage gap detection (existing behaviour)
# ===================================================================


class TestCoverageGapDetection:
    """Tests for get_coverage_gaps covering variant group scenarios."""

    @pytest.mark.asyncio
    async def test_identical_scores_no_gap(self) -> None:
        session = make_session_mock()
        group = MagicMock()
        group.pipeline_id = uuid.uuid4()
        group.variants = [
            {
                "name": "control",
                "eval_definition_ids": [str(uuid.uuid4()), str(uuid.uuid4())],
            },
            {
                "name": "experiment",
                "eval_definition_ids": [str(uuid.uuid4()), str(uuid.uuid4())],
            },
        ]
        all_def_ids = [uuid.uuid4() for _ in range(2)]
        for v in group.variants:
            v["eval_definition_ids"] = [str(eid) for eid in all_def_ids]

        gaps = await get_coverage_gaps(session, group, eval_def_ids=all_def_ids)
        assert gaps == [], f"Expected no gaps, got {gaps}"

    @pytest.mark.asyncio
    async def test_missing_eval_for_one_variant(self) -> None:
        session = make_session_mock()
        group = MagicMock()
        group.pipeline_id = uuid.uuid4()
        missing_id = uuid.uuid4()
        present_id = uuid.uuid4()
        group.variants = [
            {
                "name": "control",
                "eval_definition_ids": [str(present_id)],
            },
            {
                "name": "experiment",
                "eval_definition_ids": [str(present_id)],
            },
        ]

        gaps = await get_coverage_gaps(session, group, eval_def_ids=[present_id, missing_id])
        assert len(gaps) == 2, f"Expected 2 gaps, got {len(gaps)}: {gaps}"
        for gap in gaps:
            assert str(missing_id) in gap["missing_evals"], f"Missing eval not reported in gap: {gap}"

    @pytest.mark.asyncio
    async def test_no_evals_defined_no_gaps(self) -> None:
        session = make_session_mock()
        group = MagicMock()
        group.pipeline_id = uuid.uuid4()
        group.variants = [
            {"name": "control", "eval_definition_ids": []},
            {"name": "experiment", "eval_definition_ids": []},
        ]

        gaps = await get_coverage_gaps(session, group, eval_def_ids=[])
        assert gaps == [], f"Expected no gaps with no evals defined, got {gaps}"


# ===================================================================
#  Future features (awaiting implementation)
# ===================================================================


@pytest.mark.skip(reason="awaiting-implementation — batch_run_variants not yet implemented")
@pytest.mark.asyncio
class TestBatchRunVariants:
    """Batch-run: fire N runs distributed by variant weights."""

    async def test_distributes_by_weight(self) -> None:
        principal = make_mock_principal()
        mock_session = make_session_mock()
        group_id = uuid.uuid4()

        with (
            patch(
                "modulo.api.routes.variants.get_variant_group",
                new_callable=AsyncMock,
            ) as mock_get,
            patch(
                "modulo.api.routes.variants.batch_run_variants",
                new_callable=AsyncMock,
            ) as mock_batch,
        ):
            mock_group = MagicMock()
            mock_group.variants = [
                {
                    "name": "control",
                    "weight": 70.0,
                    "snapshot_id": str(uuid.uuid4()),
                },
                {
                    "name": "experiment",
                    "weight": 30.0,
                    "snapshot_id": str(uuid.uuid4()),
                },
            ]
            mock_get.return_value = mock_group
            mock_batch.return_value = {
                "runs": [{"run_id": uuid.uuid4(), "variant_name": "control"} for _ in range(70)]
                + [{"run_id": uuid.uuid4(), "variant_name": "experiment"} for _ in range(30)]
            }

            from modulo.api.routes.variants import batch_run_variants

            result = await batch_run_variants(group_id, count=100, session=mock_session, principal=principal)

        assert len(result["runs"]) == 100
        control_count = sum(1 for r in result["runs"] if r["variant_name"] == "control")
        assert 50 <= control_count <= 90, f"Expected ~70 control runs, got {control_count}"


@pytest.mark.skip(reason="awaiting-implementation — run_variant_sequential not yet implemented")
@pytest.mark.asyncio
class TestRunVariantSequential:
    """Sequential mode: run variants one at a time in insertion order."""

    async def test_creates_runs_in_insertion_order(self) -> None:
        principal = make_mock_principal()
        mock_session = make_session_mock()
        group_id = uuid.uuid4()

        with (
            patch(
                "modulo.api.routes.variants.get_variant_group",
                new_callable=AsyncMock,
            ) as mock_get,
        ):
            mock_group = MagicMock()
            mock_group.variants = [
                {"name": "step-a", "snapshot_id": str(uuid.uuid4())},
                {"name": "step-b", "snapshot_id": str(uuid.uuid4())},
            ]
            mock_group.selection_strategy = "sequential"
            mock_get.return_value = mock_group

            from modulo.api.routes.variants import run_variant_sequential

            result = await run_variant_sequential(
                group_id,
                input_payload={},
                session=mock_session,
                principal=principal,
            )

        assert len(result["runs"]) == 2
        assert result["runs"][0]["variant_name"] == "step-a"
        assert result["runs"][1]["variant_name"] == "step-b"


@pytest.mark.skip(reason="awaiting-implementation — compare_variants not yet implemented")
@pytest.mark.asyncio
class TestCompareVariants:
    """Comparison view: eval scores per node, token cost, output diffs."""

    async def test_returns_eval_scores_per_variant(self) -> None:
        principal = make_mock_principal()
        mock_session = make_session_mock()
        group_id = uuid.uuid4()

        with (
            patch(
                "modulo.api.routes.variants.get_variant_group",
                new_callable=AsyncMock,
            ) as mock_get,
        ):
            mock_group = MagicMock()
            mock_group.variants = [
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
            mock_get.return_value = mock_group

            from modulo.api.routes.variants import compare_variants

            result = await compare_variants(group_id, session=mock_session, principal=principal)

        variants = result.get("variants", [])
        assert len(variants) == 2
        for v in variants:
            assert "eval_scores" in v
            assert "token_cost" in v


@pytest.mark.skip(reason="awaiting-implementation — get_coverage_signal not yet implemented")
@pytest.mark.asyncio
class TestCoverageSignal:
    """Eval coverage signal: warning when variants diverge but evals agree."""

    async def test_detects_divergence_with_identical_evals(self) -> None:
        principal = make_mock_principal()
        mock_session = make_session_mock()
        group_id = uuid.uuid4()

        with (
            patch(
                "modulo.api.routes.variants.get_variant_group",
                new_callable=AsyncMock,
            ) as mock_get,
        ):
            mock_group = MagicMock()
            mock_group.variants = [
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
            mock_get.return_value = mock_group

            from modulo.api.routes.variants import get_coverage_signal

            result = await get_coverage_signal(group_id, session=mock_session, principal=principal)

        assert "coverage_warning" in result
        assert "Variants diverged but evals did not differentiate" in result["coverage_warning"]
