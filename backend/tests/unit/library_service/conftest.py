"""Package-wide fixtures for library_service unit tests.

The community catalog caches (``_COMMUNITY_PRIMITIVES`` and its id/slug
indexes) are module-level mutable state. Two test modules each carried their
own copy of this reset fixture; it is defined once here so every test in the
package starts from the seed baseline.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from modulo.core.library_service import (
    _COMMUNITY_BY_ID,
    _COMMUNITY_BY_SLUG,
    _COMMUNITY_PRIMITIVES,
)

_PRIMITIVES_BASELINE = tuple(_COMMUNITY_PRIMITIVES)
_BY_ID_BASELINE = dict(_COMMUNITY_BY_ID)
_BY_SLUG_BASELINE = dict(_COMMUNITY_BY_SLUG)


def _restore_community_cache() -> None:
    _COMMUNITY_PRIMITIVES[:] = _PRIMITIVES_BASELINE
    _COMMUNITY_BY_ID.clear()
    _COMMUNITY_BY_ID.update(_BY_ID_BASELINE)
    _COMMUNITY_BY_SLUG.clear()
    _COMMUNITY_BY_SLUG.update(_BY_SLUG_BASELINE)


@pytest.fixture(autouse=True)
def isolate_community_cache() -> Iterator[None]:
    """Reset the in-memory community catalog before and after every test."""
    _restore_community_cache()
    try:
        yield
    finally:
        _restore_community_cache()
