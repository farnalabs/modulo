"""Unit tests for variant group API — pure function tests (no DB, no auth)."""

import uuid
from datetime import UTC
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError, ProgrammingError, SQLAlchemyError

from modulo.api.routes.variants import (
    CreateVariantGroupRequest,
    VariantDef,
    _variant_to_response,
    batch_compare,
    coverage_gaps,
    create_group,
    delete_group,
    get_group,
    list_groups,
    prompt_diffs,
    run_batch,
    run_variant,
    update_group,
)
from tests.unit.api.mock_session import configure_mock_session


def make_session_mock() -> AsyncMock:
    """Create an AsyncSession mock that supports async with session.begin()."""
    session = configure_mock_session(AsyncMock())
    session.execute = AsyncMock()
    begin_ctx = AsyncMock()
    begin_ctx.__aenter__ = AsyncMock(return_value=session)
    begin_ctx.__aexit__ = AsyncMock(return_value=None)
    session.begin = MagicMock(return_value=begin_ctx)
    return session


def make_mock_principal(**kwargs: object) -> MagicMock:
    p = MagicMock()
    p.organisation_id = kwargs.get("org_id", uuid.uuid4())
    p.account_id = kwargs.get("user_id", uuid.uuid4())
    p.username = kwargs.get("username", "test_user")
    p.org_role = kwargs.get("org_role", "admin")
    return p


class TestVariantToResponse:
    def test_converts_model_to_dict(self) -> None:
        group = MagicMock()
        group.id = uuid.uuid4()
        group.pipeline_id = uuid.uuid4()
        group.name = "test-group"
        group.description = "desc"
        group.variants = [{"name": "control", "weight": 1.0}]
        group.selection_strategy = "weighted"
        group.run_count = 5
        group.max_concurrent_runs = 5
        group.degraded_evals = False
        from datetime import datetime

        group.created_at = datetime.now(UTC)
        group.updated_at = datetime.now(UTC)

        result = _variant_to_response(group)
        assert result["name"] == "test-group"
        assert result["run_count"] == 5
        assert isinstance(result["id"], uuid.UUID)


@pytest.mark.asyncio
class TestCreateGroup:
    async def test_creates_group_successfully(self) -> None:
        principal = make_mock_principal()
        mock_session = make_session_mock()

        with patch(
            "modulo.api.routes.variants.create_variant_group",
            new_callable=AsyncMock,
        ) as mock_create:
            mock_group = MagicMock()
            mock_group.id = uuid.uuid4()
            mock_group.pipeline_id = uuid.uuid4()
            mock_group.name = "test"
            mock_group.description = None
            mock_group.variants = []
            mock_group.selection_strategy = "weighted"
            mock_group.run_count = 0
            mock_group.max_concurrent_runs = 5
            mock_group.degraded_evals = False
            from datetime import datetime

            mock_group.created_at = datetime.now(UTC)
            mock_group.updated_at = datetime.now(UTC)
            mock_create.return_value = mock_group

            body = MagicMock()
            body.pipeline_id = uuid.uuid4()
            body.name = "test"
            body.description = None
            body.variants = []
            body.selection_strategy = "weighted"
            body.max_concurrent_runs = 5
            body.degraded_evals = False
            body.model_dump.return_value = {}

            result = await create_group(body, mock_session, principal)

        assert result["name"] == "test"
        mock_create.assert_awaited_once()


@pytest.mark.asyncio
class TestGetGroup:
    async def test_returns_group_when_found(self) -> None:
        principal = make_mock_principal()
        mock_session = make_session_mock()
        group_id = uuid.uuid4()

        with patch(
            "modulo.api.routes.variants.get_variant_group",
            new_callable=AsyncMock,
        ) as mock_get:
            mock_group = MagicMock()
            mock_group.id = group_id
            mock_group.pipeline_id = uuid.uuid4()
            mock_group.name = "found"
            mock_group.description = None
            mock_group.variants = []
            mock_group.selection_strategy = "weighted"
            mock_group.run_count = 0
            mock_group.max_concurrent_runs = 5
            mock_group.degraded_evals = False
            from datetime import datetime

            mock_group.created_at = datetime.now(UTC)
            mock_group.updated_at = datetime.now(UTC)
            mock_get.return_value = mock_group

            result = await get_group(group_id, mock_session, principal)
        assert result["name"] == "found"

    async def test_raises_404_when_not_found(self) -> None:
        principal = make_mock_principal()
        mock_session = make_session_mock()

        with patch(
            "modulo.api.routes.variants.get_variant_group",
            new_callable=AsyncMock,
            return_value=None,
        ):
            with pytest.raises(HTTPException) as exc:
                await get_group(uuid.uuid4(), mock_session, principal)
            assert exc.value.status_code == 404


@pytest.mark.asyncio
class TestRunVariant:
    async def test_creates_run_and_returns_response(self) -> None:
        principal = make_mock_principal()
        mock_session = make_session_mock()
        group_id = uuid.uuid4()
        body = MagicMock()
        body.input_payload = {"key": "value"}

        with (
            patch(
                "modulo.api.routes.variants.get_variant_group",
                new_callable=AsyncMock,
            ) as mock_get,
            patch(
                "modulo.api.routes.variants.check_pipeline_run_quota",
                new_callable=AsyncMock,
            ) as mock_quota,
            patch(
                "modulo.api.routes.variants.run_variant_weighted",
                new_callable=AsyncMock,
            ) as mock_run,
        ):
            mock_group = MagicMock()
            mock_group.pipeline_id = uuid.uuid4()
            mock_get.return_value = mock_group
            mock_quota.return_value = True
            mock_run.return_value = {
                "run_id": uuid.uuid4(),
                "variant": {"name": "control"},
                "merged_payload": {"key": "value"},
            }

            result = await run_variant(group_id, body, mock_session, principal)
        assert "run_id" in result
        assert result["variant_name"] == "control"

    async def test_raises_429_when_quota_exceeded(self) -> None:
        principal = make_mock_principal()
        mock_session = make_session_mock()
        group_id = uuid.uuid4()
        body = MagicMock()
        body.input_payload = {}

        with (
            patch(
                "modulo.api.routes.variants.get_variant_group",
                new_callable=AsyncMock,
            ) as mock_get,
            patch(
                "modulo.api.routes.variants.check_pipeline_run_quota",
                new_callable=AsyncMock,
            ) as mock_quota,
        ):
            mock_group = MagicMock()
            mock_get.return_value = mock_group
            mock_quota.return_value = False

            with pytest.raises(HTTPException) as exc:
                await run_variant(group_id, body, mock_session, principal)
            assert exc.value.status_code == 429


@pytest.mark.asyncio
class TestRunVariantBatch:
    def _make_group(self) -> MagicMock:
        mock_group = MagicMock()
        mock_group.pipeline_id = uuid.uuid4()
        mock_group.variants = [
            {"name": "control", "snapshot_id": str(uuid.uuid4()), "weight": 1.0},
            {"name": "experiment", "snapshot_id": str(uuid.uuid4()), "weight": 1.0},
        ]
        return mock_group

    async def test_creates_one_run_per_variant_and_returns_response(self) -> None:
        principal = make_mock_principal()
        mock_session = make_session_mock()
        group_id = uuid.uuid4()
        body = MagicMock()
        body.input_payload = {"key": "value"}

        with (
            patch(
                "modulo.api.routes.variants.get_variant_group",
                new_callable=AsyncMock,
            ) as mock_get,
            patch(
                "modulo.api.routes.variants.run_variant_batch",
                new_callable=AsyncMock,
            ) as mock_run,
            patch(
                "modulo.api.routes.variants.validate_batch_ownership",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "modulo.api.routes.variants.has_pipeline_default_evals",
                new_callable=AsyncMock,
                return_value=True,
            ),
        ):
            mock_get.return_value = self._make_group()
            batch_id = uuid.uuid4()
            mock_run.return_value = [
                {
                    "run_id": uuid.uuid4(),
                    "batch_id": batch_id,
                    "variant": {"name": "control"},
                    "merged_payload": {"key": "value"},
                },
                {
                    "run_id": uuid.uuid4(),
                    "batch_id": batch_id,
                    "variant": {"name": "experiment"},
                    "merged_payload": {"key": "value"},
                },
            ]

            result = await run_batch(group_id, body, mock_session, principal)
        assert result["count"] == 2
        assert result["batch_id"] == batch_id
        assert result["has_evals"] is True
        assert [r["variant_name"] for r in result["runs"]] == ["control", "experiment"]

    async def test_raises_429_with_quota_code_when_batch_rejected(self) -> None:
        principal = make_mock_principal()
        mock_session = make_session_mock()
        group_id = uuid.uuid4()
        body = MagicMock()
        body.input_payload = {}

        with (
            patch(
                "modulo.api.routes.variants.get_variant_group",
                new_callable=AsyncMock,
            ) as mock_get,
            patch(
                "modulo.api.routes.variants.validate_batch_ownership",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "modulo.api.routes.variants.run_variant_batch",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            mock_get.return_value = self._make_group()

            with pytest.raises(HTTPException) as exc:
                await run_batch(group_id, body, mock_session, principal)
            assert exc.value.status_code == 429
            assert "variant_group_quota_exceeded" in exc.value.detail

    async def test_raises_404_when_group_not_found(self) -> None:
        principal = make_mock_principal()
        mock_session = make_session_mock()
        body = MagicMock()
        body.input_payload = {}

        with patch(
            "modulo.api.routes.variants.get_variant_group",
            new_callable=AsyncMock,
            return_value=None,
        ):
            with pytest.raises(HTTPException) as exc:
                await run_batch(uuid.uuid4(), body, mock_session, principal)
            assert exc.value.status_code == 404
            assert "not found" in exc.value.detail.lower()

    async def test_raises_429_when_variants_empty(self) -> None:
        principal = make_mock_principal()
        mock_session = make_session_mock()
        group_id = uuid.uuid4()
        body = MagicMock()
        body.input_payload = {}

        with patch(
            "modulo.api.routes.variants.get_variant_group",
            new_callable=AsyncMock,
        ) as mock_get:
            mock_group = MagicMock()
            mock_group.variants = []
            mock_get.return_value = mock_group

            with pytest.raises(HTTPException) as exc:
                await run_batch(group_id, body, mock_session, principal)
            assert exc.value.status_code == 429
            assert "no variants" in exc.value.detail.lower()

    async def test_raises_503_on_sqlalchemy_error(self) -> None:
        principal = make_mock_principal()
        mock_session = make_session_mock()
        body = MagicMock()
        body.input_payload = {}

        with patch(
            "modulo.api.routes.variants.get_variant_group",
            new_callable=AsyncMock,
            side_effect=SQLAlchemyError("mock", "mock", "mock"),
        ):
            with pytest.raises(HTTPException) as exc:
                await run_batch(uuid.uuid4(), body, mock_session, principal)
            assert exc.value.status_code == 503

    async def test_raises_500_on_unexpected_error(self) -> None:
        principal = make_mock_principal()
        mock_session = make_session_mock()
        body = MagicMock()
        body.input_payload = {}

        with patch(
            "modulo.api.routes.variants.get_variant_group",
            new_callable=AsyncMock,
            side_effect=ValueError("unexpected"),
        ):
            with pytest.raises(HTTPException) as exc:
                await run_batch(uuid.uuid4(), body, mock_session, principal)
            assert exc.value.status_code == 500


@pytest.mark.asyncio
class TestCoverageGaps:
    async def test_returns_gaps(self) -> None:
        principal = make_mock_principal()
        mock_session = make_session_mock()
        group_id = uuid.uuid4()

        with (
            patch(
                "modulo.api.routes.variants.get_variant_group",
                new_callable=AsyncMock,
            ) as mock_get,
            patch(
                "modulo.api.routes.variants.get_coverage_gaps",
                new_callable=AsyncMock,
            ) as mock_gaps,
        ):
            mock_group = MagicMock()
            mock_get.return_value = mock_group
            mock_gaps.return_value = [{"variant": {"name": "a"}, "missing_evals": ["eval-1"]}]

            result = await coverage_gaps(group_id, mock_session, principal)
        assert len(result) == 1
        assert result[0]["variant"]["name"] == "a"


@pytest.mark.asyncio
class TestPromptDiffs:
    async def test_returns_diffs(self) -> None:
        principal = make_mock_principal()
        mock_session = make_session_mock()
        group_id = uuid.uuid4()

        with (
            patch(
                "modulo.api.routes.variants.get_variant_group",
                new_callable=AsyncMock,
            ) as mock_get,
            patch(
                "modulo.api.routes.variants.get_prompt_diffs",
                new_callable=AsyncMock,
            ) as mock_diffs,
        ):
            mock_group = MagicMock()
            mock_get.return_value = mock_group
            mock_diffs.return_value = [
                {"base_variant": {"name": "control"}, "variant": {"name": "a"}, "agent_diffs": []}
            ]

            result = await prompt_diffs(group_id, mock_session, principal)
        assert len(result) == 1
        assert result[0]["variant"]["name"] == "a"


@pytest.mark.asyncio
class TestDeleteGroup:
    async def test_deletes_when_found(self) -> None:
        principal = make_mock_principal()
        mock_session = make_session_mock()
        group_id = uuid.uuid4()

        with patch(
            "modulo.api.routes.variants.soft_delete_variant_group",
            new_callable=AsyncMock,
            return_value=True,
        ):
            result = await delete_group(group_id, mock_session, principal)  # type: ignore[func-returns-value]
        assert result is None

    async def test_raises_404_when_not_found(self) -> None:
        principal = make_mock_principal()
        mock_session = make_session_mock()

        with patch(
            "modulo.api.routes.variants.soft_delete_variant_group",
            new_callable=AsyncMock,
            return_value=False,
        ):
            with pytest.raises(HTTPException) as exc:
                await delete_group(uuid.uuid4(), mock_session, principal)
            assert exc.value.status_code == 404


@pytest.mark.asyncio
class TestUpdateGroup:
    async def test_updates_when_found(self) -> None:
        principal = make_mock_principal()
        mock_session = make_session_mock()
        group_id = uuid.uuid4()

        with patch(
            "modulo.api.routes.variants.update_variant_group",
            new_callable=AsyncMock,
        ) as mock_update:
            mock_group = MagicMock()
            mock_group.id = group_id
            mock_group.pipeline_id = uuid.uuid4()
            mock_group.name = "updated"
            mock_group.description = None
            mock_group.variants = []
            mock_group.selection_strategy = "weighted"
            mock_group.run_count = 0
            mock_group.max_concurrent_runs = 5
            mock_group.degraded_evals = False
            from datetime import datetime

            mock_group.created_at = datetime.now(UTC)
            mock_group.updated_at = datetime.now(UTC)
            mock_update.return_value = mock_group

            body = MagicMock()
            body.pipeline_id = uuid.uuid4()
            body.name = "updated"
            body.description = None
            body.variants = []
            body.selection_strategy = "weighted"
            body.max_concurrent_runs = 5
            body.degraded_evals = False
            body.model_dump.return_value = {}

            result = await update_group(group_id, body, mock_session, principal)
        assert result["name"] == "updated"

    async def test_raises_404_when_not_found(self) -> None:
        principal = make_mock_principal()
        mock_session = make_session_mock()

        with patch(
            "modulo.api.routes.variants.update_variant_group",
            new_callable=AsyncMock,
            return_value=None,
        ):
            body = MagicMock()
            body.name = "test"
            body.description = None
            body.variants = []
            body.selection_strategy = "weighted"
            body.max_concurrent_runs = 5
            body.degraded_evals = False
            body.model_dump.return_value = {}
            body.pipeline_id = uuid.uuid4()

            with pytest.raises(HTTPException) as exc:
                await update_group(uuid.uuid4(), body, mock_session, principal)
            assert exc.value.status_code == 404


@pytest.mark.asyncio
class TestListGroups:
    async def test_returns_paginated_list(self) -> None:
        principal = make_mock_principal()
        mock_session = make_session_mock()

        with patch(
            "modulo.api.routes.variants.list_variant_groups",
            new_callable=AsyncMock,
        ) as mock_list:
            mock_group = MagicMock()
            mock_group.id = uuid.uuid4()
            mock_group.pipeline_id = uuid.uuid4()
            mock_group.name = "listed"
            mock_group.description = None
            mock_group.variants = []
            mock_group.selection_strategy = "weighted"
            mock_group.run_count = 0
            mock_group.max_concurrent_runs = 5
            mock_group.degraded_evals = False
            from datetime import datetime

            mock_group.created_at = datetime.now(UTC)
            mock_group.updated_at = datetime.now(UTC)
            mock_list.return_value = ([mock_group], 1)

            result = await list_groups(
                pipeline_id=None,
                page=1,
                page_size=20,
                session=mock_session,
                principal=principal,
            )
        assert len(result) == 1
        assert result[0]["name"] == "listed"


@pytest.mark.asyncio
class TestRunVariantGroupNotFound:
    async def test_raises_404_when_group_not_found(self) -> None:
        principal = make_mock_principal()
        mock_session = make_session_mock()
        group_id = uuid.uuid4()
        body = MagicMock()
        body.input_payload = {}

        with patch(
            "modulo.api.routes.variants.get_variant_group",
            new_callable=AsyncMock,
            return_value=None,
        ):
            with pytest.raises(HTTPException) as exc:
                await run_variant(group_id, body, mock_session, principal)
            assert exc.value.status_code == 404
            assert "not found" in exc.value.detail.lower()


@pytest.mark.asyncio
class TestRunVariantEmptyVariants:
    async def test_raises_429_when_variants_empty(self) -> None:
        principal = make_mock_principal()
        mock_session = make_session_mock()
        group_id = uuid.uuid4()
        body = MagicMock()
        body.input_payload = {}

        with patch(
            "modulo.api.routes.variants.get_variant_group",
            new_callable=AsyncMock,
        ) as mock_get:
            mock_group = MagicMock()
            mock_group.variants = []
            mock_get.return_value = mock_group

            with pytest.raises(HTTPException) as exc:
                await run_variant(group_id, body, mock_session, principal)
            assert exc.value.status_code == 429
            assert "no variants" in exc.value.detail.lower()


@pytest.mark.asyncio
class TestCoverageGapsGroupNotFound:
    async def test_raises_404_when_group_not_found(self) -> None:
        principal = make_mock_principal()
        mock_session = make_session_mock()
        group_id = uuid.uuid4()

        with patch(
            "modulo.api.routes.variants.get_variant_group",
            new_callable=AsyncMock,
            return_value=None,
        ):
            with pytest.raises(HTTPException) as exc:
                await coverage_gaps(group_id, mock_session, principal)
            assert exc.value.status_code == 404
            assert "not found" in exc.value.detail.lower()


@pytest.mark.asyncio
class TestPromptDiffsGroupNotFound:
    async def test_raises_404_when_group_not_found(self) -> None:
        principal = make_mock_principal()
        mock_session = make_session_mock()
        group_id = uuid.uuid4()

        with patch(
            "modulo.api.routes.variants.get_variant_group",
            new_callable=AsyncMock,
            return_value=None,
        ):
            with pytest.raises(HTTPException) as exc:
                await prompt_diffs(group_id, mock_session, principal)
            assert exc.value.status_code == 404
            assert "not found" in exc.value.detail.lower()


@pytest.mark.asyncio
class TestCreateGroupProgrammingError:
    async def test_raises_501_on_programming_error(self) -> None:
        principal = make_mock_principal()
        mock_session = make_session_mock()

        with patch(
            "modulo.api.routes.variants.create_variant_group",
            new_callable=AsyncMock,
            side_effect=ProgrammingError("mock", "mock", "mock"),
        ):
            body = MagicMock()
            body.pipeline_id = uuid.uuid4()
            body.name = "test"
            body.description = None
            body.variants = []
            body.selection_strategy = "weighted"
            body.max_concurrent_runs = 5
            body.degraded_evals = False
            body.model_dump.return_value = {}

            with pytest.raises(HTTPException) as exc:
                await create_group(body, mock_session, principal)
            assert exc.value.status_code == 501


@pytest.mark.asyncio
class TestGetGroupProgrammingError:
    async def test_raises_501_on_programming_error(self) -> None:
        principal = make_mock_principal()
        mock_session = make_session_mock()

        with patch(
            "modulo.api.routes.variants.get_variant_group",
            new_callable=AsyncMock,
            side_effect=ProgrammingError("mock", "mock", "mock"),
        ):
            with pytest.raises(HTTPException) as exc:
                await get_group(uuid.uuid4(), mock_session, principal)
            assert exc.value.status_code == 501


@pytest.mark.asyncio
class TestCreateGroupSQLAlchemyError:
    async def test_raises_503_on_sqlalchemy_error(self) -> None:
        principal = make_mock_principal()
        mock_session = make_session_mock()

        with patch(
            "modulo.api.routes.variants.create_variant_group",
            new_callable=AsyncMock,
            side_effect=SQLAlchemyError("mock", "mock", "mock"),
        ):
            body = MagicMock()
            body.pipeline_id = uuid.uuid4()
            body.name = "test"
            body.description = None
            body.variants = []
            body.selection_strategy = "weighted"
            body.max_concurrent_runs = 5
            body.degraded_evals = False
            body.model_dump.return_value = {}

            with pytest.raises(HTTPException) as exc:
                await create_group(body, mock_session, principal)
            assert exc.value.status_code == 503


@pytest.mark.asyncio
class TestCreateGroupIntegrityError:
    async def test_raises_409_on_integrity_error(self) -> None:
        principal = make_mock_principal()
        mock_session = make_session_mock()

        with patch(
            "modulo.api.routes.variants.create_variant_group",
            new_callable=AsyncMock,
            side_effect=IntegrityError("mock", "mock", "mock"),
        ):
            body = MagicMock()
            body.pipeline_id = uuid.uuid4()
            body.name = "test"
            body.description = None
            body.variants = []
            body.selection_strategy = "weighted"
            body.max_concurrent_runs = 5
            body.degraded_evals = False
            body.model_dump.return_value = {}

            with pytest.raises(HTTPException) as exc:
                await create_group(body, mock_session, principal)
            assert exc.value.status_code == 409


@pytest.mark.asyncio
class TestRunVariantSQLAlchemyError:
    async def test_raises_503_on_sqlalchemy_error(self) -> None:
        principal = make_mock_principal()
        mock_session = make_session_mock()
        group_id = uuid.uuid4()
        body = MagicMock()
        body.input_payload = {}

        with patch(
            "modulo.api.routes.variants.get_variant_group",
            new_callable=AsyncMock,
            side_effect=SQLAlchemyError("mock", "mock", "mock"),
        ):
            with pytest.raises(HTTPException) as exc:
                await run_variant(group_id, body, mock_session, principal)
            assert exc.value.status_code == 503


@pytest.mark.asyncio
class TestCoverageGapsSQLAlchemyError:
    async def test_raises_503_on_sqlalchemy_error(self) -> None:
        principal = make_mock_principal()
        mock_session = make_session_mock()
        group_id = uuid.uuid4()

        with patch(
            "modulo.api.routes.variants.get_variant_group",
            new_callable=AsyncMock,
            side_effect=SQLAlchemyError("mock", "mock", "mock"),
        ):
            with pytest.raises(HTTPException) as exc:
                await coverage_gaps(group_id, mock_session, principal)
            assert exc.value.status_code == 503


@pytest.mark.asyncio
class TestPromptDiffsSQLAlchemyError:
    async def test_raises_503_on_sqlalchemy_error(self) -> None:
        principal = make_mock_principal()
        mock_session = make_session_mock()
        group_id = uuid.uuid4()

        with patch(
            "modulo.api.routes.variants.get_variant_group",
            new_callable=AsyncMock,
            side_effect=SQLAlchemyError("mock", "mock", "mock"),
        ):
            with pytest.raises(HTTPException) as exc:
                await prompt_diffs(group_id, mock_session, principal)
            assert exc.value.status_code == 503


@pytest.mark.asyncio
class TestDeleteGroupSQLAlchemyError:
    async def test_raises_503_on_sqlalchemy_error(self) -> None:
        principal = make_mock_principal()
        mock_session = make_session_mock()
        group_id = uuid.uuid4()

        with patch(
            "modulo.api.routes.variants.soft_delete_variant_group",
            new_callable=AsyncMock,
            side_effect=SQLAlchemyError("mock", "mock", "mock"),
        ):
            with pytest.raises(HTTPException) as exc:
                await delete_group(group_id, mock_session, principal)
            assert exc.value.status_code == 503


class TestVariantToResponseEmptyRunCount:
    def test_handles_none_run_count(self) -> None:
        group = MagicMock()
        group.id = uuid.uuid4()
        group.pipeline_id = uuid.uuid4()
        group.name = "test"
        group.description = None
        group.variants = []
        group.selection_strategy = "weighted"
        group.run_count = None
        group.max_concurrent_runs = 5
        group.degraded_evals = False
        from datetime import datetime

        group.created_at = datetime.now(UTC)
        group.updated_at = datetime.now(UTC)

        result = _variant_to_response(group)
        assert result["run_count"] == 0

    def test_handles_zero_run_count(self) -> None:
        group = MagicMock()
        group.id = uuid.uuid4()
        group.pipeline_id = uuid.uuid4()
        group.name = "test"
        group.description = None
        group.variants = []
        group.selection_strategy = "weighted"
        group.run_count = 0
        group.max_concurrent_runs = 5
        group.degraded_evals = False
        from datetime import datetime

        group.created_at = datetime.now(UTC)
        group.updated_at = datetime.now(UTC)

        result = _variant_to_response(group)
        assert result["run_count"] == 0


@pytest.mark.asyncio
class TestCreateGroupException:
    async def test_raises_500_on_unexpected_error(self) -> None:
        principal = make_mock_principal()
        mock_session = make_session_mock()

        with patch(
            "modulo.api.routes.variants.create_variant_group",
            new_callable=AsyncMock,
            side_effect=ValueError("unexpected"),
        ):
            body = MagicMock()
            body.pipeline_id = uuid.uuid4()
            body.name = "test"
            body.description = None
            body.variants = []
            body.selection_strategy = "weighted"
            body.max_concurrent_runs = 5
            body.degraded_evals = False
            body.model_dump.return_value = {}

            with pytest.raises(HTTPException) as exc:
                await create_group(body, mock_session, principal)
            assert exc.value.status_code == 500


@pytest.mark.asyncio
class TestGetGroupException:
    async def test_raises_500_on_unexpected_error(self) -> None:
        principal = make_mock_principal()
        mock_session = make_session_mock()

        with patch(
            "modulo.api.routes.variants.get_variant_group",
            new_callable=AsyncMock,
            side_effect=KeyError("missing_key"),
        ):
            with pytest.raises(HTTPException) as exc:
                await get_group(uuid.uuid4(), mock_session, principal)
            assert exc.value.status_code == 500


@pytest.mark.asyncio
class TestListGroupsException:
    async def test_raises_500_on_unexpected_error(self) -> None:
        principal = make_mock_principal()
        mock_session = make_session_mock()

        with patch(
            "modulo.api.routes.variants.list_variant_groups",
            new_callable=AsyncMock,
            side_effect=TypeError("bad type"),
        ):
            with pytest.raises(HTTPException) as exc:
                await list_groups(
                    pipeline_id=None,
                    page=1,
                    page_size=20,
                    session=mock_session,
                    principal=principal,
                )
            assert exc.value.status_code == 500


@pytest.mark.asyncio
class TestUpdateGroupException:
    async def test_raises_500_on_unexpected_error(self) -> None:
        principal = make_mock_principal()
        mock_session = make_session_mock()

        with patch(
            "modulo.api.routes.variants.update_variant_group",
            new_callable=AsyncMock,
            side_effect=ValueError("unexpected"),
        ):
            body = MagicMock()
            body.name = "test"
            body.description = None
            body.variants = []
            body.selection_strategy = "weighted"
            body.max_concurrent_runs = 5
            body.degraded_evals = False
            body.model_dump.return_value = {}
            body.pipeline_id = uuid.uuid4()

            with pytest.raises(HTTPException) as exc:
                await update_group(uuid.uuid4(), body, mock_session, principal)
            assert exc.value.status_code == 500


@pytest.mark.asyncio
class TestDeleteGroupException:
    async def test_raises_500_on_unexpected_error(self) -> None:
        principal = make_mock_principal()
        mock_session = make_session_mock()

        with patch(
            "modulo.api.routes.variants.soft_delete_variant_group",
            new_callable=AsyncMock,
            side_effect=ValueError("unexpected"),
        ):
            with pytest.raises(HTTPException) as exc:
                await delete_group(uuid.uuid4(), mock_session, principal)
            assert exc.value.status_code == 500


@pytest.mark.asyncio
class TestRunVariantException:
    async def test_raises_500_on_unexpected_error(self) -> None:
        principal = make_mock_principal()
        mock_session = make_session_mock()

        with patch(
            "modulo.api.routes.variants.get_variant_group",
            new_callable=AsyncMock,
            side_effect=ValueError("unexpected"),
        ):
            body = MagicMock()
            body.input_payload = {}

            with pytest.raises(HTTPException) as exc:
                await run_variant(uuid.uuid4(), body, mock_session, principal)
            assert exc.value.status_code == 500


@pytest.mark.asyncio
class TestCoverageGapsException:
    async def test_raises_500_on_unexpected_error(self) -> None:
        principal = make_mock_principal()
        mock_session = make_session_mock()

        with patch(
            "modulo.api.routes.variants.get_variant_group",
            new_callable=AsyncMock,
            side_effect=ValueError("unexpected"),
        ):
            with pytest.raises(HTTPException) as exc:
                await coverage_gaps(uuid.uuid4(), mock_session, principal)
            assert exc.value.status_code == 500


@pytest.mark.asyncio
class TestPromptDiffsException:
    async def test_raises_500_on_unexpected_error(self) -> None:
        principal = make_mock_principal()
        mock_session = make_session_mock()

        with patch(
            "modulo.api.routes.variants.get_variant_group",
            new_callable=AsyncMock,
            side_effect=ValueError("unexpected"),
        ):
            with pytest.raises(HTTPException) as exc:
                await prompt_diffs(uuid.uuid4(), mock_session, principal)
            assert exc.value.status_code == 500


@pytest.mark.asyncio
class TestBatchCompare:
    def _entry(self, **overrides: object) -> dict:
        entry: dict = {
            "run_id": uuid.uuid4(),
            "run_number": 1,
            "status": "complete",
            "variant_id": "variant-a",
            "variant_name": "control",
            "snapshot_id": uuid.uuid4(),
            "run_context_overrides": {"model_backend_id": "backend-a"},
            "eval_pass_rate": 0.75,
            "eval_count": 4,
            "total_cost_usd": 0.5,
            "total_tokens": 100,
            "created_at": None,
            "completed_at": None,
            "override_diff": {"added": {}, "removed": {}, "changed": {}},
        }
        entry.update(overrides)
        return entry

    async def test_returns_exactly_batch_runs(self) -> None:
        principal = make_mock_principal()
        mock_session = make_session_mock()
        batch_id = uuid.uuid4()

        entries = [self._entry(), self._entry(variant_name="experiment")]
        mock_run = MagicMock()
        mock_run.pipeline_id = uuid.uuid4()

        with (
            patch(
                "modulo.api.routes.variants.get_batch_compare",
                new_callable=AsyncMock,
                return_value=entries,
            ) as mock_cmp,
            patch(
                "modulo.api.routes.variants.get_run",
                new_callable=AsyncMock,
                return_value=mock_run,
            ),
            patch(
                "modulo.api.routes.variants.has_pipeline_default_evals",
                new_callable=AsyncMock,
                return_value=True,
            ),
        ):
            result = await batch_compare(batch_id, mock_session, principal)

        assert result["batch_id"] == batch_id
        assert result["has_evals"] is True
        assert len(result["runs"]) == 2
        assert [r["variant_name"] for r in result["runs"]] == ["control", "experiment"]
        mock_cmp.assert_awaited_once()

    async def test_raises_404_when_batch_not_found(self) -> None:
        principal = make_mock_principal()
        mock_session = make_session_mock()

        with patch(
            "modulo.api.routes.variants.get_batch_compare",
            new_callable=AsyncMock,
            return_value=[],
        ):
            with pytest.raises(HTTPException) as exc:
                await batch_compare(uuid.uuid4(), mock_session, principal)
            assert exc.value.status_code == 404

    async def test_masks_sensitive_override_values(self) -> None:
        principal = make_mock_principal()
        mock_session = make_session_mock()
        batch_id = uuid.uuid4()

        entries = [
            self._entry(
                run_context_overrides={"api_key": "sk-abc", "model": "gpt-4"},
                override_diff={"added": {"api_key": "sk-abc"}, "removed": {}, "changed": {}},
            )
        ]
        mock_run = MagicMock()
        mock_run.pipeline_id = uuid.uuid4()

        with (
            patch(
                "modulo.api.routes.variants.get_batch_compare",
                new_callable=AsyncMock,
                return_value=entries,
            ),
            patch(
                "modulo.api.routes.variants.get_run",
                new_callable=AsyncMock,
                return_value=mock_run,
            ),
            patch(
                "modulo.api.routes.variants.has_pipeline_default_evals",
                new_callable=AsyncMock,
                return_value=True,
            ),
        ):
            result = await batch_compare(batch_id, mock_session, principal)

        masked = result["runs"][0]["run_context_overrides"]
        assert masked["api_key"] != "sk-abc"
        assert masked["model"] == "gpt-4"
        assert result["runs"][0]["override_diff"]["added"]["api_key"] != "sk-abc"

    async def test_returns_404_for_cross_org_batch(self) -> None:
        """Cross-org IDOR: another org's batch_id resolves to no org-owned runs."""
        principal = make_mock_principal()
        mock_session = make_session_mock()

        with patch(
            "modulo.api.routes.variants.get_batch_compare",
            new_callable=AsyncMock,
            return_value=[],
        ):
            with pytest.raises(HTTPException) as exc:
                await batch_compare(uuid.uuid4(), mock_session, principal)
            assert exc.value.status_code == 404


@pytest.mark.asyncio
class TestRunVariantBatchOwnership:
    async def test_rejects_cross_org_reference_with_403(self) -> None:
        principal = make_mock_principal()
        mock_session = make_session_mock()
        group_id = uuid.uuid4()
        body = MagicMock()
        body.input_payload = {}

        with (
            patch(
                "modulo.api.routes.variants.get_variant_group",
                new_callable=AsyncMock,
            ) as mock_get,
            patch(
                "modulo.api.routes.variants.validate_batch_ownership",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "modulo.api.routes.variants.run_variant_batch",
                new_callable=AsyncMock,
            ) as mock_run,
        ):
            mock_group = MagicMock()
            mock_group.pipeline_id = uuid.uuid4()
            mock_group.variants = [{"name": "v", "snapshot_id": str(uuid.uuid4())}]
            mock_get.return_value = mock_group

            with pytest.raises(HTTPException) as exc:
                await run_batch(group_id, body, mock_session, principal)
            assert exc.value.status_code == 403
            mock_run.assert_not_called()


@pytest.mark.asyncio
class TestRunVariantBatchEvalCoverage:
    async def test_reports_has_evals_false_when_pipeline_has_none(self) -> None:
        principal = make_mock_principal()
        mock_session = make_session_mock()
        group_id = uuid.uuid4()
        body = MagicMock()
        body.input_payload = {}

        batch_id = uuid.uuid4()
        with (
            patch(
                "modulo.api.routes.variants.get_variant_group",
                new_callable=AsyncMock,
            ) as mock_get,
            patch(
                "modulo.api.routes.variants.validate_batch_ownership",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "modulo.api.routes.variants.has_pipeline_default_evals",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "modulo.api.routes.variants.run_variant_batch",
                new_callable=AsyncMock,
            ) as mock_run,
        ):
            mock_group = MagicMock()
            mock_group.pipeline_id = uuid.uuid4()
            mock_group.variants = [{"name": "v", "snapshot_id": str(uuid.uuid4())}]
            mock_get.return_value = mock_group
            mock_run.return_value = [
                {"run_id": uuid.uuid4(), "batch_id": batch_id, "variant": {"name": "v"}, "merged_payload": {}}
            ]

            result = await run_batch(group_id, body, mock_session, principal)

        assert result["has_evals"] is False
        assert result["batch_id"] == batch_id
        assert result["count"] == 1


class TestVariantDefId:
    def test_id_optional_and_round_trips(self) -> None:
        vid = "variant-control"
        variant = VariantDef(id=vid, snapshot_id=uuid.uuid4(), name="control")
        dumped = variant.model_dump()
        assert dumped["id"] == vid

    def test_id_defaults_to_none(self) -> None:
        variant = VariantDef(snapshot_id=uuid.uuid4(), name="control")
        assert variant.id is None


class TestCreateVariantGroupRequestValidation:
    """API-layer validation for selection_strategy and variant weights.

    These model-level checks exist so an invalid ``selection_strategy`` or a
    negative variant weight surfaces as a 422 ValidationError at the API edge
    instead of a DB CHECK-constraint IntegrityError (409).
    """

    def _valid_variant(self, **overrides: object) -> VariantDef:
        kwargs: dict[str, object] = {
            "snapshot_id": uuid.uuid4(),
            "name": "control",
            "weight": 1.0,
            "run_context_overrides": {"model_backend_id": "backend-a"},
            "eval_definition_ids": [],
        }
        kwargs.update(overrides)
        return VariantDef(**kwargs)

    def test_invalid_selection_strategy_rejected(self) -> None:
        with pytest.raises(ValidationError):
            CreateVariantGroupRequest(
                pipeline_id=uuid.uuid4(),
                name="test",
                variants=[self._valid_variant()],
                selection_strategy="bogus",
            )

    def test_selection_strategy_weighted_accepted(self) -> None:
        req = CreateVariantGroupRequest(
            pipeline_id=uuid.uuid4(),
            name="test",
            variants=[self._valid_variant()],
            selection_strategy="weighted",
        )
        assert req.selection_strategy == "weighted"

    def test_selection_strategy_single_accepted(self) -> None:
        req = CreateVariantGroupRequest(
            pipeline_id=uuid.uuid4(),
            name="test",
            variants=[self._valid_variant()],
            selection_strategy="single",
        )
        assert req.selection_strategy == "single"

    def test_negative_weight_rejected(self) -> None:
        with pytest.raises(ValidationError):
            CreateVariantGroupRequest(
                pipeline_id=uuid.uuid4(),
                name="test",
                variants=[self._valid_variant(weight=-1.0)],
            )

    def test_zero_weight_accepted(self) -> None:
        req = CreateVariantGroupRequest(
            pipeline_id=uuid.uuid4(),
            name="test",
            variants=[self._valid_variant(weight=0.0)],
        )
        assert req.variants[0].weight == 0.0
