"""Shared pytest fixtures for the pipeline_engine unit test suite."""

import os

# The backend ``Settings`` model requires DATABASE_URL/SECRET_KEY/FERNET_KEY
# and the D8 gate reads ``get_settings()`` on every dispatch path. There is no
# ``.env`` in worktrees, so provide the minimum env the same way as
# ``tests/unit/core/conftest.py`` — setdefault so explicit CI values always
# win. Without this, running THIS directory standalone (not after core/) fails
# on the first ``get_settings()`` inside the dispatch gate.
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://localhost/test")
os.environ.setdefault("SECRET_KEY", "a" * 32)
os.environ.setdefault("FERNET_KEY", "a" * 32)
os.environ.setdefault("REDIS_URL", "")
os.environ.setdefault("MODULO_ADMIN_PASSWORD", "test")
os.environ.setdefault("MODULO_CSRF_ENABLED", "false")

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
