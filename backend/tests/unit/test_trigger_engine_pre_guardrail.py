"""Unit tests for the FAR-214 pre-trigger guardrail pass at webhook intake.

Exercises the REAL ``TriggerEngine.handle_webhook`` / ``replay_event`` paths
against call-count-routed mocked sessions that return real guardrail rows for
the pre-trigger query:

  * pass ordering — the pre-trigger pass runs BEFORE the dedup insert;
  * block → reject-and-retry at the boundary: ``guardrail_blocked`` TriggerEvent
    + raw payload stored for replay, no run, no dedup slot consumed;
  * redact → masks applied at intake so the payload that proceeds to dedup +
    run creation is POST-redaction, and the dedup hash is the canonical hash of
    the post-redaction payload;
  * warn/observe → advisory, the delivery proceeds;
  * canonical dedup hashing — logically identical payloads hash identically
    regardless of encoding, different payloads differ;
  * replay → the pass re-runs DETECTION-ONLY (no re-block);
  * mechanism error → fail-closed for block/redact guardrails, advisory for
    observe/warn-only;
  * the ``guardrail_blocked`` vocabulary value exists in
    ``VALIDATION_RESULT_VALUES``.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Generator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core.trigger_engine import (
    HmacValidationError,
    TimestampExpiredError,
    TriggerEngine,
    sha256_hex,
)
from modulo.core.trigger_engine.pre_guardrail import (
    GuardrailBlockedAtIntakeError,
    canonical_payload_hash,
    run_pre_trigger_guardrail_pass,
)
from modulo.db.models.trigger_event import VALIDATION_RESULT_VALUES
from modulo.db.models.webhook import WebhookDedupHash, WebhookPayload

_ORG = uuid.UUID("00000000-0000-0000-0000-000000000001")
_SNAP = uuid.UUID("00000000-0000-0000-0000-000000000003")
_RAW_BODY = b'{"body": "leak SECRET_ABC12345"}'
_RAW_PAYLOAD: dict[str, Any] = {"body": "leak SECRET_ABC12345"}

_VALID_32 = "a" * 32


@pytest.fixture(autouse=True)
def _org_not_paused() -> Generator[None, None, None]:
    with patch("modulo.db.settings_resolver.org_is_paused", new_callable=AsyncMock, return_value=False):
        yield


def _make_trigger(**overrides: Any) -> MagicMock:
    t = MagicMock()
    t.id = uuid.uuid4()
    t.pipeline_id = uuid.uuid4()
    t.organisation_id = _ORG
    t.active = True
    t.trigger_type = "webhook"
    t.config_json = {}
    t.max_concurrent_runs = 5
    for key, value in overrides.items():
        setattr(t, key, value)
    return t


def _guardrail_config(action: str, **extra: Any) -> dict[str, Any]:
    cfg: dict[str, Any] = {
        "action": action,
        "interception_point": "input",
        "type": "regex",
        "field": "body",
        "pattern": r"SECRET_[A-Z0-9]{8}",
    }
    cfg.update(extra)
    return cfg


def _make_guardrail_row(
    *,
    action: str,
    name: str = "no-secrets",
    config: dict[str, Any] | None = None,
) -> MagicMock:
    row = MagicMock()
    row.id = uuid.uuid4()
    row.organisation_id = _ORG
    row.pipeline_id = uuid.uuid4()
    row.node_id = None
    row.name = name
    row.eval_type = "guardrail"
    row.config_json = config or _guardrail_config(action)
    row.failure_behaviour = "warn"
    row.pass_threshold = None
    row.suite_id = None
    return row


def _make_session(
    *,
    trigger: MagicMock,
    guardrail_rows: list[MagicMock],
    dedup_exists: bool = False,
    active_run_count: int = 0,
    pipeline_rate_limit: dict[str, Any] | None = None,
    recent_run_count: int = 0,
    pipeline_found: bool = True,
) -> AsyncMock:
    """Call-count-routed session with real guardrail rows at call 3.

    Routing: 1=advisory lock, 2=trigger lookup, 3=guardrail rows (FAR-214),
    4=dedup SELECT, 5=dedup DELETE, 6=count active, 7=pipeline lookup,
    8=recent rate-limited count, 9+=other.
    """
    session = AsyncMock()

    lock_result = MagicMock()
    lock_result.scalar_one.return_value = True

    trigger_result = MagicMock()
    trigger_result.scalar_one_or_none.return_value = trigger
    trigger_result.scalar_one.return_value = trigger

    guardrail_result = MagicMock()
    guardrail_result.scalars.return_value.all.return_value = guardrail_rows

    dedup_result = MagicMock()
    dedup_result.scalar_one_or_none.return_value = MagicMock() if dedup_exists else None

    generic_result = MagicMock()

    count_result = MagicMock()
    count_result.scalar_one.return_value = active_run_count

    recent_count_result = MagicMock()
    recent_count_result.scalar_one.return_value = recent_run_count

    pipeline_result = MagicMock()
    pipeline_result.scalar_one_or_none.return_value = MagicMock() if pipeline_found else None
    if pipeline_found:
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
            return guardrail_result
        if call_count == 4:
            return dedup_result
        if call_count == 5:
            return generic_result
        if call_count == 6:
            return count_result
        if call_count == 7:
            return pipeline_result
        if call_count == 8:
            return recent_count_result
        return pipeline_result

    session.execute = AsyncMock(side_effect=_execute)
    session.add = MagicMock()
    session.flush = AsyncMock()

    nested_cm = AsyncMock()
    nested_cm.__aenter__ = AsyncMock(return_value=None)
    nested_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin_nested = MagicMock(return_value=nested_cm)
    return session


def _execute_call_count(session: AsyncMock) -> int:
    return session.execute.call_count


# ---------------------------------------------------------------------------
# canonical_payload_hash
# ---------------------------------------------------------------------------


class TestCanonicalPayloadHash:
    def test_key_order_irrelevant(self) -> None:
        assert canonical_payload_hash({"a": 1, "b": 2}) == canonical_payload_hash({"b": 2, "a": 1})

    def test_whitespace_irrelevant(self) -> None:
        left = {"a": [1, 2], "b": {"c": "x"}}
        right = {"a": [1, 2], "b": {"c": "x"}}
        assert canonical_payload_hash(left) == canonical_payload_hash(right)

    def test_unicode_content_participates_in_hash(self) -> None:
        # canonical_payload_hash serializes with ensure_ascii=False, so the
        # actual unicode codepoints participate in the digest: two payloads that
        # differ only in a non-ASCII character MUST hash differently (a
        # constant-returning hash would wrongly collapse them — this assertion
        # is therefore non-vacuous). The encoding-independence of the dedup key
        # (a literal "é" vs a JSON "\u00e9" raw body) is resolved by json.loads
        # ABOVE this seam — canonical_payload_hash only ever receives an already
        # parsed dict, so that property is not observable through this function
        # and is intentionally not asserted here.
        assert canonical_payload_hash({"name": "café"}) != canonical_payload_hash({"name": "cafe"})

    def test_different_payloads_differ(self) -> None:
        assert canonical_payload_hash({"event": "push", "ref": "main"}) != canonical_payload_hash(
            {"event": "push", "ref": "dev"}
        )

    def test_nested_key_order_irrelevant(self) -> None:
        left = {"payload": {"z": 1, "a": {"y": 2, "x": 3}}}
        right = {"payload": {"a": {"x": 3, "y": 2}, "z": 1}}
        assert canonical_payload_hash(left) == canonical_payload_hash(right)


# ---------------------------------------------------------------------------
# Pre-trigger pass ordering + block semantics
# ---------------------------------------------------------------------------


async def test_handle_webhook_block_fires_before_dedup() -> None:
    """A block-action guardrail must reject at intake BEFORE the dedup insert —
    the dedup SELECT/DELETE (calls 4/5) are never reached, so no dedup slot is
    consumed."""
    trigger = _make_trigger()
    session = _make_session(trigger=trigger, guardrail_rows=[_make_guardrail_row(action="block")])

    with (
        patch("modulo.core.trigger_engine.time.time", return_value=int(time.time())),
        pytest.raises(GuardrailBlockedAtIntakeError) as exc_info,
    ):
        await TriggerEngine().handle_webhook(
            session,
            trigger_id=trigger.id,
            org_id=_ORG,
            raw_body=_RAW_BODY,
            raw_payload=_RAW_PAYLOAD,
            hmac_signature=None,
            modulo_timestamp=str(int(time.time())),
            snapshot_id=_SNAP,
        )

    assert "no-secrets" in exc_info.value.detail
    # Only lock(1), trigger(2), guardrail(3) and the final unlock ran — the dedup
    # SELECT/DELETE (4/5) never executed, proving the pass ran before dedup.
    assert _execute_call_count(session) == 4

    # A ``guardrail_blocked`` TriggerEvent was recorded ...
    events = [c[0][0] for c in session.add.call_args_list if getattr(c[0][0], "validation_result", None)]
    blocked = [e for e in events if e.validation_result == "guardrail_blocked"]
    assert len(blocked) == 1
    assert "no-secrets" in (blocked[0].error_detail or "")

    # ... and the raw payload was stored for replay.
    stored = [c[0][0] for c in session.add.call_args_list if isinstance(c[0][0], WebhookPayload)]
    assert len(stored) == 1
    assert stored[0].raw_payload == _RAW_PAYLOAD
    # No dedup hash row was created (a blocked delivery never consumes a slot).
    assert not any(isinstance(c[0][0], WebhookDedupHash) for c in session.add.call_args_list)


async def test_handle_webhook_block_never_creates_run() -> None:
    trigger = _make_trigger()
    session = _make_session(trigger=trigger, guardrail_rows=[_make_guardrail_row(action="block")])

    with (
        patch("modulo.core.trigger_engine.create_run") as mock_create,
        patch("modulo.core.trigger_engine.time.time", return_value=int(time.time())),
        pytest.raises(GuardrailBlockedAtIntakeError),
    ):
        await TriggerEngine().handle_webhook(
            session,
            trigger_id=trigger.id,
            org_id=_ORG,
            raw_body=_RAW_BODY,
            raw_payload=_RAW_PAYLOAD,
            hmac_signature=None,
            modulo_timestamp=str(int(time.time())),
            snapshot_id=_SNAP,
        )

    mock_create.assert_not_called()


async def test_handle_webhook_clean_payload_not_blocked() -> None:
    """A non-violating payload passes a block-action guardrail and creates a run."""
    trigger = _make_trigger()
    session = _make_session(trigger=trigger, guardrail_rows=[_make_guardrail_row(action="block")])
    clean_payload = {"body": "clean text"}
    clean_body = b'{"body": "clean text"}'
    run_mock = MagicMock(id=uuid.uuid4())

    with (
        patch("modulo.core.trigger_engine.create_run", return_value=run_mock),
        patch("modulo.core.trigger_engine.time.time", return_value=int(time.time())),
    ):
        run, te, _ = await TriggerEngine().handle_webhook(
            session,
            trigger_id=trigger.id,
            org_id=_ORG,
            raw_body=clean_body,
            raw_payload=clean_payload,
            hmac_signature=None,
            modulo_timestamp=str(int(time.time())),
            snapshot_id=_SNAP,
        )

    assert run is run_mock
    assert te.validation_result == "accepted"
    # A dedup slot WAS consumed for the clean delivery (dedup SELECT+DELETE ran).
    assert _execute_call_count(session) == 8


# ---------------------------------------------------------------------------
# Redact semantics
# ---------------------------------------------------------------------------


async def test_handle_webhook_redact_applies_masks_before_dedup_and_run() -> None:
    """A redact-action guardrail applies masks at intake so the payload that
    proceeds to dedup + run creation is POST-redaction, and the dedup hash is
    the canonical hash of the POST-redaction payload."""
    trigger = _make_trigger()
    guardrail = _make_guardrail_row(
        action="redact",
        name="redact-key",
        config=_guardrail_config(
            "redact",
            redaction=[{"path": "credentials.api_key", "mode": "transform"}],
        ),
    )
    session = _make_session(trigger=trigger, guardrail_rows=[guardrail])
    payload = {"body": "clean text", "credentials": {"api_key": "sk-live-123"}}
    run_mock = MagicMock(id=uuid.uuid4())

    with (
        patch("modulo.core.trigger_engine.create_run", return_value=run_mock) as mock_create,
        patch("modulo.core.trigger_engine.time.time", return_value=int(time.time())),
    ):
        await TriggerEngine().handle_webhook(
            session,
            trigger_id=trigger.id,
            org_id=_ORG,
            raw_body=b"raw",
            raw_payload=payload,
            hmac_signature=None,
            modulo_timestamp=str(int(time.time())),
            snapshot_id=_SNAP,
        )

    # create_run received the POST-redaction payload.
    called_payload = mock_create.call_args.kwargs["input_payload"]
    assert called_payload["credentials"]["api_key"] == "\u2022\u2022\u2022\u2022\u2022\u2022"
    assert called_payload["body"] == "clean text"

    # The dedup hash row carries the canonical hash of the POST-redaction payload.
    dedup_added = [c[0][0] for c in session.add.call_args_list if isinstance(c[0][0], WebhookDedupHash)]
    assert len(dedup_added) == 1
    assert dedup_added[0].payload_hash == canonical_payload_hash(called_payload)
    # The accepted TriggerEvent carries the same canonical POST-guardrail hash.
    events = [c[0][0] for c in session.add.call_args_list if getattr(c[0][0], "validation_result", None)]
    accepted = [e for e in events if e.validation_result == "accepted"]
    assert len(accepted) == 1
    assert accepted[0].raw_payload_hash == dedup_added[0].payload_hash


async def test_handle_webhook_no_guardrails_uses_canonical_dedup_hash() -> None:
    """Even with zero guardrails bound, the dedup key is the canonical payload
    hash (closing the raw-body-hash encoding-bypass residual), not the raw body."""
    trigger = _make_trigger()
    session = _make_session(trigger=trigger, guardrail_rows=[])
    run_mock = MagicMock(id=uuid.uuid4())

    with (
        patch("modulo.core.trigger_engine.create_run", return_value=run_mock),
        patch("modulo.core.trigger_engine.time.time", return_value=int(time.time())),
    ):
        await TriggerEngine().handle_webhook(
            session,
            trigger_id=trigger.id,
            org_id=_ORG,
            raw_body=b'{"b": 2, "a": 1}',
            raw_payload={"b": 2, "a": 1},
            hmac_signature=None,
            modulo_timestamp=str(int(time.time())),
            snapshot_id=_SNAP,
        )

    dedup_added = [c[0][0] for c in session.add.call_args_list if isinstance(c[0][0], WebhookDedupHash)]
    assert len(dedup_added) == 1
    assert dedup_added[0].payload_hash == canonical_payload_hash({"b": 2, "a": 1})


# ---------------------------------------------------------------------------
# warn / observe advisory
# ---------------------------------------------------------------------------


async def test_handle_webhook_warn_is_advisory() -> None:
    """A warn-action guardrail firing must NOT block the delivery — the run is
    created and the dedup slot consumed."""
    trigger = _make_trigger()
    session = _make_session(trigger=trigger, guardrail_rows=[_make_guardrail_row(action="warn")])
    run_mock = MagicMock(id=uuid.uuid4())

    with (
        patch("modulo.core.trigger_engine.create_run", return_value=run_mock),
        patch("modulo.core.trigger_engine.time.time", return_value=int(time.time())),
    ):
        run, te, _ = await TriggerEngine().handle_webhook(
            session,
            trigger_id=trigger.id,
            org_id=_ORG,
            raw_body=_RAW_BODY,
            raw_payload=_RAW_PAYLOAD,
            hmac_signature=None,
            modulo_timestamp=str(int(time.time())),
            snapshot_id=_SNAP,
        )

    assert run is run_mock
    assert te.validation_result == "accepted"
    assert _execute_call_count(session) == 8


async def test_handle_webhook_observe_is_advisory() -> None:
    trigger = _make_trigger()
    session = _make_session(trigger=trigger, guardrail_rows=[_make_guardrail_row(action="observe")])
    run_mock = MagicMock(id=uuid.uuid4())

    with (
        patch("modulo.core.trigger_engine.create_run", return_value=run_mock),
        patch("modulo.core.trigger_engine.time.time", return_value=int(time.time())),
    ):
        run, te, _ = await TriggerEngine().handle_webhook(
            session,
            trigger_id=trigger.id,
            org_id=_ORG,
            raw_body=_RAW_BODY,
            raw_payload=_RAW_PAYLOAD,
            hmac_signature=None,
            modulo_timestamp=str(int(time.time())),
            snapshot_id=_SNAP,
        )

    assert run is run_mock
    assert te.validation_result == "accepted"


# ---------------------------------------------------------------------------
# Mechanism errors — fail closed / advisory
# ---------------------------------------------------------------------------


async def test_handle_webhook_mechanism_error_fails_closed_for_block() -> None:
    """A capability source unreadable must NOT let a blocked payload through:
    a mechanism error with a block/redact-action guardrail bound rejects at the
    boundary."""
    trigger = _make_trigger()
    session = _make_session(trigger=trigger, guardrail_rows=[_make_guardrail_row(action="block")])

    with (
        patch(
            "modulo.core.trigger_engine.pre_guardrail.run_interception_pass",
            side_effect=RuntimeError("eval backend unavailable"),
        ),
        pytest.raises(GuardrailBlockedAtIntakeError) as exc_info,
    ):
        await TriggerEngine().handle_webhook(
            session,
            trigger_id=trigger.id,
            org_id=_ORG,
            raw_body=_RAW_BODY,
            raw_payload=_RAW_PAYLOAD,
            hmac_signature=None,
            modulo_timestamp=str(int(time.time())),
            snapshot_id=_SNAP,
        )

    assert "mechanism error" in exc_info.value.detail
    assert _execute_call_count(session) == 4  # no dedup consumed


async def test_handle_webhook_mechanism_error_advisory_for_observe() -> None:
    """A mechanism error with observe/warn-only guardrails logs-and-continues —
    advisory guardrails never fail the delivery closed."""
    trigger = _make_trigger()
    session = _make_session(trigger=trigger, guardrail_rows=[_make_guardrail_row(action="observe")])
    run_mock = MagicMock(id=uuid.uuid4())

    with (
        patch("modulo.core.trigger_engine.create_run", return_value=run_mock),
        patch(
            "modulo.core.trigger_engine.pre_guardrail.run_interception_pass",
            side_effect=RuntimeError("eval backend unavailable"),
        ),
        patch("modulo.core.trigger_engine.time.time", return_value=int(time.time())),
    ):
        run, te, _ = await TriggerEngine().handle_webhook(
            session,
            trigger_id=trigger.id,
            org_id=_ORG,
            raw_body=_RAW_BODY,
            raw_payload=_RAW_PAYLOAD,
            hmac_signature=None,
            modulo_timestamp=str(int(time.time())),
            snapshot_id=_SNAP,
        )

    assert run is run_mock
    assert te.validation_result == "accepted"


# ---------------------------------------------------------------------------
# Replay — detection-only
# ---------------------------------------------------------------------------


def _make_replay_session(
    *,
    event: MagicMock,
    trigger: MagicMock,
    stored_payload: MagicMock,
    guardrail_rows: list[MagicMock],
) -> AsyncMock:
    """Call-count-routed replay session with real guardrail rows at call 5.

    Routing: 1=TriggerEvent lookup, 2=advisory lock, 3=Trigger lookup,
    4=WebhookPayload lookup, 5=guardrail rows (detection-only), 6=count active,
    7=pipeline lookup, 8=recent count, 9+=other.
    """
    session = AsyncMock()

    event_result = MagicMock()
    event_result.scalar_one_or_none.return_value = event

    lock_result = MagicMock()
    lock_result.scalar_one.return_value = True

    trigger_result = MagicMock()
    trigger_result.scalar_one_or_none.return_value = trigger

    payload_result = MagicMock()
    payload_result.scalar_one_or_none.return_value = stored_payload

    guardrail_result = MagicMock()
    guardrail_result.scalars.return_value.all.return_value = guardrail_rows

    count_result = MagicMock()
    count_result.scalar_one.return_value = 0

    pipeline_result = MagicMock()
    pipeline_result.scalar_one_or_none.return_value = None

    recent_count_result = MagicMock()
    recent_count_result.scalar_one.return_value = 0

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
            return guardrail_result
        if call_count == 6:
            return count_result
        if call_count == 7:
            return pipeline_result
        if call_count == 8:
            return recent_count_result
        return pipeline_result

    session.execute = AsyncMock(side_effect=_execute)
    session.add = MagicMock()
    session.flush = AsyncMock()
    return session


async def test_replay_event_pre_trigger_pass_is_detection_only() -> None:
    """A replay of a still-violating payload re-runs the pass DETECTION-ONLY —
    consistent with the run-creation seam — so it is NOT re-blocked and the run
    is created."""
    trigger = _make_trigger()
    event = MagicMock()
    event.id = uuid.uuid4()
    event.trigger_id = trigger.id
    stored = MagicMock()
    stored.raw_body = _RAW_BODY
    stored.raw_payload = _RAW_PAYLOAD
    session = _make_replay_session(
        event=event,
        trigger=trigger,
        stored_payload=stored,
        guardrail_rows=[_make_guardrail_row(action="block")],
    )
    run_mock = MagicMock(id=uuid.uuid4())

    with patch("modulo.core.trigger_engine.create_run", return_value=run_mock):
        run, te, _ = await TriggerEngine().replay_event(
            session,
            event_id=event.id,
            org_id=_ORG,
            snapshot_id=_SNAP,
        )

    assert run is run_mock
    assert te.validation_result == "accepted"
    blocked = [
        c[0][0]
        for c in session.add.call_args_list
        if getattr(c[0][0], "validation_result", None) == "guardrail_blocked"
    ]
    assert blocked == []


# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------


def test_guardrail_blocked_in_vocabulary() -> None:
    assert "guardrail_blocked" in VALIDATION_RESULT_VALUES


async def test_run_pre_trigger_guardrail_pass_zero_definitions_fast_path() -> None:
    """Zero guardrails bound → the outcome carries the unmodified payload and
    never blocks."""
    session = AsyncMock()
    session.execute = AsyncMock(return_value=MagicMock())
    session.execute.return_value.scalars.return_value.all.return_value = []
    outcome = await run_pre_trigger_guardrail_pass(
        session,
        org_id=_ORG,
        pipeline_id=uuid.uuid4(),
        raw_payload={"a": 1},
    )
    assert outcome.blocked is False
    assert outcome.payload == {"a": 1}


# ---------------------------------------------------------------------------
# Pre-guardrail failure events keep the RAW-body hash (behaviour 6)
# ---------------------------------------------------------------------------


def _assert_failure_event_keeps_raw_body_hash(session: AsyncMock, expected_vr: str) -> None:
    """A pre-guardrail failure event describes the RAW delivery, so its
    ``raw_payload_hash`` must be the raw-body hash — NOT the canonical
    POST-guardrail payload hash (which is reserved for dedup/run-creation
    events)."""
    events = [c[0][0] for c in session.add.call_args_list if getattr(c[0][0], "validation_result", None)]
    matched = [e for e in events if e.validation_result == expected_vr]
    assert len(matched) == 1, f"expected exactly one {expected_vr} event, got {len(matched)}"
    assert matched[0].raw_payload_hash == sha256_hex(_RAW_BODY)
    assert matched[0].raw_payload_hash != canonical_payload_hash(_RAW_PAYLOAD)


async def test_handle_webhook_timestamp_failure_keeps_raw_body_hash() -> None:
    """timestamp_expired is a PRE-guardrail failure event — the canonical
    POST-guardrail hash does not exist yet (the pass never ran), so the event
    records the raw-body hash."""
    trigger = _make_trigger()
    session = _make_session(trigger=trigger, guardrail_rows=[])

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

    _assert_failure_event_keeps_raw_body_hash(session, "timestamp_expired")


async def test_handle_webhook_hmac_failure_keeps_raw_body_hash() -> None:
    """hmac_failed is a PRE-guardrail failure event (auth precedes the pass) —
    records the raw-body hash, not the canonical hash."""
    trigger = _make_trigger()
    trigger.config_json = {"hmac_secret": "secret"}
    session = _make_session(trigger=trigger, guardrail_rows=[])

    with pytest.raises(HmacValidationError):
        await TriggerEngine().handle_webhook(
            session,
            trigger_id=trigger.id,
            org_id=_ORG,
            raw_body=_RAW_BODY,
            raw_payload=_RAW_PAYLOAD,
            hmac_signature="sha256=bad",
            modulo_timestamp=str(int(time.time())),
            snapshot_id=_SNAP,
        )

    _assert_failure_event_keeps_raw_body_hash(session, "hmac_failed")


async def test_handle_webhook_event_filter_failure_keeps_raw_body_hash() -> None:
    """event_type_not_accepted is a PRE-guardrail failure event (event filters
    run before the pass) — records the raw-body hash, not the canonical hash."""
    trigger = _make_trigger()
    trigger.config_json = {"accepted_events": ["pull_request"]}
    session = _make_session(trigger=trigger, guardrail_rows=[])

    with pytest.raises(RuntimeError, match="none of the accepted event types"):
        await TriggerEngine().handle_webhook(
            session,
            trigger_id=trigger.id,
            org_id=_ORG,
            raw_body=_RAW_BODY,
            raw_payload=_RAW_PAYLOAD,
            hmac_signature=None,
            modulo_timestamp=str(int(time.time())),
            snapshot_id=_SNAP,
        )

    _assert_failure_event_keeps_raw_body_hash(session, "event_type_not_accepted")


async def test_handle_webhook_encrypted_hmac_secret_roundtrips() -> None:
    """Dispatch path correctly decrypts a base64-encrypted hmac_secret.

    This is the exact shape the API write path (``_encrypt_trigger_config_secrets``)
    now stores: a base64 ``gAAAA...`` string in ``config_json``. The intake path
    must decrypt it back to the plaintext before HMAC verification, proving the
    write/read storage types agree.
    """
    import hashlib
    import hmac as hmac_mod

    from cryptography.fernet import Fernet

    from modulo.auth.secret_storage import encrypt_stored_secret

    plaintext_secret = "whsec_encrypted_intake_1234567890"
    fernet_key = Fernet.generate_key().decode()
    stored_encrypted = encrypt_stored_secret(plaintext_secret, fernet_key).decode()
    assert stored_encrypted.startswith("gAAAA")

    ts = str(int(time.time()))
    hmac_payload = f"{ts}.".encode() + _RAW_BODY
    sig = "sha256=" + hmac_mod.new(plaintext_secret.encode(), hmac_payload, hashlib.sha256).hexdigest()

    trigger = _make_trigger()
    trigger.config_json = {"hmac_secret": stored_encrypted}
    session = _make_session(trigger=trigger, guardrail_rows=[])

    fake_settings = MagicMock()
    fake_settings.fernet_key = fernet_key
    with (
        patch("modulo.core.trigger_engine.create_run", return_value=MagicMock(id=uuid.uuid4())),
        patch("modulo.core.trigger_engine.time.time", return_value=int(time.time())),
        patch("modulo.settings.get_settings", return_value=fake_settings),
    ):
        _, te, _ = await TriggerEngine().handle_webhook(
            session,
            trigger_id=trigger.id,
            org_id=_ORG,
            raw_body=_RAW_BODY,
            raw_payload=_RAW_PAYLOAD,
            hmac_signature=sig,
            modulo_timestamp=ts,
            snapshot_id=_SNAP,
        )

    assert te.validation_result == "accepted"


async def test_handle_webhook_encrypted_hmac_secret_rejects_tampered_payload() -> None:
    """Tampering with an encrypted-secret webhook is still rejected."""
    from cryptography.fernet import Fernet

    from modulo.auth.secret_storage import encrypt_stored_secret

    plaintext_secret = "whsec_encrypted_intake_1234567890"
    fernet_key = Fernet.generate_key().decode()
    stored_encrypted = encrypt_stored_secret(plaintext_secret, fernet_key).decode()

    ts = str(int(time.time()))
    sig = "sha256=bad0deadbeef"

    trigger = _make_trigger()
    trigger.config_json = {"hmac_secret": stored_encrypted}
    session = _make_session(trigger=trigger, guardrail_rows=[])

    fake_settings = MagicMock()
    fake_settings.fernet_key = fernet_key
    with (
        patch("modulo.settings.get_settings", return_value=fake_settings),
        pytest.raises(HmacValidationError),
    ):
        await TriggerEngine().handle_webhook(
            session,
            trigger_id=trigger.id,
            org_id=_ORG,
            raw_body=_RAW_BODY,
            raw_payload=_RAW_PAYLOAD,
            hmac_signature=sig,
            modulo_timestamp=ts,
            snapshot_id=_SNAP,
        )

    _assert_failure_event_keeps_raw_body_hash(session, "hmac_failed")
