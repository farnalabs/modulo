"""Unit tests for the executor's ``context_write_by_non_setter`` audit hook (§8.18).

Verifies the hook builder returned by ``_dispatch_context_write_audit``:
- appends a ``context_write_by_non_setter`` audit event with node_id, role, and
  attempted_keys under the run's org and RLS context
- swallows and logs failures/timeouts so a broken audit write can never mask the
  ContextSetterViolationError raised by the decorator
"""

import asyncio
import logging
import uuid
from typing import Self
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core.pipeline_engine.executor import PipelineExecutor


class _FakeSession:
    def __init__(self) -> None:
        self.begin_called = False

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False

    def begin(self) -> Self:
        self.begin_called = True
        return self


class _FakeFactory:
    def __init__(self) -> None:
        self.session = _FakeSession()

    def __call__(self) -> _FakeSession:
        return self.session


def _build_executor() -> tuple[PipelineExecutor, _FakeFactory]:
    executor = PipelineExecutor(MagicMock())
    factory = _FakeFactory()
    executor._session_factory = factory  # type: ignore[assignment]
    return executor, factory


class TestContextWriteAuditHook:
    async def test_hook_appends_audit_event(self) -> None:
        executor, factory = _build_executor()
        org_id, run_id = uuid.uuid4(), uuid.uuid4()
        captured: dict[str, object] = {}
        rls_orgs: list[uuid.UUID] = []

        async def fake_append(session: object, **kwargs: object) -> None:
            captured.update(kwargs)

        async def fake_rls(session: object, org_id: uuid.UUID) -> None:
            rls_orgs.append(org_id)

        with (
            patch("modulo.core.pipeline_engine.executor.append_audit_event", fake_append),
            patch("modulo.core.pipeline_engine.executor.set_rls_org", fake_rls),
            patch("modulo.core.pipeline_engine.executor.set_rls_execution_context", AsyncMock()),
        ):
            hook = executor._dispatch_context_write_audit(org_id, run_id)
            await hook(
                {
                    "node_id": "reviewer",
                    "role": "agent",
                    "attempted_keys": ["secret", "model_tier"],
                }
            )

        assert captured["event_type"] == "context_write_by_non_setter"
        assert captured["org_id"] == org_id
        assert captured["resource_type"] == "run"
        assert captured["resource_id"] == run_id
        assert captured["payload_json"] == {
            "node_id": "reviewer",
            "role": "agent",
            "attempted_keys": ["secret", "model_tier"],
        }
        assert rls_orgs == [org_id]
        assert factory.session.begin_called is True

    async def test_hook_empty_payload_coerces_to_empty_list(self) -> None:
        executor, _ = _build_executor()
        org_id, run_id = uuid.uuid4(), uuid.uuid4()
        captured: dict[str, object] = {}

        async def fake_append(session: object, **kwargs: object) -> None:
            captured.update(kwargs)

        async def fake_rls(session: object, org_id: uuid.UUID) -> None:
            return None

        with (
            patch("modulo.core.pipeline_engine.executor.append_audit_event", fake_append),
            patch("modulo.core.pipeline_engine.executor.set_rls_org", fake_rls),
            patch("modulo.core.pipeline_engine.executor.set_rls_execution_context", AsyncMock()),
        ):
            hook = executor._dispatch_context_write_audit(org_id, run_id)
            await hook({})

        assert captured["payload_json"] == {
            "node_id": None,
            "role": None,
            "attempted_keys": [],
        }

    async def test_hook_failure_is_logged_and_swallowed(self, caplog: pytest.LogCaptureFixture) -> None:
        executor, _ = _build_executor()
        org_id, run_id = uuid.uuid4(), uuid.uuid4()

        async def failing_append(session: object, **kwargs: object) -> None:
            raise ConnectionError("audit DB down")

        async def fake_rls(session: object, org_id: uuid.UUID) -> None:
            return None

        with (
            patch("modulo.core.pipeline_engine.executor.append_audit_event", failing_append),
            patch("modulo.core.pipeline_engine.executor.set_rls_org", fake_rls),
            patch("modulo.core.pipeline_engine.executor.set_rls_execution_context", AsyncMock()),
            caplog.at_level(logging.ERROR),
        ):
            hook = executor._dispatch_context_write_audit(org_id, run_id)
            await hook({"node_id": "n", "attempted_keys": []})

        assert any("audit_hook_failed" in r.message for r in caplog.records)

    async def test_hook_timeout_is_logged_and_swallowed(self, caplog: pytest.LogCaptureFixture) -> None:
        executor, _ = _build_executor()
        org_id, run_id = uuid.uuid4(), uuid.uuid4()

        async def hanging_append(session: object, **kwargs: object) -> None:
            await asyncio.sleep(30)

        async def fake_rls(session: object, org_id: uuid.UUID) -> None:
            return None

        with (
            patch("modulo.core.pipeline_engine.executor.append_audit_event", hanging_append),
            patch("modulo.core.pipeline_engine.executor.set_rls_org", fake_rls),
            patch("modulo.core.pipeline_engine.executor.set_rls_execution_context", AsyncMock()),
            caplog.at_level(logging.WARNING),
        ):
            hook = executor._dispatch_context_write_audit(org_id, run_id)
            await hook({"node_id": "n", "attempted_keys": []})

        assert any("audit_hook_timeout" in r.message for r in caplog.records)
