"""Unit tests for FAR-220 git-sourced content in ``modulo apply`` (increment 1).

Covers the pin-on-apply flow: a declarative ``git+<repo>[@<ref>]#<path>``
content ref on a sandbox_agent node is resolved to a pinned commit SHA at plan
time (identity for an already-pinned ref, ``git ls-remote`` seam for a movable
ref), the pinned form is what the desired managed view hashes and what the
write payload carries (hence what the run snapshot surfaces for audit), and
``--diff`` drift reports commit moves per content field.

Resolution failures and malformed refs BLOCK the entity (fail closed) —
never a silent unpinned write. HTTP mocked via respx where a write path is
exercised; round-trip payloads validate against the REAL API node model.
"""

from __future__ import annotations

import json
import uuid

import httpx
import pytest
import respx

from modulo.api.routes.pipelines import PipelineUpdate
from modulo.cli.apply import pipeline_apply
from modulo.cli.apply.__init__ import render_table
from modulo.cli.apply.drift import build_drift_detail, has_drift
from modulo.cli.apply.executor import ApplyExecutor
from modulo.cli.apply.loader import parse_apply_documents
from modulo.cli.apply.models import ApplyEntityResolutionError, PipelineEntity
from modulo.cli.apply.plan import KIND_PIPELINE, build_plan

_SHA_A = "a" * 40
_SHA_B = "b" * 40
_REPO = "https://github.com/example/repo.git"
_NODE_ID = "00000000-0000-0000-0000-0000000000c1"

_MOVABLE_PROMPT = f"git+{_REPO}@main#prompts/x.md"
_PINNED_PROMPT_A = f"git+{_REPO}@{_SHA_A}#prompts/x.md"
_PINNED_PROMPT_B = f"git+{_REPO}@{_SHA_B}#prompts/x.md"

CONFIG_TEXT = f"""
api_version: modulo.dev/v1
entities:
  pipelines:
    - name: git-content
      description: Git-sourced prompt pipeline
      max_concurrent_runs: 3
      graph:
        nodes:
          - id: {_NODE_ID}
            node_type: sandbox_agent
            mode: llm
            label: Git-sourced
            position: {{x: 0, y: 0}}
            template_id: opencode
            agent_prompt: "{_MOVABLE_PROMPT}"
            agent_commands: ["opencode run --auto < /home/user/prompt.md"]
        edges: []
"""


def _resolver(sha: str):
    def _resolve(ref) -> str:
        assert ref.ref == "main"
        return sha

    return _resolve


def _entity_set():
    return parse_apply_documents(CONFIG_TEXT).entities


def _pipeline_entity() -> PipelineEntity:
    return _entity_set().pipelines[0]


def _current_entities(*, stored_prompt: str) -> dict:
    """Current org state whose stored graph carries *stored_prompt*."""
    graph = pipeline_apply.normalize_current_graph(
        {
            "nodes": [
                {
                    "id": _NODE_ID,
                    "node_type": "sandbox_agent",
                    "mode": "llm",
                    "label": "Git-sourced",
                    "position": {"x": 0, "y": 0},
                    "template_id": "opencode",
                    "agent_prompt": stored_prompt,
                    "agent_commands": ["opencode run --auto < /home/user/prompt.md"],
                }
            ],
            "edges": [],
        }
    )
    return {
        "agents": {},
        "ambiguous_pipeline_names": {},
        "ambiguous_agent_names": {},
        KIND_PIPELINE: {
            "git-content": {
                "id": str(uuid.uuid4()),
                "description": "Git-sourced prompt pipeline",
                "max_concurrent_runs": 3,
                "graph": graph,
            }
        },
    }


def _plan(stored_prompt: str, resolver_sha: str) -> tuple[dict, dict, list]:
    """Run plan-phase resolution + build_plan against a stored graph."""
    entity_set = _entity_set()
    current = _current_entities(stored_prompt=stored_prompt)
    desired = {KIND_PIPELINE: []}
    blocked: list[tuple[str, str, str]] = []
    desired, blocked = pipeline_apply.build_desired_views(
        entity_set,
        current,
        desired,
        blocked,
        set(),
        git_resolver=_resolver(resolver_sha),
    )
    report = build_plan(desired, current, blocked)
    return report, desired, current


# ---------------------------------------------------------------------------
# resolve_graph — pinning
# ---------------------------------------------------------------------------


def test_resolve_graph_pins_movable_ref_with_injected_resolver() -> None:
    """Prove-the-fix: WITHOUT pinning the payload keeps @main (the pre-change
    behaviour); WITH the change it carries the resolver's commit SHA."""
    graph = pipeline_apply.resolve_graph(_pipeline_entity(), {}, git_resolver=_resolver(_SHA_A))
    node = graph["nodes"][0]
    assert node["agent_prompt"] == _PINNED_PROMPT_A


def test_resolve_graph_pinned_ref_skips_resolver() -> None:
    """An already-pinned (even uppercase) ref re-canonicalises with NO resolver call."""

    def _boom(_ref) -> str:
        raise AssertionError("resolver must not run for a pinned ref")

    entity = _pipeline_entity().model_copy(
        update={
            "graph": _pipeline_entity().graph.model_copy(
                update={
                    "nodes": [
                        n.model_copy(update={"agent_prompt": f"git+{_REPO}@{_SHA_A.upper()}#prompts/x.md"})
                        for n in _pipeline_entity().graph.nodes
                    ]
                }
            )
        }
    )
    graph = pipeline_apply.resolve_graph(entity, {}, git_resolver=_boom)
    assert graph["nodes"][0]["agent_prompt"] == _PINNED_PROMPT_A


def test_resolve_graph_malformed_ref_is_resolution_error() -> None:
    entity = _pipeline_entity().model_copy(
        update={
            "graph": _pipeline_entity().graph.model_copy(
                update={
                    "nodes": [
                        n.model_copy(update={"agent_prompt": "git+https://github.com/example/repo"})
                        for n in _pipeline_entity().graph.nodes
                    ]
                }
            )
        }
    )
    with pytest.raises(ApplyEntityResolutionError, match="git content ref"):
        pipeline_apply.resolve_graph(entity, {}, git_resolver=_resolver(_SHA_A))


def test_resolve_graph_resolution_failure_is_resolution_error() -> None:
    from modulo.core.pipeline_engine.git_content import GitContentRefError

    def _fail(_ref) -> str:
        raise GitContentRefError("remote unreachable")

    with pytest.raises(ApplyEntityResolutionError, match="remote unreachable"):
        pipeline_apply.resolve_graph(_pipeline_entity(), {}, git_resolver=_fail)


# ---------------------------------------------------------------------------
# Plan phase — desired view + blocking
# ---------------------------------------------------------------------------


def test_desired_view_carries_pinned_sha() -> None:
    report, desired, _ = _plan(_PINNED_PROMPT_A, _SHA_B)
    assert not report["blocked"]
    pinned_view = dict(desired[KIND_PIPELINE])["git-content"]
    assert pinned_view["graph"]["nodes"][0]["agent_prompt"] == _PINNED_PROMPT_B


def test_plan_blocks_on_resolution_failure() -> None:
    from modulo.core.pipeline_engine.git_content import GitContentRefError

    entity_set = _entity_set()
    current = _current_entities(stored_prompt=_PINNED_PROMPT_A)

    def _fail(_ref) -> str:
        raise GitContentRefError("remote unreachable")

    desired = {KIND_PIPELINE: []}
    desired, blocked = pipeline_apply.build_desired_views(
        entity_set,
        current,
        desired,
        [],
        set(),
        git_resolver=_fail,
    )
    assert blocked
    assert blocked[0][0] == KIND_PIPELINE
    assert "git content ref" in blocked[0][2]
    assert not desired[KIND_PIPELINE]


# ---------------------------------------------------------------------------
# Drift (--diff) — pinned SHA vs git-derived spec
# ---------------------------------------------------------------------------


def test_drift_unchanged_when_pin_matches_spec() -> None:
    report, _, _ = _plan(_PINNED_PROMPT_A, _SHA_A)
    assert report["unchanged"]
    assert not report["updated"]
    assert not has_drift(report)


def test_drift_updated_when_branch_moved() -> None:
    """Spec tracks @main; main now resolves to a NEW commit than the deployed pin."""
    report, desired, current = _plan(_PINNED_PROMPT_A, _SHA_B)
    assert report["updated"]
    assert has_drift(report)
    detail = build_drift_detail(current, desired, report)
    entries = detail["git-content"]["git_content"]
    assert entries == [
        {
            "node": _NODE_ID,
            "field": "agent_prompt",
            "desired": _PINNED_PROMPT_B,
            "current": _PINNED_PROMPT_A,
        }
    ]


def test_drift_updated_when_deployed_ref_is_unpinned() -> None:
    """A stored @main (bypassing today's save gate) still converges to the pin."""
    report, _, _ = _plan(_MOVABLE_PROMPT, _SHA_A)
    assert report["updated"]
    assert has_drift(report)


def test_drift_renders_git_content_line() -> None:
    report, desired, current = _plan(_PINNED_PROMPT_A, _SHA_B)
    report["mode"] = "drift"
    report["drift_detail"] = build_drift_detail(current, desired, report)
    rendered = render_table(report)
    assert f"drift detail git-content 'git-content' node {_NODE_ID} agent_prompt" in rendered
    assert _PINNED_PROMPT_A in rendered
    assert _PINNED_PROMPT_B in rendered


def test_diff_git_content_renders_absent_side_as_placeholder() -> None:
    """An absent side renders as ``(none)``, never the literal ``None`` (Minor 5)."""
    from modulo.cli.apply.drift import _diff_git_content

    desired = {"nodes": [{"id": _NODE_ID, "agent_prompt": _PINNED_PROMPT_A}], "edges": []}
    current = {"nodes": [{"id": _NODE_ID}], "edges": []}
    entries = _diff_git_content(desired, current)
    assert entries == [
        {
            "node": _NODE_ID,
            "field": "agent_prompt",
            "desired": _PINNED_PROMPT_A,
            "current": "(none)",
        }
    ]


def test_diff_git_content_skips_equal_scalar_ref() -> None:
    """Two identical pinned prompts are not drift — the whole-graph hash agrees."""
    from modulo.cli.apply.drift import _diff_git_content

    graph = {"nodes": [{"id": _NODE_ID, "agent_prompt": _PINNED_PROMPT_A}], "edges": []}
    assert _diff_git_content(graph, graph) == []


def test_diff_git_content_reports_command_list_commit_move() -> None:
    """A git-sourced ``agent_commands`` item that moved commits is named."""
    from modulo.cli.apply.drift import _diff_git_content

    desired = {"nodes": [{"id": _NODE_ID, "agent_commands": [_PINNED_PROMPT_B]}], "edges": []}
    current = {"nodes": [{"id": _NODE_ID, "agent_commands": [_PINNED_PROMPT_A]}], "edges": []}
    assert _diff_git_content(desired, current) == [
        {
            "node": _NODE_ID,
            "field": "agent_commands[0]",
            "desired": _PINNED_PROMPT_B,
            "current": _PINNED_PROMPT_A,
        }
    ]


def test_diff_git_content_skips_equal_command_ref() -> None:
    """Identical git-sourced command items are not drift."""
    from modulo.cli.apply.drift import _diff_git_content

    graph = {"nodes": [{"id": _NODE_ID, "agent_commands": [_PINNED_PROMPT_A]}], "edges": []}
    assert _diff_git_content(graph, graph) == []


def test_diff_git_content_reports_command_added_on_one_side() -> None:
    """A command present on only one side renders the absent side as ``(none)``."""
    from modulo.cli.apply.drift import _diff_git_content

    desired = {"nodes": [{"id": _NODE_ID, "agent_commands": []}], "edges": []}
    current = {"nodes": [{"id": _NODE_ID, "agent_commands": [_PINNED_PROMPT_A]}], "edges": []}
    assert _diff_git_content(desired, current) == [
        {
            "node": _NODE_ID,
            "field": "agent_commands[0]",
            "desired": "(none)",
            "current": _PINNED_PROMPT_A,
        }
    ]


# ---------------------------------------------------------------------------
# Write path — the PATCH payload carries the pin (respx, real API models)
# ---------------------------------------------------------------------------


@respx.mock
def test_write_payload_carries_pinned_sha(monkeypatch: pytest.MonkeyPatch) -> None:
    """End-to-end apply WRITE sends the pinned ref (what lands in the graph -> snapshot).

    ``executor.run`` uses the default plan-time resolver; the test swaps it
    for the fixed SHA so no network is touched, then asserts the PATCH
    ``graph_json`` the executor actually sent carries the canonical pinned
    ``agent_prompt`` — round-tripped through the REAL ``PipelineUpdate`` /
    ``PipelineGraphUpdate`` models, not a hand-built fixture.
    """
    monkeypatch.setattr(
        "modulo.core.pipeline_engine.git_content.default_git_content_resolver",
        lambda ref: _SHA_A,
    )
    respx.get("https://api.test/api/v1/schemas", params={"page": "1", "page_size": "100"}).respond(
        json={"items": [], "total": 0, "page": 1, "page_size": 100}
    )
    respx.get("https://api.test/api/v1/model-backends", params={"page": "1", "page_size": "100"}).respond(
        json={"items": [], "total": 0, "page": 1, "page_size": 100}
    )
    respx.get("https://api.test/api/v1/pipelines", params={"page": "1", "page_size": "100"}).respond(
        json={"items": [], "total": 0, "page": 1, "page_size": 100}
    )
    created_id = str(uuid.uuid4())
    respx.post("https://api.test/api/v1/pipelines").respond(json={"id": created_id, "name": "git-content"})
    patch_route = respx.patch(f"https://api.test/api/v1/pipelines/{created_id}").respond(
        json={"id": created_id, "name": "git-content"}
    )

    config = parse_apply_documents(CONFIG_TEXT)
    with httpx.Client() as client:
        executor = ApplyExecutor("https://api.test", "key", client=client)
        report = executor.run(config, dry_run=False)

    assert not report["failed"]
    created = [e["name"] for e in report["created"] if e["kind"] == KIND_PIPELINE]
    assert created == ["git-content"]
    assert patch_route.call_count == 1
    patch_payload = json.loads(patch_route.calls.last.request.content)
    update = PipelineUpdate.model_validate(patch_payload)
    assert update.graph_json is not None
    assert update.graph_json.nodes[0].agent_prompt == _PINNED_PROMPT_A
