"""Unit tests for the Slack app_mention trigger engine module.

Covers:
* ``verify_slack_signature`` — valid/invalid signatures, constant-time compare,
  missing headers, tampered body, timestamp binding.
* ``verify_slack_timestamp`` — replay window.
* ``parse_app_mention_payload`` — envelope/event type checks, field extraction.
* ``extract_challenge`` — URL verification handshake.
* ``_slack_dedup_hash`` — event_id namespacing.
* ``handle_app_mention`` — happy path, mapping, dedupe by event_id, signature
  failure, parse failure, non-app_mention event skip, concurrency queuing.
"""

import hashlib
import hmac
import time
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core.trigger_engine.slack_app_mention import (
    DuplicateWebhookError,
    SlackAppMentionParseError,
    SlackChallengeNotFoundError,
    SlackEventTypeError,
    SlackSignatureError,
    SlackTimestampExpiredError,
    _slack_dedup_hash,
    extract_challenge,
    handle_app_mention,
    parse_app_mention_payload,
    verify_slack_signature,
    verify_slack_timestamp,
)

_ORG = uuid.UUID("00000000-0000-0000-0000-000000000001")
_SNAP = uuid.UUID("00000000-0000-0000-0000-000000000003")
_VALID_32 = "a" * 32

_SECRET = "8f742231b10e8888abcd99yyyzzz85a5"
_SIGNING_SECRET = "my-slack-signing-secret"

_EVENT_ID = "Ev1234567890"


def _slack_sig(body: bytes, secret: str, timestamp: str) -> str:
    base = f"v0:{timestamp}:".encode() + body
    return "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()


def _app_mention_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "token": "verification-token",
        "team_id": "T12345",
        "api_app_id": "A12345",
        "event_id": _EVENT_ID,
        "event_time": 1234567890,
        "type": "event_callback",
        "event": {
            "type": "app_mention",
            "user": "U12345",
            "text": "<@U012345> please process this",
            "ts": "1234567890.000001",
            "channel": "C12345",
            "event_ts": "1234567890.000001",
            "thread_ts": None,
        },
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# verify_slack_signature
# ---------------------------------------------------------------------------


class TestVerifySlackSignature:
    def test_valid_signature(self) -> None:
        body = b'{"type":"event_callback"}'
        ts = str(int(time.time()))
        sig = _slack_sig(body, _SIGNING_SECRET, ts)
        assert verify_slack_signature(body, _SIGNING_SECRET, ts, sig) is True

    def test_wrong_secret_rejected(self) -> None:
        body = b'{"type":"event_callback"}'
        ts = str(int(time.time()))
        sig = _slack_sig(body, "another-secret", ts)
        assert verify_slack_signature(body, _SIGNING_SECRET, ts, sig) is False

    def test_tampered_body_rejected(self) -> None:
        ts = str(int(time.time()))
        sig = _slack_sig(b"original-body", _SIGNING_SECRET, ts)
        assert verify_slack_signature(b"tampered-body", _SIGNING_SECRET, ts, sig) is False

    def test_timestamp_bound_to_signature(self) -> None:
        body = b"payload"
        ts = str(int(time.time()))
        sig = _slack_sig(body, _SIGNING_SECRET, ts)
        # Same body + signature but a DIFFERENT presented timestamp must fail
        # (the timestamp is part of the signed base string).
        assert verify_slack_signature(body, _SIGNING_SECRET, str(int(time.time()) + 1), sig) is False

    def test_missing_signature_rejected(self) -> None:
        body = b"payload"
        ts = str(int(time.time()))
        assert verify_slack_signature(body, _SIGNING_SECRET, ts, None) is False

    def test_missing_timestamp_rejected(self) -> None:
        body = b"payload"
        sig = _slack_sig(body, _SIGNING_SECRET, str(int(time.time())))
        assert verify_slack_signature(body, _SIGNING_SECRET, None, sig) is False

    def test_missing_secret_rejected(self) -> None:
        body = b"payload"
        ts = str(int(time.time()))
        sig = _slack_sig(body, _SIGNING_SECRET, ts)
        assert verify_slack_signature(body, "", ts, sig) is False

    def test_uses_constant_time_compare(self, monkeypatch: pytest.MonkeyPatch) -> None:
        body = b"payload"
        ts = str(int(time.time()))
        sig = _slack_sig(body, _SIGNING_SECRET, ts)
        real = hmac.compare_digest
        calls: list[tuple[Any, Any]] = []

        def _spy(a: Any, b: Any) -> bool:
            calls.append((a, b))
            return real(a, b)

        monkeypatch.setattr("modulo.core.trigger_engine.slack_app_mention.hmac.compare_digest", _spy)
        assert verify_slack_signature(body, _SIGNING_SECRET, ts, sig) is True
        assert len(calls) == 1


# ---------------------------------------------------------------------------
# verify_slack_timestamp
# ---------------------------------------------------------------------------


class TestVerifySlackTimestamp:
    def test_valid(self) -> None:
        assert verify_slack_timestamp(str(int(time.time()))) > 0

    def test_missing_raises(self) -> None:
        with pytest.raises(SlackTimestampExpiredError):
            verify_slack_timestamp(None)

    def test_stale_raises(self) -> None:
        with pytest.raises(SlackTimestampExpiredError):
            verify_slack_timestamp(str(int(time.time()) - 600))

    def test_malformed_raises(self) -> None:
        with pytest.raises(SlackTimestampExpiredError):
            verify_slack_timestamp("not-a-number")


# ---------------------------------------------------------------------------
# parse_app_mention_payload
# ---------------------------------------------------------------------------


class TestParseAppMentionPayload:
    def test_valid_payload_extracts_fields(self) -> None:
        mention = parse_app_mention_payload(_app_mention_payload())
        assert mention["event_id"] == _EVENT_ID
        assert mention["type"] == "app_mention"
        assert mention["team_id"] == "T12345"
        assert mention["channel"] == "C12345"
        assert mention["user"] == "U12345"
        assert mention["text"] == "<@U012345> please process this"
        assert mention["ts"] == "1234567890.000001"
        assert mention["thread_ts"] is None

    def test_thread_ts_preserved(self) -> None:
        payload = _app_mention_payload()
        payload["event"]["thread_ts"] = "1234567890.000100"
        mention = parse_app_mention_payload(payload)
        assert mention["thread_ts"] == "1234567890.000100"

    def test_url_verification_raises_challenge_error(self) -> None:
        payload = {"type": "url_verification", "challenge": "3eZbrw1aBm2rZgRNFdxV2598559m"}
        with pytest.raises(SlackChallengeNotFoundError):
            parse_app_mention_payload(payload)

    def test_non_callback_envelope_rejected(self) -> None:
        with pytest.raises(SlackEventTypeError):
            parse_app_mention_payload({"type": "banana", "event": {"type": "app_mention"}})

    def test_non_mention_event_rejected(self) -> None:
        payload = _app_mention_payload()
        payload["event"]["type"] = "message"
        with pytest.raises(SlackEventTypeError, match="not 'app_mention'"):
            parse_app_mention_payload(payload)

    def test_missing_event_rejected(self) -> None:
        with pytest.raises(SlackAppMentionParseError):
            parse_app_mention_payload({"type": "event_callback"})

    def test_missing_event_id_rejected(self) -> None:
        payload = _app_mention_payload()
        del payload["event_id"]
        with pytest.raises(SlackAppMentionParseError, match="event_id"):
            parse_app_mention_payload(payload)

    def test_non_dict_payload_rejected(self) -> None:
        with pytest.raises(SlackAppMentionParseError):
            parse_app_mention_payload("not-a-dict")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# extract_challenge
# ---------------------------------------------------------------------------


class TestExtractChallenge:
    def test_returns_challenge(self) -> None:
        assert extract_challenge({"type": "url_verification", "challenge": "abc123"}) == "abc123"

    def test_non_verification_payload_rejected(self) -> None:
        with pytest.raises(SlackChallengeNotFoundError):
            extract_challenge({"type": "event_callback"})

    def test_missing_challenge_rejected(self) -> None:
        with pytest.raises(SlackChallengeNotFoundError):
            extract_challenge({"type": "url_verification"})


# ---------------------------------------------------------------------------
# _slack_dedup_hash
# ---------------------------------------------------------------------------


class TestSlackDedupHash:
    def test_namespaced_and_deterministic(self) -> None:
        h1 = _slack_dedup_hash(_EVENT_ID)
        h2 = _slack_dedup_hash(_EVENT_ID)
        assert h1 == h2
        assert len(h1) == 64
        # Must not collide with a plain body hash (namespace prefix).
        assert h1 != hashlib.sha256(_EVENT_ID.encode()).hexdigest()

    def test_distinct_event_ids_differ(self) -> None:
        assert _slack_dedup_hash("Ev1") != _slack_dedup_hash("Ev2")


# ---------------------------------------------------------------------------
# handle_app_mention
# ---------------------------------------------------------------------------


def _make_trigger(**overrides: Any) -> MagicMock:
    t = MagicMock()
    t.id = uuid.uuid4()
    t.pipeline_id = uuid.uuid4()
    t.active = True
    t.config_json = {"signing_secret": _SIGNING_SECRET}
    t.max_concurrent_runs = 5
    t.trigger_type = "slack_app_mention"
    for k, v in overrides.items():
        setattr(t, k, v)
    return t


def _make_session(
    *,
    trigger: MagicMock,
    dedup_exists: bool = False,
    active_run_count: int = 0,
    pipeline_rate_limit: dict[str, Any] | None = None,
    recent_run_count: int = 0,
) -> AsyncMock:
    session = AsyncMock()
    lock_result = MagicMock()
    lock_result.scalar_one.return_value = True
    trigger_result = MagicMock()
    trigger_result.scalar_one_or_none.return_value = trigger
    dedup_result = MagicMock()
    dedup_result.scalar_one_or_none.return_value = MagicMock() if dedup_exists else None
    generic_result = MagicMock()
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
        return recent_count_result

    session.execute = _execute
    session.add = MagicMock()
    session.flush = AsyncMock()

    nested_cm = AsyncMock()
    nested_cm.__aenter__ = AsyncMock(return_value=None)
    nested_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin_nested = MagicMock(return_value=nested_cm)

    return session


def _body(payload: dict[str, Any]) -> bytes:
    import json

    return json.dumps(payload).encode()


@pytest.fixture(autouse=True)
def _not_paused() -> Any:
    with patch("modulo.db.settings_resolver.org_is_paused", new_callable=AsyncMock, return_value=False):
        yield


async def test_handle_app_mention_success() -> None:
    trigger = _make_trigger()
    session = _make_session(trigger=trigger)
    payload = _app_mention_payload()
    body = _body(payload)
    ts = str(int(time.time()))
    sig = _slack_sig(body, _SIGNING_SECRET, ts)

    run_mock = MagicMock(id=uuid.uuid4())
    with patch("modulo.core.trigger_engine.slack_app_mention.create_run", return_value=run_mock):
        run, te, input_payload = await handle_app_mention(
            session,
            trigger_id=trigger.id,
            org_id=_ORG,
            raw_body=body,
            raw_payload=payload,
            slack_signature=sig,
            slack_timestamp=ts,
            snapshot_id=_SNAP,
        )

    assert run is run_mock
    assert te.validation_result == "accepted"
    assert input_payload["event_id"] == _EVENT_ID
    assert input_payload["channel"] == "C12345"


async def test_handle_app_mention_applies_payload_mapping() -> None:
    trigger = _make_trigger(
        config_json={
            "signing_secret": _SIGNING_SECRET,
            "payload_mapping": {"text": "text", "channel": "channel"},
        }
    )
    session = _make_session(trigger=trigger)
    payload = _app_mention_payload()
    body = _body(payload)
    ts = str(int(time.time()))
    sig = _slack_sig(body, _SIGNING_SECRET, ts)

    run_mock = MagicMock(id=uuid.uuid4())
    with patch("modulo.core.trigger_engine.slack_app_mention.create_run", return_value=run_mock) as mock_create:
        _, _, input_payload = await handle_app_mention(
            session,
            trigger_id=trigger.id,
            org_id=_ORG,
            raw_body=body,
            raw_payload=payload,
            slack_signature=sig,
            slack_timestamp=ts,
            snapshot_id=_SNAP,
        )

    assert mock_create.await_count == 1
    assert mock_create.call_args.kwargs["trigger_type"] == "slack_app_mention"
    assert input_payload == {"text": "<@U012345> please process this", "channel": "C12345"}


async def test_handle_app_mention_duplicate_event_deduped() -> None:
    trigger = _make_trigger()
    session = _make_session(trigger=trigger, dedup_exists=True)
    payload = _app_mention_payload()
    body = _body(payload)
    ts = str(int(time.time()))
    sig = _slack_sig(body, _SIGNING_SECRET, ts)

    with (
        patch("modulo.core.trigger_engine.slack_app_mention.create_run") as mock_create,
        pytest.raises(DuplicateWebhookError),
    ):
        await handle_app_mention(
            session,
            trigger_id=trigger.id,
            org_id=_ORG,
            raw_body=body,
            raw_payload=payload,
            slack_signature=sig,
            slack_timestamp=ts,
            snapshot_id=_SNAP,
        )

    mock_create.assert_not_called()
    assert any(getattr(c[0][0], "validation_result", None) == "deduplicated" for c in session.add.call_args_list)


async def test_handle_app_mention_bad_signature_raises() -> None:
    trigger = _make_trigger()
    session = _make_session(trigger=trigger)
    payload = _app_mention_payload()
    body = _body(payload)
    ts = str(int(time.time()))

    with pytest.raises(SlackSignatureError):
        await handle_app_mention(
            session,
            trigger_id=trigger.id,
            org_id=_ORG,
            raw_body=body,
            raw_payload=payload,
            slack_signature="v0=deadbeef",
            slack_timestamp=ts,
            snapshot_id=_SNAP,
        )
    assert any(getattr(c[0][0], "validation_result", None) == "hmac_failed" for c in session.add.call_args_list)


async def test_handle_app_mention_missing_secret_raises() -> None:
    trigger = _make_trigger(config_json={})
    session = _make_session(trigger=trigger)
    payload = _app_mention_payload()
    body = _body(payload)
    ts = str(int(time.time()))

    with pytest.raises(SlackSignatureError, match="not configured"):
        await handle_app_mention(
            session,
            trigger_id=trigger.id,
            org_id=_ORG,
            raw_body=body,
            raw_payload=payload,
            slack_signature="v0=x",
            slack_timestamp=ts,
            snapshot_id=_SNAP,
        )


async def test_handle_app_mention_stale_timestamp_raises() -> None:
    trigger = _make_trigger()
    session = _make_session(trigger=trigger)
    payload = _app_mention_payload()
    body = _body(payload)
    stale_ts = str(int(time.time()) - 600)

    with pytest.raises(SlackTimestampExpiredError):
        await handle_app_mention(
            session,
            trigger_id=trigger.id,
            org_id=_ORG,
            raw_body=body,
            raw_payload=payload,
            slack_signature="v0=x",
            slack_timestamp=stale_ts,
            snapshot_id=_SNAP,
        )


async def test_handle_app_mention_non_mention_event_skipped() -> None:
    trigger = _make_trigger()
    session = _make_session(trigger=trigger)
    payload = _app_mention_payload()
    payload["event"]["type"] = "message"
    body = _body(payload)
    ts = str(int(time.time()))
    sig = _slack_sig(body, _SIGNING_SECRET, ts)

    with (
        patch("modulo.core.trigger_engine.slack_app_mention.create_run") as mock_create,
        pytest.raises(SlackEventTypeError),
    ):
        await handle_app_mention(
            session,
            trigger_id=trigger.id,
            org_id=_ORG,
            raw_body=body,
            raw_payload=payload,
            slack_signature=sig,
            slack_timestamp=ts,
            snapshot_id=_SNAP,
        )

    mock_create.assert_not_called()
    assert any(
        getattr(c[0][0], "validation_result", None) == "event_type_not_accepted" for c in session.add.call_args_list
    )


async def test_handle_app_mention_parse_failure_logs_event() -> None:
    trigger = _make_trigger()
    session = _make_session(trigger=trigger)
    payload = {"type": "event_callback"}  # missing event
    body = _body(payload)
    ts = str(int(time.time()))
    sig = _slack_sig(body, _SIGNING_SECRET, ts)

    with (
        patch("modulo.core.trigger_engine.slack_app_mention.create_run") as mock_create,
        pytest.raises(SlackAppMentionParseError),
    ):
        await handle_app_mention(
            session,
            trigger_id=trigger.id,
            org_id=_ORG,
            raw_body=body,
            raw_payload=payload,
            slack_signature=sig,
            slack_timestamp=ts,
            snapshot_id=_SNAP,
        )

    mock_create.assert_not_called()
    assert any(getattr(c[0][0], "validation_result", None) == "parse_failed" for c in session.add.call_args_list)


async def test_handle_app_mention_concurrency_queues_anyway() -> None:
    trigger = _make_trigger(max_concurrent_runs=1)
    session = _make_session(trigger=trigger, active_run_count=1)
    payload = _app_mention_payload()
    body = _body(payload)
    ts = str(int(time.time()))
    sig = _slack_sig(body, _SIGNING_SECRET, ts)

    run_mock = MagicMock(id=uuid.uuid4())
    with patch("modulo.core.trigger_engine.slack_app_mention.create_run", return_value=run_mock):
        run, te, _ = await handle_app_mention(
            session,
            trigger_id=trigger.id,
            org_id=_ORG,
            raw_body=body,
            raw_payload=payload,
            slack_signature=sig,
            slack_timestamp=ts,
            snapshot_id=_SNAP,
        )

    assert run is run_mock
    assert te.validation_result == "accepted"
    assert any(
        getattr(c[0][0], "validation_result", None) == "concurrency_limit_reached" for c in session.add.call_args_list
    )


async def test_handle_app_mention_rate_limit_exceeded() -> None:
    trigger = _make_trigger()
    session = _make_session(
        trigger=trigger,
        pipeline_rate_limit={"max_triggers": 1, "window_seconds": 3600},
        recent_run_count=1,
    )
    payload = _app_mention_payload()
    body = _body(payload)
    ts = str(int(time.time()))
    sig = _slack_sig(body, _SIGNING_SECRET, ts)

    from modulo.core.trigger_engine import PipelineRateLimitError

    with (
        patch("modulo.core.trigger_engine.slack_app_mention.create_run") as mock_create,
        pytest.raises(PipelineRateLimitError),
    ):
        await handle_app_mention(
            session,
            trigger_id=trigger.id,
            org_id=_ORG,
            raw_body=body,
            raw_payload=payload,
            slack_signature=sig,
            slack_timestamp=ts,
            snapshot_id=_SNAP,
        )

    mock_create.assert_not_called()
    assert any(getattr(c[0][0], "validation_result", None) == "rate_limited" for c in session.add.call_args_list)


async def test_handle_app_mention_busy_lock_raises() -> None:
    trigger = _make_trigger()
    session = _make_session(trigger=trigger)
    lock_result = MagicMock()
    lock_result.scalar_one.return_value = False
    session.execute = AsyncMock(return_value=lock_result)
    payload = _app_mention_payload()
    body = _body(payload)

    from modulo.core.trigger_engine import TriggerBusyError

    with pytest.raises(TriggerBusyError):
        await handle_app_mention(
            session,
            trigger_id=trigger.id,
            org_id=_ORG,
            raw_body=body,
            raw_payload=payload,
            slack_signature="v0=x",
            slack_timestamp=str(int(time.time())),
            snapshot_id=_SNAP,
        )
