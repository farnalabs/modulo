"""Regression tests: plan-context resolution with ``autobegin=False`` sessions.

Every feature-gated API endpoint 500s when the DI session factory
(``dependencies.py``) creates sessions with ``autobegin=False`` and plan-context
resolution runs a bare ``session.execute`` without an active transaction —
SQLAlchemy raises ``InvalidRequestError: Autobegin is disabled on this
Session``. These tests prove the transaction-safe catalog load and the
``get_plan_context`` CommunityTier fallback.
"""

import uuid
from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from modulo.api.dependencies import get_plan_context
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.core.feature_flags import CommunityTier, FeatureFlagRegistry
from modulo.db.models.base import Base
from modulo.db.models.tier_catalog import FeatureFlagCatalog, TierCatalog
from modulo.settings import Settings

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")


def _make_settings(modulo_license_key: str = "") -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
        modulo_license_key=modulo_license_key,
    )


@dataclass
class _FakeSession:
    """Minimal ``AsyncSession`` double for testing transaction entry.

    Mirrors the ``autobegin=False`` contract: ``execute`` raises
    ``InvalidRequestError`` unless ``begin()`` has been entered.
    """

    in_tx: bool = False
    begin_entered: int = 0

    def in_transaction(self) -> bool:
        return self.in_tx

    def begin(self):
        return _BeginCM(self)

    async def execute(self, *args, **kwargs):
        if not self.in_tx:
            raise InvalidRequestError(
                "Autobegin is disabled on this Session; please call session.begin() to start a new transaction"
            )
        result = MagicMock()
        result.scalars.return_value.all.return_value = []
        return result


class _BeginCM:
    def __init__(self, session: _FakeSession) -> None:
        self._session = session

    async def __aenter__(self) -> _FakeSession:
        self._session.begin_entered += 1
        self._session.in_tx = True
        return self._session

    async def __aexit__(self, *exc: object) -> bool:
        self._session.in_tx = False
        return False


class TestFeatureFlagRegistryFromDbTransactionSafe:
    """The catalog load must begin a transaction when none is active."""

    @pytest.mark.asyncio
    async def test_from_db_begins_transaction_when_autobegin_disabled(self) -> None:
        session = _FakeSession()
        registry = FeatureFlagRegistry(current_tier="team")

        await registry.load_from_db(session)

        assert session.begin_entered == 1
        assert registry.current_tier == "team"

    @pytest.mark.asyncio
    async def test_from_db_does_not_begin_when_transaction_already_active(self) -> None:
        session = _FakeSession(in_tx=True)
        registry = FeatureFlagRegistry(current_tier="team")

        await registry.load_from_db(session)

        assert session.begin_entered == 0
        assert registry.current_tier == "team"

    @pytest.mark.asyncio
    async def test_load_catalog_populates_registry(self) -> None:
        db_tiers = [
            {"tier_id": "community", "rank": 0},
            {"tier_id": "team", "rank": 1},
        ]
        db_flags = [
            {"name": "db_flag", "description": "From DB", "tier_id": "team", "depends_on": None, "is_active": True},
        ]
        session = _FakeSession()
        registry = FeatureFlagRegistry(current_tier="team")

        with (
            patch("modulo.db.crud.tier_catalog.list_tiers", return_value=db_tiers),
            patch("modulo.db.crud.tier_catalog.list_feature_flags", return_value=db_flags),
        ):
            await registry.load_from_db(session)

        assert registry.get_flag("db_flag") is not None
        assert registry._tier_rank == {"community": 0, "team": 1}


class TestFeatureFlagRegistryRealSession:
    """End-to-end regression against a real ``autobegin=False`` session.

    This is the exact production configuration: DI sessions use
    ``async_sessionmaker(engine, expire_on_commit=False, autobegin=False)``.
    Before the fix, ``FeatureFlagRegistry.from_db`` raised ``InvalidRequestError``
    because ``list_tiers``/``list_feature_flags`` ran ``session.execute`` with no
    active transaction.
    """

    @pytest.mark.asyncio
    async def test_from_db_no_invalid_request_error(self, engine) -> None:
        factory = async_sessionmaker(engine, expire_on_commit=False, autobegin=False)
        async with factory() as session:
            registry = await FeatureFlagRegistry.from_db(session, current_tier="community")
        assert registry.current_tier == "community"


class TestGetPlanContextFallback:
    """``get_plan_context`` must degrade to CommunityTier on non-transactional sessions."""

    def _make_user(self) -> AuthenticatedPrincipal:
        return AuthenticatedPrincipal(
            username="admin",
            organisation_id=_ORG_ID,
            account_id=_USER_ID,
            org_role="admin",
        )

    def _make_session(self) -> AsyncMock:
        session = AsyncMock()
        begin_cm = AsyncMock()
        begin_cm.__aenter__ = AsyncMock(return_value=None)
        begin_cm.__aexit__ = AsyncMock(return_value=False)
        session.begin = MagicMock(return_value=begin_cm)
        session.in_transaction = MagicMock(return_value=False)
        return session

    @pytest.mark.asyncio
    async def test_type_error_returns_community_tier(self) -> None:
        session = self._make_session()
        with (
            patch("modulo.db.crud.organisation.get_organisation", return_value=None),
            patch("modulo.core.feature_flags.resolve_plan_context", side_effect=TypeError("no support")),
        ):
            ctx = await get_plan_context(
                current_user=self._make_user(),
                session=session,
                settings=_make_settings(),
            )
        assert isinstance(ctx, CommunityTier)

    @pytest.mark.asyncio
    async def test_attribute_error_returns_community_tier(self) -> None:
        session = self._make_session()
        with (
            patch("modulo.db.crud.organisation.get_organisation", return_value=None),
            patch("modulo.core.feature_flags.resolve_plan_context", side_effect=AttributeError("no attr")),
        ):
            ctx = await get_plan_context(
                current_user=self._make_user(),
                session=session,
                settings=_make_settings(),
            )
        assert isinstance(ctx, CommunityTier)

    @pytest.mark.asyncio
    async def test_unrelated_exception_is_not_swallowed(self) -> None:
        session = self._make_session()
        with (
            patch("modulo.db.crud.organisation.get_organisation", return_value=None),
            patch("modulo.core.feature_flags.resolve_plan_context", side_effect=ValueError("boom")),
            pytest.raises(ValueError, match="boom"),
        ):
            await get_plan_context(
                current_user=self._make_user(),
                session=session,
                settings=_make_settings(),
            )


@pytest.fixture
async def engine():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(
            Base.metadata.create_all,
            tables=[TierCatalog.__table__, FeatureFlagCatalog.__table__],
        )
    yield engine
    await engine.dispose()


@pytest.fixture
async def _seed_catalog(engine) -> None:
    async with AsyncSession(engine) as session:
        session.add(TierCatalog(tier_id="community", label="Community", rank=0))
        session.add(TierCatalog(tier_id="team", label="Team", rank=1))
        session.add(FeatureFlagCatalog(name="sso", description="SSO", tier_id="team", depends_on=None, is_active=True))
        await session.commit()


@pytest.mark.usefixtures("_seed_catalog")
@pytest.mark.asyncio
async def test_real_session_loads_catalog_rows(engine) -> None:
    """With real rows present, the load runs inside the auto-begun transaction."""
    factory = async_sessionmaker(engine, expire_on_commit=False, autobegin=False)
    async with factory() as session:
        registry = await FeatureFlagRegistry.from_db(session, current_tier="team")
    assert registry.current_tier == "team"
    assert registry.get_flag("sso") is not None


@pytest.mark.asyncio
async def test_list_tiers_and_list_feature_flags_awaited_via_fake_session() -> None:
    """The CRUD reads are awaited through the fake session's execute() contract."""
    session = _FakeSession()
    with (
        patch("modulo.db.crud.tier_catalog.list_tiers", return_value=[]),
        patch("modulo.db.crud.tier_catalog.list_feature_flags", return_value=[]),
    ):
        registry = await FeatureFlagRegistry.from_db(session, current_tier="community")
    assert registry.current_tier == "community"
    assert session.begin_entered == 1


@pytest.mark.asyncio
async def test_execute_without_begin_raises_invalid_request_error() -> None:
    """Sanity check: the fake session reproduces the prod failure mode."""
    session = _FakeSession()
    with pytest.raises(InvalidRequestError, match="Autobegin is disabled"):
        await session.execute(select(TierCatalog))
