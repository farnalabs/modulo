"""Re-export of the LibrarySyncState model for the library_sync package (FAR-363).

The model itself lives in ``modulo.db.models.library_sync_state`` (the repo's
one-file-per-entity convention, and required so Alembic reconciliation sees the
table without ``modulo.db`` importing ``modulo.core`` — import-linter contract
``db-does-not-import-core``). This module re-exports it so sync code and tests
can import it through the package without reaching into ``modulo.db``.
"""

from __future__ import annotations

from modulo.db.models.library_sync_state import SINGLETON_ID, LibrarySyncState

__all__ = ["SINGLETON_ID", "LibrarySyncState"]
