"""Unit tests for variant group CRUD — pure functions only (no DB)."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.db.crud.variant_group import (
    check_pipeline_run_quota_for_batch,
    get_coverage_gaps,
    get_prompt_diffs,
    increment_run_count,
    pick_variant_weighted,
    run_variant_batch,
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

    async def test_increments_by_delta(self) -> None:
        session = AsyncMock()
        group_id = uuid.uuid4()
        mock_group = MagicMock()
        mock_group.run_count = 2
        result_mock = MagicMock()
        result_mock.scalar_one_or_none = MagicMock(return_value=mock_group)
        session.execute = AsyncMock(return_value=result_mock)

        returned = await increment_run_count(session, group_id, delta=3)

        assert returned is mock_group
        assert mock_group.run_count == 5
        session.flush.assert_awaited_once()


@pytest.mark.asyncio
class TestCheckPipelineRunQuotaForBatch:
    async def test_allows_when_headroom_for_whole_batch(self) -> None:
        session = AsyncMock()
        group = MagicMock()
        group.pipeline_id = uuid.uuid4()
        group.max_concurrent_runs = 5

        with patch(
            "modulo.db.crud.variant_group.count_active_runs_for_pipeline",
            new_callable=AsyncMock,
            return_value=3,
        ):
            assert await check_pipeline_run_quota_for_batch(session, group, batch_size=2) is True

    async def test_rejects_when_batch_breaches_quota(self) -> None:
        session = AsyncMock()
        group = MagicMock()
        group.pipeline_id = uuid.uuid4()
        group.max_concurrent_runs = 5

        with patch(
            "modulo.db.crud.variant_group.count_active_runs_for_pipeline",
            new_callable=AsyncMock,
            return_value=4,
        ):
            assert await check_pipeline_run_quota_for_batch(session, group, batch_size=2) is False

    async def test_rejects_at_exactly_quota(self) -> None:
        session = AsyncMock()
        group = MagicMock()
        group.pipeline_id = uuid.uuid4()
        group.max_concurrent_runs = 2

        with patch(
            "modulo.db.crud.variant_group.count_active_runs_for_pipeline",
            new_callable=AsyncMock,
            return_value=2,
        ):
            assert await check_pipeline_run_quota_for_batch(session, group, batch_size=1) is False


@pytest.mark.asyncio
class TestRunVariantBatch:
    def _make_group(self, *, degraded_evals: bool = False) -> MagicMock:
        group = MagicMock()
        group.id = uuid.uuid4()
        group.pipeline_id = uuid.uuid4()
        group.degraded_evals = degraded_evals
        group.max_concurrent_runs = 5
        return group

    def _make_locked(self, group: MagicMock) -> MagicMock:
        locked = MagicMock()
        locked.id = group.id
        locked.pipeline_id = group.pipeline_id
        locked.variants = group.variants
        locked.degraded_evals = group.degraded_evals
        locked.max_concurrent_runs = group.max_concurrent_runs
        return locked

    def _make_variants(self, names: list[str]) -> list[dict]:
        return [
            {
                "name": name,
                "snapshot_id": str(uuid.uuid4()),
                "weight": 1.0,
                "run_context_overrides": {"model_backend_id": f"backend-{name}"},
            }
            for name in names
        ]

    async def test_fires_one_run_per_variant_in_insertion_order(self) -> None:
        session = AsyncMock()
        org_id = uuid.uuid4()
        group = self._make_group()
        group.variants = self._make_variants(["control", "experiment"])

        locked = self._make_locked(group)
        exec_result = MagicMock()
        exec_result.scalar_one_or_none.return_value = locked
        session.execute.return_value = exec_result

        mock_run = MagicMock()
        mock_run.id = uuid.uuid4()

        with (
            patch(
                "modulo.db.crud.variant_group.check_pipeline_run_quota_for_batch",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch("modulo.db.crud.variant_group.create_run", new_callable=AsyncMock, return_value=mock_run),
            patch("modulo.db.crud.variant_group.increment_run_count", new_callable=AsyncMock) as mock_inc,
        ):
            results = await run_variant_batch(
                session,
                org_id=org_id,
                group=group,
                input_payload={"shared": "payload"},
            )

        assert results is not None
        assert len(results) == 2
        assert [r["variant"]["name"] for r in results] == ["control", "experiment"]
        assert results[0]["run_id"] == mock_run.id
        assert results[0]["merged_payload"]["shared"] == "payload"
        assert results[0]["merged_payload"]["model_backend_id"] == "backend-control"
        assert results[1]["merged_payload"]["model_backend_id"] == "backend-experiment"
        mock_inc.assert_awaited_once_with(session, group.id, delta=2)

    async def test_prompt_version_override_merged_into_payload(self) -> None:
        """Prompt version comparison via run_context_overrides.prompt_version (PRD 8.19)."""
        session = AsyncMock()
        org_id = uuid.uuid4()
        group = self._make_group()
        group.variants = [
            {
                "name": "v3",
                "snapshot_id": str(uuid.uuid4()),
                "weight": 1.0,
                "run_context_overrides": {"prompt_version": "v3"},
            },
            {
                "name": "v4",
                "snapshot_id": str(uuid.uuid4()),
                "weight": 1.0,
                "run_context_overrides": {"prompt_version": "v4"},
            },
        ]

        locked = self._make_locked(group)
        exec_result = MagicMock()
        exec_result.scalar_one_or_none.return_value = locked
        session.execute.return_value = exec_result

        mock_run = MagicMock()
        mock_run.id = uuid.uuid4()

        with (
            patch(
                "modulo.db.crud.variant_group.check_pipeline_run_quota_for_batch",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch("modulo.db.crud.variant_group.create_run", new_callable=AsyncMock, return_value=mock_run),
            patch("modulo.db.crud.variant_group.increment_run_count", new_callable=AsyncMock),
        ):
            results = await run_variant_batch(
                session,
                org_id=org_id,
                group=group,
                input_payload={"shared": "payload"},
            )

        assert results is not None
        assert results[0]["merged_payload"]["prompt_version"] == "v3"
        assert results[1]["merged_payload"]["prompt_version"] == "v4"
        assert results[0]["merged_payload"]["shared"] == "payload"

    async def test_returns_none_when_quota_exceeded_for_batch(self) -> None:
        session = AsyncMock()
        org_id = uuid.uuid4()
        group = self._make_group()
        group.variants = self._make_variants(["control", "experiment"])

        locked = self._make_locked(group)
        exec_result = MagicMock()
        exec_result.scalar_one_or_none.return_value = locked
        session.execute.return_value = exec_result

        with (
            patch(
                "modulo.db.crud.variant_group.check_pipeline_run_quota_for_batch",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch("modulo.db.crud.variant_group.create_run", new_callable=AsyncMock) as mock_create,
        ):
            result = await run_variant_batch(session, org_id=org_id, group=group)

        assert result is None
        mock_create.assert_not_called()

    async def test_returns_none_when_no_variants(self) -> None:
        session = AsyncMock()
        org_id = uuid.uuid4()
        group = self._make_group()
        group.variants = []

        locked = self._make_locked(group)
        exec_result = MagicMock()
        exec_result.scalar_one_or_none.return_value = locked
        session.execute.return_value = exec_result

        with patch("modulo.db.crud.variant_group.create_run", new_callable=AsyncMock) as mock_create:
            result = await run_variant_batch(session, org_id=org_id, group=group)

        assert result is None
        mock_create.assert_not_called()

    async def test_returns_none_when_any_variant_missing_snapshot_id(self) -> None:
        session = AsyncMock()
        org_id = uuid.uuid4()
        group = self._make_group()
        group.variants = [
            {"name": "ok", "snapshot_id": str(uuid.uuid4())},
            {"name": "no-sid", "weight": 1.0},
        ]

        locked = self._make_locked(group)
        exec_result = MagicMock()
        exec_result.scalar_one_or_none.return_value = locked
        session.execute.return_value = exec_result

        with (
            patch(
                "modulo.db.crud.variant_group.check_pipeline_run_quota_for_batch",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch("modulo.db.crud.variant_group.create_run", new_callable=AsyncMock) as mock_create,
        ):
            result = await run_variant_batch(session, org_id=org_id, group=group)

        assert result is None
        mock_create.assert_not_called()

    async def test_returns_none_when_group_deleted(self) -> None:
        session = AsyncMock()
        org_id = uuid.uuid4()
        group = self._make_group()

        exec_result = MagicMock()
        exec_result.scalar_one_or_none.return_value = None
        session.execute.return_value = exec_result

        with patch("modulo.db.crud.variant_group.create_run", new_callable=AsyncMock) as mock_create:
            result = await run_variant_batch(session, org_id=org_id, group=group)

        assert result is None
        mock_create.assert_not_called()

    async def test_merges_prompt_version_override_into_payload(self) -> None:
        """PRD 8.19: prompt version comparison via run_context_overrides ``prompt_version`` key."""
        session = AsyncMock()
        org_id = uuid.uuid4()
        group = self._make_group()
        group.variants = [
            {
                "name": "control",
                "snapshot_id": str(uuid.uuid4()),
                "weight": 1.0,
                "run_context_overrides": {"prompt_version": "v3"},
            },
            {
                "name": "experiment",
                "snapshot_id": str(uuid.uuid4()),
                "weight": 1.0,
                "run_context_overrides": {"prompt_version": "v4"},
            },
        ]

        locked = self._make_locked(group)
        exec_result = MagicMock()
        exec_result.scalar_one_or_none.return_value = locked
        session.execute.return_value = exec_result

        mock_run = MagicMock()
        mock_run.id = uuid.uuid4()

        with (
            patch(
                "modulo.db.crud.variant_group.check_pipeline_run_quota_for_batch",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch("modulo.db.crud.variant_group.create_run", new_callable=AsyncMock, return_value=mock_run),
            patch("modulo.db.crud.variant_group.increment_run_count", new_callable=AsyncMock),
        ):
            results = await run_variant_batch(session, org_id=org_id, group=group, input_payload={"topic": "x"})

        assert results is not None
        assert [r["merged_payload"].get("prompt_version") for r in results] == ["v3", "v4"]

    async def test_injects_degraded_evals_flag_into_each_run(self) -> None:
        session = AsyncMock()
        org_id = uuid.uuid4()
        group = self._make_group(degraded_evals=True)
        group.variants = self._make_variants(["control", "experiment"])

        locked = self._make_locked(group)
        exec_result = MagicMock()
        exec_result.scalar_one_or_none.return_value = locked
        session.execute.return_value = exec_result

        mock_run = MagicMock()
        mock_run.id = uuid.uuid4()

        with (
            patch(
                "modulo.db.crud.variant_group.check_pipeline_run_quota_for_batch",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch("modulo.db.crud.variant_group.create_run", new_callable=AsyncMock, return_value=mock_run),
            patch("modulo.db.crud.variant_group.increment_run_count", new_callable=AsyncMock),
        ):
            results = await run_variant_batch(session, org_id=org_id, group=group)

        assert results is not None
        for r in results:
            assert r["merged_payload"]["_degraded_evals"] is True

    async def test_filters_non_dict_variants(self) -> None:
        session = AsyncMock()
        org_id = uuid.uuid4()
        group = self._make_group()
        group.variants = [
            {"name": "valid", "snapshot_id": str(uuid.uuid4())},
            "not-a-dict",
        ]

        locked = self._make_locked(group)
        exec_result = MagicMock()
        exec_result.scalar_one_or_none.return_value = locked
        session.execute.return_value = exec_result

        mock_run = MagicMock()
        mock_run.id = uuid.uuid4()

        with (
            patch(
                "modulo.db.crud.variant_group.check_pipeline_run_quota_for_batch",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch("modulo.db.crud.variant_group.create_run", new_callable=AsyncMock, return_value=mock_run),
            patch("modulo.db.crud.variant_group.increment_run_count", new_callable=AsyncMock) as mock_inc,
        ):
            results = await run_variant_batch(session, org_id=org_id, group=group)

        assert results is not None
        assert len(results) == 1
        assert results[0]["variant"]["name"] == "valid"
        mock_inc.assert_awaited_once_with(session, group.id, delta=1)


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

    async def test_returns_empty_when_prompt_pins_json_empty_list(self) -> None:
        """Empty ``prompt_pins_json`` yields no agent diffs (edge case coverage)."""
        session = AsyncMock()
        snap1 = MagicMock()
        snap1.id = uuid.uuid4()
        snap1.prompt_pins_json = [{"agent_id": "agent_a", "prompt_version_hash": "hash_v1"}]
        snap2 = MagicMock()
        snap2.id = uuid.uuid4()
        snap2.prompt_pins_json = []

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
