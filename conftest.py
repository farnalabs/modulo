from __future__ import annotations

import os
import sys
from collections.abc import Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

_root = os.path.dirname(os.path.abspath(__file__))
for _p in [os.path.join(_root, "backend", "src"), os.path.join(_root, "backend")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from modulo.core.eval_engine import EvalDefinition, EvalEngine, EvalResult
from modulo.settings import Settings


class MockSettings:
    smtp_host = "smtp.example.com"
    smtp_port = 587
    smtp_username = "user"
    smtp_password = "pass"
    email_from = "noreply@example.com"


class MockSettingsNoSMTP:
    smtp_host = ""
    smtp_port = 587
    smtp_username = ""
    smtp_password = ""
    email_from = ""


def make_eval_def(
    eval_type: str = "regex",
    config: dict | None = None,
    *,
    name: str = "test-eval",
    failure_behaviour: str = "warn",
    pass_threshold: float | None = None,
) -> EvalDefinition:
    return EvalDefinition(
        id=uuid4(),
        org_id=uuid4(),
        name=name,
        eval_type=eval_type,
        config=config or {},
        failure_behaviour=failure_behaviour,
        pass_threshold=pass_threshold,
    )


def make_llm_callable(result: dict | None = None) -> Callable:
    default = {"passed": True, "score": 0.95, "detail": "ok"}
    return lambda output, eval_def: result if result is not None else default


def make_result(
    passed: bool,
    score: float | None = None,
    detail: str = "",
    *,
    node_id: str = "n1",
) -> EvalResult:
    return EvalResult(
        id=uuid4(),
        run_id=uuid4(),
        node_id=node_id,
        eval_id=uuid4(),
        passed=passed,
        score=score,
        detail=detail,
    )


def make_capturing_callable(captured: list) -> Callable:
    def callable(output: dict, eval_def: EvalDefinition) -> dict:
        captured.append((output, eval_def))
        return {"passed": True, "score": 0.95, "detail": "ok"}

    return callable


def make_mock_session() -> AsyncMock:
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    session.execute = AsyncMock()
    bind_mock = MagicMock()
    bind_mock.dialect.name = "postgresql"
    session.get_bind = MagicMock(return_value=bind_mock)
    return session


def make_mock_row(**attrs: Any) -> MagicMock:
    m = MagicMock()
    for k, v in attrs.items():
        setattr(m, k, v)
    return m


def make_mock_result(*, all_value: Any = None, scalar_value: Any = None) -> MagicMock:
    m = MagicMock()
    if all_value is not None:
        m.all = MagicMock(return_value=all_value)
    if scalar_value is not None:
        m.scalar = MagicMock(return_value=scalar_value)
    return m


def make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key="a" * 32,
        fernet_key="a" * 32,
        modulo_admin_password="testpass",
    )


@pytest.fixture
def eval_engine() -> EvalEngine:
    return EvalEngine()
