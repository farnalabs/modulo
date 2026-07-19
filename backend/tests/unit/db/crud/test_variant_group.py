"""Unit tests for variant group CRUD — pure functions only (no DB)."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.db.crud.variant_group import (
    get_coverage_gaps,
    get_prompt_diffs,
    increment_run_count,
    pick_variant_weighted,
    run_variant_weighted,
)


class TestPickVariantWeighted:
    def test_empty_variants_returns_none(self) -> None:
        assert pick_variant_weighted([]) is None

    def test_single_variant_returns_directly(self) -> None:
        variant = {"name": "control", "snapshot_id": str(uuid.uuid4()), "weight": 1.0}
        assert pick_variant_weighted([variant]) is variant

    def test_weighted_selection_respects_weights(self) -> None:
        variants = [
            {"name": "control", "weight": 99.0},
            {"name": "variant_a", "weight": 1.0},
        ]
        # Run many iterations to ensure both can be picked.
        selections: set[str] = set()
        for _ in range(2000):
            v = pick_variant_weighted(variants)
            if v:
                selections.add(v["name"])
        assert "control" in selections
        assert "variant_a" in selections

    def test_all_zero_weights_falls_back_to_random(self) -> None:
        variants = [
            {"name": "a", "weight": 0.0},
            {"name": "b", "weight": 0.0},
        ]
        result = pick_variant_weighted(variants)
        assert result in variants

    def test_missing_weight_defaults_to_1(self) -> None:
        variants = [
            {"name": "a"},
            {"name": "b"},
        ]
        result = pick_variant_weighted(variants)
        assert result is not None
        assert result["name"] in ("a", "b")

    def test_weighted_selection_distribution(self) -> None:
        variants = [
            {"name": "control", "weight": 100},
            {"name": "variant_a", "weight": 1},
        ]
        control_count = 0
        trials = 5000
        for _ in range(trials):
            result = pick_variant_weighted(variants)
            assert result is not None
            if result["name"] == "control":
                control_count += 1
        # Control should be picked the vast majority of the time.
        assert control_count > trials * 0.85

    def test_variants_without_weight_key(self) -> None:
        variants = [{"name": "x"}, {"name": "y"}, {"name": "z"}]
        seen: set[str] = set()
        for _ in range(300):
            result = pick_variant_weighted(variants)
            assert result is not None
            seen.add(result["name"])
        assert seen == {"x", "y", "z"}


@pytest.mark.asyncio
class TestIncrementRunCount:
    async def test_increments_count(self) -> None:
        session = AsyncMock()
        group_id = uuid.uuid4()
        mock_group = MagicMock()
        mock_group.run_count = 5
        result_mock = MagicMock()
        result_mock.scalar_one_or_none = MagicMock(return_value=mock_group)
        session.execute = AsyncMock(return_value=result_mock)

        returned = await increment_run_count(session, group_id)

        assert returned is mock_group
        assert mock_group.run_count == 6
        session.execute.assert_awaited_once()
        session.flush.assert_awaited_once()

    async def test_returns_none_when_not_found(self) -> None:
        session = AsyncMock()
        group_id = uuid.uuid4()
        result_mock = MagicMock()
        result_mock.scalar_one_or_none = MagicMock(return_value=None)
        session.execute = AsyncMock(return_value=result_mock)

        returned = await increment_run_count(session, group_id)

        assert returned is None
        session.execute.assert_awaited_once()
        session.flush.assert_not_called()


@pytest.mark.asyncio
class TestRunVariantWeighted:
    async def test_creates_run_successfully(self) -> None:
        session = AsyncMock()
        org_id = uuid.uuid4()
        group = MagicMock()
        group.id = uuid.uuid4()
        group.pipeline_id = uuid.uuid4()
        group.variants = [
            {
                "name": "test",
                "snapshot_id": str(uuid.uuid4()),
                "weight": 1.0,
                "run_context_overrides": {"key": "val"},
            }
        ]
        group.degraded_evals = False
        group.max_concurrent_runs = 5

        locked = MagicMock()
        locked.id = group.id
        locked.pipeline_id = group.pipeline_id
        locked.variants = group.variants
        locked.degraded_evals = False
        locked.max_concurrent_runs = 5
        exec_result = MagicMock()
        exec_result.scalar_one_or_none.return_value = locked
        session.execute.return_value = exec_result

        mock_run = MagicMock()
        mock_run.id = uuid.uuid4()

        with (
            patch("modulo.db.crud.variant_group.check_pipeline_run_quota", new_callable=AsyncMock, return_value=True),
            patch("modulo.db.crud.variant_group.pick_variant_weighted", return_value=group.variants[0]),
            patch("modulo.db.crud.variant_group.create_run", new_callable=AsyncMock, return_value=mock_run),
            patch("modulo.db.crud.variant_group.increment_run_count", new_callable=AsyncMock),
        ):
            result = await run_variant_weighted(session, org_id=org_id, group=group, input_payload={"existing": "data"})

        assert result is not None
        assert result["run_id"] == mock_run.id
        assert result["variant"] == group.variants[0]
        assert result["merged_payload"]["existing"] == "data"
        assert result["merged_payload"]["key"] == "val"

    async def test_returns_none_when_quota_exceeded(self) -> None:
        session = AsyncMock()
        org_id = uuid.uuid4()
        group = MagicMock()
        group.id = uuid.uuid4()
        group.pipeline_id = uuid.uuid4()
        group.variants = [{"name": "test", "snapshot_id": str(uuid.uuid4()), "weight": 1.0}]
        group.degraded_evals = False
        group.max_concurrent_runs = 5

        locked = MagicMock()
        locked.id = group.id
        locked.pipeline_id = group.pipeline_id
        locked.variants = group.variants
        locked.degraded_evals = False
        locked.max_concurrent_runs = 5
        exec_result = MagicMock()
        exec_result.scalar_one_or_none.return_value = locked
        session.execute.return_value = exec_result

        with patch("modulo.db.crud.variant_group.check_pipeline_run_quota", new_callable=AsyncMock, return_value=False):
            result = await run_variant_weighted(session, org_id=org_id, group=group)

        assert result is None

    async def test_returns_none_when_group_deleted(self) -> None:
        session = AsyncMock()
        org_id = uuid.uuid4()
        group = MagicMock()
        group.id = uuid.uuid4()

        exec_result = MagicMock()
        exec_result.scalar_one_or_none.return_value = None
        session.execute.return_value = exec_result

        result = await run_variant_weighted(session, org_id=org_id, group=group)

        assert result is None

    async def test_returns_none_when_no_variant_selected(self) -> None:
        session = AsyncMock()
        org_id = uuid.uuid4()
        group = MagicMock()
        group.id = uuid.uuid4()
        group.pipeline_id = uuid.uuid4()
        group.variants = []
        group.degraded_evals = False
        group.max_concurrent_runs = 5

        locked = MagicMock()
        locked.id = group.id
        locked.pipeline_id = group.pipeline_id
        locked.variants = []
        locked.degraded_evals = False
        locked.max_concurrent_runs = 5
        exec_result = MagicMock()
        exec_result.scalar_one_or_none.return_value = locked
        session.execute.return_value = exec_result

        with (
            patch("modulo.db.crud.variant_group.check_pipeline_run_quota", new_callable=AsyncMock, return_value=True),
            patch("modulo.db.crud.variant_group.pick_variant_weighted", return_value=None),
        ):
            result = await run_variant_weighted(session, org_id=org_id, group=group)

        assert result is None

    async def test_returns_none_when_snapshot_id_missing(self) -> None:
        session = AsyncMock()
        org_id = uuid.uuid4()
        group = MagicMock()
        group.id = uuid.uuid4()
        group.pipeline_id = uuid.uuid4()
        group.variants = [{"name": "no-sid-variant", "weight": 1.0}]
        group.degraded_evals = False
        group.max_concurrent_runs = 5

        locked = MagicMock()
        locked.id = group.id
        locked.pipeline_id = group.pipeline_id
        locked.variants = group.variants
        locked.degraded_evals = False
        locked.max_concurrent_runs = 5
        exec_result = MagicMock()
        exec_result.scalar_one_or_none.return_value = locked
        session.execute.return_value = exec_result

        with patch("modulo.db.crud.variant_group.check_pipeline_run_quota", new_callable=AsyncMock, return_value=True):
            result = await run_variant_weighted(session, org_id=org_id, group=group)

        assert result is None

    async def test_injects_degraded_evals_flag(self) -> None:
        session = AsyncMock()
        org_id = uuid.uuid4()
        group = MagicMock()
        group.id = uuid.uuid4()
        group.pipeline_id = uuid.uuid4()
        group.variants = [{"name": "test", "snapshot_id": str(uuid.uuid4()), "weight": 1.0}]
        group.degraded_evals = True
        group.max_concurrent_runs = 5

        locked = MagicMock()
        locked.id = group.id
        locked.pipeline_id = group.pipeline_id
        locked.variants = group.variants
        locked.degraded_evals = True
        locked.max_concurrent_runs = 5
        exec_result = MagicMock()
        exec_result.scalar_one_or_none.return_value = locked
        session.execute.return_value = exec_result

        mock_run = MagicMock()
        mock_run.id = uuid.uuid4()

        with (
            patch("modulo.db.crud.variant_group.check_pipeline_run_quota", new_callable=AsyncMock, return_value=True),
            patch("modulo.db.crud.variant_group.pick_variant_weighted", return_value=group.variants[0]),
            patch("modulo.db.crud.variant_group.create_run", new_callable=AsyncMock, return_value=mock_run),
            patch("modulo.db.crud.variant_group.increment_run_count", new_callable=AsyncMock),
        ):
            result = await run_variant_weighted(session, org_id=org_id, group=group)

        assert result is not None
        assert result["merged_payload"]["_degraded_evals"] is True


@pytest.mark.asyncio
class TestGetCoverageGaps:
    async def test_returns_empty_when_no_gaps(self) -> None:
        session = AsyncMock()
        group = MagicMock()
        group.pipeline_id = uuid.uuid4()
        group.variants = [
            {
                "name": "covered",
                "snapshot_id": str(uuid.uuid4()),
                "eval_definition_ids": [str(uuid.uuid4())],
            }
        ]

        exec_result = MagicMock()
        exec_result.scalars.return_value = []
        session.execute.return_value = exec_result

        result = await get_coverage_gaps(session, group)
        assert result == []

    async def test_detects_missing_evals(self) -> None:
        session = AsyncMock()
        eval_id = uuid.uuid4()
        group = MagicMock()
        group.pipeline_id = uuid.uuid4()
        group.variants = [
            {
                "name": "no-evals",
                "snapshot_id": str(uuid.uuid4()),
                "eval_definition_ids": [],
            }
        ]

        mock_eval = MagicMock()
        mock_eval.id = eval_id
        exec_result = MagicMock()
        exec_result.scalars.return_value = [mock_eval]
        session.execute.return_value = exec_result

        result = await get_coverage_gaps(session, group)
        assert len(result) == 1
        assert result[0]["variant"]["name"] == "no-evals"
        assert str(eval_id) in result[0]["missing_evals"]

    async def test_uses_provided_eval_def_ids(self) -> None:
        session = AsyncMock()
        group = MagicMock()
        group.pipeline_id = uuid.uuid4()
        eval_id = uuid.uuid4()
        group.variants = [
            {
                "name": "partial",
                "snapshot_id": str(uuid.uuid4()),
                "eval_definition_ids": [str(eval_id)],
            }
        ]

        result = await get_coverage_gaps(session, group, eval_def_ids=[eval_id])
        assert result == []

    async def test_reports_variant_with_partial_coverage(self) -> None:
        session = AsyncMock()
        group = MagicMock()
        group.pipeline_id = uuid.uuid4()
        eval_a = uuid.uuid4()
        eval_b = uuid.uuid4()
        group.variants = [
            {
                "name": "partial",
                "snapshot_id": str(uuid.uuid4()),
                "eval_definition_ids": [str(eval_a)],
            }
        ]

        result = await get_coverage_gaps(session, group, eval_def_ids=[eval_a, eval_b])
        assert len(result) == 1
        assert result[0]["variant"]["name"] == "partial"
        assert str(eval_b) in result[0]["missing_evals"]
        assert str(eval_a) not in result[0]["missing_evals"]

    async def test_handles_variant_without_eval_definition_ids_key(self) -> None:
        session = AsyncMock()
        group = MagicMock()
        group.pipeline_id = uuid.uuid4()
        eval_id = uuid.uuid4()
        group.variants = [
            {
                "name": "no-ids-key",
                "snapshot_id": str(uuid.uuid4()),
            }
        ]

        result = await get_coverage_gaps(session, group, eval_def_ids=[eval_id])
        assert len(result) == 1
        assert result[0]["variant"]["name"] == "no-ids-key"
        assert str(eval_id) in result[0]["missing_evals"]


class TestGetPromptDiffsMissingSnapshotId:
    async def test_skips_variants_without_snapshot_id(self) -> None:
        session = AsyncMock()
        group = MagicMock()
        group.variants = [
            {"name": "variant-without-sid"},
            {"name": "variant-with-sid", "snapshot_id": str(uuid.uuid4())},
        ]

        exec_result = MagicMock()
        exec_result.scalars.return_value = []
        session.execute.return_value = exec_result

        result = await get_prompt_diffs(session, group, base_snapshot_ids=[uuid.uuid4()])

        assert result == []


class TestGetPromptDiffs:
    async def test_returns_empty_when_no_snapshots(self) -> None:
        session = AsyncMock()
        group = MagicMock()
        group.variants = []

        result = await get_prompt_diffs(session, group)

        assert result == []

    async def test_detects_hash_differences(self) -> None:
        session = AsyncMock()
        snap1 = MagicMock()
        snap1.id = uuid.uuid4()
        snap1.prompt_pins_json = [{"agent_id": "agent_a", "prompt_version_hash": "hash_v1"}]
        snap2 = MagicMock()
        snap2.id = uuid.uuid4()
        snap2.prompt_pins_json = [{"agent_id": "agent_a", "prompt_version_hash": "hash_v2"}]

        group = MagicMock()
        group.variants = [
            {"name": "base", "snapshot_id": str(snap1.id)},
            {"name": "variant", "snapshot_id": str(snap2.id)},
        ]

        exec_result = MagicMock()
        exec_result.scalars.return_value = [snap1, snap2]
        session.execute.return_value = exec_result

        result = await get_prompt_diffs(session, group, base_snapshot_ids=[snap1.id])

        assert len(result) == 1
        assert result[0]["agent_diffs"][0]["agent_id"] == "agent_a"
        assert result[0]["agent_diffs"][0]["base_hash"] == "hash_v1"
        assert result[0]["agent_diffs"][0]["variant_hash"] == "hash_v2"

    async def test_skips_missing_snapshots(self) -> None:
        session = AsyncMock()
        snap1 = MagicMock()
        snap1.id = uuid.uuid4()
        snap1.prompt_pins_json = [{"agent_id": "agent_a", "prompt_version_hash": "hash_v1"}]
        snap2_id = uuid.uuid4()

        group = MagicMock()
        group.variants = [
            {"name": "base", "snapshot_id": str(snap1.id)},
            {"name": "variant", "snapshot_id": str(snap2_id)},
        ]

        exec_result = MagicMock()
        exec_result.scalars.return_value = [snap1]
        session.execute.return_value = exec_result

        result = await get_prompt_diffs(session, group, base_snapshot_ids=[snap1.id])

        assert result == []

    async def test_no_diffs_when_hashes_match(self) -> None:
        session = AsyncMock()
        snap1 = MagicMock()
        snap1.id = uuid.uuid4()
        snap1.prompt_pins_json = [{"agent_id": "agent_a", "prompt_version_hash": "same_hash"}]
        snap2 = MagicMock()
        snap2.id = uuid.uuid4()
        snap2.prompt_pins_json = [{"agent_id": "agent_a", "prompt_version_hash": "same_hash"}]

        group = MagicMock()
        group.variants = [
            {"name": "base", "snapshot_id": str(snap1.id)},
            {"name": "variant", "snapshot_id": str(snap2.id)},
        ]

        exec_result = MagicMock()
        exec_result.scalars.return_value = [snap1, snap2]
        session.execute.return_value = exec_result

        result = await get_prompt_diffs(session, group, base_snapshot_ids=[snap1.id])

        assert result == []

    async def test_handles_none_prompt_pins_json(self) -> None:
        session = AsyncMock()
        snap = MagicMock()
        snap.id = uuid.uuid4()
        snap.prompt_pins_json = None

        group = MagicMock()
        group.variants = [
            {"name": "base", "snapshot_id": str(snap.id)},
        ]

        exec_result = MagicMock()
        exec_result.scalars.return_value = [snap]
        session.execute.return_value = exec_result

        result = await get_prompt_diffs(session, group, base_snapshot_ids=[snap.id])

        assert result == []


class TestGetPromptDiffsNonDictSnapshot:
    async def test_handles_non_list_prompt_pins_json(self) -> None:
        session = AsyncMock()
        snap = MagicMock()
        snap.id = uuid.uuid4()
        snap.prompt_pins_json = "not-a-list"

        group = MagicMock()
        group.variants = [
            {"name": "base", "snapshot_id": str(snap.id)},
        ]

        exec_result = MagicMock()
        exec_result.scalars.return_value = [snap]
        session.execute.return_value = exec_result

        result = await get_prompt_diffs(session, group, base_snapshot_ids=[snap.id])

        assert result == []


class TestPickVariantWeightedNonDict:
    def test_skips_non_dict_variants(self) -> None:
        variants = [
            {"name": "valid", "weight": 1.0},
            "not-a-dict",
            42,
            None,
        ]
        result = pick_variant_weighted(variants)
        assert result is not None
        assert result["name"] == "valid"

    def test_returns_none_when_all_are_non_dict(self) -> None:
        variants = ["bad", 42, None, [1, 2, 3]]
        result = pick_variant_weighted(variants)
        assert result is None
