"""Unit tests for modulo.core.guardrails.conformance — mid-run capability re-check.

Covers the pure decision layer (reusing the T1 ``derive_conformance_state``),
the live-manifest reader (present/absent/unreadable), and the node-start
orchestration fast path. No DB, no Docker — the live-manifest reader is driven
with async mock sessions.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from modulo.connectors.base import (
    ConnectorACL,
    ConnectorPermissionError,
    ConnectorType,
    canonical_capability_set,
)
from modulo.core.eval_engine import EvalDefinition, EvalType
from modulo.core.guardrails.conformance import (
    ConformanceRecheckResult,
    _capabilities_for_agent,
    _capabilities_for_connector,
    _capabilities_for_profile,
    _register_connector_surface,
    build_live_manifest,
    canonical_capability,
    check_node_start,
    decide_conformance,
    evaluate_conformance,
    find_type_qualified_claim_guardrails,
    type_qualified_claims,
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
    # FAR-1615: the decision layer PRESERVES a type-qualified claim's name in
    # ``missing`` (``github.write``, not bare ``write``) so the operator can
    # tell which type binding failed.
    d = decide_conformance(["github.read", "github.write"], {"github.read": True, "github.write": False})
    assert d.state == "absent"
    assert d.missing == ("github.write",)


def test_decide_unknown_when_unreadable():
    # FAR-1615: same preservation on the unreadable side.
    d = decide_conformance(["github.read"], {"github.read": None})
    assert d.state == "unknown"
    assert d.unreadable == ("github.read",)


def test_decide_bare_claim_reports_bare_name():
    # A BARE claim still reports its bare canonical name.
    d = decide_conformance(["read"], {"read": None})
    assert d.state == "unknown"
    assert d.unreadable == ("read",)


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
    assert result.review_id == "guardrail_conformance_g_block"
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


def _row_connector(cid: uuid.UUID, ops: list[str], *, connector_type_id: str | None = "github") -> MagicMock:
    row = MagicMock()
    row.id = cid
    row.status = "active"
    row.allowed_operations = ops
    # Default to a github-typed surface so a same-type legacy qualified
    # allowlist entry (["github.read"]) stays verifiable (FAR-1616); tests
    # that need another type (or an unidentifiable one) override it.
    row.connector_type_id = connector_type_id
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
    # FAR-1582: the manifest emits the CANONICAL bare Capability vocabulary, so
    # a legacy-spelled stored allowlist ("github.read") surfaces as "read".
    assert registered.get("read") is True


async def test_build_live_manifest_empty_allowlist_yields_type_capabilities(monkeypatch: pytest.MonkeyPatch):
    """FAR-1564: an unset (``[]``) allowlist is UNRESTRICTED, so the manifest
    must carry the connector TYPE's full capability set — not nothing."""
    cid = uuid.uuid4()
    row = _row_connector(cid, [])
    row.connector_type_id = "github"
    session = _manifest_session(connectors=[row])
    _patch_select(monkeypatch, session)
    registered = await build_live_manifest(
        session,
        org_id=_ORG_ID,
        connector_instance_ids=[cid],
        environment_profile_id=None,
        agent_id=None,
    )
    assert registered.get("read") is True
    assert registered.get("write") is True
    assert registered.get("git_push") is True
    assert registered.get("create_pr") is True


def test_register_connector_surface_skips_alias_for_non_bare_capability():
    """A legacy-qualified capability earns NO ``<type>.<cap>`` alias.

    ``capabilities`` reaching this helper is always already canonical in
    production (``_capabilities_for_connector`` reduces every accepted spelling),
    so the guard is defensive: only a BARE capability gets the type-qualified
    alias a typed claim matches, while a spelling that does not round-trip to
    itself is left unaliased rather than mis-stamped as ``github.github.write``.
    """
    registered: dict[str, bool | None] = {}
    row = MagicMock()
    row.connector_type_id = "github"
    _register_connector_surface(registered, row, {"read", "github.write"})
    assert registered["read"] is True
    assert registered["github.read"] is True
    assert "github.github.write" not in registered


def test_capabilities_for_connector_none_allowlist_yields_type_capabilities():
    """FAR-1564: ``None`` — the other unset representation — is unrestricted."""
    row = _row_connector(uuid.uuid4(), [])
    row.allowed_operations = None
    row.connector_type_id = "github"
    caps = _capabilities_for_connector(row)
    assert caps == {str(c) for c in ConnectorType("github").capabilities}
    assert "read" in caps


def test_capabilities_for_connector_non_empty_allowlist_is_exact():
    """A non-empty allowlist remains the exact declared scope."""
    row = _row_connector(uuid.uuid4(), ["read"])
    row.connector_type_id = "github"
    assert _capabilities_for_connector(row) == {"read"}


def test_capabilities_for_connector_unknown_type_certifies_nothing():
    """Fail-closed: a type we cannot identify certifies no capability."""
    row = _row_connector(uuid.uuid4(), [])
    row.connector_type_id = "not-a-real-type"
    assert not _capabilities_for_connector(row)


def test_capabilities_for_connector_non_string_type_id_certifies_nothing():
    """FAR-1564 fail-closed: a NON-STRING ``connector_type_id`` certifies nothing.

    ``_type_capabilities`` guards ``isinstance(type_id, str)`` before it builds
    a ``ConnectorType``; a null/malformed type id (or a row lacking the
    attribute) must contribute NO capability rather than raising or certifying
    the connector TYPE's full set — the same fail-closed contract ``ConnectorACL``
    applies to a malformed ``allowed_operations``.
    """
    row = _row_connector(uuid.uuid4(), [])
    row.connector_type_id = None
    assert not _capabilities_for_connector(row)


def test_capabilities_for_connector_malformed_allowlist_certifies_nothing():
    """FAR-1564 fail-closed: a MALFORMED non-list ``allowed_operations`` is
    RESTRICTED.

    Before the shared predicate, ``isinstance(allowed, list) and allowed``
    sent EVERY non-list value (dict/str/int/...) down the unrestricted branch,
    so a malformed value certified the connector TYPE's full capability set
    while ``ConnectorACL`` read the same value restrictively — the exact
    contradiction the module's fail-closed contract forbids.
    """
    row = _row_connector(uuid.uuid4(), [])
    row.allowed_operations = {"read": 1}  # malformed: dict, not a list
    row.connector_type_id = "github"
    assert not _capabilities_for_connector(row)


def test_capabilities_for_connector_malformed_string_certifies_nothing():
    row = _row_connector(uuid.uuid4(), [])
    row.allowed_operations = "read"  # malformed: str, not a list
    row.connector_type_id = "github"
    assert not _capabilities_for_connector(row)


def test_capabilities_for_connector_malformed_allowlist_is_logged(caplog):
    """The malformed read is logged, not silently swallowed (fail closed loudly)."""
    row = _row_connector(uuid.uuid4(), [])
    row.allowed_operations = 7  # malformed: int, not a list
    row.connector_type_id = "github"
    with caplog.at_level(logging.WARNING):
        assert not _capabilities_for_connector(row)
    assert "guardrail.conformance.allowed_operations_malformed" in caplog.text


def test_capabilities_for_connector_empty_capability_type_is_logged(caplog):
    """FAR-1564 FIX D: a KNOWN type whose capability mapping is empty
    (``custom``) certifies nothing — AND is logged the same way an unknown
    type id is, so an empty-capability surface is never silent."""
    row = _row_connector(uuid.uuid4(), [])
    row.allowed_operations = []
    row.connector_type_id = "custom"
    assert not ConnectorType("custom").capabilities
    with caplog.at_level(logging.WARNING):
        assert not _capabilities_for_connector(row)
    assert "guardrail.conformance.connector_type_no_capabilities" in caplog.text
    assert "guardrail.conformance.connector_type_unknown" not in caplog.text


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
# Canonical capability vocabulary + unrestricted/allowlisted parity (FAR-1582)
# ---------------------------------------------------------------------------


async def _manifest_for(monkeypatch: pytest.MonkeyPatch, row: Any) -> dict[str, bool | None]:
    session = _manifest_session(connectors=[row])
    _patch_select(monkeypatch, session)
    return await build_live_manifest(
        session,
        org_id=_ORG_ID,
        connector_instance_ids=[row.id],
        environment_profile_id=None,
        agent_id=None,
    )


@pytest.mark.parametrize(
    ("spelling", "expected"),
    [
        ("read", "read"),
        ("write", "write"),
        ("github.read", "read"),
        ("github.write", "write"),
        ("linear:create_pr", "create_pr"),
        ("sandbox.egress", None),
        ("egress:github.com", None),
        ("not-a-capability", None),
        # A valid connector-type prefix with a non-capability suffix resolves to
        # None in BOTH accepted separators — no spelling yields a capability.
        ("github.bogus", None),
        ("github:notacap", None),
    ],
    ids=[
        "bare-read",
        "bare-write",
        "type-dotted",
        "type-colon",
        "type-prefixed-colon",
        "sandbox-surface-untouched",
        "agent-egress-untouched",
        "junk",
        "type-dotted-invalid-suffix",
        "type-colon-invalid-suffix",
    ],
)
def test_canonical_capability_vocabulary(spelling: str, expected: str | None) -> None:
    """ONE vocabulary: bare ``Capability`` values; non-connector caps pass through."""
    assert canonical_capability(spelling) == expected


def test_canonical_capability_list_non_list_is_empty() -> None:
    """A non-list is not a capability list, so it certifies nothing.

    Guards the defensive arm of the SHARED ``canonical_capability_set`` (moved
    to ``connectors.base`` in FAR-1594 so ``ConnectorACL`` reads a stored
    allowlist the same way): the caller routes only ``isinstance(allowed, list)``
    here, but the helper must still handle a malformed value fail-closed rather
    than raise.
    """
    assert not canonical_capability_set("read")
    assert not canonical_capability_set(None)


def test_canonical_capability_list_drops_non_string_and_non_capability(caplog) -> None:
    """Non-string entries are skipped; a string that is not a capability in any
    accepted spelling is DROPPED (and logged) — it grants nothing. A same-type
    qualified entry grants its bare capability (FAR-1594), so the surface type
    is supplied (FAR-1616) to verify the qualifier."""
    with caplog.at_level(logging.WARNING):
        result = canonical_capability_set(["read", 123, "junk", "github.write"], connector_type_id="github")
    assert result == {"read", "write"}
    assert "connectors.capability.operation_not_a_capability" in caplog.text


def test_canonical_capability_list_rejects_mis_typed_and_unverifiable_qualifier(caplog) -> None:
    """FAR-1616: a type-qualified entry grants ONLY on a same-type surface.

    A qualifier naming a DIFFERENT type than the surface — or one that cannot
    be verified because no surface type was supplied — is REJECTED (fail
    closed, logged), never reduced to a bare grant.
    """
    with caplog.at_level(logging.WARNING):
        mis_typed = canonical_capability_set(["github.write"], connector_type_id="filesystem")
        unverifiable = canonical_capability_set(["github.write"])
    assert not mis_typed
    assert not unverifiable
    assert "connectors.capability.operation_type_mismatch" in caplog.text


def test_decide_conformance_dedupes_canonical_claims() -> None:
    """Two SPELLINGS of one qualified claim collapse to ONE position.

    FAR-1594(b): a type-qualified claim is a binding position of its own (it is
    never merged with the bare ``read`` position — see the test below), so the
    dedupe that matters is between spellings of THAT claim: ``github.read`` and
    ``github:read`` are one position, matched against the qualified alias the
    manifest stamps for a github-typed surface.
    """
    d = decide_conformance(["github.read", "github:read"], {"github.read": True})
    assert d.state == "present"
    assert d.claimed is True
    assert not d.missing


def test_decide_conformance_qualified_claim_is_not_the_bare_position() -> None:
    """FAR-1594(b): ``github.read`` and ``read`` are DIFFERENT positions.

    Before FAR-1594 both canonicalised to ``read``, so a claim requiring
    ``github.read`` was satisfied by any surface declaring bare ``read`` (or by
    a manifest key of either spelling). The qualified position now needs the
    manifest's qualified alias — a hand-built ``{"read": True}`` carries none,
    so it fails CLOSED (unknown) while the bare claim is present.
    """
    qualified = decide_conformance(["github.read"], {"read": True})
    bare = decide_conformance(["read"], {"read": True})

    assert qualified.state == "unknown"
    assert bare.state == "present"


def test_decide_conformance_merges_multiple_spellings_of_one_capability() -> None:
    """One capability declared under several spellings merges to a single state:
    confirmed-present wins, else confirmed-absent, else unknown."""
    present = decide_conformance(["read"], {"read": True, "github.read": False})
    assert present.state == "present"
    absent = decide_conformance(["write"], {"write": False, "github.write": None})
    assert absent.state == "absent"


async def test_parity_canonical_claim_matches_unrestricted_and_allowlisted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A canonical claim is satisfied identically for EITHER connector branch.

    An UNRESTRICTED instance (``allowed_operations`` unset/empty) surfaces its
    connector TYPE's bare ``Capability`` set; an ALLOWLISTED one surfaces its
    declared operations reduced to that same bare spelling. Before FAR-1582
    the two branches emitted different vocabularies, so a claim spelled the
    legacy way (``github.read``) matched only one of them.
    """
    unrestricted = _row_connector(uuid.uuid4(), [])
    unrestricted.connector_type_id = "github"
    # FAR-1594(b): a type-qualified claim binds to the surface's TYPE, so the
    # allowlisted half must be a github-typed surface too — the claim names
    # github, and the manifest stamps its qualified alias from this id.
    allowlisted = _row_connector(uuid.uuid4(), ["read"])
    allowlisted.connector_type_id = "github"

    unrestricted_manifest = await _manifest_for(monkeypatch, unrestricted)
    allowlisted_manifest = await _manifest_for(monkeypatch, allowlisted)

    for manifest in (unrestricted_manifest, allowlisted_manifest):
        assert decide_conformance(["read"], manifest).state == "present"
        # The legacy type-qualified spelling resolves to the same capability.
        assert decide_conformance(["github.read"], manifest).state == "present"


async def test_build_live_manifest_malformed_allowlist_certifies_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A malformed ``allowed_operations`` fails CLOSED — never the type set."""
    row = _row_connector(uuid.uuid4(), [])
    row.allowed_operations = {"read": True}
    row.connector_type_id = "github"

    registered = await _manifest_for(monkeypatch, row)

    assert not registered


async def test_build_live_manifest_unrestricted_unknown_type_certifies_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unrestricted instance of a type we cannot identify grants nothing."""
    row = _row_connector(uuid.uuid4(), [])
    row.connector_type_id = "not-a-real-connector-type"

    registered = await _manifest_for(monkeypatch, row)

    assert not registered


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
    row = _row_connector(cid, ["github.write"])
    # FAR-1594(b): the claim is type-qualified, so the surface it certifies
    # must be github-typed for the manifest to stamp the binding alias.
    row.connector_type_id = "github"
    session = _manifest_session(connectors=[row])
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
    assert result.review_id == "guardrail_conformance_check_failed"
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
# node's actual enforced config, stamped into the manifest with conformance
# polarity (a block guardrail's required_capabilities on the sandbox surface is
# a deny/negative guarantee — confirmed-absent write/egress is the
# certification). Only ``sandbox.egress`` is genuinely mechanical today:
# ``sandbox.write_files`` and ``sandbox.git_credentials`` have no validated,
# enforced product surface yet (PipelineGraphNode has no such field; node_runner
# never reads them), so they always resolve unknown (fail-closed) — a derivative
# value would certify an unenforced deny-guarantee and fail open via the raw
# workflow-import path.
# ---------------------------------------------------------------------------


def _sandbox_node(**overrides: Any) -> dict[str, Any]:
    node: dict[str, Any] = {"id": "node-1", "node_type": "sandbox_agent", "agent_prompt": "p", "agent_commands": ["c"]}
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


async def test_build_live_manifest_sandbox_write_surface_readonly_certified():
    """PR B: read_only is now a real validated + enforced field, so a read-only
    sandbox mechanically certifies sandbox.write_files (the deny-guarantee that
    writes are impossible) -> the manifest's sandbox.write_files is True and a
    block guardrail is PRESENT (certified, not blocked)."""
    registered = await build_live_manifest(
        AsyncMock(),
        org_id=_ORG_ID,
        connector_instance_ids=[],
        environment_profile_id=None,
        agent_id=None,
        node_def=_sandbox_node(read_only=True),
    )
    assert registered.get("sandbox.write_files") is True
    derivation = decide_conformance(["sandbox.write_files"], registered)
    assert derivation.state == "present"


async def test_build_live_manifest_sandbox_git_credentials_scoped_certified():
    """PR B: git_credentials is now a real validated + enforced field, so a
    scoped git credential mechanically certifies sandbox.git_credentials (the
    positive guarantee that credentials are limited) -> the manifest's
    sandbox.git_credentials is True (present, certified)."""
    registered = await build_live_manifest(
        AsyncMock(),
        org_id=_ORG_ID,
        connector_instance_ids=[],
        environment_profile_id=None,
        agent_id=None,
        node_def=_sandbox_node(git_credentials="scoped"),
    )
    assert registered.get("sandbox.git_credentials") is True


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


async def test_build_live_manifest_sandbox_plumbs_profile_network_policy(monkeypatch: pytest.MonkeyPatch):
    """FAR-1085: a sandbox node with a bound profile plumbs the profile's
    network_policy into the canonical egress resolution, so the certified
    capability matches the runtime outcome (profile 'none' -> deny_all)."""
    pid = uuid.uuid4()
    row = _row_profile(pid, ["sandbox.e2b"])
    row.network_policy = "none"
    session = _manifest_session(profile=row)
    _patch_select(monkeypatch, session)
    registered = await build_live_manifest(
        session,
        org_id=_ORG_ID,
        connector_instance_ids=[],
        environment_profile_id=pid,
        agent_id=None,
        node_def=_sandbox_node(),  # egress_policy unset -> the profile fills it
    )
    assert registered.get("sandbox.egress") is True


async def test_build_live_manifest_sandbox_plumbs_profile_provider_tier(monkeypatch: pytest.MonkeyPatch):
    """FAR-1051: the bound profile's provider_type decides the tier the egress
    capability is certified under.

    A kubernetes profile with network_policy='none' certifies UNKNOWN (the
    Kubernetes tier cannot enforce deny_all — dispatch refuses the same
    combination), not the enforced-False the e2b reference tier would claim.
    """
    pid = uuid.uuid4()
    row = _row_profile(pid, ["sandbox.e2b"])
    row.network_policy = "none"
    row.provider_type = "kubernetes"
    session = _manifest_session(profile=row)
    _patch_select(monkeypatch, session)
    registered = await build_live_manifest(
        session,
        org_id=_ORG_ID,
        connector_instance_ids=[],
        environment_profile_id=pid,
        agent_id=None,
        node_def=_sandbox_node(),  # egress_policy unset -> the profile fills it
    )
    assert registered.get("sandbox.egress") is None
    derivation = decide_conformance(["sandbox.egress"], registered)
    assert derivation.state == "unknown"


async def test_build_live_manifest_sandbox_profile_missing_uses_node_only(monkeypatch: pytest.MonkeyPatch):
    """A missing profile row leaves profile_network_policy None, so the sandbox
    capability falls back to the node-only derivation (never a crash)."""
    pid = uuid.uuid4()
    session = _manifest_session(profile=None)
    _patch_select(monkeypatch, session)
    registered = await build_live_manifest(
        session,
        org_id=_ORG_ID,
        connector_instance_ids=[],
        environment_profile_id=pid,
        agent_id=None,
        node_def=_sandbox_node(egress_policy="deny_all"),
    )
    assert registered.get("sandbox.egress") is True


async def test_build_live_manifest_sandbox_profile_read_failure_falls_back():
    """A failed profile network_policy read is swallowed (debug-logged) and the
    node-only resolution is used — never a crash, never a silent grant."""
    session = AsyncMock()
    session.execute = AsyncMock(side_effect=RuntimeError("db down"))
    registered = await build_live_manifest(
        session,
        org_id=_ORG_ID,
        connector_instance_ids=[],
        environment_profile_id=uuid.uuid4(),
        agent_id=None,
        node_def=_sandbox_node(egress_policy="deny_all"),
    )
    assert registered.get("sandbox.egress") is True


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


async def test_check_node_start_sandbox_write_claim_readonly_certified(monkeypatch: pytest.MonkeyPatch):
    """PR B: a block guardrail requiring sandbox.write_files on a READ-ONLY
    sandbox is CERTIFIED (present) — the read-only workspace makes writes
    impossible, so the deny-guarantee holds."""
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


async def test_check_node_start_sandbox_git_credentials_claim_scoped_certified(monkeypatch: pytest.MonkeyPatch):
    """PR B: a block guardrail requiring sandbox.git_credentials on a SCOPED
    git-credential sandbox is CERTIFIED (present) — the scoped helper limits
    the credential to the allowlisted host, so the positive guarantee holds."""
    result = await _run_hoisted_check(
        monkeypatch,
        guardrails=[_gr("g_scopedgit", "block", ["sandbox.git_credentials"])],
        node_def=_sandbox_node(git_credentials="scoped"),
    )
    assert result.blocked is False
    assert result.state == "present"
    assert result.claimed is True


# ---------------------------------------------------------------------------
# Round-trip (FAR-212 PR A review): a REAL API-validated node through
# ``PipelineGraphNode`` can never certify ``sandbox.write_files`` /
# ``sandbox.git_credentials``. ``PipelineGraphNode`` does not declare the
# ``read_only`` / ``git_credentials`` fields (Pydantic ``extra="ignore"``
# silently drops them on the REST/MCP paths) and node_runner/e2b enforce
# neither, so the derivation must resolve both capabilities unknown — a
# smuggled ``read_only: true`` / ``git_credentials: "scoped"`` in an imported
# workflow must not certify a deny-guarantee nothing enforces.
# ---------------------------------------------------------------------------


def _api_validated_sandbox_node() -> dict[str, Any]:
    """Build a node the way the REST/MCP API would: through PipelineGraphNode.

    Returns the ``model_dump()`` the runtime node_def is built from. PR B added
    ``read_only`` / ``git_credentials`` as real PipelineGraphNode fields, so they
    round-trip and the derivation mechanically certifies them.
    """
    from modulo.api.routes.pipelines import PipelineGraphNode

    node = PipelineGraphNode.model_validate(
        {
            "id": str(uuid.uuid4()),
            "node_type": "sandbox_agent",
            "agent_id": None,
            "position": {"x": 10, "y": 20},
            "connector_binding": None,
            "agent_prompt": "Do the thing",
            "agent_commands": ["opencode run --auto < /home/user/prompt.md"],
            "template_id": "opencode",
            "egress_policy": "deny_all",
            # Smuggled keys — NOT PipelineGraphNode fields; extra="ignore" drops them.
            "read_only": True,
            "git_credentials": "scoped",
        }
    )
    dumped = node.model_dump()
    # PR B: read_only / git_credentials are now real PipelineGraphNode fields,
    # so they round-trip (not dropped).
    assert dumped["read_only"] is True
    assert dumped["git_credentials"] == "scoped"
    assert dumped["egress_policy"] == "deny_all"
    return dumped


async def test_round_trip_api_validated_node_write_and_git_certified():
    """A real API-validated node now resolves write_files/git_credentials
    CERTIFIED (PR B added the read_only/git_credentials fields + enforcement),
    while egress stays mechanical."""
    from modulo.core.pipeline_engine.sandbox_mode import (
        SANDBOX_CAPABILITY_EGRESS,
        SANDBOX_CAPABILITY_GIT_CREDENTIALS,
        SANDBOX_CAPABILITY_WRITE_FILES,
        derive_sandbox_capabilities,
    )

    node_def = _api_validated_sandbox_node()
    caps = derive_sandbox_capabilities(node_def)
    assert caps[SANDBOX_CAPABILITY_EGRESS] is False
    assert caps[SANDBOX_CAPABILITY_WRITE_FILES] is False
    assert caps[SANDBOX_CAPABILITY_GIT_CREDENTIALS] is True

    registered = await build_live_manifest(
        AsyncMock(),
        org_id=_ORG_ID,
        connector_instance_ids=[],
        environment_profile_id=None,
        agent_id=None,
        node_def=node_def,
    )
    # deny_all egress certified; read_only certifies write_files; scoped git certifies.
    assert registered.get("sandbox.egress") is True
    assert registered.get("sandbox.write_files") is True
    assert registered.get("sandbox.git_credentials") is True


async def test_check_node_start_round_trip_api_validated_node_certifies(monkeypatch: pytest.MonkeyPatch):
    """A fully API-validated node with read_only + scoped git now CERTIFIES the
    write/git block claims (PR B enforcement surface), and the egress claim is
    certified (deny_all)."""
    node_def = _api_validated_sandbox_node()

    write_ok = await _run_hoisted_check(
        monkeypatch,
        guardrails=[_gr("g_nowrite", "block", ["sandbox.write_files"])],
        node_def=node_def,
    )
    assert write_ok.blocked is False
    assert write_ok.state == "present"

    git_ok = await _run_hoisted_check(
        monkeypatch,
        guardrails=[_gr("g_scopedgit", "block", ["sandbox.git_credentials"])],
        node_def=node_def,
    )
    assert git_ok.blocked is False
    assert git_ok.state == "present"

    egress_ok = await _run_hoisted_check(
        monkeypatch,
        guardrails=[_gr("g_noegress", "block", ["sandbox.egress"])],
        node_def=node_def,
    )
    assert egress_ok.blocked is False
    assert egress_ok.state == "present"


# ---------------------------------------------------------------------------
# ONE vocabulary across certification AND enforcement (FAR-1594)
# ---------------------------------------------------------------------------


async def test_acl_and_conformance_agree_for_legacy_qualified_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The SAME stored value gets the SAME answer from both sides (defect (a)).

    ``ConnectorACL`` builds its allowlist from the SHARED
    ``canonical_capability_set`` the manifest reader uses, so a stored
    ``["github.read"]`` grants ``read`` at enforcement time exactly as it is
    certified at conformance time — before FAR-1594 conformance certified
    ``read`` while the ACL (and the polling read gate) denied it.
    """
    granted = _row_connector(uuid.uuid4(), ["github.read"])
    granted.connector_type_id = "github"
    granted_manifest = await _manifest_for(monkeypatch, granted)

    assert decide_conformance(["read"], granted_manifest).state == "present"
    assert (
        ConnectorACL(visibility="org", allowed_operations=["github.read"], connector_type_id="github").check("read")
        is None
    )

    # The DENY side agrees too: a write-only allowlist certifies no read and
    # the ACL denies the read — no half of the system can disagree.
    denied = _row_connector(uuid.uuid4(), ["github.write"])
    denied.connector_type_id = "github"
    denied_manifest = await _manifest_for(monkeypatch, denied)

    assert decide_conformance(["read"], denied_manifest).state == "unknown"
    with pytest.raises(ConnectorPermissionError, match="not in allowed_operations"):
        ConnectorACL(visibility="org", allowed_operations=["github.write"], connector_type_id="github").check("read")


async def test_acl_and_conformance_agree_for_mis_typed_legacy_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FAR-1616: the SAME mis-typed stored value is DENIED by both sides.

    ``["github.write"]`` on a FILESYSTEM connector grants nothing at
    enforcement time and certifies nothing at conformance time — before
    FAR-1616 both sides silently reduced it to bare ``write``.
    """
    mis_typed = _row_connector(uuid.uuid4(), ["github.write"], connector_type_id="filesystem")
    mis_typed_manifest = await _manifest_for(monkeypatch, mis_typed)

    assert not mis_typed_manifest
    assert decide_conformance(["write"], mis_typed_manifest).state == "unknown"
    acl = ConnectorACL(visibility="org", allowed_operations=["github.write"], connector_type_id="filesystem")
    with pytest.raises(ConnectorPermissionError, match="not in allowed_operations"):
        acl.check("write")

    # The same-type surface still grants, on both sides.
    same_type = _row_connector(uuid.uuid4(), ["github.write"], connector_type_id="github")
    same_type_manifest = await _manifest_for(monkeypatch, same_type)
    assert decide_conformance(["write"], same_type_manifest).state == "present"
    assert (
        ConnectorACL(visibility="org", allowed_operations=["github.write"], connector_type_id="github").check("write")
        is None
    )


async def test_type_qualified_claim_binds_to_its_surface_type(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ``github.read`` claim is satisfied ONLY by a github surface (defect (b)).

    The manifest carries each connector surface's type, so a type-qualified
    claim is a BINDING request: before FAR-1594 the qualifier was dropped and
    the claim was satisfied by ANY bound surface declaring bare ``read`` (an
    unrestricted rest/filesystem connector, or a linear connector allowlisted
    to ``read``).
    """
    github = _row_connector(uuid.uuid4(), [])
    github.connector_type_id = "github"
    rest = _row_connector(uuid.uuid4(), [])
    rest.connector_type_id = "rest"
    linear = _row_connector(uuid.uuid4(), ["read"])
    linear.connector_type_id = "linear"

    github_manifest = await _manifest_for(monkeypatch, github)
    rest_manifest = await _manifest_for(monkeypatch, rest)
    linear_manifest = await _manifest_for(monkeypatch, linear)

    # A BARE claim keeps its "any surface declares it" semantics.
    assert decide_conformance(["read"], rest_manifest).state == "present"

    # The qualified claim binds to the github-typed surface ...
    assert decide_conformance(["github.read"], github_manifest).state == "present"
    # ... and is NOT satisfied by a non-github surface declaring bare ``read``.
    assert decide_conformance(["github.read"], rest_manifest).state == "unknown"
    assert decide_conformance(["github.read"], linear_manifest).state == "unknown"


# ---------------------------------------------------------------------------
# The type-binding invariant across NON-CONNECTOR surfaces (FAR-1615)
# ---------------------------------------------------------------------------
#
# FAR-1594 stamped the ``<type>.<cap>`` alias only for CONNECTOR surfaces. A
# profile/agent declaring the literal string ``github.read`` used to register
# that key VERBATIM, so it satisfied a type-qualified connector claim with no
# github-typed connector bound. Profile/agent capabilities are now reduced to
# the BARE vocabulary — a type-qualified claim requires a connector surface.


async def test_profile_declaring_qualified_capability_cannot_satisfy_qualified_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A profile declaring ``github.read`` registers bare ``read`` only."""
    pid = uuid.uuid4()
    session = _manifest_session(profile=_row_profile(pid, ["github.read"]))
    _patch_select(monkeypatch, session)
    registered = await build_live_manifest(
        session,
        org_id=_ORG_ID,
        connector_instance_ids=[],
        environment_profile_id=pid,
        agent_id=None,
    )
    # The bare capability is declared ...
    assert decide_conformance(["read"], registered).state == "present"
    # ... but the type-qualified claim is NOT satisfied: a profile is not a
    # connector surface and stamps no ``github.read`` alias.
    assert "github.read" not in registered
    assert decide_conformance(["github.read"], registered).state == "unknown"


async def test_agent_declaring_qualified_capability_cannot_satisfy_qualified_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An agent declaring ``github.read`` registers bare ``read`` only."""
    aid = uuid.uuid4()
    session = _manifest_session(agent=_row_agent(aid, ["github.read"]))
    _patch_select(monkeypatch, session)
    registered = await build_live_manifest(
        session,
        org_id=_ORG_ID,
        connector_instance_ids=[],
        environment_profile_id=None,
        agent_id=aid,
    )
    assert decide_conformance(["read"], registered).state == "present"
    assert "github.read" not in registered
    assert decide_conformance(["github.read"], registered).state == "unknown"


def test_capabilities_for_profile_reduces_qualified_spelling_to_bare() -> None:
    """Direct unit: profile ``capabilities_json`` is a BARE vocabulary."""
    row = _row_profile(uuid.uuid4(), ["github.read", "sandbox.egress", "git"])
    assert _capabilities_for_profile(row) == {"read", "sandbox.egress", "git"}


def test_capabilities_for_agent_reduces_qualified_spelling_to_bare() -> None:
    """Direct unit: agent ``required_environment_capabilities`` is a BARE vocabulary."""
    row = _row_agent(uuid.uuid4(), ["linear:ticket_read", "shell"])
    assert _capabilities_for_agent(row) == {"ticket_read", "shell"}


def test_reported_detail_preserves_the_type_binding() -> None:
    """FAR-1615: a blocked qualified claim reports its QUALIFIED name.

    Before FAR-1615 ``missing``/``unreadable`` rewrote ``github.read`` to
    ``read``, so the operator could not tell which binding failed when a bare
    ``read`` was also present.
    """
    absent = decide_conformance(["github.write"], {"github.write": False})
    assert absent.missing == ("github.write",)
    unknown = decide_conformance(["github.read"], {})
    assert unknown.unreadable == ("github.read",)
    # A blocked EVALUATION surfaces the qualified name too.
    result = evaluate_conformance([_gr("g_block", "block", ["github.read"])], {})
    assert result.blocked is True
    assert result.state == "unknown"


# ---------------------------------------------------------------------------
# Legacy type-qualified claim detection (FAR-1617)
# ---------------------------------------------------------------------------


def test_type_qualified_claims_classifies_legacy_spellings() -> None:
    """The FAR-1617 detection helper: qualified claims only, normalised and
    de-duplicated — the same binding spelled ``github.read`` and ``github:read``
    collapses to one claim."""
    assert type_qualified_claims(
        ["read", "github.read", "github:read", "github:write", "sandbox.egress", "docker"]
    ) == [
        "github.read",
        "github.write",
    ]
    assert not type_qualified_claims(["read", "ticket_read"])


def test_find_type_qualified_claim_guardrails_maps_names_to_claims() -> None:
    """Guardrail-level audit view: only guardrails carrying a qualified claim."""
    guardrails = [
        _gr("g_legacy", "block", ["github.read", "read"]),
        _gr("g_bare", "block", ["read"]),
        _gr("g_none", "warn", None),
    ]
    assert find_type_qualified_claim_guardrails(guardrails) == {"g_legacy": ["github.read"]}


def test_evaluate_conformance_logs_unsatisfied_qualified_claim(caplog) -> None:
    """A qualified claim no bound surface of that type satisfies is logged, so
    a rollout surfaces legacy claims without a stored-data audit."""
    gr = _gr("g_legacy", "block", ["github.read"])
    with caplog.at_level(logging.WARNING):
        result = evaluate_conformance([gr], {"read": True})
    assert result.blocked is True
    assert "guardrail.conformance.type_qualified_claim_unsatisfied" in caplog.text
    detection_records = [r for r in caplog.records if r.name == "modulo.core.guardrails.conformance"]
    assert any(getattr(r, "claims", None) == ["github.read"] for r in detection_records)


def test_evaluate_conformance_no_qualified_log_when_satisfied(caplog) -> None:
    """A SATISFIED qualified claim logs nothing extra."""
    gr = _gr("g_bound", "block", ["github.read"])
    with caplog.at_level(logging.WARNING):
        result = evaluate_conformance([gr], {"github.read": True})
    assert result.blocked is False
    assert result.state == "present"
    assert "guardrail.conformance.type_qualified_claim_unsatisfied" not in caplog.text
