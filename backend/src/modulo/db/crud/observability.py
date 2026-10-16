"""CRUD for organisation observability/OTel config stored on Organisation."""

import json
import uuid
from typing import Any

from sqlalchemy import bindparam, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value
from sqlalchemy.orm.util import identity_key

from modulo.db.models.organisation import Organisation


async def get_otel_config(session: AsyncSession, org_id: uuid.UUID) -> dict[str, Any]:
    """Return the stored OTel config for an organisation, or {} when unset."""
    # FAR-1025: opt out of the global soft-delete filter — a pending-deletion
    # org is still operationally live; its OTel config must remain readable.
    from modulo.db.soft_delete import include_soft_deleted

    result = await session.execute(
        include_soft_deleted(select(Organisation.otel_config_json).where(Organisation.id == org_id))
    )
    row = result.scalar_one_or_none()
    return row if row is not None else {}


async def update_otel_config(
    session: AsyncSession,
    org_id: uuid.UUID,
    config: dict[str, Any],
) -> dict[str, Any]:
    """Atomically merge ``config`` into the stored OTel config and return it.

    FAR-1832: replaces the ``SELECT ... FOR UPDATE`` read-modify-write with a
    single atomic statement on Postgres (``otel_config_json = COALESCE(...) ||
    :patch``) — the read and the write are one statement, so no interleaving
    writer's keys can be lost and the org row is not pre-locked across the
    read→write window. The row-lock wait is bounded via
    ``set_mutation_row_lock_timeout`` (R5).

    **DELIBERATE DIVERGENCE from** :func:`modulo.db.crud.organisation.update_org_settings_patch`
    (FAR-1832 D1) — do not "fix" this back:

    - it patches ``otel_config_json``, a DIFFERENT column, so it cannot route
      through the settings helper without changing the column contract; and
    - it deliberately carries NO ``deleted_at IS NULL`` predicate, because
      FAR-1025 says a pending-deletion org's OTel config must remain
      **writable** during the confirmation window. The settings helper instead
      refuses soft-deleted orgs on both dialect paths (the FAR-1830 parity
      decision) — inheriting that refusal here would silently regress
      FAR-1025.
    """
    from modulo.db.crud.row_lock import set_mutation_row_lock_timeout
    from modulo.db.crud.run import get_dialect_name
    from modulo.db.soft_delete import include_soft_deleted

    # Bound the row-lock wait BEFORE the write takes one (FAR-1313 ordering).
    await set_mutation_row_lock_timeout(session)
    dialect = await get_dialect_name(session)

    if dialect == "postgresql":
        # One statement: merge + read-back. The raw UPDATE bypasses the ORM
        # soft-delete listener, which is exactly the include_soft_deleted
        # contract above (no ``deleted_at`` predicate — see the docstring).
        stmt = text(
            "UPDATE organisations "
            "SET otel_config_json = "
            "(COALESCE(otel_config_json, '{}'::jsonb) || CAST(:patch AS jsonb)) "
            "WHERE id = :org_id "
            "RETURNING otel_config_json"
        ).bindparams(bindparam("patch", value=json.dumps(config)))
        result = await session.execute(stmt, {"org_id": str(org_id)})
        row = result.first()
        if row is None:
            raise ValueError("Organisation not found")
        raw = row[0]
        merged = json.loads(raw) if isinstance(raw, str) else dict(raw)
        _sync_identity_map(session, org_id, merged)
        return merged

    # Generic backends (SQLite / MariaDB, tests + legacy): no jsonb ``||``, so
    # the equivalent read-merge-write still needs the row lock. Dialect-gated
    # so the JSONB operators never reach a backend that lacks them, and kept
    # include_soft_deleted so BOTH paths behave identically (soft-delete
    # parity — the FAR-1830 lesson).
    result = await session.execute(
        include_soft_deleted(select(Organisation).where(Organisation.id == org_id).with_for_update())
    )
    org = result.scalar_one_or_none()
    if org is None:
        raise ValueError("Organisation not found")
    merged = {**org.otel_config_json, **config}
    org.otel_config_json = merged
    await session.flush()
    return merged


def _sync_identity_map(session: AsyncSession, org_id: uuid.UUID, merged: dict[str, Any]) -> None:
    """Refresh an already-loaded org row to the merged value without dirtying it.

    Mirrors ``db.crud.organisation._sync_identity_map`` for the
    ``otel_config_json`` column: the Postgres path writes via raw SQL, so an
    in-session ``Organisation`` would otherwise keep serving the pre-merge
    config (a follow-up whole-column write from that stale value would drop the
    merge).
    """
    identity_map = getattr(session, "identity_map", None)
    getter = getattr(identity_map, "get", None)
    if getter is None:
        return
    org = getter(identity_key(Organisation, org_id))
    if isinstance(org, Organisation):
        set_committed_value(org, "otel_config_json", merged)
