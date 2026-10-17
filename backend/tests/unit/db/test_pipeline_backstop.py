"""ADR 047 service-layer backstop tests.

Verifies that the guarded pipeline write paths carry an explicit actor/auth
context: ``replace_pipeline_graph`` requires a keyword-only ``GraphWriteActor``
(FAR-1859) whose ``is_privileged`` / ``caller_type`` fields have no defaults,
and ``rollback_to_snapshot`` still requires ``is_privileged`` /
``caller_type`` directly. It also checks every call site across the
REST/MCP/onboarding layers declares that context. The HITL gate guard itself
(gate-weakening block) ships separately with
hitl-gate-removal-guard-plan.md — this module tests the plumbing that guard
will consume.
"""

from __future__ import annotations

import ast
import inspect
import uuid
from dataclasses import MISSING, fields
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from modulo.api.routes.pipelines import _is_privileged
from modulo.db.crud.pipeline import GraphWriteActor, replace_pipeline_graph
from modulo.db.crud.pipeline_snapshot_versioning import rollback_to_snapshot

_SRC_ROOT = Path(__file__).resolve().parents[3] / "src" / "modulo"


# ---------------------------------------------------------------------------
# The actor/auth context is a required keyword-only argument (FAR-1859): the
# six request-resolved values ride on one required ``GraphWriteActor``, and its
# ``is_privileged`` / ``caller_type`` fields are required (no defaults), so a
# call site cannot silently drop its privilege context.
# ---------------------------------------------------------------------------


def test_replace_pipeline_graph_actor_keyword_only_no_default() -> None:
    sig = inspect.signature(replace_pipeline_graph)
    param = sig.parameters["actor"]
    assert param.kind == inspect.Parameter.KEYWORD_ONLY
    assert param.default is inspect.Parameter.empty


def test_graph_write_actor_requires_is_privileged() -> None:
    field = next(f for f in fields(GraphWriteActor) if f.name == "is_privileged")
    assert field.default is MISSING


def test_graph_write_actor_requires_caller_type() -> None:
    field = next(f for f in fields(GraphWriteActor) if f.name == "caller_type")
    assert field.default is MISSING


def test_graph_write_actor_fields_are_keyword_only() -> None:
    """``GraphWriteActor`` is keyword-only and its required fields stay required.

    ``@dataclass(frozen=True, kw_only=True)`` is load-bearing (FAR-1859): the
    adjacent ``bool`` and ``str`` fields would otherwise be constructible
    positionally, so a swapped call would silently build a privileged actor
    from unprivileged input — ``GraphWriteActor(True, "rest")`` binding
    ``True`` to ``is_privileged`` only by luck of field order. Removing
    ``kw_only=True`` must fail this test. The required-field assertions pin
    that keyword-only does not quietly make the fields optional.
    """
    with pytest.raises(TypeError):
        GraphWriteActor(True, "rest")  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        GraphWriteActor(False, "mcp")  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        GraphWriteActor()  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        GraphWriteActor(is_privileged=True)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        GraphWriteActor(caller_type="rest")  # type: ignore[call-arg]


def test_rollback_to_snapshot_is_privileged_keyword_only_no_default() -> None:
    sig = inspect.signature(rollback_to_snapshot)
    param = sig.parameters["is_privileged"]
    assert param.kind == inspect.Parameter.KEYWORD_ONLY
    assert param.default is inspect.Parameter.empty


async def test_replace_pipeline_graph_requires_actor() -> None:
    session = AsyncMock()
    with pytest.raises(TypeError):
        await replace_pipeline_graph(
            session,
            pipeline_id=uuid.uuid4(),
            org_id=uuid.uuid4(),
            nodes=[],
            edges=[],
        )


async def test_rollback_to_snapshot_requires_is_privileged() -> None:
    session = AsyncMock()
    with pytest.raises(TypeError):
        await rollback_to_snapshot(session, uuid.uuid4(), uuid.uuid4())


# ---------------------------------------------------------------------------
# The parameter is plumbed: True/False both accepted without error
# ---------------------------------------------------------------------------


def _session_with_no_pipeline() -> AsyncMock:
    session = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    session.execute = AsyncMock(return_value=result)
    return session


async def test_replace_pipeline_graph_accepts_actor_is_privileged_true() -> None:
    result = await replace_pipeline_graph(
        _session_with_no_pipeline(),
        pipeline_id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        nodes=[],
        edges=[],
        actor=GraphWriteActor(is_privileged=True, caller_type="rest"),
    )
    assert result is None


async def test_replace_pipeline_graph_accepts_actor_is_privileged_false() -> None:
    result = await replace_pipeline_graph(
        _session_with_no_pipeline(),
        pipeline_id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        nodes=[],
        edges=[],
        actor=GraphWriteActor(is_privileged=False, caller_type="rest"),
    )
    assert result is None


async def test_rollback_to_snapshot_accepts_is_privileged_true() -> None:
    result = await rollback_to_snapshot(
        _session_with_no_pipeline(),
        uuid.uuid4(),
        uuid.uuid4(),
        is_privileged=True,
        caller_type="rest",
    )
    assert result is None


async def test_rollback_to_snapshot_accepts_is_privileged_false() -> None:
    result = await rollback_to_snapshot(
        _session_with_no_pipeline(),
        uuid.uuid4(),
        uuid.uuid4(),
        is_privileged=False,
        caller_type="rest",
    )
    assert result is None


# ---------------------------------------------------------------------------
# caller_type is a required actor field (hitl-gate-removal-guard-plan.md v19
# §3 item 5). After FAR-1859 it rides on ``GraphWriteActor``; the MCP literal
# check further down pins that the MCP call site still passes it as a literal.
# ---------------------------------------------------------------------------


def test_graph_write_actor_caller_type_required_at_construction() -> None:
    with pytest.raises(TypeError):
        GraphWriteActor(is_privileged=True)  # type: ignore[call-arg]


def test_rollback_to_snapshot_caller_type_keyword_only_no_default() -> None:
    sig = inspect.signature(rollback_to_snapshot)
    param = sig.parameters["caller_type"]
    assert param.kind == inspect.Parameter.KEYWORD_ONLY
    assert param.default is inspect.Parameter.empty


async def test_replace_pipeline_graph_requires_full_actor_context() -> None:
    """A caller cannot omit the caller type: the actor refuses to construct
    without it, so no graph write can run with a defaulted caller context."""
    session = AsyncMock()
    with pytest.raises(TypeError):
        await replace_pipeline_graph(
            session,
            pipeline_id=uuid.uuid4(),
            org_id=uuid.uuid4(),
            nodes=[],
            edges=[],
            actor=GraphWriteActor(is_privileged=True),  # type: ignore[call-arg]
        )


async def test_rollback_to_snapshot_requires_caller_type() -> None:
    session = AsyncMock()
    with pytest.raises(TypeError):
        await rollback_to_snapshot(session, uuid.uuid4(), uuid.uuid4(), is_privileged=True)


# ---------------------------------------------------------------------------
# Every call site declares is_privileged (structural backstop complementing the
# semgrep rule). The semgrep rule enforces this on commit; this test makes the
# invariant visible in the unit suite and fails loudly if a future call site
# forgets the flag.
# ---------------------------------------------------------------------------

_CALL_SITE_FILES = (
    "api/routes/pipelines.py",
    "api/routes/onboarding.py",
    "api/mcp_server.py",
)

_GUARDED_CALLS = ("replace_pipeline_graph", "rollback_to_snapshot")


def _guarded_calls(tree: ast.AST) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _GUARDED_CALLS
    ]


def _actor_kwarg(node: ast.Call) -> ast.Call | None:
    """Return the ``actor=GraphWriteActor(...)`` call on a graph-write call."""
    for kw in node.keywords:
        if kw.arg == "actor" and isinstance(kw.value, ast.Call):
            return kw.value
    return None


def test_all_call_sites_pass_is_privileged() -> None:
    """Every guarded write declares its privilege context.

    ``replace_pipeline_graph`` carries it on the required ``actor`` object
    (FAR-1859), so this asserts the nested ``GraphWriteActor(...)`` actually
    passes ``is_privileged`` — not merely that an ``actor=`` keyword exists;
    ``rollback_to_snapshot`` still takes ``is_privileged`` directly.
    """
    missing: list[str] = []
    for rel in _CALL_SITE_FILES:
        path = _SRC_ROOT / rel
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in _guarded_calls(tree):
            if node.func.id == "replace_pipeline_graph":
                actor = _actor_kwarg(node)
                if actor is None or not any(kw.arg == "is_privileged" for kw in actor.keywords):
                    missing.append(f"{rel}:{node.lineno}: {node.func.id}()")
            elif not any(kw.arg == "is_privileged" for kw in node.keywords):
                missing.append(f"{rel}:{node.lineno}: {node.func.id}()")
    assert not missing, f"Call sites missing privilege context: {missing}"


def test_all_call_sites_pass_caller_type() -> None:
    """Every guarded write declares its caller type.

    As above, this asserts the nested ``GraphWriteActor(...)`` actually passes
    ``caller_type`` for ``replace_pipeline_graph`` — not just the presence of
    the ``actor=`` keyword — and the direct ``caller_type`` argument for
    ``rollback_to_snapshot``.
    """
    missing: list[str] = []
    for rel in _CALL_SITE_FILES:
        path = _SRC_ROOT / rel
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in _guarded_calls(tree):
            if node.func.id == "replace_pipeline_graph":
                actor = _actor_kwarg(node)
                if actor is None or not any(kw.arg == "caller_type" for kw in actor.keywords):
                    missing.append(f"{rel}:{node.lineno}: {node.func.id}()")
            elif not any(kw.arg == "caller_type" for kw in node.keywords):
                missing.append(f"{rel}:{node.lineno}: {node.func.id}()")
    assert not missing, f"Call sites missing caller_type context: {missing}"


def test_mcp_call_site_passes_literal_mcp_caller_type() -> None:
    """The MCP call site must pass the literal "mcp", not a variable.

    Complements the .semgrep/hitl-gate-mcp-caller-type.yml rule with a
    structural check visible in the unit suite. After FAR-1859 the caller type
    rides inside the ``actor`` object, so the literal is asserted on the
    ``GraphWriteActor`` the MCP write constructs.
    """
    path = _SRC_ROOT / "api" / "mcp_server.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    violations: list[str] = []
    replace_calls = [n for n in _guarded_calls(tree) if n.func.id == "replace_pipeline_graph"]
    if not replace_calls:
        violations.append("api/mcp_server.py: no replace_pipeline_graph() call found")
    for node in replace_calls:
        actor = _actor_kwarg(node)
        if actor is None:
            violations.append(
                f"api/mcp_server.py:{node.lineno}: replace_pipeline_graph() has no actor=GraphWriteActor(...)"
            )
            continue
        caller_type = next((kw for kw in actor.keywords if kw.arg == "caller_type"), None)
        if caller_type is None or not isinstance(caller_type.value, ast.Constant) or caller_type.value.value != "mcp":
            violations.append(f"api/mcp_server.py:{node.lineno}: actor caller_type is not the literal 'mcp'")
    assert not violations, "\n".join(violations)


# ---------------------------------------------------------------------------
# Privilege resolution semantics (operator+ -> True). Assert on the actual
# helper both the REST routes and the MCP tool use (_is_privileged from
# pipelines.py) rather than re-implementing org_role_level(role) >=
# _OPERATOR_LEVEL, so a regression in the helper is caught here.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("role", "expected"),
    [
        ("viewer", False),
        ("runner", False),
        ("operator", True),
        ("admin", True),
        (None, False),
        ("", False),
        ("unknown_role", False),
    ],
)
def test_operator_plus_is_privileged_semantics(role: str | None, expected: bool) -> None:
    assert _is_privileged(role) is expected
