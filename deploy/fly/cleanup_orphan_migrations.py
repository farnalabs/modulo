"""Clean orphaned alembic_version entries from restructured migration branches.

Called by entrypoint.sh before ``alembic upgrade heads``. Removes any
version_num from alembic_version that does NOT match a known migration
file revision ID. This prevents ``Can't locate revision identified by X``
errors when old branch revisions were squashed or restructured.
"""
import os
import re
import asyncio
import pathlib

from modulo.db.session import AsyncSessionLocal
from sqlalchemy import text


async def _cleanup() -> None:
    known: set[str] = set()
    mod_path = (
        pathlib.Path(os.environ.get("MODULO_ROOT", "/app"))
        / "src"
        / "modulo"
        / "db"
        / "migrations"
        / "versions"
    )
    for f in mod_path.glob("*.py"):
        m = re.search(
            r'^revision:\s*str\s*=\s*"([^"]+)"',
            f.read_text(encoding="utf-8"),
            re.MULTILINE,
        )
        if m:
            known.add(m.group(1))

    async with AsyncSessionLocal() as s:
        async with s.begin():
            await s.execute(
                text(
                    "CREATE TABLE IF NOT EXISTS alembic_version "
                    "(version_num VARCHAR(255) NOT NULL PRIMARY KEY)"
                )
            )
            result = await s.execute(text("SELECT version_num FROM alembic_version"))
            db_versions = [row[0] for row in result.fetchall()]
            orphans = [v for v in db_versions if v not in known]
            for orphan in orphans:
                await s.execute(
                    text(
                        "DELETE FROM alembic_version WHERE version_num = :vn"
                    ),
                    {"vn": orphan},
                )
                print(f"  Removed orphan alembic_version: {orphan}")
            if not orphans:
                print("  No orphaned alembic_version entries found")
            else:
                print(f"  Removed {len(orphans)} orphan(s), "
                      f"{len(known)} known revisions in codebase")


if __name__ == "__main__":
    asyncio.run(_cleanup())
