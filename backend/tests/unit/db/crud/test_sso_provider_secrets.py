"""SSO provider secret storage tests."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.auth.secret_storage import decode_stored_secret
from modulo.db.crud.sso_provider import create_provider, update_provider
from modulo.db.models.sso_provider import SsoProvider


def test_decode_rejects_encrypted_secret_from_different_key() -> None:
    encrypted = Fernet(Fernet.generate_key()).encrypt(b"secret")

    with pytest.raises(ValueError, match="cannot be decrypted"):
        decode_stored_secret(encrypted, Fernet.generate_key().decode())


async def test_create_provider_encrypts_client_secret() -> None:
    key = Fernet.generate_key().decode()
    session = AsyncMock(spec=AsyncSession)
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    session.execute.return_value = result

    with patch("modulo.db.crud.sso_provider.append_audit_event", new_callable=AsyncMock):
        provider = await create_provider(
            session,
            provider_type="oidc",
            name="OIDC",
            client_secret="create-secret",
            fernet_key=key,
            org_id=uuid.uuid4(),
        )

    assert isinstance(provider.client_secret, bytes)
    assert provider.client_secret != b"create-secret"
    assert decode_stored_secret(provider.client_secret, key) == "create-secret"


async def test_update_provider_encrypts_client_secret() -> None:
    key = Fernet.generate_key().decode()
    session = AsyncMock(spec=AsyncSession)
    provider = SsoProvider(
        id=uuid.uuid4(),
        organisation_id=uuid.uuid4(),
        provider_type="oidc",
        name="OIDC",
        client_secret=None,
    )

    with (
        patch("modulo.db.crud.sso_provider.get_provider", new=AsyncMock(return_value=provider)),
        patch("modulo.db.crud.sso_provider.append_audit_event", new_callable=AsyncMock),
    ):
        updated = await update_provider(
            session,
            provider.id,
            fernet_key=key,
            client_secret="update-secret",
        )

    assert updated is provider
    assert isinstance(provider.client_secret, bytes)
    assert decode_stored_secret(provider.client_secret, key) == "update-secret"
