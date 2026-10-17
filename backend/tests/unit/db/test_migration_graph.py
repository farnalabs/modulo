"""Tests for the shared migration-graph helper (``_migration_graph``).

FAR-1929 replaced hard-coded chain-head constants across ~30 migration test
modules with a single delegation: ``assert_single_linear_head(script_directory())``.
Those call sites exercise only the *pass* path, so a regression in one of the
helper's failure branches would leave the whole suite green while the assertion
silently stopped protecting anything. This module drives every branch against a
*synthetic* migration tree built in ``tmp_path`` (never the real migrations), and
asserts each failure names the offender.

File-set note relevant to the orphan/ghost cases: the helper's static scan
recurses (``rglob``) and skips ``_``-prefixed files, while Alembic reads only the
top level of ``versions/`` and reads every ``.py`` there. Those two cases below
provoke that divergence in each direction.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic.script import ScriptDirectory

from tests.unit.db._migration_graph import assert_single_linear_head, script_directory


def _migration(revision: str, down_revision: str | None = None, *, parents: tuple[str, ...] | None = None) -> str:
    """Return migration source declaring ``revision`` and its parent(s).

    Pass ``parents`` for a merge migration (tuple ``down_revision``); otherwise
    ``down_revision`` is a single id or ``None``.
    """
    if parents is not None:
        down = "(" + ", ".join(f'"{parent}"' for parent in parents) + ",)"
    elif down_revision is None:
        down = "None"
    else:
        down = f'"{down_revision}"'
    return f'revision = "{revision}"\ndown_revision = {down}\n'


def _build_tree(tmp_path: Path, files: dict[str, str]) -> tuple[Path, Path]:
    """Write ``{relative path: source}`` under a synthetic ``migrations/versions``.

    Returns ``(migrations_dir, versions_dir)``: Alembic is built from the former,
    the helper's static scan is pointed at the latter.
    """
    migrations_dir = tmp_path / "migrations"
    versions_dir = migrations_dir / "versions"
    versions_dir.mkdir(parents=True)
    for relative, source in files.items():
        path = versions_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
    return migrations_dir, versions_dir


def _script(migrations_dir: Path) -> ScriptDirectory:
    return ScriptDirectory(str(migrations_dir))


def test_real_graph_passes() -> None:
    """The repo's own migration graph satisfies the helper it delegates to.

    Deliberately does NOT assert the head's value: pinning the literal is the
    very thing FAR-1929 removes (a new migration would fail this test).
    """
    head = assert_single_linear_head(script_directory())
    assert isinstance(head, str)
    assert head


def test_valid_chain_passes(tmp_path: Path) -> None:
    """A three-migration linear chain returns its single head.

    Included so the failure cases below are not passing for the wrong reason
    (i.e. the helper raising on any synthetic tree at all).
    """
    migrations_dir, versions_dir = _build_tree(
        tmp_path,
        {
            "a.py": _migration("a"),
            "b.py": _migration("b", "a"),
            "c.py": _migration("c", "b"),
        },
    )
    assert assert_single_linear_head(_script(migrations_dir), versions_dir=versions_dir) == "c"


def test_duplicate_revision_id_fails(tmp_path: Path) -> None:
    """Two files declaring the same revision id is an error naming both files."""
    migrations_dir, versions_dir = _build_tree(
        tmp_path,
        {
            "a.py": _migration("a"),
            "b.py": _migration("a", "a"),
        },
    )
    with pytest.raises(AssertionError, match="duplicate revision id") as excinfo:
        assert_single_linear_head(_script(migrations_dir), versions_dir=versions_dir)
    message = str(excinfo.value)
    assert "duplicate revision id 'a'" in message
    assert "a.py" in message
    assert "b.py" in message


def test_dangling_down_revision_fails(tmp_path: Path) -> None:
    """A down_revision pointing at a revision no file declares is an error."""
    migrations_dir, versions_dir = _build_tree(
        tmp_path,
        {
            "a.py": _migration("a"),
            "b.py": _migration("b", "missing"),
        },
    )
    with pytest.raises(AssertionError, match="which no migration file declares") as excinfo:
        assert_single_linear_head(_script(migrations_dir), versions_dir=versions_dir)
    message = str(excinfo.value)
    assert "b.py" in message
    assert "revision 'b'" in message
    assert "'missing'" in message


def test_two_heads_fails(tmp_path: Path) -> None:
    """A second root (a migration nothing chains onto) is two heads."""
    migrations_dir, versions_dir = _build_tree(
        tmp_path,
        {
            "a.py": _migration("a"),
            "b.py": _migration("b"),
        },
    )
    with pytest.raises(AssertionError, match="expected exactly one migration head") as excinfo:
        assert_single_linear_head(_script(migrations_dir), versions_dir=versions_dir)
    message = str(excinfo.value)
    assert "expected exactly one migration head" in message
    assert "'a'" in message
    assert "'b'" in message


def test_diamond_branch_point_and_merge_point_fails(tmp_path: Path) -> None:
    """A diamond yields one head but is NOT linear, so the helper must reject it.

    ``a`` branches into ``b`` and ``c`` (a branch point), reconciled by ``d``
    with a tuple ``down_revision`` (a merge point). ``len(get_heads()) == 1``
    alone would pass this, which is why the linearity assertion exists.
    """
    migrations_dir, versions_dir = _build_tree(
        tmp_path,
        {
            "a.py": _migration("a"),
            "b.py": _migration("b", "a"),
            "c.py": _migration("c", "a"),
            "d.py": _migration("d", parents=("b", "c")),
        },
    )
    with pytest.raises(AssertionError, match="not linear") as excinfo:
        assert_single_linear_head(_script(migrations_dir), versions_dir=versions_dir)
    message = str(excinfo.value)
    assert "not linear" in message
    assert "'a'" in message
    assert "'d'" in message


def test_orphan_migration_in_nested_directory_fails(tmp_path: Path) -> None:
    """A file the scan reads but Alembic does not walk is an orphan.

    Provoked by nesting ``c.py`` one level below ``versions/``: the helper's
    scan recurses (``rglob``) and sees it, but Alembic lists only the top level
    of ``versions/``, so ``c`` is declared to the scan yet absent from the graph.
    """
    migrations_dir, versions_dir = _build_tree(
        tmp_path,
        {
            "a.py": _migration("a"),
            "b.py": _migration("b", "a"),
            "nested/c.py": _migration("c", "b"),
        },
    )
    with pytest.raises(AssertionError, match="absent from the graph") as excinfo:
        assert_single_linear_head(_script(migrations_dir), versions_dir=versions_dir)
    message = str(excinfo.value)
    assert "absent from the graph" in message
    assert "'c'" in message


def test_ghost_migration_underscore_prefixed_fails(tmp_path: Path) -> None:
    """A graph revision with no backing file the scan reads is a ghost.

    Provoked by ``_c.py``: Alembic reads every top-level ``.py`` (so it walks
    ``c``), but the helper's scan skips ``_``-prefixed files (so ``c`` is not
    declared to the scan) — the divergence in the other direction from the
    nested-directory case.
    """
    migrations_dir, versions_dir = _build_tree(
        tmp_path,
        {
            "a.py": _migration("a"),
            "_c.py": _migration("c", "a"),
        },
    )
    with pytest.raises(AssertionError, match="no backing file on disk") as excinfo:
        assert_single_linear_head(_script(migrations_dir), versions_dir=versions_dir)
    message = str(excinfo.value)
    assert "no backing file on disk" in message
    assert "'c'" in message


def test_non_string_literal_revision_fails(tmp_path: Path) -> None:
    """A ``revision`` that is not a string literal is an error naming the file.

    Guards the ``_literal_str`` branch: an integer (or any other non-string
    constant) must be rejected rather than silently coerced into a revision id.
    """
    migrations_dir, versions_dir = _build_tree(
        tmp_path,
        {"bad.py": "revision = 123\ndown_revision = None\n"},
    )
    with pytest.raises(AssertionError, match="is not a string literal") as excinfo:
        assert_single_linear_head(_script(migrations_dir), versions_dir=versions_dir)
    message = str(excinfo.value)
    assert "is not a string literal" in message
    assert "bad.py" in message


def test_unsupported_down_revision_form_fails(tmp_path: Path) -> None:
    """A ``down_revision`` that is neither ``None``, a string literal nor a tuple
    of string literals (here a name reference) is an error naming the file.

    Guards the ``_parse_down_revision`` fall-through branch.
    """
    migrations_dir, versions_dir = _build_tree(
        tmp_path,
        {"bad.py": 'revision = "bad"\ndown_revision = SOME_PARENT\n'},
    )
    with pytest.raises(AssertionError, match="unsupported form") as excinfo:
        assert_single_linear_head(_script(migrations_dir), versions_dir=versions_dir)
    message = str(excinfo.value)
    assert "unsupported form" in message
    assert "bad.py" in message


def test_file_declaring_no_revision_fails(tmp_path: Path) -> None:
    """A scanned file that declares no ``revision`` id is an error naming it.

    Guards the ``_read_revision`` "declares no revision id" branch.
    """
    migrations_dir, versions_dir = _build_tree(
        tmp_path,
        {"bad.py": "down_revision = None\n"},
    )
    with pytest.raises(AssertionError, match="declares no revision id") as excinfo:
        assert_single_linear_head(_script(migrations_dir), versions_dir=versions_dir)
    message = str(excinfo.value)
    assert "declares no revision id" in message
    assert "bad.py" in message
