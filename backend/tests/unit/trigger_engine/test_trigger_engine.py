"""Unit tests for TriggerEngine and helpers using mocked AsyncSession."""

import datetime
import hashlib
import hmac
import time
import uuid
from collections.abc import AsyncGenerator, Generator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from modulo.core.trigger_engine import (
    ConcurrentRunLimitError,
    DuplicateWebhookError,
    HmacValidationError,
    PipelineRateLimitError,
    ReplayNotFoundError,
    TimestampExpiredError,
    TriggerBusyError,
    TriggerEngine,
    TriggerInactiveError,
    TriggerNotFoundError,
    _apply_payload_mapping,
    _extract_field,
    _is_unique_violation,
    _sha256_hex,
    _verify_hmac,
    _verify_timestamp,
)
from modulo.db.models.trigger import Trigger

# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _replay_auth_headers() -> dict[str, str]:
    """Bearer JWT for the replay route (ADR 017: runner-or-HMAC).

    The route uses ``get_current_tenant_user_optional``, which decodes the
    Bearer directly — a token signed with the test secret_key is enough.
    """
    from modulo.auth.jwt import create_access_token

    token = create_access_token(
        "ci@test.local",
        _VALID_32,
        organisation_id=str(uuid.uuid4()),
        account_id=str(uuid.uuid4()),
        org_role="admin",
    )
    return {"Authorization": f"Bearer {token}"}


_VALID_TS: int


@pytest.fixture(autouse=True)
def refresh_valid_timestamp() -> None:
    global _VALID_TS
    _VALID_TS = int(time.time())


def _sha256_sig(body: bytes, secret: str, timestamp: int | None = None) -> str:
    payload = f"{timestamp}.".encode() + body if timestamp is not None else body
    return "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


def _make_trigger(
    *,
    active: bool = True,
    hmac_secret: str | None = None,
    payload_mapping: dict[str, str] | None = None,
    max_concurrent_runs: int = 5,
    accepted_events: list[str] | None = None,
    extra_config: dict[str, Any] | None = None,
) -> MagicMock:
    t = MagicMock(spec=Trigger)
    t.id = uuid.uuid4()
    t.pipeline_id = uuid.uuid4()
    t.organisation_id = uuid.uuid4()
    t.active = active
    config: dict[str, Any] = {}
    if hmac_secret is not None:
        config["hmac_secret"] = hmac_secret
    if payload_mapping is not None:
        config["payload_mapping"] = payload_mapping
    if accepted_events is not None:
        config["accepted_events"] = accepted_events
    if extra_config is not None:
        config.update(extra_config)
    t.config_json = config
    t.max_concurrent_runs = max_concurrent_runs
    return t


def _make_session(
    *,
    trigger: MagicMock | None = None,
    active_run_count: int = 0,
    dedup_exists: bool = False,
    pipeline_rate_limit: dict[str, Any] | None = None,
    recent_run_count: int = 0,
) -> AsyncMock:
    """Build a mocked session that returns the given trigger and run count."""
    session = AsyncMock()

    lock_result = MagicMock()
    lock_result.scalar_one.return_value = True

    trigger_result = MagicMock()
    trigger_result.scalar_one_or_none.return_value = trigger
    trigger_result.scalar_one.return_value = trigger

    dedup_result = MagicMock()
    dedup_result.scalar_one_or_none.return_value = MagicMock() if dedup_exists else None

    generic_result = MagicMock()

    count_result = MagicMock()
    count_result.scalar_one.return_value = active_run_count

    recent_count_result = MagicMock()
    recent_count_result.scalar_one.return_value = recent_run_count

    call_count = 0

    # Pipeline lookup for rate-limit config (call 6+). No rate limit by default.
    pipeline_result = MagicMock()
    pipeline_result.scalar_one_or_none.return_value = MagicMock()
    pipeline_result.scalar_one_or_none.return_value.rate_limit_config = pipeline_rate_limit

    async def _execute(stmt: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal call_count
        call_count += 1
        # Order: 1=advisory lock, 2=trigger lookup, 3=dedup SELECT, 4=dedup DELETE, 5=count active runs,
        #        6=pipeline lookup (rate limit), 7=recent rate-limited count, 8+=other
        if call_count == 1:
            return lock_result
        if call_count == 2:
            return trigger_result
        if call_count == 3:
            return dedup_result
        if call_count == 4:
            return generic_result
        if call_count == 5:
            return count_result
        if call_count == 6:
            return pipeline_result
        if call_count == 7:
            return recent_count_result
        return pipeline_result

    session.execute = _execute
    session.add = MagicMock()
    session.flush = AsyncMock()

    # Replace AsyncMock get_bind with sync MagicMock (Python 3.13+ AsyncMock returns coroutines)
    bind_mock = MagicMock()
    bind_mock.dialect.name = "postgresql"
    session.in_transaction = MagicMock(return_value=True)
    session.get_bind = MagicMock(return_value=bind_mock)

    nested_cm = AsyncMock()
    nested_cm.__aenter__ = AsyncMock(return_value=None)
    nested_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin_nested = MagicMock(return_value=nested_cm)

    return session


_ORG = uuid.UUID("00000000-0000-0000-0000-000000000001")
_SNAP = uuid.UUID("00000000-0000-0000-0000-000000000003")
_RAW_BODY = b'{"action": "opened", "number": 42}'
_RAW_PAYLOAD: dict[str, Any] = {"action": "opened", "number": 42}

_VALID_32 = "a" * 32


# ---------------------------------------------------------------------------
# TestClient fixture for webhook route tests
# ---------------------------------------------------------------------------


@pytest.fixture()
def webhook_client() -> Generator[TestClient, None, None]:
    import sys
    import types

    # Mock langchain_google_vertexai to prevent the import chain from
    # hanging on google.cloud.aiplatform file I/O during app import.
    _mock_lgv = types.ModuleType("langchain_google_vertexai")
    _mock_lgv.ChatVertexAI = type("ChatVertexAI", (), {})
    _was_in_sys = "langchain_google_vertexai" in sys.modules
    if not _was_in_sys:
        sys.modules["langchain_google_vertexai"] = _mock_lgv

    try:
        from modulo.api.dependencies import _get_engine, get_db_session
        from modulo.api.main import app
        from modulo.auth.dependencies import get_current_user
        from modulo.auth.jwt import AuthenticatedPrincipal
        from modulo.settings import Settings, get_settings
    finally:
        if not _was_in_sys:
            sys.modules.pop("langchain_google_vertexai", None)

    _fake_org_id = uuid.uuid4()
    _fake_user_id = uuid.uuid4()

    def _settings() -> Settings:
        return Settings(
            database_url="postgresql+asyncpg://localhost/test",
            secret_key=_VALID_32,
            fernet_key=_VALID_32,
            modulo_admin_password="pw",
            redis_url="",
        )

    def _principal() -> AuthenticatedPrincipal:
        return AuthenticatedPrincipal(
            username="ci@test.local",
            organisation_id=_fake_org_id,
            account_id=_fake_user_id,
            org_role="admin",
        )

    trigger_mock = MagicMock()
    trigger_mock.id = uuid.uuid4()
    trigger_mock.pipeline_id = uuid.uuid4()
    trigger_mock.active = True
    trigger_mock.config_json = {}
    trigger_mock.max_concurrent_runs = 5

    execute_result = MagicMock()
    execute_result.scalar_one_or_none.return_value = trigger_mock
    execute_result.scalar_one.return_value = trigger_mock

    snapshot_mock = MagicMock()
    snapshot_mock.id = uuid.uuid4()

    session = AsyncMock()
    bind = MagicMock()
    bind.dialect.name = "sqlite"
    session.in_transaction = MagicMock(return_value=True)
    session.get_bind = MagicMock(return_value=bind)
    session.info = {}
    session.execute = AsyncMock(return_value=execute_result)
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)

    async def _session() -> AsyncGenerator[AsyncMock, None]:
        yield session

    app.dependency_overrides[get_settings] = _settings
    app.dependency_overrides[get_db_session] = _session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = _principal

    with (
        patch("modulo.db.crud.pipeline_snapshot.create_snapshot_from_live_graph", return_value=snapshot_mock),
        patch("modulo.core.rate_limiter.RateLimiterRegistry.check", return_value=True),
    ):
        yield TestClient(app, raise_server_exceptions=False)

    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Pure helper function tests
# ---------------------------------------------------------------------------


def test_sha256_hex_is_hex_string() -> None:
    result = _sha256_hex(b"hello")
    assert len(result) == 64
    assert all(c in "0123456789abcdef" for c in result)


def test_sha256_hex_is_deterministic() -> None:
    assert _sha256_hex(b"x") == _sha256_hex(b"x")


class TestVerifyTimestamp:
    @pytest.mark.parametrize(
        "timestamp_input,expect_raises",
        [
            (lambda: str(int(time.time())), False),
            (lambda: str(int(time.time()) - 600), True),
            (lambda: str(int(time.time()) + 600), True),
            (lambda: None, True),
            (lambda: "not-a-number", True),
        ],
    )
    def test_verify_timestamp(self, timestamp_input, expect_raises) -> None:
        ts = timestamp_input() if callable(timestamp_input) else timestamp_input
        if expect_raises:
            with pytest.raises(TimestampExpiredError):
                _verify_timestamp(ts)
        else:
            result = _verify_timestamp(ts)
            assert isinstance(result, int)


@pytest.mark.parametrize(
    "secret,body,sig_maker,expected",
    [
        ("my-secret", b"payload", lambda b, s: (_sha256_sig(b, s, timestamp=int(time.time())), int(time.time())), True),
        ("my-secret", b"payload", lambda b, s: (_sha256_sig(b, s), None), True),
        ("secret", b"payload", lambda b, s: ("sha256=wrong", int(time.time())), False),
        ("secret", b"payload", lambda b, s: (None, int(time.time())), False),
        # Signature is bound to the timestamp: signing a different timestamp than the
        # one presented to _verify_hmac must fail (verifies the timestamp is part of the HMAC).
        ("s", b"x", lambda b, s: (_sha256_sig(b, s, timestamp=int(time.time()) - 600), int(time.time())), False),
        # Body mutation must invalidate the signature.
        (
            "s",
            b"original-body",
            lambda b, s: (_sha256_sig(b"tampered", s, timestamp=int(time.time())), int(time.time())),
            False,
        ),
    ],
)
def test_verify_hmac(secret, body, sig_maker, expected) -> None:
    sig, ts = sig_maker(body, secret)
    assert _verify_hmac(body, secret, sig, timestamp=ts) is expected


@pytest.mark.parametrize(
    "data,field_path,expected",
    [
        ({"a": 1}, "a", 1),
        ({"a": {"b": {"c": "deep"}}}, "a.b.c", "deep"),
        ({"a": 1}, "b", None),
        ({"a": "not-a-dict"}, "a.b", None),
    ],
)
def test_extract_field(data, field_path, expected) -> None:
    assert _extract_field(data, field_path) == expected


def test_apply_payload_mapping_empty_returns_raw() -> None:
    raw: dict[str, Any] = {"x": 1, "y": 2}
    assert _apply_payload_mapping(raw, {}) == raw


def test_apply_payload_mapping_extracts_fields() -> None:
    raw: dict[str, Any] = {"pr": {"number": 7, "title": "Fix bug"}}
    mapping = {"pr_number": "pr.number", "pr_title": "pr.title"}
    result = _apply_payload_mapping(raw, mapping)
    assert result == {"pr_number": 7, "pr_title": "Fix bug"}


def test_apply_payload_mapping_missing_path_gives_none() -> None:
    result = _apply_payload_mapping({"a": 1}, {"x": "missing.path"})
    assert result == {"x": None}


def test_apply_payload_mapping_returns_new_dict() -> None:
    raw: dict[str, Any] = {"x": 1}
    result = _apply_payload_mapping(raw, {})
    assert result == raw
    assert result is not raw


class TestIsUniqueViolation:
    @staticmethod
    def _integrity(orig: Exception) -> IntegrityError:
        return IntegrityError("stmt", {}, orig)

    def test_postgres_pgcode_23505(self) -> None:
        class _PgError(Exception):
            pgcode = "23505"

        assert _is_unique_violation(self._integrity(_PgError())) is True

    def test_postgres_non_unique_pgcode(self) -> None:
        class _PgError(Exception):
            pgcode = "23503"  # foreign key violation

        assert _is_unique_violation(self._integrity(_PgError())) is False

    def test_sqlite_unique_constraint_message(self) -> None:
        err = Exception("UNIQUE constraint failed: webhook_dedup_hash.key")
        assert _is_unique_violation(self._integrity(err)) is True

    def test_mariadb_duplicate_entry_1062(self) -> None:
        class _MySQLError(Exception):
            def __init__(self) -> None:
                super().__init__(1062, "Duplicate entry 'abc' for key 'PRIMARY'")

        assert _is_unique_violation(self._integrity(_MySQLError())) is True

    def test_other_integrity_error(self) -> None:
        assert _is_unique_violation(self._integrity(Exception("some other error"))) is False

    def test_orig_none(self) -> None:
        assert _is_unique_violation(IntegrityError("stmt", {}, None)) is False


class TestComputeRateLimitKey:
    def test_exact_mode_extracts_fields_sorted(self) -> None:
        config = {"key_fields": ["repo", "org"], "match_mode": "exact"}
        payload = {"org": "acme", "repo": "app", "other": 1}
        assert TriggerEngine._compute_rate_limit_key(payload, config) == '{"org": "acme", "repo": "app"}'

    def test_exact_missing_field_is_null(self) -> None:
        config = {"key_fields": ["repo"], "match_mode": "exact"}
        assert TriggerEngine._compute_rate_limit_key({}, config) == '{"repo": null}'

    def test_presence_mode_present(self) -> None:
        config = {"key_fields": ["repo"], "match_mode": "presence"}
        assert TriggerEngine._compute_rate_limit_key({"repo": "anything"}, config) == '{"repo": "__present__"}'

    def test_presence_mode_absent(self) -> None:
        config = {"key_fields": ["repo"], "match_mode": "presence"}
        assert TriggerEngine._compute_rate_limit_key({}, config) == '{"repo": null}'

    def test_no_key_fields_is_empty_object(self) -> None:
        assert TriggerEngine._compute_rate_limit_key({"a": 1}, {}) == "{}"


# ---------------------------------------------------------------------------
# TriggerEngine.handle_webhook — happy path
# ---------------------------------------------------------------------------


async def test_handle_webhook_success_no_hmac() -> None:
    trigger = _make_trigger()
    session = _make_session(trigger=trigger, active_run_count=0)

    run_mock = MagicMock()
    run_mock.id = uuid.uuid4()

    with (
        patch("modulo.core.trigger_engine.create_run", return_value=run_mock),
        patch("modulo.core.trigger_engine.time.time", return_value=_VALID_TS),
    ):
        result = await TriggerEngine().handle_webhook(
            session,
            trigger_id=trigger.id,
            org_id=_ORG,
            raw_body=_RAW_BODY,
            raw_payload=_RAW_PAYLOAD,
            hmac_signature=None,
            modulo_timestamp=str(_VALID_TS),
            snapshot_id=_SNAP,
        )

    run, te, _payload = result
    assert run is run_mock
    assert te.validation_result == "accepted"


async def test_handle_webhook_success_with_hmac() -> None:
    secret = "test-secret"
    body = _RAW_BODY
    ts = _VALID_TS
    sig = _sha256_sig(body, secret, timestamp=ts)
    trigger = _make_trigger(hmac_secret=secret)
    session = _make_session(trigger=trigger, active_run_count=0)

    run_mock = MagicMock()
    run_mock.id = uuid.uuid4()

    with (
        patch("modulo.core.trigger_engine.create_run", return_value=run_mock),
        patch("modulo.core.trigger_engine.time.time", return_value=ts),
    ):
        run, _, _ = await TriggerEngine().handle_webhook(
            session,
            trigger_id=trigger.id,
            org_id=_ORG,
            raw_body=body,
            raw_payload=_RAW_PAYLOAD,
            hmac_signature=sig,
            modulo_timestamp=str(ts),
            snapshot_id=_SNAP,
        )

    assert run is run_mock


async def test_handle_webhook_applies_payload_mapping() -> None:
    mapping = {"action": "action", "pr_num": "number"}
    trigger = _make_trigger(payload_mapping=mapping)
    session = _make_session(trigger=trigger, active_run_count=0)

    run_mock = MagicMock()
    run_mock.id = uuid.uuid4()

    with (
        patch("modulo.core.trigger_engine.create_run", return_value=run_mock) as mock_create,
        patch("modulo.core.trigger_engine.time.time", return_value=_VALID_TS),
    ):
        _, _, input_payload = await TriggerEngine().handle_webhook(
            session,
            trigger_id=trigger.id,
            org_id=_ORG,
            raw_body=_RAW_BODY,
            raw_payload=_RAW_PAYLOAD,
            hmac_signature=None,
            modulo_timestamp=str(_VALID_TS),
            snapshot_id=_SNAP,
        )

    called_payload = mock_create.call_args.kwargs["input_payload"]
    assert called_payload == {"action": "opened", "pr_num": 42}
    assert input_payload == called_payload


# ---------------------------------------------------------------------------
# TriggerEngine.handle_webhook — validation failures
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "trigger_overrides,session_overrides,hmac_sig,mod_ts_factory,expected_exc,extra_assert",
    [
        ({}, {"trigger": None}, None, lambda: str(int(time.time())), TriggerNotFoundError, None),
        ({"active": False}, {}, None, lambda: str(int(time.time())), TriggerInactiveError, None),
        ({}, {}, None, lambda: None, TimestampExpiredError, None),
        ({"hmac_secret": "secret"}, {}, "sha256=wrong", lambda: str(int(time.time())), HmacValidationError, None),
        ({"hmac_secret": "secret"}, {}, None, lambda: str(int(time.time())), HmacValidationError, None),
        ({}, {"dedup_exists": True}, None, lambda: str(int(time.time())), DuplicateWebhookError, None),
    ],
)
async def test_handle_webhook_validation_raises(
    trigger_overrides, session_overrides, hmac_sig, mod_ts_factory, expected_exc, extra_assert
) -> None:
    session_kwargs = dict(session_overrides)
    session_trigger = session_kwargs.pop("trigger", None)
    trigger = _make_trigger(**trigger_overrides)
    if session_trigger is None and "trigger" in session_overrides:
        session = _make_session(trigger=None, **session_kwargs)
        trigger_id = uuid.uuid4()
    else:
        session = _make_session(trigger=trigger, **session_kwargs)
        trigger_id = trigger.id
    mod_ts = mod_ts_factory() if callable(mod_ts_factory) else mod_ts_factory
    with pytest.raises(expected_exc) as exc_info:
        await TriggerEngine().handle_webhook(
            session,
            trigger_id=trigger_id,
            org_id=_ORG,
            raw_body=_RAW_BODY,
            raw_payload=_RAW_PAYLOAD,
            hmac_signature=hmac_sig,
            modulo_timestamp=mod_ts,
            snapshot_id=_SNAP,
        )
    if extra_assert is not None:
        assert extra_assert(exc_info)


@pytest.mark.parametrize(
    "trigger_overrides,session_overrides,hmac_sig,mod_ts_factory,expected_exc,expected_vr",
    [
        ({}, {}, None, lambda: str(int(time.time()) - 600), TimestampExpiredError, "timestamp_expired"),
        (
            {"hmac_secret": "secret"},
            {},
            "sha256=bad",
            lambda: str(int(time.time())),
            HmacValidationError,
            "hmac_failed",
        ),
        ({}, {"dedup_exists": True}, None, lambda: str(int(time.time())), DuplicateWebhookError, "deduplicated"),
        (
            {"max_concurrent_runs": 1},
            {"active_run_count": 1},
            None,
            lambda: str(int(time.time())),
            None,  # no longer raises - run is queued
            "concurrency_limit_reached_queued",
        ),
    ],
)
async def test_handle_webhook_logs_trigger_event(
    trigger_overrides, session_overrides, hmac_sig, mod_ts_factory, expected_exc, expected_vr
) -> None:
    trigger = _make_trigger(**trigger_overrides)
    session = _make_session(trigger=trigger, **session_overrides)
    mod_ts = mod_ts_factory() if callable(mod_ts_factory) else mod_ts_factory
    if expected_exc is not None:
        with pytest.raises(expected_exc):
            await TriggerEngine().handle_webhook(
                session,
                trigger_id=trigger.id,
                org_id=_ORG,
                raw_body=_RAW_BODY,
                raw_payload=_RAW_PAYLOAD,
                hmac_signature=hmac_sig,
                modulo_timestamp=mod_ts,
                snapshot_id=_SNAP,
            )
    else:
        await TriggerEngine().handle_webhook(
            session,
            trigger_id=trigger.id,
            org_id=_ORG,
            raw_body=_RAW_BODY,
            raw_payload=_RAW_PAYLOAD,
            hmac_signature=hmac_sig,
            modulo_timestamp=mod_ts,
            snapshot_id=_SNAP,
        )
    session.add.assert_called()
    found = any(getattr(c[0][0], "validation_result", None) == expected_vr for c in session.add.call_args_list)
    assert found


async def test_handle_webhook_busy_lock_not_acquired() -> None:
    """When the advisory lock is already held, handle_webhook must raise TriggerBusyError."""
    session = _make_session(trigger=_make_trigger())
    lock_result = MagicMock()
    lock_result.scalar_one.return_value = False
    session.execute = AsyncMock(return_value=lock_result)

    with pytest.raises(TriggerBusyError):
        await TriggerEngine().handle_webhook(
            session,
            trigger_id=uuid.uuid4(),
            org_id=_ORG,
            raw_body=_RAW_BODY,
            raw_payload=_RAW_PAYLOAD,
            hmac_signature=None,
            modulo_timestamp=str(_VALID_TS),
            snapshot_id=_SNAP,
        )
    session.add.assert_not_called()


async def test_handle_webhook_event_type_not_accepted() -> None:
    """accepted_events configured but payload has no matching event -> RuntimeError + event logged."""
    trigger = _make_trigger(accepted_events=["pull_request"])
    session = _make_session(trigger=trigger, active_run_count=0)

    with (
        patch("modulo.core.trigger_engine.create_run", return_value=MagicMock(id=uuid.uuid4())),
        pytest.raises(RuntimeError, match="none of the accepted event types"),
    ):
        await TriggerEngine().handle_webhook(
            session,
            trigger_id=trigger.id,
            org_id=_ORG,
            raw_body=_RAW_BODY,
            raw_payload={"action": "opened"},
            hmac_signature=None,
            modulo_timestamp=str(_VALID_TS),
            snapshot_id=_SNAP,
        )

    assert any(
        getattr(c[0][0], "validation_result", None) == "event_type_not_accepted" for c in session.add.call_args_list
    )


async def test_handle_webhook_event_type_accepted_passes() -> None:
    """A matching accepted_events key passes validation and creates a run."""
    trigger = _make_trigger(accepted_events=["pull_request"])
    session = _make_session(trigger=trigger, active_run_count=0)
    raw_payload = {"action": "opened", "pull_request": {"number": 7}}

    run_mock = MagicMock(id=uuid.uuid4())
    with (
        patch("modulo.core.trigger_engine.create_run", return_value=run_mock),
        patch("modulo.core.trigger_engine.time.time", return_value=_VALID_TS),
    ):
        run, te, _ = await TriggerEngine().handle_webhook(
            session,
            trigger_id=trigger.id,
            org_id=_ORG,
            raw_body=b'{"action": "opened", "pull_request": {"number": 7}}',
            raw_payload=raw_payload,
            hmac_signature=None,
            modulo_timestamp=str(_VALID_TS),
            snapshot_id=_SNAP,
        )

    assert run is run_mock
    assert te.validation_result == "accepted"


async def test_handle_webhook_rate_limit_exceeded() -> None:
    """Pipeline rate limit exceeded -> PipelineRateLimitError + rate_limited event logged."""
    trigger = _make_trigger()
    session = _make_session(
        trigger=trigger,
        active_run_count=0,
        pipeline_rate_limit={"max_triggers": 1, "window_seconds": 3600},
        recent_run_count=1,
    )

    with (
        patch("modulo.core.trigger_engine.create_run", return_value=MagicMock(id=uuid.uuid4())),
        pytest.raises(PipelineRateLimitError) as exc_info,
    ):
        await TriggerEngine().handle_webhook(
            session,
            trigger_id=trigger.id,
            org_id=_ORG,
            raw_body=_RAW_BODY,
            raw_payload=_RAW_PAYLOAD,
            hmac_signature=None,
            modulo_timestamp=str(_VALID_TS),
            snapshot_id=_SNAP,
        )

    assert exc_info.value.pipeline_id == trigger.pipeline_id
    assert exc_info.value.max_triggers == 1
    assert exc_info.value.window_seconds == 3600
    assert any(getattr(c[0][0], "validation_result", None) == "rate_limited" for c in session.add.call_args_list)


async def test_handle_webhook_rate_limit_pass_through_sets_key() -> None:
    """Rate limit not exceeded -> run created with rate_limit_key from key_fields."""
    trigger = _make_trigger()
    raw_payload = {"repo": "acme/app", "action": "opened"}
    session = _make_session(
        trigger=trigger,
        active_run_count=0,
        pipeline_rate_limit={
            "max_triggers": 10,
            "window_seconds": 3600,
            "key_fields": ["repo"],
            "match_mode": "exact",
        },
        recent_run_count=1,
    )

    with (
        patch("modulo.core.trigger_engine.create_run", return_value=MagicMock(id=uuid.uuid4())) as mock_create,
        patch("modulo.core.trigger_engine.time.time", return_value=_VALID_TS),
    ):
        await TriggerEngine().handle_webhook(
            session,
            trigger_id=trigger.id,
            org_id=_ORG,
            raw_body=_RAW_BODY,
            raw_payload=raw_payload,
            hmac_signature=None,
            modulo_timestamp=str(_VALID_TS),
            snapshot_id=_SNAP,
        )

    assert mock_create.await_count == 1
    assert mock_create.call_args.kwargs["rate_limit_key"] == '{"repo": "acme/app"}'


# ---------------------------------------------------------------------------
# TriggerEngine.replay_event — unit tests
# ---------------------------------------------------------------------------


def _make_replay_session(
    *,
    event: MagicMock | None = None,
    trigger: MagicMock | None = None,
    stored_payload: MagicMock | None = None,
    active_run_count: int = 0,
    lock_acquired: bool = True,
    pipeline_rate_limit: dict[str, Any] | None = None,
    recent_run_count: int = 0,
) -> AsyncMock:
    """Build a mocked session for replay_event's query order.

    Query order: 1=TriggerEvent lookup, 2=advisory lock, 3=Trigger lookup,
    4=WebhookPayload lookup, 5=active-run count, 6=pipeline lookup,
    7=recent rate-limited count, 8+=other.
    """
    session = AsyncMock()

    event_result = MagicMock()
    event_result.scalar_one_or_none.return_value = event

    lock_result = MagicMock()
    lock_result.scalar_one.return_value = lock_acquired

    trigger_result = MagicMock()
    trigger_result.scalar_one_or_none.return_value = trigger

    payload_result = MagicMock()
    payload_result.scalar_one_or_none.return_value = stored_payload

    count_result = MagicMock()
    count_result.scalar_one.return_value = active_run_count

    recent_count_result = MagicMock()
    recent_count_result.scalar_one.return_value = recent_run_count

    pipeline_result = MagicMock()
    pipeline_result.scalar_one_or_none.return_value = MagicMock()
    pipeline_result.scalar_one_or_none.return_value.rate_limit_config = pipeline_rate_limit

    call_count = 0

    async def _execute(stmt: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return event_result
        if call_count == 2:
            return lock_result
        if call_count == 3:
            return trigger_result
        if call_count == 4:
            return payload_result
        if call_count == 5:
            return count_result
        if call_count == 6:
            return pipeline_result
        if call_count == 7:
            return recent_count_result
        return pipeline_result

    session.execute = _execute
    session.add = MagicMock()
    session.flush = AsyncMock()
    return session


def _make_stored_payload(**overrides: Any) -> MagicMock:
    stored = MagicMock()
    stored.raw_body = overrides.get("raw_body", _RAW_BODY)
    stored.raw_payload = overrides.get("raw_payload", _RAW_PAYLOAD)
    return stored


async def test_replay_event_success() -> None:
    trigger = _make_trigger()
    event = MagicMock()
    event.id = uuid.uuid4()
    event.trigger_id = trigger.id
    session = _make_replay_session(
        event=event,
        trigger=trigger,
        stored_payload=_make_stored_payload(),
        active_run_count=0,
    )

    run_mock = MagicMock(id=uuid.uuid4())
    with patch("modulo.core.trigger_engine.create_run", return_value=run_mock):
        run, te, input_payload = await TriggerEngine().replay_event(
            session,
            event_id=event.id,
            org_id=_ORG,
            snapshot_id=_SNAP,
        )

    assert run is run_mock
    assert te.validation_result == "accepted"
    assert input_payload == _RAW_PAYLOAD
    assert any(getattr(c[0][0], "validation_result", None) == "accepted" for c in session.add.call_args_list)


async def test_replay_event_event_not_found() -> None:
    session = _make_replay_session(event=None, trigger=_make_trigger())
    with pytest.raises(ReplayNotFoundError):
        await TriggerEngine().replay_event(
            session,
            event_id=uuid.uuid4(),
            org_id=_ORG,
            snapshot_id=_SNAP,
        )


async def test_replay_event_trigger_not_found() -> None:
    event = MagicMock()
    event.id = uuid.uuid4()
    event.trigger_id = uuid.uuid4()
    session = _make_replay_session(event=event, trigger=None)
    with pytest.raises(TriggerNotFoundError):
        await TriggerEngine().replay_event(
            session,
            event_id=event.id,
            org_id=_ORG,
            snapshot_id=_SNAP,
        )


async def test_replay_event_trigger_inactive() -> None:
    trigger = _make_trigger(active=False)
    event = MagicMock()
    event.id = uuid.uuid4()
    event.trigger_id = trigger.id
    session = _make_replay_session(event=event, trigger=trigger, stored_payload=_make_stored_payload())
    with pytest.raises(TriggerInactiveError):
        await TriggerEngine().replay_event(
            session,
            event_id=event.id,
            org_id=_ORG,
            snapshot_id=_SNAP,
        )


async def test_replay_event_stored_payload_missing() -> None:
    trigger = _make_trigger()
    event = MagicMock()
    event.id = uuid.uuid4()
    event.trigger_id = trigger.id
    session = _make_replay_session(event=event, trigger=trigger, stored_payload=None)
    with pytest.raises(ReplayNotFoundError):
        await TriggerEngine().replay_event(
            session,
            event_id=event.id,
            org_id=_ORG,
            snapshot_id=_SNAP,
        )


async def test_replay_event_busy_lock_not_acquired() -> None:
    trigger = _make_trigger()
    event = MagicMock()
    event.id = uuid.uuid4()
    event.trigger_id = trigger.id
    session = _make_replay_session(event=event, trigger=trigger, lock_acquired=False)
    with pytest.raises(TriggerBusyError):
        await TriggerEngine().replay_event(
            session,
            event_id=event.id,
            org_id=_ORG,
            snapshot_id=_SNAP,
        )


async def test_replay_event_concurrency_limit() -> None:
    trigger = _make_trigger(max_concurrent_runs=2)
    event = MagicMock()
    event.id = uuid.uuid4()
    event.trigger_id = trigger.id
    session = _make_replay_session(
        event=event,
        trigger=trigger,
        stored_payload=_make_stored_payload(),
        active_run_count=2,
    )
    with pytest.raises(ConcurrentRunLimitError):
        await TriggerEngine().replay_event(
            session,
            event_id=event.id,
            org_id=_ORG,
            snapshot_id=_SNAP,
        )


async def test_replay_event_event_type_not_accepted() -> None:
    trigger = _make_trigger(accepted_events=["pull_request"])
    event = MagicMock()
    event.id = uuid.uuid4()
    event.trigger_id = trigger.id
    session = _make_replay_session(
        event=event,
        trigger=trigger,
        stored_payload=_make_stored_payload(raw_payload={"action": "opened"}),
        active_run_count=0,
    )
    with pytest.raises(RuntimeError, match="none of the accepted event types"):
        await TriggerEngine().replay_event(
            session,
            event_id=event.id,
            org_id=_ORG,
            snapshot_id=_SNAP,
        )


async def test_replay_event_rate_limit_exceeded() -> None:
    trigger = _make_trigger()
    event = MagicMock()
    event.id = uuid.uuid4()
    event.trigger_id = trigger.id
    session = _make_replay_session(
        event=event,
        trigger=trigger,
        stored_payload=_make_stored_payload(),
        active_run_count=0,
        pipeline_rate_limit={"max_triggers": 1, "window_seconds": 3600},
        recent_run_count=1,
    )
    with pytest.raises(PipelineRateLimitError):
        await TriggerEngine().replay_event(
            session,
            event_id=event.id,
            org_id=_ORG,
            snapshot_id=_SNAP,
        )


# ---------------------------------------------------------------------------
# TriggerEngine.schedule_polling_trigger
# ---------------------------------------------------------------------------


async def test_schedule_polling_trigger_default_interval() -> None:
    trigger = _make_trigger()
    session = AsyncMock()
    session.flush = AsyncMock()

    await TriggerEngine().schedule_polling_trigger(session, trigger=trigger, org_id=_ORG)

    assert trigger.next_fire_at is not None
    delta = (trigger.next_fire_at - datetime.datetime.now(datetime.UTC)).total_seconds()
    assert 55 <= delta <= 65
    session.flush.assert_awaited_once()


async def test_schedule_polling_trigger_custom_interval() -> None:
    trigger = _make_trigger(extra_config={"poll_interval_seconds": 120})
    session = AsyncMock()
    session.flush = AsyncMock()

    await TriggerEngine().schedule_polling_trigger(session, trigger=trigger, org_id=_ORG)

    delta = (trigger.next_fire_at - datetime.datetime.now(datetime.UTC)).total_seconds()
    assert 115 <= delta <= 125


@pytest.mark.parametrize("bad_interval", [0, -5, "10"])
async def test_schedule_polling_trigger_invalid_interval(bad_interval: Any) -> None:
    trigger = _make_trigger(extra_config={"poll_interval_seconds": bad_interval})
    with pytest.raises(ValueError, match="poll_interval_seconds must be >= 1"):
        await TriggerEngine().schedule_polling_trigger(AsyncMock(), trigger=trigger, org_id=_ORG)


async def test_schedule_polling_trigger_none_interval_defaults() -> None:
    trigger = _make_trigger(extra_config={"poll_interval_seconds": None})
    session = AsyncMock()
    session.flush = AsyncMock()

    await TriggerEngine().schedule_polling_trigger(session, trigger=trigger, org_id=_ORG)

    assert trigger.next_fire_at is not None
    session.flush.assert_awaited_once()


# ---------------------------------------------------------------------------
# TriggerEngine.cleanup_expired_payloads
# ---------------------------------------------------------------------------


async def test_cleanup_expired_payloads_deletes_expired() -> None:
    session = AsyncMock()
    expired_result = MagicMock()
    expired_result.scalars.return_value.all.return_value = [uuid.uuid4(), uuid.uuid4()]
    session.execute = AsyncMock(return_value=expired_result)

    count = await TriggerEngine.cleanup_expired_payloads(session)
    assert count == 2
    assert session.execute.await_count == 2  # select then delete


async def test_cleanup_expired_payloads_none_expired() -> None:
    session = AsyncMock()
    empty_result = MagicMock()
    empty_result.scalars.return_value.all.return_value = []
    session.execute = AsyncMock(return_value=empty_result)

    count = await TriggerEngine.cleanup_expired_payloads(session)
    assert count == 0
    assert session.execute.await_count == 1  # only the select runs


# ---------------------------------------------------------------------------
# Webhook API route — integration smoke tests using TestClient
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "patch_target,mock_factory,expected_status,expected_body,extra_headers,body_kwargs",
    [
        (
            "handle_webhook",
            lambda tid, eid: AsyncMock(side_effect=TriggerNotFoundError(tid)),
            404,
            {"detail": "Trigger not found"},
            {},
            {"json": {"action": "ping"}},
        ),
        (
            "handle_webhook",
            lambda: AsyncMock(side_effect=TimestampExpiredError()),
            400,
            {"detail": "X-Modulo-Timestamp is outside the ±300s replay window"},
            {},
            {"json": {"action": "push"}},
        ),
        (
            "handle_webhook",
            lambda: AsyncMock(side_effect=HmacValidationError()),
            401,
            {"detail": "HMAC signature verification failed"},
            {"X-Modulo-Webhook-Secret": "sha256=bad"},
            {"json": {"action": "push"}},
        ),
        (
            "handle_webhook",
            lambda: AsyncMock(side_effect=DuplicateWebhookError("abc123")),
            400,
            {"detail": "Duplicate webhook payload"},
            {},
            {"json": {"action": "push"}},
        ),
        (
            "handle_webhook",
            lambda tid, eid: AsyncMock(side_effect=ConcurrentRunLimitError(tid, 3)),
            429,
            {"detail": "Concurrent run limit of 3 reached"},
            {},
            {"json": {"action": "push"}},
        ),
        (
            None,
            None,
            400,
            {"detail": "Request body must be a JSON object"},
            {"Content-Type": "application/json"},
            {"content": b"not-json"},
        ),
        (
            "handle_webhook",
            lambda: AsyncMock(return_value=(MagicMock(id=uuid.uuid4()), MagicMock(), {"key": "val"})),
            202,
            {"status": "accepted"},
            {},
            {"json": {"action": "push"}},
        ),
        (
            "replay_event",
            lambda: AsyncMock(return_value=(MagicMock(id=uuid.uuid4()), MagicMock(), {"key": "val"})),
            202,
            {"status": "accepted"},
            _replay_auth_headers(),
            {},
        ),
        (
            "replay_event",
            lambda tid, eid: AsyncMock(side_effect=ReplayNotFoundError(eid)),
            404,
            {"detail": "Trigger event not found"},
            _replay_auth_headers(),
            {},
        ),
    ],
)
def test_webhook_route(
    webhook_client: TestClient,
    patch_target,
    mock_factory,
    expected_status,
    expected_body,
    extra_headers,
    body_kwargs,
) -> None:
    tid = uuid.uuid4()
    eid = uuid.uuid4() if patch_target == "replay_event" else None

    if "content" in body_kwargs:
        headers = {"X-Modulo-Timestamp": str(int(time.time()))} | extra_headers
        request_kwargs = {"content": body_kwargs["content"], "headers": headers}
    elif "json" in body_kwargs:
        headers = {"X-Modulo-Timestamp": str(int(time.time()))} | extra_headers
        request_kwargs = {"json": body_kwargs["json"], "headers": headers}
    else:
        request_kwargs = {"headers": dict(extra_headers)}

    url_suffix = f"/replay/{eid}" if eid else ""

    if patch_target is None:
        resp = webhook_client.post(f"/api/v1/triggers/{tid}/webhook{url_suffix}", **request_kwargs)
    else:
        nargs = mock_factory.__code__.co_argcount
        mock_obj = mock_factory(tid, eid) if nargs == 2 else mock_factory()
        with patch(f"modulo.api.routes.webhooks._trigger_engine.{patch_target}", new=mock_obj):
            resp = webhook_client.post(f"/api/v1/triggers/{tid}/webhook{url_suffix}", **request_kwargs)

    assert resp.status_code == expected_status
    body = resp.json()
    assert expected_body.items() <= body.items()
    if expected_status == 202:
        uuid.UUID(body["run_id"])  # success responses must carry a real run id


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------


async def test_cleanup_expired_dedup_hashes() -> None:
    session = _make_session(trigger=_make_trigger())

    lock_result = MagicMock()
    lock_result.scalar_one.return_value = True

    expired_result = MagicMock()
    expired_result.scalars.return_value.all.return_value = [uuid.uuid4(), uuid.uuid4()]

    call_count = 0

    async def _execute(stmt: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return lock_result
        return expired_result

    session.execute = _execute

    count = await TriggerEngine.cleanup_expired_dedup_hashes(session)
    assert count == 2


async def test_cleanup_expired_dedup_hashes_lock_contention() -> None:
    session = _make_session(trigger=_make_trigger())

    lock_result = MagicMock()
    lock_result.scalar_one.return_value = False

    async def _execute(stmt: Any, *args: Any, **kwargs: Any) -> Any:
        return lock_result

    session.execute = _execute

    count = await TriggerEngine.cleanup_expired_dedup_hashes(session)
    assert count == 0
