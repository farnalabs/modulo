"""Tests for _ensure_default_org team-plan promotion."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.api.main import _ensure_default_org
from modulo.db.models.organisation import Organisation
from modulo.settings import Settings


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key="a" * 32,
        fernet_key="a" * 32,
        modulo_admin_password="testpass",
    )


def _make_session(org: Organisation | None) -> AsyncMock:
    session = AsyncMock()
    session.add = MagicMock()
    session.begin = MagicMock()
    session.begin.return_value.__aenter__ = AsyncMock(return_value=session)
    session.begin.return_value.__aexit__ = AsyncMock(return_value=None)

    result = MagicMock()
    result.scalar_one_or_none.return_value = org
    session.execute.return_value = result
    return session


def _make_factory(session: AsyncMock) -> MagicMock:
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=None)
    return factory


class TestEnsureDefaultOrgTeamPlan:
    @pytest.mark.asyncio
    async def test_existing_org_with_null_plan_is_promoted_to_team(self) -> None:
        settings = _make_settings()
        org = Organisation(name="Default Organisation", slug="default")
        session = _make_session(org)
        factory = _make_factory(session)

        with (
            patch("modulo.api.dependencies.get_or_create_engine", return_value=MagicMock()),
            patch("modulo.api.dependencies.get_or_create_session_factory", return_value=factory),
        ):
            await _ensure_default_org(settings)

        assert org.plan_id == "team"
        session.add.assert_not_called()

    @pytest.mark.asyncio
    async def test_existing_org_already_on_team_is_left_unchanged(self) -> None:
        settings = _make_settings()
        org = Organisation(name="Default Organisation", slug="default", plan_id="team")
        session = _make_session(org)
        factory = _make_factory(session)

        with (
            patch("modulo.api.dependencies.get_or_create_engine", return_value=MagicMock()),
            patch("modulo.api.dependencies.get_or_create_session_factory", return_value=factory),
        ):
            await _ensure_default_org(settings)

        assert org.plan_id == "team"
        session.add.assert_not_called()

    @pytest.mark.asyncio
    async def test_new_org_is_created_on_team_plan(self) -> None:
        settings = _make_settings()
        session = _make_session(None)
        factory = _make_factory(session)

        with (
            patch("modulo.api.dependencies.get_or_create_engine", return_value=MagicMock()),
            patch("modulo.api.dependencies.get_or_create_session_factory", return_value=factory),
        ):
            await _ensure_default_org(settings)

        assert session.add.call_count == 1
        created = session.add.call_args[0][0]
        assert isinstance(created, Organisation)
        assert created.plan_id == "team"
        session.flush.assert_awaited_once()
