"""Lenient UUID coercion helpers shared across request and persistence boundaries.

The schema migrations promote several historically ``String`` FK columns to
native ``Uuid`` (see migration 0166_promote_uuid_fk_columns), but the API request models still
accept ``str`` for those fields. Casting a free-form ``str`` with ``uuid.UUID(...)``
raises ``ValueError`` on malformed input and produces an unhandled 500 at the
boundary. ``coerce_uuid`` returns ``None`` for anything that is not a valid UUID,
so callers can downgrade a bad value to "unset" instead of crashing.
"""

from __future__ import annotations

import uuid
from typing import Any


def coerce_uuid(value: Any) -> uuid.UUID | None:
    """Return ``value`` as a ``uuid.UUID`` or ``None`` when it is not a valid UUID.

    Accepts ``None``, already-``UUID`` instances, and ``str``/``bytes``/``int``
    forms that ``uuid.UUID`` understands. Any ``TypeError``/``ValueError`` (e.g. a
    malformed UUID string from an API boundary) yields ``None`` rather than
    raising, so callers can treat the value as unset and avoid 500s.
    """
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return value
    try:
        if isinstance(value, int):
            return uuid.UUID(int=value)
        return uuid.UUID(value)
    except (TypeError, ValueError, AttributeError):
        return None
