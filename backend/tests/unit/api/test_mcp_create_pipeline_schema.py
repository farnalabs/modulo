"""Pin the public MCP schema of the ``create_pipeline`` tool.

An MCP tool's Python signature *is* its advertised JSON schema: FastMCP derives
``inputSchema`` from the function's parameters. So a signature edit made for an
internal reason (a lint threshold, a refactor) is a client-visible contract
change, not an implementation detail.

FAR-1883 did exactly that: to clear ``python:S107`` it grouped the two nullable
time-window overrides into a ``timeouts`` object, which removed a previously
flat, documented argument from the schema and left the grouping inconsistent
with the sibling ``lock_wait_timeout_seconds`` / ``node_timeout_seconds``
arguments. These tests pin the flat shape so a future lint-driven reshape fails
here instead of silently changing what clients see.

The Sonar exclusion that lets the 14-argument signature stand is documented in
``sonar-project.properties`` (rule ``python:S107``, this file's module only).
"""

from __future__ import annotations

from typing import Any

# The complete advertised parameter set. Update this list deliberately (with a
# client-visible-contract reason), never to make a refactor pass.
_EXPECTED_PARAMS = {
    "name",
    "description",
    "visibility",
    "max_concurrent_runs",
    "lock_wait_timeout_seconds",
    "node_timeout_seconds",
    "hitl_review_window_seconds",
    "queue_timeout_seconds",
    "default_autonomy_level",
    "max_autonomy_level",
    "folder_id",
    "circuit_breaker_threshold",
    "business_owner_id",
    "reliability_owner_id",
}


def _create_pipeline_schema() -> dict[str, Any]:
    from modulo.api.mcp_server import mcp

    return dict(mcp._tool_manager._tools["create_pipeline"].parameters)  # type: ignore[attr-defined]


def test_create_pipeline_param_names_pinned() -> None:
    """The flat parameter set is the tool contract; nothing may be added,
    removed, or renamed without updating the pin deliberately."""
    properties = _create_pipeline_schema()["properties"]
    assert set(properties) == _EXPECTED_PARAMS


def test_create_pipeline_timeouts_are_flat_not_nested() -> None:
    """Both time-window overrides are top-level arguments.

    A nested ``timeouts`` object was shipped for one day in FAR-1883; its
    absence here (and the presence of the two flat keys) is the regression
    guard against that lint-driven reshape returning.
    """
    properties = _create_pipeline_schema()["properties"]
    assert "timeouts" not in properties
    assert "hitl_review_window_seconds" in properties
    assert "queue_timeout_seconds" in properties


def test_create_pipeline_timeout_params_shape() -> None:
    """Each timeout override is an optional integer defaulting to null."""
    properties = _create_pipeline_schema()["properties"]
    expected = {
        "anyOf": [{"type": "integer"}, {"type": "null"}],
        "default": None,
    }
    hitl = properties["hitl_review_window_seconds"]
    queue = properties["queue_timeout_seconds"]
    assert hitl["anyOf"] == expected["anyOf"]
    assert hitl["default"] is None
    assert queue["anyOf"] == expected["anyOf"]
    assert queue["default"] is None


def test_create_pipeline_required_is_name_only() -> None:
    """Every other argument stays optional, so the flat signature does not
    make existing ``create_pipeline(name=...)`` calls require more."""
    schema = _create_pipeline_schema()
    assert schema["required"] == ["name"]
    assert schema["type"] == "object"
