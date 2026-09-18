"""BDD step definitions: Slack ``app_mention`` triggers.

Covers the Slack Events API ``app_mention`` trigger surface
(``modulo/core/trigger_engine/slack_app_mention.py``): Slack signed-request
verification (``X-Slack-Signature`` HMAC-SHA256 over ``v0:<timestamp>:<body>``
plus the ±300s ``X-Slack-Request-Timestamp`` replay window), the
``url_verification`` challenge handshake, envelope parsing and payload
mapping, Slack ``event_id`` deduplication, concurrency-queuing and pipeline
rate limiting, and the advisory-lock busy refusal — with every delivery
attempt audited to a TriggerEvent. Drives the real shipped seams (the pure
``verify_slack_signature`` / ``extract_challenge`` guards and the
``handle_app_mention`` delivery path) following the same mocked-DB-session
pattern as ``test_polling_triggers.py``.
"""

import asyncio
import contextlib
import hashlib
import hmac
import json
import time
import uuid
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

from pytest_bdd import given, parsers, scenarios, then, when

from modulo.core.trigger_engine import DuplicateWebhookError, PipelineRateLimitError, TriggerBusyError
from modulo.core.trigger_engine.slack_app_mention import (
    SlackAppMentionParseError,
    SlackEventTypeError,
    SlackSignatureError,
    SlackTimestampExpiredError,
    handle_app_mention,
)
from tests.bdd.conftest import ORG_ID

with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/triggers/slack_app_mention.feature")

_SNAP_ID = uuid.UUID("00000000-0000-0000-0000-000000000003")
_WRONG_SECRET = "a-completely-different-secret"


def _sign(raw_body: bytes, secret: str, timestamp: str) -> str:
    """Slack-style ``v0=HMAC-SHA256(secret, 'v0:<ts>:<body>')`` header value."""
    base = f"v0:{timestamp}:".encode() + raw_body
    return "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()


def _app_mention_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "token": "verification-token",
        "team_id": "T12345",
        "api_app_id": "A12345",
        "event_id": "Ev1234567890",
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


def _ctx(request: Any) -> dict[str, Any]:
    if not hasattr(request.node, "_ctx"):
        request.node._ctx = {
            "trigger": None,
            "signing_secret": "my-slack-signing-secret",
            "sign_with_wrong_secret": False,
            "expired_timestamp": False,
            "dedup_exists": False,
            "active_run_count": 0,
            "pipeline_rate_limit": None,
            "recent_run_count": 0,
            "busy": False,
            "payload": None,
            "error": None,
            "run": None,
            "input_payload": None,
            "session": None,
            "create_run": None,
        }
    return cast("dict[str, Any]", request.node._ctx)


def _make_trigger(
    *, secret: str, max_concurrent_runs: int = 5, payload_mapping: dict[str, str] | None = None
) -> MagicMock:
    config: dict[str, Any] = {"signing_secret": secret}
    if payload_mapping is not None:
        config["payload_mapping"] = payload_mapping
    trigger = MagicMock()
    trigger.id = uuid.uuid4()
    trigger.pipeline_id = uuid.uuid4()
    trigger.organisation_id = ORG_ID
    trigger.active = True
    trigger.trigger_type = "slack_app_mention"
    trigger.max_concurrent_runs = max_concurrent_runs
    trigger.config_json = config
    return trigger


def _make_session(
    *,
    trigger: MagicMock,
    dedup_exists: bool,
    active_run_count: int,
    pipeline_rate_limit: dict[str, Any] | None,
    recent_run_count: int,
    busy: bool,
) -> AsyncMock:
    session = AsyncMock()
    lock_result = MagicMock()
    lock_result.scalar_one.return_value = not busy
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

    async def _execute(stmt: Any, *args: Any, **kwargs: Any) -> Any:
        stmt_str = str(stmt).lower()
        if "pg_try_advisory_lock" in stmt_str:
            return lock_result
        if "delete from webhook_dedup_hash" in stmt_str:
            return generic_result
        if "webhook_dedup_hash" in stmt_str:
            return dedup_result
        if "from trigger" in stmt_str:
            return trigger_result
        if "rate_limit_key" in stmt_str:
            return recent_count_result
        if "from pipelines" in stmt_str:
            return pipeline_result
        if "from runs" in stmt_str:
            return count_result
        return generic_result

    session.execute = _execute
    session.add = MagicMock()
    session.flush = AsyncMock()
    nested_cm = AsyncMock()
    nested_cm.__aenter__ = AsyncMock(return_value=None)
    nested_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin_nested = MagicMock(return_value=nested_cm)
    return session


def _deliver(ctx: dict[str, Any]) -> None:
    trigger = ctx["trigger"]
    payload = ctx["payload"]
    raw_body = json.dumps(payload).encode()
    timestamp = str(int(time.time()))
    signature = _sign(raw_body, ctx["signing_secret"], timestamp)
    if ctx["sign_with_wrong_secret"]:
        signature = _sign(raw_body, _WRONG_SECRET, timestamp)
    if ctx["expired_timestamp"]:
        timestamp = str(int(time.time()) - 600)

    session = _make_session(
        trigger=trigger,
        dedup_exists=ctx["dedup_exists"],
        active_run_count=ctx["active_run_count"],
        pipeline_rate_limit=ctx["pipeline_rate_limit"],
        recent_run_count=ctx["recent_run_count"],
        busy=ctx["busy"],
    )
    run_mock = MagicMock(id=uuid.uuid4())

    with (
        patch("modulo.db.settings_resolver.org_is_paused", new_callable=AsyncMock, return_value=False),
        patch("modulo.core.trigger_engine.slack_app_mention.create_run", return_value=run_mock) as mock_create_run,
    ):
        try:
            run, trigger_event, input_payload = asyncio.run(
                handle_app_mention(
                    session,
                    trigger_id=trigger.id,
                    org_id=ORG_ID,
                    raw_body=raw_body,
                    raw_payload=payload,
                    slack_signature=signature,
                    slack_timestamp=timestamp,
                    snapshot_id=_SNAP_ID,
                )
            )
        except (
            SlackSignatureError,
            SlackTimestampExpiredError,
            SlackEventTypeError,
            SlackAppMentionParseError,
            DuplicateWebhookError,
            PipelineRateLimitError,
            TriggerBusyError,
        ) as exc:
            ctx["error"] = exc
            ctx["session"] = session
            ctx["create_run"] = mock_create_run
            return
    ctx["run"] = run
    ctx["trigger_event"] = trigger_event
    ctx["input_payload"] = input_payload
    ctx["session"] = session
    ctx["create_run"] = mock_create_run


def _recorded_results(ctx: dict[str, Any]) -> list[str]:
    session = ctx["session"]
    if session is None:
        return []
    return [
        add_call.args[0].validation_result
        for add_call in session.add.call_args_list
        if add_call.args and getattr(add_call.args[0], "validation_result", None) is not None
    ]


# ---------------------------------------------------------------------------
# Given
# ---------------------------------------------------------------------------


@given(parsers.parse('a Slack app_mention trigger with signing secret "{secret}"'))
def _given_slack_trigger(secret: str, request: Any) -> None:
    ctx = _ctx(request)
    ctx["signing_secret"] = secret
    ctx["trigger"] = _make_trigger(secret=secret)


@given(parsers.parse('a Slack app_mention trigger with signing secret "{secret}" and payload mapping "{mapping}"'))
def _given_slack_trigger_mapping(secret: str, mapping: str, request: Any) -> None:
    ctx = _ctx(request)
    ctx["signing_secret"] = secret
    parsed: dict[str, str] = {}
    for item in mapping.split(","):
        target, source = item.strip().split("->", 1)
        parsed[target.strip()] = source.strip()
    ctx["trigger"] = _make_trigger(secret=secret, payload_mapping=parsed)


@given(parsers.parse('a Slack app_mention trigger with signing secret "{secret}" and max_concurrent_runs {limit:d}'))
def _given_slack_trigger_concurrency(secret: str, limit: int, request: Any) -> None:
    ctx = _ctx(request)
    ctx["signing_secret"] = secret
    ctx["trigger"] = _make_trigger(secret=secret, max_concurrent_runs=limit)


@given(parsers.parse('Slack delivers an app_mention event with event_id "{event_id}"'))
def _given_slack_event_delivered(event_id: str, request: Any) -> None:
    ctx = _ctx(request)
    ctx["payload"] = _app_mention_payload(event_id=event_id)


@given(parsers.parse('Slack delivers an app_mention event mentioning "{text}"'))
def _given_slack_event_mentioning(text: str, request: Any) -> None:
    ctx = _ctx(request)
    ctx["payload"] = _app_mention_payload()
    ctx["payload"]["event"]["text"] = text


@given("Slack signs an app_mention delivery with the wrong secret")
def _given_slack_wrong_secret(request: Any) -> None:
    _ctx(request)["sign_with_wrong_secret"] = True


@given("Slack delivers an app_mention event with an expired X-Slack-Request-Timestamp")
def _given_slack_expired_timestamp(request: Any) -> None:
    ctx = _ctx(request)
    ctx["expired_timestamp"] = True
    ctx["payload"] = _app_mention_payload()


@given(parsers.parse('a Slack app_mention delivery with event_id "{event_id}" was already processed'))
def _given_slack_duplicate_seen(event_id: str, request: Any) -> None:
    ctx = _ctx(request)
    ctx["dedup_exists"] = True


@given(parsers.parse('Slack delivers a "{event_type}" event instead of an app_mention'))
def _given_slack_non_mention(event_type: str, request: Any) -> None:
    ctx = _ctx(request)
    ctx["payload"] = _app_mention_payload()
    ctx["payload"]["event"]["type"] = event_type


@given("Slack delivers an app_mention event missing its event_id")
def _given_slack_missing_event_id(request: Any) -> None:
    ctx = _ctx(request)
    ctx["payload"] = _app_mention_payload()
    del ctx["payload"]["event_id"]


@given(parsers.parse("Slack delivers an app_mention event while {count:d} run is already active"))
def _given_slack_active_run(count: int, request: Any) -> None:
    ctx = _ctx(request)
    ctx["payload"] = _app_mention_payload()
    ctx["active_run_count"] = count


@given(parsers.parse('the pipeline rate limit is "{spec}" and {count:d} delivery already fired'))
def _given_slack_rate_limit(spec: str, count: int, request: Any) -> None:
    ctx = _ctx(request)
    max_triggers, window = spec.split(" per ", 1)
    ctx["pipeline_rate_limit"] = {"max_triggers": int(max_triggers), "window_seconds": int(window.split()[0])}
    ctx["recent_run_count"] = count


@given("another delivery is already holding the trigger advisory lock")
def _given_slack_busy(request: Any) -> None:
    _ctx(request)["busy"] = True


@given(parsers.parse('Slack sends a url_verification payload with challenge "{challenge}"'))
def _given_slack_url_verification(challenge: str, request: Any) -> None:
    _ctx(request)["challenge"] = challenge


# ---------------------------------------------------------------------------
# When
# ---------------------------------------------------------------------------


@when("the app_mention delivery is verified and processed")
def _when_slack_delivery_processed(request: Any) -> None:
    _deliver(_ctx(request))


# ---------------------------------------------------------------------------
# Then
# ---------------------------------------------------------------------------


def _assert_no_error(ctx: dict[str, Any]) -> None:
    assert ctx["error"] is None, f"delivery raised an unexpected error: {ctx['error']!r}"


@then(parsers.parse('the slack delivery creates a run with trigger_type "{ttype}"'))
def _then_slack_run_created(ttype: str, request: Any) -> None:
    ctx = _ctx(request)
    _assert_no_error(ctx)
    create_run = ctx["create_run"]
    assert ctx["run"] is not None
    assert create_run is not None
    assert create_run.await_count == 1
    assert create_run.call_args.kwargs["trigger_type"] == ttype


@then(parsers.parse('the slack delivery records a TriggerEvent with result "{result}"'))
def _then_slack_event_recorded(result: str, request: Any) -> None:
    ctx = _ctx(request)
    recorded = _recorded_results(ctx)
    assert result in recorded, f"no TriggerEvent with result {result!r} recorded; got {recorded}"


@then(parsers.parse('the run input_payload carries the event_id "{event_id}" and channel "{channel}"'))
def _then_slack_input_payload_fields(event_id: str, channel: str, request: Any) -> None:
    ctx = _ctx(request)
    _assert_no_error(ctx)
    payload = ctx["input_payload"]
    assert payload is not None
    assert payload.get("event_id") == event_id
    assert payload.get("channel") == channel


@then(parsers.parse("the run input_payload equals {payload}"))
def _then_slack_input_payload_equals(payload: str, request: Any) -> None:
    ctx = _ctx(request)
    _assert_no_error(ctx)
    expected = json.loads(payload)
    assert ctx["input_payload"] == expected, f"expected {expected}, got {ctx['input_payload']}"


@then(parsers.parse('the challenge "{challenge}" is echoed back'))
def _then_slack_challenge_echoed(challenge: str, request: Any) -> None:
    from modulo.core.trigger_engine.slack_app_mention import extract_challenge

    assert _ctx(request)["challenge"] == challenge
    assert extract_challenge({"type": "url_verification", "challenge": challenge}) == challenge


@then("the slack delivery refuses with a signature error")
def _then_slack_signature_error(request: Any) -> None:
    assert isinstance(_ctx(request)["error"], SlackSignatureError)


@then("the slack delivery refuses with a timestamp error")
def _then_slack_timestamp_error(request: Any) -> None:
    assert isinstance(_ctx(request)["error"], SlackTimestampExpiredError)


@then("the slack delivery refuses as a duplicate and no run is created")
def _then_slack_duplicate(request: Any) -> None:
    ctx = _ctx(request)
    assert isinstance(ctx["error"], DuplicateWebhookError)
    create_run = ctx["create_run"]
    if create_run is not None:
        create_run.assert_not_called()
    assert ctx["run"] is None


@then("the slack delivery refuses with a payload type error")
def _then_slack_payload_type_error(request: Any) -> None:
    assert isinstance(_ctx(request)["error"], SlackEventTypeError)


@then("the slack delivery refuses with a parse error")
def _then_slack_parse_error(request: Any) -> None:
    assert isinstance(_ctx(request)["error"], SlackAppMentionParseError)


@then("the slack delivery refuses with a rate limit error")
def _then_slack_rate_limit_error(request: Any) -> None:
    assert isinstance(_ctx(request)["error"], PipelineRateLimitError)


@then("the slack delivery refuses as busy")
def _then_slack_busy(request: Any) -> None:
    assert isinstance(_ctx(request)["error"], TriggerBusyError)
