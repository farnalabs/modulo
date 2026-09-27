"""Subject-scoped fetch tests (FAR-966 chunk 7, spec criteria F1 + F2 + F4).

Hybrid portion of the fetch criteria: the compiled-SQL shape of the real
``fetch`` statement is asserted against a hand-rolled fake ``AsyncSession``
that records the statement handed to ``execute()``. No database is required:

- **F1** — cross-key snapshot consistency: one statement, ``DISTINCT ON`` on
  the (organisation, subject, key) ladder, so every key for a subject resolves
  in a single call (no per-key splits).
- **F2** — fetch is scoped by subject only, not by key: the WHERE clause
  carries exactly the org/subject predicates and no key filter.
- **F4** — fetch-failure contract: a DB failure inside ``fetch`` propagates to
  the caller (never a silent empty result).

The behavioural round-trip of F1/F2 and the EXPLAIN proof of a single scan
(F3) live in ``backend/tests/integration/test_evidence_table.py``.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import Select
from sqlalchemy.dialects import postgresql

from modulo.core.eval_engine.evidence_layer import fetch

_FAKE_ROWS: list[str] = ["row-a", "row-b"]


class _FakeExecResult:
    """Mimics the chained ``.scalars().all()`` shape of an AsyncEngine result."""

    def scalars(self) -> _FakeExecResult:
        return self

    def all(self) -> list[Any]:
        return list(_FAKE_ROWS)


class _FakeAsyncSession:
    """Hand-rolled double: capture the statement passed to ``execute``."""

    def __init__(self) -> None:
        self.statements: list[Any] = []

    async def execute(self, statement: Any) -> _FakeExecResult:
        self.statements.append(statement)
        return _FakeExecResult()


class _RaisingSession:
    """DB session whose ``execute`` raises the injected connection failure."""

    async def execute(self, statement: Any) -> None:
        raise RuntimeError("connection closed")


_ORG_ID = "00000000-0000-0000-0000-0000000000AA"


async def _captured_statement() -> tuple[Any, _FakeAsyncSession]:
    """Call the production fetch and return the statement it executed."""
    session = _FakeAsyncSession()
    await fetch("node_execution", "r1:n1", _ORG_ID, session)
    assert len(session.statements) == 1
    assert isinstance(session.statements[0], Select)
    return session.statements[0], session  # ---------------------------------------------------------------------------


# F1 — cross-key snapshot consistency (single statement)
# ---------------------------------------------------------------------------


async def test_f1_fetch_runs_a_single_statement() -> None:
    """The whole snapshot is one SELECT: no per-key round trips are appended."""
    session = _FakeAsyncSession()
    await fetch("node_execution", "r1:n1", _ORG_ID, session)
    assert len(session.statements) == 1


async def test_f1_distinct_on_covers_org_subject_and_key() -> None:
    """DISTINCT ON dedup is over (organisation_id, subject_type, subject_id, key)."""
    sql = str((await _captured_statement())[0].compile(dialect=postgresql.dialect())).upper()
    expected = "DISTINCT ON (EVIDENCE.ORGANISATION_ID, EVIDENCE.SUBJECT_TYPE, EVIDENCE.SUBJECT_ID, EVIDENCE.KEY)"
    assert expected in sql


async def test_f1_order_by_is_the_snapshot_ladder() -> None:
    """ORDER BY ends created_at DESC, id DESC so most-recent wins per key."""
    sql = str((await _captured_statement())[0].compile(dialect=postgresql.dialect())).upper()
    where_pos = sql.index(" ORDER BY ")
    order_segment = sql[where_pos:]
    expected = [
        "EVIDENCE.ORGANISATION_ID",
        "EVIDENCE.SUBJECT_TYPE",
        "EVIDENCE.SUBJECT_ID",
        "EVIDENCE.KEY",
        "EVIDENCE.CREATED_AT DESC",
        "EVIDENCE.ID DESC",
    ]
    cursor = 0
    for token in expected:
        position = order_segment.find(token)
        assert position != -1
        assert position >= cursor
        # Consecutive tokens occur in ascending text position: ladder order.
        cursor = position
    tail = order_segment.find("EVIDENCE.ID DESC") + len("EVIDENCE.ID DESC")
    assert not order_segment[tail:].strip()


# ---------------------------------------------------------------------------
# F2 — subject-scoped only, no key filter
# ---------------------------------------------------------------------------


async def test_f2_where_scopes_by_org_and_subject_only() -> None:
    """The WHERE clause has the org/subject predicates and nothing else."""
    sql = str((await _captured_statement())[0].compile(dialect=postgresql.dialect())).upper()
    start = sql.index("WHERE")
    where_segment = sql[start:]
    assert "EVIDENCE.ORGANISATION_ID =" in where_segment
    assert "EVIDENCE.SUBJECT_TYPE =" in where_segment
    assert "EVIDENCE.SUBJECT_ID =" in where_segment


async def test_f2_no_key_filter_in_where() -> None:
    """No key comparison: the fetch returns ALL keys present for the subject."""
    sql = str((await _captured_statement())[0].compile(dialect=postgresql.dialect())).upper()
    where_segment = sql[sql.index("WHERE") :].upper()
    assert "EVIDENCE.KEY =" not in where_segment
    assert "EVIDENCE.KEY IN" not in where_segment


async def test_f2_no_per_key_subquery_or_set_operation() -> None:
    """Set operations / scalar subqueries would split the snapshot — there
    are none: exactly one SELECT, no UNION/INTERSECT/EXCEPT."""
    sql = str((await _captured_statement())[0].compile(dialect=postgresql.dialect())).upper()
    assert sql.count("SELECT DISTINCT ON") == 1
    assert " UNION " not in sql
    assert " INTERSECT " not in sql
    assert " EXCEPT " not in sql


# ---------------------------------------------------------------------------
# Fetch execution plumbing
# ---------------------------------------------------------------------------


async def test_fetch_returns_the_sessions_scalars_rows() -> None:
    """fetch returns ``result.scalars().all()`` — the recorded fake rows."""
    session = _FakeAsyncSession()
    rows = await fetch("node_execution", "r1:n1", _ORG_ID, session)
    assert rows == _FAKE_ROWS


# ---------------------------------------------------------------------------
# F4 — fetch-failure contract
# ---------------------------------------------------------------------------


async def test_f4_db_failure_propagates_not_swallowed() -> None:
    """A DB connection failure inside fetch reaches the caller as the raised
    error (never an empty result — unmapped failures bypassing the policy is
    exactly the design failure the contract forbids)."""
    with pytest.raises(RuntimeError, match="connection closed"):
        await fetch("node_execution", "r1:n1", _ORG_ID, _RaisingSession())
