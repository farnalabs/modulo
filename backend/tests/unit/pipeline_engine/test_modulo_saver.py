"""Unit tests for ModuloPostgresSaver â€” org isolation, encryption, SQL."""

import asyncio
import json
import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cryptography.fernet import Fernet, InvalidToken

from modulo.core.pipeline_engine.modulo_saver import ModuloPostgresSaver


class _AsyncIter:
    def __init__(self, items):
        self._iter = iter(items)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._iter)
        except StopIteration:
            raise StopAsyncIteration from None


_FERNET_KEY = Fernet.generate_key().decode()
_ORG_ID = uuid.uuid4()


@pytest.fixture
def mock_conn():
    return MagicMock()


async def _make_saver(mock_conn, *, organisation_id=_ORG_ID, fernet_key=_FERNET_KEY):
    """Construct ``ModuloPostgresSaver`` inside the running event loop.

    ``AsyncPostgresSaver.__init__`` calls ``asyncio.get_running_loop()``, so
    construction must happen inside a running loop. Callers run as ``async
    def`` tests in pytest-asyncio's stable session loop; the methods under
    test here (``_encrypt_checkpoint``, ``_decrypt_checkpoint``,
    ``_encrypt_blob``, ``_decrypt_blobs``, ``_decrypt_writes``) are
    loop-independent.
    """

    return ModuloPostgresSaver(mock_conn, organisation_id=organisation_id, fernet_key=fernet_key)


class TestInit:
    async def test_stores_org_id(self, mock_conn):
        saver = await _make_saver(mock_conn)
        assert saver._org_id == _ORG_ID

    async def test_stores_fernet_key(self, mock_conn):
        saver = await _make_saver(mock_conn)
        assert saver._fernet is not None

    async def test_no_fernet_when_not_given(self, mock_conn):
        saver = await _make_saver(mock_conn, fernet_key=None)
        assert saver._fernet is None


class TestEncryption:
    async def test_encrypt_decrypt_roundtrip(self, mock_conn):
        saver = await _make_saver(mock_conn)
        checkpoint = {"id": "test", "channel_values": {"key": "value"}}
        encrypted = saver._encrypt_checkpoint(checkpoint)
        parsed = json.loads(encrypted)
        assert parsed["__encrypted__"] is True
        assert isinstance(parsed["data"], str)

        decrypted = saver._decrypt_checkpoint(encrypted)
        assert decrypted["id"] == "test"
        assert decrypted["channel_values"]["key"] == "value"

    async def test_no_encryption_when_not_configured(self, mock_conn):
        saver = await _make_saver(mock_conn, fernet_key=None)
        checkpoint = {"id": "plain"}
        serialized = saver._encrypt_checkpoint(checkpoint)
        assert '"__encrypted__"' not in serialized
        decrypted = saver._decrypt_checkpoint(serialized)
        assert decrypted["id"] == "plain"

    async def test_decrypt_plain_dict(self, mock_conn):
        saver = await _make_saver(mock_conn)
        result = saver._decrypt_checkpoint({"id": "plain"})
        assert result["id"] == "plain"

    async def test_encrypt_decrypt_with_different_saver(self, mock_conn):
        checkpoint = {"secret": "data"}
        saver1 = await _make_saver(mock_conn)
        encrypted = saver1._encrypt_checkpoint(checkpoint)

        saver2 = await _make_saver(mock_conn)
        result = saver2._decrypt_checkpoint(encrypted)
        assert result["secret"] == "data"


class TestSetup:
    async def test_setup_runs_migrations(self, mock_conn):
        saver = ModuloPostgresSaver(mock_conn, organisation_id=_ORG_ID, fernet_key=_FERNET_KEY)
        cursor = AsyncMock()
        cursor.__aenter__ = AsyncMock(return_value=cursor)
        cursor.__aexit__ = AsyncMock(return_value=False)
        saver._cursor = MagicMock(return_value=cursor)

        await saver.setup()

        assert cursor.execute.call_count == len(saver.MIGRATIONS)

    async def test_setup_creates_checkpoints_table(self, mock_conn):
        saver = ModuloPostgresSaver(mock_conn, organisation_id=_ORG_ID, fernet_key=_FERNET_KEY)
        cursor = AsyncMock()
        cursor.__aenter__ = AsyncMock(return_value=cursor)
        cursor.__aexit__ = AsyncMock(return_value=False)
        saver._cursor = MagicMock(return_value=cursor)

        await saver.setup()

        calls = [c[0][0] for c in cursor.execute.call_args_list]
        create_checkpoints = [c for c in calls if "CREATE TABLE" in c and "checkpoints" in c]
        assert len(create_checkpoints) > 0
        assert "organisation_id UUID NOT NULL" in create_checkpoints[0]


class TestAgetTuple:
    async def test_get_tuple_filters_by_org_id(self, mock_conn):
        saver = ModuloPostgresSaver(mock_conn, organisation_id=_ORG_ID, fernet_key=_FERNET_KEY)
        cursor = AsyncMock()
        cursor.__aenter__ = AsyncMock(return_value=cursor)
        cursor.__aexit__ = AsyncMock(return_value=False)
        cursor.__aiter__ = MagicMock(return_value=_AsyncIter([]))
        saver._cursor = MagicMock(return_value=cursor)

        config = {"configurable": {"thread_id": "thread-1", "checkpoint_ns": ""}}
        result = await saver.aget_tuple(config)

        assert result is None
        executed_sql = cursor.execute.call_args[0][0]
        assert "organisation_id" in executed_sql

    async def test_get_tuple_with_checkpoint_id(self, mock_conn):
        saver = ModuloPostgresSaver(mock_conn, organisation_id=_ORG_ID, fernet_key=_FERNET_KEY)
        cursor = AsyncMock()
        cursor.__aenter__ = AsyncMock(return_value=cursor)
        cursor.__aexit__ = AsyncMock(return_value=False)
        cursor.__aiter__ = MagicMock(return_value=_AsyncIter([]))
        saver._cursor = MagicMock(return_value=cursor)

        config = {
            "configurable": {
                "thread_id": "thread-1",
                "checkpoint_ns": "",
                "checkpoint_id": "ckp-123",
            }
        }
        await saver.aget_tuple(config)

        executed_sql = cursor.execute.call_args[0][0]
        assert "checkpoint_id" in executed_sql


class TestAput:
    async def test_put_includes_org_id_in_sql(self, mock_conn):
        saver = ModuloPostgresSaver(mock_conn, organisation_id=_ORG_ID, fernet_key=_FERNET_KEY)
        cursor = AsyncMock()
        cursor.__aenter__ = AsyncMock(return_value=cursor)
        cursor.__aexit__ = AsyncMock(return_value=False)
        saver._cursor = MagicMock(return_value=cursor)
        saver.get_next_version = MagicMock(return_value="new-ckp-id")

        config = {"configurable": {"thread_id": "thread-1", "checkpoint_ns": ""}}
        checkpoint = {"channel_values": {}}
        metadata = {"source": "test"}

        result = await saver.aput(config, checkpoint, metadata)

        assert result["configurable"]["checkpoint_id"] == "new-ckp-id"
        executed_sql = cursor.execute.call_args[0][0]
        assert "organisation_id" in executed_sql
        executed_args = cursor.execute.call_args[0][1]
        assert executed_args[0] == _ORG_ID
        assert executed_args[1] == "thread-1"

    async def test_put_encrypts_checkpoint(self, mock_conn):
        saver = ModuloPostgresSaver(mock_conn, organisation_id=_ORG_ID, fernet_key=_FERNET_KEY)
        cursor = AsyncMock()
        cursor.__aenter__ = AsyncMock(return_value=cursor)
        cursor.__aexit__ = AsyncMock(return_value=False)
        saver._cursor = MagicMock(return_value=cursor)
        saver.get_next_version = MagicMock(return_value="ckp-id")

        config = {"configurable": {"thread_id": "t1", "checkpoint_ns": ""}}
        checkpoint = {"channel_values": {"secret": "sensitive-data"}}
        metadata = {}

        await saver.aput(config, checkpoint, metadata)

        executed_args = cursor.execute.call_args[0][1]
        assert executed_args[5] is not None
        parsed = json.loads(executed_args[5])
        assert parsed["__encrypted__"] is True


class TestSQLConstants:
    def test_select_includes_org_id(self):
        assert "organisation_id" in ModuloPostgresSaver.SELECT_SQL

    def test_upsert_checkpoints_includes_org_id(self):
        assert "organisation_id" in ModuloPostgresSaver.UPSERT_CHECKPOINTS_SQL

    def test_upsert_blobs_includes_org_id(self):
        assert "organisation_id" in ModuloPostgresSaver.UPSERT_CHECKPOINT_BLOBS_SQL

    def test_upsert_writes_includes_org_id(self):
        assert "organisation_id" in ModuloPostgresSaver.UPSERT_CHECKPOINT_WRITES_SQL

    def test_migrations_create_org_id_columns(self):
        for migration in ModuloPostgresSaver.MIGRATIONS:
            if "CREATE TABLE" in migration and "checkpoint_migrations" not in migration:
                assert "organisation_id" in migration, f"Missing org_id in: {migration[:60]}"

    def test_primary_keys_include_org_id(self):
        for migration in ModuloPostgresSaver.MIGRATIONS:
            if "PRIMARY KEY" in migration and "checkpoint_migrations" not in migration:
                assert "organisation_id" in migration

    def test_migrations_add_created_at_for_retention(self):
        migration_sql = "\n".join(ModuloPostgresSaver.MIGRATIONS)
        for table in ("checkpoints", "checkpoint_blobs", "checkpoint_writes"):
            assert f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS created_at" in migration_sql


class TestBlobEncryption:
    """Tests for blob-level encryption (_encrypt_blob, _decrypt_blobs, _decrypt_writes)."""

    async def test_blob_encryption_roundtrip(self, mock_conn):
        saver = await _make_saver(mock_conn)
        original = b"hello world sensitive data"
        encrypted = saver._encrypt_blob(original)
        assert isinstance(encrypted, bytes)
        blobs = [[b"ch1", b"bytes", encrypted]]
        result = saver._decrypt_blobs(blobs)
        assert result == {"ch1": original}

    async def test_encrypted_blob_starts_with_fernet_prefix(self, mock_conn):
        saver = await _make_saver(mock_conn)
        encrypted = saver._encrypt_blob(b"anything")
        assert encrypted.startswith(b"gAAAAA")

    async def test_decrypt_blobs_returns_original_channel_values(self, mock_conn):
        saver = await _make_saver(mock_conn)
        original = b'{"key": "value"}'
        encrypted = saver._encrypt_blob(original)
        blobs = [[b"channel1", b"json", encrypted]]
        result = saver._decrypt_blobs(blobs)
        assert result == {"channel1": original}

    async def test_decrypt_writes_returns_original_writes(self, mock_conn):
        saver = await _make_saver(mock_conn)
        original = b"write_data"
        encrypted = saver._encrypt_blob(original)
        writes = [[b"task1", b"channel1", b"type1", encrypted]]
        result = saver._decrypt_writes(writes)
        assert result == [("channel1", "type1", original)]

    async def test_aput_writes_encrypts_blob(self, mock_conn):
        saver = ModuloPostgresSaver(mock_conn, organisation_id=_ORG_ID, fernet_key=_FERNET_KEY)
        cursor = AsyncMock()
        cursor.__aenter__ = AsyncMock(return_value=cursor)
        cursor.__aexit__ = AsyncMock(return_value=False)
        saver._cursor = MagicMock(return_value=cursor)
        saver.serde = MagicMock()
        saver.serde.dumps_typed = MagicMock(return_value=("json", b"sensitive-data"))

        config = {"configurable": {"thread_id": "t1", "checkpoint_ns": "", "checkpoint_id": "ckp-1"}}
        writes = [("channel1", {"data": "secret"})]
        task_id = "task-1"

        await saver.aput_writes(config, writes, task_id)

        executed_args = cursor.execute.call_args[0][1]
        blob_arg = executed_args[8]
        assert blob_arg.startswith(b"gAAAAA")

    async def test_no_encryption_when_fernet_key_none(self, mock_conn):
        saver = await _make_saver(mock_conn, fernet_key=None)
        original = b"plaintext data"
        result = saver._encrypt_blob(original)
        assert result is original

    async def test_decrypt_blobs_plaintext_fallback(self, mock_conn):
        saver = await _make_saver(mock_conn)
        plaintext = b'{"key": "value"}'
        blobs = [[b"ch1", b"json", plaintext]]
        result = saver._decrypt_blobs(blobs)
        assert result == {"ch1": plaintext}

    async def test_decrypt_writes_plaintext_fallback(self, mock_conn):
        saver = await _make_saver(mock_conn)
        plaintext = b"plain_write_data"
        writes = [[b"task1", b"ch1", b"type1", plaintext]]
        result = saver._decrypt_writes(writes)
        assert result == [("ch1", "type1", plaintext)]

    async def test_decrypt_blobs_none(self, mock_conn):
        saver = await _make_saver(mock_conn)
        assert saver._decrypt_blobs(None) is None
        assert saver._decrypt_blobs([]) is None

    async def test_no_decryption_when_saver_has_no_key(self, mock_conn):
        saver = await _make_saver(mock_conn, fernet_key=None)
        encrypted = Fernet(_FERNET_KEY.encode()).encrypt(b"secret-data")
        blobs = [[b"ch1", b"bytes", encrypted]]
        result = saver._decrypt_blobs(blobs)
        assert result == {"ch1": encrypted}


class TestIsEncrypted:
    """Tests for _is_encrypted function from scripts/migrate-checkpoint-blobs.py."""

    @staticmethod
    def _is_encrypted(blob: bytes | None) -> bool:
        if blob is None:
            return True
        try:
            return blob[:6] == b"gAAAAA"
        except (TypeError, IndexError):
            return False

    def test_encrypted_blob_detected(self):
        assert self._is_encrypted(b"gAAAAAabc123")

    def test_plaintext_blob_detected(self):
        assert not self._is_encrypted(b"plaintext data")

    def test_none_blob_returns_true(self):
        assert self._is_encrypted(None)

    def test_empty_blob_returns_false(self):
        assert not self._is_encrypted(b"")

    def test_short_blob_less_than_6_bytes(self):
        assert not self._is_encrypted(b"short")


class OperationalError(Exception):
    """A stand-in for psycopg.OperationalError (matched by class name in aput)."""


class TestDecryptEdgePaths:
    async def test_decrypt_dict_with_encrypted_flag(self, mock_conn):
        saver = await _make_saver(mock_conn)
        encrypted = saver._encrypt_checkpoint({"secret": 1})
        wrapper = json.loads(encrypted)
        # Feed the dict form (as read from a JSONB column) straight to decrypt.
        result = saver._decrypt_checkpoint(wrapper)
        assert result == {"secret": 1}

    async def test_decrypt_dict_with_encrypted_flag_no_fernet(self, mock_conn):
        saver = await _make_saver(mock_conn, fernet_key=None)
        # Without a key the dict is returned as-is (no decryption possible).
        result = saver._decrypt_checkpoint({"__encrypted__": True, "data": "garbage"})
        assert result == {"__encrypted__": True, "data": "garbage"}

    async def test_decrypt_malformed_encrypted_json_raises(self, mock_conn):
        saver = await _make_saver(mock_conn)
        with pytest.raises(json.JSONDecodeError):
            saver._decrypt_checkpoint('{"__encrypted__": true, "data": "unterminated')

    async def test_decrypt_without_fernet_returns_ciphertext(self, mock_conn):
        saver = await _make_saver(mock_conn, fernet_key=None)
        raw = b"not-encrypted-bytes"
        assert saver._decrypt_with_fallback(raw) == raw

    async def test_decrypt_old_key_fallback(self, mock_conn):
        old_key = Fernet.generate_key().decode()
        new_key = Fernet.generate_key().decode()
        old_fernet = Fernet(old_key.encode())
        saver = await _make_saver(mock_conn, fernet_key=new_key)
        saver._fernet_old = Fernet(old_key.encode())
        ciphertext = old_fernet.encrypt(b"legacy-data")
        assert saver._decrypt_with_fallback(ciphertext) == b"legacy-data"

    async def test_decrypt_fallback_reraises_when_no_old_key(self, mock_conn):
        saver = await _make_saver(mock_conn)
        other = Fernet(Fernet.generate_key())
        ciphertext = other.encrypt(b"x")
        with pytest.raises(InvalidToken):
            saver._decrypt_with_fallback(ciphertext)

    async def test_decrypt_blob_fallback_warning(self, mock_conn):
        saver = await _make_saver(mock_conn)
        other = Fernet(Fernet.generate_key())
        blob = [[b"ch", b"bytes", other.encrypt(b"x")]]
        result = saver._decrypt_blobs(blob)
        # Decryption failed, warning logged, raw ciphertext preserved.
        assert list(result.keys()) == ["ch"]

    async def test_decrypt_writes_none_returns_none(self, mock_conn):
        saver = await _make_saver(mock_conn)
        assert saver._decrypt_writes(None) is None
        assert saver._decrypt_writes([]) is None

    async def test_decrypt_writes_short_entry_skipped(self, mock_conn):
        saver = await _make_saver(mock_conn)
        writes = [[b"task", b"ch"]]  # len < 4 â€” skipped
        assert not saver._decrypt_writes(writes)


class TestAgetTupleRowPath:
    async def test_get_tuple_returns_checkpoint(self, mock_conn):
        saver = ModuloPostgresSaver(mock_conn, organisation_id=_ORG_ID, fernet_key=_FERNET_KEY)
        cursor = AsyncMock()
        cursor.__aenter__ = AsyncMock(return_value=cursor)
        cursor.__aexit__ = AsyncMock(return_value=False)
        row = {
            "checkpoint_id": "ckp-9",
            "parent_checkpoint_id": None,
            "checkpoint": saver._encrypt_checkpoint({"channel_values": {"a": 1}}),
            "metadata": {"source": "test"},
            "pending_writes": None,
            "pending_sends": None,
        }
        cursor.__aiter__ = MagicMock(return_value=_AsyncIter([row]))
        saver._cursor = MagicMock(return_value=cursor)

        config = {"configurable": {"thread_id": "t1", "checkpoint_ns": "", "checkpoint_id": "ckp-9"}}
        result = await saver.aget_tuple(config)
        assert result is not None
        assert result.config["configurable"]["checkpoint_id"] == "ckp-9"
        assert result.checkpoint["channel_values"] == {"a": 1}
        assert result.metadata == {"source": "test"}
        assert result.parent_config is None

    async def test_get_tuple_returns_checkpoint_with_parent(self, mock_conn):
        saver = ModuloPostgresSaver(mock_conn, organisation_id=_ORG_ID, fernet_key=_FERNET_KEY)
        cursor = AsyncMock()
        cursor.__aenter__ = AsyncMock(return_value=cursor)
        cursor.__aexit__ = AsyncMock(return_value=False)
        row = {
            "checkpoint_id": "ckp-10",
            "parent_checkpoint_id": "ckp-9",
            "checkpoint": saver._encrypt_checkpoint({"channel_values": {}}),
            "metadata": {},
            "pending_writes": None,
            "pending_sends": None,
        }
        cursor.__aiter__ = MagicMock(return_value=_AsyncIter([row]))
        saver._cursor = MagicMock(return_value=cursor)

        config = {"configurable": {"thread_id": "t1", "checkpoint_ns": ""}}
        result = await saver.aget_tuple(config)
        assert result is not None
        assert result.parent_config["configurable"]["checkpoint_id"] == "ckp-9"


class TestAlist:
    async def test_alist_iterates_rows(self, mock_conn):
        saver = ModuloPostgresSaver(mock_conn, organisation_id=_ORG_ID, fernet_key=_FERNET_KEY)
        cursor = AsyncMock()
        cursor.__aenter__ = AsyncMock(return_value=cursor)
        cursor.__aexit__ = AsyncMock(return_value=False)
        rows = [
            {
                "checkpoint_id": "ckp-1",
                "parent_checkpoint_id": None,
                "checkpoint": saver._encrypt_checkpoint({"channel_values": {"a": 1}}),
                "metadata": {},
                "pending_writes": None,
                "pending_sends": None,
            },
            {
                "checkpoint_id": "ckp-0",
                "parent_checkpoint_id": None,
                "checkpoint": saver._encrypt_checkpoint({"channel_values": {"a": 2}}),
                "metadata": {},
                "pending_writes": None,
                "pending_sends": None,
            },
        ]
        cursor.__aiter__ = MagicMock(return_value=_AsyncIter(rows))
        saver._cursor = MagicMock(return_value=cursor)

        config = {"configurable": {"thread_id": "t1", "checkpoint_ns": ""}}
        items = [item async for item in saver.alist(config)]
        assert len(items) == 2
        assert [i.config["configurable"]["checkpoint_id"] for i in items] == ["ckp-1", "ckp-0"]

    async def test_alist_with_before_and_limit(self, mock_conn):
        saver = ModuloPostgresSaver(mock_conn, organisation_id=_ORG_ID, fernet_key=_FERNET_KEY)
        cursor = AsyncMock()
        cursor.__aenter__ = AsyncMock(return_value=cursor)
        cursor.__aexit__ = AsyncMock(return_value=False)
        cursor.__aiter__ = MagicMock(return_value=_AsyncIter([]))
        saver._cursor = MagicMock(return_value=cursor)

        config = {"configurable": {"thread_id": "t1", "checkpoint_ns": ""}}
        items = [
            item async for item in saver.alist(config, before={"configurable": {"checkpoint_id": "ckp-5"}}, limit=3)
        ]
        assert items == []
        sql = cursor.execute.call_args[0][0]
        assert "checkpoint_id <" in sql
        assert "LIMIT 3" in sql


class TestAputExtraPaths:
    async def test_aput_with_parent_config(self, mock_conn):
        saver = ModuloPostgresSaver(mock_conn, organisation_id=_ORG_ID, fernet_key=_FERNET_KEY)
        cursor = AsyncMock()
        cursor.__aenter__ = AsyncMock(return_value=cursor)
        cursor.__aexit__ = AsyncMock(return_value=False)
        saver._cursor = MagicMock(return_value=cursor)
        saver.get_next_version = MagicMock(return_value="ckp-id")

        config = {
            "configurable": {
                "thread_id": "t1",
                "checkpoint_ns": "",
                "parent_config": {"configurable": {"checkpoint_id": "parent-ckp"}},
            }
        }
        checkpoint = {"channel_values": {}}
        await saver.aput(config, checkpoint, {})
        executed_args = cursor.execute.call_args[0][1]
        assert executed_args[4] == "parent-ckp"

    async def test_aput_retries_on_conn_drop(self, mock_conn):
        saver = ModuloPostgresSaver(
            mock_conn, organisation_id=_ORG_ID, fernet_key=_FERNET_KEY, conn_string="postgres://x"
        )
        cursor = AsyncMock()
        cursor.__aenter__ = AsyncMock(return_value=cursor)
        cursor.__aexit__ = AsyncMock(return_value=False)
        # First cursor acquisition raises OperationalError, second succeeds.
        saver._cursor = MagicMock(side_effect=[OperationalError("connection is closed"), cursor])
        saver._reconnect = AsyncMock()
        saver.get_next_version = MagicMock(return_value="ckp-id")

        config = {"configurable": {"thread_id": "t1", "checkpoint_ns": ""}}
        result = await saver.aput(config, {"channel_values": {}}, {})
        assert result["configurable"]["checkpoint_id"] == "ckp-id"
        assert saver._reconnect.await_count == 1

    async def test_aput_conn_drop_without_conn_string_reraises(self, mock_conn):
        saver = ModuloPostgresSaver(mock_conn, organisation_id=_ORG_ID, fernet_key=_FERNET_KEY)
        saver._cursor = MagicMock(side_effect=OperationalError("connection is closed"))
        saver._reconnect = AsyncMock()
        saver.get_next_version = MagicMock(return_value="ckp-id")

        config = {"configurable": {"thread_id": "t1", "checkpoint_ns": ""}}
        with pytest.raises(OperationalError, match="connection is closed"):
            await saver.aput(config, {"channel_values": {}}, {})

    async def test_aput_retry_reconnect_timeout_reraises_original(self, mock_conn):
        saver = ModuloPostgresSaver(
            mock_conn, organisation_id=_ORG_ID, fernet_key=_FERNET_KEY, conn_string="postgres://x"
        )
        saver._cursor = MagicMock(side_effect=OperationalError("connection is closed"))

        async def _reconnect_timeout():
            raise TimeoutError

        saver._reconnect = _reconnect_timeout
        saver.get_next_version = MagicMock(return_value="ckp-id")

        config = {"configurable": {"thread_id": "t1", "checkpoint_ns": ""}}
        with pytest.raises(OperationalError, match="connection is closed"):
            await saver.aput(config, {"channel_values": {}}, {})


class TestReconnect:
    async def test_reconnect_noop_without_conn_string(self, mock_conn):
        saver = await _make_saver(mock_conn)
        await saver._reconnect()  # should return silently
        assert saver.conn is mock_conn
        assert saver._conn_string is None

    async def test_reconnect_skips_when_not_stale(self, mock_conn):
        saver = ModuloPostgresSaver(
            mock_conn, organisation_id=_ORG_ID, fernet_key=_FERNET_KEY, conn_string="postgres://x"
        )
        saver._connection_is_stale = MagicMock(return_value=False)
        await saver._reconnect()  # no close, no reconnect
        assert saver.conn is mock_conn
        saver._connection_is_stale.assert_called_once()

    async def test_reconnect_success(self, mock_conn):
        saver = ModuloPostgresSaver(
            mock_conn, organisation_id=_ORG_ID, fernet_key=_FERNET_KEY, conn_string="postgres://x"
        )
        old_conn = MagicMock()
        old_conn.closed = True
        new_conn = MagicMock()
        saver.conn = old_conn
        saver._connection_is_stale = MagicMock(return_value=True)

        async def _fake_connect(*args, **kwargs):
            return new_conn

        async def _fake_close():
            return None

        old_conn.close = AsyncMock(side_effect=_fake_close)
        with pytest.MonkeyPatch.context() as mp:
            import psycopg

            mp.setattr(psycopg, "AsyncConnection", type("C", (), {"connect": staticmethod(_fake_connect)}))
            mp.setattr("modulo.core.pipeline_engine.modulo_saver._RECONNECT_TIMEOUT_SECONDS", 5.0)
            await saver._reconnect()

        assert saver.conn is new_conn

    async def test_reconnect_timeout_closes_and_raises(self, mock_conn):
        saver = ModuloPostgresSaver(
            mock_conn, organisation_id=_ORG_ID, fernet_key=_FERNET_KEY, conn_string="postgres://x"
        )
        old_conn = MagicMock()
        old_conn.close = AsyncMock()
        saver.conn = old_conn
        saver._connection_is_stale = MagicMock(return_value=True)

        async def _hanging_connect(*args, **kwargs):
            await asyncio.sleep(30)

        with pytest.MonkeyPatch.context() as mp:
            import psycopg

            mp.setattr(psycopg, "AsyncConnection", type("C", (), {"connect": staticmethod(_hanging_connect)}))
            mp.setattr("modulo.core.pipeline_engine.modulo_saver._RECONNECT_TIMEOUT_SECONDS", 0.01)
            with pytest.raises(asyncio.TimeoutError):
                await saver._reconnect()

    async def test_connection_is_stale_none_conn(self, mock_conn):
        saver = await _make_saver(mock_conn)
        saver.conn = None
        assert saver._connection_is_stale() is False

    async def test_cursor_reconnects_stale(self, mock_conn):
        saver = ModuloPostgresSaver(
            mock_conn, organisation_id=_ORG_ID, fernet_key=_FERNET_KEY, conn_string="postgres://x"
        )
        saver._connection_is_stale = MagicMock(return_value=True)
        saver._reconnect = AsyncMock()

        real_cursor = AsyncMock()
        real_cursor.__aenter__ = AsyncMock(return_value=real_cursor)
        real_cursor.__aexit__ = AsyncMock(return_value=False)

        from contextlib import asynccontextmanager

        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

        @asynccontextmanager
        async def _base_cursor(self, pipeline=False):
            yield real_cursor

        original = AsyncPostgresSaver._cursor
        AsyncPostgresSaver._cursor = _base_cursor
        try:
            async with saver._cursor():
                pass
        finally:
            AsyncPostgresSaver._cursor = original
        assert saver._reconnect.await_count == 1


class TestSyncWrappers:
    def test_run_sync_from_sync_context(self):
        saver = asyncio.run(_make_saver(MagicMock()))
        # Outside an event loop, _run_sync executes the coroutine to completion.
        result = saver._run_sync(_echo(42))
        assert result == 42

    def test_run_sync_raises_in_async_context(self):
        async def _inner():
            saver = await _make_saver(MagicMock())
            with pytest.raises(RuntimeError, match="must not be called from an async context"):
                # A plain value (not a coroutine) is fine — _run_sync raises on
                # the running-loop check before touching its argument.
                saver._run_sync(object())

        asyncio.run(_inner())

    async def test_load_blobs_and_writes_delegate(self, mock_conn):
        saver = await _make_saver(mock_conn)
        assert saver._load_blobs(None) is None
        assert saver._load_writes(None) is None


async def _echo(value):
    return value


class TestFromConnString:
    async def test_from_conn_string_constructs_saver(self):

        base = MagicMock()
        base.conn = MagicMock()

        @asynccontextmanager
        async def _base_cm(_conn_string):
            yield base

        with patch("modulo.core.pipeline_engine.modulo_saver.AsyncPostgresSaver.from_conn_string", _base_cm):
            async with ModuloPostgresSaver.from_conn_string(
                "postgresql://user:pass@localhost/db",
                organisation_id=_ORG_ID,
                fernet_key=_FERNET_KEY,
            ) as saver:
                assert saver.conn is base.conn
                assert saver._org_id == _ORG_ID
                assert saver._fernet is not None
                assert saver._conn_string == "postgresql://user:pass@localhost/db"

    async def test_from_conn_string_passes_old_fernet_key(self):

        base = MagicMock()
        base.conn = MagicMock()
        old_key = Fernet.generate_key().decode()

        @asynccontextmanager
        async def _base_cm(_conn_string):
            yield base

        with patch("modulo.core.pipeline_engine.modulo_saver.AsyncPostgresSaver.from_conn_string", _base_cm):
            async with ModuloPostgresSaver.from_conn_string(
                "postgresql://u:p@localhost/db",
                organisation_id=_ORG_ID,
                fernet_key=_FERNET_KEY,
                fernet_key_old=old_key,
            ) as saver:
                assert saver._fernet_old is not None


class TestSyncOverrides:
    def test_get_tuple_sync_wrapper(self):
        """get_tuple runs aget_tuple through _run_sync from a sync context."""
        saver = asyncio.run(_make_saver(MagicMock()))
        saver.aget_tuple = AsyncMock(return_value="tuple-result")
        result = saver.get_tuple({"configurable": {"thread_id": "t", "checkpoint_id": "c"}})
        assert result == "tuple-result"
        saver.aget_tuple.assert_awaited_once_with({"configurable": {"thread_id": "t", "checkpoint_id": "c"}})

    def test_list_sync_wrapper(self):
        """list runs _alist_sync through _run_sync and collects rows."""
        saver = asyncio.run(_make_saver(MagicMock()))
        called_with: dict[str, object] = {}

        async def _fake_alist(config, *, filter=None, before=None, limit=None):
            called_with["config"] = config
            called_with["filter"] = filter
            called_with["before"] = before
            called_with["limit"] = limit
            for row in ("row-1", "row-2"):
                yield row

        saver.alist = _fake_alist
        result = saver.list({"configurable": {"thread_id": "t"}}, limit=5)
        assert result == ["row-1", "row-2"]
        assert called_with["config"] == {"configurable": {"thread_id": "t"}}
        assert called_with["filter"] is None
        assert called_with["before"] is None
        assert called_with["limit"] == 5
