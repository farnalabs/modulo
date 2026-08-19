"""Unit tests for modulo.core.guardrails.conformance — mid-run capability re-check.

Covers the pure decision layer (reusing the T1 ``derive_conformance_state``),
the live-manifest reader (present/absent/unreadable), and the node-start
orchestration fast path. No DB, no Docker — the live-manifest reader is driven
with async mock sessions.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from modulo.core.eval_engine import EvalDefinition, EvalType
from modulo.core.guardrails.conformance import (
    ConformanceRecheckResult,
    build_live_manifest,
    check_node_start,
    decide_conformance,
    evaluate_conformance,
    worst_state,
)

_ORG_ID = uuid.uuid4()


def _gr(name: str, action: str, required: list[str] | None = None) -> EvalDefinition:
    config: dict[str, Any] = {"action": action, "interception_point": "input"}
    if required is not None:
        config["required_capabilities"] = required
    return EvalDefinition(
        id=uuid.uuid4(),
        org_id=_ORG_ID,
        pipeline_id=uuid.uuid4(),
        name=name,
        eval_type=EvalType.GUARDRAIL,
        config=config,
        failure_behaviour="block" if action == "block" else "warn",
    )


# ---------------------------------------------------------------------------
# Pure decision layer
# ---------------------------------------------------------------------------


def test_decide_present_when_all_confirmed():
    d = decide_conformance(["github.read"], {"github.read": True})
    assert d.state == "present"
    assert d.claimed is True


def test_decide_absent_when_any_missing():
    d = decide_conformance(["github.read", "github.write"], {"github.read": True, "github.write": False})
    assert d.state == "absent"
    assert d.missing == ("github.write",)


def test_decide_unknown_when_unreadable():
    d = decide_conformance(["github.read"], {"github.read": None})
    assert d.state == "unknown"
    assert d.unreadable == ("github.read",)


def test_decide_no_claim_when_empty_required():
    d = decide_conformance([], {})
    assert d.state == "present"
    assert d.claimed is False


def test_worst_state_ordering():
    assert worst_state([decide_conformance(["a"], {"a": True})]) == "present"
    assert worst_state([decide_conformance(["a"], {"a": None}), decide_conformance(["b"], {"b": False})]) == "absent"


def test_evaluate_zero_claims_fast_path():
    result = evaluate_conformance([_gr("g1", "block", [])], {})
    assert result.blocked is False
    assert result.claimed is False


def test_evaluate_block_fail_closed_absent():
    gr = _gr("g_block", "block", ["github.write"])
    result = evaluate_conformance([gr], {"github.write": False})
    assert result.blocked is True
    assert result.state == "absent"
    assert result.gate_id == "guardrail_conformance_g_block"
    assert "github.write" in result.detail


def test_evaluate_block_fail_closed_unknown():
    gr = _gr("g_block", "block", ["github.write"])
    result = evaluate_conformance([gr], {"github.write": None})
    assert result.blocked is True
    assert result.state == "unknown"


def test_evaluate_block_present_continues():
    gr = _gr("g_block", "block", ["github.write"])
    result = evaluate_conformance([gr], {"github.write": True})
    assert result.blocked is False
    assert result.state == "present"


def test_evaluate_warn_advisory_never_blocks():
    gr = _gr("g_warn", "warn", ["sandbox.e2b"])
    result = evaluate_conformance([gr], {"sandbox.e2b": False})
    assert result.blocked is False
    assert result.warned is True
    assert result.state == "absent"


def test_evaluate_observe_advisory_never_blocks():
    gr = _gr("g_obs", "observe", ["sandbox.e2b"])
    result = evaluate_conformance([gr], {"sandbox.e2b": None})
    assert result.blocked is False
    assert result.state == "unknown"


def test_evaluate_mixed_block_and_warn_block_wins():
    gb = _gr("g_b", "block", ["cap_a"])
    gw = _gr("g_w", "warn", ["cap_b"])
    result = evaluate_conformance([gb, gw], {"cap_a": False, "cap_b": False})
    assert result.blocked is True
    assert result.state == "absent"
    assert result.warned is True


# ---------------------------------------------------------------------------
# Live manifest reader (async mock session)
# ---------------------------------------------------------------------------


def _row_connector(cid: uuid.UUID, ops: list[str]) -> MagicMock:
    row = MagicMock()
    row.id = cid
    row.status = "active"
    row.allowed_operations = ops
    return row


def _row_profile(pid: uuid.UUID, caps: list[str]) -> MagicMock:
    row = MagicMock()
    row.id = pid
    row.status = "active"
    row.capabilities_json = caps
    return row


def _row_agent(aid: uuid.UUID, caps: list[str]) -> MagicMock:
    row = MagicMock()
    row.id = aid
    row.required_environment_capabilities = caps
    return row


class _ScalarResult:
    def __init__(self, row: Any) -> None:
        self._row = row

    def scalar_one_or_none(self) -> Any:
        return self._row


class _ScalarsResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> _ScalarRows:
        return _ScalarRows(self._rows)


class _ScalarRows:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return self._rows


def _manifest_session(
    *,
    connectors: list[Any] | None = None,
    profile: Any | None = None,
    agent: Any | None = None,
) -> AsyncMock:
    """AsyncMock session that returns the right result per model type.

    The module builds ``select(ConnectorInstance).where(id.in_(ids))`` and calls
    ``execute(stmt).scalars().all()``; for profile/agent it calls
    ``execute(stmt).scalar_one_or_none()``. We differentiate by the model class
    embedded in the statement (the module imports each model and selects it).
    """
    session = AsyncMock()
    connector_rows = {str(r.id): r for r in (connectors or [])}

    async def _execute(stmt: Any) -> Any:
        entity = _entity_of(stmt)
        if entity == "connector":
            ids = getattr(stmt, "_ids", None)
            if ids is None:
                ids = list(connector_rows)
            return _ScalarsResult([connector_rows[i] for i in ids if i in connector_rows])
        if entity == "profile":
            return _ScalarResult(profile)
        if entity == "agent":
            return _ScalarResult(agent)
        raise AssertionError(f"unexpected statement entity {entity!r}")

    session.execute = AsyncMock(side_effect=_execute)
    return session


def _entity_of(stmt: Any) -> str:
    marker = getattr(stmt, "_conformance_entity", None)
    if marker:
        return marker
    raise AssertionError("cannot determine statement entity")


def _patch_select(monkeypatch: pytest.MonkeyPatch, session: AsyncMock) -> None:
    """Replace the module's ``select`` so it stamps each statement's entity marker."""
    import modulo.core.guardrails.conformance as mod

    real_select = mod.select

    def _fake_select(entity: Any) -> Any:
        stmt = real_select(entity)
        from modulo.db.models.agent import Agent
        from modulo.db.models.connector_instance import ConnectorInstance
        from modulo.db.models.environment_profile import EnvironmentProfile

        if entity is ConnectorInstance:
            stmt._conformance_entity = "connector"  # type: ignore[attr-defined]
        elif entity is EnvironmentProfile:
            stmt._conformance_entity = "profile"  # type: ignore[attr-defined]
        elif entity is Agent:
            stmt._conformance_entity = "agent"  # type: ignore[attr-defined]
        return stmt

    monkeypatch.setattr(mod, "select", _fake_select)
    session._fake_select = _fake_select  # type: ignore[attr-defined]


async def test_build_live_manifest_present_and_absent(monkeypatch: pytest.MonkeyPatch):
    cid = uuid.uuid4()
    session = _manifest_session(connectors=[_row_connector(cid, ["github.read"])])
    _patch_select(monkeypatch, session)
    registered = await build_live_manifest(
        session,
        org_id=_ORG_ID,
        connector_instance_ids=[cid],
        environment_profile_id=None,
        agent_id=None,
    )
    assert registered.get("github.read") is True


async def test_build_live_manifest_connector_missing_is_unknown(monkeypatch: pytest.MonkeyPatch):
    missing_id = uuid.uuid4()
    session = _manifest_session(connectors=[])
    _patch_select(monkeypatch, session)
    registered = await build_live_manifest(
        session,
        org_id=_ORG_ID,
        connector_instance_ids=[missing_id],
        environment_profile_id=None,
        agent_id=None,
    )
    assert registered == {}


async def test_build_live_manifest_profile_and_agent(monkeypatch: pytest.MonkeyPatch):
    pid = uuid.uuid4()
    aid = uuid.uuid4()
    session = _manifest_session(
        profile=_row_profile(pid, ["sandbox.e2b", "network:github.com"]), agent=_row_agent(aid, ["git", "shell"])
    )
    _patch_select(monkeypatch, session)
    registered = await build_live_manifest(
        session,
        org_id=_ORG_ID,
        connector_instance_ids=[],
        environment_profile_id=pid,
        agent_id=aid,
    )
    assert registered.get("sandbox.e2b") is True
    assert registered.get("git") is True


async def test_build_live_manifest_inactive_connector_absent(monkeypatch: pytest.MonkeyPatch):
    cid = uuid.uuid4()
    row = _row_connector(cid, ["github.read"])
    row.status = "inactive"
    session = _manifest_session(connectors=[row])
    _patch_select(monkeypatch, session)
    registered = await build_live_manifest(
        session,
        org_id=_ORG_ID,
        connector_instance_ids=[cid],
        environment_profile_id=None,
        agent_id=None,
    )
    # Deactivated connector grants nothing -> capability not confirmed.
    assert registered == {}


async def test_build_live_manifest_inactive_profile_absent(monkeypatch: pytest.MonkeyPatch):
    pid = uuid.uuid4()
    row = _row_profile(pid, ["sandbox.e2b"])
    row.status = "inactive"
    session = _manifest_session(profile=row)
    _patch_select(monkeypatch, session)
    registered = await build_live_manifest(
        session,
        org_id=_ORG_ID,
        connector_instance_ids=[],
        environment_profile_id=pid,
        agent_id=None,
    )
    assert registered == {}


# ---------------------------------------------------------------------------
# Node-start orchestration
# ---------------------------------------------------------------------------


def _patch_orchestration(monkeypatch: pytest.MonkeyPatch, guardrails: list[Any]) -> None:
    """Patch the orchestration's load + RLS so unit tests stay DB-free."""
    import modulo.core.guardrails.conformance as mod

    async def _fake_load(*args: Any, **kwargs: Any) -> list[Any]:
        return guardrails

    async def _noop_rls(session: Any, org_id: uuid.UUID) -> None:
        return None

    monkeypatch.setattr(mod, "load_node_guardrails", _fake_load)
    monkeypatch.setattr(mod, "_set_rls", _noop_rls)


async def test_check_node_start_zero_claim_fast_path(monkeypatch: pytest.MonkeyPatch):
    _patch_orchestration(monkeypatch, [_gr("g1", "block", [])])
    session = _manifest_session()
    _patch_select(monkeypatch, session)
    factory = MagicMock()
    factory.return_value = session
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=None)
    session.begin = MagicMock(return_value=session)  # async context manager via session itself
    result = await check_node_start(
        factory,
        org_id=_ORG_ID,
        pipeline_id=uuid.uuid4(),
        node_id="node-1",
        connector_instance_ids=[],
        environment_profile_id=None,
        agent_id=None,
    )
    assert result.blocked is False
    assert result.claimed is False


async def test_check_node_start_block_absent(monkeypatch: pytest.MonkeyPatch):
    _patch_orchestration(monkeypatch, [_gr("g_block", "block", ["github.write"])])
    session = _manifest_session(connectors=[_row_connector(uuid.uuid4(), ["github.read"])])
    _patch_select(monkeypatch, session)
    session.begin = MagicMock(return_value=session)
    factory = MagicMock()
    factory.return_value = session
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=None)
    result = await check_node_start(
        factory,
        org_id=_ORG_ID,
        pipeline_id=uuid.uuid4(),
        node_id="node-1",
        connector_instance_ids=[],
        environment_profile_id=None,
        agent_id=None,
    )
    assert result.blocked is True
    assert result.state == "unknown"


async def test_check_node_start_present_continues(monkeypatch: pytest.MonkeyPatch):
    _patch_orchestration(monkeypatch, [_gr("g_block", "block", ["github.write"])])
    cid = uuid.uuid4()
    session = _manifest_session(connectors=[_row_connector(cid, ["github.write"])])
    _patch_select(monkeypatch, session)
    session.begin = MagicMock(return_value=session)
    factory = MagicMock()
    factory.return_value = session
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=None)
    result = await check_node_start(
        factory,
        org_id=_ORG_ID,
        pipeline_id=uuid.uuid4(),
        node_id="node-1",
        connector_instance_ids=[cid],
        environment_profile_id=None,
        agent_id=None,
    )
    assert result.blocked is False
    assert result.state == "present"


async def test_check_node_start_load_failure_fails_closed(monkeypatch: pytest.MonkeyPatch):
    import modulo.core.guardrails.conformance as mod

    async def _boom_load(*args: Any, **kwargs: Any) -> list[Any]:
        raise RuntimeError("db down")

    async def _noop_rls(session: Any, org_id: uuid.UUID) -> None:
        return None

    monkeypatch.setattr(mod, "load_node_guardrails", _boom_load)
    monkeypatch.setattr(mod, "_set_rls", _noop_rls)
    session = _manifest_session()
    _patch_select(monkeypatch, session)
    session.begin = MagicMock(return_value=session)
    factory = MagicMock()
    factory.return_value = session
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=None)
    result = await check_node_start(
        factory,
        org_id=_ORG_ID,
        pipeline_id=uuid.uuid4(),
        node_id="node-1",
        connector_instance_ids=[],
        environment_profile_id=None,
        agent_id=None,
    )
    assert result.blocked is True
    assert result.state == "unknown"


async def test_check_node_start_zero_claim_no_manifest_roundtrip(monkeypatch: pytest.MonkeyPatch):
    """Zero conformance claims -> fast path without a manifest DB round-trip.

    ``build_live_manifest`` must never be called when no guardrail carries a
    conformance claim — otherwise the check pays an avoidable DB read on every
    node start for pipelines that never use conformance guardrails.
    """
    import modulo.core.guardrails.conformance as mod

    async def _fake_load(*args: Any, **kwargs: Any) -> list[Any]:
        return [_gr("g1", "block", []), _gr("g2", "warn", None)]

    async def _noop_rls(session: Any, org_id: uuid.UUID) -> None:
        return None

    async def _boom_manifest(*args: Any, **kwargs: Any) -> dict[str, bool | None]:
        raise AssertionError("build_live_manifest must not be called on zero-claim fast path")

    monkeypatch.setattr(mod, "load_node_guardrails", _fake_load)
    monkeypatch.setattr(mod, "_set_rls", _noop_rls)
    monkeypatch.setattr(mod, "build_live_manifest", _boom_manifest)
    session = _manifest_session()
    session.begin = MagicMock(return_value=session)
    factory = MagicMock()
    factory.return_value = session
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=None)
    result = await check_node_start(
        factory,
        org_id=_ORG_ID,
        pipeline_id=uuid.uuid4(),
        node_id="node-1",
        connector_instance_ids=[],
        environment_profile_id=None,
        agent_id=None,
    )
    assert result.blocked is False
    assert result.claimed is False


# ---------------------------------------------------------------------------
# Hoisted claim discovery (FAR-215 MINOR 2): the executor precomputes the
# claimed guardrail list once per run; the per-node check skips its own
# guardrail-load query entirely when the list is provided.
# ---------------------------------------------------------------------------


def _guardrail_row(name: str, action: str, required: list[str] | None, node_id: str | None = None) -> MagicMock:
    """DB-row-like guardrail row for ``load_claimed_guardrails`` (which maps
    rows to engine DTOs via ``to_engine_definition``)."""
    row = MagicMock()
    row.id = uuid.uuid4()
    row.organisation_id = _ORG_ID
    row.pipeline_id = uuid.uuid4()
    row.node_id = node_id
    row.name = name
    row.eval_type = "guardrail"
    config: dict[str, Any] = {"action": action, "interception_point": "input"}
    if required is not None:
        config["required_capabilities"] = required
    row.config_json = config
    row.failure_behaviour = "block" if action == "block" else "warn"
    row.pass_threshold = None
    row.suite_id = None
    return row


def _hoist_factory(session: AsyncMock) -> MagicMock:
    session.begin = MagicMock(return_value=session)
    factory = MagicMock()
    factory.return_value = session
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=None)
    return factory


async def test_load_claimed_guardrails_hoists_claimed_only(monkeypatch: pytest.MonkeyPatch):
    """The run-start hoist loads ALL guardrail rows once and returns only those
    carrying a conformance claim (non-empty ``required_capabilities``)."""
    import modulo.core.guardrails.conformance as mod

    async def _noop_rls(session: Any, org_id: uuid.UUID) -> None:
        return None

    rows = [
        _guardrail_row("g_claim", "block", ["sandbox.e2b"]),
        _guardrail_row("g_plain", "warn", None),
        _guardrail_row("g_no_caps", "block", []),
    ]
    session = AsyncMock()
    session.execute = AsyncMock(return_value=_ScalarsResult(rows))
    factory = _hoist_factory(session)
    monkeypatch.setattr(mod, "_set_rls", _noop_rls)

    claimed, load_failed = await mod.load_claimed_guardrails(
        factory,
        org_id=_ORG_ID,
        pipeline_id=uuid.uuid4(),
    )

    assert load_failed is False
    assert [g.name for g in claimed] == ["g_claim"]


async def test_load_claimed_guardrails_failure_marks_fail_closed(monkeypatch: pytest.MonkeyPatch):
    """A run-start load failure must NOT silently skip claims — it returns the
    fail-closed marker so the node gate treats capabilities as unknown."""
    import modulo.core.guardrails.conformance as mod

    async def _noop_rls(session: Any, org_id: uuid.UUID) -> None:
        return None

    async def _boom(stmt: Any) -> Any:
        raise RuntimeError("db down")

    session = AsyncMock()
    session.execute = AsyncMock(side_effect=_boom)
    factory = _hoist_factory(session)
    monkeypatch.setattr(mod, "_set_rls", _noop_rls)

    claimed, load_failed = await mod.load_claimed_guardrails(
        factory,
        org_id=_ORG_ID,
        pipeline_id=uuid.uuid4(),
    )

    assert load_failed is True
    assert claimed == []


async def test_check_node_start_hoisted_claims_skips_guardrail_load(monkeypatch: pytest.MonkeyPatch):
    """With the executor's hoisted claimed list the per-node guardrail-load
    query is skipped entirely — a non-hoisted path that touches the loader
    would raise, proving zero per-node DB round-trip."""
    import modulo.core.guardrails.conformance as mod

    async def _boom_load(*args: Any, **kwargs: Any) -> list[Any]:
        raise AssertionError("load_node_guardrails must not be called on the hoisted path")

    async def _noop_rls(session: Any, org_id: uuid.UUID) -> None:
        return None

    async def _manifest(*args: Any, **kwargs: Any) -> dict[str, bool | None]:
        return {"sandbox.e2b": False}

    monkeypatch.setattr(mod, "load_node_guardrails", _boom_load)
    monkeypatch.setattr(mod, "build_live_manifest", _manifest)
    monkeypatch.setattr(mod, "_set_rls", _noop_rls)
    session = _manifest_session()
    factory = _hoist_factory(session)

    result = await check_node_start(
        factory,
        org_id=_ORG_ID,
        pipeline_id=uuid.uuid4(),
        node_id="node-1",
        connector_instance_ids=[],
        environment_profile_id=None,
        agent_id=None,
        claimed_guardrails=[_gr("g_block", "block", ["sandbox.e2b"])],
    )
    assert result.blocked is True
    assert result.state == "absent"
    assert result.claimed is True


async def test_check_node_start_hoisted_zero_claims_fast_path(monkeypatch: pytest.MonkeyPatch):
    """Hoisted empty claim list -> fast path: no guardrail load AND no manifest."""
    import modulo.core.guardrails.conformance as mod

    async def _boom_load(*args: Any, **kwargs: Any) -> list[Any]:
        raise AssertionError("load_node_guardrails must not be called on the hoisted path")

    async def _noop_rls(session: Any, org_id: uuid.UUID) -> None:
        return None

    async def _boom_manifest(*args: Any, **kwargs: Any) -> dict[str, bool | None]:
        raise AssertionError("build_live_manifest must not be called on zero-claim fast path")

    monkeypatch.setattr(mod, "load_node_guardrails", _boom_load)
    monkeypatch.setattr(mod, "build_live_manifest", _boom_manifest)
    monkeypatch.setattr(mod, "_set_rls", _noop_rls)
    session = _manifest_session()
    factory = _hoist_factory(session)

    result = await check_node_start(
        factory,
        org_id=_ORG_ID,
        pipeline_id=uuid.uuid4(),
        node_id="node-1",
        connector_instance_ids=[],
        environment_profile_id=None,
        agent_id=None,
        claimed_guardrails=[],
    )
    assert result.blocked is False
    assert result.claimed is False


async def test_check_node_start_claims_load_failed_fails_closed(monkeypatch: pytest.MonkeyPatch):
    """Run-start claim-discovery failure -> fail CLOSED (unknown blocks): no
    session, no load, no manifest — the node is denied, never fail-open."""
    import modulo.core.guardrails.conformance as mod

    async def _boom_load(*args: Any, **kwargs: Any) -> list[Any]:
        raise AssertionError("load_node_guardrails must not be called when claims load failed")

    async def _boom_manifest(*args: Any, **kwargs: Any) -> dict[str, bool | None]:
        raise AssertionError("build_live_manifest must not be called when claims load failed")

    monkeypatch.setattr(mod, "load_node_guardrails", _boom_load)
    monkeypatch.setattr(mod, "build_live_manifest", _boom_manifest)
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(
        side_effect=AssertionError("no session must open when claims load failed")
    )

    result = await check_node_start(
        factory,
        org_id=_ORG_ID,
        pipeline_id=uuid.uuid4(),
        node_id="node-1",
        connector_instance_ids=[],
        environment_profile_id=None,
        agent_id=None,
        claimed_guardrails=None,
        claims_load_failed=True,
    )
    assert result.blocked is True
    assert result.state == "unknown"
    assert result.gate_id == "guardrail_conformance_check_failed"
    assert result.claimed is True


async def test_check_node_start_hoisted_claims_node_scoped(monkeypatch: pytest.MonkeyPatch):
    """The hoisted list carries every node's claims; only THIS node's bindings
    (org-level + node-bound) are evaluated — another node's block claim must
    not block this node."""
    import modulo.core.guardrails.conformance as mod

    async def _noop_rls(session: Any, org_id: uuid.UUID) -> None:
        return None

    async def _manifest(*args: Any, **kwargs: Any) -> dict[str, bool | None]:
        return {"sandbox.e2b": True, "git.write": False}

    monkeypatch.setattr(mod, "build_live_manifest", _manifest)
    monkeypatch.setattr(mod, "_set_rls", _noop_rls)
    session = _manifest_session()
    factory = _hoist_factory(session)

    node_a = str(uuid.uuid4())
    node_b = str(uuid.uuid4())
    org_level = _gr("g_org_level", "block", ["sandbox.e2b"])
    node_b_claim = _gr("g_node_b", "block", ["git.write"])
    node_b_claim.node_id = node_b

    result_a = await check_node_start(
        factory,
        org_id=_ORG_ID,
        pipeline_id=uuid.uuid4(),
        node_id=node_a,
        connector_instance_ids=[],
        environment_profile_id=None,
        agent_id=None,
        claimed_guardrails=[org_level, node_b_claim],
    )
    # g_org_level's capability is present; node B's git.write claim is ignored.
    assert result_a.blocked is False
    assert result_a.state == "present"

    result_b = await check_node_start(
        factory,
        org_id=_ORG_ID,
        pipeline_id=uuid.uuid4(),
        node_id=node_b,
        connector_instance_ids=[],
        environment_profile_id=None,
        agent_id=None,
        claimed_guardrails=[org_level, node_b_claim],
    )
    assert result_b.blocked is True
    assert result_b.state == "absent"


async def test_build_live_manifest_unreadable_surface_fails_closed(monkeypatch: pytest.MonkeyPatch):
    """An unreadable capability source contributes nothing (unknown), so a
    block-action guardrail fails CLOSED — never fail-open."""
    import modulo.core.guardrails.conformance as mod

    session = AsyncMock()

    async def _boom_execute(stmt: Any) -> Any:
        raise RuntimeError("db connection lost")

    session.execute = AsyncMock(side_effect=_boom_execute)

    def _fake_select(entity: Any) -> Any:
        stmt = MagicMock()
        stmt._conformance_entity = "connector"  # type: ignore[attr-defined]
        return stmt

    monkeypatch.setattr(mod, "select", _fake_select)
    registered = await build_live_manifest(
        session,
        org_id=_ORG_ID,
        connector_instance_ids=[uuid.uuid4()],
        environment_profile_id=None,
        agent_id=None,
    )
    # Reader degrades to unknown: no capabilities confirmed -> block fails closed.
    assert registered == {}
    derivation = decide_conformance(["github.write"], registered)
    assert derivation.state == "unknown"
    result = evaluate_conformance([_gr("g_block", "block", ["github.write"])], registered)
    assert result.blocked is True
    assert result.state == "unknown"


# ---------------------------------------------------------------------------
# Sandbox capability surface (FAR-212 PR A): mechanically derived from the
# node's actual config, stamped into the manifest with conformance polarity
# (a block guardrail's required_capabilities on the sandbox surface is a
# deny/negative guarantee — confirmed-absent write/egress is the certification).
# ---------------------------------------------------------------------------


def _sandbox_node(**overrides: Any) -> dict[str, Any]:
    node: dict[str, Any] = {"id": "node-1", "node_type": "sandbox_agent", "agent_prompt": "p", "agent_command": "c"}
    node.update(overrides)
    return node


async def test_build_live_manifest_sandbox_egress_denied_is_certified():
    """egress_policy='deny_all' -> mechanical egress False -> the manifest's
    sandbox.egress is True (the 'no egress' guarantee is confirmed)."""
    registered = await build_live_manifest(
        AsyncMock(),
        org_id=_ORG_ID,
        connector_instance_ids=[],
        environment_profile_id=None,
        agent_id=None,
        node_def=_sandbox_node(egress_policy="deny_all"),
    )
    assert registered.get("sandbox.egress") is True


async def test_build_live_manifest_sandbox_read_only_is_certified():
    """read_only=True -> mechanical write_files False -> the manifest's
    sandbox.write_files is True (writes confirmed impossible)."""
    registered = await build_live_manifest(
        AsyncMock(),
        org_id=_ORG_ID,
        connector_instance_ids=[],
        environment_profile_id=None,
        agent_id=None,
        node_def=_sandbox_node(read_only=True),
    )
    assert registered.get("sandbox.write_files") is True


async def test_build_live_manifest_sandbox_writable_violates_claim():
    """A writable sandbox -> mechanical write_files True -> the manifest's
    sandbox.write_files is False (the no-writes claim is violated)."""
    registered = await build_live_manifest(
        AsyncMock(),
        org_id=_ORG_ID,
        connector_instance_ids=[],
        environment_profile_id=None,
        agent_id=None,
        node_def=_sandbox_node(read_only=False),
    )
    assert registered.get("sandbox.write_files") is False


async def test_build_live_manifest_non_sandbox_node_no_surface():
    """A non-sandbox node contributes no sandbox capability surface."""
    registered = await build_live_manifest(
        AsyncMock(),
        org_id=_ORG_ID,
        connector_instance_ids=[],
        environment_profile_id=None,
        agent_id=None,
        node_def={"id": "node-1", "node_type": "agent"},
    )
    assert "sandbox.write_files" not in registered
    assert "sandbox.egress" not in registered


async def test_build_live_manifest_sandbox_egress_default_is_violated():
    """A default-egress sandbox (internet allowed) -> mechanical egress True ->
    sandbox.egress is False (the no-egress claim is violated)."""
    registered = await build_live_manifest(
        AsyncMock(),
        org_id=_ORG_ID,
        connector_instance_ids=[],
        environment_profile_id=None,
        agent_id=None,
        node_def=_sandbox_node(egress_policy="default"),
    )
    assert registered.get("sandbox.egress") is False


async def test_build_live_manifest_sandbox_unknown_surface_fail_closed():
    """An unrecognised egress policy (no mechanical fact) -> the capability is
    absent from the manifest (unknown) -> a block guardrail fails CLOSED."""
    registered = await build_live_manifest(
        AsyncMock(),
        org_id=_ORG_ID,
        connector_instance_ids=[],
        environment_profile_id=None,
        agent_id=None,
        node_def=_sandbox_node(egress_policy="allow_all"),
    )
    assert registered.get("sandbox.egress") is None
    derivation = decide_conformance(["sandbox.egress"], registered)
    assert derivation.state == "unknown"


# ---------------------------------------------------------------------------
# Full gate: sandbox capability claims reach check_node_start (FAR-212 PR A)
# ---------------------------------------------------------------------------


async def _run_hoisted_check(
    monkeypatch: pytest.MonkeyPatch,
    *,
    guardrails: list[Any],
    node_def: dict[str, Any] | None,
) -> ConformanceRecheckResult:
    import modulo.core.guardrails.conformance as mod

    async def _noop_rls(session: Any, org_id: uuid.UUID) -> None:
        return None

    monkeypatch.setattr(mod, "_set_rls", _noop_rls)
    session = _manifest_session()
    session.begin = MagicMock(return_value=session)
    factory = MagicMock()
    factory.return_value = session
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=None)
    return await check_node_start(
        factory,
        org_id=_ORG_ID,
        pipeline_id=uuid.uuid4(),
        node_id="node-1",
        connector_instance_ids=[],
        environment_profile_id=None,
        agent_id=None,
        node_def=node_def,
        claimed_guardrails=guardrails,
    )


async def test_check_node_start_sandbox_read_only_certifies_write_guardrail(monkeypatch: pytest.MonkeyPatch):
    """A block guardrail requiring sandbox.write_files on a read-only sandbox is
    CERTIFIED (present) — the conformance hard-block confirms writes are
    impossible rather than trusting a declaration."""
    result = await _run_hoisted_check(
        monkeypatch,
        guardrails=[_gr("g_nowrite", "block", ["sandbox.write_files"])],
        node_def=_sandbox_node(read_only=True),
    )
    assert result.blocked is False
    assert result.state == "present"
    assert result.claimed is True


async def test_check_node_start_sandbox_deny_all_certifies_egress_guardrail(monkeypatch: pytest.MonkeyPatch):
    """A block guardrail requiring sandbox.egress on a deny_all sandbox is
    CERTIFIED (present) — confirmed no egress."""
    result = await _run_hoisted_check(
        monkeypatch,
        guardrails=[_gr("g_noegress", "block", ["sandbox.egress"])],
        node_def=_sandbox_node(egress_policy="deny_all"),
    )
    assert result.blocked is False
    assert result.state == "present"
    assert result.claimed is True


async def test_check_node_start_sandbox_writable_blocks_write_guardrail(monkeypatch: pytest.MonkeyPatch):
    """A block guardrail requiring sandbox.write_files on a WRITABLE sandbox is
    non-conformant (absent) -> fail-closed block: the no-writes claim is false."""
    result = await _run_hoisted_check(
        monkeypatch,
        guardrails=[_gr("g_nowrite", "block", ["sandbox.write_files"])],
        node_def=_sandbox_node(read_only=False),
    )
    assert result.blocked is True
    assert result.state == "absent"
    assert result.gate_id == "guardrail_conformance_g_nowrite"


async def test_check_node_start_sandbox_unknown_fails_closed(monkeypatch: pytest.MonkeyPatch):
    """No sandbox surface (no node_def) -> the capability is unknown -> the
    block guardrail fails CLOSED (never fail-open on an unreadable surface)."""
    result = await _run_hoisted_check(
        monkeypatch,
        guardrails=[_gr("g_nowrite", "block", ["sandbox.write_files"])],
        node_def=None,
    )
    assert result.blocked is True
    assert result.state == "unknown"
