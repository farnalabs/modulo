"""FAR-2246: connector_type_refs entries are read the same way everywhere."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.api.routes.library import _resolve_bundle_connectors
from modulo.core.connector_refs import connector_ref_type_id
from modulo.core.workflow_import_export import _build_v2_requires


@pytest.mark.parametrize(
    ("ref", "expected"),
    [
        ("github", "github"),
        ({"connector_type_id": "slack"}, "slack"),
        ({"connector_type": "github", "capabilities": ["read"]}, "github"),
        ({"type": "fs"}, "fs"),
        ({"connector_type_id": "a", "connector_type": "b", "type": "c"}, "a"),
        ({"connector_type": None}, ""),
        ({}, ""),
        (None, ""),
        (5, ""),
    ],
    ids=[
        "plain-string",
        "connector-type-id-key",
        "connector-type-key-with-capabilities",
        "legacy-type-key",
        "connector-type-id-wins",
        "connector-type-none",
        "empty-dict",
        "none",
        "non-string",
    ],
)
def test_connector_ref_type_id(ref: Any, expected: str) -> None:
    assert connector_ref_type_id(ref) == expected


def test_build_v2_requires_reads_connector_type_key() -> None:
    agents = [{"connector_type_refs": [{"connector_type": "github", "capabilities": []}]}]
    assert _build_v2_requires(agents, [])["connector_types"] == ["github"]


@pytest.mark.asyncio
async def test_resolve_bundle_connectors_reads_connector_type_key() -> None:
    bundle = {"agents": [{"connector_type_refs": [{"connector_type": "github", "capabilities": []}]}]}
    resolved: list[dict[str, Any]] = []
    with patch(
        "modulo.api.routes.library.resolve_connector_type",
        new=AsyncMock(return_value={"instance_id": "inst-1"}),
    ) as mock_resolve:
        mapping = await _resolve_bundle_connectors(
            MagicMock(), MagicMock(organisation_id="org-1"), bundle, resolved, []
        )
    assert mapping == {"github": "inst-1"}
    mock_resolve.assert_awaited_once()
