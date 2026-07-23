from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture
def mock_session():
    session = MagicMock()
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=ctx)
    ctx.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=ctx)
    session.in_transaction = MagicMock(return_value=True)
    return session


def make_mock_execute(model_class):
    def _mock_execute(*, count: int, items: list | None = None):
        m = AsyncMock()
        scalar = MagicMock()
        scalar.scalar_one.return_value = count
        scalars = MagicMock()
        scalars.scalars.return_value = (
            items or [MagicMock(spec=model_class) for _ in range(count)]
        )
        m.side_effect = [scalar, scalars]
        return m
    return _mock_execute


class TierFilterTestBase:
    crud_func = None
    model_class = None
    org_id_required = False
    org_id = None

    default_count = 2
    none_count = 2
    in_dev_count = 1
    empty_count = 5
    preview_count = 3

    @pytest.fixture(autouse=True)
    def _setup(self, mock_session):
        self.session = mock_session
        self.mock_execute = make_mock_execute(self.model_class)

    @pytest.mark.asyncio
    async def test_default_excludes_in_dev(self):
        self.session.execute = self.mock_execute(count=self.default_count)
        result = await self.crud_func(self.session, **self._crud_kwargs())
        assert result.total == self.default_count
        assert len(result.items) == self.default_count

    @pytest.mark.asyncio
    async def test_excluded_tiers_none_same_as_default(self):
        self.session.execute = self.mock_execute(count=self.none_count)
        result = await self.crud_func(
            self.session, excluded_tiers=None, **self._crud_kwargs()
        )
        assert result.total == self.none_count

    @pytest.mark.asyncio
    async def test_excluded_tiers_explicit_in_dev(self):
        self.session.execute = self.mock_execute(count=self.in_dev_count)
        result = await self.crud_func(
            self.session, excluded_tiers=["in_dev"], **self._crud_kwargs()
        )
        assert result.total == self.in_dev_count

    @pytest.mark.asyncio
    async def test_excluded_tiers_empty_skips_filter(self):
        self.session.execute = self.mock_execute(count=self.empty_count)
        result = await self.crud_func(
            self.session, excluded_tiers=[], **self._crud_kwargs()
        )
        assert result.total == self.empty_count

    @pytest.mark.asyncio
    async def test_excluded_tiers_preview(self):
        self.session.execute = self.mock_execute(count=self.preview_count)
        result = await self.crud_func(
            self.session, excluded_tiers=["preview"], **self._crud_kwargs()
        )
        assert result.total == self.preview_count

    def _crud_kwargs(self):
        if self.org_id_required:
            return {"org_id": self.org_id}
        return {}
