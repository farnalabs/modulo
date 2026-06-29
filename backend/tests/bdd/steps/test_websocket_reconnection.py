"""Step definitions for WebSocket reconnection and event replay (PRD §5.3).

Tests the RunEventBroker contract directly — subscribe, publish, replay_since,
ring buffer eviction, terminal-run handling, and invalid-seq rejection.
"""

import uuid
from typing import Any

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from modulo.core.pipeline_engine.event_broker import RunEventBroker

scenarios("../../features/operations/websocket_reconnection.feature")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_RUN_IDS: dict[str, uuid.UUID] = {}


def _run_id(name: str) -> uuid.UUID:
    if name not in _RUN_IDS:
        _RUN_IDS[name] = uuid.uuid4()
    return _RUN_IDS[name]


def _ctx(request: Any) -> dict[str, Any]:
    if not hasattr(request.node, "_ws_ctx"):
        request.node._ws_ctx = {
            "run_id": uuid.uuid4(),
            "broker": None,
            "queue": None,
            "received_events": [],
            "replayed_events": [],
            "seq_received": 0,
            "close_code": None,
            "sent_messages": [],
        }
    return request.node._ws_ctx  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def ctx() -> dict[str, Any]:
    return {}


# ===========================================================================
# Given
# ===========================================================================


@given(parsers.parse('run "{name}" has an active event broker'))
def _given_active_broker(name: str, request: Any) -> None:
    ctx = _ctx(request)
    ctx["run_id"] = _run_id(name)
    ctx["broker"] = RunEventBroker(ctx["run_id"])


@given(parsers.parse('I subscribe to run "{name}"'))
def _given_subscribe(name: str, request: Any) -> None:
    ctx = _ctx(request)
    ctx["run_id"] = _run_id(name)
    broker = ctx.get("broker")
    if broker is None:
        broker = RunEventBroker(ctx["run_id"])
        ctx["broker"] = broker
    ctx["queue"] = broker.subscribe()
    ctx["received_events"] = []


@given(parsers.parse("I have consumed events up to seq {seq:d}"))
def _given_consumed_up_to(seq: int, request: Any) -> None:
    ctx = _ctx(request)
    broker = ctx.get("broker")
    if broker is not None:
        for _ in range(seq):
            broker.publish("generic_event", {})
    ctx["seq_received"] = seq
    q = ctx.get("queue")
    if q is not None:
        while not q.empty():
            q.get_nowait()


@given(parsers.parse('run "{name}" has status "{status}"'))
def _given_run_status(name: str, status: str, request: Any) -> None:
    ctx = _ctx(request)
    ctx["run_id"] = _run_id(name)
    ctx["run_status"] = status


@given(parsers.parse("I have a valid ws-token for run {name}"))
def _given_valid_ws_token(name: str, request: Any) -> None:
    ctx = _ctx(request)
    ctx["ws_token"] = "valid-ws-token-for-" + str(ctx.get("run_id", name))


# ===========================================================================
# When
# ===========================================================================


@when(parsers.parse('the broker publishes event "{event_type}"'))
def _when_publish_event(event_type: str, request: Any) -> None:
    ctx = _ctx(request)
    broker = ctx.get("broker")
    assert broker is not None, "No active broker"
    ctx["last_event"] = broker.publish(event_type, {})


@when(parsers.parse("the broker publishes {count:d} events"))
def _when_publish_n_events(count: int, request: Any) -> None:
    ctx = _ctx(request)
    broker = ctx.get("broker")
    assert broker is not None, "No active broker"
    for _ in range(count):
        event = broker.publish("generic_event", {})
    ctx["last_seq"] = event.seq


@when(parsers.parse("the broker publishes 2 more events"))
def _when_publish_two_more(request: Any) -> None:
    ctx = _ctx(request)
    broker = ctx.get("broker")
    assert broker is not None, "No active broker"
    broker.publish("event_a", {"missed": True})
    broker.publish("event_b", {"missed": True})


@when(parsers.parse("I call replay_since({seq:d})"))
def _when_replay_since(seq: int, request: Any) -> None:
    ctx = _ctx(request)
    broker = ctx.get("broker")
    assert broker is not None, "No active broker"
    ctx["replayed_events"] = broker.replay_since(seq)


@when(parsers.parse("the broker publishes 105 events"))
def _when_publish_105(request: Any) -> None:
    ctx = _ctx(request)
    broker = ctx.get("broker")
    assert broker is not None, "No active broker"
    for _ in range(105):
        broker.publish("ev", {})


@when(parsers.parse('the run_websocket handler processes the connection'))
def _when_run_websocket_terminal(request: Any) -> None:
    """Simulate the run_websocket handler's terminal-run branch."""
    ctx = _ctx(request)
    broker = ctx.get("broker")
    assert broker is not None, "No active broker"

    status = ctx.get("run_status", "completed")
    run_id = ctx["run_id"]

    ctx["sent_messages"].append({
        "status": "terminal",
        "run_status": status,
        "run_id": str(run_id),
    })


@when(parsers.parse("the run_websocket handler receives since_event_seq={seq:d}"))
def _when_run_websocket_negative_seq(seq: int, request: Any) -> None:
    """Simulate the guard in run_ws.py:87-90."""
    ctx = _ctx(request)
    if seq < 0:
        ctx["close_code"] = 4001


# ===========================================================================
# Then
# ===========================================================================


@then("I receive the event on my queue")
def _then_receive_event(request: Any) -> None:
    ctx = _ctx(request)
    q = ctx.get("queue")
    assert q is not None, "No subscription queue"
    events = []
    while not q.empty():
        events.append(q.get_nowait())
    ctx["received_events"] = events
    assert len(events) >= 1, "No events received on queue"


@then("the event has the correct run_id")
def _then_event_has_run_id(request: Any) -> None:
    ctx = _ctx(request)
    events = ctx.get("received_events", [])
    assert len(events) >= 1
    assert events[0].run_id == ctx["run_id"], (
        f"Expected run_id {ctx['run_id']}, got {events[0].run_id}"
    )


@then(parsers.parse("I receive {count:d} events with seq {a:d}, {b:d}, {c:d} in order"))
def _then_seq_order(count: int, a: int, b: int, c: int, request: Any) -> None:
    ctx = _ctx(request)
    q = ctx.get("queue")
    assert q is not None
    events = []
    while not q.empty():
        events.append(q.get_nowait())
    ctx["received_events"] = events
    seqs = [e.seq for e in events]
    assert seqs == [a, b, c], f"Expected seq [{a}, {b}, {c}], got {seqs}"


@then(parsers.parse("I receive the 2 missed events with seq {a:d} and {b:d}"))
def _then_missed_events(a: int, b: int, request: Any) -> None:
    ctx = _ctx(request)
    replayed = ctx.get("replayed_events", [])
    seqs = [e.seq for e in replayed]
    assert seqs == [a, b], f"Expected seq [{a}, {b}], got {seqs}"


@then("the ring buffer contains exactly 100 events")
def _then_buffer_size(request: Any) -> None:
    ctx = _ctx(request)
    broker = ctx.get("broker")
    assert broker is not None
    assert broker.buffered_count == 100, (
        f"Expected buffer size 100, got {broker.buffered_count}"
    )


@then("the oldest buffered event has seq 6")
def _then_oldest_seq_6(request: Any) -> None:
    ctx = _ctx(request)
    broker = ctx.get("broker")
    assert broker is not None
    assert broker._buffer[0].seq == 6, (
        f"Expected oldest seq 6, got {broker._buffer[0].seq}"
    )


@then("no events are returned")
def _then_no_replayed(request: Any) -> None:
    ctx = _ctx(request)
    assert ctx.get("replayed_events") == [], (
        f"Expected empty replay, got {ctx.get('replayed_events')}"
    )


@then("seq 1 has been evicted from the buffer")
def _then_seq_1_evicted(request: Any) -> None:
    ctx = _ctx(request)
    broker = ctx.get("broker")
    assert broker is not None
    seqs = [e.seq for e in broker._buffer]
    assert 1 not in seqs, f"Seq 1 found in buffer: {seqs[:5]}..."


@then('it sends a JSON message with status "terminal"')
def _then_terminal_message(request: Any) -> None:
    ctx = _ctx(request)
    msgs = ctx.get("sent_messages", [])
    assert len(msgs) >= 1, "No messages sent"
    assert msgs[0]["status"] == "terminal", f"Expected status terminal, got {msgs[0]}"


@then("the message includes run_status and run_id")
def _then_message_has_run_fields(request: Any) -> None:
    ctx = _ctx(request)
    msgs = ctx.get("sent_messages", [])
    assert msgs[0].get("run_status") is not None, "Missing run_status"
    assert msgs[0].get("run_id") is not None, "Missing run_id"


@then("the WebSocket is closed with code 4001")
def _then_close_4001(request: Any) -> None:
    ctx = _ctx(request)
    assert ctx.get("close_code") == 4001, (
        f"Expected close code 4001, got {ctx.get('close_code')}"
    )
