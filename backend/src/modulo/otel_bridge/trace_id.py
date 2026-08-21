"""Deterministic OTel trace-id derivation (FAR-198).

The run-level ``trace_id`` exposed on ``RunResponse`` is a deterministic
uuid5 of the run's LangGraph ``thread_id`` (``"{org_id}:{run_id}"``). This
module is the SINGLE source of truth for that derivation so the API response,
the OTel span context seeded before graph execution, and error-tracking event
contexts all agree byte-for-byte.

Import contract (enforced by import-linter): ``modulo.otel_bridge`` must not
import ``modulo.core.pipeline_engine``, ``hitl_manager``, or ``eval_engine``.
This module imports only the standard library.
"""

import uuid

__all__ = [
    "NAMESPACE_TRACE",
    "trace_id_for_run",
    "trace_id_for_thread",
    "trace_id_int_for_thread",
]

#: DNS namespace UUID — the same namespace the original API code used
#: (``runs.py _NAMESPACE_TRACE``) so existing deterministic ids are preserved.
NAMESPACE_TRACE = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")


def trace_id_for_thread(thread_id: str) -> str:
    """Return the 32-hex OTel trace id derived from a LangGraph thread id.

    The value is ``uuid.uuid5(NAMESPACE_TRACE, thread_id).hex`` — identical to
    what the API reported historically, minus the dashes, so it matches the
    OTel trace-id wire format (16 bytes, 32 hex chars).
    """
    return uuid.uuid5(NAMESPACE_TRACE, thread_id).hex


def trace_id_for_run(org_id: uuid.UUID | str, run_id: uuid.UUID | str) -> str:
    """Return the deterministic trace id for a run given its org + run ids.

    ``create_run`` builds ``langgraph_thread_id = f"{org_id}:{run_id}"``, so
    this mirrors the API derivation exactly. *org_id* / *run_id* are coerced
    to ``uuid.UUID`` so a ``str`` of the canonical form produces the identical
    thread string (lower-case dashed) as the uuid objects ``create_run`` uses.
    """
    org = org_id if isinstance(org_id, uuid.UUID) else uuid.UUID(str(org_id))
    rid = run_id if isinstance(run_id, uuid.UUID) else uuid.UUID(str(run_id))
    return trace_id_for_thread(f"{org}:{rid}")


def trace_id_int_for_thread(thread_id: str) -> int:
    """Return the trace id as the integer used by OTel ``SpanContext``."""
    return uuid.uuid5(NAMESPACE_TRACE, thread_id).int
