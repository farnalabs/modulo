"""Focused branch coverage for refused trigger delivery recording and /me routes."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError

from modulo.api import trigger_busy
from modulo.api.routes import me

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_ACCOUNT_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_TRIGGER_ID = uuid.UUID("00000000-0000-0000-0000-000000000003")
_SOURCE_EVENT_ID = uuid.UUID("00000000-0000-0000-0000-000000000004")


class _SessionContext:
    def __init__(self, session: AsyncMock) -> None:
        self.session = session

    async def __aenter__(self) -> AsyncMock:
        return self.session

    async def __aexit__(self, *_args: object) -> None:
        return None


def _fresh_session(*, source_hash: str | None = None) -> AsyncMock:
    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = source_hash
    session.execute = AsyncMock(return_value=result)
    session.begin = MagicMock(return_value=_SessionContext(session))
    return session


@pytest.mark.anyio
async def test_busy_recorder_carries_source_hash_and_stores_webhook_payload() -> None:
    session = _fresh_session(source_hash="source-hash")
    factory = MagicMock(return_value=_SessionContext(session))

    with (
        patch.object(trigger_busy, "async_sessionmaker", return_value=factory),
        patch.object(trigger_busy, "get_or_create_engine", return_value=MagicMock()),
        patch.object(trigger_busy, "get_settings", return_value=MagicMock()),
        patch.object(trigger_busy, "set_rls_org", new_callable=AsyncMock) as set_org,
    ):
        await trigger_busy.record_busy_delivery(
            trigger_id=_TRIGGER_ID,
            org_id=_ORG_ID,
            trigger_type="webhook",
            source_event_id=_SOURCE_EVENT_ID,
            raw_body=b'{"ok":true}',
            raw_payload={"ok": True},
        )

    added = [call.args[0] for call in session.add.call_args_list]
    event = next(item for item in added if isinstance(item, trigger_busy.TriggerEvent))
    payload = next(item for item in added if isinstance(item, trigger_busy.WebhookPayload))
    assert event.raw_payload_hash == "source-hash"
    assert event.validation_result == trigger_busy.BUSY_VALIDATION_RESULT
    assert payload.raw_body == b'{"ok":true}'
    assert payload.raw_payload == {"ok": True}
    set_org.assert_awaited_once_with(session, _ORG_ID)
    assert session.flush.await_count == 2


@pytest.mark.anyio
async def test_backpressure_recorder_includes_reason_only_when_present() -> None:
    session = _fresh_session()
    factory = MagicMock(return_value=_SessionContext(session))
    with (
        patch.object(trigger_busy, "async_sessionmaker", return_value=factory),
        patch.object(trigger_busy, "get_or_create_engine", return_value=MagicMock()),
        patch.object(trigger_busy, "get_settings", return_value=MagicMock()),
        patch.object(trigger_busy, "set_rls_org", new_callable=AsyncMock),
    ):
        await trigger_busy.record_backpressure_delivery(
            trigger_id=_TRIGGER_ID,
            org_id=_ORG_ID,
            trigger_type="webhook",
            payload_hash="payload",
            reason="queue_depth=4",
        )
        first = session.add.call_args.args[0]
        assert first.error_detail.endswith("(queue_depth=4)")
        await trigger_busy.record_backpressure_delivery(
            trigger_id=_TRIGGER_ID,
            org_id=_ORG_ID,
            trigger_type="webhook",
            payload_hash="payload",
        )
        second = session.add.call_args.args[0]
        assert second.error_detail == trigger_busy.BACKPRESSURE_ERROR_DETAIL


@pytest.mark.anyio
async def test_refused_delivery_reraises_cancellation_and_logs_other_failures(caplog: pytest.LogCaptureFixture) -> None:
    session = _fresh_session()
    session.flush = AsyncMock(side_effect=RuntimeError("db unavailable"))
    factory = MagicMock(return_value=_SessionContext(session))
    with (
        patch.object(trigger_busy, "async_sessionmaker", return_value=factory),
        patch.object(trigger_busy, "get_or_create_engine", return_value=MagicMock()),
        patch.object(trigger_busy, "get_settings", return_value=MagicMock()),
        patch.object(trigger_busy, "set_rls_org", new_callable=AsyncMock),
        caplog.at_level("ERROR"),
    ):
        await trigger_busy._record_refused_delivery(
            trigger_id=_TRIGGER_ID,
            org_id=_ORG_ID,
            trigger_type="webhook",
            validation_result="busy",
            error_detail="busy",
        )
    assert "record_delivery_failed" in caplog.text

    session.flush = AsyncMock(side_effect=asyncio.CancelledError)
    with (
        patch.object(trigger_busy, "async_sessionmaker", return_value=factory),
        patch.object(trigger_busy, "get_or_create_engine", return_value=MagicMock()),
        patch.object(trigger_busy, "get_settings", return_value=MagicMock()),
        patch.object(trigger_busy, "set_rls_org", new_callable=AsyncMock),
        pytest.raises(asyncio.CancelledError),
    ):
        await trigger_busy._record_refused_delivery(
            trigger_id=_TRIGGER_ID,
            org_id=_ORG_ID,
            trigger_type="webhook",
            validation_result="busy",
            error_detail="busy",
        )


class _Principal:
    account_id = _ACCOUNT_ID
    organisation_id = _ORG_ID


def _account(**values: object) -> MagicMock:
    account = MagicMock()
    account.password_hash = values.get("password_hash", "stored")
    account.must_change_password = values.get("must_change_password", True)
    account.preferences = values.get("preferences", {})
    return account


@pytest.mark.anyio
async def test_me_settings_direct_function_covers_single_preference_and_not_found() -> None:
    session = AsyncMock()
    session.begin = MagicMock(return_value=_SessionContext(session))
    account = _account(preferences={"theme": "dark"})
    with patch.object(me, "update_account_preferences", new_callable=AsyncMock, return_value={"locale": "en-GB"}):
        result = await me.update_user_settings(me.SettingsUpdate(locale="en-GB"), _Principal(), session)
    assert result == {"locale": "en-GB"}

    with (
        patch.object(me, "update_account_preferences", new_callable=AsyncMock, side_effect=me.AccountNotFoundError()),
        pytest.raises(HTTPException, match="Account not found"),
    ):
        await me.update_user_settings(me.SettingsUpdate(theme="light"), _Principal(), session)
    assert account.preferences == {"theme": "dark"}


@pytest.mark.anyio
async def test_me_update_skill_applies_every_optional_field() -> None:
    session = AsyncMock()
    session.begin = MagicMock(return_value=_SessionContext(session))
    skill = MagicMock()
    skill.created_at = datetime(2026, 1, 1, tzinfo=UTC)
    skill.updated_at = datetime(2026, 1, 1, tzinfo=UTC)
    with (
        patch.object(me, "set_rls_org", new_callable=AsyncMock),
        patch.object(me, "get_user_skill_or_404", new_callable=AsyncMock, return_value=skill),
    ):
        result = await me.update_user_skill(
            uuid.uuid4(),
            me.SkillUpdate(
                name="new",
                description="description",
                triggers=["trigger"],
                body="body",
                active=False,
            ),
            _Principal(),
            session,
        )
    assert result.name == "new"
    assert result.description == "description"
    assert result.triggers == ["trigger"]
    assert result.body == "body"
    assert result.active is False
    assert skill.name == "new"
    assert skill.description == "description"
    assert skill.triggers == ["trigger"]
    assert skill.body == "body"
    assert skill.active is False


@pytest.mark.anyio
async def test_me_change_password_rethrows_blacklist_db_error() -> None:
    session = AsyncMock()
    session.add = MagicMock()
    session.begin = MagicMock(return_value=_SessionContext(session))
    family = MagicMock(family_id=uuid.uuid4())
    with (
        patch.object(me, "get_account_by_id", new_callable=AsyncMock, return_value=_account()),
        patch.object(me, "verify_password", return_value=True),
        patch.object(me, "validate_password_strength"),
        patch.object(me, "hash_password", return_value="new-hash"),
        patch.object(me, "list_families_for_account", new_callable=AsyncMock, return_value=[family]),
        patch.object(me, "blacklist_family", new_callable=AsyncMock, side_effect=SQLAlchemyError("failed")),
        pytest.raises(HTTPException) as raised,
    ):
        await me.change_password(
            me.PasswordChangeRequest(current_password="old-password", new_password="new-password"),
            _Principal(),
            session,
        )
    assert raised.value.status_code == 503


@pytest.mark.anyio
async def test_me_change_password_audit_failure_is_logged_and_programming_error_maps(
    caplog: pytest.LogCaptureFixture,
) -> None:
    session = AsyncMock()
    session.add = MagicMock()
    session.begin = MagicMock(return_value=_SessionContext(session))
    family = MagicMock(family_id=uuid.uuid4())
    with (
        patch.object(me, "get_account_by_id", new_callable=AsyncMock, return_value=_account()),
        patch.object(me, "verify_password", return_value=True),
        patch.object(me, "validate_password_strength"),
        patch.object(me, "hash_password", return_value="new-hash"),
        patch.object(me, "list_families_for_account", new_callable=AsyncMock, return_value=[family]),
        patch.object(me, "blacklist_family", new_callable=AsyncMock),
        patch.object(me, "set_rls_org", new_callable=AsyncMock),
        patch("modulo.core.audit_logger.append_audit_event", new_callable=AsyncMock, side_effect=RuntimeError("audit")),
        caplog.at_level("ERROR"),
    ):
        result = await me.change_password(
            me.PasswordChangeRequest(current_password="old-password", new_password="new-password"),
            _Principal(),
            session,
        )
    assert result["detail"] == "Password changed successfully"
    assert "audit write failed" in caplog.text

    with (
        patch.object(
            me,
            "get_account_by_id",
            new_callable=AsyncMock,
            side_effect=ProgrammingError("x", {}, Exception()),
        ),
        pytest.raises(HTTPException) as raised,
    ):
        await me.change_password(
            me.PasswordChangeRequest(current_password="old-password", new_password="new-password"),
            _Principal(),
            session,
        )
    assert raised.value.status_code == 501
