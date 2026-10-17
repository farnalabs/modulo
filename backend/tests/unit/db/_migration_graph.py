"""Shared helpers for asserting the shape of the Alembic migration graph.

Per-migration test modules used to hard-code the current chain head as a module
constant and assert ``ScriptDirectory(...).get_heads() == [HEAD]``. That pinned
the *value* of the head, so every PR that added a migration had to edit ~28
unrelated test modules (FAR-1929). These helpers assert the *shape* of the graph
instead -- the properties a head pin was standing in for:

* exactly one head;
* strictly linear -- no branch point (a revision with two children) and no merge
  point (a revision reconciled by two parents / tuple ``down_revision``);
* no dangling ``down_revision`` -- every parent a migration names exists;
* no duplicate ``revision`` id across files;
* no orphan or ghost -- every ``versions`` file the scan reads is represented in
  the graph Alembic walks, and every graph revision has a backing file.

Alembic still owns the graph (``ScriptDirectory.get_heads`` /
``walk_revisions``). The static scan supplies per-file identity and lets the
dangling-link check name the offender, because Alembic's graph APIs raise a bare
``KeyError`` on a dangling link rather than a message naming the child that
declared it.

The scan and the Alembic walk read a *different* file set on purpose: the scan
recurses (``rglob``) and skips ``_``-prefixed files, while Alembic lists the top
level of ``versions/`` only and reads every ``.py`` there. The orphan/ghost
check is what makes that divergence fail loudly instead of silently dropping a
migration.

File identity is each file's declared ``revision`` (not its filename stem): one
real migration -- ``0152_add_indexes.py`` declares
``revision = "0152_dismissed_by_user_id_index"`` -- has a stem that differs from
its revision id, so a filename comparison would false-positive here.
"""

from __future__ import annotations

import ast
from pathlib import Path

from alembic.script import ScriptDirectory

_VERSIONS_DIR = Path(__file__).resolve().parents[3] / "src" / "modulo" / "db" / "migrations" / "versions"
_MIGRATIONS_DIR = _VERSIONS_DIR.parent


def script_directory(*, migrations_dir: Path | None = None) -> ScriptDirectory:
    """Return the :class:`ScriptDirectory` for this repo's migration tree.

    ``migrations_dir`` overrides the repo's real migrations directory so tests
    can point the helper at a synthetic tree; production call sites pass
    ``script_directory()`` and get the real one.
    """
    target = _MIGRATIONS_DIR if migrations_dir is None else migrations_dir
    return ScriptDirectory(str(target))


def _literal_str(value: ast.expr, path: Path, field: str) -> str:
    raw = value.value if isinstance(value, ast.Constant) else None
    if not isinstance(raw, str):
        raise AssertionError(f"{path.name}: {field} is not a string literal")
    return raw


def _parse_down_revision(value: ast.expr, path: Path) -> str | tuple[str, ...] | None:
    if isinstance(value, ast.Constant) and value.value is None:
        return None
    if isinstance(value, ast.Constant):
        return _literal_str(value, path, "down_revision")
    if isinstance(value, ast.Tuple):
        return tuple(_literal_str(element, path, "down_revision") for element in value.elts)
    raise AssertionError(f"{path.name}: down_revision has an unsupported form")


def _read_revision(path: Path) -> tuple[str, str | tuple[str, ...] | None]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    revision: str | None = None
    down_revision: str | tuple[str, ...] | None = None
    for node in tree.body:
        target: ast.expr | None = None
        value: ast.expr | None = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            value = node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            target = node.target
            value = node.value
        if not isinstance(target, ast.Name) or value is None:
            continue
        if target.id == "revision":
            revision = _literal_str(value, path, "revision")
        elif target.id == "down_revision":
            down_revision = _parse_down_revision(value, path)
    if revision is None:
        raise AssertionError(f"{path.name}: declares no revision id")
    return revision, down_revision


def _parents(down_revision: str | tuple[str, ...] | None) -> tuple[str, ...]:
    if down_revision is None:
        return ()
    if isinstance(down_revision, str):
        return (down_revision,)
    return down_revision


def assert_single_linear_head(script: ScriptDirectory, *, versions_dir: Path | None = None) -> str:
    """Assert the migration graph is a single, linear, complete chain.

    Returns the (only) head revision id. Raises ``AssertionError`` naming the
    offending value when the graph has branched (two heads, or a branch/merge
    point), when a revision id is declared twice, when a file is not wired into
    the graph, or when a ``down_revision`` points at a revision no file declares.

    ``versions_dir`` overrides the directory the static scan reads; it defaults
    to the repo's real ``versions`` directory so every production call site is
    ``assert_single_linear_head(script_directory())``.
    """
    scan_dir = _VERSIONS_DIR if versions_dir is None else versions_dir
    declared: dict[str, tuple[Path, str | tuple[str, ...] | None]] = {}
    for path in sorted(scan_dir.rglob("*.py")):
        if path.name == "__init__.py" or path.name.startswith("_"):
            continue
        revision, down_revision = _read_revision(path)
        if revision in declared:
            raise AssertionError(
                f"duplicate revision id {revision!r} declared by both {declared[revision][0].name} and {path.name}"
            )
        declared[revision] = (path, down_revision)

    for revision, (path, down_revision) in declared.items():
        for parent in _parents(down_revision):
            if parent not in declared:
                raise AssertionError(
                    f"{path.name} (revision {revision!r}) declares down_revision {parent!r}, "
                    "which no migration file declares"
                )

    heads = script.get_heads()
    assert len(heads) == 1, f"expected exactly one migration head, got {heads!r}"

    revisions = list(script.walk_revisions())
    branchy = sorted(rev.revision for rev in revisions if rev.is_branch_point or rev.is_merge_point)
    assert not branchy, (
        f"migration graph is not linear: {branchy!r} are branch/merge points; "
        "the chain has diverged and been reconciled"
    )

    graph_revisions = {rev.revision for rev in revisions}
    orphans = sorted(set(declared) - graph_revisions)
    ghosts = sorted(graph_revisions - set(declared))
    assert not orphans, f"migration files present on disk but absent from the graph: {orphans!r}"
    assert not ghosts, f"graph revisions with no backing file on disk: {ghosts!r}"
    return heads[0]
