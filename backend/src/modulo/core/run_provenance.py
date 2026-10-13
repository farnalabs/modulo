"""Run-level execution provenance — the ONE serializer for every run surface.

FAR-1565 / ADR-042: a claim-ready surface for a dispatched run must be
provenance-typed, so a dispatched run never reads indistinguishably from an
executed one. Before this module existed each run-rendering payload builder
(``api/routes/runs``, ``api/mcp_server``, ``dashboard``, ``slack``,
``lifecycle_maps``, the run-keyed audit payloads in ``core/pipeline_engine``)
hand-wrote ``"execution_origin": ...`` with its own ad-hoc coercion — 12 copies
of the same field, each one a place where a 13th surface could forget the field
or re-introduce its own variant.

Every surface now composes :func:`run_provenance_fields` (``**run_provenance_fields(run)``),
so the field is defined exactly once. This module is a core leaf (no imports
beyond the stdlib): ``core`` may not import ``api``, while ``api`` may import
``core``, so one module here serves both layers.

FAR-1693 unified the provenance vocabulary (``core.provenance``): the run's
``execution_origin`` is the *where* and the sibling ``provenance`` class is the
*how we know* — ``reported`` for a dispatched run (its externally-sourced data
arrived from the customer's substrate), ``None`` for an executed / legacy run
(there is no externally-sourced datum to type). Both keys are always present.

Wire shape is additive/nullable: ``{"execution_origin": str | None,
"provenance": str | None}``.

Enforcement (``tests/unit/api/test_run_provenance_serializer.py``):

* no surface in ``api/**`` or ``core/pipeline_engine/**`` may hand-write the
  ``execution_origin`` key/keyword — the value must derive from this serializer;
* every run-shaped payload builder in those trees must compose it, so a new
  surface cannot forget the field.
"""

from __future__ import annotations

from typing import Any

from modulo.core.provenance import execution_origin_provenance_class

__all__ = ["run_provenance_fields"]


def run_provenance_fields(run: Any) -> dict[str, Any]:
    """Return the provenance fields every claim-ready run payload must carry.

    ``run`` is a ``Run`` row (or anything exposing the same attribute, e.g. a
    projected row object). It may also be ``None`` — a best-effort origin read
    that failed still yields the key with a NULL value rather than omitting it,
    because the wire contract is "always present, ``str | None``".

    A row that never carried the column (loaded before migration 0288, or
    projected without it) reads NULL via the ``getattr`` default; provenance
    must never break a read.

    ``provenance`` is ENGINE-DERIVED from ``execution_origin`` via the unified
    taxonomy (FAR-1693) — never a value a caller or an agent supplies — so no
    run payload can self-assert a stronger class than the origin supports.

    Deliberately NO ``isinstance(value, str)`` coercion (FAR-1566): the column
    is genuinely ``str | None`` in production, and degrading a non-string
    stand-in to ``None`` was test-double tolerance, not a production rule — a
    non-string value now fails loudly at the response model instead of being
    silently rewritten.
    """
    origin = getattr(run, "execution_origin", None)
    return {
        "execution_origin": origin,
        "provenance": execution_origin_provenance_class(origin),
    }
