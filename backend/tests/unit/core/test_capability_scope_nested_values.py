"""Focused coverage for the payload guard's plain-object backstop arm.

:func:`_iter_nested_values` descends into mapping values, list/tuple elements,
dataclass fields, and — as a backstop — an arbitrary non-primitive object's
``__dict__`` values. The backstop is what lets a connector/secret object
smuggled inside a plain wrapper class (not a dataclass) still be found; these
tests pin that arm directly so a decomposition refactor cannot silently drop
it.
"""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from modulo.core.capability_scope import (
    ScopeViolationError,
    _iter_nested_values,
    assert_no_secret_objects,
)


@dataclasses.dataclass
class _DataclassWrapper:
    """Dataclass holder — exercises the ``dataclasses.fields`` arm."""

    payload: Any


class _PlainWrapper:
    """A non-dataclass object with attributes — the ``__dict__`` backstop arm."""

    def __init__(self, **attrs: Any) -> None:
        for key, value in attrs.items():
            setattr(self, key, value)


class _SmuggledConnector:
    """Duck-typed connector object (``connector_type`` + callable ``query``)."""

    connector_type = "github"

    def query(self, *_args: Any, **_kwargs: Any) -> None:
        return None


def test_dataclass_fields_are_offered_for_recursion() -> None:
    """A dataclass yields its field values (the arm ConnectorResult rides)."""
    values = _iter_nested_values(_DataclassWrapper(payload=[{"ok": True}]))
    assert values is not None
    assert list(values) == [[{"ok": True}]]


def test_plain_object_dict_values_are_offered_for_recursion() -> None:
    """A non-dataclass object with an attribute bag must yield its attribute
    values, not be treated as a leaf."""
    wrapper = _PlainWrapper(one=1, two="deux")
    values = _iter_nested_values(wrapper)
    assert values is not None
    assert set(values) == {1, "deux"}


def test_guard_descends_into_plain_object_attributes_and_rejects() -> None:
    """A connector object riding in a plain wrapper's attribute bag must be
    rejected — this is exactly the arm that would otherwise let it slip."""
    with pytest.raises(ScopeViolationError) as exc_info:
        assert_no_secret_objects(_PlainWrapper(payload=_SmuggledConnector()), node_id="n1")
    assert exc_info.value.kind == "secret"


def test_guard_allows_plain_object_with_serializable_attributes() -> None:
    """The backstop must not reject an ordinary object of plain data."""
    assert assert_no_secret_objects(_PlainWrapper(one=1, two=[{"ok": True}]), node_id="n1") is None


def test_primitive_leaf_has_no_nested_values() -> None:
    """Primitives are leaves (the backstop skips them) — a regression here
    would recurse into ``str``/``int`` internals."""
    assert _iter_nested_values("plain") is None
    assert _iter_nested_values(7) is None
