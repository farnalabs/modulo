"""Unit tests for the run WebSocket forwarder's read-side sanitization (FAR-163).

The executor sanitizes error detail at the publish/write site, but the WS
forwarder must never ship a raw traceback to the browser even if a future
publisher skips that step — ``_sanitize_event`` scrubs the error-carrying
payload keys before ``send_json``.
"""

import uuid

from modulo.api.routes.run_ws import _sanitize_event
from modulo.core.pipeline_engine.event_broker import RunEvent


def _event(payload: dict) -> RunEvent:
    return RunEvent(seq=1, event_type="run_failed", run_id=uuid.uuid4(), payload=payload)


def test_sanitize_event_redacts_db_url_from_detail():
    data = _sanitize_event(
        _event({"error": "RuntimeError", "detail": "postgresql://user:supersecret@db.example/modulo"})
    )
    assert "supersecret" not in data["payload"]["detail"]
    assert "<redacted>" in data["payload"]["detail"]


def test_sanitize_event_redacts_bearer_token_from_error():
    data = _sanitize_event(_event({"error": "RuntimeError", "detail": "Bearer tok1234567890 failed"}))
    assert "tok1234567890" not in data["payload"]["detail"]
    assert "<redacted>" in data["payload"]["detail"]


def test_sanitize_event_redacts_stall_reason():
    data = _sanitize_event(_event({"node_id": "node-a", "stall_reason": "stalled with Bearer tok1234567890"}))
    assert "tok1234567890" not in data["payload"]["stall_reason"]
    assert "<redacted>" in data["payload"]["stall_reason"]


def test_sanitize_event_passes_non_error_payload_through_unchanged():
    payload = {"node_id": "node-a", "output": {"status": "completed", "summary": "all good"}}
    data = _sanitize_event(_event(payload))
    assert data["payload"] == payload


def test_sanitize_event_does_not_mutate_the_original_event():
    payload = {"error": "RuntimeError", "detail": "Bearer tok1234567890"}
    event = _event(payload)
    _sanitize_event(event)
    assert event.payload["detail"] == "Bearer tok1234567890"
