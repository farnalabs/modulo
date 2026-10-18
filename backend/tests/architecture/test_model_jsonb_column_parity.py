"""Architecture gate: the 0147-converted JSON columns that still resolve are JSONB.

SCOPE — this gate covers **every** ``(table, column)`` pair a migration declares
as ``jsonb``, not merely the 66 ``0147_json_to_jsonb_standardize`` promoted. The
pair set is derived mechanically from the migrations (FAR-1941 extended it beyond
0147): each migration's own source is parsed for the jsonb declaration idioms —
``op.add_column`` / ``op.create_table`` with a JSONB-typed ``sa.Column``,
``op.alter_column`` with a JSONB ``type_``, the raw-SQL forms ``ALTER TABLE ...
ADD/ALTER COLUMN ... jsonb`` and ``CREATE TABLE ... (col jsonb ...)``, and
declarative module-level pair/column lists such as 0147's ``_JSON_TO_JSONB`` and
0129's ``_JSON_COLUMNS``. Every declared pair whose table and column still
resolve against ``Base.metadata`` must map to a JSONB dialect impl. Pairs whose
table or column no longer exists (a later migration dropped or renamed them)
carry no check — the migration that removed them owns that decision. There is no
hand-maintained pair inventory: adding a jsonb column to any migration
automatically extends the gate's scope.

The FAR-1878 residual was CLOSED by FAR-1941: the 16 columns that were declared
bare generic ``JSON`` over a physical ``jsonb`` column are aligned, and their
entries were removed from
``tests/integration/test_initial_migration.py::_JSONB_DB_TO_JSON_ORM``.

Why the 0147 columns still matter: a mapped column whose Postgres type renders
as plain ``json`` while the physical column is ``jsonb`` makes every SQL-level
predicate against it bind as ``$1::JSON`` and raise

    operator does not exist: jsonb = json

That is not hypothetical — FAR-1856 was exactly this failure on
``lifecycle_maps.content_json`` (declared bare ``JSON``, physically ``jsonb``),
and because the offending scanner tolerated per-scanner exceptions it returned
nothing in production forever while appearing healthy. FAR-1878 aligned the 0147
columns; this gate is the **regression guard for that set**, not the ticket's
closure.

The pair list is read from the migration module itself, so the gate cannot
silently drift from what 0147 actually converted — if the migration's contract
test pins a different set, this gate follows the migration, not a hand-copied
list.

Two structural invariants are enforced:

1. **Dialect parity** — every migration-declared jsonb pair that still resolves
   against ``Base.metadata`` maps to a column whose ``postgresql`` dialect impl
   is ``JSONB``. The 0147 sub-set additionally carries explicit rename/drop
   bookkeeping: three of its pairs point at tables a later migration renamed or
   dropped, each is named explicitly below, and ANY other absent 0147 table is a
   hard failure — there is no blanket skip, so a renamed 0147 table cannot hide
   its column. The two exception maps are cross-checked against reality, not
   merely declared: a pair recorded as RENAMED is checked at its destination
   (and the destination must be mapped), and a pair recorded as DROPPED must be
   shown, from its named migration's OWN source, to actually ``DROP TABLE`` it —
   so a rename recorded as a drop fails rather than passing the absence check.
2. **One shared definition** — the ``JSON().with_variant(JSONB(), "postgresql")``
   expression is constructed exactly once (``modulo.db.models.base.JSONB_COL``,
   plus its ``none_as_null=True`` sibling ``JSONB_COL_NONE_AS_NULL``).
   Enforced over the whole backend source tree (migrations excluded) by AST, so
   a direct ``mapped_column(JSONB())`` or a qualified
   ``with_variant(postgresql.JSONB(), "postgresql")`` is caught too — both of
   which the dialect-parity check alone would accept while silently breaking
   SQLite/MariaDB parity.
3. **The ``schema_enforcement_json`` prose trap stays closed** — migration
   ``0251``'s comment claims the column is "JSONB on Postgres" while its DDL
   creates generic ``sa.JSON()``, and the ORM agrees (generic ``JSON``). The
   column is pinned as generic ``JSON`` on BOTH sides, so acting on the false
   comment and aligning it to JSONB fails a test instead of silently creating
   the reverse mismatch.

All checks are static (no database): they read the migrations, the ORM metadata
and the source tree.
"""

from __future__ import annotations

import ast
import importlib.util
import re
from pathlib import Path
from types import ModuleType

from sqlalchemy import JSON, create_engine
from sqlalchemy.dialects.postgresql import JSONB

import modulo.db.models  # noqa: F401  (registers every mapper on Base.metadata)
from modulo.db.models.base import Base

_BACKEND = Path(__file__).resolve().parent.parent.parent
_SRC = _BACKEND / "src" / "modulo"
_MIGRATION_PATH = _SRC / "db" / "migrations" / "versions" / "0147_json_to_jsonb_standardize.py"
_MIGRATIONS_DIR = _SRC / "db" / "migrations"
_MIGRATION_VERSIONS_DIR = _MIGRATIONS_DIR / "versions"
#: The ONLY module allowed to construct a ``JSONB`` type.
_JSONB_ALIAS_PATH = _SRC / "db" / "models" / "base.py"

#: A Postgres dialect instance for type resolution. ``create_engine`` builds the
#: dialect without opening a connection, and (unlike ``postgresql.dialect()``)
#: is fully typed for mypy strict.
_POSTGRES_DIALECT = create_engine("postgresql+psycopg://").dialect

#: 0147 predates two migrations that removed three of its target pairs from
#: ``Base.metadata``. EVERY such pair must be named here — the parity test
#: hard-fails on any other absent 0147 table, so these exceptions cannot grow
#: silently and a future rename cannot hide.
#:
#: A RENAME is expressed as the pair's CURRENT location, and the column is then
#: checked THERE — so a renamed table is still covered rather than skipped.
_RENAMED_0147_PAIRS: dict[tuple[str, str], tuple[str, str]] = {
    # 0257_rename_remy_to_assistant: the converted column moved with the table.
    ("remy_skills", "triggers"): ("assistant_skills", "triggers"),
}

#: A DROP is expressed per pair with the migration that removed it. The parity
#: test asserts the table stays absent; the dedicated "actually dropped" check
#: additionally reads that migration's OWN source and requires a real
#: ``DROP TABLE`` of the named table. The absence check alone cannot tell a drop
#: from a rename — both remove the old name from ``Base.metadata`` — so without
#: that second check a rename recorded here would pass while the renamed column
#: was never verified.
_DROPPED_0147_PAIRS: dict[tuple[str, str], str] = {
    # 0179_drop_workspace_leases
    ("workspace_leases", "resource_usage_json"): "0179_drop_workspace_leases",
    ("workspace_leases", "output_artifact_refs_json"): "0179_drop_workspace_leases",
}


def _load_migration() -> ModuleType:
    assert _MIGRATION_PATH.exists(), f"Migration file missing: {_MIGRATION_PATH}"
    spec = importlib.util.spec_from_file_location("migration_0147_json_to_jsonb_standardize", _MIGRATION_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _resolved_pair(pair: tuple[str, str]) -> tuple[str, str]:
    """Return the pair's current ``(table, column)`` after the 0257 rename."""
    return _RENAMED_0147_PAIRS.get(pair, pair)


def _migration_source(migration_name: str) -> str:
    """Read the named migration's source, failing loudly on a bad reference."""
    path = _MIGRATION_PATH.parent / f"{migration_name}.py"
    assert path.exists(), (
        f"_DROPPED_0147_PAIRS names migration {migration_name!r}, but {path} does not exist — "
        "the migration reference is wrong"
    )
    return path.read_text(encoding="utf-8")


def _module_string_constants(tree: ast.Module) -> dict[str, str]:
    """Module-level ``NAME = "literal"`` bindings, so ``op.drop_table(_T)`` resolves."""
    constants: dict[str, str] = {}
    for node in tree.body:
        target: ast.expr | None
        value: ast.expr | None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        elif isinstance(node, ast.AnnAssign):
            target, value = node.target, node.value
        else:
            continue
        if isinstance(target, ast.Name) and isinstance(value, ast.Constant) and isinstance(value.value, str):
            constants[target.id] = value.value
    return constants


def _execute_sql_literals(tree: ast.Module) -> list[str]:
    """Every string constant passed to an ``execute(...)``-family call.

    Covers ``op.execute(...)`` / ``<bind>.execute(...)`` and a migration-local
    wrapper such as 0254's ``_execute(sql)`` (whose whole body is
    ``op.execute(text(sql))``). Only strings actually handed to the executor are
    yielded — prose in a module/function docstring is never a declaration.
    """
    literals: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else None
        if name is None or name.strip("_") != "execute":
            continue
        for arg in node.args:
            literals.extend(
                sub.value for sub in ast.walk(arg) if isinstance(sub, ast.Constant) and isinstance(sub.value, str)
            )
    return literals


def _migration_drops_table(migration_name: str, table: str) -> bool:
    """True iff *migration_name*'s OWN source issues ``DROP TABLE <table>``.

    Recognises both idioms: ``op.drop_table("t")`` (or a module-level string
    constant ``op.drop_table(_T)``) and a raw ``DROP TABLE ... t`` inside an
    ``execute(...)`` string. The name recorded in the map is never trusted.
    """
    source = _migration_source(migration_name)
    tree = ast.parse(source, filename=migration_name)
    constants = _module_string_constants(tree)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else None
        if name != "drop_table":
            continue
        arg = node.args[0]
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            value = arg.value
        elif isinstance(arg, ast.Name):
            value = constants.get(arg.id)
        else:
            value = None
        if value == table:
            return True

    drop_re = re.compile(rf"\bDROP\s+TABLE\b[^;]*\b{re.escape(table)}\b", re.IGNORECASE)
    return any(drop_re.search(sql) for sql in _execute_sql_literals(tree))


def _migration_renames_table(migration_name: str, table: str) -> bool:
    """True iff *migration_name*'s source appears to RENAME *table* (diagnosis only)."""
    source = _migration_source(migration_name)
    if re.search(r"\bRENAME\s+TO\b", source, re.IGNORECASE) is None:
        return False
    return re.search(rf"['\"]{re.escape(table)}['\"]", source) is not None


def test_0147_pairs_that_still_resolve_are_jsonb_on_postgres() -> None:
    """Every mapped 0147 pair resolves to a JSONB dialect impl."""
    pairs = _load_migration()._JSON_TO_JSONB
    assert pairs, "0147 declares no (table, column) pairs — the contract moved"

    dialect = _POSTGRES_DIALECT
    violations: list[str] = []

    for pair in pairs:
        table_name, column_name = pair

        if pair in _DROPPED_0147_PAIRS:
            if table_name in Base.metadata.tables:
                violations.append(
                    f"{table_name}.{column_name}: recorded as dropped by "
                    f"{_DROPPED_0147_PAIRS[pair]} but the table is back in Base.metadata"
                )
            continue

        resolved_table, resolved_column = _resolved_pair(pair)
        table = Base.metadata.tables.get(resolved_table)
        if table is None:
            violations.append(
                f"{table_name}.{column_name}: table {resolved_table!r} is absent from "
                "Base.metadata and the pair is not justified in _DROPPED_0147_PAIRS — a "
                "renamed or dropped 0147 table must be named explicitly, never skipped"
            )
            continue

        column = table.columns.get(resolved_column)
        if column is None:
            violations.append(f"{resolved_table}.{resolved_column}: column missing from the mapped table")
            continue

        dialect_impl = column.type.dialect_impl(dialect)
        if not isinstance(dialect_impl, JSONB):
            violations.append(
                f"{resolved_table}.{resolved_column}: declared {type(column.type).__name__}, "
                f"renders as {type(dialect_impl).__name__} on postgresql — the physical "
                "column is JSONB after 0147"
            )

    assert not violations, (
        "0147-converted columns whose ORM declaration disagrees with the physical jsonb "
        "column (a SQL predicate against these binds as ::JSON and raises "
        "'operator does not exist: jsonb = json'):\n  " + "\n  ".join(violations)
    )


def test_0147_pair_resolution_is_complete_and_justified() -> None:
    """Every absent 0147 pair is justified, and no justification is stale."""
    pairs = set(_load_migration()._JSON_TO_JSONB)
    justified = set(_RENAMED_0147_PAIRS) | set(_DROPPED_0147_PAIRS)

    stale = sorted(justified - pairs)
    assert not stale, (
        "_RENAMED_0147_PAIRS/_DROPPED_0147_PAIRS name pairs 0147 does not convert "
        f"(a stale exception would hide whatever it still claims): {stale}"
    )

    unmapped_absent = sorted(
        pair for pair in pairs if pair not in justified and _resolved_pair(pair)[0] not in Base.metadata.tables
    )
    assert not unmapped_absent, (
        "0147 pairs whose table is absent from Base.metadata and are not justified in "
        f"_RENAMED_0147_PAIRS/_DROPPED_0147_PAIRS: {unmapped_absent}"
    )

    for renamed_from, destination in _RENAMED_0147_PAIRS.items():
        assert destination[0] in Base.metadata.tables, (
            f"_RENAMED_0147_PAIRS maps {renamed_from} to {destination}, but {destination[0]!r} "
            "is not mapped in Base.metadata"
        )


def test_dropped_0147_pairs_are_actually_dropped() -> None:
    """Each pair recorded as dropped IS dropped by the migration it names.

    Reading the named migration's own source is what closes the residual hole:
    the parity test's absence check cannot tell a drop from a rename (both remove
    the old table name from ``Base.metadata``), so a rename recorded in
    ``_DROPPED_0147_PAIRS`` would pass both it and the justification check while
    the destination column was never verified — the FAR-1856 defect class this
    guard exists to prevent.
    """
    failures: list[str] = []
    for (table_name, column_name), migration_name in sorted(_DROPPED_0147_PAIRS.items()):
        if _migration_drops_table(migration_name, table_name):
            continue
        if _migration_renames_table(migration_name, table_name):
            failures.append(
                f"{table_name}.{column_name}: recorded in _DROPPED_0147_PAIRS with migration "
                f"{migration_name}, but that migration RENAMES {table_name!r} rather than dropping "
                "it — a rename belongs in _RENAMED_0147_PAIRS so the destination column is checked"
            )
        else:
            failures.append(
                f"{table_name}.{column_name}: recorded in _DROPPED_0147_PAIRS with migration "
                f"{migration_name}, but that migration contains no DROP TABLE for {table_name!r} — "
                "either the map entry or its migration reference is wrong"
            )
    assert not failures, "_DROPPED_0147_PAIRS entries whose named migration does not drop the table:\n  " + "\n  ".join(
        failures
    )


def _jsonb_construction_lines(path: Path) -> list[int]:
    """Line numbers of every ``JSONB(...)`` Call in *path* (AST, not text).

    AST rather than a regex: prose that merely mentions ``JSONB (`` must not
    register, while every call shape — bare ``JSONB()``,
    ``postgresql.JSONB()``, ``JSON().with_variant(JSONB(), ...)`` — must.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    lines: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            name = func.id
        elif isinstance(func, ast.Attribute):
            name = func.attr
        else:
            continue
        if name == "JSONB":
            lines.append(node.lineno)
    return lines


def test_jsonb_is_only_constructed_in_the_shared_alias() -> None:
    """A ``JSONB(...)`` call may appear only in the shared alias (``base.py``).

    Migrations are excluded (they legitimately issue jsonb DDL). Anywhere else —
    a model, a Core ``Table``, a CRUD module — a locally constructed ``JSONB()``
    both re-fragments the single-definition rule and breaks SQLite/MariaDB parity,
    and the dialect-parity check alone cannot catch it (a direct ``JSONB()`` has
    a JSONB dialect impl too).
    """
    offenders: dict[str, list[int]] = {}
    for path in sorted(_SRC.rglob("*.py")):
        if _MIGRATIONS_DIR in path.parents:
            continue
        lines = _jsonb_construction_lines(path)
        if lines:
            offenders[path.relative_to(_BACKEND).as_posix()] = lines

    alias_key = _JSONB_ALIAS_PATH.relative_to(_BACKEND).as_posix()
    assert alias_key in offenders, (
        f"{alias_key} must construct the shared JSONB variant (JSONB_COL) — removing it "
        "would leave every 0147 column undeclared"
    )
    extra = {path: lines for path, lines in offenders.items() if path != alias_key}
    assert not extra, (
        "A JSONB() call outside the shared alias (modulo.db.models.base.JSONB_COL). "
        f"Import JSONB_COL instead of constructing JSONB locally: {extra}"
    )


# ---------------------------------------------------------------------------
# Migration-derived jsonb pair extraction (FAR-1941).
#
# The gate above hard-codes 0147's pair list by loading that migration module.
# FAR-1941 extends the dialect-parity check to EVERY migration that declares a
# jsonb column, without a hand-maintained pair inventory: the pair set is read
# from the migrations' own source. Supported declaration idioms:
#
#   * ``op.add_column(<table>, sa.Column(<col>, <jsonb-type>, ...))``
#   * ``op.create_table(<table>, sa.Column(<col>, <jsonb-type>, ...), ...)``
#   * ``op.alter_column(<table>, <col>, type_=<jsonb-type>, ...)``
#   * raw SQL ``ALTER TABLE <t> ADD COLUMN [IF NOT EXISTS] <c> ... jsonb``
#   * raw SQL ``ALTER TABLE <t> ALTER COLUMN <c> TYPE jsonb``
#   * raw SQL ``CREATE TABLE <t> ( ... <c> jsonb ... )`` — the parenthesised
#     body is split on TOP-LEVEL commas (paren- and quote-aware), so a
#     single-line body parses exactly like a multi-line one and a table-level
#     constraint (PRIMARY KEY / FOREIGN KEY / UNIQUE / CHECK / CONSTRAINT) is
#     never mistaken for a column
#   * a declarative module-level list — a tuple of ``(table, column)`` pairs
#     (0147's ``_JSON_TO_JSONB``) or a table-name constant plus a JSON-named
#     tuple of column names (0129's ``_TABLE`` / ``_JSON_COLUMNS``) — harvested
#     only when the module itself constructs a JSONB type and the constant's
#     NAME signals JSON, so an unrelated per-table tuple such as 0166's
#     ``_PREFLIGHT_COLUMNS`` is never misread as a jsonb declaration.
#
# A jsonb type expression is any ``JSONB(...)`` call (bare or qualified) or a
# ``...with_variant(<JSONB>, "postgresql")`` call, including the conditional
# ``<jsonb> if is_postgres else <json>`` form (0192) and assignment to a local
# variable later passed to ``sa.Column`` (0192's ``blob_type`` / 0220's
# ``col_type``). The bare CLASS reference (``sa.dialects.postgresql.JSONB``
# with no call) is deliberately NOT recognised: it is not a jsonb-type
# *expression* under this definition, and no migration declares a column that
# way — add it here if one ever does.
#
# The raw-SQL path only sees strings actually handed to an executor
# (``op.execute`` / ``<bind>.execute`` / a migration-local ``_execute``
# wrapper); prose in a docstring or comment is never a declaration, however
# jsonb-shaped it looks (0129's docstring shows an ``ALTER ... TYPE jsonb``).
# ---------------------------------------------------------------------------

_ALTER_ADD_JSONB_RE = re.compile(
    r'ALTER\s+TABLE\s+(?:public\.)?"?([A-Za-z0-9_]+)"?\s+ADD\s+COLUMN(?:\s+IF\s+NOT\s+EXISTS)?\s+"?([A-Za-z0-9_]+)"?\s+jsonb\b',
    re.IGNORECASE,
)
_ALTER_TYPE_JSONB_RE = re.compile(
    r'ALTER\s+TABLE\s+(?:public\.)?"?([A-Za-z0-9_]+)"?\s+ALTER\s+COLUMN\s+"?([A-Za-z0-9_]+)"?\s+TYPE\s+jsonb\b',
    re.IGNORECASE,
)
_CREATE_TABLE_RE = re.compile(
    r'CREATE\s+TABLE(?:\s+IF\s+NOT\s+EXISTS)?\s+(?:public\.)?"?([A-Za-z0-9_]+)"?\s*\(',
    re.IGNORECASE,
)
#: A single column definition, ``<name> jsonb ...``, matched against ONE
#: top-level comma-separated entry of a parsed ``CREATE TABLE`` body. Anchored
#: per-entry (never per line), so a single-line ``CREATE TABLE`` is parsed the
#: same as a multi-line one, and a table-level constraint never matches: its
#: first identifier is not followed by the ``jsonb`` type token (``CONSTRAINT``
#: → ``<name>``, ``PRIMARY`` → ``KEY``, ``CHECK`` → ``(``).
_COLUMN_JSONB_RE = re.compile(r'^\s*"?([A-Za-z0-9_]+)"?\s+jsonb\b', re.IGNORECASE)


def _create_table_body(sql: str, open_index: int) -> str | None:
    """The text between the ``(`` at *open_index* and its matching ``)``.

    Paren depth, single- and double-quoted strings are tracked, so a
    ``CHECK ((...))`` or a ``DEFAULT 'a)b'`` inside the body cannot terminate
    it early. Returns ``None`` when the parentheses never close.
    """
    depth = 0
    quote: str | None = None
    index = open_index
    while index < len(sql):
        char = sql[index]
        if quote is not None:
            if char == quote:
                if index + 1 < len(sql) and sql[index + 1] == quote:
                    index += 2
                    continue
                quote = None
        elif char in "'\"":
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return sql[open_index + 1 : index]
        index += 1
    return None


def _split_top_level_commas(body: str) -> list[str]:
    """Split *body* on commas that are not nested in parens or quotes.

    This is what makes a single-line ``CREATE TABLE`` parse: a column definition
    is delimited by a top-level comma, not by a newline.
    """
    parts: list[str] = []
    depth = 0
    quote: str | None = None
    start = 0
    index = 0
    while index < len(body):
        char = body[index]
        if quote is not None:
            if char == quote:
                if index + 1 < len(body) and body[index + 1] == quote:
                    index += 2
                    continue
                quote = None
        elif char in "'\"":
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char == "," and depth == 0:
            parts.append(body[start:index])
            start = index + 1
        index += 1
    parts.append(body[start:])
    return parts


def _string_constant(node: ast.expr, constants: dict[str, str]) -> str | None:
    """A string literal, or a module-level string constant it names."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name) and node.id in constants:
        return constants[node.id]
    return None


def _module_string_sequence_constants(tree: ast.Module) -> dict[str, tuple[str | tuple[str, ...], ...]]:
    """Module-level sequence bindings of string / string-tuple literals."""
    sequences: dict[str, tuple[str | tuple[str, ...], ...]] = {}
    for node in tree.body:
        target: ast.expr | None
        value: ast.expr | None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        elif isinstance(node, ast.AnnAssign):
            target, value = node.target, node.value
        else:
            continue
        if not isinstance(target, ast.Name) or not isinstance(value, (ast.Tuple, ast.List)):
            continue
        elements: list[str | tuple[str, ...]] = []
        for element in value.elts:
            if isinstance(element, ast.Constant) and isinstance(element.value, str):
                elements.append(element.value)
            elif (
                isinstance(element, ast.Tuple)
                and element.elts
                and all(isinstance(inner, ast.Constant) and isinstance(inner.value, str) for inner in element.elts)
            ):
                elements.append(tuple(inner.value for inner in element.elts))
            else:
                elements = []
                break
        if elements:
            sequences[target.id] = tuple(elements)
    return sequences


def _is_jsonb_type_expression(node: ast.expr, jsonb_names: frozenset[str]) -> bool:
    """True iff *node* constructs (or is a name bound to) a Postgres JSONB type."""
    if isinstance(node, ast.IfExp):
        return _is_jsonb_type_expression(node.body, jsonb_names) or _is_jsonb_type_expression(node.orelse, jsonb_names)
    if isinstance(node, ast.Name):
        return node.id in jsonb_names
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name) and func.id == "JSONB":
        return True
    if isinstance(func, ast.Attribute) and func.attr == "JSONB":
        return True
    if isinstance(func, (ast.Name, ast.Attribute)):
        name = func.id if isinstance(func, ast.Name) else func.attr
        if name == "with_variant":
            return any(_is_jsonb_type_expression(arg, jsonb_names) for arg in node.args) or any(
                _is_jsonb_type_expression(keyword.value, jsonb_names) for keyword in node.keywords
            )
    return False


def _jsonb_bound_names(tree: ast.Module) -> frozenset[str]:
    """Names bound to a jsonb type expression anywhere in the module."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and _is_jsonb_type_expression(node.value, frozenset()):
            names.update(target.id for target in node.targets if isinstance(target, ast.Name))
        elif (
            isinstance(node, ast.AnnAssign)
            and node.value is not None
            and isinstance(node.target, ast.Name)
            and _is_jsonb_type_expression(node.value, frozenset())
        ):
            names.add(node.target.id)
    return frozenset(names)


def _raw_sql_jsonb_pairs(sql: str) -> set[tuple[str, str]]:
    """jsonb ``(table, column)`` pairs declared by raw SQL in *sql*."""
    pairs: set[tuple[str, str]] = set()
    for match in _ALTER_ADD_JSONB_RE.finditer(sql):
        pairs.add((match.group(1).lower(), match.group(2).lower()))
    for match in _ALTER_TYPE_JSONB_RE.finditer(sql):
        pairs.add((match.group(1).lower(), match.group(2).lower()))
    for match in _CREATE_TABLE_RE.finditer(sql):
        body = _create_table_body(sql, match.end() - 1)
        if body is None:
            continue
        table = match.group(1).lower()
        for definition in _split_top_level_commas(body):
            column_match = _COLUMN_JSONB_RE.match(definition)
            if column_match is not None:
                pairs.add((table, column_match.group(1).lower()))
    return pairs


def _declared_jsonb_pairs(source: str) -> set[tuple[str, str]]:
    """Every ``(table, column)`` pair *source* declares as jsonb (see above)."""
    tree = ast.parse(source)
    constants = _module_string_constants(tree)
    sequences = _module_string_sequence_constants(tree)
    jsonb_names = _jsonb_bound_names(tree)
    pairs: set[tuple[str, str]] = set()

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else None
        if name == "add_column" and len(node.args) >= 2:
            table = _string_constant(node.args[0], constants)
            column = node.args[1]
            if isinstance(column, ast.Call) and column.args:
                column_name = _string_constant(column.args[0], constants)
                column_type = column.args[1] if len(column.args) > 1 else None
                if (
                    table
                    and column_name
                    and column_type is not None
                    and _is_jsonb_type_expression(column_type, jsonb_names)
                ):
                    pairs.add((table, column_name))
        elif name == "create_table" and node.args:
            table = _string_constant(node.args[0], constants)
            if table:
                for argument in node.args[1:]:
                    if not isinstance(argument, ast.Call) or len(argument.args) < 2:
                        continue
                    column_name = _string_constant(argument.args[0], constants)
                    if column_name and _is_jsonb_type_expression(argument.args[1], jsonb_names):
                        pairs.add((table, column_name))
        elif name == "alter_column" and len(node.args) >= 2:
            table = _string_constant(node.args[0], constants)
            column_name = _string_constant(node.args[1], constants)
            column_type = next((keyword.value for keyword in node.keywords if keyword.arg == "type_"), None)
            if (
                table
                and column_name
                and column_type is not None
                and _is_jsonb_type_expression(column_type, jsonb_names)
            ):
                pairs.add((table, column_name))

    # Only strings actually handed to an executor — NEVER every string constant.
    # A migration docstring can contain an illustrative ``ALTER TABLE ... jsonb``
    # (0129's does), and harvesting prose would inject a phantom pair: benign
    # only while the name does not resolve, but a hard false positive the day a
    # docstring names a real non-jsonb column.
    for literal in _execute_sql_literals(tree):
        pairs |= _raw_sql_jsonb_pairs(literal)

    # Declarative module-level lists (0147's ``_JSON_TO_JSONB``, 0129's
    # ``_JSON_COLUMNS``): harvested only when the module constructs a JSONB type
    # AND the constant's own NAME signals JSON — an unrelated pair tuple
    # (0166's ``_PREFLIGHT_COLUMNS``) is thereby never misread.
    if any(isinstance(n, ast.Call) and _is_jsonb_type_expression(n, frozenset()) for n in ast.walk(tree)):
        for constant_name, value in sequences.items():
            if "JSON" not in constant_name.upper():
                continue
            if all(isinstance(element, tuple) and len(element) == 2 for element in value):
                for element in value:
                    if isinstance(element, tuple) and len(element) == 2:
                        pairs.add((element[0].lower(), element[1].lower()))
            else:
                table = next((v for k, v in constants.items() if k in ("_TABLE", "TABLE")), None)
                if table:
                    for element in value:
                        if isinstance(element, str):
                            pairs.add((table.lower(), element.lower()))
    return pairs


def _migration_declared_jsonb_pairs() -> dict[tuple[str, str], list[str]]:
    """Pair -> the migration stems that declare it jsonb, across all migrations."""
    declared: dict[tuple[str, str], list[str]] = {}
    for path in sorted(_MIGRATION_VERSIONS_DIR.glob("*.py")):
        if path.name.startswith("__"):
            continue
        for pair in _declared_jsonb_pairs(path.read_text(encoding="utf-8")):
            declared.setdefault(pair, []).append(path.stem)
    return declared


def test_every_migration_declared_jsonb_column_is_jsonb_on_postgres() -> None:
    """Every mapped column a migration declares jsonb resolves to a JSONB impl.

    The pair set is read from the migrations themselves (no hand-maintained
    inventory). A declared pair whose table or column no longer exists in
    ``Base.metadata`` is skipped — a later migration dropped or renamed it, and
    that owner decides the type — but every pair that still resolves MUST be
    JSONB, or a SQL predicate against it binds as ``$1::JSON`` and raises
    ``operator does not exist: jsonb = json``.
    """
    declared = _migration_declared_jsonb_pairs()
    assert declared, "no migration declares a jsonb column — the extractor is broken"

    dialect = _POSTGRES_DIALECT
    violations: list[str] = []

    for (table_name, column_name), migrations in sorted(declared.items()):
        table = Base.metadata.tables.get(table_name)
        if table is None:
            continue
        column = table.columns.get(column_name)
        if column is None:
            continue
        if not isinstance(column.type.dialect_impl(dialect), JSONB):
            violations.append(
                f"{table_name}.{column_name}: declared {type(column.type).__name__} on the ORM, but "
                f"migration(s) {', '.join(sorted(migrations))} declare it jsonb — a SQL predicate "
                "against this column binds as ::JSON and raises 'operator does not exist: jsonb = json'"
            )

    assert not violations, (
        "ORM columns declared jsonb by a migration but mapped to generic JSON (each binds as ::JSON "
        "on a predicate and raises 'operator does not exist: jsonb = json'):\n  " + "\n  ".join(violations)
    )


def test_raw_sql_create_table_parses_jsonb_column_on_one_line() -> None:
    """A single-line ``CREATE TABLE`` body is split on top-level commas.

    Regression guard (FAR-1941 verification): the first extractor anchored a
    column definition to the START OF A LINE, so a ``CREATE TABLE`` written on
    one line — as 0108/0109/0110 are — yielded NO pair at all. The gate read as
    green while a jsonb column went unchecked, and because
    ``tests/architecture/`` is the PR-time gate while the integration parity
    test runs only in the pre-deploy gate, the drift could reach ``main``.
    """
    single_line = "CREATE TABLE IF NOT EXISTS public.foo ( id uuid NOT NULL, payload jsonb NOT NULL, name text );"
    assert _raw_sql_jsonb_pairs(single_line) == {("foo", "payload")}

    multi_line = "CREATE TABLE public.bar (\n    id uuid NOT NULL,\n    payload jsonb NOT NULL,\n    name text\n);"
    assert _raw_sql_jsonb_pairs(multi_line) == {("bar", "payload")}


def test_raw_sql_create_table_handles_constraints_and_quoted_identifiers() -> None:
    """Table-level constraints and nested type parens are never a column.

    ``numeric(18,6)`` and the ``CHECK (...)`` array contain commas INSIDE
    parens; ``PRIMARY KEY`` / ``FOREIGN KEY`` / ``CONSTRAINT`` are table-level
    clauses whose first identifier is not followed by the ``jsonb`` type token.
    The quoted ``"values"`` column is a real jsonb declaration and must survive.
    """
    sql = (
        "CREATE TABLE public.baz ("
        " id uuid NOT NULL,"
        " \"values\" jsonb DEFAULT '{}'::jsonb NOT NULL,"
        " rate numeric(18,6),"
        " CONSTRAINT ck_baz_kind CHECK (((kind)::text = ANY "
        "((ARRAY['a'::character varying, 'b'::character varying])::text[]))),"
        " PRIMARY KEY (id),"
        " FOREIGN KEY (id) REFERENCES public.other (id)"
        " );"
    )
    assert _raw_sql_jsonb_pairs(sql) == {("baz", "values")}


def test_docstring_prose_is_not_harvested_as_a_declaration() -> None:
    """Only strings handed to an executor are declarations, never docstring prose.

    The extractor used to walk EVERY string constant, so a docstring example
    containing ``ALTER TABLE ... jsonb`` entered the pair set as if it were
    executed DDL. Only the ``execute(...)`` string is a real declaration.
    """
    source = '''
"""Illustrative DDL (never executed):
    ALTER TABLE public.widgets ADD COLUMN payload jsonb;
    CREATE TABLE public.gadgets ( id uuid NOT NULL, blob jsonb NOT NULL );
"""


def upgrade() -> None:
    op.execute("ALTER TABLE public.real_table ADD COLUMN real_col jsonb")
'''
    pairs = _declared_jsonb_pairs(source)
    assert ("real_table", "real_col") in pairs
    assert ("widgets", "payload") not in pairs
    assert ("gadgets", "blob") not in pairs


def test_migration_docstring_illustration_is_not_a_declaration() -> None:
    """0129's docstring example must not become a real declared pair.

    ``0129_runs_json_to_jsonb``'s module docstring shows
    ``ALTER TABLE public."runs" ALTER COLUMN "col" TYPE jsonb`` as prose. The
    original extractor harvested it as ``("runs", "col")`` — benign only while
    the name does not resolve, but a docstring naming a real, non-jsonb column
    would fail the gate on a PR that never touched that column.
    """
    assert ("runs", "col") not in _migration_declared_jsonb_pairs()


def test_executed_ddl_through_a_local_execute_wrapper_is_harvested() -> None:
    """0254's ``_execute(sql)`` wrapper DDL still counts as a declaration.

    Restricting the scan to executed strings must not lose real DDL: 0254
    promotes ``evals.config_json`` to jsonb via its local ``_execute`` helper
    (an ``op.execute(text(sql))`` wrapper), so the execute-family match has to
    recognise it — otherwise the pair silently drops out of the gate's scope.
    """
    assert ("evals", "config_json") in _migration_declared_jsonb_pairs()


_SCHEMA_ENFORCEMENT_MIGRATION = _MIGRATION_VERSIONS_DIR / "0251_schema_enforcement_telemetry.py"
_SCHEMA_ENFORCEMENT_TABLE = "run_node_outputs"
_SCHEMA_ENFORCEMENT_COLUMN = "schema_enforcement_json"


def _add_column_type_name(source: str, table: str, column: str) -> str | None:
    """The type NAME in an ``op.add_column(<table>, sa.Column(<column>, <type>))``.

    Returns ``"JSON"`` / ``"JSONB"`` / the bare call name, or None when no such
    declaration exists in *source*.
    """
    tree = ast.parse(source)
    constants = _module_string_constants(tree)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else None
        if name != "add_column" or len(node.args) < 2:
            continue
        if _string_constant(node.args[0], constants) != table:
            continue
        column_call = node.args[1]
        if not isinstance(column_call, ast.Call) or len(column_call.args) < 2:
            continue
        if _string_constant(column_call.args[0], constants) != column:
            continue
        type_node = column_call.args[1]
        if isinstance(type_node, ast.Call):
            type_func = type_node.func
            if isinstance(type_func, ast.Name):
                return type_func.id
            if isinstance(type_func, ast.Attribute):
                return type_func.attr
        return None
    return None


def test_schema_enforcement_json_is_generic_json_on_both_sides() -> None:
    """Pin ``run_node_outputs.schema_enforcement_json`` as generic ``JSON``.

    Migration 0251's comment claims the column is "JSONB on Postgres", but its
    DDL creates generic ``sa.JSON()`` and the ORM maps generic ``JSON`` — the
    two agree on ``json``. This test pins BOTH sides: anyone who acts on the
    false comment and aligns the column to JSONB fails here (and, once 0251 is
    edited, the extractor's general jsonb check fires too), instead of silently
    creating the reverse ORM-JSONB-over-DB-json mismatch.
    """
    orm_column = Base.metadata.tables[_SCHEMA_ENFORCEMENT_TABLE].columns[_SCHEMA_ENFORCEMENT_COLUMN]
    assert type(orm_column.type) is JSON, (
        f"{_SCHEMA_ENFORCEMENT_TABLE}.{_SCHEMA_ENFORCEMENT_COLUMN} must stay generic JSON "
        f"(0251 created it as sa.JSON()); found {type(orm_column.type).__name__}"
    )
    assert not isinstance(orm_column.type.dialect_impl(_POSTGRES_DIALECT), JSONB), (
        f"{_SCHEMA_ENFORCEMENT_TABLE}.{_SCHEMA_ENFORCEMENT_COLUMN} renders as JSONB on postgres — "
        "0251's comment is wrong and the column is genuinely json on both sides"
    )
    declared_migration_type = _add_column_type_name(
        _SCHEMA_ENFORCEMENT_MIGRATION.read_text(encoding="utf-8"),
        _SCHEMA_ENFORCEMENT_TABLE,
        _SCHEMA_ENFORCEMENT_COLUMN,
    )
    assert declared_migration_type == "JSON", (
        "0251 must declare schema_enforcement_json with generic sa.JSON() (its comment's "
        f"'JSONB on Postgres' claim is false); found {declared_migration_type!r}"
    )
    assert (_SCHEMA_ENFORCEMENT_TABLE, _SCHEMA_ENFORCEMENT_COLUMN) not in _migration_declared_jsonb_pairs(), (
        "no migration may declare schema_enforcement_json as jsonb — it is generic JSON on both sides"
    )
