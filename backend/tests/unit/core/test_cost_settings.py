"""Unit tests locking the shared org cost-control settings contract.

``modulo.core.cost_settings`` holds the ``cost_controls`` settings key and
defaults shared by the admin cost-controls router
(``modulo.api.routes.costs``) and the org-level display-settings router
(``modulo.api.routes.org_settings``). The module docstring exists precisely
because both routers read and write the org's ``settings_json`` under the same
key with the same defaults — a drift between them would silently change the
currency rendered across cost surfaces. Previously the module was exercised
only indirectly (through the HTTP-level suites), so that contract could drift
without a failing test.

This suite locks:

* every exported constant's exact value — changing a default or the settings
  key is now a deliberate, reviewed decision instead of silent drift
* the invariants every consumer relies on — the ``DEFAULT_*`` values are
  members of the ``SUPPORTED_*`` sets the routers validate against, alert
  thresholds stay within the 1..100 range the read path accepts, and the
  supported sets hold canonical, duplicate-free spellings
* the cross-router wiring — both routers import their constants from this
  module rather than re-declaring them, and the org-settings response model's
  hardcoded ``currency`` default still matches ``DEFAULT_CURRENCY``
"""

from __future__ import annotations

import ast
import math
from pathlib import Path

import pytest

from modulo.core import cost_settings as cs

BACKEND = Path(__file__).resolve().parent.parent.parent.parent
ROUTERS = BACKEND / "src" / "modulo" / "api" / "routes"


def _router_imports(router: str) -> set[str]:
    """Names a router module imports from ``modulo.core.cost_settings``."""
    source = (ROUTERS / f"{router}.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "modulo.core.cost_settings":
            imported.update(alias.name for alias in node.names)
    return imported


def _org_settings_currency_default() -> str | None:
    """The hardcoded ``currency`` default on ``OrgSettingsResponse``, if any."""
    source = (ROUTERS / "org_settings.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or node.name != "OrgSettingsResponse":
            continue
        for item in node.body:
            if (
                isinstance(item, ast.AnnAssign)
                and isinstance(item.target, ast.Name)
                and item.target.id == "currency"
                and isinstance(item.value, ast.Constant)
                and isinstance(item.value.value, str)
            ):
                return item.value.value
    return None


class TestConstantValues:
    """Pin the exported values so any change is deliberate and reviewed."""

    def test_cost_controls_key(self) -> None:
        assert cs.COST_CONTROLS_KEY == "cost_controls"

    def test_default_currency(self) -> None:
        assert cs.DEFAULT_CURRENCY == "USD"

    def test_default_billing_period(self) -> None:
        assert cs.DEFAULT_BILLING_PERIOD == "monthly"

    def test_default_circuit_breaker_is_disabled(self) -> None:
        assert cs.DEFAULT_CIRCUIT_BREAKER_ENABLED is False

    def test_default_alert_thresholds(self) -> None:
        assert cs.DEFAULT_ALERT_THRESHOLDS == (50.0, 75.0, 90.0)


class TestSupportedSets:
    """Lock the supported spellings and their shared-contract invariants."""

    def test_supported_currencies(self) -> None:
        assert frozenset({"USD", "EUR", "GBP"}) == cs.SUPPORTED_CURRENCIES

    def test_supported_billing_periods(self) -> None:
        assert frozenset({"monthly", "quarterly", "annual"}) == cs.SUPPORTED_BILLING_PERIODS

    def test_supported_sets_are_immutable_frozensets(self) -> None:
        assert isinstance(cs.SUPPORTED_CURRENCIES, frozenset)
        assert isinstance(cs.SUPPORTED_BILLING_PERIODS, frozenset)

    @pytest.mark.parametrize("code", ["USD", "EUR", "GBP"])
    def test_currency_codes_are_iso_4217_style(self, code: str) -> None:
        assert len(code) == 3
        assert code.isupper()
        assert code.isalpha()

    @pytest.mark.parametrize("period", ["monthly", "quarterly", "annual"])
    def test_billing_periods_are_lowercase(self, period: str) -> None:
        assert period == period.lower()

    def test_default_currency_is_supported(self) -> None:
        assert cs.DEFAULT_CURRENCY in cs.SUPPORTED_CURRENCIES

    def test_default_billing_period_is_supported(self) -> None:
        assert cs.DEFAULT_BILLING_PERIOD in cs.SUPPORTED_BILLING_PERIODS


class TestAlertThresholds:
    """Default alert thresholds must stay valid persisted values."""

    def test_defaults_are_immutable_tuple(self) -> None:
        assert isinstance(cs.DEFAULT_ALERT_THRESHOLDS, tuple)

    @pytest.mark.parametrize("threshold", [50.0, 75.0, 90.0])
    def test_threshold_is_accepted_float(self, threshold: float) -> None:
        assert threshold in cs.DEFAULT_ALERT_THRESHOLDS

    def test_all_thresholds_are_finite_floats_in_1_100(self) -> None:
        for threshold in cs.DEFAULT_ALERT_THRESHOLDS:
            assert isinstance(threshold, float)
            assert math.isfinite(threshold)
            assert 1 <= threshold <= 100

    def test_thresholds_are_strictly_ascending_and_distinct(self) -> None:
        values = list(cs.DEFAULT_ALERT_THRESHOLDS)
        assert values == sorted(values)
        assert len(set(values)) == len(values)


class TestCrossRouterWiring:
    """Both consuming routers must share the module's constants, not copies."""

    @pytest.mark.parametrize(
        "name",
        ["COST_CONTROLS_KEY", "DEFAULT_CURRENCY", "SUPPORTED_CURRENCIES"],
    )
    def test_both_routers_import_shared_constant(self, name: str) -> None:
        assert name in _router_imports("costs")
        assert name in _router_imports("org_settings")

    @pytest.mark.parametrize(
        "name",
        [
            "COST_CONTROLS_KEY",
            "DEFAULT_ALERT_THRESHOLDS",
            "DEFAULT_BILLING_PERIOD",
            "DEFAULT_CIRCUIT_BREAKER_ENABLED",
            "DEFAULT_CURRENCY",
            "SUPPORTED_BILLING_PERIODS",
            "SUPPORTED_CURRENCIES",
        ],
    )
    def test_costs_router_imports_every_exported_constant(self, name: str) -> None:
        assert name in _router_imports("costs")

    def test_org_settings_currency_default_matches_shared_default(self) -> None:
        assert _org_settings_currency_default() == cs.DEFAULT_CURRENCY

    def test_no_router_imports_settings_namespace(self) -> None:
        # a star/namespace import would hide a copy-paste divergence behind an
        # attribute-access indirection; the routers must name the constants
        for router in ("costs", "org_settings"):
            source = (ROUTERS / f"{router}.py").read_text(encoding="utf-8")
            tree = ast.parse(source)
            for node in ast.walk(tree):
                assert not (
                    isinstance(node, ast.ImportFrom)
                    and node.module == "modulo.core.cost_settings"
                    and node.names[0].name == "*"
                )
