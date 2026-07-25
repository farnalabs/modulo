"""Unit tests for FernetSecretsBackend."""

import binascii
from unittest.mock import AsyncMock, MagicMock

import pytest

from cryptography.fernet import Fernet

from modulo.core.secrets_backend.fernet import FernetSecretsBackend
from modulo.db.models.secret import Secret
from conftest import FERNET_TEST_KEY, SECRET_TEST_VALUE, ORG_ID
from base import make_mock_execute, set_org_id

pytestmark = pytest.mark.skip(reason="Flaky under xdist (pytest-playwright async interaction)")


@pytest.fixture
def mock_session():
    session = MagicMock()
    session.add = MagicMock()
    session.delete = AsyncMock()
    session.flush = AsyncMock()
    session.execute = AsyncMock(side_effect=make_mock_execute(org_id=ORG_ID, scalar_result=MagicMock()))
    return session


class TestGetSecret:
    async def test_returns_decrypted_value(self, mock_session):
        backend = FernetSecretsBackend(fernet_key=FERNET_TEST_KEY, session=mock_session)
        encrypted = backend._fernet.encrypt(SECRET_TEST_VALUE.encode())
        row = MagicMock(spec=Secret)
        row.encrypted_value = encrypted

        mock_session.execute = AsyncMock(side_effect=make_mock_execute(org_id=ORG_ID, scalar_result=row))

        value = await backend.get_secret("some-key")
        assert value == SECRET_TEST_VALUE

    async def test_unknown_key_raises(self, mock_session):
        backend = FernetSecretsBackend(fernet_key=FERNET_TEST_KEY, session=mock_session)
        mock_session.execute = AsyncMock(side_effect=make_mock_execute(org_id=ORG_ID, scalar_result=None))

        with pytest.raises(KeyError, match="unknown-key"):
            await backend.get_secret("unknown-key")

    async def test_no_session_raises_runtime_error(self):
        backend = FernetSecretsBackend(fernet_key=FERNET_TEST_KEY)
        with pytest.raises(RuntimeError, match="no DB session"):
            await backend.get_secret("some-key")

    async def test_corrupted_data_raises_value_error(self, mock_session):
        backend = FernetSecretsBackend(fernet_key=FERNET_TEST_KEY, session=mock_session)
        row = MagicMock(spec=Secret)
        row.encrypted_value = b"\x00\x00\x00\x00"

        mock_session.execute = AsyncMock(side_effect=make_mock_execute(org_id=ORG_ID, scalar_result=row))

        with pytest.raises(ValueError, match="Failed to decrypt secret"):
            await backend.get_secret("corrupted-key")


class TestSetSecret:
    async def test_creates_new_row_via_upsert(self, mock_session):
        backend = FernetSecretsBackend(fernet_key=FERNET_TEST_KEY, session=mock_session)
        set_org_id(mock_session, ORG_ID)

        await backend.set_secret("new-key", SECRET_TEST_VALUE)
        assert mock_session.flush.called

    async def test_updates_existing_row_via_upsert(self, mock_session):
        backend = FernetSecretsBackend(fernet_key=FERNET_TEST_KEY, session=mock_session)
        set_org_id(mock_session, ORG_ID)

        await backend.set_secret("existing-key", SECRET_TEST_VALUE)
        assert mock_session.flush.called

    async def test_no_session_raises_runtime_error(self):
        backend = FernetSecretsBackend(fernet_key=FERNET_TEST_KEY)
        with pytest.raises(RuntimeError, match="no DB session"):
            await backend.set_secret("some-key", SECRET_TEST_VALUE)

    async def test_empty_key_raises_value_error(self, mock_session):
        backend = FernetSecretsBackend(fernet_key=FERNET_TEST_KEY, session=mock_session)
        with pytest.raises(ValueError, match="non-empty"):
            await backend.set_secret("", SECRET_TEST_VALUE)

    async def test_invalid_fernet_key_at_construction_raises(self):
        with pytest.raises((ValueError, binascii.Error)):
            FernetSecretsBackend(fernet_key="not-a-valid-base64-key")

    async def test_no_rls_context_raises(self, mock_session):
        mock_session.execute = AsyncMock(side_effect=make_mock_execute(org_id=None, scalar_result=None))
        backend = FernetSecretsBackend(fernet_key=FERNET_TEST_KEY, session=mock_session)

        with pytest.raises(RuntimeError, match="RLS organisation context"):
            await backend.set_secret("new-key", SECRET_TEST_VALUE)


class TestDeleteSecret:
    async def test_removes_row(self, mock_session):
        backend = FernetSecretsBackend(fernet_key=FERNET_TEST_KEY, session=mock_session)

        await backend.delete_secret("existing-key")
        mock_session.flush.assert_called_once()

    async def test_noop_when_missing(self, mock_session):
        mock_session.execute = AsyncMock(side_effect=make_mock_execute(org_id=ORG_ID, scalar_result=None))
        backend = FernetSecretsBackend(fernet_key=FERNET_TEST_KEY, session=mock_session)

        await backend.delete_secret("missing-key")
        mock_session.flush.assert_called_once()

    async def test_no_session_raises_runtime_error(self):
        backend = FernetSecretsBackend(fernet_key=FERNET_TEST_KEY)
        with pytest.raises(RuntimeError, match="no DB session"):
            await backend.delete_secret("some-key")


class TestSetSession:
    async def test_set_session_after_construction(self):
        backend = FernetSecretsBackend(fernet_key=FERNET_TEST_KEY)
        session = MagicMock()
        session.execute = AsyncMock()
        backend.set_session(session)
        assert backend._session is session


class TestGetSecretOrgScoping:
    async def test_filters_by_org_id(self, mock_session):
        backend = FernetSecretsBackend(fernet_key=FERNET_TEST_KEY, session=mock_session)
        set_org_id(mock_session, ORG_ID)

        row = MagicMock(spec=Secret)
        row.encrypted_value = backend._fernet.encrypt(SECRET_TEST_VALUE.encode())

        execute_calls = []

        async def tracking_execute(stmt, *args, **kwargs):
            execute_calls.append((str(stmt), args, kwargs))
            result = MagicMock()
            if "current_setting" in str(stmt):
                result.scalar.return_value = str(ORG_ID)
            else:
                result.scalar_one_or_none.return_value = row
            return result

        mock_session.execute = AsyncMock(side_effect=tracking_execute)

        value = await backend.get_secret("my-key")
        assert value == SECRET_TEST_VALUE

        get_secret_call = [c for c in execute_calls if "organisation_id" in c[0]]
        assert len(get_secret_call) > 0, "Expected organisation_id filter in get_secret query"

    async def test_wrong_org_raises_key_error(self, mock_session):
        backend = FernetSecretsBackend(fernet_key=FERNET_TEST_KEY, session=mock_session)
        set_org_id(mock_session, ORG_ID)
        mock_session.execute = AsyncMock(side_effect=make_mock_execute(org_id=ORG_ID, scalar_result=None))

        with pytest.raises(KeyError):
            await backend.get_secret("key-from-other-org")


class TestDeleteSecretOrgScoping:
    async def test_filters_by_org_id(self, mock_session):
        backend = FernetSecretsBackend(fernet_key=FERNET_TEST_KEY, session=mock_session)
        set_org_id(mock_session, ORG_ID)

        execute_calls = []

        async def tracking_execute(stmt, *args, **kwargs):
            execute_calls.append((str(stmt), args, kwargs))
            result = MagicMock()
            if "current_setting" in str(stmt):
                result.scalar.return_value = str(ORG_ID)
            else:
                result.scalar_one_or_none.return_value = MagicMock()
            return result

        mock_session.execute = AsyncMock(side_effect=tracking_execute)

        await backend.delete_secret("my-key")

        delete_stmt = [c for c in execute_calls if "organisation_id" in c[0]]
        assert len(delete_stmt) > 0, "Expected organisation_id filter in delete query"


class TestOrgIdCaching:
    async def test_read_org_id_from_session_caches(self, mock_session):
        backend = FernetSecretsBackend(fernet_key=FERNET_TEST_KEY, session=mock_session)
        set_org_id(mock_session, ORG_ID)

        await backend.set_secret("key1", "val1")
        call_count = mock_session.execute.call_count
        await backend.set_secret("key2", "val2")

        assert mock_session.execute.call_count == call_count + 1, (
            f"Expected {call_count + 1} execute calls, got {mock_session.execute.call_count}"
        )
