"""Unit tests for FernetSecretsBackend."""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import text

from modulo.core.secrets_backend.fernet import FernetSecretsBackend
from modulo.db.models.secret import Secret

_KEY = Fernet.generate_key().decode()
_SECRET_VALUE = "my-secret-value"
_ORG_ID = uuid.UUID(int=42)


def _set_org_id(session: MagicMock) -> None:
    """Patch session.execute so that a current_setting query returns _ORG_ID."""
    real_execute = session.execute

    async def mock_execute(stmt, *args, **kwargs):
        compiled = str(stmt) if hasattr(stmt, "__str__") else str(stmt)
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
    session.execute = AsyncMock(return_value=MagicMock())
    session.add = MagicMock()
    session.delete = AsyncMock()
    session.flush = AsyncMock()
    return session


class TestGetSecret:
    async def test_returns_decrypted_value(self, mock_session):
        backend = FernetSecretsBackend(fernet_key=_KEY, session=mock_session)
        encrypted = backend._fernet.encrypt(_SECRET_VALUE.encode())
        row = MagicMock(spec=Secret)
        row.encrypted_value = encrypted

        async def mock_execute(stmt, *args, **kwargs):
            result = MagicMock()
            result.scalar_one_or_none.return_value = row
            return result
        mock_session.execute = AsyncMock(side_effect=mock_execute)

        value = await backend.get_secret("some-key")
        assert value == _SECRET_VALUE

    async def test_unknown_key_raises(self, mock_session):
        backend = FernetSecretsBackend(fernet_key=_KEY, session=mock_session)

        async def mock_execute(stmt, *args, **kwargs):
            result = MagicMock()
            result.scalar_one_or_none.return_value = None
            return result
        mock_session.execute = AsyncMock(side_effect=mock_execute)

        with pytest.raises(KeyError, match="unknown-key"):
            await backend.get_secret("unknown-key")

    async def test_no_session_raises(self):
        backend = FernetSecretsBackend(fernet_key=_KEY)

        with pytest.raises(RuntimeError, match="no DB session"):
            await backend.get_secret("some-key")

    async def test_corrupted_data_raises_value_error(self, mock_session):
        backend = FernetSecretsBackend(fernet_key=_KEY, session=mock_session)
        row = MagicMock(spec=Secret)
        row.encrypted_value = b"\x00\x00\x00\x00"

        async def mock_execute(stmt, *args, **kwargs):
            result = MagicMock()
            result.scalar_one_or_none.return_value = row
            return result
        mock_session.execute = AsyncMock(side_effect=mock_execute)

        with pytest.raises(ValueError, match="Failed to decrypt secret"):
            await backend.get_secret("corrupted-key")


class TestSetSecret:
    async def test_creates_new_row_via_upsert(self, mock_session):
        backend = FernetSecretsBackend(fernet_key=_KEY, session=mock_session)
        _set_org_id(mock_session)

        await backend.set_secret("new-key", _SECRET_VALUE)

        assert mock_session.execute.called
        assert mock_session.flush.called

    async def test_updates_existing_row_via_upsert(self, mock_session):
        backend = FernetSecretsBackend(fernet_key=_KEY, session=mock_session)
        _set_org_id(mock_session)

        await backend.set_secret("existing-key", _SECRET_VALUE)

        assert mock_session.execute.called
        assert mock_session.flush.called

    async def test_no_session_raises(self):
        backend = FernetSecretsBackend(fernet_key=_KEY)

        with pytest.raises(RuntimeError, match="no DB session"):
            await backend.set_secret("some-key", _SECRET_VALUE)

    async def test_no_rls_context_raises(self, mock_session):
        async def mock_execute(stmt, *args, **kwargs):
            result = MagicMock()
            compiled = str(stmt) if hasattr(stmt, "__str__") else str(stmt)
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

        assert mock_session.execute.called
        mock_session.flush.assert_called_once()

    async def test_noop_when_missing(self, mock_session):
        backend = FernetSecretsBackend(fernet_key=_KEY, session=mock_session)

        await backend.delete_secret("missing-key")

        assert mock_session.execute.called
        mock_session.flush.assert_called_once()

    async def test_no_session_raises(self):
        backend = FernetSecretsBackend(fernet_key=_KEY)

        with pytest.raises(RuntimeError, match="no DB session"):
            await backend.delete_secret("some-key")


class TestSetSession:
    async def test_set_session_after_construction(self):
        backend = FernetSecretsBackend(fernet_key=_KEY)
        session = MagicMock()
        session.execute = AsyncMock()
        backend.set_session(session)
        assert backend._session is session


class TestOrgIdCaching:
    async def test_read_org_id_from_session_caches(self, mock_session):
        backend = FernetSecretsBackend(fernet_key=_KEY, session=mock_session)

        _set_org_id(mock_session)
        await backend.set_secret("key1", "val1")

        call_count = mock_session.execute.call_count
        await backend.set_secret("key2", "val2")

        assert mock_session.execute.call_count == call_count + 1
