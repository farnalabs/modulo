"""Unit tests for variant group API — pure function tests (no DB, no auth)."""

import uuid
from datetime import UTC
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from modulo.api.routes.variants import (
    _variant_to_response,
    coverage_gaps,
    create_group,
    delete_group,
    get_group,
    list_groups,
    prompt_diffs,
    run_variant,
    update_group,
)


def make_session_mock() -> AsyncMock:
    """Create an AsyncSession mock that supports async with session.begin()."""
    session = AsyncMock()
    session.in_transaction.return_value = True
    session.execute = AsyncMock()
    begin_ctx = AsyncMock()
    begin_ctx.__aenter__ = AsyncMock(return_value=session)
    begin_ctx.__aexit__ = AsyncMock(return_value=None)
    session.begin = MagicMock(return_value=begin_ctx)
    return session


def make_mock_principal(**kwargs: object) -> MagicMock:
    p = MagicMock()
    p.organisation_id = kwargs.get("org_id", uuid.uuid4())
    p.user_id = kwargs.get("user_id", uuid.uuid4())
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
            "modulo.api.routes.variants.delete_variant_group",
            new_callable=AsyncMock,
            return_value=True,
        ):
            result = await delete_group(group_id, mock_session, principal)  # type: ignore[func-returns-value]
        assert result is None

    async def test_raises_404_when_not_found(self) -> None:
        principal = make_mock_principal()
        mock_session = make_session_mock()

        with patch(
            "modulo.api.routes.variants.delete_variant_group",
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
