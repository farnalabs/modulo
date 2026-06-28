"""Integration tests for pre-run graph validation blocking.

Asserts GraphValidator.validate_for_run() blocks on real DB-backed snapshots.
"""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.core.graph_validator import GraphValidator

pytestmark = pytest.mark.integration


class _FakeSnapshot:
    """Minimal snapshot duck-type for validate_for_run."""

    def __init__(
        self,
        graph_json: dict,
        *,
        schema_pins_json: list | None = None,
        connector_bindings_json: list | None = None,
        model_backend_pins_json: list | None = None,
    ) -> None:
        self.graph_json = graph_json
        self.schema_pins_json = schema_pins_json or []
        self.connector_bindings_json = connector_bindings_json or []
        self.model_backend_pins_json = model_backend_pins_json or []
        self.environment_profile_id = None


async def test_pre_run_blocks_on_empty_graph(
    rls_session: AsyncSession,
) -> None:
    result = await GraphValidator().validate_for_run(
        _FakeSnapshot({"nodes": [], "edges": []}),
        {"input": "data"},
        rls_session,
    )
    assert not result.is_valid
    assert any(i.code == "TOPOLOGY_NO_NODES" for i in result.issues)


async def test_pre_run_blocks_on_cycle(
    rls_session: AsyncSession,
) -> None:
    result = await GraphValidator().validate_for_run(
        _FakeSnapshot(
            {
                "nodes": [{"id": "a"}, {"id": "b"}],
                "edges": [
                    {"source": "a", "target": "b", "type": "normal"},
                    {"source": "b", "target": "a", "type": "normal"},
                ],
            }
        ),
        {},
        rls_session,
    )
    assert not result.is_valid
    assert any(i.code == "TOPOLOGY_CYCLE" for i in result.issues)


async def test_pre_run_blocks_on_nesting_exceeded(
    rls_session: AsyncSession,
) -> None:
    result = await GraphValidator().validate_for_run(
        _FakeSnapshot(
            {
                "nodes": [{"id": "a"}, {"id": "b"}, {"id": "c"}, {"id": "d"}, {"id": "e"}],
                "edges": [
                    {"source": "a", "target": "b", "type": "normal"},
                    {"source": "b", "target": "c", "type": "normal"},
                    {"source": "c", "target": "d", "type": "normal"},
                    {"source": "d", "target": "e", "type": "normal"},
                ],
            }
        ),
        {},
        rls_session,
    )
    assert not result.is_valid
    assert any(i.code == "TOPOLOGY_NESTING_EXCEEDED" for i in result.issues)


async def test_pre_run_passes_valid_graph(
    rls_session: AsyncSession,
) -> None:
    result = await GraphValidator().validate_for_run(
        _FakeSnapshot(
            {
                "nodes": [{"id": "a"}, {"id": "b"}, {"id": "c"}],
                "edges": [
                    {"source": "a", "target": "b", "type": "normal"},
                    {"source": "b", "target": "c", "type": "normal"},
                ],
            }
        ),
        {},
        rls_session,
    )
    assert result.is_valid
