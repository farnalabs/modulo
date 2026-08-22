"""Sync orchestration for the hosted community library (FAR-363).

Fetches the signed manifest, verifies it, applies revocations to the cached
catalogue, and persists the last-good state. Fail-open by contract: any error
is logged and the last-good cached manifest is preserved — the library is
optional and never blocks the product.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.core.library_sync.client import LibraryClient
from modulo.core.library_sync.manifest import parse_manifest
from modulo.core.library_sync.models import SINGLETON_ID, LibrarySyncState
from modulo.settings import get_settings

__all__ = [
    "SyncResult",
    "get_cached_manifest",
    "is_revoked",
    "sync_library",
]

_log = logging.getLogger(__name__)

# Status value written onto catalogue entries whose id appears in the manifest's
# revoked list. Consumed by the library browser as "suppressed from browse".
REVOKED_STATUS = "revoked"


@dataclass
class SyncResult:
    success: bool
    entries_count: int = 0
    revoked_count: int = 0
    error: str | None = None


async def _load_state(session: AsyncSession) -> LibrarySyncState | None:
    result = await session.execute(select(LibrarySyncState).where(LibrarySyncState.id == SINGLETON_ID))
    return result.scalar_one_or_none()


async def _in_transaction(session: AsyncSession) -> bool:
    in_transaction = session.in_transaction()
    if asyncio.iscoroutine(in_transaction):
        in_transaction = await in_transaction
    return bool(in_transaction)


async def _read_state(session: AsyncSession) -> LibrarySyncState | None:
    """Read the singleton state, wrapping in a transaction only when none is active.

    Sessions use ``autobegin=False`` (the codebase DI convention), so a bare
    ``session.execute`` raises ``InvalidRequestError`` outside a transaction.
    """
    if await _in_transaction(session):
        return await _load_state(session)
    async with session.begin():
        return await _load_state(session)


async def sync_library(session: AsyncSession) -> SyncResult:
    """Main sync entry — fetch, verify, apply revocations, cache.

    Never raises: every failure path logs and returns a ``SyncResult`` with
    ``success=False`` (fail-open — the last-good cached manifest survives).
    """
    settings = get_settings()
    endpoint = settings.modulo_library_endpoint
    root_key = settings.modulo_library_root_public_key
    if not endpoint:
        return SyncResult(success=False, error="community library disabled (MODULO_LIBRARY_ENDPOINT unset)")

    client = LibraryClient(
        endpoint=endpoint,
        root_public_key_pem=root_key,
        timeout_seconds=settings.modulo_library_sync_timeout_seconds,
    )
    try:
        manifest = await client.fetch_manifest()
        if manifest is None:
            error = "manifest fetch or verification failed"
            await _record_failure(session, error)
            return SyncResult(success=False, error=error)

        catalog_entries = await client.fetch_catalog()
        if catalog_entries is None:
            error = "catalog fetch failed"
            await _record_failure(session, error)
            return SyncResult(success=False, error=error)

        data = parse_manifest(manifest)
        revoked_ids = {str(entry.get("id")) for entry in data.revoked if entry.get("id") is not None}
        applied_catalog = _apply_revocations(catalog_entries, revoked_ids)

        now = datetime.now(UTC)
        async with session.begin():
            state = await _load_state(session)
            if state is None:
                state = LibrarySyncState(
                    id=SINGLETON_ID,
                    manifest_json=manifest,
                    catalog_json=applied_catalog,
                    last_synced_at=now,
                    last_success_at=now,
                    last_error=None,
                )
                session.add(state)
            else:
                state.manifest_json = manifest
                state.catalog_json = applied_catalog
                state.last_synced_at = now
                state.last_success_at = now
                state.last_error = None

        _log.info(
            "library_sync.success",
            extra={
                "entries_count": len(applied_catalog),
                "revoked_count": len(revoked_ids),
            },
        )
        return SyncResult(
            success=True,
            entries_count=len(applied_catalog),
            revoked_count=len(revoked_ids),
        )
    except Exception:
        _log.exception("library_sync.sync_failed")
        await _record_failure(session, "unexpected sync failure")
        return SyncResult(success=False, error="unexpected sync failure")
    finally:
        await client.close()


def _apply_revocations(catalog_entries: list[dict[str, Any]], revoked_ids: set[str]) -> list[dict[str, Any]]:
    """Mark catalogue entries whose id is in the manifest's revoked list.

    Returns a new list; the caller's list is never mutated.
    """
    applied: list[dict[str, Any]] = []
    for entry in catalog_entries:
        entry_id = str(entry.get("id")) if entry.get("id") is not None else ""
        if entry_id in revoked_ids:
            copied = dict(entry)
            copied["status"] = REVOKED_STATUS
            applied.append(copied)
        else:
            applied.append(entry)
    return applied


async def _record_failure(session: AsyncSession, error: str) -> None:
    """Stamp the singleton state's last_error / last_synced_at without touching the
    last-good manifest/catalogue (fail-open: stale-but-present beats absent)."""
    try:
        now = datetime.now(UTC)
        async with session.begin():
            state = await _load_state(session)
            if state is None:
                state = LibrarySyncState(
                    id=SINGLETON_ID,
                    manifest_json={},
                    catalog_json=[],
                    last_synced_at=now,
                    last_success_at=None,
                    last_error=error,
                )
                session.add(state)
            else:
                state.last_synced_at = now
                state.last_error = error
    except Exception:
        _log.exception("library_sync.record_failure_failed")


async def get_cached_manifest(session: AsyncSession) -> dict[str, Any] | None:
    """Return the last-good cached manifest, or ``None`` if none was ever stored."""
    try:
        state = await _read_state(session)
        if state is None:
            return None
        return state.manifest_json if isinstance(state.manifest_json, dict) and state.manifest_json else None
    except Exception:
        _log.exception("library_sync.get_cached_manifest_failed")
        return None


async def is_revoked(session: AsyncSession, entry_id: str) -> bool:
    """Return True when *entry_id* appears in the cached manifest's revoked list."""
    try:
        manifest = await get_cached_manifest(session)
        if not manifest:
            return False
        data = parse_manifest(manifest)
        return any(str(entry.get("id")) == entry_id for entry in data.revoked)
    except Exception:
        _log.exception("library_sync.is_revoked_failed")
        return False
