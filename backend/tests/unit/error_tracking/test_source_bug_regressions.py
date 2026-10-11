"""Regression tests for three source defects found in ``core/error_tracking``.

FAR-1673 (security) — the email alerter fell back to the RAW CIPHERTEXT as the
SMTP password whenever ``decode_stored_secret_scoped`` failed.
FAR-1674 (fail-open) — ``check_missed_fire_alerts`` built ``AsyncRedis`` outside
its try/except, so a malformed ``redis_url`` raised and escaped instead of
degrading to the documented fail-open.

All new tests live in this module on purpose: it keeps this branch from
colliding with a parallel test-hygiene branch that edits the pre-existing
error-tracking test files.

The decrypt tests drive the REAL ``decode_stored_secret_scoped`` ->
``decode_stored_secret`` chain (Fernet key + token are real); the only mocked
collaborators are the SMTP sender, the settings object and the DB session.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cryptography.fernet import Fernet

import modulo.core.error_tracking as et
from modulo.core.error_tracking.alert_dispatcher import SmtpPasswordDecryptError, dispatch_alert
from modulo.core.error_tracking.alerting import AlertEngine, TriggeredAlert
from modulo.db.models.organisation import Organisation

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
_GROUP_ID = uuid.UUID("00000000-0000-0000-0000-0000000000b1")

# The org's configured key vs the key the stored token was actually encrypted
# with — the realistic "rotated / wrong FERNET_KEY" decrypt-failure shape.
_FERNET_KEY = Fernet.generate_key().decode()
_FOREIGN_FERNET_KEY = Fernet.generate_key().decode()


class _Settings:
    """Minimal Settings stand-in carrying every field the dispatcher reads."""

    def __init__(
        self,
        *,
        smtp_host: str = "smtp.default.example.com",
        smtp_password: str = "global-fallback-password",
        fernet_key: str = _FERNET_KEY,
    ) -> None:
        self.smtp_host = smtp_host
        self.smtp_port = 587
        self.smtp_username = "alerter"
        self.smtp_password = smtp_password
        self.email_from = "alerts@example.com"
        self.fernet_key = fernet_key

    def model_copy(self, *, update: dict[str, Any]) -> _Settings:
        clone = _Settings(
            smtp_host=self.smtp_host,
            smtp_password=self.smtp_password,
            fernet_key=self.fernet_key,
        )
        clone.smtp_port = self.smtp_port
        clone.smtp_username = self.smtp_username
        clone.email_from = self.email_from
        for key, value in update.items():
            setattr(clone, key, value)
        return clone


def _token_encrypted_with(key: str, plaintext: str = "smtp-relay-secret") -> str:
    return Fernet(key.encode()).encrypt(plaintext.encode()).decode()


def _alert() -> TriggeredAlert:
    return TriggeredAlert(
        rule_id=uuid.uuid4(),
        rule_name="SMTP Regression Rule",
        action_type="email",
        webhook_url=None,
        error_group_id=_GROUP_ID,
        fingerprint="fp-regression",
        level="critical",
        count=4,
        environment="prod",
    )


def _dispatch_session(stored_password: str | None) -> AsyncMock:
    """Session + org whose stored SMTP password is *stored_password*.

    ``None`` omits the org-level password so the dispatcher falls back to the
    global settings value. The session also satisfies the REAL
    ``decode_stored_secret_scoped`` preconditions (active transaction + a
    sqlite-dialect bind, which scopes via ``session.info`` and issues no SQL),
    so the decrypt exercised here is the production path rather than a mock.
    """
    session = AsyncMock()
    session.in_transaction = MagicMock(return_value=True)
    session.info = {}

    bind = MagicMock()
    bind.dialect.name = "sqlite"

    async def _get_bind() -> MagicMock:
        return bind

    session.get_bind = _get_bind

    org = MagicMock(spec=Organisation)
    email_cfg: dict[str, Any] = {"smtp_host": "smtp.org.example.com"}
    if stored_password is not None:
        email_cfg["smtp_password"] = stored_password
    org.settings_json = {"email": email_cfg}
    session.get = AsyncMock(return_value=org)

    account_id = uuid.uuid4()

    membership = MagicMock()
    membership.account_id = account_id
    membership.role = "admin"
    membership.deactivated_at = None

    account = MagicMock()
    account.id = account_id
    account.email = "admin@example.com"
    account.active = True

    def _result(rows: list[Any]) -> MagicMock:
        return MagicMock(scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=rows))))

    session.execute = AsyncMock(side_effect=[_result([membership]), _result([account])])
    return session


class TestSmtpPasswordDecryptFailure:
    """FAR-1673: an undecryptable stored password must fail the delivery."""

    async def test_undecryptable_stored_password_never_becomes_the_smtp_password(self) -> None:
        """A token that cannot be decrypted is CIPHERTEXT — never a password.

        Before the fix the dispatcher kept the raw stored value on decrypt
        failure and passed it to ``send_email`` as ``smtp_password``. The
        ciphertext being sent IS the defect, so asserting the sender was never
        called is the discriminating observation: on the old code ``send`` is
        called exactly once with ``smtp_password == token``.
        """
        foreign_token = _token_encrypted_with(_FOREIGN_FERNET_KEY)
        session = _dispatch_session(stored_password=foreign_token)

        with (
            patch("modulo.core.error_tracking.alert_dispatcher.get_settings", return_value=_Settings()),
            patch("modulo.core.error_tracking.alert_dispatcher.send_email") as send,
            pytest.raises(SmtpPasswordDecryptError, match="could not be decrypted"),
        ):
            await dispatch_alert(_ORG_ID, _alert(), session)

        send.assert_not_called()

    async def test_decrypt_failure_is_logged_before_the_typed_error_escapes(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        foreign_token = _token_encrypted_with(_FOREIGN_FERNET_KEY)
        session = _dispatch_session(stored_password=foreign_token)

        with (
            patch("modulo.core.error_tracking.alert_dispatcher.get_settings", return_value=_Settings()),
            patch("modulo.core.error_tracking.alert_dispatcher.send_email"),
            caplog.at_level("ERROR", logger="modulo.core.error_tracking.alert_dispatcher"),
            pytest.raises(SmtpPasswordDecryptError),
        ):
            await dispatch_alert(_ORG_ID, _alert(), session)

        assert any("alert.email_smtp_password_decrypt_failed" in rec.message for rec in caplog.records)

    async def test_decrypt_failure_records_a_delivery_failure_for_the_alert_loop(self) -> None:
        """The escaping typed error is what the alert loop turns into a metric."""
        foreign_token = _token_encrypted_with(_FOREIGN_FERNET_KEY)
        session = _dispatch_session(stored_password=foreign_token)
        alert = _alert()

        with (
            patch("modulo.core.error_tracking.alert_dispatcher.get_settings", return_value=_Settings()),
            patch("modulo.core.error_tracking.alert_dispatcher.send_email") as send,
            patch("modulo.core.error_tracking.alerting.record_error_alert"),
            patch("modulo.core.error_tracking.alerting.record_alert_delivery_failed") as failed,
        ):
            await AlertEngine().dispatch_all(_ORG_ID, [alert], session)

        failed.assert_called_once_with(str(alert.rule_id), "email")
        send.assert_not_called()

    async def test_decryptable_stored_password_is_sent_decrypted(self) -> None:
        """The happy path still routes the DECRYPTED plaintext to the sender."""
        plaintext = "decrypted-relay-password"
        session = _dispatch_session(stored_password=_token_encrypted_with(_FERNET_KEY, plaintext))

        with (
            patch("modulo.core.error_tracking.alert_dispatcher.get_settings", return_value=_Settings()),
            patch("modulo.core.error_tracking.alert_dispatcher.send_email", return_value=True) as send,
        ):
            await dispatch_alert(_ORG_ID, _alert(), session)

        send.assert_called_once()
        assert send.call_args.args[0].smtp_password == plaintext

    async def test_legacy_plaintext_password_still_sends(self) -> None:
        """A non-token stored value has nothing to decrypt — behaviour unchanged."""
        legacy = "legacy-plaintext-password"
        session = _dispatch_session(stored_password=legacy)

        with (
            patch("modulo.core.error_tracking.alert_dispatcher.get_settings", return_value=_Settings()),
            patch("modulo.core.error_tracking.alert_dispatcher.send_email", return_value=True) as send,
        ):
            await dispatch_alert(_ORG_ID, _alert(), session)

        send.assert_called_once()
        assert send.call_args.args[0].smtp_password == legacy

    async def test_plaintext_password_is_used_when_the_scoped_decrypt_guard_fails(self) -> None:
        """A plaintext value survives a scoped-decrypt guard failure.

        ``decode_stored_secret_scoped`` raises BEFORE decrypting when the
        session has no active transaction / RLS scope. The stored value here is
        legacy plaintext, so there is nothing to decrypt: the dispatcher must
        still send it as-is. The typed fatal error is reserved for a
        token-shaped value (FAR-1673), so a non-token value must not be routed
        into the ciphertext refusal.
        """
        legacy = "legacy-plaintext-password"
        session = _dispatch_session(stored_password=legacy)
        session.in_transaction = MagicMock(return_value=False)

        with (
            patch("modulo.core.error_tracking.alert_dispatcher.get_settings", return_value=_Settings()),
            patch("modulo.core.error_tracking.alert_dispatcher.send_email", return_value=True) as send,
        ):
            await dispatch_alert(_ORG_ID, _alert(), session)

        send.assert_called_once()
        assert send.call_args.args[0].smtp_password == legacy


class TestMissedFireRedisConstruction:
    """FAR-1674: a malformed redis_url must degrade to fail-open, not escape."""

    async def test_malformed_redis_url_fails_open_instead_of_raising(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """``AsyncRedis.from_url`` is built INSIDE the guarded block.

        Before the fix the construction sat above the ``try``: a malformed URL
        raised ``ValueError`` straight out of ``check_missed_fire_alerts`` and
        took the caller's cron tick with it. The fail-open contract for a Redis
        problem is "log + return 0", so the discriminating observations are the
        ``0`` return, the logged failure, and that the engine was never touched
        (proving the URL was rejected at client construction, before any query).
        """
        settings = MagicMock()
        settings.redis_url = "not-a-valid-redis-url"
        aengine = MagicMock()

        with (
            patch.object(et, "get_settings", return_value=settings),
            caplog.at_level("ERROR", logger="modulo.core.error_tracking"),
        ):
            emitted = await et.check_missed_fire_alerts(aengine, org_id=_ORG_ID)

        assert emitted == 0
        assert any("error_tracking.missed_fire_check_failed" in rec.message for rec in caplog.records)
        assert not any("error_tracking.missed_fire_redis_close_failed" in rec.message for rec in caplog.records)
        aengine.connect.assert_not_called()

    async def test_valid_redis_url_still_builds_and_closes_the_client(self) -> None:
        """The happy path is untouched: a client is built, used and closed."""
        settings = MagicMock()
        settings.redis_url = "redis://localhost:6379/0"
        aengine = MagicMock()
        aengine.connect.side_effect = RuntimeError("engine boom")
        redis_client = AsyncMock()
        redis_cls = MagicMock()
        redis_cls.from_url.return_value = redis_client

        with (
            patch.object(et, "get_settings", return_value=settings),
            patch.object(et, "AsyncRedis", redis_cls),
        ):
            emitted = await et.check_missed_fire_alerts(aengine, org_id=_ORG_ID)

        assert emitted == 0
        redis_cls.from_url.assert_called_once()
        redis_client.aclose.assert_awaited_once()

    async def test_redis_close_cancellation_is_not_swallowed(self) -> None:
        """A ``CancelledError`` during the guarded close propagates.

        The finally block closes the client and must re-raise cancellation so
        the caller's cancelled task stays cancelled — a swallowed
        ``CancelledError`` breaks task cancellation. The plain close-failure
        path (log-only) is covered separately; this pins the re-raise arm.
        """
        settings = MagicMock()
        settings.redis_url = "redis://localhost:6379/0"
        aengine = MagicMock()
        aengine.connect.side_effect = RuntimeError("engine boom")
        redis_client = AsyncMock()
        redis_client.aclose.side_effect = asyncio.CancelledError()
        redis_cls = MagicMock()
        redis_cls.from_url.return_value = redis_client

        with (
            patch.object(et, "get_settings", return_value=settings),
            patch.object(et, "AsyncRedis", redis_cls),
            pytest.raises(asyncio.CancelledError),
        ):
            await et.check_missed_fire_alerts(aengine, org_id=_ORG_ID)
