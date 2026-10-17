"""The shared list-condition chain must stay complete and ordered.

``list_library_primitives`` builds its WHERE predicates once, in
``_build_list_conditions``, and reuses that single list for the cursor page,
the offset row page and the COUNT. These tests pin the chain itself, so a
future filter cannot be dropped from one path (or from the COUNT) without a
failure — the class of regression that made ``total`` describe a different
universe from the rows returned (FAR-1897).

The helper is pure: each predicate is compiled to SQL with literal binds, so
no database is needed.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from modulo.db.crud.library_primitive import _build_list_conditions
from modulo.db.models.library_primitive import LibraryPrimitive

_ORG = uuid.UUID("00000000-0000-0000-0000-0000000000aa")


def _compile(**filters: Any) -> str:
    conditions = _build_list_conditions(**filters)
    stmt = select(LibraryPrimitive).where(*conditions)
    compiled = stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    return str(compiled)


def test_build_list_conditions_emits_every_filter_in_chain_order() -> None:
    sql = _compile(
        org_id=_ORG,
        primitive_type="ignored-when-a-list-is-given",
        primitive_types=["agent", "schema"],
        search="  Remy  ",
        source="local",
        excluded_tiers=["in_dev"],
    )

    assert sql.index("library_primitives.deleted_at IS NULL") < sql.index("library_primitives.organisation_id =")
    assert sql.index("library_primitives.organisation_id =") < sql.index("library_primitives.primitive_type IN")
    assert sql.index("library_primitives.primitive_type IN") < sql.index("library_primitives.name ILIKE")
    assert sql.index("library_primitives.name ILIKE") < sql.index("library_primitives.source =")
    assert sql.index("library_primitives.source =") < sql.index("library_primitives.tier NOT IN")

    assert "library_primitives.primitive_type IN ('agent', 'schema')" in sql
    # ``literal_binds`` renders the bound ``%Remy%`` through the DBAPI escape,
    # which doubles the wildcards; the value actually bound is ``%Remy%``.
    assert "library_primitives.name ILIKE '%%Remy%%'" in sql
    assert "library_primitives.source = 'local'" in sql
    assert "library_primitives.tier NOT IN ('in_dev')" in sql


def test_build_list_conditions_defaults_to_excluding_in_dev_tier() -> None:
    conditions = _build_list_conditions(
        org_id=None,
        primitive_type=None,
        primitive_types=None,
        search=None,
        source=None,
        excluded_tiers=None,
    )

    assert len(conditions) == 2
    assert "library_primitives.tier NOT IN ('in_dev')" in _compile(
        org_id=None,
        primitive_type=None,
        primitive_types=None,
        search=None,
        source=None,
        excluded_tiers=None,
    )


def test_build_list_conditions_treats_an_empty_excluded_tiers_list_as_no_filter() -> None:
    # ``None`` means "use the in_dev default"; an explicitly empty list means
    # "exclude nothing". Collapsing the two with a falsy check would silently
    # start excluding in_dev again.
    sql = _compile(
        org_id=None,
        primitive_type=None,
        primitive_types=None,
        search=None,
        source=None,
        excluded_tiers=[],
    )

    assert "library_primitives.tier NOT IN" not in sql


def test_build_list_conditions_prefers_the_types_list_over_the_scalar_type() -> None:
    sql = _compile(
        org_id=None,
        primitive_type="agent",
        primitive_types=["schema"],
        search=None,
        source=None,
        excluded_tiers=None,
    )

    assert "library_primitives.primitive_type IN ('schema')" in sql
    assert "library_primitives.primitive_type = 'agent'" not in sql
