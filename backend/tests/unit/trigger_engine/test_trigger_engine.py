"""Unit tests for TriggerEngine and helpers using mocked AsyncSession."""

import hashlib
import hmac
import time
import uuid
from collections.abc import AsyncGenerator, Generator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from modulo.core.trigger_engine import (
    ConcurrentRunLimitError,
    DuplicateWebhookError,
    HmacValidationError,
    ReplayNotFoundError,
    TimestampExpiredError,
    TriggerEngine,
    TriggerInactiveError,
    TriggerNotFoundError,
    _apply_payload_mapping,
    _extract_field,
    _sha256_hex,
    _verify_hmac,
    _verify_timestamp,
)
from modulo.db.models.trigger import Trigger

# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

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
    t.config_json = config
    t.max_concurrent_runs = max_concurrent_runs
    return t


def _make_session(
    *,
    trigger: MagicMock | None = None,
    active_run_count: int = 0,
    dedup_exists: bool = False,
) -> AsyncMock:
    """Build a mocked session that returns the given trigger and run count."""
    session = AsyncMock()

    trigger_result = MagicMock()
    trigger_result.scalar_one_or_none.return_value = trigger
    trigger_result.scalar_one.return_value = trigger

    dedup_result = MagicMock()
    dedup_result.scalar_one_or_none.return_value = MagicMock() if dedup_exists else None

    count_result = MagicMock()
    count_result.scalar_one.return_value = active_run_count

    call_count = 0

    async def _execute(stmt: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal call_count
        call_count += 1
        # Order: 1=trigger (FOR UPDATE), 2=dedup check, 3=count active runs
        if call_count == 1:
            return trigger_result
        if call_count == 2:
            return dedup_result
        if call_count == 3:
            return count_result
        return count_result

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
        (
            "s",
            b"x",
            lambda b, s: (
                hmac.new(s.encode(), f"{int(time.time())}.".encode() + b, hashlib.sha256).hexdigest(),
                int(time.time()),
            ),
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
        (
            {"max_concurrent_runs": 2},
            {"active_run_count": 2},
            None,
            lambda: str(int(time.time())),
            ConcurrentRunLimitError,
            lambda e: e.value.limit == 2,
        ),
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
            ConcurrentRunLimitError,
            "concurrency_limit_reached",
        ),
    ],
)
async def test_handle_webhook_logs_trigger_event(
    trigger_overrides, session_overrides, hmac_sig, mod_ts_factory, expected_exc, expected_vr
) -> None:
    trigger = _make_trigger(**trigger_overrides)
    session = _make_session(trigger=trigger, **session_overrides)
    mod_ts = mod_ts_factory() if callable(mod_ts_factory) else mod_ts_factory
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
    session.add.assert_called()
    found = any(getattr(c[0][0], "validation_result", None) == expected_vr for c in session.add.call_args_list)
    assert found


# ---------------------------------------------------------------------------
# Webhook API route — integration smoke tests using TestClient
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "patch_target,mock_factory,expected_status,extra_headers,body_kwargs",
    [
        (
            "handle_webhook",
            lambda tid, eid: AsyncMock(side_effect=TriggerNotFoundError(tid)),
            404,
            {},
            {"json": {"action": "ping"}},
        ),
        (
            "handle_webhook",
            lambda: AsyncMock(side_effect=TimestampExpiredError()),
            400,
            {},
            {"json": {"action": "push"}},
        ),
        (
            "handle_webhook",
            lambda: AsyncMock(side_effect=HmacValidationError()),
            401,
            {"X-Modulo-Webhook-Secret": "sha256=bad"},
            {"json": {"action": "push"}},
        ),
        (
            "handle_webhook",
            lambda: AsyncMock(side_effect=DuplicateWebhookError("abc123")),
            400,
            {},
            {"json": {"action": "push"}},
        ),
        (
            "handle_webhook",
            lambda tid, eid: AsyncMock(side_effect=ConcurrentRunLimitError(tid, 3)),
            429,
            {},
            {"json": {"action": "push"}},
        ),
        (None, None, 400, {"Content-Type": "application/json"}, {"content": b"not-json"}),
        (
            "handle_webhook",
            lambda: AsyncMock(return_value=(MagicMock(), MagicMock(), {"key": "val"})),
            202,
            {},
            {"json": {"action": "push"}},
        ),
        ("replay_event", lambda: AsyncMock(return_value=(MagicMock(), MagicMock(), {"key": "val"})), 202, {}, {}),
        ("replay_event", lambda tid, eid: AsyncMock(side_effect=ReplayNotFoundError(eid)), 404, {}, {}),
    ],
)
def test_webhook_route(
    webhook_client: TestClient, patch_target, mock_factory, expected_status, extra_headers, body_kwargs
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
