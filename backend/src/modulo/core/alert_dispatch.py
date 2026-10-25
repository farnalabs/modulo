"""Shared plumbing for the operator alert sweeps (FAR-2116).

Both alert sweeps — ``core.capacity_alerts`` (capacity pressure and backpressure)
and ``core.pipeline_decay_alerts`` (pipeline success-rate decay) — hold one open
alert per subject in Redis and dispatch through the same ``Notifier``. The two
pieces that must never drift between them live here once:

* :func:`read_alert_state` — the Redis state read. A corrupt or non-dict blob is
  treated as ABSENT (never as "an alert is open", which would silence the
  subject forever), and the caller's logger/event name is used so each sweep
  keeps its own log namespace.
* :func:`build_notifier` — the single construction of the shared ``Notifier``.
  The imports are function-local so this module stays a light dependency for the
  cron graph and for the sweeps that import it at module scope.

Deliberately a leaf apart from :mod:`modulo.settings`: no sweep imports another
sweep to reach either helper.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from redis.asyncio import Redis as AsyncRedis

from modulo.settings import Settings

__all__ = ["build_notifier", "decode_redis_value", "read_alert_state"]


def decode_redis_value(raw: Any) -> str:
    """Decode a Redis value that may come back as ``bytes`` or ``str``."""
    return raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)


async def read_alert_state(
    redis_client: AsyncRedis,
    key: str,
    *,
    logger: logging.Logger,
    event: str,
) -> dict[str, Any] | None:
    """Read one open-alert state blob, treating corruption as absent.

    ``event`` is the sweep's own log event name (for example
    ``"capacity_alerts.state_unreadable"``) so the warning stays attributable to
    the sweep that hit the bad blob.
    """
    raw = await redis_client.get(key)
    if raw is None:
        return None
    try:
        parsed = json.loads(decode_redis_value(raw))
    except (json.JSONDecodeError, TypeError):
        # A corrupt blob is treated as ABSENT so the subject can re-alert;
        # treating it as "an alert is open" would silence it forever.
        logger.warning("%s.state_unreadable", event, extra={"key": key})
        return None
    return parsed if isinstance(parsed, dict) else None


def build_notifier(settings: Settings) -> Any:
    """Build the shared Notifier (lazy imports keep the cron graph light)."""
    from modulo.core.cron_helpers import _get_engine
    from modulo.core.notifier import Notifier

    return Notifier(_get_engine(), settings.fernet_key)
