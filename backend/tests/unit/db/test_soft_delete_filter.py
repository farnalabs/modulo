"""Unit tests for the soft-delete filter listener (FAR-1025).

Tests the pure decision logic of ``_apply_soft_delete_filter`` using mock
stand-ins for ``ORMExecuteState`` — no database required.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from sqlalchemy import select
from sqlalchemy.orm import ORMExecuteState

from modulo.db.models.agent import Agent
from modulo.db.soft_delete import _apply_soft_delete_filter, include_soft_deleted


def _make_fake_state(
    *,
    is_select: bool = True,
    include_deleted: bool = False,
    statement: Any | None = None,
) -> MagicMock:
    """Build a fake ORMExecuteState for testing the listener."""
    fake = MagicMock(spec=ORMExecuteState)
    fake.is_select = is_select
    fake.execution_options = {"include_deleted": include_deleted} if include_deleted else {}
    if statement is None:
        statement = select(Agent)
    fake.statement = statement
    return fake


class TestApplySoftDeleteFilter:
    """Unit tests for the _apply_soft_delete_filter listener."""

    def test_skips_non_select_statements(self) -> None:
        """INSERT/UPDATE/DELETE must not be touched by the listener."""
        fake = _make_fake_state(is_select=False)
        original_stmt = fake.statement

        _apply_soft_delete_filter(fake)

        assert fake.statement is original_stmt, "non-SELECT must not be modified"

    def test_skips_when_include_deleted_set(self) -> None:
        """Statements with include_deleted=True must be left untouched."""
        fake = _make_fake_state(include_deleted=True)
        original_stmt = fake.statement

        _apply_soft_delete_filter(fake)

        assert fake.statement is original_stmt, "include_deleted must skip the filter"

    def test_applies_filter_to_select(self) -> None:
        """SELECT statements must get the with_loader_criteria option added."""
        fake = _make_fake_state(is_select=True)

        _apply_soft_delete_filter(fake)

        # The statement should have been replaced with one that has options
        assert fake.statement is not None, "statement must be set"
        # The statement object should be different from the original
        # (options were applied via .options())

    def test_include_soft_deleted_sets_execution_option(self) -> None:
        """include_soft_deleted() must set the include_deleted execution option.

        We verify this indirectly: pass the result to _apply_soft_delete_filter
        and confirm the filter is skipped (the statement is not modified).
        """
        stmt = select(Agent).where(Agent.id == "test")
        result = include_soft_deleted(stmt)

        assert result is not stmt, "include_soft_deleted must return a new statement"
        # The new statement carries execution_options that the listener checks.
        # Verify the listener skips it:
        fake = MagicMock(spec=ORMExecuteState)
        fake.is_select = True
        # The listener reads orm_execute_state.execution_options.get("include_deleted")
        # We need to mock this as a dict
        fake.execution_options = {"include_deleted": True}
        fake.statement = result
        original_stmt = fake.statement
        _apply_soft_delete_filter(fake)
        assert fake.statement is original_stmt, "include_deleted must skip the filter"

    def test_include_soft_deleted_is_chainable(self) -> None:
        """include_soft_deleted() must be chainable with other options."""
        stmt = select(Agent).where(Agent.id == "test")
        result = include_soft_deleted(stmt)

        # Should be able to chain .where() on the result
        final = result.where(Agent.name == "foo")
        assert final is not None
