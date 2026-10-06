"""FAR-1523 — ``consume_authorization_code`` must compose with a caller-owned transaction.

The production token exchange (``mcp_server._exchange_authorization_code``)
runs the whole grant inside ``async with session_factory() as s, s.begin():``.
``consume_authorization_code`` used to open its OWN ``session.begin()`` inside
that active transaction, which raises
``sqlalchemy.exc.InvalidRequestError: A transaction is already begun on this
Session.`` — a ``SQLAlchemyError`` the ``_oauth_token`` handler turned into
HTTP 500 (``MSG_SESSION_CONTRACT``), so no client could obtain a token.

The unit tests that shipped green passed a ``MagicMock`` session, whose
``begin()`` context manager accepts everything. These tests therefore use a
REAL async SQLAlchemy session (aiosqlite in-memory) and open an OUTER
transaction around the call — the exact shape of the production caller.
"""

from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from modulo.auth.oauth import (
    InvalidGrantError,
    compute_pkce_challenge,
    consume_authorization_code,
    create_authorization_code,
    create_oauth_client,
)
from modulo.db.models.account import Account
from modulo.db.models.base import Base
from modulo.db.models.oauth_token import OAuthAuthorizationCode
from modulo.db.models.organisation import Organisation

_REDIRECT_URI = "http://localhost/callback"
_CODE_VERIFIER = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
_CODE_CHALLENGE = compute_pkce_challenge(_CODE_VERIFIER)

# Scoped create_all — unrelated models use Postgres-only constructs SQLite
# cannot render (same pattern as test_demo_login_endpoint).
_CONSUME_TABLES = {"organisations", "accounts", "oauth_clients", "oauth_authorization_codes"}


@pytest.fixture
async def session_factory() -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    engine = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with engine.begin() as conn:
        wanted = [t for t in Base.metadata.sorted_tables if t.name in _CONSUME_TABLES]
        await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=wanted))
    maker: async_sessionmaker[AsyncSession] = async_sessionmaker(engine, expire_on_commit=False)
    yield maker
    await engine.dispose()


async def _seed_client_and_code(
    maker: async_sessionmaker[AsyncSession],
    *,
    code_redirect_uri: str = _REDIRECT_URI,
) -> tuple[str, str, str]:
    """Seed an org, an account, an OAuth client and an unused auth code.

    ``code_redirect_uri`` is stored ON THE CODE row; it defaults to the
    client's allow-listed ``_REDIRECT_URI`` but may be set to a different value
    to exercise the stored-row redirect_uri mismatch branch.

    Returns ``(client_id, client_secret, authorization_code)``.
    """
    async with maker() as session, session.begin():
        org = Organisation(name="FAR-1523", slug="far-1523", settings_json={})
        session.add(org)
        await session.flush()

        account = Account(email="far1523@example.com", display_name="FAR-1523")
        session.add(account)
        await session.flush()

        client, client_secret = await create_oauth_client(
            session,
            org_id=org.id,
            name="txn-composition-client",
            scopes="trigger:run",
            redirect_uris=_REDIRECT_URI,
            created_by=account.id,
        )

        code = await create_authorization_code(
            session,
            client_id=client.client_id,
            org_id=org.id,
            scopes="trigger:run",
            redirect_uri=code_redirect_uri,
            account_id=account.id,
            code_challenge=_CODE_CHALLENGE,
        )

        # SQLite's DATETIME bind processor drops the UTC offset, so a value
        # written through the ORM round-trips NAIVE and the tz-aware
        # ``expires_at < datetime.now(UTC)`` comparison inside
        # ``consume_authorization_code`` would raise TypeError instead of
        # exercising the transaction path under test. Re-store with the offset
        # so the fixture matches Postgres ``timestamptz`` behaviour.
        expires_at = (datetime.now(UTC) + timedelta(minutes=10)).isoformat()
        await session.execute(
            text("UPDATE oauth_authorization_codes SET expires_at = :expires_at"),
            {"expires_at": expires_at},
        )

        return client.client_id, client_secret, code


async def _fetch_code(
    maker: async_sessionmaker[AsyncSession],
    code: str,
) -> OAuthAuthorizationCode:
    async with maker() as session:
        result = await session.execute(select(OAuthAuthorizationCode).where(OAuthAuthorizationCode.code == code))
        return result.scalar_one()


async def test_consume_composes_with_caller_owned_transaction(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Calling consume inside an OUTER ``session.begin()`` must not raise.

    Before FAR-1523 this raised
    ``InvalidRequestError: A transaction is already begun on this Session.``
    and the token endpoint answered HTTP 500.
    """
    client_id, client_secret, code = await _seed_client_and_code(session_factory)
    assert not (await _fetch_code(session_factory, code)).used

    async with session_factory() as session, session.begin():
        # If consume opened its own nested transaction this raises
        # sqlalchemy.exc.InvalidRequestError (a SQLAlchemyError) and the test
        # fails here — that IS the regression assertion.
        consumed = await consume_authorization_code(
            session,
            code=code,
            client_id=client_id,
            redirect_uri=_REDIRECT_URI,
            client_secret=client_secret,
            code_verifier=_CODE_VERIFIER,
        )
        assert consumed.used is True

    # The mark-used write committed with the caller's transaction.
    assert (await _fetch_code(session_factory, code)).used is True


async def test_consume_unknown_code_raises_invalid_grant(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An unknown code is rejected from the stored-row lookup, not a nested txn.

    Covers the ``auth_code is None`` arm of ``consume_authorization_code`` on
    the same caller-owned-transaction shape as production: the outer
    ``session.begin()`` stays open while the lookup returns nothing.
    """
    client_id, client_secret, _ = await _seed_client_and_code(session_factory)

    async with session_factory() as session, session.begin():
        with pytest.raises(InvalidGrantError, match="not found"):
            await consume_authorization_code(
                session,
                code="no-such-code",
                client_id=client_id,
                redirect_uri=_REDIRECT_URI,
                client_secret=client_secret,
                code_verifier=_CODE_VERIFIER,
            )


async def test_consume_code_stored_for_other_redirect_uri_raises(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A code whose STORED redirect_uri differs from the request is rejected.

    The requested ``_REDIRECT_URI`` is on the client's allow-list, so the
    pre-try ``check_redirect_uri`` gate passes and the mismatch must be caught
    from the stored code row (the arm that FAR-1523 re-indented).
    """
    client_id, client_secret, code = await _seed_client_and_code(
        session_factory,
        code_redirect_uri="http://localhost/other-callback",
    )

    async with session_factory() as session, session.begin():
        with pytest.raises(InvalidGrantError, match="redirect_uri mismatch"):
            await consume_authorization_code(
                session,
                code=code,
                client_id=client_id,
                redirect_uri=_REDIRECT_URI,
                client_secret=client_secret,
                code_verifier=_CODE_VERIFIER,
            )
