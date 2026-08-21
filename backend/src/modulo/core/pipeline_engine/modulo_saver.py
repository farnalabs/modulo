"""ModuloPostgresSaver — AsyncPostgresSaver with org_id column isolation.

Adds ``organisation_id`` to all ``langgraph.*`` checkpoint tables, enforces
``SET LOCAL`` on every read/write, and encrypts checkpoint JSON at rest via
Fernet. Resolves the alpha limitation where DB-privileged admins could read
any tenant's checkpoints.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager, suppress
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
    (
        "CREATE INDEX IF NOT EXISTS ix_checkpoint_writes_org"
        " ON checkpoint_writes (organisation_id, thread_id, checkpoint_id);"
    ),
    # created_at for the nightly checkpoint retention job (idempotent — safe to
    # re-run because setup() executes every migration on each startup).
    "ALTER TABLE checkpoints ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT now();",
    "ALTER TABLE checkpoint_blobs ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT now();",
    "ALTER TABLE checkpoint_writes ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT now();",
]


def _serialize_checkpoint(checkpoint: Checkpoint) -> str:
    return json.dumps(checkpoint, default=str, sort_keys=True)


def _deserialize_checkpoint(raw: str) -> Checkpoint:
    return cast("Checkpoint", json.loads(raw))


_log = logging.getLogger(__name__)

# Bound on a single reconnect attempt (monkeypatchable in tests). A reconnect
# that hangs past this budget must NOT surface as a bare timeout — the retry
# sites re-raise the ORIGINAL error instead (see aput / aput_writes).
_RECONNECT_TIMEOUT_SECONDS = 10.0


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
        conn_string: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(conn, **kwargs)
        self._org_id = organisation_id
        self._conn_string = conn_string
        # Serializes _reconnect() so concurrent callers (parallel aput on a
        # stale conn) perform ONE reconnection and the rest skip via the
        # stale double-check. Scoped to _reconnect() ONLY — never wraps a
        # cursor block (the base AsyncPostgresSaver._cursor already holds
        # self.lock; nesting would deadlock).
        self._reconnect_lock = asyncio.Lock()
        self._fernet = Fernet(fernet_key.encode()) if fernet_key else None
        self._fernet_old = Fernet(fernet_key_old.encode()) if fernet_key_old else None
        if fernet_key is None:
            _log.warning(
                "checkpoint.encryption_disabled",
                extra={"detail": "No Fernet key configured — checkpoint data stored in plaintext"},
            )

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
            return cast("Checkpoint", raw)
        if isinstance(raw, str) and raw.startswith('{"__encrypted__"'):
            try:
                wrapper = json.loads(raw)
                if wrapper.get("__encrypted__") and self._fernet is not None:
                    plain = self._decrypt_with_fallback(wrapper["data"].encode())
                    return _deserialize_checkpoint(plain.decode())
            except (json.JSONDecodeError, KeyError, InvalidToken):
                _log.exception("checkpoint.decrypt_failed")
                raise
        return _deserialize_checkpoint(raw)

    def _encrypt_blob(self, blob: bytes) -> bytes:
        if self._fernet is not None:
            return self._fernet.encrypt(blob)
        return blob

    def _decrypt_with_fallback(self, ciphertext: bytes) -> bytes:
        """Decrypt with primary key, falling back to old key on InvalidToken."""
        if self._fernet is None:
            return ciphertext
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
                raw = blob[2] or None
                if raw is not None and self._fernet is not None:
                    try:
                        raw = self._decrypt_with_fallback(raw)
                    except InvalidToken:
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
                    except InvalidToken:
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
                return CheckpointTuple(
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
                    (self._load_writes(value["pending_writes"]) if value.get("pending_writes") else None),
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
                yield CheckpointTuple(
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
                    (self._load_writes(value["pending_writes"]) if value.get("pending_writes") else None),
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
        """Write a checkpoint, retrying once on a connection-drop OperationalError.

        The write path (aput / aput_writes) reconnects + retries because a
        dropped connection there would fail the whole run. The READ paths
        (aget_tuple / alist / setup) are NOT retried: the ``_cursor`` stale
        pre-check covers pre-detected drops, and mid-flight read drops are out
        of scope (a read failure surfaces to the caller for LangGraph to
        decide).
        """
        thread_id = config["configurable"]["thread_id"]
        checkpoint_ns = config["configurable"].get("checkpoint_ns", "")
        checkpoint_id = config["configurable"].get("checkpoint_id")
        parent_checkpoint_id = config["configurable"].get("parent_checkpoint_id")
        parent_config = config["configurable"].get("parent_config")

        if parent_config:
            parent_checkpoint_id = parent_config["configurable"].get("checkpoint_id")

        if not checkpoint_id:
            nv = new_versions or {}
            channel = next(iter(nv.keys())) if nv else ""
            current = nv.get(channel) if nv else None
            checkpoint_id = self.get_next_version(current, channel)  # type: ignore[arg-type]

        encrypted_checkpoint = self._encrypt_checkpoint(checkpoint)

        # 2-attempt retry on a connection-drop OperationalError, mirroring
        # aput_writes: the drop typically fires at CURSOR ACQUIRE inside
        # _cursor, so the WHOLE ``async with self._cursor()`` block is wrapped,
        # not just cur.execute. Reconnect is bounded by
        # _RECONNECT_TIMEOUT_SECONDS; a timed-out reconnect re-raises the
        # ORIGINAL OperationalError (never a bare timeout).
        result = {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": checkpoint_ns,
                "checkpoint_id": checkpoint_id,
            }
        }
        for _attempt in range(2):
            try:
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
                return result
            except Exception as exc:
                is_conn_drop = type(exc).__name__ == "OperationalError"
                if _attempt == 0 and is_conn_drop and self._conn_string:
                    _log.warning(
                        "checkpoint.aput_retry",
                        extra={"error": str(exc)[:300]},
                    )
                    try:
                        await self._reconnect()
                    except TimeoutError:
                        # A reconnect that hung past the budget must not surface
                        # as a bare timeout replacing the real error — re-raise
                        # the ORIGINAL OperationalError.
                        raise exc from None
                    continue
                raise
        return result

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

        for _attempt in range(2):
            try:
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
                return
            except Exception as exc:
                is_conn_drop = type(exc).__name__ == "OperationalError"
                if _attempt == 0 and is_conn_drop:
                    _log.warning(
                        "checkpoint.aput_writes_retry",
                        extra={"error": str(exc)[:300]},
                    )
                    await self._reconnect()
                    continue
                raise

    # ------------------------------------------------------------------
    # Retry helpers — reconnect after a connection-drop OperationalError
    # ------------------------------------------------------------------

    async def _reconnect(self) -> None:
        """Re-establish the DB connection after a connection-drop OperationalError.

        Lock-serialized (``self._reconnect_lock``) with a stale double-check
        inside the lock: when two coroutines detect the same stale connection,
        the first performs the reconnect and the second observes the fresh
        connection and skips — one ``AsyncConnection.connect`` instead of two.
        The connect itself is bounded by :data:`_RECONNECT_TIMEOUT_SECONDS`;
        a timeout closes the (old) connection and re-raises ``TimeoutError`` so
        the retry call sites can fall back to the ORIGINAL error.
        """
        if not self._conn_string:
            return
        async with self._reconnect_lock:
            # Double-check inside the lock: a concurrent coroutine may have
            # already reconnected while we waited — skip rather than replace a
            # fresh connection / leak a second one.
            if not self._connection_is_stale():
                return
            with suppress(Exception):
                await self.conn.close()
            try:
                # Match the AsyncPostgresSaver.from_conn_string connection setup
                from psycopg import AsyncConnection
                from psycopg.rows import dict_row

                self.conn = await asyncio.wait_for(
                    AsyncConnection.connect(
                        self._conn_string,
                        autocommit=True,
                        prepare_threshold=0,
                        row_factory=dict_row,
                    ),
                    timeout=_RECONNECT_TIMEOUT_SECONDS,
                )
            except TimeoutError:
                # Clean up before re-raising: the old conn was already closed;
                # make sure nothing half-open survives.
                with suppress(Exception):
                    await self.conn.close()
                _log.exception("checkpoint.reconnect_timeout")
                raise

    def _connection_is_stale(self) -> bool:
        """True when the current DB connection can no longer be used."""
        conn = getattr(self, "conn", None)
        if conn is None:
            return False
        return bool(getattr(conn, "closed", False) or getattr(conn, "broken", False))

    @asynccontextmanager
    async def _cursor(self, *, pipeline: bool = False) -> AsyncIterator[Any]:
        """Create a DB cursor, transparently reconnecting a stale connection.

        Long-running pipeline runs can idle the DB connection until the server
        closes it; the inherited AsyncPostgresSaver._cursor then raises
        ``psycopg.OperationalError: the connection is closed`` on the next
        checkpoint write (aput/aget_tuple/alist/setup), failing the whole run.
        Detect the stale connection up front and reconnect before opening a
        cursor. Only reconnects when a conn_string is available; without one
        the base behavior (raise) is preserved. The reconnect is
        lock-serialized + timeout-bounded inside :meth:`_reconnect`, so
        concurrent callers dedupe and a hung reconnect is bounded.
        """
        if self._conn_string and self._connection_is_stale():
            _log.warning(
                "checkpoint.cursor_reconnect",
                extra={"detail": "stale DB connection detected; reconnecting before cursor"},
            )
            await self._reconnect()
        async with super()._cursor(pipeline=pipeline) as cur:
            yield cur

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
                conn_string=conn_string,
            )

    # ------------------------------------------------------------------
    # Sync overrides (delegate to async with org_id enforcement)
    # ------------------------------------------------------------------

    def get_tuple(self, config: dict[str, Any]) -> CheckpointTuple | None:  # type: ignore[override]
        return cast("CheckpointTuple | None", self._run_sync(self.aget_tuple(config)))

    def list(  # type: ignore[override]
        self,
        config: dict[str, Any],
        *,
        filter: dict[str, Any] | None = None,
        before: dict[str, Any] | None = None,
        limit: int | None = None,
    ) -> Sequence[CheckpointTuple]:
        return cast(
            "Sequence[CheckpointTuple]",
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
        return cast(
            "dict[str, Any]",
            self._run_sync(self.aput(config, checkpoint, metadata, new_versions=new_versions)),
        )

    def put_writes(  # type: ignore[override]
        self,
        config: dict[str, Any],
        writes: list[tuple[str, Any]],  # type: ignore[valid-type]
        task_id: str,
    ) -> None:
        self._run_sync(self.aput_writes(config, writes, task_id))

    @staticmethod
    def _run_sync(coro: Any) -> Any:
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
