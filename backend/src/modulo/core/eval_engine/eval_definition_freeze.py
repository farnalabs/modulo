"""Eval-definition creation/editing freeze guard (FAR-1100 chunk 3 → 3b).

Between chunk 3's read cutover (PR-1) and chunk 3b's write cutover (PR-2 of
the chunk-3 delivery), creation and editing of eval definitions is disabled.
Any attempt returns a typed 409 Conflict that names chunk 3b as the
remediation.

This module is the single place to remove when chunk 3b lands (CO-8).

Usage from REST handlers::

    from modulo.core.eval_engine.eval_definition_freeze import raise_if_frozen
    raise_if_frozen()  # raises HTTPException(409)

Usage from MCP ``_impl`` functions::

    from modulo.core.eval_engine.eval_definition_freeze import definition_frozen_response
    if (err := definition_frozen_response()) is not None:
        return err
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, status

# --- Error-code constant (follows the _CODE_EVALS_* convention) ---------------

CODE_EVALS_DEFINITION_FROZEN: str = "evals.definition_frozen"

# --- Freeze master switch ----------------------------------------------------
# While ``True`` every create/edit entry point returns the typed freeze error.
# Flip to ``False`` -- or delete this module and its call sites -- when chunk 3b
# lands (CO-8) and writes are redirected to the new tables.  Tests that need to
# exercise the still-live validation/DB code paths below the guard patch the
# guard functions themselves rather than this constant.
EVAL_DEFINITION_WRITE_FROZEN: bool = True

# --- User-facing message (names chunk 3b as the remediation) -----------------

_DETAIL: str = (
    "Eval-definition creation and editing are frozen during the "
    "eval-gate taxonomy migration. This window will be removed when "
    "chunk 3b lands and redirects writes to the new tables. "
    "See FAR-1100."
)

# --- MCP error shape ---------------------------------------------------------

_MCP_ERROR: dict[str, Any] = {
    "error": "definition_frozen",
    "detail": _DETAIL,
}


def raise_if_frozen() -> None:
    """Raise ``HTTPException(409)`` while creation/editing is frozen.

    Call at the very top of REST create/edit handlers — before any validation
    or DB work.  Returns without raising once the freeze is lifted
    (``EVAL_DEFINITION_WRITE_FROZEN`` is ``False``).
    """
    if not EVAL_DEFINITION_WRITE_FROZEN:
        return
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=_DETAIL,
    )


def definition_frozen_response() -> dict[str, Any] | None:
    """Return the typed MCP error dict while creation/editing is frozen, else ``None``.

    Call at the very top of MCP ``_create_eval_definition_impl`` /
    ``_update_eval_definition_impl`` — before any validation or DB work.
    Returns ``None`` once the freeze is lifted (``EVAL_DEFINITION_WRITE_FROZEN``
    is ``False``), which matches the MCP ``_impl`` convention where a ``None``
    error means "continue".
    """
    if not EVAL_DEFINITION_WRITE_FROZEN:
        return None
    return dict(_MCP_ERROR)
