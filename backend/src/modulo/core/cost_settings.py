"""Shared org cost-control settings keys and defaults.

Both the admin cost-controls router (``modulo.api.routes.costs``) and the
org-level display-settings router (``modulo.api.routes.org_settings``) read and
write the org's ``settings_json`` under the same ``cost_controls`` key with the
same defaults. Centralising the constants here keeps the display currency in
sync with what the admin UI persists — a drift between the two routers would
silently change the currency rendered across cost surfaces.
"""

COST_CONTROLS_KEY = "cost_controls"
DEFAULT_CURRENCY = "USD"
DEFAULT_BILLING_PERIOD = "monthly"
DEFAULT_CIRCUIT_BREAKER_ENABLED = False
DEFAULT_ALERT_THRESHOLDS = (50, 75, 90)

SUPPORTED_CURRENCIES = frozenset({"USD", "EUR", "GBP"})
SUPPORTED_BILLING_PERIODS = frozenset({"monthly", "quarterly", "annual"})
