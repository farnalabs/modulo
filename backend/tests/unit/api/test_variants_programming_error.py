"""Unit tests for variant group ProgrammingError→501 handling.

These tests verify that ALL variant group routes properly convert
SQLAlchemy ProgrammingError into 501 Not Implemented responses.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import ProgrammingError

from modulo.api.routes.variants import (
    coverage_gaps,
    delete_group,
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
    p.account_id = kwargs.get("user_id", uuid.uuid4())
    p.username = kwargs.get("username", "test_user")
    p.org_role = kwargs.get("org_role", "admin")
    return p


@pytest.mark.asyncio
class TestListGroupsProgrammingError:
    async def test_raises_501_on_programming_error(self) -> None:
        principal = make_mock_principal()
        mock_session = make_session_mock()

        with patch(
            "modulo.api.routes.variants.list_variant_groups",
            new_callable=AsyncMock,
            side_effect=ProgrammingError("mock", "mock", "mock"),
        ):
            with pytest.raises(HTTPException) as exc:
                await list_groups(
                    pipeline_id=None,
                    page=1,
                    page_size=20,
                    session=mock_session,
                    principal=principal,
                )
            assert exc.value.status_code == 501


@pytest.mark.asyncio
class TestUpdateGroupProgrammingError:
    async def test_raises_501_on_programming_error(self) -> None:
        principal = make_mock_principal()
        mock_session = make_session_mock()

        with patch(
            "modulo.api.routes.variants.update_variant_group",
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
                await update_group(uuid.uuid4(), body, mock_session, principal)
            assert exc.value.status_code == 501


@pytest.mark.asyncio
class TestDeleteGroupProgrammingError:
    async def test_raises_501_on_programming_error(self) -> None:
        principal = make_mock_principal()
        mock_session = make_session_mock()

        with patch(
            "modulo.api.routes.variants.delete_variant_group",
            new_callable=AsyncMock,
            side_effect=ProgrammingError("mock", "mock", "mock"),
        ):
            with pytest.raises(HTTPException) as exc:
                await delete_group(uuid.uuid4(), mock_session, principal)
            assert exc.value.status_code == 501


@pytest.mark.asyncio
class TestRunVariantProgrammingError:
    async def test_raises_501_on_programming_error(self) -> None:
        principal = make_mock_principal()
        mock_session = make_session_mock()
        body = MagicMock()
        body.input_payload = {}

        with patch(
            "modulo.api.routes.variants.get_variant_group",
            new_callable=AsyncMock,
            side_effect=ProgrammingError("mock", "mock", "mock"),
        ):
            with pytest.raises(HTTPException) as exc:
                await run_variant(uuid.uuid4(), body, mock_session, principal)
            assert exc.value.status_code == 501


@pytest.mark.asyncio
class TestCoverageGapsProgrammingError:
    async def test_raises_501_on_programming_error(self) -> None:
        principal = make_mock_principal()
        mock_session = make_session_mock()

        with patch(
            "modulo.api.routes.variants.get_variant_group",
            new_callable=AsyncMock,
            side_effect=ProgrammingError("mock", "mock", "mock"),
        ):
            with pytest.raises(HTTPException) as exc:
                await coverage_gaps(uuid.uuid4(), mock_session, principal)
            assert exc.value.status_code == 501


@pytest.mark.asyncio
class TestPromptDiffsProgrammingError:
    async def test_raises_501_on_programming_error(self) -> None:
        principal = make_mock_principal()
        mock_session = make_session_mock()

        with patch(
            "modulo.api.routes.variants.get_variant_group",
            new_callable=AsyncMock,
            side_effect=ProgrammingError("mock", "mock", "mock"),
        ):
            with pytest.raises(HTTPException) as exc:
                await prompt_diffs(uuid.uuid4(), mock_session, principal)
            assert exc.value.status_code == 501
