"""Unit tests for CostComponent CRUD (mocked session)."""

import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.crud.cost_component import (
    _duplicate_exists,
    _is_last_enabled_calculated,
    count_active_components,
    create_cost_component,
    get_cost_component,
    list_cost_components,
    soft_delete_cost_component,
    update_cost_component,
)
from modulo.db.models.cost_component import CostComponentKind

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_COMPONENT_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")


@pytest.fixture
def mock_session() -> AsyncMock:
    return AsyncMock(spec=AsyncSession)


def _exec_scalar(value: object) -> MagicMock:
    result = MagicMock()
    result.scalar_one = MagicMock(return_value=value)
    return result


def _exec_scalar_one_or_none(value: object) -> MagicMock:
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=value)
    return result


def _exec_scalars(items: list[object]) -> MagicMock:
    result = MagicMock()
    scalars = MagicMock()
    scalars.all = MagicMock(return_value=items)
    result.scalars = MagicMock(return_value=scalars)
    return result


def _mock_component(**overrides: object) -> MagicMock:
    comp = MagicMock()
    comp.id = overrides.get("id", _COMPONENT_ID)
    comp.name = overrides.get("name", "comp-a")
    comp.display_name = overrides.get("display_name", "Component A")
    comp.kind = overrides.get("kind", CostComponentKind.SELF_REPORTED.value)
    comp.rate_usd = overrides.get("rate_usd", Decimal("0.01"))
    comp.rate_fallback = overrides.get("rate_fallback")
    comp.formula = overrides.get("formula")
    comp.report_key = overrides.get("report_key", "key_a")
    comp.enabled = overrides.get("enabled", True)
    comp.sort_order = overrides.get("sort_order", 0)
    comp.organisation_id = overrides.get("org_id", _ORG_ID)
    comp.deleted_at = overrides.get("deleted_at")
    return comp


# ── count_active_components ────────────────────────────────────────


class TestCountActiveComponents:
    async def test_returns_count(self, mock_session: AsyncMock) -> None:
        mock_session.execute = AsyncMock(return_value=_exec_scalar(5))

        count = await count_active_components(mock_session, _ORG_ID)

        assert count == 5

    async def test_returns_zero_when_none(self, mock_session: AsyncMock) -> None:
        mock_session.execute = AsyncMock(return_value=_exec_scalar(0))

        count = await count_active_components(mock_session, _ORG_ID)

        assert count == 0


# ── _duplicate_exists ──────────────────────────────────────────────


class TestDuplicateExists:
    async def test_returns_true_when_duplicate(self, mock_session: AsyncMock) -> None:
        mock_session.execute = AsyncMock(return_value=_exec_scalar_one_or_none(uuid.uuid4()))

        result = await _duplicate_exists(mock_session, _ORG_ID, name="x", report_key="k", _kind="self_reported")

        assert result is True

    async def test_returns_false_when_no_duplicate(self, mock_session: AsyncMock) -> None:
        mock_session.execute = AsyncMock(return_value=_exec_scalar_one_or_none(None))

        result = await _duplicate_exists(mock_session, _ORG_ID, name="x", report_key=None, _kind="self_reported")

        assert result is False


# ── create_cost_component ──────────────────────────────────────────


class TestCreateCostComponent:
    async def test_creates_component(self, mock_session: AsyncMock) -> None:
        # count_active_components → 0, _duplicate_exists → False
        mock_session.execute = AsyncMock(side_effect=[_exec_scalar(0), _exec_scalar_one_or_none(None)])

        await create_cost_component(
            mock_session,
            org_id=_ORG_ID,
            name="test",
            display_name="Test",
            kind="self_reported",
            rate_usd=Decimal("0.05"),
            rate_fallback=None,
            formula=None,
            report_key="rk",
            enabled=True,
            sort_order=1,
        )

        mock_session.add.assert_called_once()
        mock_session.flush.assert_awaited_once()

    async def test_raises_when_org_cap_exceeded(self, mock_session: AsyncMock) -> None:
        mock_session.execute = AsyncMock(return_value=_exec_scalar(50))

        with pytest.raises(ValueError, match="org_cap"):
            await create_cost_component(
                mock_session,
                org_id=_ORG_ID,
                name="test",
                display_name="Test",
                kind="self_reported",
                rate_usd=Decimal("0.01"),
                rate_fallback=None,
                formula=None,
                report_key=None,
                enabled=True,
                sort_order=0,
                max_components=50,
            )

    async def test_raises_when_duplicate_exists(self, mock_session: AsyncMock) -> None:
        mock_session.execute = AsyncMock(side_effect=[_exec_scalar(0), _exec_scalar_one_or_none(uuid.uuid4())])

        with pytest.raises(ValueError, match="duplicate_component"):
            await create_cost_component(
                mock_session,
                org_id=_ORG_ID,
                name="dup",
                display_name="Dup",
                kind="self_reported",
                rate_usd=Decimal("0.01"),
                rate_fallback=None,
                formula=None,
                report_key="rk",
                enabled=True,
                sort_order=0,
            )

    async def test_custom_max_components(self, mock_session: AsyncMock) -> None:
        mock_session.execute = AsyncMock(return_value=_exec_scalar(2))

        with pytest.raises(ValueError, match="org_cap"):
            await create_cost_component(
                mock_session,
                org_id=_ORG_ID,
                name="test",
                display_name="Test",
                kind="self_reported",
                rate_usd=Decimal("0.01"),
                rate_fallback=None,
                formula=None,
                report_key=None,
                enabled=True,
                sort_order=0,
                max_components=2,
            )


# ── list_cost_components ──────────────────────────────────────────


class TestListCostComponents:
    async def test_returns_list(self, mock_session: AsyncMock) -> None:
        comp = _mock_component()
        mock_session.execute = AsyncMock(return_value=_exec_scalars([comp]))

        result = await list_cost_components(mock_session)

        assert len(result) == 1
        assert result[0] is comp

    async def test_returns_empty(self, mock_session: AsyncMock) -> None:
        mock_session.execute = AsyncMock(return_value=_exec_scalars([]))

        result = await list_cost_components(mock_session)

        assert result == []


# ── get_cost_component ────────────────────────────────────────────


class TestGetCostComponent:
    async def test_returns_component(self, mock_session: AsyncMock) -> None:
        comp = _mock_component()
        mock_session.execute = AsyncMock(return_value=_exec_scalar_one_or_none(comp))

        result = await get_cost_component(mock_session, _COMPONENT_ID)

        assert result is comp

    async def test_returns_none_when_not_found(self, mock_session: AsyncMock) -> None:
        mock_session.execute = AsyncMock(return_value=_exec_scalar_one_or_none(None))

        result = await get_cost_component(mock_session, uuid.uuid4())

        assert result is None


# ── _is_last_enabled_calculated ───────────────────────────────────


class TestIsLastEnabledCalculated:
    async def test_returns_true_when_no_others(self, mock_session: AsyncMock) -> None:
        mock_session.execute = AsyncMock(return_value=_exec_scalar(0))

        result = await _is_last_enabled_calculated(mock_session, _COMPONENT_ID)

        assert result is True

    async def test_returns_false_when_others_exist(self, mock_session: AsyncMock) -> None:
        mock_session.execute = AsyncMock(return_value=_exec_scalar(2))

        result = await _is_last_enabled_calculated(mock_session, _COMPONENT_ID)

        assert result is False


# ── update_cost_component ─────────────────────────────────────────


class TestUpdateCostComponent:
    async def test_returns_none_when_not_found(self, mock_session: AsyncMock) -> None:
        mock_session.execute = AsyncMock(return_value=_exec_scalar_one_or_none(None))

        result = await update_cost_component(mock_session, component_id=uuid.uuid4(), updates={})

        assert result is None

    async def test_updates_fields(self, mock_session: AsyncMock) -> None:
        comp = _mock_component(name="comp-a", display_name="Old")
        # get_cost_component: returns comp
        # duplicate check skipped (name is being changed, but new_name=comp-a matches)
        # Wait — we're updating name to "new", so duplicate check runs.
        # get_cost_component → comp, _duplicate_exists → None (no dup)
        mock_session.execute = AsyncMock(side_effect=[_exec_scalar_one_or_none(comp), _exec_scalar_one_or_none(None)])

        result = await update_cost_component(
            mock_session,
            component_id=_COMPONENT_ID,
            updates={"name": "new", "display_name": "New"},
        )

        assert result is comp
        assert comp.name == "new"
        assert comp.display_name == "New"
        mock_session.flush.assert_awaited_once()

    async def test_skips_immutable_fields(self, mock_session: AsyncMock) -> None:
        comp = _mock_component()
        original_id = comp.id
        original_org = comp.organisation_id
        # get_cost_component → comp, no name change → no dup check
        mock_session.execute = AsyncMock(return_value=_exec_scalar_one_or_none(comp))

        await update_cost_component(
            mock_session,
            component_id=_COMPONENT_ID,
            updates={"id": uuid.uuid4(), "organisation_id": uuid.uuid4()},
        )

        assert comp.id == original_id
        assert comp.organisation_id == original_org

    async def test_raises_last_calculated_kind_change(self, mock_session: AsyncMock) -> None:
        comp = _mock_component(kind=CostComponentKind.CALCULATED.value, enabled=True)
        # get_cost_component → comp, _is_last_enabled_calculated → True (0 others)
        mock_session.execute = AsyncMock(side_effect=[_exec_scalar_one_or_none(comp), _exec_scalar(0)])

        with pytest.raises(ValueError, match="last_calculated_kind_change"):
            await update_cost_component(
                mock_session,
                component_id=_COMPONENT_ID,
                updates={"kind": CostComponentKind.SELF_REPORTED.value},
            )

    async def test_raises_last_calculated_disable(self, mock_session: AsyncMock) -> None:
        comp = _mock_component(kind=CostComponentKind.CALCULATED.value, enabled=True)
        # get_cost_component → comp, _is_last_enabled_calculated → True
        mock_session.execute = AsyncMock(side_effect=[_exec_scalar_one_or_none(comp), _exec_scalar(0)])

        with pytest.raises(ValueError, match="last_calculated_disable"):
            await update_cost_component(
                mock_session,
                component_id=_COMPONENT_ID,
                updates={"enabled": False},
            )

    async def test_raises_duplicate_on_name_change(self, mock_session: AsyncMock) -> None:
        comp = _mock_component(name="old", kind=CostComponentKind.SELF_REPORTED.value, enabled=True)
        # get_cost_component → comp, _duplicate_exists → True
        mock_session.execute = AsyncMock(
            side_effect=[_exec_scalar_one_or_none(comp), _exec_scalar_one_or_none(uuid.uuid4())]
        )

        with pytest.raises(ValueError, match="duplicate_component"):
            await update_cost_component(
                mock_session,
                component_id=_COMPONENT_ID,
                updates={"name": "dup"},
            )

    async def test_no_kind_change_skips_kind_guard(self, mock_session: AsyncMock) -> None:
        comp = _mock_component(kind=CostComponentKind.CALCULATED.value, enabled=True)
        # get_cost_component → comp, _duplicate_exists → no dup (name changes from "comp-a" to "renamed")
        mock_session.execute = AsyncMock(side_effect=[_exec_scalar_one_or_none(comp), _exec_scalar_one_or_none(None)])

        result = await update_cost_component(
            mock_session,
            component_id=_COMPONENT_ID,
            updates={"name": "renamed"},
        )

        assert result is comp
        assert comp.name == "renamed"

    async def test_non_last_calculated_can_change_kind(self, mock_session: AsyncMock) -> None:
        comp = _mock_component(kind=CostComponentKind.CALCULATED.value, enabled=True)
        # get_cost_component → comp, _is_last_enabled_calculated → False (1 other)
        mock_session.execute = AsyncMock(side_effect=[_exec_scalar_one_or_none(comp), _exec_scalar(1)])

        result = await update_cost_component(
            mock_session,
            component_id=_COMPONENT_ID,
            updates={"kind": CostComponentKind.SELF_REPORTED.value},
        )

        assert result is comp
        assert comp.kind == CostComponentKind.SELF_REPORTED.value

    async def test_disable_of_non_last_calculated_allowed(self, mock_session: AsyncMock) -> None:
        comp = _mock_component(kind=CostComponentKind.CALCULATED.value, enabled=True)
        # get_cost_component → comp, _is_last_enabled_calculated → False
        mock_session.execute = AsyncMock(side_effect=[_exec_scalar_one_or_none(comp), _exec_scalar(1)])

        result = await update_cost_component(
            mock_session,
            component_id=_COMPONENT_ID,
            updates={"enabled": False},
        )

        assert result is comp
        assert comp.enabled is False


# ── soft_delete_cost_component ─────────────────────────────────────


class TestSoftDeleteCostComponent:
    async def test_returns_none_when_not_found(self, mock_session: AsyncMock) -> None:
        mock_session.execute = AsyncMock(return_value=_exec_scalar_one_or_none(None))

        result = await soft_delete_cost_component(mock_session, component_id=uuid.uuid4())

        assert result is None

    async def test_soft_deletes_component(self, mock_session: AsyncMock) -> None:
        comp = _mock_component(kind=CostComponentKind.SELF_REPORTED.value, enabled=True)
        mock_session.execute = AsyncMock(return_value=_exec_scalar_one_or_none(comp))

        result = await soft_delete_cost_component(mock_session, component_id=_COMPONENT_ID)

        assert result is comp
        mock_session.flush.assert_awaited_once()

    async def test_raises_when_last_calculated_delete(self, mock_session: AsyncMock) -> None:
        comp = _mock_component(kind=CostComponentKind.CALCULATED.value, enabled=True)
        # _is_last_enabled_calculated → True
        mock_session.execute = AsyncMock(side_effect=[_exec_scalar_one_or_none(comp), _exec_scalar(0)])

        with pytest.raises(ValueError, match="last_calculated_delete"):
            await soft_delete_cost_component(mock_session, component_id=_COMPONENT_ID)

    async def test_delete_non_last_calculated_allowed(self, mock_session: AsyncMock) -> None:
        comp = _mock_component(kind=CostComponentKind.CALCULATED.value, enabled=True)
        # _is_last_enabled_calculated → False
        mock_session.execute = AsyncMock(side_effect=[_exec_scalar_one_or_none(comp), _exec_scalar(1)])

        result = await soft_delete_cost_component(mock_session, component_id=_COMPONENT_ID)

        assert result is comp

    async def test_delete_disabled_calculated_allowed(self, mock_session: AsyncMock) -> None:
        comp = _mock_component(kind=CostComponentKind.CALCULATED.value, enabled=False)
        mock_session.execute = AsyncMock(return_value=_exec_scalar_one_or_none(comp))

        result = await soft_delete_cost_component(mock_session, component_id=_COMPONENT_ID)

        assert result is comp
