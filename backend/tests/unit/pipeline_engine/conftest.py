"""Shared pytest fixtures for the pipeline_engine unit test suite."""

from typing import Any

import pytest
from langgraph.errors import GraphInterrupt
from langgraph.types import Interrupt


@pytest.fixture
def _interrupt_without_graph_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    """Simulate Interrupt() outside a LangGraph runtime (raises GraphInterrupt).

    Opt-in (not autouse) so tests that exercise the real interrupt machinery
    (e.g. test_executor.py) keep their real-interrupt expectations.
    """

    def raise_interrupt(value: Any) -> None:
        raise GraphInterrupt((Interrupt(value=value),))

    monkeypatch.setattr("modulo.core.pipeline_engine.node_runner.interrupt", raise_interrupt)
