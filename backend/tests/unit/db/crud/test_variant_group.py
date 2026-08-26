"""Unit tests for variant group CRUD — pure functions only (no DB)."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.db.crud.variant_group import (
    _merge_variant_payload,
    _resolve_prompt_template_override,
    check_pipeline_run_quota_for_batch,
    get_coverage_gaps,
    get_prompt_diffs,
    increment_run_count,
    pick_variant_weighted,
    run_variant_batch,
    run_variant_weighted,
)
from modulo.db.models.agent import Agent


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
        # The override namespace lives in the frozen snapshot, NOT the payload.
        assert "_run_overrides" not in results[0]["merged_payload"]
        assert results[0]["frozen_snapshot"]["_run_overrides"]["model_backend_id"] == "backend-control"
        assert results[1]["frozen_snapshot"]["_run_overrides"]["model_backend_id"] == "backend-experiment"
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
        # The prompt_version control override lives in the frozen snapshot.
        assert results[0]["frozen_snapshot"]["_run_overrides"]["prompt_version"] == "v3"
        assert results[1]["frozen_snapshot"]["_run_overrides"]["prompt_version"] == "v4"
        assert results[0]["merged_payload"]["shared"] == "payload"
        assert "_run_overrides" not in results[0]["merged_payload"]

    async def test_prompt_version_override_resolved_to_template(self) -> None:
        """A prompt_version override resolves to its per-agent templates (FAR-342).

        The batch runner resolves the version label via each agent's
        ``prompt_version_history`` and stores the per-agent template map
        alongside the version under ``_run_overrides`` so the node runner can
        render the right template for each node. The map contains ONLY the
        matching agent.
        """
        session = AsyncMock()
        org_id = uuid.uuid4()
        agent_a = str(uuid.uuid4())
        group = self._make_group()
        group.variants = [
            {
                "name": "v3",
                "snapshot_id": str(uuid.uuid4()),
                "weight": 1.0,
                "run_context_overrides": {"prompt_version": "v3"},
            }
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
            patch(
                "modulo.db.crud.variant_group._resolve_prompt_template_override",
                new_callable=AsyncMock,
                return_value={agent_a: "You are v3."},
            ),
        ):
            results = await run_variant_batch(
                session,
                org_id=org_id,
                group=group,
                input_payload={"shared": "payload"},
            )

        assert results is not None
        overrides = results[0]["frozen_snapshot"]["_run_overrides"]
        assert overrides["prompt_version"] == "v3"
        assert overrides["prompt_templates"] == {agent_a: "You are v3."}
        # The override namespace never leaks into the input payload.
        assert "_run_overrides" not in results[0]["merged_payload"]

    async def test_prompt_version_override_unresolved_leaves_template_unset(self) -> None:
        """An unresolvable prompt_version leaves ``_run_overrides["prompt_templates"]`` unset.

        The node runner then falls back to its snapshot-embedded prompt.
        """
        session = AsyncMock()
        org_id = uuid.uuid4()
        group = self._make_group()
        group.variants = [
            {
                "name": "v9",
                "snapshot_id": str(uuid.uuid4()),
                "weight": 1.0,
                "run_context_overrides": {"prompt_version": "v9"},
            }
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
            patch(
                "modulo.db.crud.variant_group._resolve_prompt_template_override",
                new_callable=AsyncMock,
                return_value={},
            ),
        ):
            results = await run_variant_batch(
                session,
                org_id=org_id,
                group=group,
                input_payload={"shared": "payload"},
            )

        assert results is not None
        overrides = results[0]["frozen_snapshot"]["_run_overrides"]
        assert overrides["prompt_version"] == "v9"
        assert "prompt_templates" not in overrides

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
        assert [r["frozen_snapshot"]["_run_overrides"].get("prompt_version") for r in results] == ["v3", "v4"]

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

    async def test_stamps_same_batch_id_across_all_runs(self) -> None:
        """FAR-332 3c/3i: one fire stamps N runs with the SAME batch_id."""
        session = AsyncMock()
        org_id = uuid.uuid4()
        group = self._make_group()
        group.variants = self._make_variants(["control", "experiment", "variant_c"])

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
            patch(
                "modulo.db.crud.variant_group.create_run",
                new_callable=AsyncMock,
                return_value=mock_run,
            ) as mock_create,
            patch("modulo.db.crud.variant_group.increment_run_count", new_callable=AsyncMock),
        ):
            results = await run_variant_batch(session, org_id=org_id, group=group)

        assert results is not None
        assert len(results) == 3
        batch_ids = {r["batch_id"] for r in results}
        assert len(batch_ids) == 1
        batch_id = batch_ids.pop()
        assert batch_id is not None
        # Every create_run in this batch was stamped with the same batch_id.
        for call in mock_create.await_args_list:
            kwargs = call.kwargs
            assert kwargs["batch_id"] == batch_id
            assert kwargs["variant_group_id"] == group.id

    async def test_second_fire_yields_different_batch_id(self) -> None:
        """FAR-332 3i: two fires produce DIFFERENT batch_ids."""
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

        # First fire.
        with (
            patch(
                "modulo.db.crud.variant_group.check_pipeline_run_quota_for_batch",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch("modulo.db.crud.variant_group.create_run", new_callable=AsyncMock, return_value=mock_run),
            patch("modulo.db.crud.variant_group.increment_run_count", new_callable=AsyncMock),
        ):
            first = await run_variant_batch(session, org_id=org_id, group=group)
        # Second fire.
        with (
            patch(
                "modulo.db.crud.variant_group.check_pipeline_run_quota_for_batch",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch("modulo.db.crud.variant_group.create_run", new_callable=AsyncMock, return_value=mock_run),
            patch("modulo.db.crud.variant_group.increment_run_count", new_callable=AsyncMock),
        ):
            second = await run_variant_batch(session, org_id=org_id, group=group)

        assert first is not None
        assert second is not None
        first_batch = {r["batch_id"] for r in first}
        second_batch = {r["batch_id"] for r in second}
        assert len(first_batch) == 1
        assert len(second_batch) == 1
        assert first_batch != second_batch

    async def test_freezes_snapshot_and_overrides_onto_each_run(self) -> None:
        """FAR-332 3c/3i: each run carries a frozen snapshot + override config."""
        session = AsyncMock()
        org_id = uuid.uuid4()
        group = self._make_group()
        snap_a = str(uuid.uuid4())
        snap_b = str(uuid.uuid4())
        group.variants = [
            {
                "id": "variant-control",
                "name": "control",
                "snapshot_id": snap_a,
                "weight": 1.0,
                "run_context_overrides": {"model_backend_id": "backend-control"},
            },
            {
                "id": "variant-experiment",
                "name": "experiment",
                "snapshot_id": snap_b,
                "weight": 1.0,
                "run_context_overrides": {"model_backend_id": "backend-experiment"},
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
            patch(
                "modulo.db.crud.variant_group.create_run",
                new_callable=AsyncMock,
                return_value=mock_run,
            ) as mock_create,
            patch("modulo.db.crud.variant_group.increment_run_count", new_callable=AsyncMock),
        ):
            results = await run_variant_batch(session, org_id=org_id, group=group)

        assert results is not None
        assert len(results) == 2
        # Result dict carries the frozen snapshot.
        assert results[0]["frozen_snapshot"]["snapshot_id"] == snap_a
        assert results[1]["frozen_snapshot"]["snapshot_id"] == snap_b
        assert results[0]["frozen_snapshot"]["variant_id"] == "variant-control"
        # create_run was handed the frozen config per variant.
        call_snaps = [call.kwargs["variant_config_snapshot"]["snapshot_id"] for call in mock_create.await_args_list]
        assert set(call_snaps) == {snap_a, snap_b}
        assert mock_create.await_args_list[0].kwargs["variant_config_snapshot"]["run_context_overrides"] == {
            "model_backend_id": "backend-control"
        }
        assert mock_create.await_args_list[1].kwargs["variant_config_snapshot"]["run_context_overrides"] == {
            "model_backend_id": "backend-experiment"
        }
        # The system-reserved override namespace rides in the frozen snapshot
        # (not the input payload), and each run was handed it via create_run.
        assert mock_create.await_args_list[0].kwargs["variant_config_snapshot"]["_run_overrides"] == {
            "model_backend_id": "backend-control"
        }
        assert mock_create.await_args_list[1].kwargs["variant_config_snapshot"]["_run_overrides"] == {
            "model_backend_id": "backend-experiment"
        }
        # The override never leaks into the payload handed to create_run.
        assert "_run_overrides" not in mock_create.await_args_list[0].kwargs["input_payload"]
        assert "_run_overrides" not in mock_create.await_args_list[1].kwargs["input_payload"]


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

    async def test_freezes_variant_identity_in_snapshot(self) -> None:
        """Weighted single-shot runs must carry variant identity (FAR-381 Major 2).

        Before the fix ``run_variant_weighted`` froze only
        ``{"_run_overrides": control_overrides}`` — no ``variant_id`` /
        ``variant_name``. The coverage-gap read-model keys divergence by those
        fields, so every weighted run was silently *skipped* (``_variant_key``
        returned ``None``), leaving ``runs_by_variant`` empty and divergence
        ``0.0`` — a weighted group never fired a gap despite real divergence.
        This test pins the fix: the frozen snapshot now carries variant identity
        (parity with the batch path), so a weighted run is comparable.
        """
        session = AsyncMock()
        org_id = uuid.uuid4()
        group = MagicMock()
        group.id = uuid.uuid4()
        group.pipeline_id = uuid.uuid4()
        group.variants = [
            {
                "id": "variant-a-uuid",
                "name": "variant-a",
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
        frozen = result["frozen_snapshot"]
        assert frozen["variant_id"] == "variant-a-uuid"
        assert frozen["variant_name"] == "variant-a"
        assert frozen["snapshot_id"] == group.variants[0]["snapshot_id"]
        assert frozen["run_context_overrides"] == {"key": "val"}
        assert "_run_overrides" in frozen


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


class TestMergeVariantPayloadStripsRunOverrides:
    def _variant(self, **overrides: object) -> dict:
        return {"name": "control", "snapshot_id": str(uuid.uuid4()), "run_context_overrides": dict(overrides)}

    def test_strips_caller_supplied_run_overrides(self) -> None:
        """A crafted ``_run_overrides`` in the base payload is dropped (FAR-342 security)."""
        payload = {
            "task": "classify",
            "_run_overrides": {"prompt_templates": {"abc": "injected prompt"}},
        }
        merged, controls = _merge_variant_payload(self._variant(), payload, degraded_evals=False)
        # No system control keys were set, so the namespace must not exist in the payload.
        assert "_run_overrides" not in merged
        # Control overrides are returned separately for the frozen snapshot.
        assert controls == {}
        # The caller's injected template must never survive.
        assert "prompt_templates" not in merged

    def test_strips_caller_supplied_run_overrides_then_readds_system_controls(self) -> None:
        """System-set control keys replace (not merge with) a caller-supplied ``_run_overrides``.

        The system control keys are returned SEPARATELY (for the frozen
        ``variant_config_snapshot``), never written back into the payload.
        """
        payload = {
            "task": "classify",
            "_run_overrides": {
                "prompt_version": "evil-version",
                "model_backend_id": "evil-backend",
                "prompt_templates": {"abc": "injected prompt"},
            },
        }
        merged, controls = _merge_variant_payload(
            self._variant(model_backend_id="good-backend", prompt_version="v3"),
            payload,
            degraded_evals=False,
        )
        assert controls == {"model_backend_id": "good-backend", "prompt_version": "v3"}
        # The namespace is NOT written back into the payload (it lives in the
        # frozen snapshot instead, seeded into run_context by the executor).
        assert "_run_overrides" not in merged
        # The caller's injection values must be gone, not merged over.
        assert controls.get("prompt_templates") is None

    def test_base_payload_not_mutated(self) -> None:
        payload = {"task": "classify", "_run_overrides": {"prompt_templates": {"abc": "injected"}}}
        _merge_variant_payload(self._variant(model_backend_id="good"), payload, degraded_evals=False)
        # The caller's original dict is untouched (we copy before popping).
        assert payload["_run_overrides"] == {"prompt_templates": {"abc": "injected"}}

    def test_model_override_is_a_system_control_key(self) -> None:
        """A ``model`` override (opencode model ID) flows into ``_run_overrides``.

        FAR-343: ``model`` is a system-reserved control key for ``sandbox_agent``
        nodes — the opencode CLI model ID is rendered into the ``agent_command``
        template via ``{{ run_context._run_overrides.model }}``. Like the other
        control keys it is returned SEPARATELY (stored in the frozen snapshot's
        ``_run_overrides``, seeded into run_context by the executor), never
        written back into the payload.
        """
        merged, controls = _merge_variant_payload(
            self._variant(model="opencode-go/hy3"),
            {"task": "classify"},
            degraded_evals=False,
        )
        assert controls == {"model": "opencode-go/hy3"}
        # The namespace is NOT written back into the payload — it lives in the
        # frozen snapshot so the sandbox_agent command can read it per-run.
        assert "_run_overrides" not in merged

    def test_model_override_kept_out_of_data_payload(self) -> None:
        """A ``model`` override is NOT merged into the payload as a data field.

        ``model`` is a control key, so it must be returned in ``controls`` (the
        frozen ``_run_overrides``) and excluded from the data overrides that get
        written back into the payload.
        """
        merged, controls = _merge_variant_payload(
            self._variant(model="opencode-go/hy3"),
            {"task": "classify"},
            degraded_evals=False,
        )
        assert controls == {"model": "opencode-go/hy3"}
        assert "model" not in merged


@pytest.mark.asyncio
class TestResolvePromptTemplateOverride:
    def _agent(self, agent_id: uuid.UUID, *, current: str, history: list[dict]) -> Agent:
        return Agent(
            id=agent_id,
            name=f"agent-{agent_id}",
            account_id=uuid.uuid4(),
            prompt_template=current,
            prompt_version_history=history,
        )

    def _session(self, *, snapshot_exists: bool, graph_json: object | None) -> AsyncMock:
        session = AsyncMock()
        id_result = MagicMock()
        id_result.scalar_one_or_none.return_value = uuid.uuid4() if snapshot_exists else None
        graph_result = MagicMock()
        graph_result.scalar_one_or_none.return_value = graph_json
        # snapshot_exists=True => run the graph query too; otherwise return early.
        session.execute = AsyncMock(side_effect=[id_result] if not snapshot_exists else [id_result, graph_result])
        return session

    async def test_resolves_matching_version_per_agent(self) -> None:
        """A version carried by an agent resolves to that agent's template."""
        snapshot_id = uuid.uuid4()
        a = self._agent(
            uuid.UUID("22222222-2222-2222-2222-222222222222"),
            current="current-a",
            history=[{"version": "v3", "template": "A v3"}, {"version": "v1", "template": "A v1"}],
        )
        b = self._agent(
            uuid.UUID("33333333-3333-3333-3333-333333333333"),
            current="current-b",
            history=[{"version": "v3", "template": "B v3"}],
        )
        session = self._session(
            snapshot_exists=True,
            graph_json={
                "nodes": [
                    {"agent_id": str(a.id)},
                    {"agent_id": str(b.id)},
                    {"agent_id": "not-a-uuid"},
                    {"nested": True},
                ]
            },
        )

        def _get(session_mock, agent_id):
            by_id = {a.id: a, b.id: b}
            return by_id[agent_id]

        with patch(
            "modulo.db.crud.variant_group.get_agent",
            new_callable=AsyncMock,
            side_effect=_get,
        ):
            resolved = await _resolve_prompt_template_override(session, snapshot_id=snapshot_id, prompt_version="v3")

        assert resolved == {str(a.id): "A v3", str(b.id): "B v3"}

    async def test_unknown_version_omits_agent(self) -> None:
        """An agent not carrying the requested version is omitted from the map."""
        snapshot_id = uuid.uuid4()
        a = self._agent(
            uuid.UUID("22222222-2222-2222-2222-222222222222"),
            current="current-a",
            history=[{"version": "v1", "template": "A v1"}],
        )
        session = self._session(snapshot_exists=True, graph_json={"nodes": [{"agent_id": str(a.id)}]})

        with patch(
            "modulo.db.crud.variant_group.get_agent",
            new_callable=AsyncMock,
            return_value=a,
        ):
            resolved = await _resolve_prompt_template_override(session, snapshot_id=snapshot_id, prompt_version="v9")

        assert resolved == {}

    async def test_current_resolves_to_active_template(self) -> None:
        """``"current"`` resolves to the agent's live ``prompt_template``."""
        snapshot_id = uuid.uuid4()
        a = self._agent(
            uuid.UUID("22222222-2222-2222-2222-222222222222"),
            current="live template",
            history=[{"version": "v3", "template": "A v3"}],
        )
        session = self._session(snapshot_exists=True, graph_json={"nodes": [{"agent_id": str(a.id)}]})

        with patch(
            "modulo.db.crud.variant_group.get_agent",
            new_callable=AsyncMock,
            return_value=a,
        ):
            resolved = await _resolve_prompt_template_override(
                session, snapshot_id=snapshot_id, prompt_version="current"
            )

        assert resolved == {str(a.id): "live template"}

    async def test_empty_templates_omitted(self) -> None:
        """Agents whose resolved template is empty are omitted."""
        snapshot_id = uuid.uuid4()
        a = self._agent(
            uuid.UUID("22222222-2222-2222-2222-222222222222"),
            current="",
            history=[{"version": "v3", "template": ""}],
        )
        session = self._session(snapshot_exists=True, graph_json={"nodes": [{"agent_id": str(a.id)}]})

        with patch(
            "modulo.db.crud.variant_group.get_agent",
            new_callable=AsyncMock,
            return_value=a,
        ):
            resolved = await _resolve_prompt_template_override(session, snapshot_id=snapshot_id, prompt_version="v3")

        assert resolved == {}

    async def test_result_is_deterministic_and_keyed_per_agent(self) -> None:
        """Iteration is over sorted agent ids, so the map is deterministic and keyed per-agent."""
        snapshot_id = uuid.uuid4()
        # Deliberately unordered in the graph so we prove the sort.
        low = self._agent(
            uuid.UUID("11111111-1111-1111-1111-111111111111"),
            current="low",
            history=[{"version": "v3", "template": "low v3"}],
        )
        high = self._agent(
            uuid.UUID("99999999-9999-9999-9999-999999999999"),
            current="high",
            history=[{"version": "v3", "template": "high v3"}],
        )
        mid = self._agent(
            uuid.UUID("55555555-5555-5555-5555-555555555555"),
            current="mid",
            history=[{"version": "v3", "template": "mid v3"}],
        )
        session = self._session(
            snapshot_exists=True,
            graph_json={"nodes": [{"agent_id": str(high.id)}, {"agent_id": str(low.id)}, {"agent_id": str(mid.id)}]},
        )

        def _get(session_mock, agent_id):
            by_id = {low.id: low, mid.id: mid, high.id: high}
            return by_id[agent_id]

        with patch(
            "modulo.db.crud.variant_group.get_agent",
            new_callable=AsyncMock,
            side_effect=_get,
        ):
            resolved = await _resolve_prompt_template_override(session, snapshot_id=snapshot_id, prompt_version="v3")

        # Keyed per-agent with each agent's OWN template — never a single run-wide value.
        assert resolved == {str(low.id): "low v3", str(mid.id): "mid v3", str(high.id): "high v3"}
        assert list(resolved) == sorted(resolved)

    async def test_returns_empty_when_snapshot_missing(self) -> None:
        """A missing snapshot short-circuits to an empty map (node falls back to its prompt)."""
        session = self._session(snapshot_exists=False, graph_json=None)
        resolved = await _resolve_prompt_template_override(session, snapshot_id=uuid.uuid4(), prompt_version="v3")
        assert resolved == {}

    async def test_returns_empty_when_graph_not_a_dict(self) -> None:
        """A non-dict ``graph_json`` yields an empty map."""
        snapshot_id = uuid.uuid4()
        session = self._session(snapshot_exists=True, graph_json="not-a-dict")
        resolved = await _resolve_prompt_template_override(session, snapshot_id=snapshot_id, prompt_version="v3")
        assert resolved == {}
