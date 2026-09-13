"""Org-scoped, persistent runtime flags backed by ``Organisation.settings_json``.

FAR-795 Slice A substrate. The process-global
:class:`~modulo.core.runtime_config.store.RuntimeConfigStore` is keyed by
env-var names and is NOT visible across processes — a flag set via the API
process is invisible to the SAQ worker processes that finalise/reconcile
work-item minting. This module provides the org-scoped, DB-backed alternative:

- Storage reuses the existing org settings surface — the same
  ``Organisation.settings_json`` JSONB column that backs
  ``sandbox_concurrency_limit``, ``run_concurrency_limit`` and
  ``retention_days``. No schema change is needed.
- Reads are cached in-process with a short TTL (≤30s) so the mint chokepoint
  can gate cheaply. A write invalidates the writing process's cache entry;
  OTHER processes see the change within at most ``_CACHE_TTL_SECONDS`` —
  callers must treat this as worst-case staleness, which is acceptable
  because the safety layer fail-closes to OFF.
- Fail-closed: :func:`is_org_flag_enabled` returns ``False`` (disabled) on
  ANY read error, including a DB outage. This is a SAFETY control — agent
  minting must never be enabled when its enablement state is unknown.

RLS/tenant context: the caller (API route or SAQ worker session) is
responsible for setting the org RLS context (``set_rls_org``) on the session
before reading/writing — same contract as the other ``settings_json`` readers
in ``db/crud/run.py``.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

from sqlalchemy import select

_log = logging.getLogger(__name__)

# ── Flag keys (settings_json keys; never rename once shipped) ──────────────

#: FAR-795 safety-layer kill-switch for agent-sourced work-item minting.
#: Default OFF; only an explicit ``True`` (bool) enables minting.
FLAG_WORK_ITEM_AGENT_MINTING_ENABLED = "work_item_agent_minting_enabled"

_KNOWN_FLAGS: frozenset[str] = frozenset({FLAG_WORK_ITEM_AGENT_MINTING_ENABLED})

# ── In-process TTL cache ────────────────────────────────────────────────────

#: Worst-case staleness after another process writes the flag.
_CACHE_TTL_SECONDS = 30.0

# key: (org_id, flag_name) -> (value, monotonic expiry)
_flag_cache: dict[tuple[uuid.UUID, str], tuple[bool, float]] = {}


def clear_org_flag_cache() -> None:
    """Drop every cached flag value (test isolation / operator escape hatch)."""
    _flag_cache.clear()


def _resolve_flag_value(raw: Any) -> bool:
    """Normalize a stored settings_json value to the flag boolean.

    Strictly ``True`` (bool) enables; every other stored shape (``False``,
    ``None``, string, int, absent key → ``None`` from ``.get``) disables.
    Booleans arriving via JSON decoders are the contract; a string
    ``"true"`` written by direct DB access is deliberately NOT honoured.
    """
    return raw is True


async def _read_org_settings_json(session: Any, org_id: uuid.UUID) -> Any:
    """Read the org's ``settings_json`` mapping (or ``None`` when missing)."""
    from modulo.db.crud.organisation import get_organisation

    org = await get_organisation(session, org_id)
    if org is None:
        _log.warning("org_flags.org_not_found", extra={"org_id": str(org_id)})
        return None
    return org.settings_json


async def read_org_flag(session: Any, org_id: uuid.UUID, flag_name: str, *, default: bool = False) -> bool:
    """Read an org flag from ``Organisation.settings_json`` with TTL caching.

    Populates the in-process cache; DB errors propagate to the caller —
    use :func:`is_org_flag_enabled` for the fail-closed variant.
    """
    if flag_name not in _KNOWN_FLAGS:
        _log.warning("org_flags.unknown_flag", extra={"flag": flag_name})
        return default
    now = time.monotonic()
    cached = _flag_cache.get((org_id, flag_name))
    if cached is not None and cached[1] > now:
        return cached[0]

    settings_json = await _read_org_settings_json(session, org_id)
    raw = settings_json.get(flag_name) if isinstance(settings_json, dict) else None
    value = _resolve_flag_value(raw) if raw is not None else default
    _flag_cache[(org_id, flag_name)] = (value, now + _CACHE_TTL_SECONDS)
    return value


async def is_org_flag_enabled(session: Any, org_id: uuid.UUID, flag_name: str) -> bool:
    """Fail-closed org-flag read for hot paths.

    Returns ``False`` (flag disabled) on ANY error — DB outage, missing org,
    malformed settings. Agent-sourced work-item minting (FAR-795 safety
    layer) must never run while its enablement state is unknown. Errors are
    logged and the (possibly stale-then-evicted) cache entry is dropped so
    the next call retries the DB rather than trusting a poisoned value.
    """
    try:
        return await read_org_flag(session, org_id, flag_name, default=False)
    except Exception:
        _log.exception("org_flags.read_failed_fail_closed", extra={"org_id": str(org_id), "flag": flag_name})
        _flag_cache.pop((org_id, flag_name), None)
        return False


async def set_org_flag(session: Any, org_id: uuid.UUID, flag_name: str, value: bool) -> None:
    """Persist an org flag to ``Organisation.settings_json``.

    Row-level lock (``SELECT ... FOR UPDATE``) so the read-modify-write
    cannot drop a concurrent settings writer's change. The caller owns the
    transaction (``session.begin()`` / commit) — the minting route wraps
    this in its own transaction and records the audit event after it.

    On success the writing process's cache entry is refreshed immediately;
    other processes see the change within ``_CACHE_TTL_SECONDS``.
    """
    if not isinstance(value, bool):
        raise TypeError(f"org flag value must be bool, got {type(value).__name__}")
    if flag_name not in _KNOWN_FLAGS:
        raise ValueError(f"unknown org flag: {flag_name!r}")

    from modulo.db.models.organisation import Organisation

    result = await session.execute(select(Organisation).where(Organisation.id == org_id).limit(1).with_for_update())
    org = result.scalar_one_or_none()
    if org is None:
        raise LookupError(f"organisation not found: {org_id}")

    settings = dict(org.settings_json) if org.settings_json else {}
    settings[flag_name] = value
    org.settings_json = settings
    await session.flush()

    _flag_cache[(org_id, flag_name)] = (value, time.monotonic() + _CACHE_TTL_SECONDS)
    _log.info(
        "org_flags.set",
        extra={"org_id": str(org_id), "flag": flag_name, "value": value},
    )
