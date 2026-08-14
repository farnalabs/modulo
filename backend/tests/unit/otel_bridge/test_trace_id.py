"""Unit tests for the deterministic trace-id helpers (FAR-198)."""

import uuid

from modulo.otel_bridge import trace_id_for_run, trace_id_for_thread

_NAMESPACE = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")


def test_trace_id_for_thread_matches_uuid5_hex() -> None:
    thread_id = "org-uuid:run-uuid"
    assert trace_id_for_thread(thread_id) == uuid.uuid5(_NAMESPACE, thread_id).hex
    assert len(trace_id_for_thread(thread_id)) == 32


def test_trace_id_for_thread_is_deterministic() -> None:
    thread_id = "org-uuid:run-uuid"
    assert trace_id_for_thread(thread_id) == trace_id_for_thread(thread_id)


def test_trace_id_for_thread_differs_across_threads() -> None:
    assert trace_id_for_thread("a:1") != trace_id_for_thread("a:2")


def test_trace_id_for_run_matches_api_derivation() -> None:
    org_id = uuid.uuid4()
    run_id = uuid.uuid4()
    thread_id = f"{org_id}:{run_id}"
    # Mirrors create_run's langgraph_thread_id format.
    assert trace_id_for_run(org_id, run_id) == trace_id_for_thread(thread_id)
    assert trace_id_for_run(str(org_id), str(run_id)) == trace_id_for_thread(thread_id)


def test_trace_id_for_run_none_safe_raises() -> None:
    with __import__("pytest").raises((TypeError, ValueError)):
        trace_id_for_run(None, "not-a-uuid")  # type: ignore[arg-type]
