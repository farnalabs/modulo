"""Python and SQL must agree on the autonomy rank ordering (one source of truth).

The ``max_autonomy_level >= default_autonomy_level`` invariant is encoded in
FIVE independently-edited places:

* ``core.run_context.autonomy._LEVEL_RANK`` (the Python side - every
  min/max/comparison in the app goes through it, never through StrEnum string
  ordering),
* migration ``0264``'s CHECK + its behaviour-preserving repair (``pipelines``),
* migration ``0270``'s CHECK + its behaviour-preserving repair
  (``pipeline_snapshots``),
* both ORM ``CheckConstraint``s (``ck_pipelines_max_autonomy_ge_default`` and
  ``ck_pipeline_snapshots_max_autonomy_ge_default``).

The existing migration/model tests prove that each SQL copy is byte-identical
to its MODEL sibling and that every vocabulary value has an arm - but nothing
pins the SQL ranks against the PYTHON ranks. A one-sided swap in SQL (say
``'fully_autonomous' THEN 0``) would therefore keep every one of those gates
green while the DB and the app disagree about which level is higher: the CHECK
would accept rows the app clamps, and reject rows it allows.

This module closes that gap: every SQL CASE map is parsed out of its source and
asserted EQUAL to the map ``autonomy_level_rank`` returns - same keys, same
ranks - so any divergence fails loudly here.

Deliberately cheap and deterministic: pure text parsing of already-loaded
modules, no database, no clock.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from types import ModuleType

import pytest
from sqlalchemy import CheckConstraint

from modulo.core.run_context.autonomy import AUTONOMY_LEVEL_VALUES, AutonomyLevel, autonomy_level_rank
from modulo.db.models.pipeline import Pipeline
from modulo.db.models.pipeline_snapshot import PipelineSnapshot

_M0264 = "0264_pipelines_max_autonomy_ge_default"
_M0270 = "0270_pipeline_snapshots_max_autonomy_ge_default"

_VERSIONS = Path(__file__).resolve().parents[3] / "src" / "modulo" / "db" / "migrations" / "versions"

#: A ``CASE <column> WHEN '<level>' THEN <rank> ... END`` block. Both the CHECK
#: predicate and the repair's WHERE spell the rank map out inline (never
#: interpolated - the S608 f-string-SQL rule applies to that directory), so a
#: regex over the literal SQL text is authoritative. The column name is
#: captured so a test can assert that BOTH sides of the ``<=``/``>`` comparison
#: were checked, not just whichever one the parser happened to match first.
_CASE_BLOCK = re.compile(r"CASE\s+(\w+)\s+((?:WHEN\s+'[^']+'\s+THEN\s+-?\d+\s*)+)END", re.IGNORECASE)
_WHEN_ARM = re.compile(r"WHEN\s+'([^']+)'\s+THEN\s+(-?\d+)")
#: Both comparisons rank a default side against a ceiling side.
_EXPECTED_CASE_COLUMNS = frozenset({"default_autonomy_level", "max_autonomy_level"})

#: ``(id, sql)`` for every place the autonomy rank ordering is written down as
#: executable SQL (built lazily so an import failure names the source).
_SOURCES: tuple[tuple[str, str], ...] = (
    ("orm:ck_pipelines_max_autonomy_ge_default", "ck_pipelines_max_autonomy_ge_default"),
    ("orm:ck_pipeline_snapshots_max_autonomy_ge_default", "ck_pipeline_snapshots_max_autonomy_ge_default"),
    (f"{_M0264}:REPAIR_INVERTED_DEFAULTS", "REPAIR_INVERTED_DEFAULTS"),
    (f"{_M0264}:_ADD_CEILING_GE_DEFAULT_CHECK_NOT_VALID", "_ADD_CEILING_GE_DEFAULT_CHECK_NOT_VALID"),
    (f"{_M0270}:REPAIR_INVERTED_DEFAULTS", "REPAIR_INVERTED_DEFAULTS"),
    (f"{_M0270}:_ADD_CEILING_GE_DEFAULT_CHECK_NOT_VALID", "_ADD_CEILING_GE_DEFAULT_CHECK_NOT_VALID"),
)


def _load_migration(revision: str) -> ModuleType:
    path = _VERSIONS / f"{revision}.py"
    assert path.exists(), f"Migration file missing: {path}"
    spec = importlib.util.spec_from_file_location(f"migration_{revision}", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _source_sql(source_id: str, attr: str) -> str:
    if source_id.startswith("orm:"):
        model = Pipeline if source_id == "orm:ck_pipelines_max_autonomy_ge_default" else PipelineSnapshot
        for constraint in model.__table__.constraints:
            if isinstance(constraint, CheckConstraint) and constraint.name == attr:
                return str(constraint.sqltext)
        raise AssertionError(f"{model.__name__} declares no CHECK constraint named {attr!r}")
    revision = source_id.split(":", 1)[0]
    return str(getattr(_load_migration(revision), attr))


def _expected_rank_map() -> dict[str, int]:
    """The Python truth: ``_LEVEL_RANK`` reached through its public accessor.

    Iterating ``AutonomyLevel`` (not a hard-coded list) means a level added to
    the enum but forgotten in ``_LEVEL_RANK`` fails here with a ``KeyError``
    rather than silently defaulting.
    """
    return {level.value: autonomy_level_rank(level) for level in AutonomyLevel}


def _sql_rank_maps(sql: str) -> list[tuple[str, dict[str, int]]]:
    """Every ``(column, rank map)`` CASE block in *sql*, in order of appearance."""
    return [
        (column, {level: int(rank) for level, rank in _WHEN_ARM.findall(block)})
        for column, block in _CASE_BLOCK.findall(sql)
    ]


def test_every_declared_level_has_a_rank() -> None:
    """``_LEVEL_RANK`` is total over the declared vocabulary (no KeyError)."""
    ranked = _expected_rank_map()
    assert sorted(ranked) == sorted(AUTONOMY_LEVEL_VALUES)
    assert ranked["manual_approval"] < ranked["notify_on_complete"]
    assert ranked["notify_on_complete"] < ranked["fully_autonomous"]


@pytest.mark.parametrize(
    ("source_id", "attr"),
    _SOURCES,
    ids=[source_id for source_id, _attr in _SOURCES],
)
def test_sql_case_maps_match_the_python_rank(source_id: str, attr: str) -> None:
    """Every SQL CASE rank map is EXACTLY ``_LEVEL_RANK`` - keys and ranks.

    The column assertion guards the PARSER, not the SQL: if the predicate were
    ever rewritten so the regex matched only one side of the comparison, the
    rank check would silently cover half the invariant.
    """
    maps = _sql_rank_maps(_source_sql(source_id, attr))
    assert len(maps) >= 2, (
        f"{source_id}.{attr} yielded {len(maps)} CASE rank map(s) - the parser found nothing to check"
    )
    columns = {column for column, _found in maps}
    assert columns == set(_EXPECTED_CASE_COLUMNS), (
        f"{source_id}.{attr} ranks columns {columns!r}, expected {_EXPECTED_CASE_COLUMNS!r}"
    )
    expected = _expected_rank_map()
    for index, (_column, found) in enumerate(maps):
        assert found == expected, (
            f"{source_id}.{attr} CASE #{index} ranks {found!r} but _LEVEL_RANK ranks {expected!r} - "
            "the DB and the app disagree about autonomy ordering"
        )
