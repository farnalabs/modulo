"""Unit tests for FernetSecretsBackend."""

import asyncio
import binascii
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from cryptography.fernet import Fernet
from sqlalchemy.exc import IntegrityError, OperationalError

from modulo.core.secrets_backend.fernet import FernetSecretsBackend
from modulo.db.models.secret import Secret

_KEY = Fernet.generate_key().decode()
_SECRET_VALUE = "my-secret-value"
_ORG_ID = uuid.UUID(int=42)


def _set_org_id(session: MagicMock) -> None:
    """Patch session.execute so that a current_setting query returns _ORG_ID."""
    real_execute = session.execute

    async def mock_execute(stmt, *args, **kwargs):
        compiled = str(stmt)
        result = MagicMock()
        if "current_setting" in compiled:
            result.scalar.return_value = str(_ORG_ID)
        else:
            return await real_execute(stmt, *args, **kwargs)
        return result

    session.execute = AsyncMock(side_effect=mock_execute)


@pytest.fixture
def mock_session():
    session = MagicMock()
    session.add = MagicMock()
    session.delete = AsyncMock()
    session.flush = AsyncMock()

    async def default_execute(stmt, *args, **kwargs):
        result = MagicMock()
        if "current_setting" in str(stmt):
            result.scalar.return_value = str(_ORG_ID)
        else:
            result.scalar_one_or_none.return_value = MagicMock()
        return result

    session.execute = AsyncMock(side_effect=default_execute)
    return session


class TestGetSecret:
    async def test_returns_decrypted_value(self, mock_session):
        backend = FernetSecretsBackend(fernet_key=_KEY, session=mock_session)
        encrypted = backend._fernet.encrypt(_SECRET_VALUE.encode())
        row = MagicMock(spec=Secret)
        row.encrypted_value = encrypted

        async def mock_execute(stmt, *args, **kwargs):
            result = MagicMock()
            if "current_setting" in str(stmt):
                result.scalar.return_value = str(_ORG_ID)
            else:
                result.scalar_one_or_none.return_value = row
            return result

        mock_session.execute = AsyncMock(side_effect=mock_execute)

        value = await backend.get_secret("some-key")
        assert value == _SECRET_VALUE, f"Expected {_SECRET_VALUE}, got {value}"

    async def test_unknown_key_raises(self, mock_session):
        backend = FernetSecretsBackend(fernet_key=_KEY, session=mock_session)

        async def mock_execute(stmt, *args, **kwargs):
            result = MagicMock()
            if "current_setting" in str(stmt):
                result.scalar.return_value = str(_ORG_ID)
            else:
                result.scalar_one_or_none.return_value = None
            return result

        mock_session.execute = AsyncMock(side_effect=mock_execute)

        with pytest.raises(KeyError, match="unknown-key"):
            await backend.get_secret("unknown-key")

    async def test_no_session_raises_runtime_error(self):
        backend = FernetSecretsBackend(fernet_key=_KEY)

        with pytest.raises(RuntimeError, match="no DB session"):
            await backend.get_secret("some-key")

    async def test_corrupted_data_raises_value_error(self, mock_session):
        backend = FernetSecretsBackend(fernet_key=_KEY, session=mock_session)
        row = MagicMock(spec=Secret)
        row.encrypted_value = b"\x00\x00\x00\x00"

        async def mock_execute(stmt, *args, **kwargs):
            result = MagicMock()
            if "current_setting" in str(stmt):
                result.scalar.return_value = str(_ORG_ID)
            else:
                result.scalar_one_or_none.return_value = row
            return result

        mock_session.execute = AsyncMock(side_effect=mock_execute)

        with pytest.raises(ValueError, match="Failed to decrypt secret"):
            await backend.get_secret("corrupted-key")

    async def test_none_encrypted_value_raises_value_error(self, mock_session):
        """A NULL encrypted_value must raise ValueError, not a raw TypeError."""
        backend = FernetSecretsBackend(fernet_key=_KEY, session=mock_session)
        row = MagicMock(spec=Secret)
        row.encrypted_value = None

        async def mock_execute(stmt, *args, **kwargs):
            result = MagicMock()
            if "current_setting" in str(stmt):
                result.scalar.return_value = str(_ORG_ID)
            else:
                result.scalar_one_or_none.return_value = row
            return result

        mock_session.execute = AsyncMock(side_effect=mock_execute)

        with pytest.raises(ValueError, match="Failed to decrypt secret"):
            await backend.get_secret("null-value-key")

    async def test_old_key_fallback_decrypts(self, mock_session):
        """Rotation: secrets encrypted with old_key must decrypt via fallback."""
        old_key = Fernet.generate_key().decode()
        old_fernet = Fernet(old_key.encode())
        encrypted = old_fernet.encrypt(_SECRET_VALUE.encode())
        row = MagicMock(spec=Secret)
        row.encrypted_value = encrypted

        async def mock_execute(stmt, *args, **kwargs):
            result = MagicMock()
            if "current_setting" in str(stmt):
                result.scalar.return_value = str(_ORG_ID)
            else:
                result.scalar_one_or_none.return_value = row
            return result

        mock_session.execute = AsyncMock(side_effect=mock_execute)

        backend = FernetSecretsBackend(fernet_key=_KEY, old_key=old_key, session=mock_session)
        value = await backend.get_secret("rotated-key")
        assert value == _SECRET_VALUE


class TestSetSecret:
    async def test_creates_new_row_via_upsert(self, mock_session):
        backend = FernetSecretsBackend(fernet_key=_KEY, session=mock_session)
        _set_org_id(mock_session)

        await backend.set_secret("new-key", _SECRET_VALUE)

        assert mock_session.flush.called, "Expected flush to be called after set_secret"

    async def test_updates_existing_row_via_upsert(self, mock_session):
        backend = FernetSecretsBackend(fernet_key=_KEY, session=mock_session)
        _set_org_id(mock_session)

        await backend.set_secret("existing-key", _SECRET_VALUE)

        assert mock_session.flush.called, "Expected flush to be called after set_secret"

    async def test_no_session_raises_runtime_error(self):
        backend = FernetSecretsBackend(fernet_key=_KEY)

        with pytest.raises(RuntimeError, match="no DB session"):
            await backend.set_secret("some-key", _SECRET_VALUE)

    async def test_empty_key_raises_value_error(self, mock_session):
        backend = FernetSecretsBackend(fernet_key=_KEY, session=mock_session)

        with pytest.raises(ValueError, match="non-empty"):
            await backend.set_secret("", _SECRET_VALUE)

    async def test_invalid_fernet_key_at_construction_raises(self):
        with pytest.raises((ValueError, binascii.Error)):
            FernetSecretsBackend(fernet_key="not-a-valid-base64-key")

    async def test_no_rls_context_raises(self, mock_session):
        async def mock_execute(stmt, *args, **kwargs):
            result = MagicMock()
            compiled = str(stmt)
            if "current_setting" in compiled:
                result.scalar.return_value = None
            else:
                result.scalar_one_or_none.return_value = None
            return result

        mock_session.execute = AsyncMock(side_effect=mock_execute)

        backend = FernetSecretsBackend(fernet_key=_KEY, session=mock_session)
        with pytest.raises(RuntimeError, match="RLS organisation context"):
            await backend.set_secret("new-key", _SECRET_VALUE)


class TestDeleteSecret:
    async def test_removes_row(self, mock_session):
        backend = FernetSecretsBackend(fernet_key=_KEY, session=mock_session)

        await backend.delete_secret("existing-key")

        mock_session.flush.assert_called_once()

    async def test_noop_when_missing(self, mock_session):
        backend = FernetSecretsBackend(fernet_key=_KEY, session=mock_session)

        async def mock_execute(stmt, *args, **kwargs):
            result = MagicMock()
            compiled = str(stmt)
            if "current_setting" in compiled:
                result.scalar.return_value = str(_ORG_ID)
            else:
                result.scalar_one_or_none.return_value = None
            return result

        mock_session.execute = AsyncMock(side_effect=mock_execute)

        await backend.delete_secret("missing-key")

        mock_session.flush.assert_called_once()

    async def test_no_session_raises_runtime_error(self):
        backend = FernetSecretsBackend(fernet_key=_KEY)

        with pytest.raises(RuntimeError, match="no DB session"):
            await backend.delete_secret("some-key")


class TestSetSession:
    async def test_set_session_after_construction(self):
        backend = FernetSecretsBackend(fernet_key=_KEY)
        session = MagicMock()
        session.execute = AsyncMock()
        backend.set_session(session)
        assert backend._session is session, "Expected session to be set on backend"


class TestSetSecretTOCTOU:
    @staticmethod
    def _session_with_flush(flush: AsyncMock) -> MagicMock:
        session = MagicMock()
        session.add = MagicMock()

        async def mock_execute(stmt, *args, **kwargs):
            result = MagicMock()
            if "current_setting" in str(stmt):
                result.scalar.return_value = str(_ORG_ID)
            else:
                result.scalar_one_or_none.return_value = MagicMock()
            return result

        session.execute = AsyncMock(side_effect=mock_execute)
        session.flush = flush
        return session

    async def test_integrity_error_retries_then_succeeds(self):
        """A concurrent insert racing this one is retried exactly once."""
        session = self._session_with_flush(
            AsyncMock(side_effect=[IntegrityError("stmt", {}, Exception("duplicate key")), None])
        )
        backend = FernetSecretsBackend(fernet_key=_KEY, session=session)

        await backend.set_secret("new-key", _SECRET_VALUE)

        assert session.flush.call_count == 2, "Expected one retry after IntegrityError"

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
        session.info = {"organisation_id": str(_ORG_ID)}
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
        session = MagicMock()
        session.execute = AsyncMock()
        _set_org_id(session)
        session.flush = AsyncMock(side_effect=RuntimeError("db down"))
        backend = FernetSecretsBackend(fernet_key=_KEY, session=session)

        with pytest.raises(RuntimeError, match="db down"):
            await backend.delete_secret("some-key")

    async def test_cancelled_error_propagates(self):
        session = MagicMock()
        session.execute = AsyncMock()
        _set_org_id(session)
        session.flush = AsyncMock(side_effect=asyncio.CancelledError())
        backend = FernetSecretsBackend(fernet_key=_KEY, session=session)

        with pytest.raises(asyncio.CancelledError):
            await backend.delete_secret("some-key")


class TestGetSecretOrgScoping:
    async def test_filters_by_org_id(self, mock_session):
        backend = FernetSecretsBackend(fernet_key=_KEY, session=mock_session)
        _set_org_id(mock_session)

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

        mock_session.execute = AsyncMock(side_effect=mock_execute)

        value = await backend.get_secret("my-key")
        assert value == _SECRET_VALUE, f"Expected {_SECRET_VALUE}, got {value}"

        # Verify org_id was added to WHERE clause
        get_secret_call = [c for c in execute_calls if "organisation_id" in str(c[0])]
        assert len(get_secret_call) > 0, "Expected organisation_id filter in get_secret query"

    async def test_wrong_org_raises_key_error(self, mock_session):
        """get_secret with key that exists but under a different org should raise KeyError."""
        backend = FernetSecretsBackend(fernet_key=_KEY, session=mock_session)
        _set_org_id(mock_session)

        async def mock_execute(stmt, *args, **kwargs):
            result = MagicMock()
            if "current_setting" in str(stmt):
                result.scalar.return_value = str(_ORG_ID)
            else:
                result.scalar_one_or_none.return_value = None
            return result

        mock_session.execute = AsyncMock(side_effect=mock_execute)

        with pytest.raises(KeyError):
            await backend.get_secret("key-from-other-org")


class TestDeleteSecretOrgScoping:
    async def test_filters_by_org_id(self, mock_session):
        backend = FernetSecretsBackend(fernet_key=_KEY, session=mock_session)
        _set_org_id(mock_session)

        execute_calls = []

        async def mock_execute(stmt, *args, **kwargs):
            execute_calls.append((str(stmt), args, kwargs))
            result = MagicMock()
            if "current_setting" in str(stmt):
                result.scalar.return_value = str(_ORG_ID)
            else:
                result.scalar_one_or_none.return_value = MagicMock()
            return result

        mock_session.execute = AsyncMock(side_effect=mock_execute)

        await backend.delete_secret("my-key")

        # Verify at least one captured call contains organisation_id
        delete_stmt = [c for c in execute_calls if "organisation_id" in c[0]]
        assert len(delete_stmt) > 0, "Expected organisation_id filter in delete query"


class TestOrgIdCaching:
    async def test_read_org_id_from_session_caches(self, mock_session):
        backend = FernetSecretsBackend(fernet_key=_KEY, session=mock_session)

        _set_org_id(mock_session)
        await backend.set_secret("key1", "val1")

        call_count = mock_session.execute.call_count
        await backend.set_secret("key2", "val2")

        assert mock_session.execute.call_count == call_count + 1, (
            f"Expected {call_count + 1} execute calls, got {mock_session.execute.call_count}"
        )
