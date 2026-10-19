"""ADR 063 step 1 (FAR-1957): ``resolve_live_role_and_kind`` - the human-linkage DB seam.

``auth.human_linkage.resolve_human_linkage`` needs the account's ``kind``
alongside the live role. ``resolve_role_from_membership`` projects only the
role, so ``resolve_live_role_and_kind`` is a sibling query that DELIBERATELY
mirrors its predicates. These tests pin both the return shape and the
predicates, so the two queries cannot silently drift apart.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock

from modulo.db.crud.org_membership import resolve_live_role_and_kind


def _session_returning(row: tuple[str, str] | None) -> AsyncMock:
    session = AsyncMock()
    result = MagicMock()
    result.one_or_none.return_value = row
    session.execute = AsyncMock(return_value=result)
    return session


async def test_returns_live_role_and_kind_when_a_row_resolves() -> None:
    session = _session_returning(("operator", "human"))

    outcome = await resolve_live_role_and_kind(session, str(uuid.uuid4()), str(uuid.uuid4()))

    assert outcome == ("operator", "human")


async def test_returns_none_when_no_live_membership() -> None:
    """No row = inactive account / deactivated or absent membership ⇒ deny."""
    session = _session_returning(None)

    outcome = await resolve_live_role_and_kind(session, str(uuid.uuid4()), str(uuid.uuid4()))

    assert outcome is None


async def test_query_applies_the_live_role_clamp_predicates() -> None:
    """Mirrors ``resolve_role_from_membership``: active account, live membership,
    break-glass deny — the predicates the human-linkage decision rests on."""
    session = _session_returning(None)
    account_id = str(uuid.uuid4())
    org_id = str(uuid.uuid4())

    await resolve_live_role_and_kind(session, account_id, org_id)

    statement = str(session.execute.await_args.args[0])
    assert "accounts.active IS true" in statement
    assert "org_memberships.deactivated_at IS NULL" in statement
    assert "accounts.is_break_glass IS true" in statement
    assert "org_memberships.account_id = " in statement
    assert "org_memberships.organisation_id = " in statement
