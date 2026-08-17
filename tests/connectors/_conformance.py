"""Internal module — conformance registry shared by conftest.py and test modules.

This lives in the same package as the test files so it can be imported
unambiguously regardless of pytest's conftest handling.

NOTE: This module has a sibling copy for standalone root-level test collection
(under tests/connectors/ and backend/tests/connectors/). Keep all copies of this
file byte-identical.
"""

import json
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


def _assert_json_serializable(value: Any, label: str) -> None:
    """Assert *value* survives a ``json.dumps``/``json.loads`` round-trip unchanged."""
    try:
        restored = json.loads(json.dumps(value))
    except (TypeError, ValueError) as exc:
        raise AssertionError(f"{label} is not JSON-serializable: {exc}") from exc
    if restored != value:
        raise AssertionError(
            f"{label} does not survive a JSON round-trip unchanged: got {restored!r}, expected {value!r}"
        )


def assert_result_shape(result: Any) -> None:
    from modulo.connectors.base import ConnectorResult

    assert isinstance(result, ConnectorResult), f"Expected ConnectorResult, got {type(result).__name__}"
    assert isinstance(result.records, list), f"ConnectorResult.records must be a list, got {type(result.records)}"
    assert all(isinstance(r, dict) for r in result.records), "ConnectorResult.records must contain only dicts"
    assert result.next_cursor is None or isinstance(result.next_cursor, str)
    assert result.total is None or isinstance(result.total, int)
    if result.total is not None:
        assert result.total >= 0, f"ConnectorResult.total must be non-negative, got {result.total}"
    assert isinstance(result.metadata, dict), (
        f"ConnectorResult.metadata must be a dict, got {type(result.metadata).__name__}"
    )
    # Records feed into JMESPath evaluation, JSON API responses, and LangGraph
    # state — non-serializable values would only fail at runtime, not in tests.
    _assert_json_serializable(result.records, "ConnectorResult.records")
    _assert_json_serializable(result.metadata, "ConnectorResult.metadata")


def assert_write_result_shape(result: Any) -> None:
    """Assert a connector ``write()`` result is a JSON-serializable dict."""
    assert isinstance(result, dict), f"Expected dict from write(), got {type(result).__name__}"
    _assert_json_serializable(result, "write() result")


def assert_health_shape(result: Any) -> None:
    from modulo.connectors.base import HealthResult

    assert isinstance(result, HealthResult), f"Expected HealthResult, got {type(result).__name__}"
    assert isinstance(result.ok, bool)
    assert isinstance(result.detail, str)
    if not result.ok:
        assert result.detail, "HealthResult with ok=False must provide a non-empty detail"
