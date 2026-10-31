"""System Maps ORM models (FAR-2236).

Eight org-scoped, soft-delete models:
* SystemMap — top-level map container.
* SystemMapRevision — immutable revision history per map.
* SystemMapNode — nodes within a revision.
* SystemMapRelationship — edges within a revision.
* SystemMapLoopConfig — improvement-loop parameters per loop node.
* SystemMapAttributionBinding — binds pipelines/triggers/actions to a map node.
* SystemMapLoopAdmissionAttempt — idempotency-keyed attempt log (CAS deduplication).
* SystemMapLoopReservation — active reservations with cooldown tracking.
"""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from modulo.db.models.base import JSONB_COL, ONDELETE_SET_NULL, OrgScoped, SoftDeleteMixin

if TYPE_CHECKING:
    from modulo.db.models.pipeline import Pipeline
    from modulo.db.models.trigger import Trigger

# Centralised vocabularies shared with CHECK constraints.
_VALID_NODE_TYPES = (
    "pipeline",
    "agent",
    "connector",
    "improvement_loop",
    "data_store",
    "external_system",
)
_node_type_sql = ", ".join(f"'{v}'" for v in _VALID_NODE_TYPES)

_VALID_REL_TYPES = ("flow", "data", "trigger", "loop_back")
_rel_type_sql = ", ".join(f"'{v}'" for v in _VALID_REL_TYPES)

_VALID_REVISION_STATUSES = ("draft", "published", "archived")
_revision_status_sql = ", ".join(f"'{v}'" for v in _VALID_REVISION_STATUSES)

_VALID_BINDING_KINDS = ("trigger", "action_pipeline")
_binding_kind_sql = ", ".join(f"'{v}'" for v in _VALID_BINDING_KINDS)

_VALID_ADMISSION_STATUSES = ("pending", "admitted", "rejected", "duplicate")
_admission_status_sql = ", ".join(f"'{v}'" for v in _VALID_ADMISSION_STATUSES)

_VALID_RESERVATION_STATUSES = ("active", "consumed", "released", "expired")
_reservation_status_sql = ", ".join(f"'{v}'" for v in _VALID_RESERVATION_STATUSES)

_ONDELETE_RESTRICT = "RESTRICT"
_ONDELETE_CASCADE = "CASCADE"


class SystemMap(SoftDeleteMixin, OrgScoped):
    __tablename__ = "system_maps"
    __table_args__ = (
        CheckConstraint("visibility IN ('org', 'team')", name="ck_system_maps_visibility"),
        CheckConstraint("visibility = 'org' OR owner_team_id IS NOT NULL", name="ck_system_maps_team_owner"),
        # Partial unique: one live default map per org
        Index(
            "uq_system_maps_org_default_live",
            "organisation_id",
            unique=True,
            postgresql_where=text("default_map AND deleted_at IS NULL"),
            sqlite_where=text("default_map AND deleted_at IS NULL"),
        ),
        UniqueConstraint("id", "organisation_id", name="uq_system_maps_id_organisation_id"),
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(String(2000))
    owner_team_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("teams.id", ondelete=ONDELETE_SET_NULL), index=True
    )
    visibility: Mapped[str] = mapped_column(String(10), nullable=False, server_default="org")
    default_map: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false", default=False)
    published_revision_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), nullable=True, index=True)
    account_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(), ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("accounts.id", ondelete=ONDELETE_SET_NULL), nullable=True
    )
    deleted_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("accounts.id", ondelete=ONDELETE_SET_NULL), nullable=True, index=True
    )

    # Composite FK to system_map_revisions (id + organisation_id) is declared in the migration.
    # SQLAlchemy doesn't natively support composite FKs to a non-PK unique constraint,
    # so we omit the relationship here and handle it at the repository layer.

    published_revision: Mapped["SystemMapRevision | None"] = relationship(
        foreign_keys=[published_revision_id],
        primaryjoin="and_(SystemMap.published_revision_id==SystemMapRevision.id, "
        "SystemMap.organisation_id==SystemMapRevision.organisation_id)",
        viewonly=True,
    )

    revisions: Mapped[list["SystemMapRevision"]] = relationship(
        back_populates="system_map",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    attribution_bindings: Mapped[list["SystemMapAttributionBinding"]] = relationship(
        back_populates="system_map",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    # nodes, relationships, and loop_configs are accessed via revisions
    # (no direct FK from those tables to system_maps)


class SystemMapRevision(SoftDeleteMixin, OrgScoped):
    __tablename__ = "system_map_revisions"
    __table_args__ = (
        CheckConstraint(f"status IN ({_revision_status_sql})", name="ck_system_map_revisions_status"),
        UniqueConstraint("system_map_id", "revision_number", name="uq_system_map_revisions_map_number"),
        UniqueConstraint("id", "organisation_id", name="uq_system_map_revisions_id_organisation_id"),
        ForeignKeyConstraint(
            ["system_map_id", "organisation_id"],
            ["system_maps.id", "system_maps.organisation_id"],
            ondelete=_ONDELETE_CASCADE,
            name="fk_system_map_revisions_map_org",
        ),
    )

    system_map_id: Mapped[uuid.UUID] = mapped_column(Uuid(), nullable=False, index=True)
    revision_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, server_default="draft")
    content_json: Mapped[dict[str, Any]] = mapped_column(JSONB_COL, nullable=False, default=dict)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("accounts.id", ondelete=ONDELETE_SET_NULL), nullable=True, index=True
    )

    system_map: Mapped[SystemMap] = relationship(
        back_populates="revisions",
        primaryjoin="and_(SystemMapRevision.system_map_id==SystemMap.id, "
        "SystemMapRevision.organisation_id==SystemMap.organisation_id)",
        foreign_keys="[SystemMapRevision.system_map_id, SystemMapRevision.organisation_id]",
    )
    nodes: Mapped[list["SystemMapNode"]] = relationship(
        back_populates="revision",
        primaryjoin="and_(SystemMapNode.revision_id==SystemMapRevision.id, "
        "SystemMapNode.organisation_id==SystemMapRevision.organisation_id)",
        foreign_keys="[SystemMapNode.revision_id, SystemMapNode.organisation_id]",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    relationships: Mapped[list["SystemMapRelationship"]] = relationship(
        back_populates="revision",
        primaryjoin="and_(SystemMapRelationship.revision_id==SystemMapRevision.id, "
        "SystemMapRelationship.organisation_id==SystemMapRevision.organisation_id)",
        foreign_keys="[SystemMapRelationship.revision_id, SystemMapRelationship.organisation_id]",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    loop_configs: Mapped[list["SystemMapLoopConfig"]] = relationship(
        back_populates="revision",
        primaryjoin="and_(SystemMapLoopConfig.revision_id==SystemMapRevision.id, "
        "SystemMapLoopConfig.organisation_id==SystemMapRevision.organisation_id)",
        foreign_keys="[SystemMapLoopConfig.revision_id, SystemMapLoopConfig.organisation_id]",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class SystemMapNode(SoftDeleteMixin, OrgScoped):
    __tablename__ = "system_map_nodes"
    __table_args__ = (
        CheckConstraint(f"node_type IN ({_node_type_sql})", name="ck_system_map_nodes_type"),
        UniqueConstraint("revision_id", "node_key", name="uq_system_map_nodes_revision_key"),
        UniqueConstraint("id", "organisation_id", name="uq_system_map_nodes_id_organisation_id"),
        ForeignKeyConstraint(
            ["revision_id", "organisation_id"],
            ["system_map_revisions.id", "system_map_revisions.organisation_id"],
            ondelete=_ONDELETE_CASCADE,
            name="fk_system_map_nodes_revision",
        ),
    )

    revision_id: Mapped[uuid.UUID] = mapped_column(Uuid(), nullable=False, index=True)
    node_key: Mapped[str] = mapped_column(String(100), nullable=False)
    node_type: Mapped[str] = mapped_column(String(50), nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    config_json: Mapped[dict[str, Any]] = mapped_column(JSONB_COL, nullable=False, default=dict)
    pipeline_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("pipelines.id", ondelete=ONDELETE_SET_NULL), index=True
    )
    connector_instance_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("connector_instances.id", ondelete=ONDELETE_SET_NULL), index=True
    )
    owner_team_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("teams.id", ondelete=ONDELETE_SET_NULL), index=True
    )
    eval_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("evals.id", ondelete=ONDELETE_SET_NULL), index=True
    )
    position_x: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False, server_default="0")
    position_y: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False, server_default="0")
    deleted_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("accounts.id", ondelete=ONDELETE_SET_NULL), nullable=True, index=True
    )

    revision: Mapped[SystemMapRevision] = relationship(
        back_populates="nodes",
        primaryjoin="and_(SystemMapNode.revision_id==SystemMapRevision.id, "
        "SystemMapNode.organisation_id==SystemMapRevision.organisation_id)",
        foreign_keys="[SystemMapNode.revision_id, SystemMapNode.organisation_id]",
    )


class SystemMapRelationship(SoftDeleteMixin, OrgScoped):
    __tablename__ = "system_map_relationships"
    __table_args__ = (
        CheckConstraint(f"relationship_type IN ({_rel_type_sql})", name="ck_system_map_relationships_type"),
        UniqueConstraint(
            "revision_id",
            "source_node_key",
            "target_node_key",
            "relationship_type",
            name="uq_system_map_relationships_rev_src_tgt_type",
        ),
        UniqueConstraint("id", "organisation_id", name="uq_system_map_relationships_id_organisation_id"),
        ForeignKeyConstraint(
            ["revision_id", "organisation_id"],
            ["system_map_revisions.id", "system_map_revisions.organisation_id"],
            ondelete=_ONDELETE_CASCADE,
            name="fk_system_map_relationships_revision",
        ),
    )

    revision_id: Mapped[uuid.UUID] = mapped_column(Uuid(), nullable=False, index=True)
    source_node_key: Mapped[str] = mapped_column(String(100), nullable=False)
    target_node_key: Mapped[str] = mapped_column(String(100), nullable=False)
    relationship_type: Mapped[str] = mapped_column(String(50), nullable=False)
    config_json: Mapped[dict[str, Any]] = mapped_column(JSONB_COL, nullable=False, default=dict)
    deleted_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("accounts.id", ondelete=ONDELETE_SET_NULL), nullable=True, index=True
    )

    revision: Mapped[SystemMapRevision] = relationship(
        back_populates="relationships",
        primaryjoin="and_(SystemMapRelationship.revision_id==SystemMapRevision.id, "
        "SystemMapRelationship.organisation_id==SystemMapRevision.organisation_id)",
        foreign_keys="[SystemMapRelationship.revision_id, SystemMapRelationship.organisation_id]",
    )


class SystemMapLoopConfig(SoftDeleteMixin, OrgScoped):
    __tablename__ = "system_map_loop_configs"
    __table_args__ = (
        UniqueConstraint("revision_id", "node_key", name="uq_system_map_loop_configs_revision_key"),
        CheckConstraint("observation_window_seconds > 0", name="ck_loop_configs_obs_window"),
        CheckConstraint("budget_limit_usd >= 0", name="ck_loop_configs_budget"),
        CheckConstraint("cooldown_seconds >= 0", name="ck_loop_configs_cooldown"),
        UniqueConstraint("id", "organisation_id", name="uq_system_map_loop_configs_id_organisation_id"),
        ForeignKeyConstraint(
            ["revision_id", "organisation_id"],
            ["system_map_revisions.id", "system_map_revisions.organisation_id"],
            ondelete=_ONDELETE_CASCADE,
            name="fk_system_map_loop_configs_revision",
        ),
    )

    revision_id: Mapped[uuid.UUID] = mapped_column(Uuid(), nullable=False, index=True)
    node_key: Mapped[str] = mapped_column(String(100), nullable=False)
    target_signal: Mapped[str] = mapped_column(String(100), nullable=False)
    observation_window_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    action_pipeline_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(), ForeignKey("pipelines.id", ondelete=_ONDELETE_RESTRICT), nullable=False, index=True
    )
    budget_limit_usd: Mapped[Decimal] = mapped_column(Numeric(14, 6), nullable=False)
    cooldown_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    outcome_metric: Mapped[str] = mapped_column(String(100), nullable=False)
    deleted_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("accounts.id", ondelete=ONDELETE_SET_NULL), nullable=True, index=True
    )

    revision: Mapped[SystemMapRevision] = relationship(
        back_populates="loop_configs",
        primaryjoin="and_(SystemMapLoopConfig.revision_id==SystemMapRevision.id, "
        "SystemMapLoopConfig.organisation_id==SystemMapRevision.organisation_id)",
        foreign_keys="[SystemMapLoopConfig.revision_id, SystemMapLoopConfig.organisation_id]",
    )
    action_pipeline: Mapped["Pipeline"] = relationship(foreign_keys=[action_pipeline_id])
    admission_attempts: Mapped[list["SystemMapLoopAdmissionAttempt"]] = relationship(
        back_populates="loop_config",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    reservations: Mapped[list["SystemMapLoopReservation"]] = relationship(
        back_populates="loop_config",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class SystemMapAttributionBinding(SoftDeleteMixin, OrgScoped):
    __tablename__ = "system_map_attribution_bindings"
    __table_args__ = (
        CheckConstraint(f"binding_kind IN ({_binding_kind_sql})", name="ck_system_map_attribution_bindings_kind"),
        CheckConstraint(
            "(binding_kind = 'trigger' AND trigger_id IS NOT NULL) "
            "OR (binding_kind = 'action_pipeline' AND trigger_id IS NULL)",
            name="ck_system_map_attribution_bindings_trigger_semantics",
        ),
        UniqueConstraint("id", "organisation_id", name="uq_system_map_attribution_bindings_id_organisation_id"),
        Index(
            "uq_attr_binding_trigger_per_pipeline_node",
            "organisation_id",
            "pipeline_id",
            "node_key",
            unique=True,
            postgresql_where=text("binding_kind = 'trigger' AND deleted_at IS NULL"),
            sqlite_where=text("binding_kind = 'trigger' AND deleted_at IS NULL"),
        ),
        Index(
            "uq_attr_binding_action_per_pipeline_node",
            "organisation_id",
            "pipeline_id",
            "node_key",
            unique=True,
            postgresql_where=text("binding_kind = 'action_pipeline' AND deleted_at IS NULL"),
            sqlite_where=text("binding_kind = 'action_pipeline' AND deleted_at IS NULL"),
        ),
        ForeignKeyConstraint(
            ["system_map_id", "organisation_id"],
            ["system_maps.id", "system_maps.organisation_id"],
            ondelete=_ONDELETE_CASCADE,
            name="fk_system_map_attribution_bindings_map",
        ),
    )

    pipeline_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(), ForeignKey("pipelines.id", ondelete=_ONDELETE_CASCADE), nullable=False, index=True
    )
    trigger_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("triggers.id", ondelete=_ONDELETE_CASCADE), index=True
    )
    system_map_id: Mapped[uuid.UUID] = mapped_column(Uuid(), nullable=False, index=True)
    node_key: Mapped[str] = mapped_column(String(100), nullable=False)
    binding_kind: Mapped[str] = mapped_column(String(30), nullable=False)
    deleted_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("accounts.id", ondelete=ONDELETE_SET_NULL), nullable=True, index=True
    )

    system_map: Mapped[SystemMap] = relationship(back_populates="attribution_bindings")
    pipeline: Mapped["Pipeline"] = relationship(foreign_keys=[pipeline_id])
    trigger: Mapped["Trigger"] = relationship(foreign_keys=[trigger_id])


class SystemMapLoopAdmissionAttempt(SoftDeleteMixin, OrgScoped):
    __tablename__ = "system_map_loop_admission_attempts"
    __table_args__ = (
        CheckConstraint(f"status IN ({_admission_status_sql})", name="ck_loop_admission_attempts_status"),
        UniqueConstraint("loop_config_id", "idempotency_key", name="uq_loop_admission_attempts_config_key"),
        UniqueConstraint("id", "organisation_id", name="uq_system_map_loop_admission_attempts_id_organisation_id"),
        ForeignKeyConstraint(
            ["loop_config_id", "organisation_id"],
            ["system_map_loop_configs.id", "system_map_loop_configs.organisation_id"],
            ondelete=_ONDELETE_CASCADE,
            name="fk_system_map_loop_admission_attempts_config",
        ),
    )

    loop_config_id: Mapped[uuid.UUID] = mapped_column(Uuid(), nullable=False, index=True)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, server_default="pending")
    deleted_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("accounts.id", ondelete=ONDELETE_SET_NULL), nullable=True, index=True
    )

    loop_config: Mapped[SystemMapLoopConfig] = relationship(back_populates="admission_attempts")


class SystemMapLoopReservation(SoftDeleteMixin, OrgScoped):
    __tablename__ = "system_map_loop_reservations"
    __table_args__ = (
        CheckConstraint(f"status IN ({_reservation_status_sql})", name="ck_loop_reservations_status"),
        UniqueConstraint("loop_config_id", "idempotency_key", name="uq_loop_reservations_config_key"),
        UniqueConstraint("id", "organisation_id", name="uq_system_map_loop_reservations_id_organisation_id"),
        ForeignKeyConstraint(
            ["loop_config_id", "organisation_id"],
            ["system_map_loop_configs.id", "system_map_loop_configs.organisation_id"],
            ondelete=_ONDELETE_CASCADE,
            name="fk_system_map_loop_reservations_config",
        ),
    )

    loop_config_id: Mapped[uuid.UUID] = mapped_column(Uuid(), nullable=False, index=True)
    run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(), ForeignKey("runs.id", ondelete=_ONDELETE_RESTRICT), nullable=False, index=True
    )
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    reserved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False
    )
    cooldown_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, server_default="active")
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("accounts.id", ondelete=ONDELETE_SET_NULL), nullable=True, index=True
    )

    loop_config: Mapped[SystemMapLoopConfig] = relationship(back_populates="reservations")
