"""Real-Postgres integration tests proving cross-tenant checkpoint isolation.

``ModuloPostgresSaver`` scopes every checkpoint read/write by
``organisation_id`` (part of each checkpoint table's primary key), so org A can
never read org B's checkpoints even when both share a thread name. The unit
tests assert the org_id appears in the generated SQL; these tests prove the
isolation holds against a real Postgres database (testcontainers).

On Windows, pytest-asyncio runs on the ``ProactorEventLoop``, which psycopg's
async connection refuses. ``from_conn_string`` (psycopg v3) therefore runs its
DB work on a dedicated background ``SelectorEventLoop`` thread, which works on
both Windows and Linux.
"""

import asyncio
import selectors
import threading
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from modulo.core.pipeline_engine.modulo_saver import ModuloPostgresSaver

pytestmark = [
    pytest.mark.integration,
]

_FERNET_KEY = Fernet.generate_key().decode()

_saver_loop: asyncio.AbstractEventLoop | None = None


def _run_saver(coro: Any) -> Any:
    """Run a psycopg-backed saver coroutine on a dedicated SelectorEventLoop.

    pytest-asyncio uses the ProactorEventLoop on Windows, which psycopg's async
    connections reject. A background SelectorEventLoop thread serves the same
    connections on both platforms.
    """
    global _saver_loop
    if _saver_loop is None or _saver_loop.is_closed():
        _saver_loop = asyncio.SelectorEventLoop(selectors.SelectSelector())
        threading.Thread(target=_saver_loop.run_forever, name="saver-selector-loop", daemon=True).start()
    future = asyncio.run_coroutine_threadsafe(coro, _saver_loop)
    return future.result(timeout=120)


async def _create_org(db_engine: AsyncEngine) -> uuid.UUID:
    org_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO organisations (id, name, slug, settings_json) VALUES (:id, :name, :slug, '{}'::json)",
            ),
            {
                "id": str(org_id),
                "name": "Checkpoint Isolation Org",
                "slug": f"ckpt-iso-{org_id.hex[:8]}",
            },
        )
    return org_id


def _psycopg_url(migrated_db_url: str) -> str:
    """``from_conn_string`` connects via psycopg (v3), not the asyncpg driver."""
    return migrated_db_url.replace("postgresql+asyncpg://", "postgresql://", 1)


def _checkpoint(marker: str) -> dict[str, Any]:
    """A minimal LangGraph checkpoint carrying an org-distinguishing marker."""
    return {
        "v": 1,
        "id": f"ckp-{marker}",
        "ts": "2026-01-01T00:00:00+00:00",
        "channel_values": {"owner": marker},
        "versions_seen": {},
        "pending_sends": [],
    }


@asynccontextmanager
async def _saver_pair(
    migrated_db_url: str,
    org_a: uuid.UUID,
    org_b: uuid.UUID,
) -> AsyncIterator[tuple[ModuloPostgresSaver, ModuloPostgresSaver]]:
    """Two savers scoped to different orgs, each on its own DB connection."""
    async with ModuloPostgresSaver.from_conn_string(
        _psycopg_url(migrated_db_url),
        organisation_id=org_a,
        fernet_key=_FERNET_KEY,
    ) as saver_a:
        await saver_a.setup()
        async with ModuloPostgresSaver.from_conn_string(
            _psycopg_url(migrated_db_url),
            organisation_id=org_b,
            fernet_key=_FERNET_KEY,
        ) as saver_b:
            await saver_b.setup()
            yield saver_a, saver_b


class TestCrossTenantIsolation:
    async def test_org_b_cannot_read_org_a_checkpoint(
        self,
        migrated_db_url: str,
        db_engine: AsyncEngine,
    ) -> None:
        """A checkpoint written by org A is invisible to org B's saver."""
        org_a = await _create_org(db_engine)
        org_b = await _create_org(db_engine)
        thread_id = f"thread-a-{uuid.uuid4().hex[:8]}"

        async def _interaction() -> None:
            async with _saver_pair(migrated_db_url, org_a, org_b) as (saver_a, saver_b):
                await saver_a.aput(
                    {
                        "configurable": {
                            "thread_id": thread_id,
                            "checkpoint_ns": "",
                            "checkpoint_id": "ckp-a",
                        }
                    },
                    _checkpoint("org-a"),
                    {"source": "isolation-test"},
                )

                read_config = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}

                # Org B cannot read org A's checkpoint...
                assert await saver_b.aget_tuple(read_config) is None

                # ...while org A's own saver reads it back.
                org_a_tuple = await saver_a.aget_tuple(read_config)
                assert org_a_tuple is not None
                assert org_a_tuple.checkpoint["channel_values"]["owner"] == "org-a"

                # alist behaves the same way.
                org_b_list = [t async for t in saver_b.alist(read_config)]
                assert org_b_list == []
                org_a_list = [t async for t in saver_a.alist(read_config)]
                assert len(org_a_list) == 1

        _run_saver(_interaction())

    async def test_same_thread_id_does_not_collide_across_orgs(
        self,
        migrated_db_url: str,
        db_engine: AsyncEngine,
    ) -> None:
        """Two orgs sharing a thread name keep fully isolated checkpoints."""
        org_a = await _create_org(db_engine)
        org_b = await _create_org(db_engine)
        thread_id = f"shared-thread-{uuid.uuid4().hex[:8]}"

        async def _interaction() -> None:
            async with _saver_pair(migrated_db_url, org_a, org_b) as (saver_a, saver_b):
                await saver_a.aput(
                    {
                        "configurable": {
                            "thread_id": thread_id,
                            "checkpoint_ns": "",
                            "checkpoint_id": "ckp-a-1",
                        }
                    },
                    _checkpoint("org-a"),
                    {"source": "isolation-test"},
                )
                await saver_b.aput(
                    {
                        "configurable": {
                            "thread_id": thread_id,
                            "checkpoint_ns": "",
                            "checkpoint_id": "ckp-b-1",
                        }
                    },
                    _checkpoint("org-b"),
                    {"source": "isolation-test"},
                )

                read_config = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}

                tuple_a = await saver_a.aget_tuple(read_config)
                tuple_b = await saver_b.aget_tuple(read_config)
                assert tuple_a is not None
                assert tuple_b is not None
                assert tuple_a.checkpoint["channel_values"]["owner"] == "org-a"
                assert tuple_b.checkpoint["channel_values"]["owner"] == "org-b"

                # Each saver sees exactly its own row — org A never sees org B's payload.
                list_a = [t async for t in saver_a.alist(read_config)]
                list_b = [t async for t in saver_b.alist(read_config)]
                assert len(list_a) == 1
                assert len(list_b) == 1
                assert list_a[0].checkpoint["channel_values"]["owner"] == "org-a"
                assert list_b[0].checkpoint["channel_values"]["owner"] == "org-b"

        _run_saver(_interaction())

    async def test_raw_db_count_confirms_org_scoping(
        self,
        migrated_db_url: str,
        db_engine: AsyncEngine,
    ) -> None:
        """The checkpoints row is physically stored under org A only."""
        org_a = await _create_org(db_engine)
        org_b = await _create_org(db_engine)
        thread_id = f"raw-count-{uuid.uuid4().hex[:8]}"

        async def _interaction() -> None:
            async with _saver_pair(migrated_db_url, org_a, org_b) as (saver_a, _saver_b):
                await saver_a.aput(
                    {
                        "configurable": {
                            "thread_id": thread_id,
                            "checkpoint_ns": "",
                            "checkpoint_id": "ckp-a",
                        }
                    },
                    _checkpoint("org-a"),
                    {"source": "isolation-test"},
                )

        _run_saver(_interaction())

        async with db_engine.connect() as conn:
            count_a = (
                await conn.execute(
                    text("SELECT COUNT(*) FROM checkpoints WHERE organisation_id = :oid"),
                    {"oid": str(org_a)},
                )
            ).scalar_one()
            count_b = (
                await conn.execute(
                    text("SELECT COUNT(*) FROM checkpoints WHERE organisation_id = :oid"),
                    {"oid": str(org_b)},
                )
            ).scalar_one()

        assert count_a == 1
        assert count_b == 0
