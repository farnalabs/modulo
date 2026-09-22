"""Real backing-store evidence for the Fernet secrets backend (FAR-1127).

Raises the at-rest secret-storage evidence (``cap-core-secrets-backend``)
above mock-only coverage by exercising the REAL ``FernetSecretsBackend``
end-to-end against an encrypted-at-rest store local to the process: an
on-disk SQLite file (``sqlite+aiosqlite``) created and owned by the test — no
external vault service and no testcontainer.

Everything is real: the cipher (``cryptography.fernet``), the ``secrets``
table, SQLAlchemy persistence, the factory, and the RLS-org setup via the
real ``set_rls_org`` helper (which uses the documented non-Postgres
``session.info["org_id"]`` tenant fallback for SQLite). Failure paths are
asserted against the real store: a missing secret key raises ``KeyError``, a
missing Fernet key is rejected by the real factory, tampered/undecryptable
ciphertext and a wrong key raise ``ValueError``, and no decrypted value ever
leaks into a log record or an exception message.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from modulo.core.secrets_backend import create_secrets_backend
from modulo.core.secrets_backend.fernet import FernetSecretsBackend
from modulo.db.models.base import Base
from modulo.db.models.organisation import Organisation
from modulo.db.models.secret import Secret
from modulo.db.rls import set_rls_org

# Distinctive value that must never appear in logs / exceptions / the store.
_SECRET_VALUE = "modulo-secret-42-<s3cr3t>-v0"
_ORG_ID = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_ORG_ID_OTHER = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


@pytest_asyncio.fixture(scope="module")
async def sqlite_secret_engine(tmp_path_factory: pytest.TempPathFactory) -> AsyncIterator[AsyncEngine]:
    """On-disk SQLite engine with the real ``secrets`` schema (FK target included)."""
    store_path = tmp_path_factory.mktemp("secret-store") / "secrets.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{store_path}", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync: Base.metadata.create_all(sync, tables=[Organisation.__table__, Secret.__table__])
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add(Organisation(id=_ORG_ID, name="Store Test Org", slug="store-test-org"))
        session.add(Organisation(id=_ORG_ID_OTHER, name="Store Test Org B", slug="store-test-org-b"))
        await session.commit()
    try:
        yield engine
    finally:
        await engine.dispose()


@asynccontextmanager
async def _secret_backend(
    engine: AsyncEngine,
    fernet_key: str,
    org_id: uuid.UUID = _ORG_ID,
) -> AsyncIterator[FernetSecretsBackend]:
    """Build a REAL backend bound to a real session with the org scope applied.

    The org context is applied through the real :func:`set_rls_org` helper —
    the production path — which on SQLite stores the tenant id in
    ``session.info`` (the documented non-Postgres fallback the backend reads
    when ``current_setting()`` is unavailable). The session is committed and
    closed when the block exits, so each ``async with`` block is a clean
    durable slice of the store.
    """
    factory = async_sessionmaker(engine, expire_on_commit=False)
    session = factory()
    try:
        async with session.begin():
            await set_rls_org(session, org_id)
        backend = FernetSecretsBackend(fernet_key=fernet_key, session=session)
        try:
            yield backend
            await session.commit()
        finally:
            await session.close()
    except Exception:
        await session.close()
        raise


async def test_roundtrip_persists_encrypted_at_rest(sqlite_secret_engine: AsyncEngine) -> None:
    """A secret round-trips through the REAL store; the bytes at rest are ciphertext.

    The persisted ``encrypted_value`` must not contain the plaintext, and the
    real cipher must decrypt it back to exactly the stored value. Deleting the
    key makes it unreadable again (KeyError).
    """
    key = Fernet.generate_key().decode()
    async with _secret_backend(sqlite_secret_engine, key) as backend:
        await backend.set_secret("github-token", _SECRET_VALUE)

    async with _secret_backend(sqlite_secret_engine, key) as reader:
        async with reader._session.begin():
            row = (
                await reader._session.execute(
                    select(Secret).where(Secret.key == "github-token", Secret.organisation_id == _ORG_ID)
                )
            ).scalar_one()
        stored = row.encrypted_value
        assert _SECRET_VALUE.encode() not in stored
        assert isinstance(stored, bytes)
        assert Fernet(key.encode()).decrypt(stored) == _SECRET_VALUE.encode()

        value = await reader.get_secret("github-token")
        assert value == _SECRET_VALUE

        await reader.delete_secret("github-token")
        with pytest.raises(KeyError, match="github-token"):
            await reader.get_secret("github-token")


async def test_cross_org_cannot_read_another_orgs_secret(sqlite_secret_engine: AsyncEngine) -> None:
    """Tenant isolation on the REAL store: another org's secret is unreadable.

    The RLS-org context is applied through the real ``set_rls_org`` path (the
    ``session.info["org_id"]`` non-Postgres fallback the backend reads on
    SQLite). A different org's backend sees no row and raises ``KeyError`` —
    the test fails loudly if the tenant filter is ever dropped.
    """
    key = Fernet.generate_key().decode()
    async with _secret_backend(sqlite_secret_engine, key, org_id=_ORG_ID) as owner:
        await owner.set_secret("tenant-secret", _SECRET_VALUE)

    async with _secret_backend(sqlite_secret_engine, key, org_id=_ORG_ID_OTHER) as intruder:
        with pytest.raises(KeyError, match="tenant-secret"):
            await intruder.get_secret("tenant-secret")


async def test_missing_key_raises_key_error(sqlite_secret_engine: AsyncEngine) -> None:
    """A key that was never stored raises ``KeyError`` against the real store."""
    key = Fernet.generate_key().decode()
    async with _secret_backend(sqlite_secret_engine, key) as backend:
        await backend.set_secret("present-key", "value-one")
        with pytest.raises(KeyError, match="does-not-exist"):
            await backend.get_secret("does-not-exist")


async def test_tampered_ciphertext_raises_value_error(sqlite_secret_engine: AsyncEngine) -> None:
    """Tampered/undecryptable ciphertext in the real store raises ``ValueError``."""
    key = Fernet.generate_key().decode()
    async with _secret_backend(sqlite_secret_engine, key) as backend:
        await backend.set_secret("tamper-me", _SECRET_VALUE)

    async with _secret_backend(sqlite_secret_engine, key) as reader:
        async with reader._session.begin():
            row = (await reader._session.execute(select(Secret).where(Secret.key == "tamper-me"))).scalar_one()
            row.encrypted_value = b"\xde\xad\xbe\xef" + b"\xff" * 8
        with pytest.raises(ValueError, match="Failed to decrypt secret") as excinfo:
            await reader.get_secret("tamper-me")
        assert _SECRET_VALUE not in str(excinfo.value)


async def test_wrong_key_raises_value_error(sqlite_secret_engine: AsyncEngine) -> None:
    """A backend holding the wrong Fernet key cannot decrypt stored ciphertext."""
    key = Fernet.generate_key().decode()
    other_key = Fernet.generate_key().decode()
    async with _secret_backend(sqlite_secret_engine, key) as backend:
        await backend.set_secret("rotate-me", _SECRET_VALUE)

    async with _secret_backend(sqlite_secret_engine, other_key) as wrong_backend:
        with pytest.raises(ValueError, match="Failed to decrypt secret") as excinfo:
            await wrong_backend.get_secret("rotate-me")
        assert _SECRET_VALUE not in str(excinfo.value)


async def test_decrypted_values_never_leak_into_logs(
    sqlite_secret_engine: AsyncEngine,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No decrypted value appears in any log record across success and failure paths.

    Exercises set/get/delete plus the tamper and wrong-key failure paths with
    capture at DEBUG across the ``modulo`` tree, and asserts the secret never
    shows up either in the rendered log text or in any individual record.
    """
    key = Fernet.generate_key().decode()
    other_key = Fernet.generate_key().decode()
    async with (
        _secret_backend(sqlite_secret_engine, key) as backend,
        _secret_backend(sqlite_secret_engine, other_key) as wrong_backend,
    ):
        with caplog.at_level(logging.DEBUG, logger="modulo"):
            await backend.set_secret("log-guard", _SECRET_VALUE)
            value = await backend.get_secret("log-guard")
            assert value == _SECRET_VALUE

            with pytest.raises(ValueError, match="Failed to decrypt secret"):
                await wrong_backend.get_secret("log-guard")

            await backend._session.commit()
            async with backend._session.begin():
                row = (await backend._session.execute(select(Secret).where(Secret.key == "log-guard"))).scalar_one()
                row.encrypted_value = b"\x00\x01\x02\x03" * 4
            with pytest.raises(ValueError, match="Failed to decrypt secret"):
                await backend.get_secret("log-guard")

            await backend.delete_secret("log-guard")
            with pytest.raises(KeyError, match="log-guard"):
                await backend.get_secret("log-guard")

    assert _SECRET_VALUE not in caplog.text
    assert all(_SECRET_VALUE not in record.getMessage() for record in caplog.records)


def test_factory_requires_fernet_key_for_default_backend() -> None:
    """A missing Fernet key is rejected by the real factory, typed as ``ValueError``."""
    with pytest.raises(ValueError, match="fernet_key is required"):
        create_secrets_backend(backend_name="fernet")
