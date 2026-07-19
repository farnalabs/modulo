"""Housekeeping service — scans for cleanup candidates within an org scope."""

import contextlib
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.models.agent import Agent
from modulo.db.models.connector_instance import ConnectorInstance
from modulo.db.models.model_backend import ModelBackend
from modulo.db.models.pipeline import Pipeline
from modulo.db.models.pipeline_snapshot import PipelineSnapshot
from modulo.db.models.run import Run
from modulo.db.models.secret import Secret
from modulo.db.models.trigger import Trigger
from modulo.db.models.webhook import WebhookDedupHash

_log = logging.getLogger(__name__)

ENTITY_MODEL_MAP: dict[str, type] = {
    "secret": Secret,
    "connector": ConnectorInstance,
    "model_backend": ModelBackend,
    "pipeline": Pipeline,
    "pipeline_snapshot": PipelineSnapshot,
    "trigger": Trigger,
    "webhook_dedup": WebhookDedupHash,
}

_CATEGORY_LABELS: dict[str, str] = {
    "orphan_secrets": "Orphan Secrets",
    "unbound_connectors": "Unbound Connectors",
    "untriggered_pipelines": "Untriggered Pipelines",
    "stale_pipelines": "Stale Pipelines",
    "unused_model_backends": "Unused Model Backends",
    "inactive_triggers": "Inactive Triggers",
    "orphan_snapshots": "Orphan Snapshots",
    "expired_webhook_dedups": "Expired Webhook Dedups",
}

_CATEGORY_DESCRIPTIONS: dict[str, str] = {
    "orphan_secrets": "Secrets whose key is not referenced by any connector config or agent connector_type_refs",
    "unbound_connectors": "Connector instances not bound to any pipeline snapshot",
    "untriggered_pipelines": "Pipelines with no trigger and no runs",
    "stale_pipelines": "Pipelines with no runs in the last 90 days",
    "unused_model_backends": "Model backends not assigned to any agent",
    "inactive_triggers": "Triggers that are inactive and have never fired",
    "orphan_snapshots": "Snapshots whose pipeline no longer exists",
    "expired_webhook_dedups": "Expired webhook deduplication hash entries",
}


class Candidate:
    def __init__(self, id: str, name: str, detail: str, created_at: str | None = None) -> None:
        self.id = id
        self.name = name
        self.detail = detail
        self.created_at = created_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "detail": self.detail,
            "created_at": self.created_at,
        }


class CategoryResult:
    def __init__(self, category: str, candidates: list[Candidate]) -> None:
        self.category = category
        self.label = _CATEGORY_LABELS.get(category, category)
        self.description = _CATEGORY_DESCRIPTIONS.get(category, "")
        self.candidates = candidates

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "label": self.label,
            "description": self.description,
            "candidates": [c.to_dict() for c in self.candidates],
            "count": len(self.candidates),
        }


async def _scan_orphan_secrets(session: AsyncSession, org_id: uuid.UUID) -> list[Candidate]:
    secrets = (await session.execute(select(Secret).where(Secret.organisation_id == org_id))).scalars().all()
    if not secrets:
        return []

    connectors = (
        (await session.execute(select(ConnectorInstance).where(ConnectorInstance.organisation_id == org_id)))
        .scalars()
        .all()
    )
    agents = (await session.execute(select(Agent).where(Agent.organisation_id == org_id))).scalars().all()

    referenced_keys: set[str] = set()
    for c in connectors:
        cfg = c.config_json or {}
        for v in cfg.values():
            if isinstance(v, str):
                referenced_keys.add(v)
    for a in agents:
        refs = a.connector_type_refs or []
        for ref in refs:
            if isinstance(ref, dict):
                secret_key = ref.get("secret_key") or ref.get("key")
                if secret_key:
                    referenced_keys.add(secret_key)

    candidates: list[Candidate] = []
    for s in secrets:
        if s.key not in referenced_keys:
            candidates.append(
                Candidate(
                    id=str(s.id),
                    name=s.key,
                    detail="Orphan secret — no connector or agent references this key",
                    created_at=s.created_at.isoformat() if s.created_at else None,
                )
            )
    return candidates


async def _scan_unbound_connectors(session: AsyncSession, org_id: uuid.UUID) -> list[Candidate]:
    connectors = (
        (await session.execute(select(ConnectorInstance).where(ConnectorInstance.organisation_id == org_id)))
        .scalars()
        .all()
    )
    if not connectors:
        return []

    snapshots = (
        (await session.execute(select(PipelineSnapshot).where(PipelineSnapshot.organisation_id == org_id)))
        .scalars()
        .all()
    )

    bound_ids: set[uuid.UUID] = set()
    for snap in snapshots:
        bindings = snap.connector_bindings_json or []
        for b in bindings:
            cid = b.get("connector_instance_id") or b.get("connector_id")
            if cid:
                with contextlib.suppress(ValueError, TypeError):
                    bound_ids.add(uuid.UUID(cid) if isinstance(cid, str) else cid)

    candidates: list[Candidate] = []
    for c in connectors:
        if c.id not in bound_ids:
            candidates.append(
                Candidate(
                    id=str(c.id),
                    name=c.name,
                    detail=f"Connector instance (type: {c.connector_type_id}) — not bound to any snapshot",
                    created_at=c.created_at.isoformat() if c.created_at else None,
                )
            )
    return candidates


async def _scan_untriggered_pipelines(session: AsyncSession, org_id: uuid.UUID) -> list[Candidate]:
    trigger_subq = select(Trigger.pipeline_id).where(Trigger.organisation_id == org_id).subquery()
    run_subq = select(Run.pipeline_id).where(Run.organisation_id == org_id).distinct().subquery()
    pipelines = (
        (
            await session.execute(
                select(Pipeline).where(
                    Pipeline.organisation_id == org_id,
                    Pipeline.id.notin_(select(trigger_subq.c.pipeline_id)),
                    Pipeline.id.notin_(select(run_subq.c.pipeline_id)),
                )
            )
        )
        .scalars()
        .all()
    )
    return [
        Candidate(
            id=str(p.id),
            name=p.name,
            detail="Pipeline has no triggers and no runs",
            created_at=p.created_at.isoformat() if p.created_at else None,
        )
        for p in pipelines
    ]


async def _scan_stale_pipelines(session: AsyncSession, org_id: uuid.UUID) -> list[Candidate]:
    ninety_days_ago = datetime.now(UTC) - timedelta(days=90)
    max_run = (
        select(
            Run.pipeline_id,
            func.max(Run.created_at).label("last_run_at"),
        )
        .where(Run.organisation_id == org_id)
        .group_by(Run.pipeline_id)
        .subquery()
    )
    pipelines = (
        (
            await session.execute(
                select(Pipeline)
                .join(
                    max_run,
                    Pipeline.id == max_run.c.pipeline_id,
                )
                .where(
                    Pipeline.organisation_id == org_id,
                    max_run.c.last_run_at < ninety_days_ago,
                )
            )
        )
        .scalars()
        .all()
    )
    return [
        Candidate(
            id=str(p.id),
            name=p.name,
            detail="Pipeline last run over 90 days ago",
            created_at=p.created_at.isoformat() if p.created_at else None,
        )
        for p in pipelines
    ]


async def _scan_unused_model_backends(session: AsyncSession, org_id: uuid.UUID) -> list[Candidate]:
    used_ids = (
        (await session.execute(select(Agent.model_backend_id).distinct().where(Agent.organisation_id == org_id)))
        .scalars()
        .all()
    )
    used_set = set(used_ids)
    backends = (
        (await session.execute(select(ModelBackend).where(ModelBackend.organisation_id == org_id))).scalars().all()
    )
    candidates: list[Candidate] = []
    for mb in backends:
        if mb.id not in used_set:
            candidates.append(
                Candidate(
                    id=str(mb.id),
                    name=mb.name,
                    detail=f"Model backend ({mb.provider}/{mb.model_id}) — not assigned to any agent",
                    created_at=mb.created_at.isoformat() if mb.created_at else None,
                )
            )
    return candidates


async def _scan_inactive_triggers(session: AsyncSession, org_id: uuid.UUID) -> list[Candidate]:
    triggers = (
        (
            await session.execute(
                select(Trigger).where(
                    Trigger.organisation_id == org_id,
                    Trigger.active.is_(False),
                    Trigger.last_fired_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    return [
        Candidate(
            id=str(t.id),
            name=f"Trigger {t.trigger_type} for pipeline {t.pipeline_id}",
            detail="Trigger is inactive and has never fired",
            created_at=t.created_at.isoformat() if t.created_at else None,
        )
        for t in triggers
    ]


async def _scan_orphan_snapshots(session: AsyncSession, org_id: uuid.UUID) -> list[Candidate]:
    pipeline_ids_subq = select(Pipeline.id).where(Pipeline.organisation_id == org_id).subquery()
    snapshots = (
        (
            await session.execute(
                select(PipelineSnapshot).where(
                    PipelineSnapshot.organisation_id == org_id,
                    PipelineSnapshot.pipeline_id.notin_(select(pipeline_ids_subq.c.id)),
                )
            )
        )
        .scalars()
        .all()
    )
    return [
        Candidate(
            id=str(s.id),
            name=f"Snapshot v{s.snapshot_version} for pipeline {s.pipeline_id}",
            detail="Orphan snapshot — referenced pipeline no longer exists",
            created_at=s.created_at.isoformat() if s.created_at else None,
        )
        for s in snapshots
    ]


async def _scan_expired_webhook_dedups(session: AsyncSession, org_id: uuid.UUID) -> list[Candidate]:
    now = datetime.now(UTC)
    rows = (
        (
            await session.execute(
                select(WebhookDedupHash).where(
                    WebhookDedupHash.organisation_id == org_id,
                    WebhookDedupHash.expires_at < now,
                )
            )
        )
        .scalars()
        .all()
    )
    return [
        Candidate(
            id=str(r.id),
            name=f"Webhook dedup {r.payload_hash[:16]}...",
            detail="Expired webhook deduplication hash",
            created_at=r.expires_at.isoformat() if r.expires_at else None,
        )
        for r in rows
    ]


_SCANNERS: list[tuple[str, Any]] = [
    ("orphan_secrets", _scan_orphan_secrets),
    ("unbound_connectors", _scan_unbound_connectors),
    ("untriggered_pipelines", _scan_untriggered_pipelines),
    ("stale_pipelines", _scan_stale_pipelines),
    ("unused_model_backends", _scan_unused_model_backends),
    ("inactive_triggers", _scan_inactive_triggers),
    ("orphan_snapshots", _scan_orphan_snapshots),
    ("expired_webhook_dedups", _scan_expired_webhook_dedups),
]


async def scan_all(session: AsyncSession, org_id: uuid.UUID) -> list[CategoryResult]:
    results: list[CategoryResult] = []
    for category, scanner in _SCANNERS:
        try:
            candidates = await scanner(session, org_id)
            results.append(CategoryResult(category=category, candidates=candidates))
        except Exception:
            _log.exception("Housekeeping scanner '%s' failed", category)
            results.append(CategoryResult(category=category, candidates=[]))
    return results
