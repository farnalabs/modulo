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

_VALID_TS = int(time.time())


def _sha256_sig(body: bytes, secret: str, timestamp: int | None = None) -> str:
    if timestamp is not None:
        payload = f"{timestamp}.".encode() + body
    else:
        payload = body
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
    from modulo.api.dependencies import _get_engine, get_db_session
    from modulo.api.main import app
    from modulo.auth.dependencies import get_current_user
    from modulo.auth.jwt import AuthenticatedPrincipal
    from modulo.settings import Settings, get_settings

    _fake_org_id = uuid.uuid4()
    _fake_user_id = uuid.uuid4()

    def _settings() -> Settings:
        return Settings(
            database_url="postgresql+asyncpg://localhost/test",
            secret_key=_VALID_32,
            fernet_key=_VALID_32,
            modulo_admin_password="pw",
        )

    def _principal() -> AuthenticatedPrincipal:
        return AuthenticatedPrincipal(
            username="ci@test.local",
            organisation_id=_fake_org_id,
            user_id=_fake_user_id,
            org_role="admin",
        )

    session = AsyncMock()
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


def test_verify_timestamp_valid() -> None:
    ts = str(int(time.time()))
    result = _verify_timestamp(ts)
    assert isinstance(result, int)


def test_verify_timestamp_expired_past() -> None:
    ts = str(int(time.time()) - 600)
    with pytest.raises(TimestampExpiredError):
        _verify_timestamp(ts)


def test_verify_timestamp_expired_future() -> None:
    ts = str(int(time.time()) + 600)
    with pytest.raises(TimestampExpiredError):
        _verify_timestamp(ts)


def test_verify_timestamp_none_raises() -> None:
    with pytest.raises(TimestampExpiredError):
        _verify_timestamp(None)


def test_verify_timestamp_non_int_raises() -> None:
    with pytest.raises(TimestampExpiredError):
        _verify_timestamp("not-a-number")


def test_verify_hmac_correct_signature_with_timestamp() -> None:
    secret = "my-secret"
    body = b"payload"
    ts = int(time.time())
    sig = _sha256_sig(body, secret, timestamp=ts)
    assert _verify_hmac(body, secret, sig, timestamp=ts) is True


def test_verify_hmac_correct_signature_without_timestamp() -> None:
    secret = "my-secret"
    body = b"payload"
    sig = _sha256_sig(body, secret)
    assert _verify_hmac(body, secret, sig) is True


def test_verify_hmac_wrong_signature() -> None:
    assert _verify_hmac(b"payload", "secret", "sha256=wrong", timestamp=int(time.time())) is False


def test_verify_hmac_missing_signature() -> None:
    assert _verify_hmac(b"payload", "secret", None, timestamp=int(time.time())) is False


def test_verify_hmac_missing_prefix() -> None:
    secret = "s"
    body = b"x"
    ts = int(time.time())
    raw_hex = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    assert _verify_hmac(body, secret, raw_hex, timestamp=ts) is False


def test_extract_field_top_level() -> None:
    assert _extract_field({"a": 1}, "a") == 1


def test_extract_field_nested() -> None:
    assert _extract_field({"a": {"b": {"c": "deep"}}}, "a.b.c") == "deep"


def test_extract_field_missing_returns_none() -> None:
    assert _extract_field({"a": 1}, "b") is None


def test_extract_field_non_dict_returns_none() -> None:
    assert _extract_field({"a": "not-a-dict"}, "a.b") is None


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

    called_payload = mock_create.call_args.kwargs["input_payload"]
    assert called_payload == {"action": "opened", "pr_num": 42}


async def test_handle_webhook_returns_mapped_input_payload() -> None:
    mapping = {"pr_num": "number"}
    trigger = _make_trigger(payload_mapping=mapping)
    session = _make_session(trigger=trigger, active_run_count=0)

    run_mock = MagicMock()
    run_mock.id = uuid.uuid4()

    with (
        patch("modulo.core.trigger_engine.create_run", return_value=run_mock),
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

    assert input_payload == {"pr_num": 42}


# ---------------------------------------------------------------------------
# TriggerEngine.handle_webhook — validation failures
# ---------------------------------------------------------------------------


async def test_handle_webhook_trigger_not_found_raises() -> None:
    session = _make_session(trigger=None)
    with pytest.raises(TriggerNotFoundError):
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


async def test_handle_webhook_inactive_trigger_raises() -> None:
    trigger = _make_trigger(active=False)
    session = _make_session(trigger=trigger)
    with pytest.raises(TriggerInactiveError):
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


async def test_handle_webhook_missing_timestamp_raises() -> None:
    trigger = _make_trigger()
    session = _make_session(trigger=trigger)
    with pytest.raises(TimestampExpiredError):
        await TriggerEngine().handle_webhook(
            session,
            trigger_id=trigger.id,
            org_id=_ORG,
            raw_body=_RAW_BODY,
            raw_payload=_RAW_PAYLOAD,
            hmac_signature=None,
            modulo_timestamp=None,
            snapshot_id=_SNAP,
        )


async def test_handle_webhook_invalid_hmac_raises() -> None:
    trigger = _make_trigger(hmac_secret="secret")
    session = _make_session(trigger=trigger)
    with pytest.raises(HmacValidationError):
        await TriggerEngine().handle_webhook(
            session,
            trigger_id=trigger.id,
            org_id=_ORG,
            raw_body=_RAW_BODY,
            raw_payload=_RAW_PAYLOAD,
            hmac_signature="sha256=wrong",
            modulo_timestamp=str(_VALID_TS),
            snapshot_id=_SNAP,
        )


async def test_handle_webhook_missing_hmac_raises() -> None:
    trigger = _make_trigger(hmac_secret="secret")
    session = _make_session(trigger=trigger)
    with pytest.raises(HmacValidationError):
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


async def test_handle_webhook_duplicate_raises() -> None:
    trigger = _make_trigger()
    session = _make_session(trigger=trigger, dedup_exists=True)
    with pytest.raises(DuplicateWebhookError):
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


async def test_handle_webhook_concurrency_limit_raises() -> None:
    trigger = _make_trigger(max_concurrent_runs=2)
    session = _make_session(trigger=trigger, active_run_count=2)
    with pytest.raises(ConcurrentRunLimitError) as exc_info:
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
    assert exc_info.value.limit == 2


async def test_handle_webhook_logs_trigger_event_on_timestamp_expired() -> None:
    trigger = _make_trigger()
    session = _make_session(trigger=trigger)
    with pytest.raises(TimestampExpiredError):
        await TriggerEngine().handle_webhook(
            session,
            trigger_id=trigger.id,
            org_id=_ORG,
            raw_body=_RAW_BODY,
            raw_payload=_RAW_PAYLOAD,
            hmac_signature=None,
            modulo_timestamp=str(int(time.time()) - 600),
            snapshot_id=_SNAP,
        )
    session.add.assert_called()
    added = session.add.call_args[0][0]
    assert added.validation_result == "timestamp_expired"


async def test_handle_webhook_logs_trigger_event_on_hmac_failure() -> None:
    trigger = _make_trigger(hmac_secret="secret")
    session = _make_session(trigger=trigger)
    with pytest.raises(HmacValidationError):
        await TriggerEngine().handle_webhook(
            session,
            trigger_id=trigger.id,
            org_id=_ORG,
            raw_body=_RAW_BODY,
            raw_payload=_RAW_PAYLOAD,
            hmac_signature="sha256=bad",
            modulo_timestamp=str(_VALID_TS),
            snapshot_id=_SNAP,
        )
    session.add.assert_called()
    added = session.add.call_args[0][0]
    assert added.validation_result == "hmac_failed"


async def test_handle_webhook_logs_trigger_event_on_duplicate() -> None:
    trigger = _make_trigger()
    session = _make_session(trigger=trigger, dedup_exists=True)
    with pytest.raises(DuplicateWebhookError):
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
    session.add.assert_called()
    added = session.add.call_args[0][0]
    assert added.validation_result == "deduplicated"


async def test_handle_webhook_logs_trigger_event_on_concurrency_limit() -> None:
    trigger = _make_trigger(max_concurrent_runs=1)
    session = _make_session(trigger=trigger, active_run_count=1)
    with pytest.raises(ConcurrentRunLimitError):
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
    session.add.assert_called()
    trigger_event_call = [
        a for a in session.add.call_args_list if hasattr(a[0][0], "validation_result")
    ]
    assert any(
        getattr(c[0][0], "validation_result", None) == "concurrency_limit_reached"
        for c in trigger_event_call
    )


# ---------------------------------------------------------------------------
# Webhook API route — integration smoke tests using TestClient
# ---------------------------------------------------------------------------


def test_webhook_route_returns_404_on_missing_trigger(webhook_client: TestClient) -> None:
    tid = uuid.uuid4()
    with patch(
        "modulo.api.routes.webhooks._trigger_engine.handle_webhook",
        new=AsyncMock(side_effect=TriggerNotFoundError(tid)),
    ):
        resp = webhook_client.post(
            f"/api/v1/triggers/{tid}/webhook",
            json={"action": "ping"},
            headers={"X-Modulo-Timestamp": str(_VALID_TS)},
        )
    assert resp.status_code == 404


def test_webhook_route_returns_400_on_expired_timestamp(webhook_client: TestClient) -> None:
    tid = uuid.uuid4()
    with patch(
        "modulo.api.routes.webhooks._trigger_engine.handle_webhook",
        new=AsyncMock(side_effect=TimestampExpiredError()),
    ):
        resp = webhook_client.post(
            f"/api/v1/triggers/{tid}/webhook",
            json={"action": "push"},
            headers={"X-Modulo-Timestamp": str(int(time.time()) - 600)},
        )
    assert resp.status_code == 400


def test_webhook_route_returns_401_on_hmac_failure(webhook_client: TestClient) -> None:
    tid = uuid.uuid4()
    with patch(
        "modulo.api.routes.webhooks._trigger_engine.handle_webhook",
        new=AsyncMock(side_effect=HmacValidationError()),
    ):
        resp = webhook_client.post(
            f"/api/v1/triggers/{tid}/webhook",
            json={"action": "push"},
            headers={
                "X-Modulo-Timestamp": str(_VALID_TS),
                "X-Modulo-Webhook-Secret": "sha256=bad",
            },
        )
    assert resp.status_code == 401


def test_webhook_route_returns_400_on_duplicate(webhook_client: TestClient) -> None:
    tid = uuid.uuid4()
    with patch(
        "modulo.api.routes.webhooks._trigger_engine.handle_webhook",
        new=AsyncMock(side_effect=DuplicateWebhookError("abc123")),
    ):
        resp = webhook_client.post(
            f"/api/v1/triggers/{tid}/webhook",
            json={"action": "push"},
            headers={"X-Modulo-Timestamp": str(_VALID_TS)},
        )
    assert resp.status_code == 400


def test_webhook_route_returns_429_on_concurrency_limit(webhook_client: TestClient) -> None:
    trigger_id = uuid.uuid4()
    with patch(
        "modulo.api.routes.webhooks._trigger_engine.handle_webhook",
        new=AsyncMock(side_effect=ConcurrentRunLimitError(trigger_id, 3)),
    ):
        resp = webhook_client.post(
            f"/api/v1/triggers/{trigger_id}/webhook",
            json={"action": "push"},
            headers={"X-Modulo-Timestamp": str(_VALID_TS)},
        )
    assert resp.status_code == 429


def test_webhook_route_returns_400_on_non_json_object(webhook_client: TestClient) -> None:
    resp = webhook_client.post(
        f"/api/v1/triggers/{uuid.uuid4()}/webhook",
        content=b"not-json",
        headers={
            "Content-Type": "application/json",
            "X-Modulo-Timestamp": str(_VALID_TS),
        },
    )
    assert resp.status_code == 400


def test_webhook_route_returns_202_on_success(webhook_client: TestClient) -> None:
    tid = uuid.uuid4()
    run_mock = MagicMock()
    run_mock.id = uuid.uuid4()
    with patch(
        "modulo.api.routes.webhooks._trigger_engine.handle_webhook",
        new=AsyncMock(return_value=(run_mock, MagicMock(), {"key": "val"})),
    ):
        resp = webhook_client.post(
            f"/api/v1/triggers/{tid}/webhook",
            json={"action": "push"},
            headers={"X-Modulo-Timestamp": str(_VALID_TS)},
        )
    assert resp.status_code == 202


def test_webhook_replay_returns_202(webhook_client: TestClient) -> None:
    tid = uuid.uuid4()
    eid = uuid.uuid4()
    run_mock = MagicMock()
    run_mock.id = uuid.uuid4()
    with patch(
        "modulo.api.routes.webhooks._trigger_engine.replay_event",
        new=AsyncMock(return_value=(run_mock, MagicMock(), {"key": "val"})),
    ):
        resp = webhook_client.post(
            f"/api/v1/triggers/{tid}/webhook/replay/{eid}",
        )
    assert resp.status_code == 202


def test_webhook_replay_returns_404_on_missing_event(webhook_client: TestClient) -> None:
    tid = uuid.uuid4()
    eid = uuid.uuid4()
    with patch(
        "modulo.api.routes.webhooks._trigger_engine.replay_event",
        new=AsyncMock(side_effect=ReplayNotFoundError(eid)),
    ):
        resp = webhook_client.post(
            f"/api/v1/triggers/{tid}/webhook/replay/{eid}",
        )
    assert resp.status_code == 404


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
