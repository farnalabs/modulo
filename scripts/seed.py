#!/usr/bin/env -S uv run
"""Modulo Dev Seed Data Script

Seeds a development database with demo data for local development:
  - Demo Organisation + admin user
  - 3 sample pipelines with graph_nodes and edges
  - 6 library primitives (2 agents, 2 schemas, 2 workflows)
  - 1 FilesystemConnector instance
  - 3 sample runs (complete / failed / awaiting_human)
  - 4 audit events

Idempotent: if the target organisation already exists the script skips
all seeding and exits cleanly.  Re-run after a DB reset to re-seed.

Usage:
    cd codebase/backend
    uv run ../scripts/seed.py

Environment variables:
    MODULO_SEED_PASSWORD   bcrypt-hashed password for admin@demo.modulo
                           (default: admin123)
    MODULO_SEED_ORG_NAME   Organisation name to create (default: Demo Corp)
"""

import asyncio
import hashlib
import json
import os
import sys
import uuid
from datetime import UTC, datetime

import bcrypt
from sqlalchemy import select

# ---------------------------------------------------------------------------
# Ensure the modulo package is importable when run from codebase/scripts/
# ---------------------------------------------------------------------------
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.abspath(os.path.join(_SCRIPT_DIR, ".."))
_SRC = os.path.join(_PROJECT_ROOT, "backend", "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from modulo.db.session import AsyncSessionLocal
from modulo.db.models import (
    ConnectorInstance,
    LibraryPrimitive,
    Organisation,
    Pipeline,
    PipelineEdge,
    PipelineSnapshot,
    Run,
    User,
)
from modulo.core.audit_logger import append_audit_event

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
SEED_PASSWORD = os.environ.get("MODULO_SEED_PASSWORD", "admin123")
SEED_ORG_NAME = os.environ.get("MODULO_SEED_ORG_NAME", "Demo Corp")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _input_hash(payload: dict) -> str:
    serialised = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialised.encode()).hexdigest()


def _build_pipeline_graph(labels: list[str]) -> tuple[list[dict], list[dict]]:
    """Return (nodes, edges) for a linear pipeline stage graph."""
    ids = [uuid.uuid4() for _ in labels]
    nodes = [
        {"id": str(nid), "type": "agent", "label": label}
        for nid, label in zip(ids, labels)
    ]
    # Last edge is a HITL gate if there is one
    edges = []
    for i in range(len(ids) - 1):
        is_hitl = i == len(ids) - 2 and len(ids) > 2
        edges.append(
            {
                "source_node_id": ids[i],
                "target_node_id": ids[i + 1],
                "edge_type": "hitl" if is_hitl else "normal",
                "hitl_gate_config": {"timeout_seconds": 900} if is_hitl else None,
            }
        )
    return nodes, edges


# ---------------------------------------------------------------------------
# Seed
# ---------------------------------------------------------------------------
async def seed() -> None:
    print(f"[seed] Starting — org='{SEED_ORG_NAME}'")

    async with AsyncSessionLocal() as session:
        try:
            # ---- 1. Idempotency check ----------------------------------------
            slug = SEED_ORG_NAME.lower().replace(" ", "-")
            existing = (
                await session.execute(
                    select(Organisation).where(Organisation.slug == slug)
                )
            ).scalar_one_or_none()
            if existing is not None:
                print(f"[seed] Organisation '{SEED_ORG_NAME}' already exists — skipping")
                await session.commit()
                return

            # ---- 2. Organisation ---------------------------------------------
            org = Organisation(
                name=SEED_ORG_NAME,
                slug=slug,
                status="active",
                settings_json={},
                otel_config_json={},
            )
            session.add(org)
            await session.flush()
            print(f"[seed] ✓ Organisation '{org.name}' ({org.id})")

            # ---- 3. Admin user -----------------------------------------------
            pw_hash = bcrypt.hashpw(SEED_PASSWORD.encode(), bcrypt.gensalt()).decode()
            admin = User(
                organisation_id=org.id,
                email="admin@demo.modulo",
                display_name="Admin User",
                password_hash=pw_hash,
                org_role="admin",
                auth_provider="local",
            )
            session.add(admin)
            await session.flush()
            print(f"[seed] ✓ Admin user '{admin.email}'")

            # ---- 4. Pipelines + snapshots ------------------------------------
            pipeline_defs = [
                {
                    "name": "PR Review Pipeline",
                    "description": "Automated PR review with HITL approval gate",
                    "stages": [
                        "Issue Reader",
                        "Code Diff Analyzer",
                        "Comment Generator",
                        "HITL Gate",
                        "PR Poster",
                    ],
                },
                {
                    "name": "Release Checklist",
                    "description": "Automated release checklist with HITL sign-off",
                    "stages": [
                        "Version Bumper",
                        "Changelog Generator",
                        "Release Notes Writer",
                        "HITL Gate",
                        "Tag Creator",
                    ],
                },
                {
                    "name": "Incident Response",
                    "description": "Automated incident response pipeline",
                    "stages": [
                        "Alert Ingestor",
                        "Severity Classifier",
                        "Runbook Matcher",
                        "Remediation",
                        "HITL Review",
                        "Postmortem",
                    ],
                },
            ]

            pipelines: list[Pipeline] = []
            snapshots: list[PipelineSnapshot] = []

            for pd in pipeline_defs:
                nodes, edges = _build_pipeline_graph(pd["stages"])
                pipeline = Pipeline(
                    organisation_id=org.id,
                    name=pd["name"],
                    description=pd["description"],
                    created_by=admin.id,
                    graph_nodes_json=nodes,
                    visibility="org",
                )
                session.add(pipeline)
                await session.flush()

                for ed in edges:
                    edge = PipelineEdge(
                        organisation_id=org.id,
                        pipeline_id=pipeline.id,
                        source_node_id=ed["source_node_id"],
                        target_node_id=ed["target_node_id"],
                        edge_type=ed["edge_type"],
                        hitl_gate_config=ed["hitl_gate_config"],
                    )
                    session.add(edge)
                await session.flush()

                graph_edges = [
                    {
                        "source": str(e["source_node_id"]),
                        "target": str(e["target_node_id"]),
                        "type": e["edge_type"],
                        "hitl_gate_config": e["hitl_gate_config"],
                    }
                    for e in edges
                ]
                snapshot = PipelineSnapshot(
                    organisation_id=org.id,
                    pipeline_id=pipeline.id,
                    snapshot_version=1,
                    created_by=admin.id,
                    graph_json={"nodes": nodes, "edges": graph_edges},
                    connector_bindings_json=[],
                    schema_pins_json=[],
                    prompt_pins_json=[],
                    model_backend_pins_json=[],
                )
                session.add(snapshot)
                await session.flush()

                pipelines.append(pipeline)
                snapshots.append(snapshot)
                print(f"[seed] ✓ Pipeline '{pipeline.name}'")

            # ---- 5. Library primitives ---------------------------------------
            primitives = [
                LibraryPrimitive(
                    organisation_id=org.id,
                    source="local",
                    primitive_type="agent",
                    name="Code Review Agent",
                    slug="code-review-agent",
                    description="Analyses pull requests for code quality and security",
                    author="Modulo",
                    version="1.0.0",
                    tags=[],
                    content_json={
                        "prompt_template": "Review the following code changes…",
                        "model": "claude-3-5-sonnet",
                    },
                    source_url=None,
                    forked_from=None,
                    checksum=None,
                    ed25519_signature=None,
                    verified=None,
                    download_count=None,
                    average_rating=None,
                    review_count=None,
                    visibility="org",
                    owner_team_id=None,
                    created_by=admin.id,
                ),
                LibraryPrimitive(
                    organisation_id=org.id,
                    source="local",
                    primitive_type="agent",
                    name="Changelog Writer",
                    slug="changelog-writer",
                    description="Generates release changelogs from commit history",
                    author="Modulo",
                    version="1.0.0",
                    tags=[],
                    content_json={
                        "prompt_template": "Generate a changelog from these commits…",
                        "model": "claude-3-haiku",
                    },
                    source_url=None,
                    forked_from=None,
                    checksum=None,
                    ed25519_signature=None,
                    verified=None,
                    download_count=None,
                    average_rating=None,
                    review_count=None,
                    visibility="org",
                    owner_team_id=None,
                    created_by=admin.id,
                ),
                LibraryPrimitive(
                    organisation_id=org.id,
                    source="local",
                    primitive_type="schema",
                    name="PR Schema",
                    slug="pr-schema",
                    description="JSON Schema for pull request data",
                    author="Modulo",
                    version="1.0.0",
                    tags=[],
                    content_json={
                        "type": "object",
                        "properties": {
                            "title": {"type": "string"},
                            "description": {"type": "string"},
                            "files": {"type": "array"},
                        },
                    },
                    source_url=None,
                    forked_from=None,
                    checksum=None,
                    ed25519_signature=None,
                    verified=None,
                    download_count=None,
                    average_rating=None,
                    review_count=None,
                    visibility="org",
                    owner_team_id=None,
                    created_by=admin.id,
                ),
                LibraryPrimitive(
                    organisation_id=org.id,
                    source="local",
                    primitive_type="schema",
                    name="Release Schema",
                    slug="release-schema",
                    description="JSON Schema for release data",
                    author="Modulo",
                    version="1.0.0",
                    tags=[],
                    content_json={
                        "type": "object",
                        "properties": {
                            "version": {"type": "string"},
                            "changes": {"type": "array"},
                        },
                    },
                    source_url=None,
                    forked_from=None,
                    checksum=None,
                    ed25519_signature=None,
                    verified=None,
                    download_count=None,
                    average_rating=None,
                    review_count=None,
                    visibility="org",
                    owner_team_id=None,
                    created_by=admin.id,
                ),
                LibraryPrimitive(
                    organisation_id=org.id,
                    source="local",
                    primitive_type="workflow",
                    name="PR Review Workflow",
                    slug="pr-review-workflow",
                    description="End-to-end PR review with code analysis and HITL",
                    author="Modulo",
                    version="1.0.0",
                    tags=[],
                    content_json={
                        "steps": ["review", "analyze", "approve"],
                        "timeout": 600,
                    },
                    source_url=None,
                    forked_from=None,
                    checksum=None,
                    ed25519_signature=None,
                    verified=None,
                    download_count=None,
                    average_rating=None,
                    review_count=None,
                    visibility="org",
                    owner_team_id=None,
                    created_by=admin.id,
                ),
                LibraryPrimitive(
                    organisation_id=org.id,
                    source="local",
                    primitive_type="workflow",
                    name="Incident Response Workflow",
                    slug="incident-response-workflow",
                    description="Complete incident response from alert to postmortem",
                    author="Modulo",
                    version="1.0.0",
                    tags=[],
                    content_json={
                        "steps": ["triage", "respond", "resolve", "postmortem"],
                        "timeout": 3600,
                    },
                    source_url=None,
                    forked_from=None,
                    checksum=None,
                    ed25519_signature=None,
                    verified=None,
                    download_count=None,
                    average_rating=None,
                    review_count=None,
                    visibility="org",
                    owner_team_id=None,
                    created_by=admin.id,
                ),
            ]
            session.add_all(primitives)
            await session.flush()
            print(f"[seed] ✓ {len(primitives)} library primitives")

            # ---- 6. Connector instance ---------------------------------------
            connector = ConnectorInstance(
                organisation_id=org.id,
                name="Demo Filesystem",
                connector_type_id="filesystem",
                owner_id=admin.id,
                credentials_ciphertext=b"",
                config_json={"base_path": "demo/"},
                allowed_operations=["read", "write"],
                visibility="org",
            )
            session.add(connector)
            await session.flush()
            print(f"[seed] ✓ Connector '{connector.name}'")

            # ---- 7. Sample runs ----------------------------------------------
            now = datetime.now(UTC)
            run_defs = [
                {
                    "pipeline_idx": 0,
                    "status": "complete",
                    "input_payload": {"action": "review", "pr_number": 42},
                    "started_at": now,
                    "completed_at": now,
                    "total_tokens": 1500,
                    "total_cost_usd": "0.03",
                    "outputs_json": {"summary": "LGTM with minor nits"},
                },
                {
                    "pipeline_idx": 0,
                    "status": "failed",
                    "input_payload": {"action": "review", "pr_number": 99},
                    "started_at": now,
                    "completed_at": now,
                    "error_code": "AGENT_TIMEOUT",
                    "total_tokens": 3200,
                    "total_cost_usd": "0.08",
                    "outputs_json": None,
                },
                {
                    "pipeline_idx": 1,
                    "status": "awaiting_human",
                    "input_payload": {"action": "release", "version": "2.1.0"},
                    "started_at": now,
                    "completed_at": None,
                    "total_tokens": 800,
                    "total_cost_usd": "0.015",
                    "outputs_json": None,
                },
            ]

            for rd in run_defs:
                pipeline = pipelines[rd["pipeline_idx"]]
                snapshot = snapshots[rd["pipeline_idx"]]
                run_id = uuid.uuid4()
                run = Run(
                    id=run_id,
                    organisation_id=org.id,
                    pipeline_id=pipeline.id,
                    snapshot_id=snapshot.id,
                    trigger_type="manual",
                    input_hash=_input_hash(rd["input_payload"]),
                    input_payload=rd["input_payload"],
                    status=rd["status"],
                    started_at=rd.get("started_at"),
                    completed_at=rd.get("completed_at"),
                    created_by=admin.id,
                    langgraph_thread_id=f"{org.id}:{run_id}",
                    total_tokens=rd.get("total_tokens"),
                    total_cost_usd=rd.get("total_cost_usd"),
                    error_code=rd.get("error_code"),
                )
                session.add(run)
            await session.flush()
            print(f"[seed] ✓ {len(run_defs)} sample runs")

            # ---- 8. Audit events ---------------------------------------------
            audit_events = [
                {
                    "event_type": "organisation.created",
                    "actor_user_id": None,
                    "resource_type": "organisation",
                    "resource_id": org.id,
                    "payload_json": {"name": SEED_ORG_NAME},
                },
                {
                    "event_type": "user.created",
                    "actor_user_id": admin.id,
                    "resource_type": "user",
                    "resource_id": admin.id,
                    "payload_json": {"email": admin.email},
                },
                {
                    "event_type": "pipeline.created",
                    "actor_user_id": admin.id,
                    "resource_type": "pipeline",
                    "resource_id": pipelines[0].id,
                    "payload_json": {"name": pipelines[0].name},
                },
                {
                    "event_type": "run.completed",
                    "actor_user_id": admin.id,
                    "resource_type": "run",
                    "resource_id": pipelines[0].id,
                    "payload_json": {"status": "complete"},
                },
            ]
            for ae in audit_events:
                await append_audit_event(
                    session,
                    org_id=org.id,
                    event_type=ae["event_type"],
                    actor_user_id=ae["actor_user_id"],
                    resource_type=ae["resource_type"],
                    resource_id=ae["resource_id"],
                    payload_json=ae["payload_json"],
                )
            print(f"[seed] ✓ {len(audit_events)} audit events")

            await session.commit()
            print("[seed] ✅ Done")

        except Exception:
            await session.rollback()
            print("[seed] ❌ Fatal error — rolling back", file=sys.stderr)
            import traceback

            traceback.print_exc()
            sys.exit(1)


def main() -> None:
    asyncio.run(seed())


if __name__ == "__main__":
    main()
