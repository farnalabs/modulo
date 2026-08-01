"""ADR 017 service-layer backstop tests.

Verifies that the guarded pipeline write paths (``replace_pipeline_graph``,
``rollback_to_snapshot``) require an explicit ``is_privileged`` keyword-only
argument, and that every call site across the REST/MCP/onboarding layers
declares the flag. The HITL gate guard itself (gate-weakening block) ships
separately with hitl-gate-removal-guard-plan.md — this module tests the
plumbing that guard will consume.
"""

from __future__ import annotations

import ast
import inspect
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from modulo.api.routes.pipelines import _is_privileged
from modulo.db.crud.pipeline import replace_pipeline_graph
from modulo.db.crud.pipeline_snapshot_versioning import rollback_to_snapshot

_SRC_ROOT = Path(__file__).resolve().parents[3] / "src" / "modulo"


# ---------------------------------------------------------------------------
# is_privileged is a required keyword-only argument
# ---------------------------------------------------------------------------


def test_replace_pipeline_graph_is_privileged_keyword_only_no_default() -> None:
    sig = inspect.signature(replace_pipeline_graph)
    param = sig.parameters["is_privileged"]
    assert param.kind == inspect.Parameter.KEYWORD_ONLY
    assert param.default is inspect.Parameter.empty


def test_rollback_to_snapshot_is_privileged_keyword_only_no_default() -> None:
    sig = inspect.signature(rollback_to_snapshot)
    param = sig.parameters["is_privileged"]
    assert param.kind == inspect.Parameter.KEYWORD_ONLY
    assert param.default is inspect.Parameter.empty


async def test_replace_pipeline_graph_requires_is_privileged() -> None:
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


async def test_replace_pipeline_graph_accepts_is_privileged_true() -> None:
    result = await replace_pipeline_graph(
        _session_with_no_pipeline(),
        pipeline_id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        nodes=[],
        edges=[],
        is_privileged=True,
    )
    assert result is None


async def test_replace_pipeline_graph_accepts_is_privileged_false() -> None:
    result = await replace_pipeline_graph(
        _session_with_no_pipeline(),
        pipeline_id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        nodes=[],
        edges=[],
        is_privileged=False,
    )
    assert result is None


async def test_rollback_to_snapshot_accepts_is_privileged_true() -> None:
    result = await rollback_to_snapshot(
        _session_with_no_pipeline(),
        uuid.uuid4(),
        uuid.uuid4(),
        is_privileged=True,
    )
    assert result is None


async def test_rollback_to_snapshot_accepts_is_privileged_false() -> None:
    result = await rollback_to_snapshot(
        _session_with_no_pipeline(),
        uuid.uuid4(),
        uuid.uuid4(),
        is_privileged=False,
    )
    assert result is None


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


def test_all_call_sites_pass_is_privileged() -> None:
    missing: list[str] = []
    for rel in _CALL_SITE_FILES:
        path = _SRC_ROOT / rel
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not isinstance(node.func, ast.Name):
                continue
            if node.func.id not in _GUARDED_CALLS:
                continue
            keywords = {kw.arg for kw in node.keywords}
            if "is_privileged" not in keywords:
                missing.append(f"{rel}:{node.lineno}: {node.func.id}()")
    assert not missing, f"Call sites missing is_privileged=: {missing}"


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
