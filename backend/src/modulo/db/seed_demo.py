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
  Notes Generator, Docs Sync, plus the original Demo Governance Pipeline),
* 20 synthetic terminal runs spread over 14 days (mixed statuses:
  complete/failed/awaiting_human), realistic tokens/costs/durations,
* 2 agents with realistic prompts and I/O schema references,
* a webhook trigger (GitHub PR events) and a cron trigger (daily release
  notes),
* RunDailyFact rows for analytics surface seeding.

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
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from modulo.core.demo import DEMO_ORG_ROLE, DEMO_ORG_SLUG, demo_login_config
from modulo.db.models.account import Account
from modulo.db.models.agent import Agent
from modulo.db.models.org_membership import OrgMembership
from modulo.db.models.organisation import Organisation
from modulo.db.models.pipeline import Pipeline
from modulo.db.models.pipeline_snapshot import PipelineSnapshot
from modulo.db.models.run import Run
from modulo.db.models.run_daily_facts import RunDailyFact
from modulo.db.models.schema import Schema, SchemaVersion
from modulo.db.models.team import Team
from modulo.db.models.team_membership import TeamMembership
from modulo.db.models.trigger import Trigger
from modulo.db.rls import set_rls_execution_context, set_rls_org
from modulo.settings import get_settings

_log = logging.getLogger(__name__)

DEMO_ORG_NAME = "Demo"

DEMO_PIPELINE_NAME = "Demo Governance Pipeline"

# Expanded pipeline names (FAR-977).
_DEMO_PIPELINES: list[dict[str, object]] = [
    {
        "name": "PR Review & Triage",
        "description": "Reviews incoming PRs, classifies severity, and posts review comments.",
        "nodes": [
            {
                "id": "classify",
                "node_type": "agent",
                "label": "Classify PR",
                "position": {"x": 100, "y": 100},
                "config": {
                    "agent_prompt": (
                        "Classify this PR by type (feature, bugfix, refactor, "
                        "docs) and severity (low, medium, high, critical)."
                    )
                },
            },
            {
                "id": "review",
                "node_type": "agent",
                "label": "Code Review",
                "position": {"x": 400, "y": 100},
                "config": {
                    "agent_prompt": (
                        "Review the PR diff for correctness, security, and style issues. Post inline comments."
                    )
                },
            },
            {
                "id": "summarize",
                "node_type": "agent",
                "label": "Post Summary",
                "position": {"x": 700, "y": 100},
                "config": {"agent_prompt": "Post a concise summary of the review findings as a PR comment."},
            },
        ],
        "edges": [
            {"id": "pr-e1", "source": "classify", "target": "review"},
            {"id": "pr-e2", "source": "review", "target": "summarize"},
        ],
    },
    {
        "name": "Release Notes Generator",
        "description": "Collects merged PRs since the last release and generates formatted release notes.",
        "nodes": [
            {
                "id": "collect",
                "node_type": "agent",
                "label": "Collect PRs",
                "position": {"x": 100, "y": 100},
                "config": {
                    "agent_prompt": (
                        "List all PRs merged since the last release tag. Group by type (features, fixes, breaking)."
                    )
                },
            },
            {
                "id": "format",
                "node_type": "agent",
                "label": "Format Notes",
                "position": {"x": 400, "y": 100},
                "config": {
                    "agent_prompt": (
                        "Format the grouped PRs into markdown release notes "
                        "with sections for Features, Fixes, and Breaking Changes."
                    )
                },
            },
        ],
        "edges": [
            {"id": "rn-e1", "source": "collect", "target": "format"},
        ],
    },
    {
        "name": "Docs Sync",
        "description": "Detects code changes and updates relevant documentation pages.",
        "nodes": [
            {
                "id": "diff",
                "node_type": "agent",
                "label": "Detect Changes",
                "position": {"x": 100, "y": 100},
                "config": {
                    "agent_prompt": (
                        "Compare the latest commit against the previous release. "
                        "Identify files with user-facing API changes."
                    )
                },
            },
            {
                "id": "update-docs",
                "node_type": "agent",
                "label": "Update Docs",
                "position": {"x": 400, "y": 100},
                "config": {
                    "agent_prompt": ("For each changed file, find and update the corresponding documentation page.")
                },
            },
            {
                "id": "validate-links",
                "node_type": "agent",
                "label": "Validate Links",
                "position": {"x": 700, "y": 100},
                "config": {
                    "agent_prompt": "Verify all internal links in updated docs still resolve. Report broken links."
                },
            },
        ],
        "edges": [
            {"id": "ds-e1", "source": "diff", "target": "update-docs"},
            {"id": "ds-e2", "source": "update-docs", "target": "validate-links"},
        ],
    },
]

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
]

# Run specs for the expanded seed (FAR-977): (run_number, status, trigger_type,
# pipeline_name, total_tokens, total_cost_usd, days_ago, hours_into_day).
# Spread over 14 days with realistic patterns.
_DEMO_RUN_SPECS: list[tuple[int, str, str, str, int, float, int, int]] = [
    # Today (day 0)
    (1, "complete", "manual", "Demo Governance Pipeline", 1840, 0.0042, 0, 2),
    (2, "failed", "manual", "Demo Governance Pipeline", 210, 0.0005, 0, 4),
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
    (9, "complete", "manual", "Demo Governance Pipeline", 2200, 0.0051, 4, 8),
    (10, "complete", "webhook", "PR Review & Triage", 3500, 0.0081, 4, 13),
    # Day 5
    (11, "complete", "cron", "Release Notes Generator", 4300, 0.0099, 5, 9),
    (12, "complete", "manual", "Demo Governance Pipeline", 1950, 0.0045, 5, 17),
    # Day 7
    (13, "complete", "webhook", "PR Review & Triage", 3100, 0.0072, 7, 10),
    (14, "failed", "manual", "Docs Sync", 650, 0.0015, 7, 14),
    # Day 9
    (15, "complete", "cron", "Release Notes Generator", 3800, 0.0088, 9, 9),
    (16, "complete", "webhook", "PR Review & Triage", 2700, 0.0062, 9, 16),
    # Day 11
    (17, "complete", "manual", "Demo Governance Pipeline", 2100, 0.0049, 11, 11),
    (18, "complete", "cron", "Docs Sync", 1600, 0.0037, 11, 15),
    # Day 13
    (19, "complete", "webhook", "PR Review & Triage", 3300, 0.0076, 13, 10),
    (20, "failed", "manual", "PR Review & Triage", 420, 0.0010, 13, 14),
]

# The seeded demo run's single node output (FAR-583: written to
# run_node_outputs via the repo REPLACE, no longer a legacy runs column).
_DEMO_OUTPUT_NODE_ID = "demo"
_DEMO_OUTPUT_TEXT = "Sample demo output — synthetic, no agent execution."

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


def _demo_pipeline_graph() -> list[dict[str, object]]:
    """Four-node demo pipeline graph (expanded for FAR-977)."""
    return [
        {
            "id": "demo-intake",
            "node_type": "agent",
            "label": "Demo: Intake",
            "position": {"x": 100, "y": 100},
            "config": {"agent_prompt": "Parse the demo input payload and extract key fields."},
        },
        {
            "id": "demo-classify",
            "node_type": "agent",
            "label": "Demo: Classify",
            "position": {"x": 300, "y": 100},
            "config": {"agent_prompt": "Classify the input by type and assign a priority."},
        },
        {
            "id": "demo-process",
            "node_type": "agent",
            "label": "Demo: Process",
            "position": {"x": 500, "y": 100},
            "config": {"agent_prompt": "Process the classified input and generate a result."},
        },
        {
            "id": "demo-report",
            "node_type": "agent",
            "label": "Demo: Report",
            "position": {"x": 700, "y": 100},
            "config": {"agent_prompt": "Write a short demo report from the processed result."},
        },
    ]


def _demo_graph_json(nodes: list[dict[str, object]]) -> dict[str, object]:
    edges = [
        {"id": f"demo-edge-{i + 1}", "source": nodes[i]["id"], "target": nodes[i + 1]["id"]}
        for i in range(len(nodes) - 1)
    ]
    return {"nodes": nodes, "edges": edges}


def _pipeline_graph_json(pipeline_spec: dict[str, object]) -> dict[str, object]:
    """Build a graph_json from a pipeline spec's nodes and edges."""
    return {"nodes": pipeline_spec["nodes"], "edges": pipeline_spec["edges"]}


async def _get_or_create_pipeline(
    session: AsyncSession,
    org: Organisation,
    account: Account,
    name: str,
    description: str,
    graph_nodes: list[dict[str, object]],
) -> Pipeline:
    """Idempotently create a pipeline by (org, name)."""
    result = await session.execute(select(Pipeline).where(Pipeline.organisation_id == org.id, Pipeline.name == name))
    pipeline = result.scalar_one_or_none()
    if pipeline is None:
        pipeline = Pipeline(
            organisation_id=org.id,
            name=name,
            description=description,
            account_id=account.id,
            visibility="org",
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
    return pipeline


async def _get_or_create_snapshot(
    session: AsyncSession,
    org: Organisation,
    pipeline: Pipeline,
    graph_json: dict[str, object],
) -> PipelineSnapshot:
    """Idempotently create a snapshot v1 for a pipeline."""
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
    return snapshot


async def _seed_demo_pipeline_and_runs(
    session: AsyncSession, org: Organisation, account: Account
) -> dict[str, tuple[Pipeline, PipelineSnapshot]]:
    """Seed all demo pipelines, their snapshots, and expanded runs + daily facts.

    Race-safe across multi-instance boots: each insert runs in a savepoint with
    IntegrityError recovery on its natural key.

    Returns the pipeline/snapshot lookup so callers (trigger seeding) can reuse it
    instead of re-querying.
    """
    # Build pipeline lookup: name -> (pipeline, snapshot)
    pipeline_lookup: dict[str, tuple[Pipeline, PipelineSnapshot]] = {}

    # Original Demo Governance Pipeline (expanded to 4 nodes).
    gov_nodes = _demo_pipeline_graph()
    gov_pipeline = await _get_or_create_pipeline(
        session,
        org,
        account,
        DEMO_PIPELINE_NAME,
        "Demo sample pipeline — read-only demo data (FAR-535).",
        gov_nodes,
    )
    gov_snapshot = await _get_or_create_snapshot(session, org, gov_pipeline, _demo_graph_json(gov_nodes))
    pipeline_lookup[DEMO_PIPELINE_NAME] = (gov_pipeline, gov_snapshot)

    # Additional pipelines from _DEMO_PIPELINES.
    for spec in _DEMO_PIPELINES:
        name = str(spec["name"])
        desc = str(spec["description"])
        nodes = spec["nodes"]
        pipeline = await _get_or_create_pipeline(session, org, account, name, desc, nodes)  # type: ignore[arg-type]
        graph_json = _pipeline_graph_json(spec)
        snapshot = await _get_or_create_snapshot(session, org, pipeline, graph_json)
        pipeline_lookup[name] = (pipeline, snapshot)

    # Deterministic, idempotent runs: fixed run_numbers with per-number
    # existence checks. Spread over 14 days with mixed statuses.
    for (
        run_number,
        status,
        trigger_type,
        pipeline_name,
        total_tokens,
        total_cost_usd,
        days_ago,
        hours_into_day,
    ) in _DEMO_RUN_SPECS:
        existing_result = await session.execute(
            select(Run.id).where(Run.organisation_id == org.id, Run.run_number == run_number)
        )
        if existing_result.scalar_one_or_none() is not None:
            continue

        if pipeline_name not in pipeline_lookup:
            _log.warning(
                "demo_seed.run_spec_unknown_pipeline",
                extra={"pipeline_name": pipeline_name, "run_number": run_number},
            )
            continue
        pipeline, snapshot = pipeline_lookup[pipeline_name]
        started = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(
            days=days_ago, hours=-hours_into_day
        )
        # Duration varies by pipeline type: 2-8 min for PR Review, 3-12 min for Release Notes, etc.
        duration_minutes = {
            "PR Review & Triage": 4,
            "Release Notes Generator": 8,
            "Docs Sync": 5,
            DEMO_PIPELINE_NAME: 3,
        }.get(pipeline_name, 4)
        completed = started + timedelta(minutes=duration_minutes)

        thread_id = f"demo-seed-{org.id}-{run_number}"
        run = Run(
            organisation_id=org.id,
            pipeline_id=pipeline.id,
            snapshot_id=snapshot.id,
            account_id=account.id,
            trigger_type=trigger_type,
            status=status,
            run_number=run_number,
            input_hash=hashlib.sha256(thread_id.encode()).hexdigest(),
            langgraph_thread_id=thread_id,
            started_at=started,
            completed_at=completed if status != "awaiting_human" else None,
            total_tokens=total_tokens,
            total_cost_usd=Decimal(str(total_cost_usd)),
            error_detail="Demo sample failure — no real work was performed." if status == "failed" else None,
            error_code="DEMO_SAMPLE" if status == "failed" else None,
        )
        try:
            async with session.begin_nested():
                session.add(run)
                await session.flush()
                # FAR-583: write node outputs best-effort.
                try:
                    from modulo.db.crud.run_node_outputs import replace_run_node_outputs

                    async with session.begin_nested():
                        await replace_run_node_outputs(
                            session,
                            run_id=run.id,
                            organisation_id=org.id,
                            outputs={_DEMO_OUTPUT_NODE_ID: _DEMO_OUTPUT_TEXT},
                            telemetry=None,
                        )
                except Exception as exc:
                    _log.warning(
                        "demo_seed.run_outputs_write_failed run=%s: %s",
                        run.id,
                        _safe_exc_text(exc),
                    )
        except IntegrityError:
            existing_result = await session.execute(
                select(Run.id).where(Run.organisation_id == org.id, Run.run_number == run_number)
            )
            if existing_result.scalar_one_or_none() is None:
                raise
            _log.info("demo_seed.run_recovered_after_conflict", extra={"run_number": run_number})
        else:
            _log.info("demo_seed.run_created", extra={"run_number": run_number, "status": status})

            # Write RunDailyFact for analytics (best-effort, own savepoint).
            try:
                async with session.begin_nested():
                    # Mirror the Run row: non-terminal statuses have no
                    # completed_at / duration_ms.
                    # nosemgrep: raw-status-complete — seed script, not routing code.
                    is_terminal = status in ("complete", "failed")
                    fact = RunDailyFact(
                        organisation_id=org.id,
                        run_id=run.id,
                        run_date=started.date(),
                        pipeline_id=pipeline.id,
                        pipeline_name=pipeline_name,
                        trigger_type=trigger_type,
                        status=status,
                        total_cost_usd=Decimal(str(total_cost_usd)),
                        total_tokens=total_tokens,
                        duration_ms=(int((completed - started).total_seconds() * 1000) if is_terminal else None),
                        run_number=run_number,
                        started_at=started,
                        completed_at=completed if is_terminal else None,
                    )
                    session.add(fact)
                    await session.flush()
            except IntegrityError:
                _log.info("demo_seed.daily_fact_recovered_after_conflict", extra={"run_number": run_number})
            except Exception as exc:
                _log.warning(
                    "demo_seed.daily_fact_write_failed run=%s: %s",
                    run.id,
                    _safe_exc_text(exc),
                )

    return pipeline_lookup


async def _seed_demo_agents(session: AsyncSession, org: Organisation, account: Account) -> None:
    """Idempotent demo agents (FAR-977).

    Race-safe: savepoint + IntegrityError recovery on the (org, name) key.
    """
    for spec in _DEMO_AGENT_SPECS:
        name = str(spec["name"])
        result = await session.execute(select(Agent).where(Agent.organisation_id == org.id, Agent.name == name))
        if result.scalar_one_or_none() is not None:
            continue
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
            if result.scalar_one_or_none() is None:
                raise
            _log.info("demo_seed.agent_recovered_after_conflict", extra={"agent_name": name})
        else:
            _log.info("demo_seed.agent_created", extra={"agent_name": name})


async def _seed_demo_triggers(
    session: AsyncSession,
    org: Organisation,
    account: Account,
    pipeline_lookup: dict[str, tuple[Pipeline, PipelineSnapshot]],
) -> None:
    """Idempotent demo triggers: a webhook (PR events) and a cron (daily release notes).

    Race-safe: savepoint + IntegrityError recovery on the (pipeline, trigger_type,
    config) key. Triggers are linked to existing pipelines.
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
    if webhook_result.scalar_one_or_none() is None:
        trigger = Trigger(
            organisation_id=org.id,
            pipeline_id=pr_pipeline.id,
            trigger_type="webhook",
            active=True,
            config_json={
                # Intentional non-secret placeholder for the read-only
                # demo — not a leaked credential.
                "secret": "demo-webhook-secret",
                "events": ["pull_request"],
                "payload_mapping": {},
            },
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

    # Cron trigger on Release Notes Generator pipeline.
    rn_pipeline, _ = pipeline_lookup["Release Notes Generator"]
    cron_result = await session.execute(
        select(Trigger).where(
            Trigger.organisation_id == org.id,
            Trigger.pipeline_id == rn_pipeline.id,
            Trigger.trigger_type == "cron",
        )
    )
    if cron_result.scalar_one_or_none() is None:
        trigger = Trigger(
            organisation_id=org.id,
            pipeline_id=rn_pipeline.id,
            trigger_type="cron",
            active=True,
            cron_expression="0 9 * * 1",
            cron_timezone="UTC",
            config_json={"description": "Weekly release notes generation"},
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
        select(Organisation)
        .where(Organisation.slug == DEMO_ORG_SLUG)
        .order_by(Organisation.deleted_at.is_not(None), Organisation.created_at.desc())
        .limit(1)
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
    adopts the winner row and continues.
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
        version_result = await session.execute(
            select(SchemaVersion).where(
                SchemaVersion.schema_id == schema.id,
                SchemaVersion.version == "v1",
                SchemaVersion.organisation_id == org.id,
            )
        )
        if version_result.scalar_one_or_none() is not None:
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
    await _seed_demo_agents(session, org, account)
    # Pipelines + runs + daily facts (returns lookup for triggers).
    pipeline_lookup = await _seed_demo_pipeline_and_runs(session, org, account)
    await _seed_demo_triggers(session, org, account, pipeline_lookup)

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
