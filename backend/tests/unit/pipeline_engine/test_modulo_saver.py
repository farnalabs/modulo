"""Unit tests for ModuloPostgresSaver — org isolation, encryption, SQL."""

import json
import uuid
from unittest.mock import AsyncMock, MagicMock


class _AsyncIter:
    def __init__(self, items):
        self._iter = iter(items)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._iter)
        except StopIteration:
            raise StopAsyncIteration


import pytest  # noqa: E402
from cryptography.fernet import Fernet  # noqa: E402

from modulo.core.pipeline_engine.modulo_saver import ModuloPostgresSaver  # noqa: E402

_FERNET_KEY = Fernet.generate_key().decode()
_ORG_ID = uuid.uuid4()


@pytest.fixture
def mock_conn():
    return MagicMock()


class TestInit:
    async def test_stores_org_id(self, mock_conn):
        saver = ModuloPostgresSaver(mock_conn, organisation_id=_ORG_ID, fernet_key=_FERNET_KEY)
        assert saver._org_id == _ORG_ID

    async def test_stores_fernet_key(self, mock_conn):
        saver = ModuloPostgresSaver(mock_conn, organisation_id=_ORG_ID, fernet_key=_FERNET_KEY)
        assert saver._fernet is not None

    async def test_no_fernet_when_not_given(self, mock_conn):
        saver = ModuloPostgresSaver(mock_conn, organisation_id=_ORG_ID, fernet_key=None)
        assert saver._fernet is None


class TestEncryption:
    async def test_encrypt_decrypt_roundtrip(self, mock_conn):
        saver = ModuloPostgresSaver(mock_conn, organisation_id=_ORG_ID, fernet_key=_FERNET_KEY)
        checkpoint = {"id": "test", "channel_values": {"key": "value"}}
        encrypted = saver._encrypt_checkpoint(checkpoint)
        parsed = json.loads(encrypted)
        assert parsed["__encrypted__"] is True
        assert isinstance(parsed["data"], str)

        decrypted = saver._decrypt_checkpoint(encrypted)
        assert decrypted["id"] == "test"
        assert decrypted["channel_values"]["key"] == "value"

    async def test_no_encryption_when_not_configured(self, mock_conn):
        saver = ModuloPostgresSaver(mock_conn, organisation_id=_ORG_ID, fernet_key=None)
        checkpoint = {"id": "plain"}
        serialized = saver._encrypt_checkpoint(checkpoint)
        assert '"__encrypted__"' not in serialized
        decrypted = saver._decrypt_checkpoint(serialized)
        assert decrypted["id"] == "plain"

    async def test_decrypt_plain_dict(self, mock_conn):
        saver = ModuloPostgresSaver(mock_conn, organisation_id=_ORG_ID, fernet_key=_FERNET_KEY)
        result = saver._decrypt_checkpoint({"id": "plain"})
        assert result["id"] == "plain"

    async def test_encrypt_decrypt_with_different_saver(self, mock_conn):
        checkpoint = {"secret": "data"}
        saver1 = ModuloPostgresSaver(mock_conn, organisation_id=_ORG_ID, fernet_key=_FERNET_KEY)
        encrypted = saver1._encrypt_checkpoint(checkpoint)

        saver2 = ModuloPostgresSaver(mock_conn, organisation_id=_ORG_ID, fernet_key=_FERNET_KEY)
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


class TestBlobEncryption:
    """Tests for blob-level encryption (_encrypt_blob, _decrypt_blobs, _decrypt_writes)."""

    async def test_blob_encryption_roundtrip(self, mock_conn):
        saver = ModuloPostgresSaver(mock_conn, organisation_id=_ORG_ID, fernet_key=_FERNET_KEY)
        original = b"hello world sensitive data"
        encrypted = saver._encrypt_blob(original)
        assert isinstance(encrypted, bytes)
        blobs = [[b"ch1", b"bytes", encrypted]]
        result = saver._decrypt_blobs(blobs)
        assert result == {"ch1": original}

    async def test_encrypted_blob_starts_with_fernet_prefix(self, mock_conn):
        saver = ModuloPostgresSaver(mock_conn, organisation_id=_ORG_ID, fernet_key=_FERNET_KEY)
        encrypted = saver._encrypt_blob(b"anything")
        assert encrypted.startswith(b"gAAAAA")

    async def test_decrypt_blobs_returns_original_channel_values(self, mock_conn):
        saver = ModuloPostgresSaver(mock_conn, organisation_id=_ORG_ID, fernet_key=_FERNET_KEY)
        original = b'{"key": "value"}'
        encrypted = saver._encrypt_blob(original)
        blobs = [[b"channel1", b"json", encrypted]]
        result = saver._decrypt_blobs(blobs)
        assert result == {"channel1": original}

    async def test_decrypt_writes_returns_original_writes(self, mock_conn):
        saver = ModuloPostgresSaver(mock_conn, organisation_id=_ORG_ID, fernet_key=_FERNET_KEY)
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
        saver = ModuloPostgresSaver(mock_conn, organisation_id=_ORG_ID, fernet_key=None)
        original = b"plaintext data"
        result = saver._encrypt_blob(original)
        assert result is original

    async def test_decrypt_blobs_plaintext_fallback(self, mock_conn):
        saver = ModuloPostgresSaver(mock_conn, organisation_id=_ORG_ID, fernet_key=_FERNET_KEY)
        plaintext = b'{"key": "value"}'
        blobs = [[b"ch1", b"json", plaintext]]
        result = saver._decrypt_blobs(blobs)
        assert result == {"ch1": plaintext}

    async def test_decrypt_writes_plaintext_fallback(self, mock_conn):
        saver = ModuloPostgresSaver(mock_conn, organisation_id=_ORG_ID, fernet_key=_FERNET_KEY)
        plaintext = b"plain_write_data"
        writes = [[b"task1", b"ch1", b"type1", plaintext]]
        result = saver._decrypt_writes(writes)
        assert result == [("ch1", "type1", plaintext)]

    async def test_decrypt_blobs_none(self, mock_conn):
        saver = ModuloPostgresSaver(mock_conn, organisation_id=_ORG_ID, fernet_key=_FERNET_KEY)
        assert saver._decrypt_blobs(None) is None
        assert saver._decrypt_blobs([]) is None

    async def test_no_decryption_when_saver_has_no_key(self, mock_conn):
        saver = ModuloPostgresSaver(mock_conn, organisation_id=_ORG_ID, fernet_key=None)
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
        except Exception:
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
