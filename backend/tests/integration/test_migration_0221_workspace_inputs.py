"""Migration 0221 (FAR-802 MWI P1): ``environment_profiles.workspace_inputs``.

Runs against the migrated testcontainer (``test_initial_migration`` harness,
same pattern as test_migration_0066). Suite is integration-only — deferred
locally in worktree QA; runs in the deploy-workflow CI.

Skips (does not fail) when no Docker engine is reachable — same pattern as
test_schemathesis.py's Redis probe: a collection-time probe via
``docker info``, mirroring the BDD-step Docker-availability check. CI
(deploy workflow) has Docker, so the suite runs there.

Asserts the column exists with the right type/nullability and the server
default backfill expectation (nullable pre-backfill, '[]' default per the
migration's design so every org profile is MWI-OPT-IN with no inputs).
"""

from __future__ import annotations

import shutil
import subprocess
from typing import Any

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncEngine


def _docker_reachable() -> bool:
    if not shutil.which("docker"):
        return False
    try:
        subprocess.run(
            ["docker", "info"],  # noqa: S607 — test helper
            capture_output=True,
            timeout=10,
            check=True,
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return False
    return True


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not _docker_reachable(),
        reason="no reachable Docker engine (testcontainers Postgres required)",
    ),
]


async def _columns(db_engine: AsyncEngine, table: str) -> list[dict[str, Any]]:
    async with db_engine.connect() as connection:
        return await connection.run_sync(
            lambda sync_connection: inspect(sync_connection).get_columns(table),
        )


async def test_environment_profiles_workspace_inputs_column_exists(db_engine: AsyncEngine) -> None:
    cols = await _columns(db_engine, "environment_profiles")
    by_name = {c["name"]: c for c in cols}
    assert "workspace_inputs" in by_name
    col = by_name["workspace_inputs"]
    assert col["nullable"] is True  # MWI is opt-in: absent/default means no managed inputs
    assert str(col["type"]).lower().startswith("json")


async def test_fresh_profiles_default_to_empty_workspace_inputs(db_engine: AsyncEngine) -> None:
    """Server default '[]' (set by the migration): a new profile is MWI-OPT-IN
    with zero managed inputs until an operator adds them."""
    await db_engine.dispose()
    async with db_engine.connect() as conn:
        default = None
        rows = await conn.execute(
            text(
                "SELECT column_default FROM information_schema.columns "
                "WHERE table_name = 'environment_profiles' AND column_name = 'workspace_inputs' "
                "AND table_schema = 'public'"
            )
        )
        row = rows.fetchone()
        if row is not None:
            default = row[0]
    assert default is not None
    assert "[]" in str(default)
