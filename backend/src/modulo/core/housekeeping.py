"""Housekeeping service — scans for cleanup candidates within an org scope."""

import contextlib
import logging
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.crud.run_retention import CHECKPOINT_RETENTION_DAYS, _checkpoint_detail
from modulo.db.models.account import Account
from modulo.db.models.agent import Agent
from modulo.db.models.api_key import OrgApiKey
from modulo.db.models.connector_instance import ConnectorInstance
from modulo.db.models.environment_profile import EnvironmentProfile
from modulo.db.models.lifecycle_map import LifecycleMap
from modulo.db.models.model_backend import ModelBackend
from modulo.db.models.org_membership import OrgMembership
from modulo.db.models.organisation import Organisation
from modulo.db.models.parameter_schema import ParameterSchema
from modulo.db.models.pipeline import Pipeline
from modulo.db.models.pipeline_snapshot import PipelineSnapshot
from modulo.db.models.run import TERMINAL_STATUSES, Run
from modulo.db.models.schema import Schema
from modulo.db.models.secret import Secret
from modulo.db.models.snapshot_schema_pin import SnapshotSchemaPin
from modulo.db.models.sso_provider import SsoProvider
from modulo.db.models.team import Team
from modulo.db.models.team_membership import TeamMembership
from modulo.db.models.trigger import Trigger
from modulo.db.models.webhook import WebhookDedupHash

_log = logging.getLogger(__name__)

# Registry of every ORM model that carries an ``organisation_id`` column
# (the tenant-scoped tables). Populated lazily from the mapper registry so it
# stays in sync with the model layer without a hand-maintained list.
_TENANT_MODELS: list[type] | None = None


def _collect_tenant_models() -> list[type]:
    """Return all mapped classes that own an ``organisation_id`` column.

    Excludes the ``organisations`` table itself (which is the FK target).
    """
    import modulo.db.models  # noqa: F401  (ensures all models are registered)
    from modulo.db.models.base import Base

    models: list[type] = []
    for mapper in Base.registry.mappers:
        cls = mapper.class_
        table = getattr(cls, "__table__", None)
        if table is None or table.name == "organisations":
            continue
        if "organisation_id" not in table.columns:
            continue
        models.append(cls)
    return models


def _tenant_models() -> list[type]:
    global _TENANT_MODELS
    if _TENANT_MODELS is None:
        _TENANT_MODELS = _collect_tenant_models()
    return _TENANT_MODELS


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

# Categories that are detection-only (surfaced for triage, never auto-deleted).
# Submitting a candidate from one of these to the cleanup endpoint returns a
# clear triage message instead of a misleading "Unknown entity type" error.
NON_DELETABLE_ENTITY_TYPES: frozenset[str] = frozenset({"invalid_org_fk"})


@dataclass(frozen=True)
class Scanner:
    """A single housekeeping scanner registration.

    Consolidates what was previously spread across four parallel dicts
    (_SCANNERS, _CATEGORY_LABELS, _CATEGORY_DESCRIPTIONS, _CATEGORY_TO_ENTITY)
    into one typed structure.

    ``entity_type`` is the cleanup target entity type applied to every candidate
    produced by the scanner. Set it to ``None`` for DETECTION-ONLY scanners
    (e.g. ``invalid_org_fk``) whose candidates carry their own ``entity_type``
    directly — those must not be overridden by the category default.
    """

    category: str
    scan_func: Callable[[AsyncSession, uuid.UUID], Awaitable[list["Candidate"]]]
    label: str
    description: str
    entity_type: str | None = None


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
        entry = SCANNERS_BY_CATEGORY.get(category)
        self.label = entry.label if entry is not None else category
        self.description = entry.description if entry is not None else ""
        self.candidates = candidates

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "label": self.label,
            "description": self.description,
            "candidates": [c.to_dict() for c in self.candidates],
            "count": len(self.candidates),
        }


def _connector_secret_keys(connectors: Sequence[Any]) -> set[str]:
    """Collect every secret key referenced by connector configs."""
    referenced: set[str] = set()
    for c in connectors:
        for v in (c.config_json or {}).values():
            if isinstance(v, str):
                referenced.add(v)
    return referenced


def _agent_secret_keys(agents: Sequence[Any]) -> set[str]:
    """Collect every secret key referenced by agent connector_type_refs."""
    referenced: set[str] = set()
    for a in agents:
        for ref in a.connector_type_refs or []:
            if not isinstance(ref, dict):
                continue
            secret_key = ref.get("secret_key") or ref.get("key")
            if secret_key:
                referenced.add(secret_key)
    return referenced


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

    referenced_keys = _connector_secret_keys(connectors) | _agent_secret_keys(agents)

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


def _collect_bound_connector_ids(snapshots: Sequence[Any]) -> set[uuid.UUID]:
    """Collect every connector instance id bound by any pipeline snapshot."""
    bound_ids: set[uuid.UUID] = set()
    for snap in snapshots:
        bindings = snap.connector_bindings_json or []
        for b in bindings:
            cid = b.get("connector_instance_id") or b.get("connector_id")
            if cid:
                with contextlib.suppress(ValueError, TypeError):
                    bound_ids.add(uuid.UUID(cid) if isinstance(cid, str) else cid)
    return bound_ids


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

    bound_ids = _collect_bound_connector_ids(snapshots)

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
    stmt = select(Trigger).where(
        Trigger.organisation_id == org_id,
        Trigger.deleted_at.is_(None),
        Trigger.active.is_(False),
        Trigger.last_fired_at.is_(None),
    )
    result = await session.execute(stmt)
    triggers = result.scalars().all()

    candidates: list[Candidate] = []
    for t in triggers:
        candidates.append(
            Candidate(
                id=str(t.id),
                name=f"Trigger {t.trigger_type} for pipeline {t.pipeline_id}",
                detail="Trigger is inactive and has never fired",
                created_at=t.created_at.isoformat() if t.created_at else None,
            )
        )
    return candidates


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
    dup_subq = (
        select(
            Trigger.pipeline_id,
            Trigger.trigger_type,
            func.count(Trigger.id).label("cnt"),
        )
        .where(
            Trigger.organisation_id == org_id,
            Trigger.deleted_at.is_(None),
        )
        .group_by(Trigger.pipeline_id, Trigger.trigger_type)
        .having(func.count(Trigger.id) > 1)
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

    def _describe_key_usage(k: OrgApiKey) -> str:
        if k.last_used_at is None:
            return "never used"
        return f"last used {k.last_used_at.isoformat()}"

    return [
        Candidate(
            id=str(k.id),
            name=k.name,
            detail=f"API key (role: {k.role}) — {_describe_key_usage(k)}",
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
    agent_input_ids = (
        select(Agent.input_schema_id)
        .where(Agent.organisation_id == org_id, Agent.input_schema_id.is_not(None))
        .distinct()
        .subquery()
    )
    agent_output_ids = (
        select(Agent.output_schema_id)
        .where(Agent.organisation_id == org_id, Agent.output_schema_id.is_not(None))
        .distinct()
        .subquery()
    )

    # IDs used by snapshot schema pins
    pin_schema_ids = (
        select(SnapshotSchemaPin.schema_id)
        .where(SnapshotSchemaPin.organisation_id == org_id, SnapshotSchemaPin.schema_id.is_not(None))
        .distinct()
        .subquery()
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


async def _scan_invalid_org_fk(session: AsyncSession, org_id: uuid.UUID) -> list[Candidate]:
    """Detect tenant-scoped rows whose ``organisation_id`` points to a missing org.

    This is the detection counterpart to migration ``0120_org_fk_hardening``. That
    migration adds DB-level FK constraints (where data is already clean) to prevent
    NEW orphaned tenant rows; this scan reports any that already exist so they can
    be triaged. It is a **read-only, detection-only** category — floated rows are
    surfaced for human review, never auto-deleted, because reparenting/removing
    orphaned tenant data is a destructive, decision-gated action.

    Because housekeeping runs scoped to ``org_id`` (under RLS), the practical
    signal is: if ``organisation_id`` no longer exists in ``organisations``, every
    tenant-scoped row still carrying that value is orphaned and is floated here.
    """
    org_exists = (
        await session.execute(select(Organisation.id).where(Organisation.id == org_id))
    ).scalar_one_or_none() is not None

    if org_exists:
        # The org is valid, so no row within this scope can reference a missing org.
        return []

    candidates: list[Candidate] = []
    for cls in _tenant_models():
        model_cls = cast(Any, cls)
        table = model_cls.__table__
        if "organisation_id" not in table.c:
            continue
        org_col = table.c.organisation_id
        # Some tenant-scoped tables have a primary key that is NOT a surrogate
        # ``id`` (e.g. OAuthAuthorizationCode PK ``code``, OAuthTokenFamily PK
        # ``family_id``). Derive the candidate id from the real primary key so we
        # never assume ``r.id`` exists and silently drop the whole category when
        # an orphaned row is found in those tables.
        pk_cols = list(table.primary_key.columns)
        if not pk_cols:
            continue
        stmt = select(model_cls).where(org_col == org_id).where(org_col.is_not(None))
        rows = (await session.execute(stmt)).scalars().all()
        for r in rows:
            pk_values = [str(getattr(r, pk.name)) for pk in pk_cols]
            pk_str = "/".join(pk_values)
            candidates.append(
                Candidate(
                    id=pk_str,
                    name=f"{table.name}#{pk_str[:8]}",
                    detail=f"Orphaned tenant row: organisation_id {org_id} no longer exists",
                    entity_type="invalid_org_fk",
                )
            )
    return candidates


async def _scan_checkpoint_retention(session: AsyncSession, org_id: uuid.UUID) -> list[Candidate]:
    """Detect terminal runs whose LangGraph checkpoint rows are reclaimable.

    FAR-432: a terminal run's checkpoint rows (``checkpoints``,
    ``checkpoint_blobs``, ``checkpoint_writes``) are unread after the run
    finishes and dominate DB volume (~7.9GB). Reads ``runs`` for TERMINAL runs
    older than ``CHECKPOINT_RETENTION_DAYS``, estimates the checkpoint bytes each
    owns, and returns one Candidate per run. DETECTION-ONLY (``entity_type`` is
    left empty and the category is registered with ``entity_type=None``): the
    purge is a bulk age-based action, so candidates here are informational and
    are cleared through the dedicated
    ``/api/v1/admin/housekeeping/checkpoints/purge`` endpoint rather than the
    generic per-item cleanup.
    """

    cutoff = datetime.now(UTC) - timedelta(days=CHECKPOINT_RETENTION_DAYS)
    runs = (
        (
            await session.execute(
                select(Run)
                .where(
                    Run.organisation_id == org_id,
                    Run.status.in_(sorted(TERMINAL_STATUSES)),
                    func.coalesce(Run.completed_at, Run.created_at) < cutoff,
                )
                .order_by(Run.created_at)
                .limit(1000)
            )
        )
        .scalars()
        .all()
    )
    if not runs:
        return []

    thread_ids = [r.langgraph_thread_id for r in runs]
    bytes_by_thread, _counts = await _checkpoint_detail(session, thread_ids, org_id)

    candidates: list[Candidate] = []
    for run in runs:
        est = int(bytes_by_thread.get(run.langgraph_thread_id, 0) or 0)
        if est > 0:
            candidates.append(
                Candidate(
                    id=run.langgraph_thread_id,
                    name=f"Run {run.run_number}",
                    detail=f"Terminal run ({run.status}) — ~{est} bytes of checkpoint data beyond retention",
                    created_at=run.created_at.isoformat() if run.created_at else None,
                )
            )
    return candidates


_SCANNERS: list[Scanner] = [
    Scanner(
        category="orphan_secrets",
        scan_func=_scan_orphan_secrets,
        label="Orphan Secrets",
        description="Secrets whose key is not referenced by any connector config or agent connector_type_refs",
        entity_type="secret",
    ),
    Scanner(
        category="unbound_connectors",
        scan_func=_scan_unbound_connectors,
        label="Unbound Connectors",
        description="Connector instances not bound to any pipeline snapshot",
        entity_type="connector",
    ),
    Scanner(
        category="untriggered_pipelines",
        scan_func=_scan_untriggered_pipelines,
        label="Untriggered Pipelines",
        description="Pipelines with no trigger and no runs",
        entity_type="pipeline",
    ),
    Scanner(
        category="stale_pipelines",
        scan_func=_scan_stale_pipelines,
        label="Stale Pipelines",
        description="Pipelines with no runs in the last 4 weeks",
        entity_type="pipeline",
    ),
    Scanner(
        category="unused_model_backends",
        scan_func=_scan_unused_model_backends,
        label="Unused Model Backends",
        description="Model backends not assigned to any agent",
        entity_type="model_backend",
    ),
    Scanner(
        category="inactive_triggers",
        scan_func=_scan_inactive_triggers,
        label="Inactive Triggers",
        description="Triggers that are inactive and have never fired",
        entity_type="trigger",
    ),
    Scanner(
        category="orphan_snapshots",
        scan_func=_scan_orphan_snapshots,
        label="Orphan Snapshots",
        description="Snapshots whose pipeline no longer exists",
        entity_type="pipeline_snapshot",
    ),
    Scanner(
        category="expired_webhook_dedups",
        scan_func=_scan_expired_webhook_dedups,
        label="Expired Webhook Dedups",
        description="Expired webhook deduplication hash entries",
        entity_type="webhook_dedup",
    ),
    Scanner(
        category="duplicate_triggers",
        scan_func=_scan_duplicate_triggers,
        label="Duplicate Triggers",
        description="Pipelines with multiple triggers of the same type (e.g. two cron triggers)",
        entity_type="trigger",
    ),
    Scanner(
        category="unused_environment_profiles",
        scan_func=_scan_unused_environment_profiles,
        label="Unused Environment Profiles",
        description="Environment profiles not referenced by any pipeline snapshot",
        entity_type="environment_profile",
    ),
    Scanner(
        category="stale_api_keys",
        scan_func=_scan_stale_api_keys,
        label="Stale API Keys",
        description="API keys not used in the last 4 weeks",
        entity_type="org_api_key",
    ),
    Scanner(
        category="unused_sso_providers",
        scan_func=_scan_unused_sso_providers,
        label="Unused SSO Providers",
        description="SSO providers with no accounts using them for authentication",
        entity_type="sso_provider",
    ),
    Scanner(
        category="empty_teams",
        scan_func=_scan_empty_teams,
        label="Empty Teams",
        description="Teams with no active user members",
        entity_type="team",
    ),
    Scanner(
        category="unused_parameter_schemas",
        scan_func=_scan_unused_parameter_schemas,
        label="Unused Parameter Schemas",
        description="Parameter schemas not assigned to any agent",
        entity_type="parameter_schema",
    ),
    Scanner(
        category="unused_schemas",
        scan_func=_scan_unused_schemas,
        label="Unused Schemas",
        description="Schemas not referenced by any agent or pipeline snapshot",
        entity_type="schema",
    ),
    Scanner(
        category="empty_lifecycle_maps",
        scan_func=_scan_empty_lifecycle_maps,
        label="Empty Lifecycle Maps",
        description="Lifecycle maps with empty content (no stages configured)",
        entity_type="lifecycle_map",
    ),
    Scanner(
        category="invalid_org_fk",
        scan_func=_scan_invalid_org_fk,
        label="Invalid Organisation FK",
        description=(
            "Tenant-scoped rows whose organisation_id references a non-existent "
            "organisation (orphaned data) — surfaced for triage, not auto-deleted."
        ),
        entity_type=None,
    ),
    Scanner(
        category="checkpoint_retention",
        scan_func=_scan_checkpoint_retention,
        label="Checkpoint Retention",
        description=(
            "Terminal runs with LangGraph graph-state checkpoints beyond the retention "
            "window — surfaced for purge via the Checkpoint Retention panel (bulk, age-based)."
        ),
        entity_type=None,
    ),
]

SCANNERS_BY_CATEGORY: dict[str, Scanner] = {entry.category: entry for entry in _SCANNERS}


async def scan_all(session: AsyncSession, org_id: uuid.UUID) -> list[CategoryResult]:
    results: list[CategoryResult] = []
    for entry in _SCANNERS:
        try:
            candidates = await entry.scan_func(session, org_id)
            # Detection-only scanners (entity_type=None) set entity_type directly
            # on their candidates and must not be overridden here.
            if entry.entity_type is not None:
                for c in candidates:
                    c.entity_type = entry.entity_type
            results.append(CategoryResult(category=entry.category, candidates=candidates))
        except Exception:
            _log.exception("Housekeeping scanner '%s' failed", entry.category)
            results.append(CategoryResult(category=entry.category, candidates=[]))
    return results
