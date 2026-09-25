"""CRUD helpers for policy_gate_decisions — FAR-1102.

Shared purge helper used by both org-deletion paths (Path 3: org_deletion.py
confirm_org_deletion, and Path 6: organisation.py delete_organisation).
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.models.policy_gate_decision import PolicyGateDecision

_log = logging.getLogger(__name__)

# Constraint names whose presence on an IntegrityError signals a
# policy_gate_decisions RESTRICT violation (used by the housekeeping paths to
# classify FK violations as blocked-with-reason).
_DECISION_FK_CONSTRAINTS: frozenset[str] = frozenset(
    {
        "fk_policy_gate_decisions_gate_org",
        "fk_policy_gate_decisions_eval_org",
    }
)

# Substrings matched case-insensitively against the constraint name when the
# exact name is unavailable (e.g. some DB drivers).
_DECISION_FK_SUBSTRINGS: tuple[str, ...] = ("policy_gate_decision",)


def _extract_constraint_name(exc: object) -> str | None:
    """Extract the constraint name from a SQLAlchemy IntegrityError.

    Tries ``exc.constraint_name`` first (set by some DBAPI adapters).  Falls
    back to the underlying DBAPI exception (``exc.orig``) which carries the
    native constraint name for asyncpg and psycopg2.  As a last resort, parse
    the constraint name from the error message string (asyncpg populates the
    ``detail`` attribute with the constraint name in its standard error format).
    """
    name = getattr(exc, "constraint_name", None)
    if name:
        return name
    orig = getattr(exc, "orig", None)
    if orig is not None:
        name = getattr(orig, "constraint_name", None)
        if name:
            return name
        # asyncpg ForeignKeyViolationError message format:
        # '...violates foreign key constraint "constraint_name"...'
        msg = str(orig)
        marker = 'foreign key constraint "'
        idx = msg.find(marker)
        if idx != -1:
            start = idx + len(marker)
            end = msg.find('"', start)
            if end != -1:
                return msg[start:end]
    return None


def is_policy_gate_decision_fk_error(exc: Exception) -> bool:
    """Return True if *exc* is an IntegrityError referencing a decision FK.

    Used by the housekeeping paths (REST + MCP) to classify FK violations as
    blocked-with-reason instead of generic "Foreign key constraint violation".
    """
    from sqlalchemy.exc import IntegrityError

    if not isinstance(exc, IntegrityError):
        return False
    constraint = _extract_constraint_name(exc)
    if constraint is None:
        return False
    if constraint in _DECISION_FK_CONSTRAINTS:
        return True
    lower = constraint.lower()
    return any(sub in lower for sub in _DECISION_FK_SUBSTRINGS)


async def purge_org_decision_records(
    session: AsyncSession,
    org_id: uuid.UUID,
) -> int:
    """Delete ALL policy_gate_decisions rows for an org.

    Called as a child-most purge step BEFORE hard-deleting pipelines/evals/
    policy_gates (Paths 3 and 6).  The RESTRICT FKs on policy_gate_decisions
    require that decision rows be removed before their parents.

    Returns the number of rows deleted.
    """
    result = await session.execute(
        delete(PolicyGateDecision)
        .where(PolicyGateDecision.organisation_id == org_id)
        .execution_options(synchronize_session=False)
    )
    count = result.rowcount if hasattr(result, "rowcount") else 0
    if count:
        _log.info(
            "purge_org_decision_records: deleted %d decision rows for org %s",
            count,
            org_id,
        )
    return count
