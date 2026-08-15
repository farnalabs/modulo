"""Housekeeping service — scans for cleanup candidates within an org scope."""

import contextlib
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.models.account import Account
from modulo.db.models.agent import Agent
from modulo.db.models.api_key import OrgApiKey
from modulo.db.models.connector_instance import ConnectorInstance
from modulo.db.models.environment_profile import EnvironmentProfile
from modulo.db.models.lifecycle_map import LifecycleMap
from modulo.db.models.model_backend import ModelBackend
from modulo.db.models.org_membership import OrgMembership
from modulo.db.models.parameter_schema import ParameterSchema
from modulo.db.models.pipeline import Pipeline
from modulo.db.models.pipeline_snapshot import PipelineSnapshot
from modulo.db.models.run import Run
from modulo.db.models.schema import Schema
from modulo.db.models.secret import Secret
from modulo.db.models.snapshot_schema_pin import SnapshotSchemaPin
from modulo.db.models.sso_provider import SsoProvider
from modulo.db.models.team import Team
from modulo.db.models.team_membership import TeamMembership
from modulo.db.models.trigger import Trigger
from modulo.db.models.webhook import WebhookDedupHash

_log = logging.getLogger(__name__)

ENTITY_MODEL_MAP: dict[str, type] = {
    "secret": Secret,
    "connector": ConnectorInstance,
    "environment_profile": EnvironmentProfile,
    "lifecycle_map": LifecycleMap,
    "model_backend": ModelBackend,
    "org_api_key": OrgApiKey,
    "parameter_schema": ParameterSchema,
    "pipeline": Pipeline,
    "pipeline_snapshot": PipelineSnapshot,
    "schema": Schema,
    "sso_provider": SsoProvider,
    "team": Team,
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
    "duplicate_triggers": "Duplicate Triggers",
    "unused_environment_profiles": "Unused Environment Profiles",
    "stale_api_keys": "Stale API Keys",
    "unused_sso_providers": "Unused SSO Providers",
    "empty_teams": "Empty Teams",
    "unused_parameter_schemas": "Unused Parameter Schemas",
    "unused_schemas": "Unused Schemas",
    "empty_lifecycle_maps": "Empty Lifecycle Maps",
}

_CATEGORY_TO_ENTITY: dict[str, str] = {
    "orphan_secrets": "secret",
    "unbound_connectors": "connector",
    "untriggered_pipelines": "pipeline",
    "stale_pipelines": "pipeline",
    "unused_model_backends": "model_backend",
    "inactive_triggers": "trigger",
    "orphan_snapshots": "pipeline_snapshot",
    "expired_webhook_dedups": "webhook_dedup",
    "duplicate_triggers": "trigger",
    "unused_environment_profiles": "environment_profile",
    "stale_api_keys": "org_api_key",
    "unused_sso_providers": "sso_provider",
    "empty_teams": "team",
    "unused_parameter_schemas": "parameter_schema",
    "unused_schemas": "schema",
    "empty_lifecycle_maps": "lifecycle_map",
}

_CATEGORY_DESCRIPTIONS: dict[str, str] = {
    "orphan_secrets": "Secrets whose key is not referenced by any connector config or agent connector_type_refs",
    "unbound_connectors": "Connector instances not bound to any pipeline snapshot",
    "untriggered_pipelines": "Pipelines with no trigger and no runs",
    "stale_pipelines": "Pipelines with no runs in the last 4 weeks",
    "unused_model_backends": "Model backends not assigned to any agent",
    "inactive_triggers": "Triggers that are inactive and have never fired",
    "orphan_snapshots": "Snapshots whose pipeline no longer exists",
    "expired_webhook_dedups": "Expired webhook deduplication hash entries",
    "duplicate_triggers": "Pipelines with multiple triggers of the same type (e.g. two cron triggers on the same pipeline)",
    "unused_environment_profiles": "Environment profiles not referenced by any pipeline snapshot",
    "stale_api_keys": "API keys not used in the last 4 weeks",
    "unused_sso_providers": "SSO providers with no accounts using them for authentication",
    "empty_teams": "Teams with no active user members",
    "unused_parameter_schemas": "Parameter schemas not assigned to any agent",
    "unused_schemas": "Schemas not referenced by any agent or pipeline snapshot",
    "empty_lifecycle_maps": "Lifecycle maps with empty content (no stages configured)",
}


class Candidate:
    def __init__(self, id: str, name: str, detail: str, created_at: str | None = None, entity_type: str = "") -> None:
        self.id = id
        self.name = name
        self.detail = detail
        self.created_at = created_at
        self.entity_type = entity_type

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "detail": self.detail,
            "created_at": self.created_at,
            "entity_type": self.entity_type,
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

    return [
        Candidate(
            id=str(s.id),
            name=s.key,
            detail="Orphan secret — no connector or agent references this key",
            created_at=s.created_at.isoformat() if s.created_at else None,
        )
        for s in secrets
        if s.key not in referenced_keys
    ]


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

    return [
        Candidate(
            id=str(c.id),
            name=c.name,
            detail=f"Connector instance (type: {c.connector_type_id}) — not bound to any snapshot",
            created_at=c.created_at.isoformat() if c.created_at else None,
        )
        for c in connectors
        if c.id not in bound_ids
    ]


async def _scan_untriggered_pipelines(session: AsyncSession, org_id: uuid.UUID) -> list[Candidate]:
    trigger_subq = select(Trigger.pipeline_id).where(Trigger.organisation_id == org_id).subquery()
    run_subq = select(Run.pipeline_id).where(Run.organisation_id == org_id).distinct().subquery()
    pipelines = (
        (
            await session.execute(
                select(Pipeline).where(
                    Pipeline.organisation_id == org_id,
                    Pipeline.deleted_at.is_(None),
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
    four_weeks_ago = datetime.now(UTC) - timedelta(weeks=4)
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
                    Pipeline.deleted_at.is_(None),
                    max_run.c.last_run_at < four_weeks_ago,
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
            detail="Pipeline last run over 4 weeks ago",
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
    return [
        Candidate(
            id=str(mb.id),
            name=mb.name,
            detail=f"Model backend ({mb.provider}/{mb.model_id}) — not assigned to any agent",
            created_at=mb.created_at.isoformat() if mb.created_at else None,
        )
        for mb in backends
        if mb.id not in used_set
    ]


async def _scan_inactive_triggers(session: AsyncSession, org_id: uuid.UUID) -> list[Candidate]:
    triggers = (
        (
            await session.execute(
                select(Trigger).where(
                    Trigger.organisation_id == org_id,
                    Trigger.deleted_at.is_(None),
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
            created_at=r.created_at.isoformat() if r.created_at else None,
        )
        for r in rows
    ]


async def _scan_duplicate_triggers(session: AsyncSession, org_id: uuid.UUID) -> list[Candidate]:
    """Find pipelines with multiple triggers of the same type (e.g. two cron triggers)."""
    from sqlalchemy import func as sa_func

    dup_subq = (
        select(
            Trigger.pipeline_id,
            Trigger.trigger_type,
            sa_func.count(Trigger.id).label("cnt"),
        )
        .where(
            Trigger.organisation_id == org_id,
            Trigger.deleted_at.is_(None),
        )
        .group_by(Trigger.pipeline_id, Trigger.trigger_type)
        .having(sa_func.count(Trigger.id) > 1)
        .subquery()
    )

    duplicate_triggers = (
        (
            await session.execute(
                select(Trigger)
                .join(
                    dup_subq,
                    (Trigger.pipeline_id == dup_subq.c.pipeline_id) & (Trigger.trigger_type == dup_subq.c.trigger_type),
                )
                .where(
                    Trigger.organisation_id == org_id,
                    Trigger.deleted_at.is_(None),
                )
                .order_by(Trigger.pipeline_id, Trigger.trigger_type, Trigger.created_at)
            )
        )
        .scalars()
        .all()
    )

    # Group by pipeline+type so the detail message is informative
    groups: dict[tuple[uuid.UUID, str], list[Trigger]] = {}
    for t in duplicate_triggers:
        groups.setdefault((t.pipeline_id, t.trigger_type), []).append(t)

    return [
        Candidate(
            id=str(t.id),
            name=f"Trigger {ttype} for pipeline {pid}",
            detail=f"Duplicate {ttype} trigger — {len(triggers)} total on this pipeline. "
            f"Created: {t.created_at.isoformat() if t.created_at else 'N/A'}",
            created_at=t.created_at.isoformat() if t.created_at else None,
        )
        for (pid, ttype), triggers in groups.items()
        for t in triggers[1:]
    ]


async def _scan_unused_environment_profiles(session: AsyncSession, org_id: uuid.UUID) -> list[Candidate]:
    """Environment profiles not referenced by any pipeline snapshot."""
    used_ids = (
        select(PipelineSnapshot.environment_profile_id)
        .where(
            PipelineSnapshot.organisation_id == org_id,
            PipelineSnapshot.environment_profile_id.is_not(None),
        )
        .distinct()
        .subquery()
    )
    profiles = (
        (
            await session.execute(
                select(EnvironmentProfile).where(
                    EnvironmentProfile.organisation_id == org_id,
                    EnvironmentProfile.deleted_at.is_(None),
                    EnvironmentProfile.id.notin_(select(used_ids.c.environment_profile_id)),
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
            detail=f"Environment profile ({p.provider_type}) — not used by any pipeline snapshot",
            created_at=p.created_at.isoformat() if p.created_at else None,
        )
        for p in profiles
    ]


async def _scan_stale_api_keys(session: AsyncSession, org_id: uuid.UUID) -> list[Candidate]:
    """API keys not used in the last 4 weeks, excluding already-revoked or expired keys."""
    four_weeks_ago = datetime.now(UTC) - timedelta(weeks=4)
    keys = (
        (
            await session.execute(
                select(OrgApiKey).where(
                    OrgApiKey.organisation_id == org_id,
                    OrgApiKey.revoked_at.is_(None),
                    ((OrgApiKey.last_used_at.is_(None)) | (OrgApiKey.last_used_at < four_weeks_ago)),
                )
            )
        )
        .scalars()
        .all()
    )
    return [
        Candidate(
            id=str(k.id),
            name=k.name,
            detail=f"API key (role: {k.role}) — {'never used' if k.last_used_at is None else f'last used {k.last_used_at.isoformat()}'}",
            created_at=k.created_at.isoformat() if k.created_at else None,
        )
        for k in keys
    ]


async def _scan_unused_sso_providers(session: AsyncSession, org_id: uuid.UUID) -> list[Candidate]:
    """SSO providers with no accounts using SSO authentication in this org."""
    providers = (
        (await session.execute(select(SsoProvider).where(SsoProvider.organisation_id == org_id))).scalars().all()
    )
    if not providers:
        return []

    # Check if any org members use SSO auth (non-local auth_provider)
    sso_accounts_subq = (
        select(OrgMembership.account_id)
        .join(Account, OrgMembership.account_id == Account.id)
        .where(
            OrgMembership.organisation_id == org_id,
            OrgMembership.deactivated_at.is_(None),
            Account.auth_provider.in_(["oidc", "saml", "scim"]),
        )
        .distinct()
        .subquery()
    )
    result = await session.execute(select(func.count()).select_from(sso_accounts_subq))
    sso_user_count = result.scalar() or 0

    if sso_user_count > 0:
        return []  # SSO is in use, no candidates

    return [
        Candidate(
            id=str(p.id),
            name=p.name,
            detail=f"SSO provider ({p.provider_type}) — no accounts use SSO authentication in this org",
            created_at=p.created_at.isoformat() if p.created_at else None,
        )
        for p in providers
    ]


async def _scan_empty_teams(session: AsyncSession, org_id: uuid.UUID) -> list[Candidate]:
    """Teams with no active user members."""
    teams_with_members = (
        select(TeamMembership.team_id).where(TeamMembership.organisation_id == org_id).distinct().subquery()
    )

    teams = (
        (
            await session.execute(
                select(Team).where(
                    Team.organisation_id == org_id,
                    Team.deleted_at.is_(None),
                    Team.id.notin_(select(teams_with_members.c.team_id)),
                )
            )
        )
        .scalars()
        .all()
    )
    return [
        Candidate(
            id=str(t.id),
            name=t.name,
            detail="Team has no member assignments",
            created_at=t.created_at.isoformat() if t.created_at else None,
        )
        for t in teams
    ]


async def _scan_unused_parameter_schemas(session: AsyncSession, org_id: uuid.UUID) -> list[Candidate]:
    """Parameter schemas not assigned to any agent."""
    used_ids = (
        select(Agent.parameter_schema_id)
        .where(
            Agent.organisation_id == org_id,
            Agent.parameter_schema_id.is_not(None),
        )
        .distinct()
        .subquery()
    )
    schemas = (
        (
            await session.execute(
                select(ParameterSchema).where(
                    ParameterSchema.organisation_id == org_id,
                    ParameterSchema.deleted_at.is_(None),
                    ParameterSchema.id.notin_(select(used_ids.c.parameter_schema_id)),
                )
            )
        )
        .scalars()
        .all()
    )
    return [
        Candidate(
            id=str(s.id),
            name=s.name,
            detail=f"Parameter schema v{s.version} — not assigned to any agent",
            created_at=s.created_at.isoformat() if s.created_at else None,
        )
        for s in schemas
    ]


async def _scan_unused_schemas(session: AsyncSession, org_id: uuid.UUID) -> list[Candidate]:
    """Schemas not referenced by any agent (input/output) or snapshot schema pin. Excludes system schemas."""
    # IDs used by agents (input or output schema)
    agent_input_ids = select(Agent.input_schema_id).where(Agent.organisation_id == org_id).distinct().subquery()
    agent_output_ids = select(Agent.output_schema_id).where(Agent.organisation_id == org_id).distinct().subquery()

    # IDs used by snapshot schema pins
    pin_schema_ids = (
        select(SnapshotSchemaPin.schema_id).where(SnapshotSchemaPin.organisation_id == org_id).distinct().subquery()
    )

    schemas = (
        (
            await session.execute(
                select(Schema).where(
                    Schema.organisation_id == org_id,
                    Schema.system.is_(False),
                    Schema.id.notin_(select(agent_input_ids.c.input_schema_id)),
                    Schema.id.notin_(select(agent_output_ids.c.output_schema_id)),
                    Schema.id.notin_(select(pin_schema_ids.c.schema_id)),
                )
            )
        )
        .scalars()
        .all()
    )
    return [
        Candidate(
            id=str(s.id),
            name=s.name,
            detail="Schema not used by any agent or pipeline snapshot",
            created_at=s.created_at.isoformat() if s.created_at else None,
        )
        for s in schemas
    ]


async def _scan_empty_lifecycle_maps(session: AsyncSession, org_id: uuid.UUID) -> list[Candidate]:
    """Lifecycle maps with empty content (no stages configured)."""
    maps = (
        (
            await session.execute(
                select(LifecycleMap).where(
                    LifecycleMap.organisation_id == org_id,
                    LifecycleMap.deleted_at.is_(None),
                    LifecycleMap.content_json == {},  # empty dict = no content
                )
            )
        )
        .scalars()
        .all()
    )
    return [
        Candidate(
            id=str(m.id),
            name=m.name,
            detail="Lifecycle map has no stages configured (empty content)",
            created_at=m.created_at.isoformat() if m.created_at else None,
        )
        for m in maps
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
    ("duplicate_triggers", _scan_duplicate_triggers),
    ("unused_environment_profiles", _scan_unused_environment_profiles),
    ("stale_api_keys", _scan_stale_api_keys),
    ("unused_sso_providers", _scan_unused_sso_providers),
    ("empty_teams", _scan_empty_teams),
    ("unused_parameter_schemas", _scan_unused_parameter_schemas),
    ("unused_schemas", _scan_unused_schemas),
    ("empty_lifecycle_maps", _scan_empty_lifecycle_maps),
]


async def scan_all(session: AsyncSession, org_id: uuid.UUID) -> list[CategoryResult]:
    results: list[CategoryResult] = []
    for category, scanner in _SCANNERS:
        try:
            candidates = await scanner(session, org_id)
            entity_type = _CATEGORY_TO_ENTITY[category]
            for c in candidates:
                c.entity_type = entity_type
            results.append(CategoryResult(category=category, candidates=candidates))
        except Exception:
            _log.exception("Housekeeping scanner '%s' failed", category)
            results.append(CategoryResult(category=category, candidates=[]))
    return results
