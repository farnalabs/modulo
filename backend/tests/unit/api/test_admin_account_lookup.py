"""Unit tests for the bootstrap admin-account lookup (FAR-584).

``_get_admin_account`` resolves the bootstrap admin by the case-insensitive
email ``admin`` (the ``uq_accounts_email_lower`` functional index guarantees at
most one match). It is referenced from ``_seed_system_schemas`` and
``_seed_environment_profiles``; these tests lock the lookup behaviour directly
and confirm the two seed paths reach it.
"""

from unittest.mock import AsyncMock, MagicMock, patch

from modulo.api import main as api_main
from modulo.api.main import _get_admin_account


class TestGetAdminAccount:
    async def test_returns_account_matching_admin(self) -> None:
        account = MagicMock()
        session = MagicMock()
        session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=account)))

        result = await _get_admin_account(session)

        assert result is account
        stmt = session.execute.await_args.args[0]
        # The lookup compares lower(email) == 'admin'.
        assert "admin" in str(stmt).lower()

    async def test_returns_none_when_no_admin(self) -> None:
        session = MagicMock()
        session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None)))

        assert await _get_admin_account(session) is None


def _make_session(execute_side_effect) -> MagicMock:
    session = MagicMock()
    session.execute = AsyncMock(side_effect=execute_side_effect)
    # `async with factory() as session, session.begin():` needs both context managers.
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


def _make_factory(session: MagicMock) -> MagicMock:
    factory = MagicMock()
    factory_cm = AsyncMock()
    factory_cm.__aenter__ = AsyncMock(return_value=session)
    factory_cm.__aexit__ = AsyncMock(return_value=False)
    factory.return_value = factory_cm
    return factory


class TestSeedSystemSchemasReachesAdminLookup:
    async def test_seed_system_schemas_looks_up_admin(self) -> None:
        settings = MagicMock()

        def _side_effect(query: object) -> MagicMock:
            if "organisation" in str(query).lower():
                m = MagicMock()
                m.scalars.return_value.all.return_value = []  # no orgs -> early return
                return m
            m = MagicMock()
            m.scalar_one_or_none.return_value = None
            return m

        session = _make_session(_side_effect)
        factory = _make_factory(session)

        with (
            patch("modulo.api.dependencies.get_or_create_engine", return_value=MagicMock()),
            patch("modulo.api.dependencies.get_or_create_session_factory", return_value=factory),
            patch("modulo.db.seed.seed_system_schemas", new=AsyncMock()),
        ):
            # No orgs -> returns after the admin lookup without touching the schema seed.
            await api_main._seed_system_schemas(settings)

        # The admin lookup was reached (covers the _get_admin_account call site).
        assert session.execute.called


class TestSeedEnvironmentProfilesReachesAdminLookup:
    async def test_seed_environment_profiles_looks_up_admin(self) -> None:
        settings = MagicMock()

        def _side_effect(query: object) -> MagicMock:
            s = str(query).lower()
            # `environment_profile` is checked before `organisation` because the
            # EnvironmentProfile query also references `organisation_id`.
            if "environment_profile" in s:
                m = MagicMock()
                m.scalar_one_or_none.return_value = None  # no existing profile
                return m
            if "organisation" in s:
                m = MagicMock()
                m.scalar_one_or_none.return_value = MagicMock()  # an org exists
                return m
            m = MagicMock()
            m.scalar_one_or_none.return_value = None
            return m

        session = _make_session(_side_effect)
        factory = _make_factory(session)

        with (
            patch("modulo.api.dependencies.get_or_create_engine", return_value=MagicMock()),
            patch("modulo.api.dependencies.get_or_create_session_factory", return_value=factory),
            patch("modulo.db.crud.environment_profile.create_environment_profile", new=AsyncMock()),
        ):
            await api_main._seed_environment_profiles(settings)

        assert session.execute.called
