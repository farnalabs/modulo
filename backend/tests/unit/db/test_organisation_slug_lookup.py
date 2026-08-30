"""Behavioural unit test for the partial-unique org slug lookup fix.

``get_organisation_by_slug`` must ignore soft-deleted rows (the
``organisations.slug`` partial UNIQUE only covers ``deleted_at IS NULL``) and
return at most one row. Before this fix the lookup matched *any* slug row and
used ``scalar_one_or_none()`` — so a create would 409 against a slug that is
free to reuse, and a duplicate slug materialising would raise
``MultipleResultsFound`` -> 500.

This test captures the SELECT the function emits and asserts it filters
``deleted_at IS NULL`` and bounds the result with ``LIMIT 1``. It runs without
a database.
"""

from unittest.mock import MagicMock

from modulo.db.crud.organisation import get_organisation_by_slug


def _capture_statement() -> object:
    """Return a fake AsyncSession that records the executed statement."""
    captured: dict[str, object] = {}

    async def _execute(stmt: object) -> MagicMock:
        captured["stmt"] = stmt
        result = MagicMock()
        result.scalars.return_value.first.return_value = None
        return result

    session = MagicMock()
    session.execute = _execute
    # Attach the captured dict so the caller can inspect what was executed.
    session._captured = captured  # type: ignore[attr-defined]
    return session


async def test_get_organisation_by_slug_filters_soft_deleted() -> None:
    session = _capture_statement()
    await get_organisation_by_slug(session, "acme")  # type: ignore[arg-type]

    stmt = session._captured["stmt"]  # type: ignore[attr-defined]
    compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))

    assert "deleted_at IS NULL" in compiled, "lookup must ignore soft-deleted orgs"
    assert "LIMIT" in compiled.upper(), "lookup must bound the result (no scalar_one_or_none)"


async def test_get_organisation_by_slug_matches_slug_only() -> None:
    session = _capture_statement()
    await get_organisation_by_slug(session, "acme")  # type: ignore[arg-type]

    stmt = session._captured["stmt"]  # type: ignore[attr-defined]
    compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))

    assert "slug" in compiled
    assert "acme" in compiled
