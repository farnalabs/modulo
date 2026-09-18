"""Boot health predicates (FAR-671) — pure, no ``modulo`` app imports.

Promoted from the private ``modulo.api.main._db_is_at_migration_head`` so the
native launcher (ADR 031) can reuse the exact migration-head fast-path check.
The predicate is dependency-light: alembic and SQLAlchemy are imported lazily
inside the function; nothing at module import time. The engine is passed in
by the caller (duck-typed ``connect()``), keeping this module free of
engine-factory imports.
"""

import logging
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)


def resolve_alembic_ini() -> Path:
    """Locate backend/alembic.ini relative to this module (cwd-independent).

    Same robustness contract as the historical ``modulo.api.main
    ._resolve_alembic_ini`` — but anchored at this module's own file so the
    native launcher and the API process resolve the same config.
    """
    candidate = Path(__file__).resolve().parents[3] / "alembic.ini"
    if candidate.exists():
        return candidate
    return Path("alembic.ini")


async def db_is_at_migration_head(engine: Any, alembic_ini: Path | None = None) -> bool:
    """Return True when the DB's ``alembic_version`` already equals the head.

    Boot fast-path: multiple machines boot simultaneously on a fresh deploy and
    every process group runs migrations serialised by the advisory lock —
    machines that did not win the lock waited for the full poll budget before
    FATALing, even when the schema was already up to date. When the DB is
    already at head there is no work to do, so the advisory lock acquisition and
    the alembic run are pure contention and are skipped entirely.

    Fail-safe: any failure (missing table, multiple heads, connection error)
    returns False so the caller proceeds through the normal retry/lock path.
    """
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    from sqlalchemy import text

    ini_path = alembic_ini if alembic_ini is not None else resolve_alembic_ini()
    config = Config(str(ini_path))
    config.set_main_option(
        "script_location",
        str(ini_path.parent / "src" / "modulo" / "db" / "migrations"),
    )
    try:
        head = ScriptDirectory.from_config(config).get_current_head()
    except Exception:
        return False
    if not head:
        return False
    try:
        async with engine.connect() as conn:
            result = await conn.execute(text("SELECT version_num FROM alembic_version"))
            versions = {row[0] for row in result.fetchall()}
    except Exception:
        return False
    return versions == {head}
