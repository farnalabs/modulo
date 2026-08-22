"""LibrarySyncState — instance-global cache of the community-library sync (FAR-363).

NOT org-scoped: the community library is a single global catalogue shared by the
whole instance, so the cache row is a singleton (fixed primary key) rather than
per-organisation state. Lives in ``modulo.db.models`` (one file per entity) so
Alembic reconciliation sees it; ``modulo.core.library_sync.models`` re-exports
it for the sync package's public API.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, String, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from modulo.db.models.base import Base

# Fixed singleton primary key — the library sync state is a single-row table.
SINGLETON_ID = uuid.UUID("6f6a1c1e-0b3a-4c8d-9e2f-7a5b1c2d3e4f")


class LibrarySyncState(Base):
    __tablename__ = "library_sync_state"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=SINGLETON_ID)
    # Last-good signed manifest (as verified against the root public key).
    manifest_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    # Last-good catalog entries list ({id, type, slug, ...} dicts).
    catalog_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    last_synced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.current_timestamp(),
        nullable=False,
    )
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.current_timestamp(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
        nullable=False,
    )
