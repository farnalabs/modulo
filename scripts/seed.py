#!/usr/bin/env -S uv run
"""Modulo Demo Seed — lots of realistic fake data for a rich demo experience.

Idempotent: if the target org exists, skips all seeding and exits cleanly.

Usage:
    cd codebase/backend
    uv run ../scripts/seed.py
"""

import asyncio
import hashlib
import json
import os
import sys
import tempfile
import uuid
from datetime import UTC, datetime, timedelta

import bcrypt
from sqlalchemy import select

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.abspath(os.path.join(_SCRIPT_DIR, ".."))
# Try container layout (/app/src/) first, then local dev layout (backend/src/)
_SRC = os.path.join(_PROJECT_ROOT, "src")
if not os.path.isdir(_SRC):
    _SRC = os.path.join(_PROJECT_ROOT, "backend", "src")
if not os.path.isdir(_SRC):
    _SRC = os.path.join(_PROJECT_ROOT, "..", "backend", "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from modulo.db.session import AsyncSessionLocal
from modulo.db.models import (
    Agent,
    ConnectorInstance,
    EvalDefinition,
    EvalResult,
    FeedbackRecord,
    LibraryPrimitive,
    ModelBackend,
    OrgApiKey,
    Organisation,
    Pipeline,
    PipelineEdge,
    PipelineSnapshot,
    PrimitiveRating,
    Run,
    Schema,
    SchemaVersion,
    Stage,
    Team,
    TeamMembership,
    User,
)
from modulo.core.audit_logger import append_audit_event

SEED_PASSWORD = os.environ.get("MODULO_SEED_PASSWORD", "admin123")
SEED_ORG_NAME = os.environ.get("MODULO_SEED_ORG_NAME", "Demo Corp")


def _input_hash(payload: dict) -> str:
    s = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(s.encode()).hexdigest()


async def seed() -> None:
    print(f"[seed] Starting — org='{SEED_ORG_NAME}'")
    async with AsyncSessionLocal() as session:
        try:
            # Use existing first org (where MODULO_USERS users live) rather than creating a new one
            existing = (await session.execute(
                select(Organisation).order_by(Organisation.created_at).limit(1)
            )).scalar_one_or_none()
            if existing is None:
                print("[seed] No org found — creating")
                existing = Organisation(name=SEED_ORG_NAME, slug=SEED_ORG_NAME.lower().replace(" ", "-"), status="active", settings_json={}, otel_config_json={})
                session.add(existing)
                await session.flush()

            org = existing
            # Check if seed data already exists in this org (idempotency)
            seed_check = (await session.execute(
                select(User).where(User.organisation_id == org.id, User.email == "admin@demo.modulo")
            )).first()
            pipelines_exist = (await session.execute(
                select(Pipeline).where(Pipeline.organisation_id == org.id).limit(1)
            )).first()
            if seed_check is not None and pipelines_exist is not None:
                print(f"[seed] Org '{org.name}' already has seeded data — skipping")
                await session.commit()
                return
            print(f"[seed] Using org '{org.name}' ({org.id})")

            # ── Users (12) ────────────────────────────────────────────────
            pw_hash = bcrypt.hashpw(SEED_PASSWORD.encode(), bcrypt.gensalt()).decode()
            user_defs = [
                ("admin@demo.modulo", "Admin User", "admin"),
                ("alice@demo.modulo", "Alice Chen", "admin"),
                ("bob@demo.modulo", "Bob Martinez", "operator"),
                ("carol@demo.modulo", "Carol Singh", "operator"),
                ("dave@demo.modulo", "Dave Kim", "runner"),
                ("eve@demo.modulo", "Eve Johnson", "runner"),
                ("frank@demo.modulo", "Frank Okafor", "runner"),
                ("grace@demo.modulo", "Grace Lee", "runner"),
                ("hank@demo.modulo", "Hank Patel", "viewer"),
                ("iris@demo.modulo", "Iris Tanaka", "runner"),
                ("jack@demo.modulo", "Jack Wilson", "runner"),
                ("kate@demo.modulo", "Kate Mueller", "viewer"),
            ]
            users = []
            for email, display_name, org_role in user_defs:
                existing = (await session.execute(
                    select(User).where(User.organisation_id == org.id, User.email == email)
                )).scalar_one_or_none()
                if existing is not None:
                    users.append(existing)
                else:
                    u = User(
                        organisation_id=org.id, email=email, display_name=display_name,
                        password_hash=pw_hash, org_role=org_role, auth_provider="local",
                    )
                    session.add(u)
                    await session.flush()
                    users.append(u)
            # Ensure admin user (first in list) is marked as admin role
            admin = users[0]
            if admin.org_role != "admin":
                admin.org_role = "admin"
            print(f"[seed] {len(users)} users (admin={admin.email})")

            # ── Teams (3) + memberships ───────────────────────────────────
            team_defs_data = [
                ("Engineering", "Core platform engineering team", [admin, users[2], users[4], users[6], users[9]]),
                ("Product", "Product management and design", [users[1], users[3], users[5], users[8]]),
                ("Operations", "DevOps and infrastructure", [users[7], users[10], users[11]]),
            ]
            teams = []
            for name, desc, members in team_defs_data:
                t = (await session.execute(
                    select(Team).where(Team.organisation_id == org.id, Team.name == name)
                )).scalar_one_or_none()
                if t is None:
                    t = Team(organisation_id=org.id, name=name, description=desc, created_by=admin.id)
                    session.add(t)
                    await session.flush()
                for m in members:
                    existing_m = (await session.execute(
                        select(TeamMembership).where(TeamMembership.team_id == t.id, TeamMembership.user_id == m.id)
                    )).first()
                    if not existing_m:
                        session.add(TeamMembership(organisation_id=org.id, team_id=t.id, user_id=m.id))
                await session.flush()
                teams.append(t)
            print(f"[seed] 3 teams with memberships")

            # ── Schemas (6) + SchemaVersions ──────────────────────────────
            schema_defs_data = [
                ("PR Input", "pr-input", "Input data for a PR review", {"type": "object", "properties": {"title": {"type": "string"}, "description": {"type": "string"}, "files": {"type": "array", "items": {"type": "string"}}}}),
                ("Code Review Output", "code-review-output", "Output of a code review", {"type": "object", "properties": {"comments": {"type": "array"}, "score": {"type": "integer"}, "approved": {"type": "boolean"}}}),
                ("Release Manifest", "release-manifest", "Release metadata", {"type": "object", "properties": {"version": {"type": "string"}, "changelog": {"type": "string"}, "tag": {"type": "string"}}}),
                ("Incident Report", "incident-report", "Incident data", {"type": "object", "properties": {"severity": {"type": "string"}, "service": {"type": "string"}, "summary": {"type": "string"}}}),
                ("Customer Feedback", "customer-feedback", "Customer feedback entry", {"type": "object", "properties": {"rating": {"type": "integer"}, "comment": {"type": "string"}, "source": {"type": "string"}}}),
                ("Deployment Config", "deployment-config", "Deployment configuration", {"type": "object", "properties": {"env": {"type": "string"}, "region": {"type": "string"}, "replicas": {"type": "integer"}}}),
            ]
            schemas = []
            for sname, slug_name, desc, schema_json in schema_defs_data:
                s = (await session.execute(
                    select(Schema).where(Schema.organisation_id == org.id, Schema.name == sname)
                )).scalar_one_or_none()
                if s is None:
                    s = Schema(organisation_id=org.id, name=sname, description=desc, created_by=admin.id)
                    session.add(s)
                    await session.flush()
                    sv = SchemaVersion(organisation_id=org.id, schema_id=s.id, version="1.0", version_number=1, definition_json=schema_json, created_by=admin.id)
                    session.add(sv)
                await session.flush()
                schemas.append((s, sv))
            print(f"[seed] 6 schemas + versions")

            # ── Model Backends (4) ────────────────────────────────────────
            mb_defs = [
                ("GPT-4o", "openai", "gpt-4o", {"max_tokens": 8192, "temperature": 0.7}),
                ("Claude 3.5 Sonnet", "anthropic", "claude-3-5-sonnet", {"max_tokens": 8192}),
                ("Claude 3 Haiku", "anthropic", "claude-3-haiku", {"max_tokens": 4096}),
                ("GPT-4o Mini", "openai", "gpt-4o-mini", {"max_tokens": 16384, "temperature": 0.3}),
            ]
            backends = []
            for name, provider, model, config in mb_defs:
                mb = ModelBackend(organisation_id=org.id, name=name, display_name=name, provider=provider, model_id=model, default_params=config, credentials_ciphertext=b"", created_by=admin.id, visibility="org")
                session.add(mb)
                await session.flush()
                backends.append(mb)
            print(f"[seed] 4 model backends")

            # ── Agents (8) ────────────────────────────────────────────────
            agent_defs = [
                ("PR Reviewer", "Reviews pull request code changes", 0, 1, 0, backends[0].id,
                 "Review the following code diff. Identify bugs, style issues, security concerns, and performance problems. Be specific and actionable.\n\nDiff:\n{{ input }}", [{"connector_type": "github", "capabilities": ["issue_read"]}]),
                ("Release Manager", "Manages the release process", 2, 2, 1, backends[1].id,
                 "You are a release manager. Given the release data, bump the version, generate a changelog, and prepare release notes.\n\n{{ input }}", []),
                ("Incident Classifier", "Classifies incident severity", 3, 3, 0, backends[2].id,
                 "Classify the following incident as CRITICAL, HIGH, MEDIUM, or LOW based on impact and urgency.\n\nIncident:\n{{ input }}", []),
                ("Sentiment Analyzer", "Analyses customer feedback sentiment", 4, 4, 0, backends[3].id,
                 "Analyse the following customer feedback for sentiment (positive/negative/neutral), key themes, and urgency.\n\nFeedback:\n{{ input }}", []),
                ("Deployment Planner", "Plans deployment rollout", 5, 5, 0, backends[0].id,
                 "Given the deployment config, plan the rollout strategy including canary percentage, rollback criteria, and health check intervals.\n\nConfig:\n{{ input }}", [{"connector_type": "shell", "capabilities": ["read"]}]),
                ("Changelog Writer", "Writes changelogs from commit data", 4, 2, 1, backends[1].id,
                 "Generate a clear, well-formatted changelog from the following commit messages.\n\nCommits:\n{{ input }}", []),
                ("Code Quality Analyser", "Analyses code quality metrics", 0, 1, 0, backends[3].id,
                 "Analyse the following code for: 1) Cyclomatic complexity, 2) Code duplication, 3) Test coverage gaps, 4) Documentation quality.\n\nCode:\n{{ input }}", []),
                ("Onboarding Assistant", "Helps new users get started", 4, 4, 0, backends[2].id,
                 "You are a friendly onboarding assistant. Guide the new user through setting up their first pipeline.\n\nUser context:\n{{ input }}", []),
            ]
            agents_list = []
            for name, desc, in_schema_idx, out_schema_idx, _mb_idx, mb_id, prompt_template, connector_refs in agent_defs:
                in_s = schemas[in_schema_idx][0]
                out_s = schemas[out_schema_idx][0]
                a = Agent(
                    organisation_id=org.id, name=name, description=desc,
                    input_schema_id=in_s.id, input_schema_version="1.0",
                    output_schema_id=out_s.id, output_schema_version="1.0",
                    prompt_template=prompt_template, model_backend_id=mb_id,
                    connector_type_refs=connector_refs,
                    required_environment_capabilities=[],
                    retry_policy={},
                    created_by=admin.id,
                )
                session.add(a)
                await session.flush()
                agents_list.append(a)
            print(f"[seed] 8 agents")

            # ── Pipelines (5) + edges + snapshots ─────────────────────────
            pipeline_defs = [
                {
                    "name": "PR Review Pipeline",
                    "description": "Automated PR review: reads GitHub issues, analyses code diffs, generates comments, human approval gate, posts back to PR",
                    "stages": ["Issue Reader", "Code Analyzer", "Comment Generator", "Approval Gate", "PR Poster"],
                    "agent_refs": [agents_list[0], agents_list[6], agents_list[0], None, agents_list[0]],
                },
                {
                    "name": "Release Pipeline",
                    "description": "Semi-automated release: version bump, changelog, release notes, HITL gate, tag creation",
                    "stages": ["Version Bumper", "Changelog Writer", "Release Notes Writer", "Release Gate", "Tag Creator"],
                    "agent_refs": [agents_list[1], agents_list[5], agents_list[1], None, agents_list[1]],
                },
                {
                    "name": "Incident Response Pipeline",
                    "description": "Automated incident handling: alert ingestion, severity classification, runbook matching, remediation, verification gate, postmortem",
                    "stages": ["Alert Ingestor", "Severity Classifier", "Runbook Matcher", "Remediation", "Verification Gate", "Postmortem"],
                    "agent_refs": [agents_list[2], agents_list[2], agents_list[2], agents_list[2], None, agents_list[2]],
                },
                {
                    "name": "Customer Feedback Pipeline",
                    "description": "Process customer feedback: ingestion, sentiment analysis, trend tracking, escalation routing",
                    "stages": ["Feedback Ingestor", "Sentiment Analyser", "Trend Tracker", "Escalation Router"],
                    "agent_refs": [agents_list[3], agents_list[3], agents_list[3], agents_list[3]],
                },
                {
                    "name": "Deployment Automation",
                    "description": "Automated deployment pipeline: plan rollout, run canary, health checks, full rollout, post-deploy validation",
                    "stages": ["Deployment Planner", "Canary Deploy", "Health Check", "Full Rollout", "Post-Deploy Validation"],
                    "agent_refs": [agents_list[4], agents_list[4], agents_list[4], agents_list[4], agents_list[4]],
                },
            ]

            pipelines_list = []
            snapshots_list = []
            for pd in pipeline_defs:
                node_ids = [str(uuid.uuid4()) for _ in pd["stages"]]
                nodes = []
                for i, label in enumerate(pd["stages"]):
                    node = {"id": node_ids[i], "type": "agent" if "Gate" not in label else "manual", "label": label, "position": {"x": 50 + i * 280, "y": 100}}
                    if node["type"] == "agent" and pd["agent_refs"][i] is not None:
                        node["agent_ref_id"] = str(pd["agent_refs"][i].id)
                    nodes.append(node)

                edges_obj = []
                for i in range(len(node_ids) - 1):
                    is_hitl = "Gate" in pd["stages"][i + 1]
                    edge = PipelineEdge(
                        organisation_id=org.id, pipeline_id=uuid.uuid4(),
                        source_node_id=uuid.UUID(node_ids[i]), target_node_id=uuid.UUID(node_ids[i + 1]),
                        edge_type="conditional" if is_hitl else "normal",
                        hitl_gate_config={"timeout_seconds": 900} if is_hitl else None,
                    )
                    edges_obj.append(edge)

                pipeline = Pipeline(
                    organisation_id=org.id, name=pd["name"], description=pd["description"],
                    created_by=admin.id, graph_nodes_json=nodes, visibility="org",
                )
                session.add(pipeline)
                await session.flush()

                for edge in edges_obj:
                    edge.pipeline_id = pipeline.id
                    edge.id = uuid.uuid4()
                    session.add(edge)
                await session.flush()

                graph_edges = [{"source_node_id": str(e.source_node_id), "target_node_id": str(e.target_node_id), "edge_type": e.edge_type, "hitl_gate_config": e.hitl_gate_config} for e in edges_obj]
                snapshot = PipelineSnapshot(
                    organisation_id=org.id, pipeline_id=pipeline.id, snapshot_version=1,
                    created_by=admin.id, graph_json={"nodes": nodes, "edges": graph_edges},
                    connector_bindings_json=[], schema_pins_json=[], prompt_pins_json=[], model_backend_pins_json=[],
                )
                session.add(snapshot)
                await session.flush()

                pipelines_list.append(pipeline)
                snapshots_list.append(snapshot)
                print(f"[seed] Pipeline '{pipeline.name}'")
            print(f"[seed] 5 pipelines with edges and snapshots")

            # ── Library Primitives (12) ───────────────────────────────────
            lib_primitives = [
                LibraryPrimitive(organisation_id=org.id, source="local", primitive_type="agent", name="Code Review Agent", slug="code-review-agent",
                    description="Analyses pull requests for code quality, security, and style", author=str(admin.id), version="1.0.0", tags=["code-review", "pr", "github"],
                    content_json={"prompt_template": "Review the following code changes…", "model": "claude-3-5-sonnet"},
                    visibility="org", created_by=admin.id),
                LibraryPrimitive(organisation_id=org.id, source="local", primitive_type="agent", name="Changelog Writer", slug="changelog-writer",
                    description="Generates release changelogs from commit history", author=str(admin.id), version="1.0.0", tags=["release", "changelog"],
                    content_json={"prompt_template": "Generate a changelog from these commits…", "model": "claude-3-haiku"},
                    visibility="org", created_by=admin.id, owner_team_id=teams[0].id),
                LibraryPrimitive(organisation_id=org.id, source="local", primitive_type="schema", name="PR Schema", slug="pr-schema",
                    description="JSON Schema for pull request data", author=str(admin.id), version="1.0.0", tags=["pr", "github", "schema"],
                    content_json={"type": "object", "properties": {"title": {"type": "string"}, "description": {"type": "string"}, "files": {"type": "array"}}},
                    visibility="org", created_by=admin.id),
                LibraryPrimitive(organisation_id=org.id, source="local", primitive_type="schema", name="Release Schema", slug="release-schema",
                    description="JSON Schema for release data", author=str(admin.id), version="1.0.0", tags=["release", "schema"],
                    content_json={"type": "object", "properties": {"version": {"type": "string"}, "changes": {"type": "array"}, "date": {"type": "string"}}},
                    visibility="org", created_by=admin.id),
                LibraryPrimitive(organisation_id=org.id, source="local", primitive_type="workflow", name="PR Review Workflow", slug="pr-review-workflow",
                    description="End-to-end PR review with code analysis and HITL", author=str(admin.id), version="1.0.0", tags=["workflow", "pr", "github"],
                    content_json={"steps": ["review", "analyze", "approve"], "timeout": 600}, visibility="org", created_by=admin.id),
                LibraryPrimitive(organisation_id=org.id, source="local", primitive_type="workflow", name="Incident Response Workflow", slug="incident-response-workflow",
                    description="Complete incident response from alert to postmortem", author=str(admin.id), version="1.0.0", tags=["workflow", "incident", "ops"],
                    content_json={"steps": ["triage", "respond", "resolve", "postmortem"], "timeout": 3600}, visibility="org", created_by=admin.id),
                LibraryPrimitive(organisation_id=org.id, source="local", primitive_type="integration", name="GitHub Connector", slug="github-connector",
                    description="Connects to GitHub for issue reading and PR creation", author=str(admin.id), version="1.0.0", tags=["integration", "github", "git"],
                    content_json={"type": "github", "capabilities": ["issue_read", "create_pr"], "auth_type": "oauth"}, visibility="org", created_by=admin.id),
                LibraryPrimitive(organisation_id=org.id, source="local", primitive_type="integration", name="Slack Notifier", slug="slack-notifier",
                    description="Sends notifications to Slack channels", author=str(admin.id), version="1.0.0", tags=["integration", "slack", "notification"],
                    content_json={"type": "slack", "capabilities": ["send_message"], "webhook_required": True}, visibility="org", created_by=admin.id),
                LibraryPrimitive(organisation_id=org.id, source="local", primitive_type="pipeline_template", name="PR Review Pipeline Template", slug="pr-review-template",
                    description="Template for creating a PR review pipeline", author=str(admin.id), version="1.0.0", tags=["template", "pr-review"],
                    content_json={"agents": [], "graph_nodes": [], "edges": [], "category": "code-review"}, visibility="org", created_by=admin.id),
                LibraryPrimitive(organisation_id=org.id, source="local", primitive_type="pipeline_template", name="Release Pipeline Template", slug="release-template",
                    description="Template for creating a release pipeline", author=str(admin.id), version="1.0.0", tags=["template", "release"],
                    content_json={"agents": [], "graph_nodes": [], "edges": [], "category": "release"}, visibility="org", created_by=admin.id),
                LibraryPrimitive(organisation_id=org.id, source="local", primitive_type="test_fixture", name="PR Review Test Fixture", slug="pr-review-fixture",
                    description="Test fixture for PR review pipeline", author=str(admin.id), version="1.0.0", tags=["test", "fixture", "pr-review"],
                    content_json={"fixture_map": {"Review: Add login endpoint": "Approved with minor comments"}}, visibility="org", created_by=admin.id),
                LibraryPrimitive(organisation_id=org.id, source="local", primitive_type="test_fixture", name="Incident Test Fixture", slug="incident-fixture",
                    description="Test fixture for incident response pipeline", author=str(admin.id), version="1.0.0", tags=["test", "fixture", "incident"],
                    content_json={"fixture_map": {"Alert: P99 latency spike": "CRITICAL — investigate database query performance"}}, visibility="org", created_by=admin.id),
            ]
            session.add_all(lib_primitives)
            await session.flush()
            print(f"[seed] 12 library primitives")

            # ── Eval Definitions (4) ──────────────────────────────────────
            eval_defs_list = []
            for ed_name, ed_type, ed_desc in [
                ("PR Review Quality", "llm_judge", "Evaluates PR review quality using LLM-as-judge"),
                ("Release Correctness", "json_schema", "Asserts release output matches expected format"),
                ("Sentiment Accuracy", "llm_judge", "Validates sentiment classification accuracy"),
                ("Response Time SLA", "custom_function", "Ensures pipeline completes within SLA bounds"),
            ]:
                ed = EvalDefinition(
                    organisation_id=org.id, name=ed_name, eval_type=ed_type,
                    config_json={}, created_by=admin.id,
                    pipeline_id=pipelines_list[0].id,
                )
                session.add(ed)
                await session.flush()
                eval_defs_list.append(ed)

            print(f"[seed] 4 eval definitions")

            # ── Connectors (3) ───────────────────────────────────────────
            for cname, ctype, cfg in [
                ("Demo GitHub", "github", {"repo": "farnalabs/modulo", "base_url": "https://api.github.com"}),
                ("Demo Slack", "slack", {"workspace": "farnalabs", "default_channel": "#deployments"}),
                ("Demo Filesystem", "filesystem", {"base_path": os.path.join(tempfile.gettempdir(), "modulo-demo")}),
            ]:
                session.add(ConnectorInstance(
                    organisation_id=org.id, name=cname, connector_type_id=ctype,
                    owner_id=admin.id, credentials_ciphertext=b"", config_json=cfg,
                    allowed_operations=["read", "write"], visibility="org",
                ))
            await session.flush()
            print(f"[seed] 3 connectors")

            # ── Runs (15) with stages ─────────────────────────────────────
            runs_list = []
            now = datetime.now(UTC)
            statuses = ["complete", "complete", "complete", "complete", "complete", "complete", "failed", "failed", "awaiting_human", "awaiting_human", "running", "cancelled", "complete", "complete", "complete"]
            trigger_types = ["manual", "manual", "webhook", "manual", "webhook", "cron", "manual", "manual", "webhook", "manual", "cron", "manual", "manual", "webhook", "manual"]
            run_inputs = [
                {"action": "review", "pr_number": 42, "repo": "farnalabs/modulo"},
                {"action": "review", "pr_number": 43, "repo": "farnalabs/modulo"},
                {"action": "review", "pr_number": 44, "repo": "farnalabs/modulo", "event": "pull_request"},
                {"action": "release", "version": "2.1.0", "channel": "stable"},
                {"action": "release", "version": "2.2.0-beta", "channel": "beta", "event": "push"},
                {"action": "deploy", "env": "staging", "region": "lhr", "cron": "daily"},
                {"action": "review", "pr_number": 99, "repo": "farnalabs/modulo"},
                {"action": "incident", "severity": "critical", "service": "api-gateway"},
                {"action": "release", "version": "2.0.0", "channel": "stable", "needs_approval": True},
                {"action": "deploy", "env": "production", "region": "lhr", "governance_check": "pending"},
                {"action": "review", "pr_number": 100, "repo": "farnalabs/modulo", "schedule": "nightly"},
                {"action": "review", "pr_number": 101, "repo": "farnalabs/modulo", "reason": "user_cancelled"},
                {"action": "review", "pr_number": 102, "repo": "farnalabs/modulo"},
                {"action": "release", "version": "2.3.0", "channel": "stable", "event": "release_published"},
                {"action": "review", "pr_number": 103, "repo": "farnalabs/modulo", "automated": True},
            ]
            for i in range(15):
                pi = i % len(pipelines_list)
                pipeline = pipelines_list[pi]
                snapshot = snapshots_list[pi]
                run_id = uuid.uuid4()
                run = Run(
                    id=run_id, organisation_id=org.id, pipeline_id=pipeline.id,
                    snapshot_id=snapshot.id, trigger_type=trigger_types[i],
                    status=statuses[i], created_by=users[i % len(users)].id,
                    input_hash=_input_hash(run_inputs[i]),
                    input_payload=run_inputs[i],
                    started_at=now - timedelta(hours=i * 3),
                    completed_at=now - timedelta(hours=i * 3 - 0.5) if statuses[i] in ("complete", "failed", "cancelled") else None,
                    langgraph_thread_id=f"{org.id}:{run_id}",
                    total_tokens=800 + i * 200,
                    total_cost_usd=str(0.01 + i * 0.005),
                    error_code="AGENT_TIMEOUT" if statuses[i] == "failed" and i == 6 else None,
                    error_detail="Agent exceeded maximum execution time of 300s" if statuses[i] == "failed" and i == 6 else None,
                    outputs_json={"summary": f"Run {i} completed successfully", "findings": []} if statuses[i] == "complete" else None,
                    owner_team_id=teams[i % 3].id if teams else None,
                )
                session.add(run)
                runs_list.append(run)
                await session.flush()

                # Stages for each run
                stage_labels = pipeline_defs[pi]["stages"]
                for si, sl in enumerate(stage_labels):
                    session.add(Stage(
                        organisation_id=org.id, name=sl,
                        position=si, created_by=admin.id,
                    ))
                await session.flush()
            print(f"[seed] 15 runs + stage records")

            # ── Primitive Ratings (5) ─────────────────────────────────────
            for ri in range(5):
                session.add(PrimitiveRating(
                    organisation_id=org.id, primitive_id=lib_primitives[ri].id,
                    user_id=users[ri + 1].id, thumbs_up=ri % 2 == 0,
                    comment=["Great for PR reviews!", "Works well for changelogs", "Good schema for PR data", "Release schema is solid", "Nice integration"][ri],
                ))
            await session.flush()
            print(f"[seed] 5 primitive ratings")

            # ── Feedback Records (5) ──────────────────────────────────────
            # Note: FeedbackRecord model is now for HITL gate rejection tracking,
            # not generic user feedback. Skipping for now.
            print(f"[seed] 0 feedback records")

            # ── API Keys (2) ──────────────────────────────────────────────
            session.add(OrgApiKey(
                organisation_id=org.id, name="CI/CD Deployment Key",
                lookup_prefix="mod_cd_", hashed_secret="hash_placeholder_1",
                role="operator", created_by=admin.id,
            ))
            session.add(OrgApiKey(
                organisation_id=org.id, name="Dev Testing Key",
                lookup_prefix="mod_dev_", hashed_secret="hash_placeholder_2",
                role="runner", created_by=admin.id,
            ))
            await session.flush()
            print(f"[seed] 2 API keys")

            # ── Eval Results (12) ─────────────────────────────────────────
            for i in range(12):
                er = EvalResult(
                    organisation_id=org.id, eval_id=eval_defs_list[i % 4].id,
                    run_id=runs_list[i % len(runs_list)].id if runs_list else None, passed=i % 3 != 0,
                    score=0.75 + (i % 3) * 0.1, detail=f"eval-result-{i}: {'pass' if i % 3 != 0 else 'fail'}",
                    evaluated_at=now - timedelta(hours=i * 6),
                )
                session.add(er)
            await session.flush()
            print(f"[seed] 12 eval results")

            # ── Audit Events (25) ─────────────────────────────────────────
            audit_defs = [
                ("organisation.created", None, "organisation", org.id, {"name": SEED_ORG_NAME}),
                ("user.created", admin.id, "user", admin.id, {"email": admin.email}),
                ("pipeline.created", admin.id, "pipeline", pipelines_list[0].id, {"name": pipelines_list[0].name}),
                ("pipeline.created", admin.id, "pipeline", pipelines_list[1].id, {"name": pipelines_list[1].name}),
                ("pipeline.created", admin.id, "pipeline", pipelines_list[2].id, {"name": pipelines_list[2].name}),
                ("run.started", users[2].id, "run", None, {"pipeline": pipelines_list[0].name, "status": "running"}),
                ("run.completed", users[2].id, "run", None, {"pipeline": pipelines_list[0].name, "status": "complete"}),
                ("run.failed", users[4].id, "run", None, {"pipeline": pipelines_list[1].name, "error": "AGENT_TIMEOUT"}),
                ("run.awaiting_human", users[5].id, "run", None, {"pipeline": pipelines_list[2].name, "gate": "Verification Gate"}),
                ("team.created", admin.id, "team", teams[0].id, {"name": teams[0].name}),
                ("team.created", admin.id, "team", teams[1].id, {"name": teams[1].name}),
                ("team.created", admin.id, "team", teams[2].id, {"name": teams[2].name}),
                ("user.added_to_team", admin.id, "team_membership", teams[0].id, {"user": users[2].email, "team": teams[0].name}),
                ("schema.created", admin.id, "schema", schemas[0][0].id, {"name": schemas[0][0].name}),
                ("schema.created", admin.id, "schema", schemas[1][0].id, {"name": schemas[1][0].name}),
                ("library.primitive_created", admin.id, "library_primitive", lib_primitives[0].id, {"name": lib_primitives[0].name}),
                ("library.primitive_created", admin.id, "library_primitive", lib_primitives[4].id, {"name": lib_primitives[4].name}),
                ("connector.created", admin.id, "connector", None, {"connector_type": "github"}),
                ("api_key.created", admin.id, "api_key", None, {"name": "CI/CD Deployment Key"}),
                ("eval.definition_created", admin.id, "eval_definition", eval_defs_list[0].id, {"name": eval_defs_list[0].name}),
                ("eval.result_recorded", admin.id, "eval_result", None, {"eval": eval_defs_list[0].name, "passed": True}),
                ("feedback.submitted", users[5].id, "feedback", None, {"category": "praise"}),
                ("settings.observability_updated", admin.id, "settings", None, {"otel_enabled": True}),
                ("user.login", users[6].id, "session", None, {"method": "password"}),
                ("user.role_changed", admin.id, "user", users[8].id, {"old_role": "runner", "new_role": "viewer"}),
            ]
            for event_type, actor, resource_type, resource_id, payload in audit_defs:
                await append_audit_event(
                    session, org_id=org.id, event_type=event_type,
                    actor_user_id=actor, resource_type=resource_type,
                    resource_id=resource_id, payload_json=payload,
                )
            print(f"[seed] 25 audit events")

            await session.commit()
            print(f"[seed] Done — {SEED_ORG_NAME} seeded with rich demo data")

        except Exception:
            await session.rollback()
            print("[seed] Fatal error — rolling back", file=sys.stderr)
            import traceback
            traceback.print_exc()
            sys.exit(1)


def main() -> None:
    asyncio.run(seed())


if __name__ == "__main__":
    main()
