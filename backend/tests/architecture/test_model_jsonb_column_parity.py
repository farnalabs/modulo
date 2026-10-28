"""Architecture gate: the 0147-converted JSON columns that still resolve are JSONB.

SCOPE — the gate checks every ``(table, column)`` pair it can PARSE out of the
migrations, not merely the 66 ``0147_json_to_jsonb_standardize`` promoted. The
pair set is derived mechanically from the migrations (FAR-1941 extended it beyond
0147): each migration's own source is parsed for the jsonb declaration idioms —
``op.add_column`` / ``op.create_table`` with a JSONB-typed ``sa.Column``,
``op.alter_column`` with a JSONB ``type_``, the raw-SQL forms ``ALTER TABLE ...
ADD/ALTER COLUMN ... jsonb`` and ``CREATE TABLE ... (col jsonb ...)``,
declarative module-level pair/column lists such as 0147's ``_JSON_TO_JSONB`` and
0129's ``_JSON_COLUMNS``, and the SIMPLE interpolated case: an
``op.execute(f "...")`` inside a ``for <col> in <CONSTANT>`` loop over a
module-level sequence of column names (0226's ``_columns``, 0235's ``COLUMNS``),
rendered once per element. Every parsed pair whose table and column still resolve
against ``Base.metadata`` must map to a JSONB dialect impl. Pairs whose table or
column no longer exists (a later migration dropped or renamed them) carry no
check — the migration that removed them owns that decision.

This is **not** a "no hand-maintained inventory, so any migration is covered"
gate, and an earlier revision of this docstring wrongly said it was. Parsing
cannot see every interpolation: a declaration whose table, column or TYPE is an
interpolation the loop resolver cannot bind — a function parameter, a call
result, an attribute (0147's ``_add_temp_column`` builds ``... ADD COLUMN
"{tmp}" {cast}`` from parameters) — yields no pair. A parsed-pairs inventory
alone would then miss it SILENTLY — the exact defect class this file has now
shipped three times.
``test_every_executed_jsonb_declaration_is_parsed_or_justified`` narrows that gap
but does NOT close it — it is bounded, best-effort cover, not the guarantee. It
inspects only the declarations the renderer can reach AT THE CALL SITE: one
written SYNTACTICALLY as a string literal, an f-string, a ``+``-concatenation,
or a module-level scalar-string constant, passed directly to an execute-family
call. Each such declaration that yields no pair — a literal ``jsonb`` type
token, or a column TYPE that is an interpolation (which COULD be jsonb) — must
be named in ``_UNPARSED_JSONB_DDL`` with the reason it is out of the extractor's
reach, or the gate fails naming it. A declaration reached any other way —
through a function-local variable, a module-level constant whose value is a CALL
(``DDL = sa.text("...")``), or a subscript (``DDLS[0]``) — produces no rendering
at all, so the net emits nothing for it and it passes unseen.

That class is closed elsewhere, not here: the authoritative net is the
live-database oracle
``tests/integration/test_initial_migration.py::test_migrated_schema_matches_orm_metadata``,
which runs the real migration chain and compares the resulting schema to the ORM
metadata. Its ``_JSONB_DB_TO_JSON_ORM`` ignore map is now empty, so a genuine
``jsonb``(DB) → ``JSON``(ORM) divergence — however the migration declared it —
fails there. This PR-time gate is a fast partial guardrail in front of that
oracle; it is not the authority.

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
from tests.unit._source_scan import iter_source_text

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

#: The token an UNRESOLVED interpolation renders to in a declaration SHAPE.
#: Deliberately not identifier-shaped for the parser's purposes, so a
#: ``TYPE {variable}`` interpolation is never mistaken for a literal ``jsonb`` —
#: the declaration net treats it as a possible-jsonb declaration instead.
_INTERP = "__INTERP__"

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
    # FAR-1962 (chunk 13 slice 3+4): the legacy eval_definitions table is purged
    # after the cutover proved every row redundant.
    ("eval_definitions", "config_json"): "0324_purge_eval_definitions",
}

#: A TRANSITIONAL state between "mapped" and "dropped": the ORM entity is gone
#: but the physical table still exists until a later slice drops it, so there is
#: no ORM declaration left to check. Every such pair must be named here with
#: the change that removed the model; the parity test asserts the table really
#: is absent from ``Base.metadata`` so the entry cannot go stale while a model
#: still exists.
_NO_ORM_MODEL_0147_PAIRS: dict[tuple[str, str], str] = {}


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


def _is_execute_call(node: ast.Call) -> bool:
    """True for ``op.execute`` / ``<bind>.execute`` / a ``_execute`` wrapper."""
    func = node.func
    name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else None
    return name is not None and name.strip("_") == "execute"


def _module_string_expressions(tree: ast.Module) -> dict[str, ast.expr]:
    """Module-level bindings whose value is a SQL string expression.

    ``NAME = "..."``, ``NAME = f"..."`` and ``NAME = "..." + "..."``. A migration
    that assigns DDL to a module-level name and passes the NAME to ``execute`` is
    still a real declaration, so the name resolves to its expression rather than
    dropping out of the gate.
    """
    expressions: dict[str, ast.expr] = {}
    for node in tree.body:
        target: ast.expr | None
        value: ast.expr | None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        elif isinstance(node, ast.AnnAssign):
            target, value = node.target, node.value
        else:
            continue
        if isinstance(target, ast.Name) and isinstance(value, (ast.Constant, ast.JoinedStr, ast.BinOp)):
            expressions[target.id] = value
    return expressions


def _loop_sequence_bindings(
    tree: ast.Module, sequences: dict[str, tuple[str | tuple[str, ...], ...]]
) -> dict[int, dict[str, tuple[str, ...]]]:
    """Loop variables in scope at each execute call, when they iterate a constant.

    Deliberately shallow: ONLY ``for <name> in <CONSTANT>`` whose iterable is a
    module-level list/tuple of strings is resolved (0226's ``_columns``, 0235's
    ``COLUMNS``). A loop over a call, an attribute or any computed iterable is
    left unresolved — the declaration net then demands the DDL be justified.
    """
    string_sequences = {
        name: tuple(element for element in value if isinstance(element, str))
        for name, value in sequences.items()
        if value and all(isinstance(element, str) for element in value)
    }
    bindings: dict[int, dict[str, tuple[str, ...]]] = {}

    def visit(current: ast.AST, env: dict[str, tuple[str, ...]]) -> None:
        if isinstance(current, ast.For):
            inner = dict(env)
            if isinstance(current.target, ast.Name) and isinstance(current.iter, ast.Name):
                values = string_sequences.get(current.iter.id)
                if values is not None:
                    inner[current.target.id] = values
            for child in (*current.body, *current.orelse):
                visit(child, inner)
            return
        if isinstance(current, ast.Call) and _is_execute_call(current):
            bindings[id(current)] = dict(env)
        for child in ast.iter_child_nodes(current):
            visit(child, env)

    visit(tree, {})
    return bindings


def _render_sql_renderings(
    node: ast.expr, module_values: dict[str, ast.expr], env: dict[str, tuple[str, ...]]
) -> list[tuple[str, tuple[str, ...]]]:
    """``(shape, concrete renderings)`` for every string *node* produces.

    *shape* replaces each interpolation with ``__INTERP__`` so a declaration
    whose names come from an f-string still presents a declaration shape;
    *concrete* holds the fully-rendered strings, and is empty when some
    interpolation is not a bound loop variable. Descends through wrappers
    (``sa.text(...)``), tuples/lists and ``+`` concatenations.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [(node.value, (node.value,))]
    if isinstance(node, ast.JoinedStr):
        shape_parts: list[str] = []
        concretes: tuple[str, ...] = ("",)
        resolvable = True
        for part in node.values:
            if isinstance(part, ast.Constant) and isinstance(part.value, str):
                shape_parts.append(part.value)
                concretes = tuple(concrete + part.value for concrete in concretes)
            elif (
                isinstance(part, ast.FormattedValue)
                and part.conversion == -1
                and part.format_spec is None
                and isinstance(part.value, ast.Name)
                and part.value.id in env
            ):
                shape_parts.append(_INTERP)
                concretes = tuple(concrete + value for concrete in concretes for value in env[part.value.id])
            else:
                shape_parts.append(_INTERP)
                resolvable = False
        return [("".join(shape_parts), concretes if resolvable else ())]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        combined: list[tuple[str, tuple[str, ...]]] = []
        for left_shape, left_concretes in _render_sql_renderings(node.left, module_values, env):
            for right_shape, right_concretes in _render_sql_renderings(node.right, module_values, env):
                combined.append(
                    (left_shape + right_shape, tuple(a + b for a in left_concretes for b in right_concretes))
                )
        return combined
    if isinstance(node, ast.Name):
        if node.id in env:
            return [(value, (value,)) for value in env[node.id]]
        if node.id in module_values:
            return _render_sql_renderings(module_values[node.id], module_values, env)
        return []
    if isinstance(node, ast.Call):
        renderings: list[tuple[str, tuple[str, ...]]] = []
        for argument in node.args:
            renderings.extend(_render_sql_renderings(argument, module_values, env))
        for keyword in node.keywords:
            renderings.extend(_render_sql_renderings(keyword.value, module_values, env))
        return renderings
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        element_renderings: list[tuple[str, tuple[str, ...]]] = []
        for element in node.elts:
            element_renderings.extend(_render_sql_renderings(element, module_values, env))
        return element_renderings
    return []


def _execute_sql_literals(tree: ast.Module) -> list[str]:
    """Every SQL string handed to an ``execute(...)``-family call.

    Covers ``op.execute(...)`` / ``<bind>.execute(...)`` / a migration-local
    ``_execute`` wrapper, nested wrappers (``op.execute(sa.text(...))``),
    module-level string constants referenced by name, ``+`` concatenations, and
    the SIMPLE interpolated case — a ``for <col> in <CONSTANT>`` loop, rendered
    once per element (0226/0235). A string whose interpolation the loop resolver
    cannot bind yields nothing here, and so does a declaration reached through a
    function-local variable, a call-wrapped constant or a subscript:
    ``_executed_sql_jsonb_declarations`` catches the first kind (a rendered shape
    with an unresolved interpolation) but not the second. Only strings actually
    handed to the executor are yielded — docstring prose is never a declaration.
    """
    module_values = _module_string_expressions(tree)
    bindings = _loop_sequence_bindings(tree, _module_string_sequence_constants(tree))
    literals: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not _is_execute_call(node):
            continue
        env = bindings.get(id(node), {})
        for argument in node.args:
            for _shape, concretes in _render_sql_renderings(argument, module_values, env):
                literals.extend(concretes)
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

        if pair in _NO_ORM_MODEL_0147_PAIRS:
            if table_name in Base.metadata.tables:
                violations.append(
                    f"{table_name}.{column_name}: recorded in _NO_ORM_MODEL_0147_PAIRS by "
                    f"{_NO_ORM_MODEL_0147_PAIRS[pair]} but the table is mapped again in Base.metadata"
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
    justified = set(_RENAMED_0147_PAIRS) | set(_DROPPED_0147_PAIRS) | set(_NO_ORM_MODEL_0147_PAIRS)

    stale = sorted(justified - pairs)
    assert not stale, (
        "_RENAMED_0147_PAIRS/_DROPPED_0147_PAIRS/_NO_ORM_MODEL_0147_PAIRS name pairs 0147 "
        f"does not convert (a stale exception would hide whatever it still claims): {stale}"
    )

    unmapped_absent = sorted(
        pair for pair in pairs if pair not in justified and _resolved_pair(pair)[0] not in Base.metadata.tables
    )
    assert not unmapped_absent, (
        "0147 pairs whose table is absent from Base.metadata and are not justified in "
        f"_RENAMED_0147_PAIRS/_DROPPED_0147_PAIRS/_NO_ORM_MODEL_0147_PAIRS: {unmapped_absent}"
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


def _jsonb_construction_lines(source: str, filename: str = "<unknown>") -> list[int]:
    """Line numbers of every ``JSONB(...)`` Call in *source* (AST, not text).

    AST rather than a regex: prose that merely mentions ``JSONB (`` must not
    register, while every call shape — bare ``JSONB()``,
    ``postgresql.JSONB()``, ``JSON().with_variant(JSONB(), ...)`` — must.
    """
    tree = ast.parse(source, filename=filename)
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
    for path, source in iter_source_text(_SRC):
        if _MIGRATIONS_DIR in path.parents:
            continue
        lines = _jsonb_construction_lines(source, str(path))
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
# FAR-1941 extends the dialect-parity check beyond 0147 to every migration whose
# jsonb declaration the extractor can PARSE, with the pair set read from the
# migrations' own source. Supported declaration idioms:
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
#   * an ``op.execute(f "...")`` inside a ``for <col> in <CONSTANT>`` loop whose
#     iterable is a module-level list/tuple of strings — rendered once per
#     element (0226's ``_columns``, 0235's ``COLUMNS``). Any OTHER interpolation
#     (a function parameter, a call, an attribute) is left unresolved.
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
# The raw-SQL path only inspects strings actually handed to an executor
# (``op.execute`` / ``<bind>.execute`` / a migration-local ``_execute`` wrapper)
# AND reachable by the renderer at that call site — a literal, an f-string, a
# ``+``-concatenation or a module-level scalar-string constant. A declaration
# passed through a function-local variable, a call-wrapped constant or a
# subscript is not seen here at all (see the boundary note in this module's
# docstring). Prose in a docstring or comment is never a declaration, however
# jsonb-shaped it looks (0129's docstring shows an ``ALTER ... TYPE jsonb``).
#
# NOT parsed, and no longer claimed to be: an interpolation the loop resolver
# cannot bind. 0147's ``_add_temp_column`` builds ``... ADD COLUMN "{tmp}"
# {cast}`` from a runtime variable, and its table/column also arrive by
# parameter; 0129 has the identical helper. Where such a shape IS rendered (the
# f-string at the execute call site), the declaration net
# (``_executed_sql_jsonb_declarations`` + ``_UNPARSED_JSONB_DDL`` below) makes it
# either parse to a pair or be named there with a reason, so it does not pass
# silently. Where it is NOT rendered — the argument is a function-local
# variable, a call-wrapped constant or a subscript — the net produces nothing
# for it and cannot see it; that class is closed by the live-database oracle in
# ``tests/integration/test_initial_migration.py``, not by this file (see the
# module docstring).
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

# --- declaration SHAPES (the net's detection vocabulary) ---------------------
# A shape replaces every interpolation with ``_INTERP``. Detection is
# deliberately BROADER than parsing: a column whose TYPE is the literal
# ``jsonb`` token OR an unresolved interpolation is a possible jsonb
# declaration, because the interpolation could be jsonb at runtime. Detecting
# the runtime-variable form is what makes 0147's ``ADD COLUMN "{tmp}" {cast}``
# loud rather than a silent gap; the cost is that a non-jsonb dynamic column
# would need one explicit justification, which is the intended trade.
_SHAPE_IDENTIFIER = rf'(?:{_INTERP}|"[A-Za-z0-9_]+"|[A-Za-z0-9_]+)'
_ADD_COLUMN_JSONB_SHAPE_RE = re.compile(
    rf"\bADD\s+COLUMN(?:\s+IF\s+NOT\s+EXISTS)?\s+{_SHAPE_IDENTIFIER}\s+(?:jsonb|{_INTERP})\b",
    re.IGNORECASE,
)
_ALTER_COLUMN_TYPE_JSONB_SHAPE_RE = re.compile(
    rf"\bALTER\s+COLUMN\s+{_SHAPE_IDENTIFIER}\s+TYPE\s+(?:jsonb|{_INTERP})\b",
    re.IGNORECASE,
)
_ANY_CREATE_TABLE_RE = re.compile(r"\bCREATE\s+TABLE(?:\s+IF\s+NOT\s+EXISTS)?\b", re.IGNORECASE)
_COLUMN_JSONB_SHAPE_RE = re.compile(rf"^\s*{_SHAPE_IDENTIFIER}\s+(?:jsonb|{_INTERP})\b", re.IGNORECASE)

#: Executed-SQL jsonb declarations the extractor CANNOT parse, keyed by
#: ``(migration_stem, declaration_shape)`` and justified per entry — the direct
#: analogue of ``_RENAMED_0147_PAIRS`` / ``_DROPPED_0147_PAIRS`` above. The
#: declaration net requires every detected declaration to parse to a pair OR to
#: appear here with its reason, so a NEW unparsed declaration FAILS the gate
#: instead of slipping through. Staleness is enforced too: an entry whose shape
#: no longer appears (or that now parses) fails
#: ``test_unparsed_jsonb_ddl_exceptions_are_live``, so the map cannot rot into a
#: blanket skip.
_UNPARSED_JSONB_DDL: dict[tuple[str, str], str] = {
    # Both migrations share the same ``_add_temp_column`` helper: the temp
    # column's TYPE is the runtime ``cast`` argument ("jsonb" on upgrade,
    # "json" on downgrade), so the string alone cannot declare a pair. The jsonb
    # pairs are enumerated declaratively — 0129's ``_JSON_COLUMNS`` and 0147's
    # ``_JSON_TO_JSONB`` — and parsed from THERE, which is why the extractor's
    # pair set already contains them.
    ("0129_runs_json_to_jsonb", 'ALTER TABLE public."__INTERP__" ADD COLUMN "__INTERP__" __INTERP__'): (
        "type is the runtime `cast` argument (jsonb on upgrade / json on downgrade); the "
        "pairs are declared in `_JSON_COLUMNS` and parsed from there"
    ),
    ("0147_json_to_jsonb_standardize", 'ALTER TABLE public."__INTERP__" ADD COLUMN "__INTERP__" __INTERP__'): (
        "same `_add_temp_column` shape as 0129 — type is the runtime `cast`; the pairs are "
        "declared in `_JSON_TO_JSONB` and parsed from there"
    ),
}


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


def _shape_declares_jsonb(shape: str) -> bool:
    """True iff a declaration SHAPE declares a jsonb (or possible-jsonb) column.

    Broader than ``_raw_sql_jsonb_pairs``: the TYPE may be an unresolved
    interpolation, which could be ``jsonb`` at runtime. Shares the body/quote
    parsing with the parser so a single-line ``CREATE TABLE`` and a table-level
    constraint are handled identically.
    """
    if _ADD_COLUMN_JSONB_SHAPE_RE.search(shape):
        return True
    if _ALTER_COLUMN_TYPE_JSONB_SHAPE_RE.search(shape):
        return True
    for match in _ANY_CREATE_TABLE_RE.finditer(shape):
        open_index = shape.find("(", match.end())
        if open_index == -1:
            continue
        body = _create_table_body(shape, open_index)
        if body is None:
            continue
        if any(_COLUMN_JSONB_SHAPE_RE.match(definition) for definition in _split_top_level_commas(body)):
            return True
    return False


def _executed_sql_jsonb_declarations(source: str) -> list[tuple[str, set[tuple[str, str]]]]:
    """Every jsonb column declaration inside an executed SQL string in *source*.

    Each item is ``(shape, pairs)``: the declaration's SQL shape (interpolations
    rendered as ``_INTERP``) and the ``(table, column)`` pairs parsed from that
    SAME declaration. An EMPTY *pairs* set means the extractor could not resolve
    it — exactly the declaration the net requires be made parseable or recorded
    in ``_UNPARSED_JSONB_DDL``.
    """
    tree = ast.parse(source)
    module_values = _module_string_expressions(tree)
    bindings = _loop_sequence_bindings(tree, _module_string_sequence_constants(tree))

    declarations: list[tuple[str, set[tuple[str, str]]]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not _is_execute_call(node):
            continue
        env = bindings.get(id(node), {})
        for argument in node.args:
            for shape, concretes in _render_sql_renderings(argument, module_values, env):
                if not _shape_declares_jsonb(shape):
                    continue
                pairs: set[tuple[str, str]] = set()
                for concrete in concretes:
                    pairs |= _raw_sql_jsonb_pairs(concrete)
                declarations.append((shape, pairs))
    return declarations


def _unexplained_executed_jsonb_declarations(stem: str, source: str) -> list[str]:
    """Declarations in *source* that parse to no pair and are not justified."""
    return [
        f"{stem}: {shape}"
        for shape, pairs in _executed_sql_jsonb_declarations(source)
        if not pairs and (stem, shape) not in _UNPARSED_JSONB_DDL
    ]


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
    inventory), and covers every declaration the extractor can PARSE — see the
    module docstring and the FAR-1941 extractor header for the exact idioms.
    ``test_every_executed_jsonb_declaration_is_parsed_or_justified`` is what
    guarantees a declaration the extractor CANNOT parse is not silently outside
    this set. A declared pair whose table or column no longer exists in
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


def test_interpolated_loop_ddl_is_resolved_not_excused() -> None:
    """0226/0235's ``op.execute(f "...")`` loop DDL is genuinely PARSED.

    The extractor used to skip f-string-interpolated DDL entirely, so these ten
    columns were covered only by accident (0147 happens to declare each of them
    too). The simple ``for <col> in <CONSTANT>`` case is now rendered once per
    element, so the pair is attributed to the migration that declares it — and
    neither migration needs an ``_UNPARSED_JSONB_DDL`` exception.
    """
    declared = _migration_declared_jsonb_pairs()
    expected = {
        "0226_agents_json_to_jsonb": (
            "agents",
            (
                "agent_commands",
                "prompt_version_history",
                "connector_type_refs",
                "required_environment_capabilities",
                "evals",
                "retry_policy",
            ),
        ),
        "0235_promote_organisations_json_to_jsonb": (
            "organisations",
            ("settings_json", "otel_config_json", "export_bundle_json", "guardrail_pins_json"),
        ),
    }
    for migration, (table, columns) in expected.items():
        for column in columns:
            assert migration in declared[(table, column)]
        assert not any(stem == migration for stem, _shape in _UNPARSED_JSONB_DDL), (
            f"{migration}'s interpolated DDL must be PARSED, not excused in _UNPARSED_JSONB_DDL"
        )


def test_every_executed_jsonb_declaration_is_parsed_or_justified() -> None:
    """Every RENDERED executed-SQL jsonb declaration parses or is justified.

    Best-effort, not exhaustive. This net sees only declarations the renderer
    can reach at the execute call site — a string literal, an f-string, a ``+``
    concatenation or a module-level scalar-string constant. Each such
    declaration that does not resolve to a ``(table, column)`` pair — including
    one whose TYPE is an interpolation which could be jsonb — must be named in
    ``_UNPARSED_JSONB_DDL`` with a reason, or it fails HERE, naming it, rather
    than passing quietly while
    ``test_every_migration_declared_jsonb_column_is_jsonb_on_postgres`` reads as
    green. A declaration reached through a function-local variable, a
    call-wrapped constant or a subscript produces no rendering and is therefore
    NOT seen by this test; that class is closed by the live-database oracle in
    ``tests/integration/test_initial_migration.py`` (see the module docstring),
    not here.
    """
    unexplained: list[str] = []
    for path in sorted(_MIGRATION_VERSIONS_DIR.glob("*.py")):
        if path.name.startswith("__"):
            continue
        unexplained.extend(_unexplained_executed_jsonb_declarations(path.stem, path.read_text(encoding="utf-8")))
    assert not unexplained, (
        "executed-SQL jsonb declaration(s) that neither parse to a (table, column) pair nor are "
        "justified in _UNPARSED_JSONB_DDL — make them parseable or record the reason:\n  " + "\n  ".join(unexplained)
    )


def test_unparsed_jsonb_ddl_exceptions_are_live() -> None:
    """Every ``_UNPARSED_JSONB_DDL`` entry still corresponds to a real gap.

    Mirrors ``test_0147_pair_resolution_is_complete_and_justified``: an exception
    whose declaration no longer appears (or that now parses) is stale and would
    hide whatever it still claims, so it fails rather than lingering.
    """
    detected: set[tuple[str, str]] = set()
    for path in sorted(_MIGRATION_VERSIONS_DIR.glob("*.py")):
        if path.name.startswith("__"):
            continue
        for shape, pairs in _executed_sql_jsonb_declarations(path.read_text(encoding="utf-8")):
            if not pairs:
                detected.add((path.stem, shape))
    stale = sorted(set(_UNPARSED_JSONB_DDL) - detected)
    assert not stale, (
        "_UNPARSED_JSONB_DDL justifies declarations that no longer need it (a stale exception "
        f"would hide whatever it still claims): {stale}"
    )


def test_unparsed_interpolated_jsonb_declaration_is_never_silently_skipped() -> None:
    """An unresolvable interpolation is DETECTED and fails, not skipped.

    The loop resolver binds only ``for <col> in <CONSTANT>``. An iterable it
    cannot bind (here a call) leaves the DDL unparsed — exactly the shape the
    reported gap shipped. The net must therefore SEE the declaration and report
    it as unexplained; otherwise a future migration could add a jsonb column the
    PR-time gate ignores.
    """
    source = """
from alembic import op


def upgrade() -> None:
    for column in columns_for("agents"):
        op.execute(f"ALTER TABLE agents ALTER COLUMN {column} TYPE jsonb USING {column}::jsonb")
"""
    assert not _declared_jsonb_pairs(source), "the extractor must NOT be able to parse this"
    declarations = _executed_sql_jsonb_declarations(source)
    unexplained = [shape for shape, pairs in declarations if not pairs]
    assert "ALTER TABLE agents ALTER COLUMN __INTERP__ TYPE jsonb USING __INTERP__::jsonb" in unexplained
    assert _unexplained_executed_jsonb_declarations("synthetic", source), "the net must fail loudly"


def test_dynamic_type_declaration_is_detected_and_its_exception_suppresses_it() -> None:
    """0129's runtime-variable ADD COLUMN is DETECTED, then justified — not skipped.

    Its ``_add_temp_column`` helper adds a temp column whose TYPE is the
    ``cast`` argument, so the string alone parses to no pair. The net flags it;
    the ``_UNPARSED_JSONB_DDL`` entry (with its reason) is what turns that into a
    justified gap instead of a silent one.
    """
    source = (_MIGRATION_VERSIONS_DIR / "0129_runs_json_to_jsonb.py").read_text(encoding="utf-8")
    unparsed = [shape for shape, pairs in _executed_sql_jsonb_declarations(source) if not pairs]
    expected_shape = 'ALTER TABLE public."__INTERP__" ADD COLUMN "__INTERP__" __INTERP__'
    assert expected_shape in unparsed, "the runtime-type declaration must be DETECTED"
    assert ("0129_runs_json_to_jsonb", expected_shape) in _UNPARSED_JSONB_DDL
    assert not _unexplained_executed_jsonb_declarations("0129_runs_json_to_jsonb", source)


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
