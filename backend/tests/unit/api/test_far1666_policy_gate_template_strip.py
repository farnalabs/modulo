"""FAR-1666: template writers strip declared policy gates with a warning.

Both ``create-pipeline-from-template`` writers (``api/routes/library.py`` and
``api/routes/templates.py``) build the persisted graph nodes directly and never
reconcile. A template cannot carry the pipeline's ``PolicyGate`` rows (evals and
gates are not cloned), so a declared block would persist INERT. Both routes now
strip ``policy_gate`` blocks through the shared helper and record what was
dropped; these tests drive the node-building helpers directly and assert both
halves — the persisted nodes are clean AND the drop is disclosed.
"""

from __future__ import annotations

import logging
import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from modulo.api.routes.library import _build_pipeline_from_template
from modulo.api.routes.templates import _resolve_template_nodes

_EVAL_ID = str(uuid.uuid4())


def _gate_node() -> dict[str, Any]:
    return {
        "id": "template-node-1",
        "node_type": "manual",
        "label": "Gated step",
        "position": {"x": 1, "y": 2},
        "output_schema_json": {"type": "object"},
        "policy_gate": {"eval_id": _EVAL_ID, "action": "block"},
    }


def _plain_node() -> dict[str, Any]:
    return {
        "id": "template-node-2",
        "node_type": "manual",
        "label": "Plain step",
        "position": {"x": 3, "y": 4},
        "output_schema_json": {"type": "object"},
    }


class TestLibraryInstallFromTemplateStrip:
    def test_declared_gate_is_stripped_and_warned(self, caplog: pytest.LogCaptureFixture) -> None:
        primitive = SimpleNamespace(
            id=uuid.uuid4(),
            name="Template",
            description="d",
            content_json={"agents": [], "graph_nodes": [_gate_node(), _plain_node()], "edges": []},
        )

        with caplog.at_level(logging.WARNING, logger="modulo.api.routes.library"):
            _name, _desc, nodes, _edges, _agent_count, _edge_count = _build_pipeline_from_template(
                primitive, None, None
            )

        assert all("policy_gate" not in node for node in nodes)
        assert len(nodes) == 2
        assert "policy_gate declaration(s) stripped when installing pipeline template" in caplog.text
        assert "template-node-1" in caplog.text

    def test_gate_free_template_does_not_warn(self, caplog: pytest.LogCaptureFixture) -> None:
        primitive = SimpleNamespace(
            id=uuid.uuid4(),
            name="Template",
            description="d",
            content_json={"agents": [], "graph_nodes": [_plain_node()], "edges": []},
        )

        with caplog.at_level(logging.WARNING, logger="modulo.api.routes.library"):
            _name, _desc, nodes, _edges, _agent_count, _edge_count = _build_pipeline_from_template(
                primitive, None, None
            )

        assert len(nodes) == 1
        assert "policy_gate declaration(s) stripped" not in caplog.text


class TestTemplatesCreateFromTemplateStrip:
    def test_declared_gate_is_stripped_and_warned(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING, logger="modulo.api.routes.templates"):
            nodes = _resolve_template_nodes([_gate_node(), _plain_node()], [], {})

        assert all("policy_gate" not in node for node in nodes)
        assert len(nodes) == 2
        assert "policy_gate declaration(s) stripped when creating a pipeline from template" in caplog.text
        assert "template-node-1" in caplog.text

    def test_gate_free_template_does_not_warn(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING, logger="modulo.api.routes.templates"):
            nodes = _resolve_template_nodes([_plain_node()], [], {})

        assert len(nodes) == 1
        assert "policy_gate declaration(s) stripped" not in caplog.text
