"""Unit tests for persist_artifact_pointers (FAR-582 artifact pointers CRUD).

Uses a mocked AsyncSession — no database. Covers the empty-pointers (NULL)
and populated-pointers write paths.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.crud import run_node_outputs as rno


async def _call(pointers: list[dict[str, object]]) -> dict[str, object]:
    session = AsyncMock(spec=AsyncSession)
    session.get_bind.return_value.dialect.name = "postgresql"
    await rno.persist_artifact_pointers(
        session,
        run_id=uuid.uuid4(),
        organisation_id=uuid.uuid4(),
        node_id="n1",
        attempt_key="run:x:node:y:0",
        pointers=pointers,
    )
    assert session.execute.await_count == 1
    # session.execute(stmt, [values]) — values is the single element of the list arg.
    return session.execute.call_args.args[1][0]


async def test_persist_artifact_pointers_stores_null_when_empty() -> None:
    """Empty pointers list is persisted as NULL (per docstring)."""
    values = await _call([])
    assert values["artifacts_json"] is None


async def test_persist_artifact_pointers_stores_pointers() -> None:
    """Populated pointers list is persisted verbatim."""
    ptrs: list[dict[str, object]] = [
        {
            "stream": "stdout",
            "rel_path": "org/run/node/attempt.stdout.zst",
            "size_bytes": 11,
            "sha256": "x",
            "compression": "zstd",
        }
    ]
    values = await _call(ptrs)
    assert values["artifacts_json"] == ptrs


async def test_persist_artifact_pointers_writes_run_and_node_keys() -> None:
    run_id = uuid.uuid4()
    org_id = uuid.uuid4()
    session = AsyncMock(spec=AsyncSession)
    session.get_bind.return_value.dialect.name = "postgresql"
    await rno.persist_artifact_pointers(
        session,
        run_id=run_id,
        organisation_id=org_id,
        node_id="node-9",
        attempt_key="attempt-key",
        pointers=[],
    )
    values = session.execute.call_args.args[1][0]
    assert values["run_id"] == run_id
    assert values["organisation_id"] == org_id
    assert values["node_id"] == "node-9"
    assert values["attempt_key"] == "attempt-key"
