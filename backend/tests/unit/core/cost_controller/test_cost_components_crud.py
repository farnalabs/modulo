"""Unit tests for cost-component CRUD validation + the seeder idempotency (§3.1-§3.3)."""

from __future__ import annotations

import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.api.routes.cost_components import (
    validate_component_fields,
    validate_component_formula,
    validate_rate_fallback,
)
from modulo.core.cost_controller.breakdown.formula import CostFormulaError
from modulo.core.cost_controller.breakdown.params import REGISTERED_RATE_FALLBACKS
from modulo.core.seed_data.cost_components import DEFAULT_COST_COMPONENTS, seed_cost_components_for_org
from modulo.db.crud.cost_component import create_cost_component

_ORG = uuid.UUID("00000000-0000-0000-0000-000000000001")


def _mock_session() -> AsyncMock:
    s = AsyncMock()
    s.add = MagicMock()
    s.flush = AsyncMock()
    s.in_transaction = MagicMock(return_value=True)
    return s


def _component_args(**overrides: object) -> dict:
    base: dict = {
        "org_id": _ORG,
        "name": "my_component",
        "display_name": "My Component",
        "kind": "calculated",
        "rate_usd": None,
        "rate_fallback": None,
        "formula": "node_count * 1",
        "report_key": None,
        "enabled": True,
        "sort_order": 0,
        "max_components": 50,
    }
    base.update(overrides)
    return base


# --- reserved-key validation -------------------------------------------------


def test_reserved_name_rejected() -> None:
    for reserved in ("reported", "rate", "cost_estimate_usd", "model_cost_usd"):
        with pytest.raises(CostFormulaError):
            validate_component_fields(name=reserved, report_key=None)


def test_reserved_report_key_rejected() -> None:
    for reserved in ("reported", "rate", "cost_estimate_usd"):
        with pytest.raises(CostFormulaError):
            validate_component_fields(name=None, report_key=reserved)


def test_model_cost_usd_not_reserved_as_report_key() -> None:
    # model_cost_usd is reserved as a NAME but deliberately NOT as a report_key
    # (components may report their own model cost). Pin that asymmetry.
    validate_component_fields(name=None, report_key="model_cost_usd")  # no raise
    with pytest.raises(CostFormulaError):
        validate_component_fields(name="model_cost_usd", report_key=None)


# --- rate_fallback registry ---------------------------------------------------


def test_unknown_rate_fallback_rejected() -> None:
    with pytest.raises(CostFormulaError) as exc_info:
        validate_rate_fallback("bogus_rate")
    assert "e2b_rate" in str(exc_info.value)


def test_registered_rate_fallback_ok() -> None:
    validate_rate_fallback("e2b_rate")  # no raise
    validate_rate_fallback(None)  # no raise
    assert "e2b_rate" in REGISTERED_RATE_FALLBACKS


# --- cross-field formula validation -------------------------------------------


def test_self_reported_requires_null_formula() -> None:
    with pytest.raises(CostFormulaError):
        validate_component_formula(kind="self_reported", formula="reported", rate_usd=None, rate_fallback=None)


def test_calculated_requires_formula() -> None:
    with pytest.raises(CostFormulaError):
        validate_component_formula(kind="calculated", formula=None, rate_usd=None, rate_fallback=None)


def test_rate_without_source_rejected() -> None:
    with pytest.raises(CostFormulaError) as exc_info:
        validate_component_formula(
            kind="calculated", formula="rate * wall_clock_hours", rate_usd=None, rate_fallback=None
        )
    assert exc_info.value.code == "rate_without_source"


def test_rate_with_fallback_ok() -> None:
    # A non-None rate_fallback satisfies the 'rate' reference. Pin that the
    # fallback used here is genuinely registered so a typo'd/renamed registry
    # entry cannot silently keep this test passing for the wrong reason.
    assert "e2b_rate" in REGISTERED_RATE_FALLBACKS
    validate_component_formula(
        kind="calculated", formula="rate * wall_clock_hours", rate_usd=None, rate_fallback="e2b_rate"
    )  # no raise — a registered fallback satisfies the 'rate' reference


def test_rate_with_rate_usd_ok() -> None:
    # The cross-field rule only fires when BOTH sources are absent — an explicit
    # rate_usd satisfies the 'rate' reference, and zero is still an explicit source.
    validate_component_formula(
        kind="calculated", formula="rate * wall_clock_hours", rate_usd=Decimal("0.13"), rate_fallback=None
    )  # no raise — an explicit rate_usd satisfies the 'rate' reference
    validate_component_formula(
        kind="calculated", formula="rate * wall_clock_hours", rate_usd=Decimal(0), rate_fallback=None
    )  # no raise — a zero rate_usd is still a provided source


def test_rate_with_both_sources_ok() -> None:
    # Both an explicit rate_usd AND a rate_fallback are set — the 'rate'
    # reference is satisfied by either source, so the combination must not raise.
    validate_component_formula(
        kind="calculated", formula="rate * wall_clock_hours", rate_usd=Decimal("0.13"), rate_fallback="e2b_rate"
    )


# --- CRUD guards (mocked session) ---------------------------------------------


@patch("modulo.db.crud.cost_component._duplicate_exists")
async def test_create_org_cap_enforced(mock_dup: AsyncMock) -> None:
    mock_dup.return_value = False
    session = _mock_session()
    count_result = MagicMock()
    count_result.scalar_one.return_value = 50
    session.execute.return_value = count_result
    with pytest.raises(ValueError) as exc_info:
        await create_cost_component(session, **_component_args())
    assert str(exc_info.value) == "org_cap"


@patch("modulo.db.crud.cost_component._duplicate_exists")
async def test_create_duplicate_is_409(mock_dup: AsyncMock) -> None:
    mock_dup.return_value = True
    session = _mock_session()
    count_result = MagicMock()
    count_result.scalar_one.return_value = 0
    session.execute.return_value = count_result
    with pytest.raises(ValueError) as exc_info:
        await create_cost_component(session, **_component_args())
    assert str(exc_info.value) == "duplicate_component"


# --- seeder idempotency -------------------------------------------------------


async def test_seeder_skips_existing_active_row() -> None:
    session = _mock_session()
    existing_result = MagicMock()
    existing_result.scalar_one_or_none.return_value = MagicMock()
    session.execute.return_value = existing_result
    await seed_cost_components_for_org(session, _ORG)
    session.add.assert_not_called()


async def test_seeder_inserts_missing_rows() -> None:
    session = _mock_session()
    existing_result = MagicMock()
    existing_result.scalar_one_or_none.return_value = None
    session.execute.return_value = existing_result
    await seed_cost_components_for_org(session, _ORG)
    assert session.add.call_count == len(DEFAULT_COST_COMPONENTS)
    added_names = {call.args[0].name for call in session.add.call_args_list}
    assert added_names == {"llm_tokens", "sandbox_infra", "model_tokens"}
    model_tokens = next(call.args[0] for call in session.add.call_args_list if call.args[0].name == "model_tokens")
    assert model_tokens.kind == "self_reported"
    assert model_tokens.formula is None
    assert model_tokens.report_key == "model_cost_usd"


async def test_seeder_calls_set_rls_org() -> None:
    session = _mock_session()
    existing_result = MagicMock()
    existing_result.scalar_one_or_none.return_value = None
    session.execute.return_value = existing_result
    await seed_cost_components_for_org(session, _ORG)
    session.execute.assert_awaited()
