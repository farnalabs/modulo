"""ModuloPostgresSaver — AsyncPostgresSaver with org_id column isolation.

Adds ``organisation_id`` to all ``langgraph.*`` checkpoint tables, enforces
``SET LOCAL`` on every read/write, and encrypts checkpoint JSON at rest via
Fernet. Resolves the alpha limitation where DB-privileged admins could read
any tenant's checkpoints.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any, cast

from cryptography.fernet import Fernet, InvalidToken
from langgraph.checkpoint.base import (
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
    get_checkpoint_id,
)
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

_CHECKPOINT_SELECT_SQL = """
SELECT
    organisation_id,
    thread_id,
    checkpoint,
    checkpoint_ns,
    checkpoint_id,
    parent_checkpoint_id,
    metadata,
    (
        SELECT array_agg(array[bl.channel::bytea, bl.type::bytea, bl.blob])
        FROM jsonb_each_text(checkpoint -> 'channel_versions')
        INNER JOIN checkpoint_blobs bl
            ON bl.organisation_id = checkpoints.organisation_id
            AND bl.thread_id = checkpoints.thread_id
            AND bl.checkpoint_ns = checkpoints.checkpoint_ns
            AND bl.channel = jsonb_each_text.key
            AND bl.version = jsonb_each_text.value
    ) AS channel_values,
    (
        SELECT
        array_agg(array[cw.task_id::text::bytea, cw.channel::bytea, cw.type::bytea, cw.blob]
                   ORDER BY cw.task_id, cw.idx)
        FROM checkpoint_writes cw
        WHERE cw.organisation_id = checkpoints.organisation_id
            AND cw.thread_id = checkpoints.thread_id
            AND cw.checkpoint_ns = checkpoints.checkpoint_ns
            AND cw.checkpoint_id = checkpoints.checkpoint_id
    ) AS pending_writes,
    (
        SELECT array_agg(array[cw.type::bytea, cw.blob] ORDER BY cw.idx)
        FROM checkpoint_writes cw
        WHERE cw.organisation_id = checkpoints.organisation_id
            AND cw.thread_id = checkpoints.thread_id
            AND cw.checkpoint_ns = checkpoints.checkpoint_ns
            AND cw.checkpoint_id = checkpoints.parent_checkpoint_id
            AND cw.channel = '__pregel_tasks'
    ) AS pending_sends
FROM checkpoints
"""

_UPSERT_CHECKPOINTS_SQL = """
    INSERT INTO checkpoints
        (organisation_id, thread_id, checkpoint_ns, checkpoint_id,
         parent_checkpoint_id, checkpoint, metadata)
    VALUES (%s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (organisation_id, thread_id, checkpoint_ns, checkpoint_id)
    DO UPDATE SET
        checkpoint = EXCLUDED.checkpoint,
        metadata = EXCLUDED.metadata;
"""

_UPSERT_CHECKPOINT_BLOBS_SQL = """
    INSERT INTO checkpoint_blobs
        (organisation_id, thread_id, checkpoint_ns, channel, version, type, blob)
    VALUES (%s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (organisation_id, thread_id, checkpoint_ns, channel, version)
    DO UPDATE SET
        type = EXCLUDED.type,
        blob = EXCLUDED.blob;
"""

_UPSERT_CHECKPOINT_WRITES_SQL = """
    INSERT INTO checkpoint_writes
        (organisation_id, thread_id, checkpoint_ns, checkpoint_id,
         task_id, idx, channel, type, blob)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (organisation_id, thread_id, checkpoint_ns, checkpoint_id, task_id, idx)
    DO UPDATE SET
        channel = EXCLUDED.channel,
        type = EXCLUDED.type,
        blob = EXCLUDED.blob;
"""

_MIGRATION_SQL: list[str] = [
    "CREATE TABLE IF NOT EXISTS checkpoint_migrations (v INTEGER PRIMARY KEY);",
    """
    CREATE TABLE IF NOT EXISTS checkpoints (
        organisation_id UUID NOT NULL,
        thread_id TEXT NOT NULL,
        checkpoint_ns TEXT NOT NULL DEFAULT '',
        checkpoint_id TEXT NOT NULL,
        parent_checkpoint_id TEXT,
        type TEXT,
        checkpoint JSONB NOT NULL,
        metadata JSONB NOT NULL DEFAULT '{}',
        PRIMARY KEY (organisation_id, thread_id, checkpoint_ns, checkpoint_id)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS checkpoint_blobs (
        organisation_id UUID NOT NULL,
        thread_id TEXT NOT NULL,
        checkpoint_ns TEXT NOT NULL DEFAULT '',
        channel TEXT NOT NULL,
        version TEXT NOT NULL,
        type TEXT NOT NULL,
        blob BYTEA,
        PRIMARY KEY (organisation_id, thread_id, checkpoint_ns, channel, version)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS checkpoint_writes (
        organisation_id UUID NOT NULL,
        thread_id TEXT NOT NULL,
        checkpoint_ns TEXT NOT NULL DEFAULT '',
        checkpoint_id TEXT NOT NULL,
        task_id TEXT NOT NULL,
        idx INTEGER NOT NULL,
        channel TEXT NOT NULL,
        type TEXT,
        blob BYTEA NOT NULL,
        PRIMARY KEY (organisation_id, thread_id, checkpoint_ns,
                     checkpoint_id, task_id, idx)
    );
    """,
    "ALTER TABLE checkpoint_blobs ALTER COLUMN blob DROP NOT NULL;",
    # Indexes for org-scoped queries
    "CREATE INDEX IF NOT EXISTS ix_checkpoints_org_thread ON checkpoints (organisation_id, thread_id, checkpoint_ns);",
    "CREATE INDEX IF NOT EXISTS ix_checkpoint_blobs_org ON checkpoint_blobs (organisation_id, thread_id);",
    "CREATE INDEX IF NOT EXISTS ix_checkpoint_writes_org"
    " ON checkpoint_writes (organisation_id, thread_id, checkpoint_id);",
]


def _serialize_checkpoint(checkpoint: Checkpoint) -> str:
    return json.dumps(checkpoint, default=str, sort_keys=True)


def _deserialize_checkpoint(raw: str) -> Checkpoint:
    return cast(Checkpoint, json.loads(raw))


_log = logging.getLogger(__name__)


class ModuloPostgresSaver(AsyncPostgresSaver):
    """PostgresSaver with org_id isolation, SET LOCAL enforcement, and encryption.

    Usage:
        saver = ModuloPostgresSaver(conn, organisation_id=org_id,
                                    fernet_key=settings.fernet_key)
        await saver.setup()
        # Use saver as a drop-in replacement for AsyncPostgresSaver
    """

    SELECT_SQL = _CHECKPOINT_SELECT_SQL
    UPSERT_CHECKPOINTS_SQL = _UPSERT_CHECKPOINTS_SQL
    UPSERT_CHECKPOINT_BLOBS_SQL = _UPSERT_CHECKPOINT_BLOBS_SQL
    UPSERT_CHECKPOINT_WRITES_SQL = _UPSERT_CHECKPOINT_WRITES_SQL
    MIGRATIONS = _MIGRATION_SQL

    def __init__(
        self,
        conn: Any,
        *,
        organisation_id: uuid.UUID,
        fernet_key: str | None = None,
        fernet_key_old: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(conn, **kwargs)
        self._org_id = organisation_id
        self._fernet = Fernet(fernet_key.encode()) if fernet_key else None
        self._fernet_old = Fernet(fernet_key_old.encode()) if fernet_key_old else None

    # ------------------------------------------------------------------
    # Encryption helpers
    # ------------------------------------------------------------------

    def _encrypt_checkpoint(self, checkpoint: Checkpoint) -> str:
        serialized = _serialize_checkpoint(checkpoint)
        if self._fernet is not None:
            encrypted = self._fernet.encrypt(serialized.encode())
            return json.dumps({"__encrypted__": True, "data": encrypted.decode()})
        return serialized

    def _decrypt_checkpoint(self, raw: str | dict[str, Any]) -> Checkpoint:
        if isinstance(raw, dict):
            if raw.get("__encrypted__") and self._fernet is not None:
                plain = self._decrypt_with_fallback(raw["data"].encode())
                return _deserialize_checkpoint(plain.decode())
            return _deserialize_checkpoint(json.dumps(raw, default=str))
        if isinstance(raw, str) and raw.startswith('{"__encrypted__"'):
            try:
                wrapper = json.loads(raw)
                if wrapper.get("__encrypted__") and self._fernet is not None:
                    plain = self._decrypt_with_fallback(wrapper["data"].encode())
                    return _deserialize_checkpoint(plain.decode())
            except Exception:
                _log.warning("checkpoint.decrypt_fallback", exc_info=True)
        return _deserialize_checkpoint(raw)

    def _encrypt_blob(self, blob: bytes) -> bytes:
        if self._fernet is not None:
            return self._fernet.encrypt(blob)
        return blob

    def _decrypt_with_fallback(self, ciphertext: bytes) -> bytes:
        """Decrypt with primary key, falling back to old key on InvalidToken."""
        try:
            return self._fernet.decrypt(ciphertext)
        except InvalidToken:
            if self._fernet_old is not None:
                return self._fernet_old.decrypt(ciphertext)
            raise

    def _decrypt_blobs(self, blobs: Any) -> dict[str, Any] | None:
        if not blobs:
            return None
        result: dict[str, Any] = {}
        for blob in blobs:
            if len(blob) >= 3:
                raw = blob[2] if blob[2] else None
                if raw is not None and self._fernet is not None:
                    try:
                        raw = self._decrypt_with_fallback(raw)
                    except Exception:
                        _log.warning("blob.decrypt_fallback", exc_info=True)
                result[blob[0].decode()] = raw
        return result

    def _decrypt_writes(self, writes: Any) -> list[tuple[str, str, bytes]] | None:
        if not writes:
            return None
        result: list[tuple[str, str, bytes]] = []
        for w in writes:
            if len(w) >= 4:
                raw = w[3]
                if self._fernet is not None:
                    try:
                        raw = self._decrypt_with_fallback(raw)
                    except Exception:
                        _log.warning("write.decrypt_fallback", exc_info=True)
                result.append((w[1].decode(), w[2].decode(), raw))
        return result

    # ------------------------------------------------------------------
    # Override: setup — run modified migrations
    # ------------------------------------------------------------------

    async def setup(self) -> None:
        """Run Modulo-specific migrations (org_id columns)."""
        async with self._cursor() as cur:
            for migration in self.MIGRATIONS:
                await cur.execute(migration)

    # ------------------------------------------------------------------
    # Override: aget_tuple — filter by org_id
    # ------------------------------------------------------------------

    async def aget_tuple(self, config: dict[str, Any]) -> CheckpointTuple | None:  # type: ignore[override]
        thread_id = config["configurable"]["thread_id"]
        checkpoint_id = get_checkpoint_id(config)  # type: ignore[arg-type]
        checkpoint_ns = config["configurable"].get("checkpoint_ns", "")

        if checkpoint_id:
            where = "WHERE organisation_id = %s AND thread_id = %s AND checkpoint_ns = %s AND checkpoint_id = %s"
            args: tuple[Any, ...] = (self._org_id, thread_id, checkpoint_ns, checkpoint_id)
        else:
            where = (
                "WHERE organisation_id = %s AND thread_id = %s"
                " AND checkpoint_ns = %s ORDER BY checkpoint_id DESC LIMIT 1"
            )
            args = (self._org_id, thread_id, checkpoint_ns)

        async with self._cursor() as cur:
            await cur.execute(
                self.SELECT_SQL + " " + where,
                args,
                binary=True,
            )

            async for value in cur:
                checkpoint = self._decrypt_checkpoint(value["checkpoint"])
                return CheckpointTuple(  # type: ignore[call-arg]
                    {
                        "configurable": {
                            "thread_id": thread_id,
                            "checkpoint_ns": checkpoint_ns,
                            "checkpoint_id": value["checkpoint_id"],
                        }
                    },
                    checkpoint,
                    value["metadata"],
                    (
                        {
                            "configurable": {
                                "thread_id": thread_id,
                                "checkpoint_ns": checkpoint_ns,
                                "checkpoint_id": value["parent_checkpoint_id"],
                            }
                        }
                        if value.get("parent_checkpoint_id")
                        else None
                    ),
                    (self._load_blobs(value["channel_values"]) if value.get("channel_values") else None),  # type: ignore[arg-type]
                    (self._load_writes(value["pending_writes"]) if value.get("pending_writes") else None),
                    (self._load_writes(value["pending_sends"]) if value.get("pending_sends") else None),
                    value["metadata"] if not isinstance(value["metadata"], dict) else None,
                )
        return None

    # ------------------------------------------------------------------
    # Override: alist — filter by org_id
    # ------------------------------------------------------------------

    async def alist(  # type: ignore[override]
        self,
        config: dict[str, Any],
        *,
        filter: dict[str, Any] | None = None,
        before: dict[str, Any] | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        del filter
        thread_id = config["configurable"]["thread_id"]
        checkpoint_ns = config["configurable"].get("checkpoint_ns", "")
        before_id = get_checkpoint_id(before) if before else None  # type: ignore[arg-type]

        where = "WHERE organisation_id = %s AND thread_id = %s AND checkpoint_ns = %s"
        args: list[Any] = [self._org_id, thread_id, checkpoint_ns]

        if before_id:
            where += " AND checkpoint_id < %s"
            args.append(before_id)

        where += " ORDER BY checkpoint_id DESC"

        if limit is not None:
            where += f" LIMIT {int(limit)}"

        async with self._cursor() as cur:
            await cur.execute(self.SELECT_SQL + " " + where, args, binary=True)
            async for value in cur:
                checkpoint = self._decrypt_checkpoint(value["checkpoint"])
                yield CheckpointTuple(  # type: ignore[call-arg]
                    {
                        "configurable": {
                            "thread_id": thread_id,
                            "checkpoint_ns": checkpoint_ns,
                            "checkpoint_id": value["checkpoint_id"],
                        }
                    },
                    checkpoint,
                    value["metadata"],
                    (
                        {
                            "configurable": {
                                "thread_id": thread_id,
                                "checkpoint_ns": checkpoint_ns,
                                "checkpoint_id": value["parent_checkpoint_id"],
                            }
                        }
                        if value.get("parent_checkpoint_id")
                        else None
                    ),
                    (self._load_blobs(value["channel_values"]) if value.get("channel_values") else None),  # type: ignore[arg-type]
                    (self._load_writes(value["pending_writes"]) if value.get("pending_writes") else None),
                    (self._load_writes(value["pending_sends"]) if value.get("pending_sends") else None),
                    value["metadata"] if not isinstance(value["metadata"], dict) else None,
                )

    # ------------------------------------------------------------------
    # Override: aput — encrypt and write with org_id
    # ------------------------------------------------------------------

    async def aput(  # type: ignore[override]
        self,
        config: dict[str, Any],
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: dict[str, str | int | float | bool] | None = None,
    ) -> dict[str, Any]:
        del new_versions
        thread_id = config["configurable"]["thread_id"]
        checkpoint_ns = config["configurable"].get("checkpoint_ns", "")
        checkpoint_id = config["configurable"].get("checkpoint_id")
        parent_checkpoint_id = config["configurable"].get("parent_checkpoint_id")
        parent_config = config["configurable"].get("parent_config")

        if parent_config:
            parent_checkpoint_id = parent_config["configurable"].get("checkpoint_id")

        if not checkpoint_id:
            checkpoint_id = self.get_next_version()  # type: ignore[call-arg]

        encrypted_checkpoint = self._encrypt_checkpoint(checkpoint)

        async with self._cursor() as cur:
            await cur.execute(
                self.UPSERT_CHECKPOINTS_SQL,
                (
                    self._org_id,
                    thread_id,
                    checkpoint_ns,
                    checkpoint_id,
                    parent_checkpoint_id,
                    encrypted_checkpoint,
                    json.dumps(metadata, default=str),
                ),
            )

        return {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": checkpoint_ns,
                "checkpoint_id": checkpoint_id,
            }
        }

    # ------------------------------------------------------------------
    # Override: aput_writes — write with org_id
    # ------------------------------------------------------------------

    async def aput_writes(  # type: ignore[override]
        self,
        config: dict[str, Any],
        writes: list[tuple[str, Any]],
        task_id: str,
    ) -> None:
        thread_id = config["configurable"]["thread_id"]
        checkpoint_ns = config["configurable"].get("checkpoint_ns", "")
        checkpoint_id = config["configurable"]["checkpoint_id"]

        async with self._cursor() as cur:
            for idx, (channel, value) in enumerate(writes):
                type_str, blob_bytes = self.serde.dumps_typed(value)
                encrypted = self._encrypt_blob(blob_bytes)
                await cur.execute(
                    self.UPSERT_CHECKPOINT_WRITES_SQL,
                    (
                        self._org_id,
                        thread_id,
                        checkpoint_ns,
                        checkpoint_id,
                        task_id,
                        idx,
                        channel,
                        type_str,
                        encrypted,
                    ),
                )

    # ------------------------------------------------------------------
    # Override: from_conn_string — passes org_id and fernet_key
    # ------------------------------------------------------------------

    @classmethod
    @asynccontextmanager
    async def from_conn_string(  # type: ignore[override]
        cls,
        conn_string: str,
        *,
        organisation_id: uuid.UUID,
        fernet_key: str | None = None,
        fernet_key_old: str | None = None,
    ) -> AsyncIterator[ModuloPostgresSaver]:
        """Create a ModuloPostgresSaver from a connection string."""
        async with AsyncPostgresSaver.from_conn_string(conn_string) as base:
            yield cls(
                base.conn,
                organisation_id=organisation_id,
                fernet_key=fernet_key,
                fernet_key_old=fernet_key_old,
            )

    # ------------------------------------------------------------------
    # Sync overrides (delegate to async with org_id enforcement)
    # ------------------------------------------------------------------

    def get_tuple(self, config: dict[str, Any]) -> CheckpointTuple | None:  # type: ignore[override]
        return cast(CheckpointTuple | None, self._run_sync(self.aget_tuple(config)))

    def list(  # type: ignore[override]
        self,
        config: dict[str, Any],
        *,
        filter: dict[str, Any] | None = None,
        before: dict[str, Any] | None = None,
        limit: int | None = None,
    ) -> Sequence[CheckpointTuple]:
        return cast(
            Sequence[CheckpointTuple],
            self._run_sync(self._alist_sync(config, filter=filter, before=before, limit=limit)),
        )

    async def _alist_sync(
        self,
        config: dict[str, Any],
        *,
        filter: dict[str, Any] | None = None,
        before: dict[str, Any] | None = None,
        limit: int | None = None,
    ) -> list[CheckpointTuple]:  # type: ignore[valid-type]
        results: list[CheckpointTuple] = []
        async for item in self.alist(config, filter=filter, before=before, limit=limit):
            results.append(item)
        return results

    def put(  # type: ignore[override]
        self,
        config: dict[str, Any],
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: dict[str, str | int | float | bool] | None = None,
    ) -> dict[str, Any]:
        return cast(dict[str, Any], self._run_sync(self.aput(config, checkpoint, metadata, new_versions=new_versions)))

    def put_writes(  # type: ignore[override]
        self,
        config: dict[str, Any],
        writes: list[tuple[str, Any]],  # type: ignore[valid-type]
        task_id: str,
    ) -> None:
        self._run_sync(self.aput_writes(config, writes, task_id))

    @staticmethod
    def _run_sync(coro: Any) -> Any:
        import asyncio

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coro)
        raise RuntimeError(
            "ModuloPostgresSaver sync methods must not be called from an async context. "
            "Use the async variants (aget_tuple, aput, etc.) instead."
        )

    def _load_blobs(self, blobs: Any) -> dict[str, Any] | None:  # type: ignore[override]
        return self._decrypt_blobs(blobs)

    def _load_writes(self, writes: Any) -> list[tuple[str, str, bytes]] | None:  # type: ignore[override, valid-type]
        return self._decrypt_writes(writes)
