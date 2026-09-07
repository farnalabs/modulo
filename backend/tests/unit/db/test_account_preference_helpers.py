"""Unit tests for the account preferences write helpers (FAR-620 Phase 1, item 7).

``set_hitl_email_preference`` (the SINGLE writer of the ``hitl_email`` block)
and the migrated row-locked ``update_account_preferences`` — begin-AGNOSTIC
(they operate inside the caller's transaction and NEVER call ``begin()``),
serialise on the account row lock, merge per-top-level-key (sibling keys
preserved) and are 404-loud via ``AccountNotFoundError``.
"""

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from modulo.db.crud.account import (
    Account,
    AccountNotFoundError,
    set_hitl_email_preference,
    update_account_preferences,
)

_ACCOUNT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_OTHER_UUID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_UUID3 = uuid.UUID("00000000-0000-0000-0000-000000000003")


def _make_account(preferences: Any) -> MagicMock:
    account = MagicMock()
    account.preferences = preferences
    return account


def _mock_session(account: Any | None) -> AsyncMock:
    """Mimic the REST DI session (autobegin=False): ``begin`` EXISTS but is
    NEVER invoked by the helpers themselves — the caller opens the txn."""
    session = AsyncMock()
    session.get = AsyncMock(return_value=account)
    session.begin = MagicMock(side_effect=AssertionError("helper must never call begin()"))
    session.flush = AsyncMock()
    return session


class TestSetHitlEmailPreference:
    async def test_writes_only_the_hitl_email_key(self) -> None:
        account = _make_account({"theme": "dark"})
        session = _mock_session(account)
        merged = await set_hitl_email_preference(session, _ACCOUNT_ID, default=True)
        assert merged == {
            "theme": "dark",
            "hitl_email": {"default": True, "pipeline_overrides": {}},
        }
        assert account.preferences is merged
        session.get.assert_awaited_once_with(Account, _ACCOUNT_ID, with_for_update=True)

    async def test_row_lock_taken_with_for_update(self) -> None:
        account = _make_account({})
        session = _mock_session(account)
        await set_hitl_email_preference(session, _ACCOUNT_ID, default=False)
        session.get.assert_awaited_once_with(Account, _ACCOUNT_ID, with_for_update=True)

    async def test_begin_agnostic_inside_caller_transaction(self) -> None:
        """MCP session mode: the caller opened its own ``s.begin()`` (the
        ``_session`` wrapper); the helper must operate in that txn and must
        not call ``begin()`` itself."""
        account = _make_account({"theme": "dark"})
        session = _mock_session(account)
        merged = await set_hitl_email_preference(
            session, _ACCOUNT_ID, default=True, pipeline_overrides={str(_OTHER_UUID): True}
        )
        assert merged["hitl_email"] == {
            "default": True,
            "pipeline_overrides": {str(_OTHER_UUID): True},
        }
        session.begin.assert_not_called()

    async def test_pipeline_overrides_none_preserves_stored_overrides(self) -> None:
        account = _make_account(
            {"theme": "dark", "hitl_email": {"default": False, "pipeline_overrides": {str(_OTHER_UUID): True}}}
        )
        session = _mock_session(account)
        merged = await set_hitl_email_preference(session, _ACCOUNT_ID, default=True)
        assert merged["hitl_email"] == {
            "default": True,
            "pipeline_overrides": {str(_OTHER_UUID): True},
        }
        assert merged["theme"] == "dark"

    async def test_pipeline_overrides_replaces_atomically(self) -> None:
        account = _make_account({"hitl_email": {"default": False, "pipeline_overrides": {str(_OTHER_UUID): True}}})
        session = _mock_session(account)
        merged = await set_hitl_email_preference(
            session, _ACCOUNT_ID, default=False, pipeline_overrides={str(_UUID3): True}
        )
        assert merged["hitl_email"] == {
            "default": False,
            "pipeline_overrides": {str(_UUID3): True},
        }

    async def test_missing_account_is_404_loud(self) -> None:
        session = _mock_session(None)
        with pytest.raises(AccountNotFoundError):
            await set_hitl_email_preference(session, _ACCOUNT_ID, default=True)

    async def test_malformed_stored_preferences_normalise(self) -> None:
        account = _make_account("garbage")
        session = _mock_session(account)
        merged = await set_hitl_email_preference(session, _ACCOUNT_ID, default=True, pipeline_overrides={})
        assert merged == {"hitl_email": {"default": True, "pipeline_overrides": {}}}


class TestUpdateAccountPreferences:
    """The migrated sibling writer (PUT /me/settings, dashboard level)."""

    async def test_merges_per_top_level_key(self) -> None:
        account = _make_account({"theme": "light", "hitl_email": {"default": True, "pipeline_overrides": {}}})
        session = _mock_session(account)
        merged = await update_account_preferences(session, _ACCOUNT_ID, {"theme": "dark", "locale": "en-GB"})
        assert merged == {
            "theme": "dark",
            "locale": "en-GB",
            "hitl_email": {"default": True, "pipeline_overrides": {}},
        }
        assert account.preferences is merged
        assert session.get.await_args.kwargs.get("with_for_update") is True

    async def test_non_dict_preferences_treated_as_empty(self) -> None:
        account = _make_account(None)
        session = _mock_session(account)
        merged = await update_account_preferences(session, _ACCOUNT_ID, {"theme": "dark"})
        assert merged == {"theme": "dark"}

    async def test_missing_account_raises(self) -> None:
        session = _mock_session(None)
        with pytest.raises(AccountNotFoundError):
            await update_account_preferences(session, _ACCOUNT_ID, {"theme": "dark"})

    async def test_never_calls_begin_in_either_session_mode(self) -> None:
        """Both session modes (REST DI autobegin=False and the MCP
        ``s.begin()`` wrapper) are served by the same begin-agnostic body."""
        for preferences in ({}, {"theme": "x"}):
            session = _mock_session(_make_account(preferences))
            await update_account_preferences(session, _ACCOUNT_ID, {"locale": "en"})
            session.begin.assert_not_called()


class TestDatetimeRaceRegression:
    """The lost-update race the unlocked helper used to document: a settings
    write and a hitl_email write on the SAME account must not drop the
    other's top-level key. The lock + per-key merge makes the second writer
    read the first's committed blob."""

    async def test_settings_write_after_hitl_write_preserves_hitl_key(self) -> None:
        account = _make_account({})
        session = _mock_session(account)
        await set_hitl_email_preference(session, _ACCOUNT_ID, default=True)
        merged = await update_account_preferences(session, _ACCOUNT_ID, {"theme": "dark"})
        assert merged["hitl_email"] == {"default": True, "pipeline_overrides": {}}
        assert merged["theme"] == "dark"

    async def test_hitl_write_after_settings_write_preserves_settings_keys(self) -> None:
        account = _make_account({})
        session = _mock_session(account)
        await update_account_preferences(session, _ACCOUNT_ID, {"theme": "dark"})
        merged = await set_hitl_email_preference(
            session, _ACCOUNT_ID, default=False, pipeline_overrides={str(_OTHER_UUID): True}
        )
        assert merged["theme"] == "dark"
        assert merged["hitl_email"] == {
            "default": False,
            "pipeline_overrides": {str(_OTHER_UUID): True},
        }
