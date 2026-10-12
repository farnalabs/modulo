"""Unit tests for the deterministic trace-id helpers (FAR-198)."""

import uuid

import pytest

from modulo.otel_bridge import trace_id_for_run, trace_id_for_thread
from modulo.otel_bridge.trace_id import trace_id_int_for_thread

_NAMESPACE = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")


def test_trace_id_for_thread_matches_uuid5_hex_and_is_deterministic() -> None:
    """Pins the wire format (32 lower-case hex) and determinism for one thread id."""
    thread_id = "org-uuid:run-uuid"
    result = trace_id_for_thread(thread_id)
    assert result == uuid.uuid5(_NAMESPACE, thread_id).hex
    assert len(result) == 32
    assert result == result.lower(), "OTel trace ids must be lower-case hex"
    # Determinism: the same thread id always maps to the same trace id.
    assert trace_id_for_thread(thread_id) == result


def test_trace_id_int_for_thread_is_the_int_form_of_the_hex_id() -> None:
    """The OTel SpanContext form must be the same id the API reports, as an int.

    ``start_run_root`` seeds the SpanContext from this int while ``RunResponse``
    reports the hex string; if the two ever diverge, exported spans land in a
    different trace than the API told the caller (FAR-198).
    """
    thread_id = "org-uuid:run-uuid"
    hex_id = trace_id_for_thread(thread_id)
    as_int = trace_id_int_for_thread(thread_id)

    assert as_int == int(hex_id, 16)
    assert as_int == uuid.uuid5(_NAMESPACE, thread_id).int
    # OTel trace ids are unsigned 128-bit; anything wider/longer would be truncated.
    assert 0 <= as_int < 2**128
    assert f"{as_int:032x}" == hex_id


def test_trace_id_for_thread_differs_across_threads() -> None:
    assert trace_id_for_thread("a:1") != trace_id_for_thread("a:2")


def test_trace_id_for_run_coerces_str_and_uuid_to_the_same_thread_id() -> None:
    """str and UUID inputs must produce an identical thread id (and trace id).

    ``create_run`` builds the thread id from ``uuid.UUID`` objects while the OTel
    bridge receives a plain ``str``; mixed forms must not fork the trace.
    """
    org_id = uuid.uuid4()
    run_id = uuid.uuid4()
    thread_id = f"{org_id}:{run_id}"  # Mirrors create_run's langgraph_thread_id format.

    assert trace_id_for_run(org_id, run_id) == trace_id_for_thread(thread_id)
    assert trace_id_for_run(str(org_id), str(run_id)) == trace_id_for_thread(thread_id)
    # Mixed forms must agree too.
    assert trace_id_for_run(str(org_id), run_id) == trace_id_for_thread(thread_id)
    assert trace_id_for_run(org_id, str(run_id)) == trace_id_for_thread(thread_id)


def test_trace_id_for_run_normalises_uppercase_and_bare_hex_input() -> None:
    """UUID strings in non-canonical forms must normalise to the same trace id.

    An uppercase or dash-less uuid string is the same UUID, so it must derive
    the same thread id as the canonical dashed lower-case form.
    """
    org_id = uuid.uuid4()
    run_id = uuid.uuid4()

    assert trace_id_for_run(str(org_id).upper(), str(run_id).upper()) == trace_id_for_run(org_id, run_id)
    assert trace_id_for_run(str(org_id).replace("-", ""), str(run_id)) == trace_id_for_run(org_id, run_id)


@pytest.mark.parametrize(
    ("org_id", "run_id"),
    [
        pytest.param(None, "not-a-uuid", id="none-org"),
        pytest.param("not-a-uuid", None, id="none-run"),
        pytest.param("not-a-uuid", "also-not-a-uuid", id="both-malformed"),
        pytest.param("not-a-uuid", "11111111-1111-1111-1111-111111111111", id="malformed-org-valid-run"),
    ],
)
def test_trace_id_for_run_rejects_non_uuid_input(org_id: object, run_id: object) -> None:
    """Malformed ids must fail loudly rather than derive a bogus trace id."""
    with pytest.raises((TypeError, ValueError)):
        trace_id_for_run(org_id, run_id)  # type: ignore[arg-type]
