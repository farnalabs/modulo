"""Constants for the product analytics consent and settings model."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

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

# Metrics-dump system_config keys. These live here (not in ``metrics_dump``) so
# every reader can import them without dragging in the dump's heavy transitive
# chain (``metrics_dump`` -> ``saq_worker`` -> ``pipeline_execution`` ->
# ``langgraph``), which the ``api-does-not-import-langgraph-directly``
# import-linter contract forbids.
DUMP_WATERMARK_KEY: str = "product_analytics_last_dumped_date"
DUMP_COUNT_KEY: str = "product_analytics_dump_count"


def coerce_dump_count(value: Any) -> int:
    """Coerce a stored dump-count value to a non-negative int.

    Missing, malformed, or negative stored values degrade to ``0`` so a corrupt
    row can never mask the count as a failure or produce a negative total. This
    is the single source of truth for the key's storage format, shared by the
    dump increment path and the transparency reader.
    """
    if value is None:
        return 0
    try:
        return max(0, int(value))
    except (ValueError, TypeError, OverflowError):
        return 0


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
