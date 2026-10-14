"""Focused unit tests for the shared SQLSTATE leaf module (FAR-1644).

``db.sqlstates`` holds ONE spelling of every SQLSTATE the codebase classifies
on. FAR-1644 hoisted the ``57014`` statement-timeout constant and its
predicate here out of ``core.cron_helpers`` (a private copy of both), exactly
as ``is_row_lock_timeout``/``LOCK_NOT_AVAILABLE_SQLSTATE`` were hoisted
earlier. These tests pin the constants, the two bounded-wait predicates and
the chain walk each depends on, so a re-copy anywhere has a canonical home to
fail against.
"""

from __future__ import annotations

from sqlalchemy.exc import OperationalError

from modulo.db.sqlstates import (
    DUAL_WRITE_RETRYABLE_SQLSTATES,
    LOCK_NOT_AVAILABLE_SQLSTATE,
    STATEMENT_TIMEOUT_SQLSTATE,
    is_row_lock_timeout,
    is_statement_timeout,
    sqlstate_of,
)


class _FakeDriverError(Exception):
    """Stand-in for a raw asyncpg driver error: carries its SQLSTATE as an
    attribute. A real ``Exception`` (not a bare object) because Python only
    accepts BaseException instances in a chain (``__context__``)."""

    def __init__(self, state: str) -> None:
        super().__init__(f"driver error {state}")
        self.sqlstate = state


def _driver_error(state: str) -> _FakeDriverError:
    return _FakeDriverError(state)


class TestStatementTimeoutPredicate:
    """``is_statement_timeout`` / ``STATEMENT_TIMEOUT_SQLSTATE`` (FAR-1644)."""

    def test_the_constant_is_the_single_spelling(self) -> None:
        assert STATEMENT_TIMEOUT_SQLSTATE == "57014"
        # The dual-write retry vocabulary names the SAME SQLSTATE — as a
        # reference to the constant, not a second literal that could drift
        # from it (membership is the drift detector: a re-copied literal
        # would not contain the canonical constant).
        assert STATEMENT_TIMEOUT_SQLSTATE in DUAL_WRITE_RETRYABLE_SQLSTATES

    def test_a_raw_driver_error_is_recognised(self) -> None:
        assert is_statement_timeout(_driver_error(STATEMENT_TIMEOUT_SQLSTATE))

    def test_a_wrapped_driver_error_is_recognised_through_the_chain(self) -> None:
        # The real shape on Postgres: a SQLAlchemy OperationalError wrapping
        # asyncpg's QueryCanceledError in ``.orig``.
        wrapped = OperationalError("UPDATE runs SET ...", {}, _driver_error(STATEMENT_TIMEOUT_SQLSTATE))
        assert sqlstate_of(wrapped) == STATEMENT_TIMEOUT_SQLSTATE
        assert is_statement_timeout(wrapped)

    def test_a_context_chained_driver_error_is_recognised(self) -> None:
        # A savepoint-rollback failure surfaces as a wrapper whose ORIGINAL
        # driver error rides on ``__context__`` — the pg-fidelity walk.
        wrapper: Exception = Exception("ROLLBACK TO SAVEPOINT failed")
        wrapper.__context__ = _driver_error(STATEMENT_TIMEOUT_SQLSTATE)
        assert is_statement_timeout(wrapper)

    def test_other_and_missing_states_are_not_statement_timeouts(self) -> None:
        assert not is_statement_timeout(_driver_error(LOCK_NOT_AVAILABLE_SQLSTATE))
        assert not is_statement_timeout(Exception("no SQLSTATE on this one"))


class TestRowLockTimeoutPredicate:
    """``is_row_lock_timeout`` — the sibling predicate, pinned alongside."""

    def test_the_constant_is_the_single_spelling(self) -> None:
        assert LOCK_NOT_AVAILABLE_SQLSTATE == "55P03"

    def test_wrapped_driver_error_is_recognised_and_statement_timeouts_are_not(self) -> None:
        wrapped = OperationalError("SELECT ... FOR UPDATE", {}, _driver_error(LOCK_NOT_AVAILABLE_SQLSTATE))
        assert is_row_lock_timeout(wrapped)
        assert not is_statement_timeout(wrapped)
        assert not is_row_lock_timeout(_driver_error(STATEMENT_TIMEOUT_SQLSTATE))
        assert not is_row_lock_timeout(Exception("no SQLSTATE on this one"))
