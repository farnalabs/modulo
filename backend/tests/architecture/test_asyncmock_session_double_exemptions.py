"""Regression pins for the bare-AsyncMock session-double guard's exemptions.

``configure_rls_preamble`` is a PARTIAL contract helper: it types the RLS
preamble's own members (``in_transaction``, ``get_bind``, ``add``) but leaves
``begin``, ``add_all``/``expunge`` and every ``execute`` result reader as bare
async children. Treating "the name appears in ``facts.configured``" as a
blanket exemption therefore let the exact leak shape the guard exists for slip
through::

    session = AsyncMock()
    configure_rls_preamble(session)
    result = await session.execute(...)
    result.scalar_one_or_none()      # never-awaited coroutine

These tests pin that the exemption is applied member-by-member, so a partial
helper cannot exempt a shape it does not make safe, while the full-contract
helper and explicitly typed members stay accepted.
"""

from __future__ import annotations

from tests.architecture.test_no_bare_asyncmock_session_fixtures import (
    _all_violations,
    _violations_in_source,
)

_RLS_PREAMBLE = """
from unittest.mock import AsyncMock
from tests.unit.api.mock_session import configure_rls_preamble

async def test_x():
    session = AsyncMock()
    configure_rls_preamble(session)
"""


def test_rls_preamble_does_not_exempt_a_bare_execute_result_read() -> None:
    source = (
        _RLS_PREAMBLE
        + """
    result = await session.execute(object())
    result.scalar_one_or_none()
"""
    )

    violations = _violations_in_source(source, "rls_execute.py")

    assert len(violations) == 1
    assert "execute() result read" in violations[0]


def test_rls_preamble_does_not_exempt_a_bare_begin() -> None:
    source = (
        _RLS_PREAMBLE
        + """
    async with session.begin():
        pass
"""
    )

    violations = _violations_in_source(source, "rls_begin.py")

    assert len(violations) == 1
    assert "begin() is not an async context manager" in violations[0]


def test_rls_preamble_does_not_exempt_unrelated_untyped_sync_members() -> None:
    source = (
        _RLS_PREAMBLE
        + """
    session.add_all([])
    session.expunge(object())
"""
    )

    violations = _violations_in_source(source, "rls_sync.py")

    assert len(violations) == 1
    assert "add_all" in violations[0]
    assert "expunge" in violations[0]


def test_full_contract_helper_exempts_the_members_it_types() -> None:
    source = """
from unittest.mock import AsyncMock
from tests.unit.api.mock_session import configure_mock_session

async def test_x():
    session = configure_mock_session(AsyncMock())
    async with session.begin():
        pass
    session.in_transaction()
    result = await session.execute(object())
    result.scalar_one_or_none()
"""

    assert not _violations_in_source(source, "full_contract.py")


def test_rls_preamble_with_an_explicit_execute_result_is_accepted() -> None:
    source = (
        _RLS_PREAMBLE
        + """
    session.execute = AsyncMock(return_value=object())
    result = await session.execute(object())
    result.scalar_one_or_none()
"""
    )

    assert not _violations_in_source(source, "rls_stubbed_execute.py")


def test_rls_preamble_with_typed_members_is_accepted() -> None:
    source = """
from unittest.mock import AsyncMock, MagicMock
from tests.unit.api.mock_session import configure_rls_preamble

def _make_begin():
    begin = MagicMock()
    return begin

async def test_x():
    session = AsyncMock()
    configure_rls_preamble(session)
    session.begin = MagicMock(return_value=_make_begin())
    session.add_all = MagicMock()
    session.expunge = MagicMock()
    async with session.begin():
        pass
    session.add_all([])
    session.expunge(object())
"""

    assert not _violations_in_source(source, "rls_typed.py")


def test_no_bare_asyncmock_violations_in_the_current_test_tree() -> None:
    """The tightened detector must not flag main's real fixtures."""
    assert not _all_violations()
