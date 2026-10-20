"""Focused branch coverage for the S3776 decomposition of ``migrate_org``.

The import helpers were split out of ``_do_import``/``_import_row``; these
cases exercise the branches that the existing suite never reached: rows with
no ``id`` (so no id-map entry is written), columns absent from the existing
row, and a table with no name-conflict field.
"""

import uuid
from typing import Any, Self
from unittest.mock import AsyncMock, MagicMock, patch

from modulo.cli.migrate_org import _do_import

ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


class _Existing:
    """Minimal stand-in for a pre-existing ORM row."""

    def __init__(self, **kwargs: object) -> None:
        for key, value in kwargs.items():
            setattr(self, key, value)


class _ScalarResult:
    def __init__(self, first: object | None = None) -> None:
        self._first = first

    def scalars(self) -> "_ScalarResult":
        return self

    def first(self) -> object | None:
        return self._first


class _NestedTx:
    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


class _FakeSession:
    def __init__(self, results: list[_ScalarResult] | None = None) -> None:
        self._queue = list(results or [])
        self.added: list[Any] = []
        self.flushed = 0

    def begin_nested(self) -> _NestedTx:
        return _NestedTx()

    async def execute(self, stmt: object) -> _ScalarResult:
        if not self._queue:
            return _ScalarResult()
        return self._queue.pop(0)

    def add(self, obj: object) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        self.flushed += 1

    async def commit(self) -> None:
        return None


def _patched_session(results: list[_ScalarResult] | None = None) -> tuple[_FakeSession, MagicMock]:
    session = _FakeSession(results)
    mock_local = MagicMock()
    mock_local.__aenter__ = AsyncMock(return_value=session)
    mock_local.__aexit__ = AsyncMock(return_value=False)
    return session, mock_local


async def _run_import(
    bundle: dict[str, Any],
    strategy: str,
    results: list[_ScalarResult] | None = None,
) -> tuple[dict[str, int], _FakeSession]:
    session, mock_local = _patched_session(results)
    with patch("modulo.cli.migrate_org.AsyncSessionLocal", return_value=mock_local):
        counts = await _do_import(bundle, ORG_ID, strategy)
    return counts, session


async def test_create_row_without_id_skips_id_map() -> None:
    counts, session = await _run_import({"teams": [{"name": "Platform"}]}, "skip")

    assert counts["created"] == 1
    assert len(session.added) == 1


async def test_skip_row_without_id_still_counts_the_skip() -> None:
    existing = _Existing(id=uuid.uuid4(), name="Platform")
    counts, _ = await _run_import({"teams": [{"name": "Platform"}]}, "skip", [_ScalarResult(first=existing)])

    assert counts["skipped"] == 1


async def test_overwrite_row_without_id_updates_in_place() -> None:
    existing: Any = _Existing(id=uuid.uuid4(), name="Platform", description="old")
    counts, session = await _run_import(
        {"teams": [{"name": "Platform", "description": "new"}]},
        "overwrite",
        [_ScalarResult(first=existing)],
    )

    assert counts["overwritten"] == 1
    assert not session.added
    assert existing.description == "new"


async def test_overwrite_ignores_columns_the_row_does_not_have() -> None:
    existing: Any = _Existing(id=uuid.uuid4(), name="Platform")
    counts, _ = await _run_import(
        {"teams": [{"id": "t1", "name": "Platform", "not_a_real_column": "x"}]},
        "overwrite",
        [_ScalarResult(first=existing)],
    )

    assert counts["overwritten"] == 1
    assert not hasattr(existing, "not_a_real_column")


async def test_table_without_name_field_skips_conflict_lookup() -> None:
    counts, session = await _run_import({"runs": [{"id": "r1"}]}, "skip")

    assert counts["created"] == 1
    assert len(session.added) == 1
