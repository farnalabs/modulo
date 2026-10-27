"""Coverage tests for runtime-provider compatibility and terminal advances."""

from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from modulo.core import run_terminal_advance as rta
from modulo.core.runtime_provider import (
    ExecResult,
    ProviderCapabilityUnsupportedError,
    RuntimeProvider,
    StreamingUnsupportedError,
)
from modulo.core.runtime_provider.base import RuntimeProvider as BaseRuntimeProvider


class _Provider(RuntimeProvider):
    async def create_workspace(self, spec):
        return "ref"

    async def exec_command(self, provider_ref, command, *, cmd_timeout=None):
        return ExecResult(exit_code=0, stdout="", stderr="")

    async def destroy_workspace(self, provider_ref):
        return None

    async def get_workspace_status(self, provider_ref):
        return "ready"


class _FakeSession:
    def __init__(self, run=None):
        self.run = run
        self.info = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def begin(self):
        return self

    async def execute(self, statement):
        return None


class _FakeFactory:
    def __init__(self, session):
        self.session = session

    def __call__(self):
        return self.session


@pytest.mark.asyncio
async def test_runtime_provider_base_is_compatibility_alias_and_defaults_refuse_optional_features():
    assert BaseRuntimeProvider is RuntimeProvider
    provider = _Provider()
    with pytest.raises(StreamingUnsupportedError):
        await provider.exec_command_stream("ref", ["true"])
    with pytest.raises(ProviderCapabilityUnsupportedError, match="destroy_workspace_by_ref"):
        await provider.destroy_workspace_by_ref("ref")
    with pytest.raises(ProviderCapabilityUnsupportedError, match="read_log_tail"):
        await provider.read_log_tail("ref", max_bytes=10)
    with pytest.raises(ProviderCapabilityUnsupportedError, match="apply_isolation"):
        await provider.apply_isolation("ref", MagicMock(), MagicMock())


@pytest.mark.asyncio
async def test_apply_agent_mint_gate_flag_off_drops_agent_refs(monkeypatch):
    org_id = uuid.uuid4()
    run_id = uuid.uuid4()
    refs = [{"kind": "linear", "ref": "1", "source": "agent"}, {"kind": "linear", "ref": "2"}]
    suppressed = MagicMock()
    monkeypatch.setattr("modulo.core.lifecycle_map.advancement.agent_minting_enabled", AsyncMock(return_value=False))
    monkeypatch.setattr("modulo.core.lifecycle_map.reconcile.record_refs_agent_mint_suppressed_by_flag", suppressed)
    result = await rta._apply_agent_mint_gate(MagicMock(), org_id, run_id, refs, [refs[0]])
    assert result == [refs[1]]
    suppressed.assert_called_once_with(1)


@pytest.mark.asyncio
async def test_apply_agent_mint_gate_no_agent_refs_returns_unchanged(monkeypatch):
    refs = [{"kind": "linear", "ref": "2"}]
    monkeypatch.setattr("modulo.core.lifecycle_map.advancement.agent_minting_enabled", AsyncMock(return_value=True))
    result = await rta._apply_agent_mint_gate(MagicMock(), uuid.uuid4(), uuid.uuid4(), refs, [])
    assert result == refs


@pytest.mark.asyncio
async def test_apply_agent_mint_gate_probe_failure_and_budget_paths(monkeypatch):
    org_id = uuid.uuid4()
    run_id = uuid.uuid4()
    refs = [{"kind": "linear", "ref": "1", "source": "agent"}]
    monkeypatch.setattr("modulo.core.lifecycle_map.advancement.agent_minting_enabled", AsyncMock(return_value=True))
    monkeypatch.setattr(
        "modulo.core.lifecycle_map.advancement.confirm_reported_refs",
        AsyncMock(side_effect=RuntimeError("probe down")),
    )
    session = MagicMock()
    session = MagicMock()
    consume = AsyncMock(return_value=False)
    exceeded = MagicMock()
    monkeypatch.setattr("modulo.core.runtime_config.mint_budget.consume_agent_mint_budget", consume)
    monkeypatch.setattr("modulo.core.lifecycle_map.reconcile.record_refs_agent_mint_budget_exceeded", exceeded)
    assert not await rta._apply_agent_mint_gate(session, org_id, run_id, refs, refs)
    consume.assert_awaited_once_with(session, org_id, n=1)
    exceeded.assert_called_once_with(1)

    consume.reset_mock()
    minted = MagicMock()
    monkeypatch.setattr("modulo.core.lifecycle_map.advancement.confirm_reported_refs", AsyncMock(return_value=([], [])))
    monkeypatch.setattr(
        "modulo.core.runtime_config.mint_budget.consume_agent_mint_budget",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr("modulo.core.lifecycle_map.reconcile.record_refs_agent_minted", minted)
    assert await rta._apply_agent_mint_gate(MagicMock(), org_id, run_id, refs, refs) == refs
    minted.assert_called_once_with(1)


@pytest.mark.asyncio
async def test_terminal_advance_missing_runs_are_fail_open(monkeypatch):
    session = _FakeSession()
    factory = _FakeFactory(session)
    monkeypatch.setattr(rta, "async_sessionmaker", lambda *args, **kwargs: factory)
    monkeypatch.setattr(rta, "get_run", AsyncMock(return_value=None))
    monkeypatch.setattr(rta, "set_rls_org", AsyncMock())
    await rta.advance_journeys_from_stored_refs(MagicMock(), uuid.uuid4(), uuid.uuid4())
    await rta.record_terminal_failed_fact(MagicMock(), uuid.uuid4(), uuid.uuid4())
    assert session.run is None


@pytest.mark.asyncio
async def test_terminal_advance_swallow_failures_and_rethrow_cancellation(monkeypatch):
    engine = MagicMock()
    monkeypatch.setattr(rta, "advance_journeys_from_stored_refs", AsyncMock())
    monkeypatch.setattr(rta, "record_terminal_failed_fact", AsyncMock(side_effect=RuntimeError("fact")))
    monkeypatch.setattr(
        "modulo.core.audit_logger.background.record_run_state_change_audits",
        AsyncMock(side_effect=RuntimeError("audit")),
    )
    await rta.advance_terminalised_run(engine, uuid.uuid4(), uuid.uuid4(), source="test")

    monkeypatch.setattr(rta, "advance_journeys_from_stored_refs", AsyncMock(side_effect=asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError):
        await rta.advance_terminalised_run(engine, uuid.uuid4(), uuid.uuid4(), source="test")
