"""Internal module — conformance registry shared by conftest.py and test modules.

This lives in the same package as the test files so it can be imported
unambiguously regardless of pytest's conftest handling.
"""

from typing import Any

# ── Registry ────────────────────────────────────────────────────────────────

_CONFORMANCE_REGISTRY: dict[str, str] = {}
"""Maps connector type name → pytest fixture name."""


def register_conformance_connector(name: str, fixture_name: str) -> None:
    """Register *fixture_name* as the provider for connector *name*.

    Must be called at module level in a connector-specific test module.
    """
    _CONFORMANCE_REGISTRY[name] = fixture_name


def get_registered_types() -> list[str]:
    return sorted(_CONFORMANCE_REGISTRY)


def get_registered_fixture(name: str) -> str | None:
    return _CONFORMANCE_REGISTRY.get(name)


# ── Helper assertions for conformance scenarios ─────────────────────────────


def assert_result_shape(result: Any) -> None:
    from modulo.connectors.base import ConnectorResult

    assert isinstance(result, ConnectorResult), f"Expected ConnectorResult, got {type(result).__name__}"
    assert isinstance(result.records, list), f"ConnectorResult.records must be a list, got {type(result.records)}"
    assert result.next_cursor is None or isinstance(result.next_cursor, str)
    assert result.total is None or isinstance(result.total, int)


def assert_health_shape(result: Any) -> None:
    from modulo.connectors.base import HealthResult

    assert isinstance(result, HealthResult), f"Expected HealthResult, got {type(result).__name__}"
    assert isinstance(result.ok, bool)
    assert isinstance(result.detail, str)
