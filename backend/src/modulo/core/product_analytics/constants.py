"""Constants for the product analytics consent and settings model."""

from __future__ import annotations

from datetime import timedelta

# ---------------------------------------------------------------------------
# settings_json key
# ---------------------------------------------------------------------------
PRODUCT_ANALYTICS_KEY: str = "product_analytics"

# ---------------------------------------------------------------------------
# Consent levels
# ---------------------------------------------------------------------------
LEVEL_OFF: str = "off"
LEVEL_ALL: str = "all"
VALID_LEVELS: frozenset[str] = frozenset({LEVEL_OFF, LEVEL_ALL})

# ---------------------------------------------------------------------------
# Prompted states
# ---------------------------------------------------------------------------
PROMPTED_YES: str = "yes"
PROMPTED_NO: str = "no"
PROMPTED_DISMISSED: str = "dismissed"
VALID_PROMPTED: frozenset[str | None] = frozenset({None, PROMPTED_YES, PROMPTED_NO, PROMPTED_DISMISSED})

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
DEFAULT_LEVEL: str = LEVEL_OFF
DEFAULT_PROMPTED: None = None
DEFAULT_PROMPTED_AT: None = None
DEFAULT_LEVEL_CHANGED_AT: None = None

# ---------------------------------------------------------------------------
# SystemConfig keys
# ---------------------------------------------------------------------------
INSTANCE_SWITCH_KEY: str = "product_analytics_enabled"
LICENSE_ENFORCEMENT_KILL_SWITCH_KEY: str = "product_analytics_license_enforcement_kill_switch"

# ---------------------------------------------------------------------------
# Env var override
# ---------------------------------------------------------------------------
ENV_INSTANCE_SWITCH: str = "MODULO_PRODUCT_ANALYTICS_ENABLED"

# ---------------------------------------------------------------------------
# Dismiss cooldown
# ---------------------------------------------------------------------------
DISMISS_COOLDOWN: timedelta = timedelta(days=7)

# ---------------------------------------------------------------------------
# Partner license claim
# ---------------------------------------------------------------------------
PARTNER_LICENSE_CLAIM: str = "product_analytics_required"
