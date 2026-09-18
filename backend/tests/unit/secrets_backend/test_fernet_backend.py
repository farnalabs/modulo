"""Unit tests for FernetSecretsBackend."""

import asyncio
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from cryptography.fernet import Fernet
from sqlalchemy.exc import IntegrityError, OperationalError

from modulo.core.secrets_backend.fernet import FernetSecretsBackend
from modulo.db.models.secret import Secret

_KEY = Fernet.generate_key().decode()
_SECRET_VALUE = "my-secret-value"
_ORG_ID = uuid.UUID(int=42)


def _make_session(*, row: Any = None, org_id: uuid.UUID | None = _ORG_ID) -> MagicMock:
    """Build a mock async session whose execute() answers current_setting queries.

    Non-org queries return *row* from scalar_one_or_none() (None by default).
    *org_id* is returned for current_setting queries; pass None to simulate a
    Postgres backend with no RLS context set.
    """
    session = MagicMock()
    session.add = MagicMock()
    session.flush = AsyncMock()

    async def mock_execute(stmt, *args, **kwargs):
        result = MagicMock()
        if "current_setting" in str(stmt):
            result.scalar.return_value = str(org_id) if org_id is not None else None
        else:
            result.scalar_one_or_none.return_value = row
        return result

    session.execute = AsyncMock(side_effect=mock_execute)
    return session


@pytest.fixture
def mock_session() -> MagicMock:
    return _make_session()


class TestGetSecret:
    async def test_returns_decrypted_value(self):
        encrypted = Fernet(_KEY.encode()).encrypt(_SECRET_VALUE.encode())
        row = MagicMock(spec=Secret)
        row.encrypted_value = encrypted

        backend = FernetSecretsBackend(fernet_key=_KEY, session=_make_session(row=row))

        value = await backend.get_secret("some-key")
        assert value == _SECRET_VALUE, f"Expected {_SECRET_VALUE}, got {value}"

    async def test_unknown_key_raises(self):
        backend = FernetSecretsBackend(fernet_key=_KEY, session=_make_session())

        with pytest.raises(KeyError, match="unknown-key"):
            await backend.get_secret("unknown-key")

    async def test_no_session_raises_runtime_error(self):
        backend = FernetSecretsBackend(fernet_key=_KEY)

        with pytest.raises(RuntimeError, match="no DB session"):
            await backend.get_secret("some-key")

    async def test_empty_key_raises_value_error(self, mock_session):
        backend = FernetSecretsBackend(fernet_key=_KEY, session=mock_session)

        with pytest.raises(ValueError, match="non-empty"):
            await backend.get_secret("")

    async def test_corrupted_data_raises_value_error(self):
        row = MagicMock(spec=Secret)
        row.encrypted_value = b"\x00\x00\x00\x00"
        backend = FernetSecretsBackend(fernet_key=_KEY, session=_make_session(row=row))

        with pytest.raises(ValueError, match="Failed to decrypt secret"):
            await backend.get_secret("corrupted-key")

    async def test_none_encrypted_value_raises_value_error(self):
        """A NULL encrypted_value must raise ValueError, not a raw TypeError."""
        row = MagicMock(spec=Secret)
        row.encrypted_value = None
        backend = FernetSecretsBackend(fernet_key=_KEY, session=_make_session(row=row))

        with pytest.raises(ValueError, match="Failed to decrypt secret"):
            await backend.get_secret("null-value-key")

    async def test_old_key_fallback_decrypts(self):
        """Rotation: secrets encrypted with old_key must decrypt via fallback."""
        old_key = Fernet.generate_key().decode()
        old_fernet = Fernet(old_key.encode())
        encrypted = old_fernet.encrypt(_SECRET_VALUE.encode())
        row = MagicMock(spec=Secret)
        row.encrypted_value = encrypted

        backend = FernetSecretsBackend(fernet_key=_KEY, old_key=old_key, session=_make_session(row=row))
        value = await backend.get_secret("rotated-key")
        assert value == _SECRET_VALUE

    async def test_both_keys_fail_raises_value_error(self):
        """When neither the current nor the rotation key can decrypt, ValueError is raised."""
        other_key = Fernet.generate_key().decode()
        encrypted = Fernet(other_key.encode()).encrypt(_SECRET_VALUE.encode())
        row = MagicMock(spec=Secret)
        row.encrypted_value = encrypted

        backend = FernetSecretsBackend(
            fernet_key=_KEY,
            old_key=Fernet.generate_key().decode(),
            session=_make_session(row=row),
        )
        with pytest.raises(ValueError, match="Failed to decrypt secret"):
            await backend.get_secret("unreadable-key")


class TestSetSecret:
    async def test_creates_new_row_via_upsert(self):
        """No existing row -> a new Secret is added with the encrypted value."""
        session = _make_session()
        backend = FernetSecretsBackend(fernet_key=_KEY, session=session)

        await backend.set_secret("new-key", _SECRET_VALUE)

        session.flush.assert_called_once()
        session.add.assert_called_once()
        added = session.add.call_args[0][0]
        assert added.key == "new-key"
        assert added.organisation_id == _ORG_ID
        assert backend._fernet.decrypt(added.encrypted_value).decode() == _SECRET_VALUE

    async def test_updates_existing_row_via_upsert(self):
        """An existing row is re-encrypted in place — no new row is added."""
        existing = MagicMock(spec=Secret)
        existing.key = "existing-key"
        existing.organisation_id = _ORG_ID
        existing.encrypted_value = b"stale-ciphertext"
        session = _make_session(row=existing)
        backend = FernetSecretsBackend(fernet_key=_KEY, session=session)

        await backend.set_secret("existing-key", _SECRET_VALUE)

        session.flush.assert_called_once()
        session.add.assert_not_called()
        assert existing.encrypted_value != b"stale-ciphertext"
        assert backend._fernet.decrypt(existing.encrypted_value).decode() == _SECRET_VALUE

    async def test_no_session_raises_runtime_error(self):
        backend = FernetSecretsBackend(fernet_key=_KEY)

        with pytest.raises(RuntimeError, match="no DB session"):
            await backend.set_secret("some-key", _SECRET_VALUE)

    async def test_empty_key_raises_value_error(self, mock_session):
        backend = FernetSecretsBackend(fernet_key=_KEY, session=mock_session)

        with pytest.raises(ValueError, match="non-empty"):
            await backend.set_secret("", _SECRET_VALUE)

    def test_invalid_fernet_key_at_construction_raises(self) -> None:
        with pytest.raises(ValueError, match="base64-encoded"):
            FernetSecretsBackend(fernet_key="not-a-valid-base64-key")

    def test_invalid_old_key_at_construction_raises(self) -> None:
        """An invalid rotation key must fail fast at construction, like the primary key."""
        with pytest.raises(ValueError, match="base64-encoded"):
            FernetSecretsBackend(fernet_key=_KEY, old_key="not-a-valid-base64-key")

    async def test_no_rls_context_raises(self):
        session = _make_session(org_id=None)
        backend = FernetSecretsBackend(fernet_key=_KEY, session=session)

        with pytest.raises(RuntimeError, match="RLS organisation context"):
            await backend.set_secret("new-key", _SECRET_VALUE)


class TestDeleteSecret:
    async def test_executes_delete_scoped_to_key_and_org(self, mock_session):
        """delete_secret is a bulk delete scoped by key and org — independent of row existence."""
        backend = FernetSecretsBackend(fernet_key=_KEY, session=mock_session)

        await backend.delete_secret("existing-key")

        delete_stmt = next(
            (
                call.args[0]
                for call in mock_session.execute.await_args_list
                if "DELETE FROM secrets" in str(call.args[0])
            ),
            None,
        )
        assert delete_stmt is not None, "Expected a DELETE statement against the secrets table"
        assert "organisation_id" in str(delete_stmt), "Expected the delete to be scoped by organisation_id"
        mock_session.flush.assert_awaited_once()

    async def test_no_session_raises_runtime_error(self):
        backend = FernetSecretsBackend(fernet_key=_KEY)

        with pytest.raises(RuntimeError, match="no DB session"):
            await backend.delete_secret("some-key")

    async def test_empty_key_raises_value_error(self, mock_session):
        backend = FernetSecretsBackend(fernet_key=_KEY, session=mock_session)

        with pytest.raises(ValueError, match="non-empty"):
            await backend.delete_secret("")


class TestSetSession:
    def test_set_session_after_construction(self):
        backend = FernetSecretsBackend(fernet_key=_KEY)
        session = MagicMock()
        session.execute = AsyncMock()
        backend.set_session(session)
        assert backend._session is session, "Expected session to be set on backend"


class TestSetSecretTOCTOU:
    @staticmethod
    def _session_with_flush(flush: AsyncMock) -> MagicMock:
        session = _make_session()
        session.flush = flush
        return session

    async def test_integrity_error_retries_then_succeeds(self):
        """A concurrent INSERT racing this one is retried exactly once."""
        session = self._session_with_flush(
            AsyncMock(side_effect=[IntegrityError("stmt", {}, Exception("duplicate key")), None])
        )
        backend = FernetSecretsBackend(fernet_key=_KEY, session=session)

        await backend.set_secret("new-key", _SECRET_VALUE)

        assert session.flush.call_count == 2, "Expected one retry after IntegrityError"
        assert session.add.call_count == 2, "Expected the new row to be re-added on retry"

    async def test_integrity_error_exhausted_raises(self):
        session = self._session_with_flush(
            AsyncMock(side_effect=IntegrityError("stmt", {}, Exception("duplicate key")))
        )
        backend = FernetSecretsBackend(fernet_key=_KEY, session=session)

        with pytest.raises(IntegrityError):
            await backend.set_secret("new-key", _SECRET_VALUE)

        assert session.flush.call_count == 2, "Expected both attempts to fail before re-raise"


class TestOrgIdResolution:
    async def test_falls_back_to_session_info_on_non_postgres(self):
        """current_setting() is unavailable on non-Postgres backends; session.info is used."""
        session = MagicMock()
        session.info = {"org_id": str(_ORG_ID)}
        session.execute = AsyncMock()
        session.add = MagicMock()
        session.flush = AsyncMock()

        async def mock_execute(stmt, *args, **kwargs):
            if "current_setting" in str(stmt):
                raise OperationalError("stmt", {}, Exception("function does not exist"))
            result = MagicMock()
            result.scalar_one_or_none.return_value = None
            return result

        session.execute = AsyncMock(side_effect=mock_execute)
        backend = FernetSecretsBackend(fernet_key=_KEY, session=session)

        await backend.set_secret("new-key", _SECRET_VALUE)

        session.flush.assert_called_once()

    async def test_invalid_org_id_format_raises(self):
        session = MagicMock()

        async def mock_execute(stmt, *args, **kwargs):
            result = MagicMock()
            result.scalar.return_value = "not-a-uuid"
            return result

        session.execute = AsyncMock(side_effect=mock_execute)
        backend = FernetSecretsBackend(fernet_key=_KEY, session=session)

        with pytest.raises(RuntimeError, match="invalid organisation_id"):
            await backend.set_secret("new-key", _SECRET_VALUE)

    async def test_no_session_during_org_id_read_raises(self):
        backend = FernetSecretsBackend(fernet_key=_KEY, session=MagicMock())
        backend._session = None
        backend._org_id = None

        with pytest.raises(RuntimeError, match="no DB session"):
            await backend._read_org_id_from_session()


class TestDeleteSecretErrors:
    async def test_error_raises(self):
        session = _make_session()
        session.flush = AsyncMock(side_effect=RuntimeError("db down"))
        backend = FernetSecretsBackend(fernet_key=_KEY, session=session)

        with pytest.raises(RuntimeError, match="db down"):
            await backend.delete_secret("some-key")

    async def test_cancelled_error_propagates(self):
        session = _make_session()
        session.flush = AsyncMock(side_effect=asyncio.CancelledError())
        backend = FernetSecretsBackend(fernet_key=_KEY, session=session)

        with pytest.raises(asyncio.CancelledError):
            await backend.delete_secret("some-key")


class TestGetSecretOrgScoping:
    async def test_filters_by_org_id(self):
        backend = FernetSecretsBackend(fernet_key=_KEY, session=_make_session())

        row = MagicMock(spec=Secret)
        row.encrypted_value = backend._fernet.encrypt(_SECRET_VALUE.encode())

        execute_calls = []

        async def mock_execute(stmt, *args, **kwargs):
            execute_calls.append((str(stmt), args, kwargs))
            result = MagicMock()
            if "current_setting" in str(stmt):
                result.scalar.return_value = str(_ORG_ID)
            else:
                result.scalar_one_or_none.return_value = row
            return result

        backend._session.execute = AsyncMock(side_effect=mock_execute)

        value = await backend.get_secret("my-key")
        assert value == _SECRET_VALUE, f"Expected {_SECRET_VALUE}, got {value}"

        # Verify org_id was added to WHERE clause
        get_secret_call = [c for c in execute_calls if "organisation_id" in str(c[0])]
        assert len(get_secret_call) > 0, "Expected organisation_id filter in get_secret query"

    async def test_wrong_org_raises_key_error(self):
        """get_secret with key that exists but under a different org should raise KeyError."""
        backend = FernetSecretsBackend(fernet_key=_KEY, session=_make_session())

        with pytest.raises(KeyError):
            await backend.get_secret("key-from-other-org")


class TestDeleteSecretOrgScoping:
    async def test_filters_by_org_id(self):
        backend = FernetSecretsBackend(fernet_key=_KEY, session=_make_session())

        execute_calls = []

        async def mock_execute(stmt, *args, **kwargs):
            execute_calls.append((str(stmt), args, kwargs))
            result = MagicMock()
            if "current_setting" in str(stmt):
                result.scalar.return_value = str(_ORG_ID)
            else:
                result.scalar_one_or_none.return_value = MagicMock()
            return result

        backend._session.execute = AsyncMock(side_effect=mock_execute)

        await backend.delete_secret("my-key")

        # Verify at least one captured call contains organisation_id
        delete_stmt = [c for c in execute_calls if "organisation_id" in c[0]]
        assert len(delete_stmt) > 0, "Expected organisation_id filter in delete query"


class TestOrgIdCaching:
    async def test_read_org_id_from_session_caches(self):
        session = _make_session()
        backend = FernetSecretsBackend(fernet_key=_KEY, session=session)

        await backend.set_secret("key1", "val1")

        call_count = session.execute.call_count
        await backend.set_secret("key2", "val2")

        assert session.execute.call_count == call_count + 1, (
            f"Expected {call_count + 1} execute calls, got {session.execute.call_count}"
        )


class TestDBSessionTimeout:
    """The _DB_TIMEOUT wait_for wrappers must surface TimeoutError.

    Timeouts are intentionally NOT wrapped in RuntimeError: the DB is a core
    dependency and a timeout is a real failure, not a degraded-mode fallback.
    """

    async def test_get_secret_timeout_propagates(self, mock_session):
        backend = FernetSecretsBackend(fernet_key=_KEY, session=mock_session)
        mock_session.execute = AsyncMock(side_effect=TimeoutError())

        with pytest.raises(TimeoutError):
            await backend.get_secret("some-key")

    async def test_set_secret_timeout_propagates(self, mock_session):
        backend = FernetSecretsBackend(fernet_key=_KEY, session=mock_session)
        mock_session.execute = AsyncMock(side_effect=TimeoutError())

        with pytest.raises(TimeoutError):
            await backend.set_secret("some-key", "value")

    async def test_delete_secret_timeout_propagates(self, mock_session):
        backend = FernetSecretsBackend(fernet_key=_KEY, session=mock_session)
        mock_session.execute = AsyncMock(side_effect=TimeoutError())

        with pytest.raises(TimeoutError):
            await backend.delete_secret("some-key")


class TestSetSessionResetsCache:
    async def test_set_session_clears_cached_org_id(self):
        """set_session must drop the cached org id so it is re-read from the new session."""
        session = _make_session()
        backend = FernetSecretsBackend(fernet_key=_KEY, session=session)
        await backend.set_secret("key1", "val1")
        assert backend._org_id == _ORG_ID

        new_session = _make_session()
        backend.set_session(new_session)
        assert backend._session is new_session
        assert backend._org_id is None, "set_session must reset the cached organisation id"

        await backend.set_secret("key2", "val2")
        assert backend._org_id == _ORG_ID
