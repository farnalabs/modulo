"""Shared fixtures for composite_engine tests."""

import uuid
from contextlib import contextmanager
from typing import Any
from unittest.mock import patch

import pytest


@pytest.fixture
def patch_expander():
    """Fixture that patches expander module attributes with automatic cleanup.

    Usage::

        async def test_something(patch_expander):
            with patch_expander(expand=my_expand_fn, validate=my_validate_fn):
                ...
    """
    import modulo.core.composite_engine.expander as mod

    @contextmanager
    def _patcher(expand: Any = None, validate: Any = None):
        patches: list[patch] = []
        if expand is not None:
            p = patch.object(mod, "expand_composite_node", expand)
            p.start()
            patches.append(p)
        if validate is not None:
            p = patch.object(mod, "run_output_validation", validate)
            p.start()
            patches.append(p)
        try:
            yield
        finally:
            for p in patches:
                p.stop()

    return _patcher


def make_default_template(nodes: list[dict] | None = None) -> dict:
    """Create a minimal composite template dict for tests."""
    return {
        "nodes": nodes or [{"id": str(uuid.uuid4()), "agent_id": str(uuid.uuid4()), "prompt": "Hello"}],
        "edges": [],
    }


def make_node_def(**overrides: Any) -> dict:
    """Create a minimal composite node definition for tests."""
    defn: dict[str, Any] = {
        "id": str(uuid.uuid4()),
        "node_type": "composite",
        "composite_ref": str(uuid.uuid4()),
    }
    defn.update(overrides)
    return defn
