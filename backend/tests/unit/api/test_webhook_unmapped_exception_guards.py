"""Fail-loud guards on the webhook intake/dispatch error mappers.

Each mapper is written against a fixed tuple of exception types (the ones its
callers actually catch). The final arm raises ``AssertionError`` if an
unmapped type ever reaches it, so a drift in the caught tuple surfaces as a
loud, diagnosable failure instead of a silent mis-mapping. That arm is
deliberately unreachable in normal operation, so it needs a direct test.

These tests call the helpers directly with an exception type no mapper
recognises and pin the exact loud-raise message. They reach the guard BEFORE
any await, so no session/DB machinery is needed.
"""

from __future__ import annotations

import uuid

import pytest

from modulo.api.routes.webhooks import (
    _handle_receive_dispatch_failure,
    _handle_replay_dispatch_failure,
    _raise_intake_http_error,
    _raise_receive_refusal,
    _raise_replay_refusal,
    _WebhookState,
)

_TRIGGER_ID = uuid.uuid4()
_EVENT_ID = uuid.uuid4()


def test_intake_mapper_fails_loud_on_unmapped_exception() -> None:
    """An exception outside ``_INTAKE_ERROR_RESPONSES`` never falls through silently."""
    with pytest.raises(AssertionError, match="Unmapped intake error: RuntimeError"):
        _raise_intake_http_error(
            RuntimeError("unmapped intake"),
            trigger_id=_TRIGGER_ID,
            code="webhooks.receive_webhook",
        )


@pytest.mark.asyncio
async def test_receive_refusal_fails_loud_on_unmapped_exception() -> None:
    """A refusal type outside the receive tuple raises rather than half-mapping."""
    with pytest.raises(AssertionError, match="Unmapped receive refusal: RuntimeError"):
        await _raise_receive_refusal(
            RuntimeError("unmapped refusal"),
            trigger_id=_TRIGGER_ID,
            state=_WebhookState(),
            raw_body=b"{}",
            raw_payload={},
        )


@pytest.mark.asyncio
async def test_replay_refusal_fails_loud_on_unmapped_exception() -> None:
    """A refusal type outside the replay tuple raises rather than half-mapping."""
    with pytest.raises(AssertionError, match="Unmapped replay refusal: RuntimeError"):
        await _raise_replay_refusal(
            RuntimeError("unmapped refusal"),
            trigger_id=_TRIGGER_ID,
            state=_WebhookState(),
            event_id=_EVENT_ID,
        )


@pytest.mark.asyncio
async def test_receive_dispatch_failure_fails_loud_on_unmapped_exception() -> None:
    """A dispatch failure outside the receive tuple raises rather than half-mapping."""
    with pytest.raises(AssertionError, match="Unmapped receive dispatch failure: RuntimeError"):
        await _handle_receive_dispatch_failure(
            RuntimeError("unmapped dispatch failure"),
            trigger_id=_TRIGGER_ID,
            state=_WebhookState(),
            raw_body=b"{}",
            raw_payload={},
        )


@pytest.mark.asyncio
async def test_replay_dispatch_failure_fails_loud_on_unmapped_exception() -> None:
    """A dispatch failure outside the replay tuple raises rather than half-mapping."""
    with pytest.raises(AssertionError, match="Unmapped replay dispatch failure: RuntimeError"):
        await _handle_replay_dispatch_failure(
            RuntimeError("unmapped dispatch failure"),
            trigger_id=_TRIGGER_ID,
            state=_WebhookState(),
            event_id=_EVENT_ID,
        )
