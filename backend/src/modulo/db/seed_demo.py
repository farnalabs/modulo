"""Idempotent demo-org seed for the /demo auto-login experience (FAR-535).

GATED: runs only when ``MODULO_DEMO_ENABLED`` is truthy AND both
``MODULO_DEMO_USER`` (email) and ``MODULO_DEMO_PASSWORD`` are non-empty
(see ``modulo.core.demo`` — the neutral gate both this seed and the auth
route share). Default off — the seed is a no-op otherwise, so the
default release path behaviour is unchanged.

Creates (idempotently, NO Alembic migrations):

* the demo organisation (slug ``demo``),
* the demo user account (email/password from env; the password hash is
  re-stamped to match the env on every run so rotating the secret works),
* a ``viewer``-role org membership (read-only permission set — the org-role
  hierarchy viewer < runner < operator < admin is the enforcement boundary for
  every route via ``require_permission``; the seed also forces the role BACK to
  viewer if it drifted, and forces ``is_system_admin`` off),
* a ``Demo Engineering`` team with the demo user as a viewer member,
* 5 published schemas with realistic JSON Schema definitions (Demo Intake,
  Demo Report, GitHub Pull Request, Linear Issue, Release Notes),
* 4 named pipelines with multi-node graphs (PR Review & Triage, Release
  Notes Generator, Docs Sync, plus the Demo Governance Pipeline — the product
  intro video's flow: Implement -> PR risk level -> [risk > 0.50] Human
  review -> Open PR, with a conditional risk <= 0.50 edge straight to Open PR).
  Every graph is valid against the SAME node/edge models the
  ``GET /pipelines/{id}/graph`` read uses (FAR-1248): node ids are
  deterministic UUIDs (``demo_node_id``), agent nodes bind a seeded agent,
  and the edges are written as first-class ``pipeline_edges`` rows (the live
  graph read path) as well as into the v1 snapshot ``graph_json``,
* 20 synthetic terminal runs spread over 14 days (mixed statuses:
  complete/failed/awaiting_human), realistic tokens/costs/durations, each
  carrying node-level execution data (``node_token_usage`` + per-node
  ``run_node_outputs`` outputs/telemetry keyed by the graph's node ids) so the
  run detail Execution Trace and per-node cost render,
* 6 agents with realistic prompts (every seeded agent node binds one),
* a webhook trigger (GitHub PR events), a cron trigger (weekly release
  notes) and a "Ticket ready" webhook trigger on the governance pipeline,
* a "Delivery lifecycle" lifecycle map (Ticket -> Implement -> Review ->
  Deploy staging -> Deploy prod) linking the governance and PR review
  pipelines,
* RunDailyFact rows for analytics surface seeding.

Convergence: every spec-owned field (pipeline graph, edges, snapshot graph,
run display fields + node-level data, daily-fact cost/tokens/status, trigger
config, lifecycle-map content) is converged on existing rows at every boot, so
a deployment seeded by an older release (e.g. string node ids) is repaired on
the next boot without manual DB surgery.

Safety:
* No migrations — pure runtime ORM inserts.
* Org-scoped writes run under ``set_rls_org`` + ``set_rls_execution_context``
  (the documented boot/execution context) so Postgres RLS admits them.
* Account/email writes follow the same pattern as the boot-time
  ``_seed_modulo_users`` seed (system-context transaction, no RLS org).
* Idempotent by natural keys (org slug, account email, schema name, pipeline
  name, per-org run_number) — re-running never duplicates.

Runnable standalone (entrypoint-style, mirrors ``modulo.db.bootstrap_role``):

    python -m modulo.db.seed_demo
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import sys
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from modulo.core.demo import DEMO_ORG_ROLE, DEMO_ORG_SLUG, demo_login_config
from modulo.db.models.account import Account
from modulo.db.models.agent import Agent
from modulo.db.models.lifecycle_map import LifecycleMap
from modulo.db.models.lifecycle_map_stage import LifecycleMapStage
from modulo.db.models.org_membership import OrgMembership
from modulo.db.models.organisation import Organisation
from modulo.db.models.pipeline import Pipeline
from modulo.db.models.pipeline_edge import PipelineEdge
from modulo.db.models.pipeline_snapshot import PipelineSnapshot
from modulo.db.models.run import Run
from modulo.db.models.run_daily_facts import RunDailyFact
from modulo.db.models.schema import Schema, SchemaVersion
from modulo.db.models.team import Team
from modulo.db.models.team_membership import TeamMembership
from modulo.db.models.trigger import Trigger
from modulo.db.rls import set_rls_execution_context, set_rls_org
from modulo.db.soft_delete import include_soft_deleted
from modulo.settings import get_settings

_log = logging.getLogger(__name__)

DEMO_ORG_NAME = "Demo"

DEMO_PIPELINE_NAME = "Demo Governance Pipeline"

# Fixed namespace for the deterministic demo ids (FAR-1248). Never change it:
# the ids derived from it are persisted in graph_nodes_json, pipeline_edges,
# snapshot graph_json, run_node_outputs and node_token_usage, and convergence
# relies on the same spec producing the same ids on every boot.
_DEMO_ID_NAMESPACE = uuid.UUID("6f1c2a9e-4b7d-5e3a-9c81-0d2e4f6a8b1c")


def demo_node_id(pipeline_name: str, node_key: str) -> str:
    """Deterministic UUID (string) for a seeded graph node.

    The graph read path validates node ids as UUIDs (``PipelineGraphNode.id``);
    string keys like ``demo-intake`` were dropped from the editor canvas
    (FAR-1248). ``uuid5`` over a fixed namespace + a stable key keeps the id
    stable across boots so run-level rows keyed on it stay consistent.
    """
    return str(uuid.uuid5(_DEMO_ID_NAMESPACE, f"node:{pipeline_name}:{node_key}"))


def demo_edge_id(organisation_id: uuid.UUID, pipeline_name: str, edge_key: str) -> uuid.UUID:
    """Deterministic UUID for a seeded ``pipeline_edges`` row.

    Edge ids are table primary keys (globally unique), so the org id is part of
    the key: two demo orgs never collide, and a re-seed of the same org
    reproduces the same ids.
    """
    return uuid.uuid5(_DEMO_ID_NAMESPACE, f"edge:{organisation_id}:{pipeline_name}:{edge_key}")


@dataclass(frozen=True)
class _NodeSpec:
    """One seeded graph node. ``agent`` names a ``_DEMO_AGENT_SPECS`` entry."""

    key: str
    label: str
    description: str
    x: float
    y: float
    node_type: str = "agent"
    agent: str | None = None
    hitl_config: Mapping[str, Any] | None = None
    idempotent: bool = True
    # Relative share of the run's tokens/cost/duration this node accounts for
    # in the seeded execution trace (0 = no model usage, e.g. a HITL gate).
    weight: int = 1
    # Sample ``summary`` for the node's seeded output.
    output_summary: str = ""


@dataclass(frozen=True)
class _EdgeSpec:
    key: str
    source: str
    target: str
    edge_type: str = "normal"
    condition_expression: str | None = None


@dataclass(frozen=True)
class _PipelineSpec:
    name: str
    description: str
    nodes: tuple[_NodeSpec, ...]
    edges: tuple[_EdgeSpec, ...]
    duration_minutes: int = 4


# Risk threshold the governance pipeline's conditional edges route on.
_RISK_THRESHOLD = "0.5"

# The Demo Governance Pipeline mirrors the product intro video (FAR-1248):
# Implement -> PR risk level -> (risk > 0.50) Human review -> Open PR, with a
# conditional edge straight to Open PR when risk <= 0.50. Conditional edges are
# JMESPath ``condition_expression`` edges (the engine's conditional router,
# graph_cache._make_conditional_router); the HITL node carries the node-level
# ``hitl_config`` the graph model requires for node_type="hitl".
_GOVERNANCE_PIPELINE = _PipelineSpec(
    name=DEMO_PIPELINE_NAME,
    description=(
        "Implements a ready ticket, scores the PR's risk, routes risky changes (risk > 0.50) "
        "to a human reviewer and opens the PR. Read-only demo data."
    ),
    duration_minutes=3,
    nodes=(
        _NodeSpec(
            key="implement",
            label="Implement",
            description="Implements the ticket on a feature branch and pushes the change.",
            x=80,
            y=200,
            agent="Implementer",
            weight=68,
            output_summary="Implemented the ticket on a feature branch: 4 files changed, tests added.",
        ),
        _NodeSpec(
            key="risk",
            label="PR risk level",
            description="Scores the change's risk from 0.00 to 1.00 (blast radius, test coverage, sensitive paths).",
            x=380,
            y=200,
            agent="PR Risk Scorer",
            weight=12,
        ),
        _NodeSpec(
            key="review",
            label="Human review",
            description="Risky changes (risk > 0.50) wait here for a human approval.",
            x=680,
            y=60,
            node_type="hitl",
            hitl_config={
                "label": "Human review",
                "description": (
                    "The PR risk level scored above 0.50: a human must review and approve the change "
                    "before the PR is opened."
                ),
                "claim_expiry_minutes": 60,
                "human_only": True,
            },
            weight=0,
        ),
        _NodeSpec(
            key="open_pr",
            label="Open PR",
            description="Opens the pull request with a structured description linked to the ticket.",
            x=980,
            y=200,
            agent="PR Opener",
            # Opening a PR is an external side effect: never auto-retried.
            idempotent=False,
            weight=20,
        ),
    ),
    edges=(
        _EdgeSpec(key="implement-risk", source="implement", target="risk"),
        _EdgeSpec(
            key="risk-open-pr",
            source="risk",
            target="open_pr",
            edge_type="conditional",
            condition_expression=f"risk_score <= `{_RISK_THRESHOLD}`",
        ),
        _EdgeSpec(
            key="risk-review",
            source="risk",
            target="review",
            edge_type="conditional",
            condition_expression=f"risk_score > `{_RISK_THRESHOLD}`",
        ),
        _EdgeSpec(key="review-open-pr", source="review", target="open_pr"),
    ),
)

# Expanded pipelines (FAR-977); node ids made UUIDs + agents bound (FAR-1248).
_DEMO_PIPELINES: tuple[_PipelineSpec, ...] = (
    _GOVERNANCE_PIPELINE,
    _PipelineSpec(
        name="PR Review & Triage",
        description="Reviews incoming PRs, classifies severity, and posts review comments.",
        duration_minutes=4,
        nodes=(
            _NodeSpec(
                key="classify",
                label="Classify PR",
                description="Classifies the PR by type (feature, bugfix, refactor, docs) and severity.",
                x=80,
                y=150,
                agent="AI Code Reviewer",
                weight=2,
                output_summary="Classified as bugfix, severity medium.",
            ),
            _NodeSpec(
                key="review",
                label="Code Review",
                description="Reviews the diff for correctness, security and style; posts inline comments.",
                x=380,
                y=150,
                agent="AI Code Reviewer",
                weight=6,
                output_summary="3 findings: 1 correctness (major), 2 style (minor). Inline comments posted.",
            ),
            _NodeSpec(
                key="summarize",
                label="Post Summary",
                description="Posts a concise summary of the review findings as a PR comment.",
                x=680,
                y=150,
                agent="AI Code Reviewer",
                weight=2,
                output_summary="Posted the review summary comment on the PR.",
            ),
        ),
        edges=(
            _EdgeSpec(key="classify-review", source="classify", target="review"),
            _EdgeSpec(key="review-summarize", source="review", target="summarize"),
        ),
    ),
    _PipelineSpec(
        name="Release Notes Generator",
        description="Collects merged PRs since the last release and generates formatted release notes.",
        duration_minutes=8,
        nodes=(
            _NodeSpec(
                key="collect",
                label="Collect PRs",
                description="Lists every PR merged since the last release tag, grouped by type.",
                x=80,
                y=150,
                agent="Release Notes Writer",
                weight=3,
                output_summary="Collected 14 merged PRs: 6 features, 7 fixes, 1 breaking change.",
            ),
            _NodeSpec(
                key="format",
                label="Format Notes",
                description="Formats the grouped PRs into markdown release notes.",
                x=380,
                y=150,
                agent="Release Notes Writer",
                weight=5,
                output_summary="Drafted release notes with Features, Fixes and Breaking Changes sections.",
            ),
        ),
        edges=(_EdgeSpec(key="collect-format", source="collect", target="format"),),
    ),
    _PipelineSpec(
        name="Docs Sync",
        description="Detects code changes and updates relevant documentation pages.",
        duration_minutes=5,
        nodes=(
            _NodeSpec(
                key="diff",
                label="Detect Changes",
                description="Finds files with user-facing API changes since the previous release.",
                x=80,
                y=150,
                agent="Docs Maintainer",
                weight=2,
                output_summary="Found 3 files with user-facing API changes.",
            ),
            _NodeSpec(
                key="update-docs",
                label="Update Docs",
                description="Updates the documentation page for each changed file.",
                x=380,
                y=150,
                agent="Docs Maintainer",
                weight=5,
                output_summary="Updated 3 documentation pages.",
            ),
            _NodeSpec(
                key="validate-links",
                label="Validate Links",
                description="Verifies every internal link in the updated docs still resolves.",
                x=680,
                y=150,
                agent="Docs Maintainer",
                weight=1,
                output_summary="All 42 internal links resolve.",
            ),
        ),
        edges=(
            _EdgeSpec(key="diff-update-docs", source="diff", target="update-docs"),
            _EdgeSpec(key="update-docs-validate-links", source="update-docs", target="validate-links"),
        ),
    ),
)

# Expanded schema specs (FAR-977) — realistic JSON Schema definitions.
_DEMO_SCHEMA_SPECS: list[dict[str, object]] = [
    {
        "name": "Demo Intake",
        "description": "Demo sample: intake payload",
        "definition": {
            "type": "object",
            "properties": {"title": {"type": "string"}, "summary": {"type": "string"}},
            "required": ["title"],
        },
    },
    {
        "name": "Demo Report",
        "description": "Demo sample: report payload",
        "definition": {
            "type": "object",
            "properties": {"report": {"type": "string"}},
            "required": ["report"],
        },
    },
    {
        "name": "GitHub Pull Request",
        "description": "Schema for a GitHub pull request webhook payload",
        "definition": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "PR title"},
                "body": {"type": "string", "description": "PR description body"},
                "author": {"type": "string", "description": "GitHub username of the author"},
                "state": {"type": "string", "enum": ["open", "closed", "merged"]},
                "labels": {"type": "array", "items": {"type": "string"}},
                "base_branch": {"type": "string"},
                "head_branch": {"type": "string"},
                "review_decision": {
                    "type": "string",
                    "enum": ["approved", "changes_requested", "review_required", "null"],
                },
                "files_changed": {"type": "integer", "minimum": 0},
                "additions": {"type": "integer", "minimum": 0},
                "deletions": {"type": "integer", "minimum": 0},
            },
            "required": ["title", "author", "state"],
        },
    },
    {
        "name": "Linear Issue",
        "description": "Schema for a Linear issue payload",
        "definition": {
            "type": "object",
            "properties": {
                "identifier": {"type": "string", "description": "e.g. FAR-123"},
                "title": {"type": "string"},
                "description": {"type": "string"},
                "priority": {"type": "integer", "minimum": 0, "maximum": 4},
                "state": {"type": "string"},
                "assignee": {"type": "string"},
                "labels": {"type": "array", "items": {"type": "string"}},
                "estimate": {"type": "number"},
                "cycle_name": {"type": "string"},
            },
            "required": ["identifier", "title", "state"],
        },
    },
    {
        "name": "Release Notes",
        "description": "Schema for generated release notes output",
        "definition": {
            "type": "object",
            "properties": {
                "version": {"type": "string", "description": "Semver version string"},
                "date": {"type": "string", "format": "date"},
                "features": {"type": "array", "items": {"type": "string"}},
                "fixes": {"type": "array", "items": {"type": "string"}},
                "breaking_changes": {"type": "array", "items": {"type": "string"}},
                "authors": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["version", "date"],
        },
    },
]

# Agent specs (FAR-977).
_DEMO_AGENT_SPECS: list[dict[str, str | list[str] | None]] = [
    {
        "name": "AI Code Reviewer",
        "description": (
            "Analyses code changes for correctness, security, and style, then posts a review with inline comments."
        ),
        "prompt_template": (
            "You are a code reviewer for a software engineering team.\n"
            "Analyse the provided code diff for:\n"
            "1. Correctness — logic errors, edge cases, null handling\n"
            "2. Security — injection, auth bypass, secret exposure\n"
            "3. Style — naming, DRY, complexity\n"
            "Post a structured review with severity-rated findings."
        ),
    },
    {
        "name": "Release Notes Writer",
        "description": "Generates formatted, user-facing release notes from a list of merged PRs.",
        "prompt_template": (
            "You are a technical writer generating release notes.\n"
            "Given a list of merged pull requests grouped by type, produce\n"
            "a concise, user-friendly release notes document in markdown.\n"
            "Use clear headings for Features, Bug Fixes, and Breaking Changes.\n"
            "Link to the original PRs where possible."
        ),
    },
    # FAR-1248: the governance pipeline's agents + the Docs Sync agent, so
    # every seeded agent node binds an agent (the graph model requires
    # ``agent_id`` on node_type="agent").
    {
        "name": "Implementer",
        "description": "Implements a groomed ticket on a feature branch: code, tests and a short change summary.",
        "prompt_template": (
            "You are a software engineer implementing a ready ticket.\n"
            "Read the ticket and the relevant code, implement the change on a\n"
            "feature branch, add or update tests, and summarise what changed."
        ),
    },
    {
        "name": "PR Risk Scorer",
        "description": "Scores a change's risk from 0.00 to 1.00 so risky PRs are routed to a human reviewer.",
        "prompt_template": (
            "You assess the risk of a code change.\n"
            "Consider blast radius, test coverage, touched sensitive paths\n"
            "(auth, billing, migrations) and diff size. Return risk_score\n"
            "between 0 and 1 with a one-line rationale."
        ),
    },
    {
        "name": "PR Opener",
        "description": "Opens the pull request with a structured description linked to the ticket.",
        "prompt_template": (
            "You open pull requests.\n"
            "Write a clear title and a structured description (summary,\n"
            "testing, risk) that links the originating ticket, then open the PR."
        ),
    },
    {
        "name": "Docs Maintainer",
        "description": "Keeps user-facing documentation in sync with code changes.",
        "prompt_template": (
            "You maintain product documentation.\n"
            "For each user-facing code change, update the matching docs page\n"
            "and verify every internal link still resolves."
        ),
    },
]

# Run specs for the expanded seed (FAR-977): (run_number, status, trigger_type,
# pipeline_name, total_tokens, total_cost_usd, days_ago, hours_into_day).
# Spread over 14 days with realistic patterns.
_DEMO_RUN_SPECS: list[tuple[int, str, str, str, int, float, int, int]] = [
    # Today (day 0)
    # Governance runs carry video-flow costs (FAR-1248): run 1 took the auto
    # path (risk 0.18 -> Open PR, ~$0.09); run 9 hit Human review (risk 0.74
    # -> approved -> Open PR, ~$0.14). See _GOVERNANCE_RUN_PATHS.
    (1, "complete", "webhook", "Demo Governance Pipeline", 41200, 0.0900, 0, 2),
    (2, "failed", "webhook", "Demo Governance Pipeline", 21500, 0.0410, 0, 4),
    # Day 1
    (3, "complete", "webhook", "PR Review & Triage", 3200, 0.0074, 1, 10),
    (4, "complete", "webhook", "PR Review & Triage", 2800, 0.0065, 1, 14),
    # Day 2
    (5, "complete", "cron", "Release Notes Generator", 4100, 0.0095, 2, 9),
    (6, "awaiting_human", "manual", "PR Review & Triage", 1500, 0.0035, 2, 16),
    # Day 3
    (7, "complete", "webhook", "PR Review & Triage", 2900, 0.0067, 3, 11),
    (8, "failed", "cron", "Docs Sync", 890, 0.0021, 3, 15),
    # Day 4
    (9, "complete", "webhook", "Demo Governance Pipeline", 63400, 0.1400, 4, 8),
    (10, "complete", "webhook", "PR Review & Triage", 3500, 0.0081, 4, 13),
    # Day 5
    (11, "complete", "cron", "Release Notes Generator", 4300, 0.0099, 5, 9),
    (12, "complete", "webhook", "Demo Governance Pipeline", 38900, 0.0850, 5, 17),
    # Day 7
    (13, "complete", "webhook", "PR Review & Triage", 3100, 0.0072, 7, 10),
    (14, "failed", "manual", "Docs Sync", 650, 0.0015, 7, 14),
    # Day 9
    (15, "complete", "cron", "Release Notes Generator", 3800, 0.0088, 9, 9),
    (16, "complete", "webhook", "PR Review & Triage", 2700, 0.0062, 9, 16),
    # Day 11
    (17, "complete", "webhook", "Demo Governance Pipeline", 58800, 0.1320, 11, 11),
    (18, "complete", "cron", "Docs Sync", 1600, 0.0037, 11, 15),
    # Day 13
    (19, "complete", "webhook", "PR Review & Triage", 3300, 0.0076, 13, 10),
    (20, "failed", "manual", "PR Review & Triage", 420, 0.0010, 13, 14),
]

_DEMO_FAILURE_DETAIL = "Demo sample failure — no real work was performed."


@dataclass(frozen=True)
class _GovernanceRunPath:
    """Which branch a seeded governance run took through the video flow."""

    risk_score: float | None
    human_review: bool = False
    pr_number: int | None = None


# FAR-1248: the governance runs' routes through the conditional edges.
# risk <= 0.50 -> straight to Open PR; risk > 0.50 -> Human review (approved)
# -> Open PR. Run 2 failed while scoring risk (no score, no PR).
_GOVERNANCE_RUN_PATHS: dict[int, _GovernanceRunPath] = {
    1: _GovernanceRunPath(risk_score=0.18, pr_number=1482),
    2: _GovernanceRunPath(risk_score=None),
    9: _GovernanceRunPath(risk_score=0.74, human_review=True, pr_number=1471),
    12: _GovernanceRunPath(risk_score=0.31, pr_number=1466),
    17: _GovernanceRunPath(risk_score=0.62, human_review=True, pr_number=1459),
}

# Human review adds reviewer wait time to a governance run's wall clock.
_HUMAN_REVIEW_MINUTES = 23

# SQLAlchemy DBAPIError/StatementError str() and repr() embed the failed
# statement's bind parameters as a "[parameters: (...)]" section. The demo
# account INSERT binds include the demo user's bcrypt password_hash, so every
# seed-failure log/print goes through _safe_exc_text — never the raw
# exception text or repr.
_PARAMETERS_SECTION_RE = re.compile(r"\[parameters:\s*[^]]*\]", re.DOTALL)


def _safe_exc_text(exc: BaseException) -> str:
    """Type + message for a seed-failure log, with bind parameters stripped.

    SQLAlchemy's DBAPIError/StatementError string and repr forms embed
    ``[parameters: (...)]``; for the demo account INSERT those bind params
    contain the demo account's bcrypt password_hash. The section is removed
    entirely (not masked) so neither the hash nor a "[parameters:" marker
    survives in any log/stdout surface.
    """
    text = f"{type(exc).__name__}: {exc}"
    return _PARAMETERS_SECTION_RE.sub("", text)


class DemoSeedError(Exception):
    """Raised by main.py's demo-seed wrapper when the demo seed fails.

    The message is always ``_safe_exc_text`` output: ``_boot_seed`` prints
    ``repr(exc)`` to stdout and logs the traceback, and SQLAlchemy exception
    reprs embed bind parameters (this seed's include the demo password hash),
    so the original exception is deliberately NOT chained here.
    """


def _graph_nodes(spec: _PipelineSpec, agent_ids: Mapping[str, uuid.UUID]) -> list[dict[str, Any]]:
    """The pipeline's ``graph_nodes_json`` — valid against ``PipelineGraphNode``.

    Node ids are deterministic UUIDs; agent nodes carry the bound agent's id
    (strict validation requires ``agent_id`` on node_type="agent"); the HITL
    node carries its ``hitl_config``. Positions run left to right: the editor
    renders stored positions verbatim (no auto-layout).
    """
    nodes: list[dict[str, Any]] = []
    for node in spec.nodes:
        entry: dict[str, Any] = {
            "id": demo_node_id(spec.name, node.key),
            "node_type": node.node_type,
            "label": node.label,
            "description": node.description,
            "position": {"x": node.x, "y": node.y},
        }
        if node.agent is not None and node.agent in agent_ids:
            entry["agent_id"] = str(agent_ids[node.agent])
        if node.hitl_config is not None:
            entry["hitl_config"] = dict(node.hitl_config)
        if not node.idempotent:
            entry["idempotent"] = False
        nodes.append(entry)
    return nodes


def _edge_rows(organisation_id: uuid.UUID, spec: _PipelineSpec) -> list[dict[str, Any]]:
    """The pipeline's first-class ``pipeline_edges`` rows (PipelineGraphEdge shape).

    ``GET /pipelines/{id}/graph`` reads edges from the ``pipeline_edges``
    table, never from ``graph_nodes_json`` — without these rows the canvas has
    no connections.
    """
    return [
        {
            "id": demo_edge_id(organisation_id, spec.name, edge.key),
            "source_node_id": uuid.UUID(demo_node_id(spec.name, edge.source)),
            "target_node_id": uuid.UUID(demo_node_id(spec.name, edge.target)),
            "edge_type": edge.edge_type,
            "condition_expression": edge.condition_expression,
            "hitl_gate_config": None,
        }
        for edge in spec.edges
    ]


def _snapshot_graph_json(nodes: list[dict[str, Any]], edge_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """The v1 snapshot ``graph_json`` in the shape run-start snapshots use.

    Mirrors ``crud.pipeline_snapshot._load_pipeline_and_edges`` (edges as
    ``id/source/target/type/hitl_gate_config/condition_expression``), so the
    run detail's node labels and the GraphValidator read it like a real
    snapshot.
    """
    return {
        "nodes": [dict(node) for node in nodes],
        "edges": [
            {
                "id": str(edge["id"]),
                "source": str(edge["source_node_id"]),
                "target": str(edge["target_node_id"]),
                "type": edge["edge_type"],
                "hitl_gate_config": edge["hitl_gate_config"],
                "condition_expression": edge["condition_expression"],
            }
            for edge in edge_rows
        ],
    }


@dataclass(frozen=True)
class _NodeStep:
    """One node a seeded run executed, with its sample output."""

    node: _NodeSpec
    status: str
    output: dict[str, Any] | None
    summary: str


@dataclass(frozen=True)
class _RunTrace:
    """Node-level execution data for one seeded run (keyed by graph node id)."""

    outputs: dict[str, Any]
    telemetry: dict[str, Any]
    node_token_usage: dict[str, Any]


def _complete_step(node: _NodeSpec, extra: Mapping[str, Any] | None = None) -> _NodeStep:
    output: dict[str, Any] = {"summary": node.output_summary}
    if extra:
        output.update(extra)
    return _NodeStep(node=node, status="complete", output=output, summary=str(output["summary"]))


def _failed_step(node: _NodeSpec) -> _NodeStep:
    return _NodeStep(node=node, status="failed", output=None, summary=_DEMO_FAILURE_DETAIL)


def _governance_path(run_number: int) -> _GovernanceRunPath:
    return _GOVERNANCE_RUN_PATHS.get(run_number, _GovernanceRunPath(risk_score=0.25, pr_number=1400 + run_number))


def _governance_steps(run_number: int) -> list[_NodeStep]:
    """The governance run's route through the conditional edges."""
    nodes = {node.key: node for node in _GOVERNANCE_PIPELINE.nodes}
    path = _governance_path(run_number)
    steps = [_complete_step(nodes["implement"], {"branch": f"feat/demo-ticket-{run_number}"})]
    if path.risk_score is None:
        steps.append(_failed_step(nodes["risk"]))
        return steps
    risky = path.risk_score > float(_RISK_THRESHOLD)
    route = "routed to Human review" if risky else "below 0.50, straight to Open PR"
    steps.append(
        _complete_step(
            nodes["risk"],
            {
                "summary": f"Risk {path.risk_score:.2f}: {route}.",
                "risk_score": path.risk_score,
                "risk_level": "high" if risky else "low",
            },
        )
    )
    if path.human_review:
        steps.append(
            _complete_step(
                nodes["review"],
                {"summary": "Approved by a human reviewer after checking the risky paths.", "decision": "approved"},
            )
        )
    pr_number = path.pr_number or 1400 + run_number
    steps.append(
        _complete_step(
            nodes["open_pr"],
            {
                "summary": f"Opened PR #{pr_number} linked to the ticket.",
                "pr_number": pr_number,
                "pr_url": f"https://github.com/acme/webapp/pull/{pr_number}",
            },
        )
    )
    return steps


def _generic_steps(spec: _PipelineSpec, status: str) -> list[_NodeStep]:
    """Linear pipelines: complete runs ran every node; failed runs stopped at node 2."""
    nodes = list(spec.nodes)
    if status == "failed":
        fail_index = min(1, len(nodes) - 1)
        return [*(_complete_step(node) for node in nodes[:fail_index]), _failed_step(nodes[fail_index])]
    if status == "awaiting_human":
        return [_complete_step(node) for node in nodes[: max(1, len(nodes) - 1)]]
    return [_complete_step(node) for node in nodes]


def _run_steps(spec: _PipelineSpec, run_number: int, status: str) -> list[_NodeStep]:
    if spec.name == DEMO_PIPELINE_NAME:
        return _governance_steps(run_number)
    return _generic_steps(spec, status)


def _split_int(total: int, weights: list[int]) -> list[int]:
    """Split *total* by *weights*; the last weighted share absorbs the remainder."""
    weight_sum = sum(weights)
    if weight_sum == 0:
        return [0 for _ in weights]
    parts = [total * weight // weight_sum for weight in weights]
    last = max(i for i, weight in enumerate(weights) if weight > 0)
    parts[last] += total - sum(parts)
    return parts


_COST_QUANTUM = Decimal("0.000001")


def _split_cost(total: Decimal, weights: list[int]) -> list[Decimal]:
    """Split a run cost by *weights*, summing EXACTLY to *total*."""
    weight_sum = sum(weights)
    if weight_sum == 0:
        return [Decimal(0) for _ in weights]
    parts = [(total * weight / weight_sum).quantize(_COST_QUANTUM, rounding=ROUND_HALF_UP) for weight in weights]
    last = max(i for i, weight in enumerate(weights) if weight > 0)
    parts[last] += total - sum(parts, Decimal(0))
    return parts


def _build_run_trace(
    spec: _PipelineSpec,
    run_number: int,
    status: str,
    total_tokens: int,
    total_cost_usd: Decimal,
) -> _RunTrace:
    """Deterministic node-level execution data for a seeded run (FAR-1248).

    The run detail's Execution Trace is the union of ``node_token_usage``
    (per-node tokens + cost), the ``run_node_outputs`` outputs and the
    telemetry, keyed by graph node id; node labels resolve from the snapshot
    graph. Per-node tokens/costs sum exactly to the run totals. Pure function
    of the spec, so a re-seed reproduces byte-identical rows (no churn).
    """
    steps = _run_steps(spec, run_number, status)
    weights = [step.node.weight for step in steps]
    tokens = _split_int(total_tokens, weights)
    costs = _split_cost(total_cost_usd, weights)
    durations = _split_int(spec.duration_minutes * 60_000, weights)

    outputs: dict[str, Any] = {}
    telemetry: dict[str, Any] = {}
    usage: dict[str, Any] = {}
    for step, node_tokens, node_cost, node_ms in zip(steps, tokens, costs, durations, strict=True):
        node_id = demo_node_id(spec.name, step.node.key)
        if step.output is not None:
            outputs[node_id] = step.output
        entry: dict[str, Any] = {"status": step.status, "summary": step.summary}
        if step.node.node_type == "hitl":
            entry["duration_ms"] = _HUMAN_REVIEW_MINUTES * 60_000
        else:
            entry["duration_ms"] = node_ms
        if step.status == "failed":
            entry["error"] = _DEMO_FAILURE_DETAIL
        telemetry[node_id] = entry
        if step.node.weight > 0:
            input_tokens = node_tokens * 4 // 5
            usage[node_id] = {
                "input_tokens": input_tokens,
                "output_tokens": node_tokens - input_tokens,
                "total_tokens": node_tokens,
                "cost_usd": float(node_cost),
            }
    return _RunTrace(outputs=outputs, telemetry=telemetry, node_token_usage=usage)


def _run_window(spec: _PipelineSpec, run_number: int, days_ago: int, hours_into_day: int) -> tuple[datetime, datetime]:
    """(started_at, completed_at) for a seeded run, relative to today (UTC)."""
    started = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(
        days=days_ago, hours=-hours_into_day
    )
    minutes = spec.duration_minutes
    if spec.name == DEMO_PIPELINE_NAME and _governance_path(run_number).human_review:
        minutes += _HUMAN_REVIEW_MINUTES
    return started, started + timedelta(minutes=minutes)


async def _get_or_create_pipeline(
    session: AsyncSession,
    org: Organisation,
    account: Account,
    name: str,
    description: str,
    graph_nodes: list[dict[str, object]],
) -> Pipeline:
    """Idempotent create-or-converge a pipeline by (org, name).

    When the row already exists, converges ``description`` and
    ``graph_nodes_json`` to the current spec without touching identity
    fields.  The write is a no-op when the row already matches.
    """
    result = await session.execute(select(Pipeline).where(Pipeline.organisation_id == org.id, Pipeline.name == name))
    pipeline = result.scalar_one_or_none()
    if pipeline is None:
        pipeline = Pipeline(
            organisation_id=org.id,
            name=name,
            description=description,
            account_id=account.id,
            visibility="org",
            # FAR-889: the demo graphs are trusted fixture data (FAR-535) built
            # from agent nodes only, not a user-supplied new-node entry point,
            # so they are not routed through the manual-node output-schema
            # guard (enforce_manual_node_output_schemas).
            graph_nodes_json=graph_nodes,
            default_autonomy_level="manual_approval",
        )
        try:
            async with session.begin_nested():
                session.add(pipeline)
                await session.flush()
        except IntegrityError:
            result = await session.execute(
                select(Pipeline).where(Pipeline.organisation_id == org.id, Pipeline.name == name)
            )
            pipeline = result.scalar_one_or_none()
            if pipeline is None:
                raise
            _log.info("demo_seed.pipeline_recovered_after_conflict", extra={"pipeline_name": name})
        else:
            _log.info("demo_seed.pipeline_created", extra={"pipeline_name": name})
    else:
        # Converge spec-owned fields on existing rows.
        changed = False
        if pipeline.description != description:
            pipeline.description = description
            changed = True
        if pipeline.graph_nodes_json != graph_nodes:
            pipeline.graph_nodes_json = graph_nodes
            changed = True
        if changed:
            _log.info("demo_seed.pipeline_converged", extra={"pipeline_name": name})
    return pipeline


async def _get_or_create_snapshot(
    session: AsyncSession,
    org: Organisation,
    pipeline: Pipeline,
    graph_json: dict[str, object],
) -> PipelineSnapshot:
    """Idempotent create-or-converge a snapshot v1 for a pipeline.

    When the row already exists, converges ``graph_json`` to the current
    spec.  The write is a no-op when the row already matches.
    """
    result = await session.execute(
        select(PipelineSnapshot).where(
            PipelineSnapshot.pipeline_id == pipeline.id,
            PipelineSnapshot.snapshot_version == 1,
        )
    )
    snapshot = result.scalar_one_or_none()
    if snapshot is None:
        snapshot = PipelineSnapshot(
            organisation_id=org.id,
            pipeline_id=pipeline.id,
            snapshot_version=1,
            account_id=pipeline.account_id,
            graph_json=graph_json,
            connector_bindings_json=[],
            schema_pins_json=[],
            prompt_pins_json=[],
            model_backend_pins_json=[],
        )
        try:
            async with session.begin_nested():
                session.add(snapshot)
                await session.flush()
        except IntegrityError:
            result = await session.execute(
                select(PipelineSnapshot).where(
                    PipelineSnapshot.pipeline_id == pipeline.id,
                    PipelineSnapshot.snapshot_version == 1,
                )
            )
            snapshot = result.scalar_one_or_none()
            if snapshot is None:
                raise
            _log.info("demo_seed.snapshot_recovered_after_conflict", extra={"pipeline_id": str(pipeline.id)})
        else:
            _log.info("demo_seed.snapshot_created", extra={"snapshot_id": str(snapshot.id)})
    elif snapshot.graph_json != graph_json:
        snapshot.graph_json = graph_json
        _log.info("demo_seed.snapshot_converged", extra={"pipeline_id": str(pipeline.id)})
    return snapshot


def _edge_signature(row: Mapping[str, Any]) -> tuple[str, str, str, str, str | None, Any]:
    return (
        str(row["id"]),
        str(row["source_node_id"]),
        str(row["target_node_id"]),
        str(row["edge_type"]),
        row["condition_expression"],
        row["hitl_gate_config"],
    )


async def _converge_pipeline_edges(
    session: AsyncSession,
    org: Organisation,
    pipeline: Pipeline,
    nodes: list[dict[str, Any]],
    edge_rows: list[dict[str, Any]],
) -> None:
    """Idempotently make the pipeline's ``pipeline_edges`` rows equal the spec.

    A no-op when the stored rows already match. Otherwise the graph is written
    through ``replace_pipeline_graph`` (the guarded graph-write primitive: the
    HITL gate-weakening + guardrail-strip guards run under the row lock), as a
    privileged system write. The deterministic edge ids make the replacement
    stable; edges left by an older seed are removed (FAR-1248: none existed,
    so the live graph read showed no connections).
    """
    from modulo.db.crud.pipeline import replace_pipeline_graph

    existing = list(
        (await session.execute(select(PipelineEdge).where(PipelineEdge.pipeline_id == pipeline.id))).scalars()
    )
    current = sorted(
        _edge_signature(
            {
                "id": edge.id,
                "source_node_id": edge.source_node_id,
                "target_node_id": edge.target_node_id,
                "edge_type": edge.edge_type,
                "condition_expression": edge.condition_expression,
                "hitl_gate_config": edge.hitl_gate_config,
            }
        )
        for edge in existing
    )
    if current == sorted(_edge_signature(row) for row in edge_rows):
        return
    await replace_pipeline_graph(
        session,
        pipeline_id=pipeline.id,
        org_id=org.id,
        nodes=nodes,
        edges=[dict(row) for row in edge_rows],
        is_privileged=True,
        caller_type="rest",
        is_guardrail_admin=True,
    )
    _log.info(
        "demo_seed.pipeline_edges_converged" if existing else "demo_seed.pipeline_edges_created",
        extra={"pipeline_name": pipeline.name, "edge_count": len(edge_rows)},
    )


async def _seed_demo_pipeline_and_runs(
    session: AsyncSession,
    org: Organisation,
    account: Account,
    agent_ids: Mapping[str, uuid.UUID] | None = None,
) -> dict[str, tuple[Pipeline, PipelineSnapshot]]:
    """Seed all demo pipelines (graph + edges + snapshot) and their runs.

    Race-safe across multi-instance boots: each insert runs in a savepoint with
    IntegrityError recovery on its natural key.

    Returns the pipeline/snapshot lookup so callers (trigger + lifecycle-map
    seeding) can reuse it instead of re-querying.
    """
    agents = agent_ids or {}
    pipeline_lookup: dict[str, tuple[Pipeline, PipelineSnapshot]] = {}
    for spec in _DEMO_PIPELINES:
        nodes = _graph_nodes(spec, agents)
        edge_rows = _edge_rows(org.id, spec)
        pipeline = await _get_or_create_pipeline(session, org, account, spec.name, spec.description, nodes)
        await _converge_pipeline_edges(session, org, pipeline, nodes, edge_rows)
        snapshot = await _get_or_create_snapshot(session, org, pipeline, _snapshot_graph_json(nodes, edge_rows))
        pipeline_lookup[spec.name] = (pipeline, snapshot)

    specs_by_name = {spec.name: spec for spec in _DEMO_PIPELINES}
    for run_spec in _DEMO_RUN_SPECS:
        await _seed_demo_run(session, org, account, run_spec, pipeline_lookup, specs_by_name)
    return pipeline_lookup


def _values_match(stored: Any, desired: Any) -> bool:
    """Compare a stored column value with its desired replacement, tz-insensitively.

    SQLite strips the UTC offset on a DATETIME round-trip (Postgres timestamptz
    keeps it), so an aware ``_run_window`` datetime would otherwise compare
    unequal on every boot on SQLite and rewrite an identical timestamp — no-op
    UPDATE churn against the "no churn on every boot" convergence contract.
    """
    if isinstance(stored, datetime) and isinstance(desired, datetime):
        stored_utc = stored if stored.tzinfo is None else stored.astimezone(UTC).replace(tzinfo=None)
        desired_utc = desired if desired.tzinfo is None else desired.astimezone(UTC).replace(tzinfo=None)
        return stored_utc == desired_utc
    return bool(stored == desired)


async def _seed_demo_run(
    session: AsyncSession,
    org: Organisation,
    account: Account,
    run_spec: tuple[int, str, str, str, int, float, int, int],
    pipeline_lookup: Mapping[str, tuple[Pipeline, PipelineSnapshot]],
    specs_by_name: Mapping[str, _PipelineSpec],
) -> None:
    """Create or converge one deterministic demo run (fixed run_number).

    Both paths (new + existing) write the run's node-level execution data and
    ensure its RunDailyFact, so rows seeded by an older release (no trace, or
    a trace keyed on the old string node ids) are repaired on the next boot.
    """
    run_number, status, trigger_type, pipeline_name, total_tokens, total_cost_usd, days_ago, hours_into_day = run_spec
    existing_result = await session.execute(
        select(Run).where(Run.organisation_id == org.id, Run.run_number == run_number)
    )
    run = existing_result.scalar_one_or_none()
    if pipeline_name not in pipeline_lookup or pipeline_name not in specs_by_name:
        if run is None:
            _log.warning(
                "demo_seed.run_spec_unknown_pipeline",
                extra={"pipeline_name": pipeline_name, "run_number": run_number},
            )
        return
    pipeline, snapshot = pipeline_lookup[pipeline_name]
    spec = specs_by_name[pipeline_name]
    cost = Decimal(str(total_cost_usd))
    started, completed = _run_window(spec, run_number, days_ago, hours_into_day)
    trace = _build_run_trace(spec, run_number, status, total_tokens, cost)
    is_failed = status == "failed"
    desired: dict[str, Any] = {
        "status": status,
        "trigger_type": trigger_type,
        "total_tokens": total_tokens,
        "total_cost_usd": cost,
        "started_at": started,
        "completed_at": completed if status != "awaiting_human" else None,
        "pipeline_id": pipeline.id,
        "snapshot_id": snapshot.id,
        "error_detail": _DEMO_FAILURE_DETAIL if is_failed else None,
        "error_code": "DEMO_SAMPLE" if is_failed else None,
        "node_token_usage": trace.node_token_usage,
    }

    if run is not None:
        # Converge display fields on existing runs (never touch identity).
        drifted: list[str] = []
        for attr, val in desired.items():
            if not _values_match(getattr(run, attr), val):
                setattr(run, attr, val)
                drifted.append(attr)
        if drifted:
            _log.info("demo_seed.run_converged", extra={"run_number": run_number, "fields": drifted})
    else:
        thread_id = f"demo-seed-{org.id}-{run_number}"
        run = Run(
            organisation_id=org.id,
            account_id=account.id,
            run_number=run_number,
            input_hash=hashlib.sha256(thread_id.encode()).hexdigest(),
            langgraph_thread_id=thread_id,
            **desired,
        )
        try:
            async with session.begin_nested():
                session.add(run)
                await session.flush()
        except IntegrityError:
            recovered = await session.execute(
                select(Run.id).where(Run.organisation_id == org.id, Run.run_number == run_number)
            )
            if recovered.scalar_one_or_none() is None:
                raise
            _log.info("demo_seed.run_recovered_after_conflict", extra={"run_number": run_number})
            return
        _log.info("demo_seed.run_created", extra={"run_number": run_number, "status": status})

    await _write_run_trace(session, org, run, trace)
    await _ensure_daily_fact(
        session,
        org,
        run,
        pipeline=pipeline,
        pipeline_name=pipeline_name,
        started=started,
        completed=completed,
    )


async def _write_run_trace(session: AsyncSession, org: Organisation, run: Run, trace: _RunTrace) -> None:
    """Best-effort REPLACE of the run's per-node outputs + telemetry (FAR-583 store).

    Skipped when the stored rows already match (no churn on every boot). The
    REPLACE blanks node ids absent from the new dicts, so a trace keyed on the
    pre-FAR-1248 ids (``demo``) is dropped in favour of the graph's UUIDs.
    """
    try:
        from modulo.db.crud.run_node_outputs import read_run_node_outputs_raw, replace_run_node_outputs

        async with session.begin_nested():
            stored = await read_run_node_outputs_raw(session, run_id=run.id, organisation_id=org.id)
            if stored.outputs == trace.outputs and stored.telemetry == trace.telemetry:
                return
            await replace_run_node_outputs(
                session,
                run_id=run.id,
                organisation_id=org.id,
                outputs=trace.outputs,
                telemetry=trace.telemetry,
            )
            _log.info("demo_seed.run_trace_written", extra={"run_number": run.run_number})
    except Exception as exc:
        _log.warning(
            "demo_seed.run_outputs_write_failed run=%s: %s",
            run.id,
            _safe_exc_text(exc),
        )


async def _ensure_daily_fact(
    session: AsyncSession,
    org: Organisation,
    run: Run,
    *,
    pipeline: Pipeline,
    pipeline_name: str,
    started: datetime,
    completed: datetime,
) -> None:
    """Create (best-effort) or converge the run's RunDailyFact analytics row.

    Mirrors the Run row: non-terminal statuses have no completed_at /
    duration_ms. An existing fact has its cost/tokens/status converged (the
    analytics surfaces sum these); its timestamps are left alone.
    """
    # nosemgrep: raw-status-complete — seed script, not routing code.
    is_terminal = run.status in ("complete", "failed")
    fact_result = await session.execute(select(RunDailyFact).where(RunDailyFact.run_id == run.id).limit(1))
    fact = fact_result.scalars().first()
    if fact is not None:
        changed = False
        for attr, val in (
            ("status", run.status),
            ("trigger_type", run.trigger_type),
            ("total_cost_usd", run.total_cost_usd),
            ("total_tokens", run.total_tokens),
        ):
            if getattr(fact, attr) != val:
                setattr(fact, attr, val)
                changed = True
        if changed:
            _log.info("demo_seed.daily_fact_converged", extra={"run_number": run.run_number})
        return
    try:
        async with session.begin_nested():
            session.add(
                RunDailyFact(
                    organisation_id=org.id,
                    run_id=run.id,
                    run_date=started.date(),
                    pipeline_id=pipeline.id,
                    pipeline_name=pipeline_name,
                    trigger_type=run.trigger_type,
                    status=run.status,
                    total_cost_usd=run.total_cost_usd,
                    total_tokens=run.total_tokens,
                    duration_ms=(int((completed - started).total_seconds() * 1000) if is_terminal else None),
                    run_number=run.run_number,
                    started_at=started,
                    completed_at=completed if is_terminal else None,
                )
            )
            await session.flush()
    except IntegrityError:
        _log.info("demo_seed.daily_fact_recovered_after_conflict", extra={"run_number": run.run_number})
    except Exception as exc:
        _log.warning(
            "demo_seed.daily_fact_write_failed run=%s: %s",
            run.id,
            _safe_exc_text(exc),
        )


async def _seed_demo_agents(session: AsyncSession, org: Organisation, account: Account) -> dict[str, uuid.UUID]:
    """Idempotent demo agents (FAR-977).

    Race-safe: savepoint + IntegrityError recovery on the (org, name) key.
    Existing rows are converged to the current spec (description,
    prompt_template). Returns ``{agent name: agent id}`` so the pipeline graphs
    can bind their agent nodes (FAR-1248).
    """
    agent_ids: dict[str, uuid.UUID] = {}
    for spec in _DEMO_AGENT_SPECS:
        name = str(spec["name"])
        result = await session.execute(select(Agent).where(Agent.organisation_id == org.id, Agent.name == name))
        agent = result.scalar_one_or_none()
        if agent is None:
            agent = Agent(
                organisation_id=org.id,
                name=name,
                description=str(spec["description"]),
                prompt_template=str(spec["prompt_template"]),
                account_id=account.id,
                is_executable=True,
                prompt_version_history=[],
                connector_type_refs=[],
                required_environment_capabilities=[],
                retry_policy={},
            )
            try:
                async with session.begin_nested():
                    session.add(agent)
                    await session.flush()
            except IntegrityError:
                result = await session.execute(select(Agent).where(Agent.organisation_id == org.id, Agent.name == name))
                agent = result.scalar_one_or_none()
                if agent is None:
                    raise
                _log.info("demo_seed.agent_recovered_after_conflict", extra={"agent_name": name})
            else:
                _log.info("demo_seed.agent_created", extra={"agent_name": name})
        else:
            # Converge spec-owned fields on existing rows.
            changed = False
            if agent.description != str(spec["description"]):
                agent.description = str(spec["description"])
                changed = True
            if agent.prompt_template != str(spec["prompt_template"]):
                agent.prompt_template = str(spec["prompt_template"])
                changed = True
            if changed:
                _log.info("demo_seed.agent_converged", extra={"agent_name": name})
        agent_ids[name] = agent.id
    return agent_ids


async def _seed_demo_triggers(
    session: AsyncSession,
    org: Organisation,
    account: Account,
    pipeline_lookup: dict[str, tuple[Pipeline, PipelineSnapshot]],
) -> None:
    """Idempotent demo triggers: a webhook (PR events) and a cron (daily release notes).

    Race-safe: savepoint + IntegrityError recovery on the (pipeline, trigger_type,
    config) key. Triggers are linked to existing pipelines.  Existing rows are
    converged to the current spec (config_json).
    """
    # Webhook trigger on PR Review & Triage pipeline.
    pr_pipeline, _ = pipeline_lookup["PR Review & Triage"]
    webhook_result = await session.execute(
        select(Trigger).where(
            Trigger.organisation_id == org.id,
            Trigger.pipeline_id == pr_pipeline.id,
            Trigger.trigger_type == "webhook",
        )
    )
    existing_webhook = webhook_result.scalar_one_or_none()
    webhook_config = {
        # No HMAC secret: the demo webhook is intentionally public-run-creation
        # (ADR 047). The demo viewer cannot trigger runs — the endpoint is
        # gated on the unguessable trigger id and the viewer role is read-only.
        "events": ["pull_request"],
        "payload_mapping": {},
    }
    if existing_webhook is None:
        trigger = Trigger(
            organisation_id=org.id,
            pipeline_id=pr_pipeline.id,
            trigger_type="webhook",
            active=True,
            config_json=webhook_config,
            account_id=account.id,
        )
        try:
            async with session.begin_nested():
                session.add(trigger)
                await session.flush()
        except IntegrityError:
            _log.info("demo_seed.webhook_trigger_recovered", extra={"org_id": str(org.id)})
        else:
            _log.info("demo_seed.webhook_trigger_created", extra={"trigger_id": str(trigger.id)})
    elif existing_webhook.config_json != webhook_config:
        existing_webhook.config_json = webhook_config
        _log.info("demo_seed.webhook_trigger_converged", extra={"org_id": str(org.id)})

    # Cron trigger on Release Notes Generator pipeline.
    rn_pipeline, _ = pipeline_lookup["Release Notes Generator"]
    cron_result = await session.execute(
        select(Trigger).where(
            Trigger.organisation_id == org.id,
            Trigger.pipeline_id == rn_pipeline.id,
            Trigger.trigger_type == "cron",
        )
    )
    existing_cron = cron_result.scalar_one_or_none()
    cron_config = {"description": "Weekly release notes generation"}
    if existing_cron is None:
        trigger = Trigger(
            organisation_id=org.id,
            pipeline_id=rn_pipeline.id,
            trigger_type="cron",
            active=True,
            cron_expression="0 9 * * 1",
            cron_timezone="UTC",
            config_json=cron_config,
            account_id=account.id,
        )
        try:
            async with session.begin_nested():
                session.add(trigger)
                await session.flush()
        except IntegrityError:
            _log.info("demo_seed.cron_trigger_recovered", extra={"org_id": str(org.id)})
        else:
            _log.info("demo_seed.cron_trigger_created", extra={"trigger_id": str(trigger.id)})
    elif existing_cron.config_json != cron_config:
        existing_cron.config_json = cron_config
        _log.info("demo_seed.cron_trigger_converged", extra={"org_id": str(org.id)})

    await _seed_ticket_ready_trigger(session, org, account, pipeline_lookup)


_TICKET_READY_TRIGGER_NAME = "Ticket ready"
_TICKET_READY_CONFIG: dict[str, Any] = {
    # Same public-run-creation posture as the PR webhook above (ADR 047); the
    # demo viewer is read-only and cannot fire it.
    "events": ["issue.ready"],
    "payload_mapping": {},
    "description": "Fires when a ticket moves to Ready in the tracker.",
}


async def _seed_ticket_ready_trigger(
    session: AsyncSession,
    org: Organisation,
    account: Account,
    pipeline_lookup: Mapping[str, tuple[Pipeline, PipelineSnapshot]],
) -> None:
    """Idempotent "Ticket ready" webhook trigger on the governance pipeline (FAR-1248).

    Keyed on the (organisation, pipeline, name) declarative identity (the live
    ``uq_triggers_org_pipeline_name`` index); config and active flag converge.
    """
    gov_pipeline, _ = pipeline_lookup[DEMO_PIPELINE_NAME]
    result = await session.execute(
        select(Trigger).where(
            Trigger.organisation_id == org.id,
            Trigger.pipeline_id == gov_pipeline.id,
            Trigger.name == _TICKET_READY_TRIGGER_NAME,
        )
    )
    existing = result.scalar_one_or_none()
    if existing is None:
        trigger = Trigger(
            organisation_id=org.id,
            pipeline_id=gov_pipeline.id,
            name=_TICKET_READY_TRIGGER_NAME,
            trigger_type="webhook",
            active=True,
            config_json=dict(_TICKET_READY_CONFIG),
            account_id=account.id,
        )
        try:
            async with session.begin_nested():
                session.add(trigger)
                await session.flush()
        except IntegrityError:
            _log.info("demo_seed.ticket_ready_trigger_recovered", extra={"org_id": str(org.id)})
        else:
            _log.info("demo_seed.ticket_ready_trigger_created", extra={"trigger_id": str(trigger.id)})
        return
    changed = False
    if existing.config_json != _TICKET_READY_CONFIG:
        existing.config_json = dict(_TICKET_READY_CONFIG)
        changed = True
    if existing.trigger_type != "webhook":
        existing.trigger_type = "webhook"
        changed = True
    if existing.active is not True:
        existing.active = True
        changed = True
    if changed:
        _log.info("demo_seed.ticket_ready_trigger_converged", extra={"org_id": str(org.id)})


_DEMO_LIFECYCLE_MAP_NAME = "Delivery lifecycle"


def _lifecycle_map_content(
    pipeline_lookup: Mapping[str, tuple[Pipeline, PipelineSnapshot]],
) -> dict[str, Any]:
    """Canonical ``content_json`` for the demo lifecycle map (FAR-1248).

    Already in the canonical shape ``core.lifecycle_map.validation.
    normalize_content`` produces (``type``/``source``/``target``), so storing
    it verbatim is equivalent to a save through the service; the unit tests
    assert ``normalize_content(content) == content``.
    """
    gov_pipeline, _ = pipeline_lookup[DEMO_PIPELINE_NAME]
    review_pipeline, _ = pipeline_lookup["PR Review & Triage"]
    return {
        "stages": [
            {
                "id": "ticket",
                "name": "Ticket",
                "type": "external",
                "description": "A groomed ticket is marked Ready in the tracker.",
                "owner": "Product",
                "x": 0,
                "y": 120,
            },
            {
                "id": "implement",
                "name": "Implement",
                "type": "modulo",
                "description": "Demo Governance Pipeline: implement, score PR risk, human review when risky, open PR.",
                "pipeline_id": str(gov_pipeline.id),
                "owner": "Engineering",
                "x": 260,
                "y": 120,
            },
            {
                "id": "review",
                "name": "Review",
                "type": "modulo",
                "description": "PR Review & Triage: automated review, severity and summary on the PR.",
                "pipeline_id": str(review_pipeline.id),
                "owner": "Engineering",
                "x": 520,
                "y": 120,
            },
            {
                "id": "deploy-staging",
                "name": "Deploy staging",
                "type": "external",
                "description": "CI deploys the merged change to staging and runs the end-to-end suite.",
                "owner": "Platform",
                "x": 780,
                "y": 120,
            },
            {
                "id": "deploy-prod",
                "name": "Deploy prod",
                "type": "manual",
                "description": "A release manager promotes the staging build to production.",
                "owner": "Platform",
                "x": 1040,
                "y": 120,
            },
        ],
        "edges": [
            {
                "id": "ticket-implement",
                "source": "ticket",
                "target": "implement",
                "trigger_type": "webhook",
                "description": "Ticket ready",
            },
            {
                "id": "implement-review",
                "source": "implement",
                "target": "review",
                "trigger_type": "webhook",
                "description": "PR opened",
            },
            {
                "id": "review-deploy-staging",
                "source": "review",
                "target": "deploy-staging",
                "trigger_type": "webhook",
                "description": "PR merged",
            },
            {
                "id": "deploy-staging-deploy-prod",
                "source": "deploy-staging",
                "target": "deploy-prod",
                "trigger_type": "manual",
                "description": "Staging checks green",
            },
        ],
        "notes": "Demo lifecycle: where the governance and PR review pipelines sit in delivery.",
    }


async def _derive_lifecycle_map_stages(session: AsyncSession, lifecycle_map: LifecycleMap) -> None:
    """Replace the map's ``lifecycle_map_stages`` projection rows from content_json.

    Same projection ``core.lifecycle_map.service.derive_lifecycle_map_stages``
    maintains (db must not import core, importlinter), restricted to the demo
    content's well-formed stages.
    """
    await session.execute(delete(LifecycleMapStage).where(LifecycleMapStage.map_id == lifecycle_map.id))
    for position, stage in enumerate(lifecycle_map.content_json.get("stages") or []):
        raw_pipeline = stage.get("pipeline_id")
        session.add(
            LifecycleMapStage(
                organisation_id=lifecycle_map.organisation_id,
                account_id=lifecycle_map.account_id,
                map_id=lifecycle_map.id,
                version=lifecycle_map.version,
                stage_id=stage["id"],
                stage_name=stage["name"],
                position=position,
                stage_type=stage["type"],
                pipeline_id=uuid.UUID(raw_pipeline) if raw_pipeline else None,
            )
        )
    await session.flush()


async def _seed_demo_lifecycle_map(
    session: AsyncSession,
    org: Organisation,
    account: Account,
    pipeline_lookup: Mapping[str, tuple[Pipeline, PipelineSnapshot]],
) -> None:
    """Idempotent "Delivery lifecycle" map for /lifecycle-maps (FAR-1248).

    Best-effort in its own savepoint (like the daily facts): a lifecycle-map
    failure must never cost the demo its pipelines and runs. Keyed on
    (organisation, name) among live maps; content and the stage projection
    converge, and an archived demo map is un-archived.
    """
    try:
        content = _lifecycle_map_content(pipeline_lookup)
        async with session.begin_nested():
            result = await session.execute(
                select(LifecycleMap)
                .where(LifecycleMap.organisation_id == org.id, LifecycleMap.name == _DEMO_LIFECYCLE_MAP_NAME)
                .order_by(LifecycleMap.created_at)
                .limit(1)
            )
            lifecycle_map = result.scalars().first()
            if lifecycle_map is None:
                lifecycle_map = LifecycleMap(
                    organisation_id=org.id,
                    name=_DEMO_LIFECYCLE_MAP_NAME,
                    description="Ticket to production: the delivery stages the demo pipelines automate.",
                    visibility="org",
                    version=1,
                    content_json=content,
                    account_id=account.id,
                    updated_by=account.id,
                )
                session.add(lifecycle_map)
                await session.flush()
                await _derive_lifecycle_map_stages(session, lifecycle_map)
                _log.info("demo_seed.lifecycle_map_created", extra={"map_id": str(lifecycle_map.id)})
                return
            if lifecycle_map.content_json != content or lifecycle_map.archived_at is not None:
                lifecycle_map.content_json = content
                lifecycle_map.archived_at = None
                await session.flush()
                await _derive_lifecycle_map_stages(session, lifecycle_map)
                _log.info("demo_seed.lifecycle_map_converged", extra={"map_id": str(lifecycle_map.id)})
    except Exception as exc:
        _log.warning("demo_seed.lifecycle_map_write_failed: %s", _safe_exc_text(exc))


async def _select_demo_org(session: AsyncSession) -> Organisation | None:
    """Deterministic single-row lookup for the demo org slug.

    ``organisations.slug`` uniqueness is a PARTIAL index (``deleted_at IS
    NULL``), so multiple soft-deleted 'demo' rows can coexist and a bare
    ``scalar_one_or_none`` would raise ``MultipleResultsFound`` on every boot.
    Mirrors the ``crud.organisation.get_organisation_by_slug`` defence:
    order live rows first, then soft-deleted rows most-recent first, and take
    one. (Organisation carries no ``updated_at``, so ``created_at`` is the
    recency tiebreaker.)
    """
    result = await session.execute(
        include_soft_deleted(
            select(Organisation)
            .where(Organisation.slug == DEMO_ORG_SLUG)
            .order_by(Organisation.deleted_at.is_not(None), Organisation.created_at.desc())
            .limit(1)
        )
    )
    return result.scalars().first()


def _undelete_demo_org(org: Organisation) -> Organisation:
    """Revive a soft-deleted demo org (the seed owns the slug's live row)."""
    if org.deleted_at is not None:
        org.deleted_at = None
        _log.info("demo_seed.org_undeleted", extra={"slug": DEMO_ORG_SLUG})
    return org


async def _get_or_create_demo_org(session: AsyncSession) -> Organisation:
    """Idempotently create the demo organisation (slug-unique, race-safe)."""
    org = await _select_demo_org(session)
    if org is not None:
        return _undelete_demo_org(org)
    org = Organisation(name=DEMO_ORG_NAME, slug=DEMO_ORG_SLUG, settings_json={})
    try:
        # Savepoint so a concurrent boot that already committed the slug only
        # rolls back this insert, not the surrounding seed transaction.
        async with session.begin_nested():
            session.add(org)
            await session.flush()
    except IntegrityError:
        org = await _select_demo_org(session)
        if org is None:
            raise
        _log.info("demo_seed.org_recovered_after_conflict", extra={"slug": DEMO_ORG_SLUG})
    else:
        _log.info("demo_seed.org_created", extra={"slug": DEMO_ORG_SLUG})
    # The recovered row may be soft-deleted too — the undelete repair applies
    # to every adoption path, not just the primary lookup.
    return _undelete_demo_org(org)


async def _seed_demo_account(session: AsyncSession, email: str, password: str) -> Account:
    """Idempotently create/update the demo user (password re-stamped from env).

    The insert is race-safe across multi-instance boots: a concurrent boot that
    already committed the email rolls back only this savepoint, then the seed
    adopts the winner row and runs the same drift-repair path on it.
    """
    from modulo.auth.passwords import hash_password, verify_password

    result = await session.execute(select(Account).where(Account.email == email))
    account = result.scalar_one_or_none()
    if account is None:
        account = Account(
            email=email,
            display_name="Demo",
            password_hash=hash_password(password),
            auth_provider="local",
            active=True,
            is_system_admin=False,
            must_change_password=False,
        )
        try:
            # Savepoint so a concurrent boot that already committed the email
            # only rolls back this insert, not the surrounding seed transaction.
            async with session.begin_nested():
                session.add(account)
                await session.flush()
        except IntegrityError:
            result = await session.execute(select(Account).where(Account.email == email))
            account = result.scalar_one_or_none()
            if account is None:
                raise
            _log.info("demo_seed.account_recovered_after_conflict", extra={"email": email})
        else:
            _log.info("demo_seed.account_created", extra={"email": email})
            return account

    changed = False
    # Re-stamp the hash every run when the stored hash no longer verifies
    # against the env password, so rotating MODULO_DEMO_PASSWORD takes effect
    # on the next boot without manual DB surgery. bcrypt hashes are salted, so
    # the comparison must go through verify_password (never hash-to-hash).
    # Corrupt-hash safety: a malformed stored hash must not crash the boot
    # seed — verify_password swallows bcrypt's ValueError, and the guard below
    # catches anything unexpected (e.g. a non-string hash read shape) and
    # re-stamps from env instead, matching the rotation intent.
    try:
        stored_hash = account.password_hash or ""
        stored_hash_verifies = bool(stored_hash) and verify_password(password, stored_hash)
    except Exception:
        _log.warning("demo_seed.account_hash_corrupt", extra={"email": email})
        stored_hash_verifies = False
    if not stored_hash_verifies:
        account.password_hash = hash_password(password)
        changed = True
    if account.active is not True:
        account.active = True
        changed = True
    # Defense: the demo account must never be a system admin.
    if account.is_system_admin is True:
        account.is_system_admin = False
        changed = True
    # A pre-existing account with must_change_password set would trap the demo
    # viewer in ForceChangePasswordView, whose mutation is viewer-denied — the
    # demo account must always be immediately usable.
    if account.must_change_password is not False:
        account.must_change_password = False
        changed = True
    if changed:
        _log.info("demo_seed.account_updated", extra={"email": email})
    return account


async def _seed_demo_membership(session: AsyncSession, account: Account, org: Organisation) -> None:
    """Idempotent viewer-role membership; forces a drifted role back to viewer.

    The insert is race-safe across multi-instance boots (savepoint +
    IntegrityError recovery on the (account, org) unique key), and the drift
    warning reports the role captured BEFORE the overwrite.
    """
    result = await session.execute(
        select(OrgMembership).where(
            OrgMembership.account_id == account.id,
            OrgMembership.organisation_id == org.id,
        )
    )
    membership = result.scalar_one_or_none()
    if membership is None:
        membership = OrgMembership(
            account_id=account.id,
            organisation_id=org.id,
            role=DEMO_ORG_ROLE,
        )
        try:
            # Savepoint so a concurrent boot that already committed the
            # (account, org) pair only rolls back this insert, not the
            # surrounding seed transaction.
            async with session.begin_nested():
                session.add(membership)
                await session.flush()
        except IntegrityError:
            result = await session.execute(
                select(OrgMembership).where(
                    OrgMembership.account_id == account.id,
                    OrgMembership.organisation_id == org.id,
                )
            )
            membership = result.scalar_one_or_none()
            if membership is None:
                raise
            _log.info("demo_seed.membership_recovered_after_conflict", extra={"email": account.email})
        else:
            _log.info("demo_seed.membership_created", extra={"email": account.email, "role": DEMO_ORG_ROLE})
            return
    if membership.role != DEMO_ORG_ROLE:
        # Capture BEFORE overwriting so the warning reports the actual
        # previous role, not the role we just wrote.
        previous_role = membership.role
        membership.role = DEMO_ORG_ROLE
        _log.warning(
            "demo_seed.membership_role_reset",
            extra={"email": account.email, "previous_role": previous_role, "role": DEMO_ORG_ROLE},
        )


_DEMO_TEAM_NAME = "Demo Engineering"


async def _seed_demo_team(session: AsyncSession, org: Organisation, account: Account) -> None:
    """Idempotent Demo Engineering team + demo user as viewer member.

    Race-safe: savepoint + IntegrityError recovery on the (org, name) unique
    key, matching the existing seed pattern.
    """
    result = await session.execute(select(Team).where(Team.organisation_id == org.id, Team.name == _DEMO_TEAM_NAME))
    team = result.scalar_one_or_none()
    if team is None:
        team = Team(
            organisation_id=org.id,
            name=_DEMO_TEAM_NAME,
            description="Demo team for the /demo visitor experience.",
            account_id=account.id,
            notification_endpoints=[],
            settings={},
        )
        try:
            async with session.begin_nested():
                session.add(team)
                await session.flush()
        except IntegrityError:
            result = await session.execute(
                select(Team).where(Team.organisation_id == org.id, Team.name == _DEMO_TEAM_NAME)
            )
            team = result.scalar_one_or_none()
            if team is None:
                raise
            _log.info("demo_seed.team_recovered_after_conflict", extra={"org_id": str(org.id)})
        else:
            _log.info("demo_seed.team_created", extra={"team_id": str(team.id)})

    # Team membership for the demo user.
    member_result = await session.execute(
        select(TeamMembership).where(
            TeamMembership.team_id == team.id,
            TeamMembership.account_id == account.id,
        )
    )
    if member_result.scalar_one_or_none() is None:
        try:
            async with session.begin_nested():
                session.add(
                    TeamMembership(
                        team_id=team.id,
                        account_id=account.id,
                        organisation_id=org.id,
                        role="viewer",
                    )
                )
                await session.flush()
        except IntegrityError:
            _log.info("demo_seed.team_membership_recovered_after_conflict", extra={"team_id": str(team.id)})
        else:
            _log.info("demo_seed.team_membership_created", extra={"team_id": str(team.id)})


async def _seed_demo_schemas(session: AsyncSession, org: Organisation, account: Account) -> None:
    """Published demo schemas (idempotent by (organisation, name)).

    Race-safe across multi-instance boots like the org/account/membership
    inserts: each insert runs in a savepoint; a concurrent boot that already
    committed the natural key only rolls back that savepoint, and the seed
    adopts the winner row and continues.  Existing rows are converged to the
    current spec (description + SchemaVersion definition_json).
    """
    for spec in _DEMO_SCHEMA_SPECS:
        result = await session.execute(
            select(Schema).where(Schema.organisation_id == org.id, Schema.name == spec["name"])
        )
        schema = result.scalar_one_or_none()
        if schema is None:
            schema = Schema(
                organisation_id=org.id,
                name=spec["name"],
                account_id=account.id,
                description=spec["description"],
            )
            try:
                # Savepoint: a concurrent boot that committed the same
                # (organisation, name) only rolls back this insert.
                async with session.begin_nested():
                    session.add(schema)
                    await session.flush()
            except IntegrityError:
                result = await session.execute(
                    select(Schema).where(Schema.organisation_id == org.id, Schema.name == spec["name"])
                )
                schema = result.scalar_one_or_none()
                if schema is None:
                    raise
                _log.info("demo_seed.schema_recovered_after_conflict", extra={"schema_name": spec["name"]})
            else:
                _log.info("demo_seed.schema_created", extra={"schema_name": spec["name"]})
        elif schema.description != str(spec["description"]):
            schema.description = str(spec["description"])
            _log.info("demo_seed.schema_converged", extra={"schema_name": spec["name"]})

        version_result = await session.execute(
            select(SchemaVersion).where(
                SchemaVersion.schema_id == schema.id,
                SchemaVersion.version == "v1",
                SchemaVersion.organisation_id == org.id,
            )
        )
        existing_version = version_result.scalar_one_or_none()
        if existing_version is not None:
            # Converge definition_json on existing published versions.
            desired_def = spec["definition"]
            if existing_version.definition_json != desired_def:
                existing_version.definition_json = desired_def  # type: ignore[assignment]
                _log.info(
                    "demo_seed.schema_version_converged",
                    extra={"schema_name": spec["name"], "version": "v1"},
                )
            continue
        try:
            # Savepoint: same multi-boot protection for the version row.
            async with session.begin_nested():
                session.add(
                    SchemaVersion(
                        organisation_id=org.id,
                        schema_id=schema.id,
                        version="v1",
                        version_number=1,
                        definition_json=spec["definition"],
                        published=True,
                        account_id=account.id,
                    )
                )
                await session.flush()
        except IntegrityError:
            version_result = await session.execute(
                select(SchemaVersion).where(
                    SchemaVersion.schema_id == schema.id,
                    SchemaVersion.version == "v1",
                    SchemaVersion.organisation_id == org.id,
                )
            )
            if version_result.scalar_one_or_none() is None:
                raise
            _log.info("demo_seed.schema_version_recovered_after_conflict", extra={"schema_name": spec["name"]})
        else:
            _log.info("demo_seed.schema_version_created", extra={"schema_name": spec["name"], "version": "v1"})


async def seed_demo(session: AsyncSession) -> str | None:
    """Seed the demo org, demo user, and sample data. Idempotent.

    Returns a summary string when the demo feature is configured (something may
    have been created or already existed), or ``None`` when the feature is not
    configured (a deliberate no-op — the caller logs its own outcome).
    """
    settings = get_settings()
    config = demo_login_config(settings)
    if config is None:
        _log.info("demo_seed.disabled")
        return None
    # ADR 005: the demo seed creates a second org (slug "demo"). In single-org
    # deployments (modulo_multi_org_enabled=False), this must not happen — the
    # demo login endpoint returns 404 and the demo org is never created.
    if not settings.modulo_multi_org_enabled:
        _log.info("demo_seed.skipped_single_org")
        return None
    email, password = config

    # Org/account/membership writes follow the boot-seed pattern (system
    # context, no org RLS) used by _seed_modulo_users / seed_demo_org.
    org = await _get_or_create_demo_org(session)
    account = await _seed_demo_account(session, email, password)
    await _seed_demo_membership(session, account, org)

    # Operator-misconfiguration observability: MODULO_DEMO_USER should name a
    # dedicated account. If it named an existing account with memberships in
    # other orgs, the demo endpoint still only ever mints the demo-org viewer
    # session (see auth._resolve_demo_org_membership) — but flag the stray
    # memberships loudly so the operator can point the env at a fresh account.
    # The join filters soft-deleted orgs (deleted_at IS NULL) so resurrected
    # or expired memberships cannot inflate other_org_count.
    stray_org_ids = (
        (
            await session.execute(
                select(OrgMembership.organisation_id)
                .join(Organisation, Organisation.id == OrgMembership.organisation_id)
                .where(
                    OrgMembership.account_id == account.id,
                    OrgMembership.organisation_id != org.id,
                    Organisation.deleted_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    if stray_org_ids:
        _log.warning(
            "demo_seed.account_has_memberships_outside_demo_org",
            extra={"email": email, "other_org_count": len(stray_org_ids)},
        )

    # Org-scoped entity writes run under the documented execution context so
    # Postgres RLS admits them (mirrors seed_cost_components_for_org).
    await set_rls_org(session, org.id)
    await set_rls_execution_context(session)
    await _seed_demo_team(session, org, account)
    await _seed_demo_schemas(session, org, account)
    agent_ids = await _seed_demo_agents(session, org, account)
    # Pipelines (graph + edges + snapshot) + runs (with node-level traces) +
    # daily facts; returns the lookup for triggers and the lifecycle map.
    pipeline_lookup = await _seed_demo_pipeline_and_runs(session, org, account, agent_ids)
    await _seed_demo_triggers(session, org, account, pipeline_lookup)
    await _seed_demo_lifecycle_map(session, org, account, pipeline_lookup)

    return f"org={DEMO_ORG_SLUG} user={email}"


async def seed_demo_runtime(session_factory: async_sessionmaker[AsyncSession] | None = None) -> str | None:
    """Run ``seed_demo`` in its own transaction on a session factory.

    The single transaction wrapper for both callers: main.py's boot lifespan
    passes its DI ``get_or_create_session_factory`` engine-backed factory (one
    engine path per caller), while the ``python -m modulo.db.seed_demo``
    entry point below falls back to the shared module-level
    ``AsyncSessionLocal``.
    """
    factory = session_factory
    if factory is None:
        from modulo.db.session import AsyncSessionLocal

        factory = AsyncSessionLocal
    async with factory() as session, session.begin():
        return await seed_demo(session)


def main() -> None:
    """Standalone entry: seed the demo org/user when configured, then exit."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    if demo_login_config(get_settings()) is None:
        print("[demo-seed] disabled — set MODULO_DEMO_ENABLED, MODULO_DEMO_USER, MODULO_DEMO_PASSWORD")  # noqa: T201
        return
    try:
        summary = asyncio.run(seed_demo_runtime())
    except Exception as exc:
        detail = _safe_exc_text(exc)
        # Sanitized type + message only — NO exc_info. SQLAlchemy reprs embed
        # bind params (this seed's include the demo password hash), and
        # traceback rendering re-embeds them via str(exc), so the raw
        # exception/traceback must never reach stdout or the logs.
        _log.error("demo_seed.failed", extra={"error": detail})
        print(f"[demo-seed] FAILED ({detail})", flush=True)  # noqa: T201
        sys.exit(1)
    print(f"[demo-seed] ok ({summary})", flush=True)  # noqa: T201


if __name__ == "__main__":
    main()
