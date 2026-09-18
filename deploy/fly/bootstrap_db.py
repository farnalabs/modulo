"""Run at container startup to prepare the database.

Thin importing shim (FAR-671 / ADR 031 Decision 2): the implementation lives
in ``modulo.db.bootstrap`` so the container boot and the native single-install
launcher share the exact logic and cannot drift. Container-boot behaviour is
unchanged — the entrypoint still runs ``python3 /app/deploy/fly/bootstrap_db.py``.

The helpers are re-exported here for the legacy characterization tests (which
load this file directly via importlib) and for any script that imported them
from this path.
"""

import asyncio

from modulo.db.bootstrap import main
from modulo.db.url_utils import derive_system_database_url, fix_database_url

__all__ = ["asyncio", "derive_system_database_url", "fix_database_url", "main"]

if __name__ == "__main__":
    main()
