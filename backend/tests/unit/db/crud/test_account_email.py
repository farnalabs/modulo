"""Unit tests for email normalisation on the Account CRUD layer (FAR-584).

Emails are case-insensitive everywhere: ``create_account`` stores the
canonical (trimmed, lowercased) form and ``get_account_by_email`` compares
against ``lower(email)`` so case-variants of an address — including rows
written before migration 0176's backfill — resolve to the same account.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.db.crud.account import create_account, get_account_by_email
from modulo.util.emails import normalize_email


@pytest.fixture
def mock_session() -> AsyncMock:
    session = AsyncMock()
    session.add = MagicMock()
    return session


class TestNormalizeEmail:
    def test_lowercases_and_trims(self) -> None:
        assert normalize_email("  User@Example.COM ") == "user@example.com"

    def test_already_canonical_is_stable(self) -> None:
        assert normalize_email("user@example.com") == "user@example.com"


class TestCreateAccountNormalizesEmail:
    async def test_create_account_stores_canonical_email(self, mock_session: AsyncMock) -> None:
        with patch("modulo.db.crud.account.Account", return_value=MagicMock()) as mock_account:
            await create_account(
                mock_session,
                email="  ADMIN@Example.COM ",
                display_name="Admin",
                password_hash="hashed",
            )

        kwargs = mock_account.call_args.kwargs
        assert kwargs["email"] == "admin@example.com"

    async def test_create_account_canonical_email_passthrough(self, mock_session: AsyncMock) -> None:
        with patch("modulo.db.crud.account.Account", return_value=MagicMock()) as mock_account:
            await create_account(
                mock_session,
                email="user@example.com",
                display_name="User",
            )

        kwargs = mock_account.call_args.kwargs
        assert kwargs["email"] == "user@example.com"


class TestGetAccountByEmailCaseInsensitive:
    async def test_lookup_normalizes_submitted_email(self, mock_session: AsyncMock) -> None:
        mock_session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None)))

        await get_account_by_email(mock_session, "  ADMIN@Example.COM ")

        stmt = mock_session.execute.await_args.args[0]
        where = stmt.whereclause
        # The submitted value is canonicalised before it reaches the query.
        assert where.right.value == "admin@example.com"

    async def test_lookup_compares_against_lowercased_column(self, mock_session: AsyncMock) -> None:
        mock_session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None)))

        await get_account_by_email(mock_session, "admin@example.com")

        stmt = mock_session.execute.await_args.args[0]
        where = stmt.whereclause
        # The stored column side of the comparison is lower(email), so
        # pre-0176 mixed-case rows still match.
        assert "lower" in str(where.left).lower()

    async def test_lookup_returns_account(self, mock_session: AsyncMock) -> None:
        account = MagicMock()
        mock_session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=account)))

        result = await get_account_by_email(mock_session, "admin@example.com")

        assert result is account
