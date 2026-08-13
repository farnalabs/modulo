"""Shared safe-int coercion helper for connector clients and API routes.

The Jira and Slack connectors each guarded their pagination parsing against
corrupt/non-finite values with a private ``_safe_int`` copy; the dashboard
route carried an older variant that lacked the bool/non-finite guards.
Keeping a single implementation in one place avoids drift between the copies.
"""

from __future__ import annotations

import math
from decimal import Decimal

_NUMERIC_TYPES = (int, float, str, bytes, bytearray, Decimal)


def safe_int(value: object, default: int = 0) -> int:
    """Coerce *value* to int, returning *default* for None, non-finite, or unparseable values.

    Guards against non-finite floats (``inf``/``nan``) which otherwise raise
    ``OverflowError``/``ValueError`` on ``int()`` — Python's json parser
    produces ``inf`` for overflowing literals such as ``1e999``, so a corrupt
    or hostile API response must not be able to crash pagination or
    aggregation. Booleans are rejected (``True == 1`` is a footgun).
    """
    if isinstance(value, bool) or not isinstance(value, _NUMERIC_TYPES):
        return default
    if isinstance(value, (float, Decimal)) and not math.isfinite(value):
        return default
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default
