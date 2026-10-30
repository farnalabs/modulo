"""System Maps repository / CRUD layer (FAR-2236).

Provides typed data access for all System Maps entities with proper
organisation scoping and RLS-compatible patterns.
"""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Select, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.models.system_map import (
    SystemMap,
    SystemMapAttributionBinding,
    SystemMapLoopAdmissionAttempt,
    SystemMapLoopConfig,
    SystemMapLoopReservation,
    SystemMapNode,
    SystemMapRelationship,
    SystemMapRevision,
)
from modulo.db.repositories.base import BaseRepository
from modulo.db.soft_delete import include_soft_deleted

_UPDATABLE_NODE_FIELDS = frozenset(
    {
        "node_key",
        "node_type",
        "display_name",
        "config_json",
        "pipeline_id",
        "connector_instance_id",
        "owner_team_id",
        "eval_id",
        "position_x",
        "position_y",
    }
)


class SystemMapRepository(BaseRepository):  # vulture: ignore
    """Repository for System Maps entities."""

    def apply_tenant_filter(self, stmt: Select[Any], _org_id: uuid.UUID) -> Select[Any]:
        """Rely on PostgreSQL RLS for tenant isolation."""
        return stmt

    # ─── SystemMap CRUD ───

    async def create_map(
        self,
        session: AsyncSession,
        *,
        organisation_id: uuid.UUID,
        name: str,
        description: str | None = None,
        owner_team_id: uuid.UUID | None = None,
        visibility: str = "org",
        default_map: bool = False,
        account_id: uuid.UUID,
    ) -> SystemMap:
        """Create a new system map."""
        if default_map:
            # Ensure no other live default map exists for this org
            existing = await session.execute(
                select(SystemMap).where(
                    SystemMap.organisation_id == organisation_id,
                    SystemMap.default_map.is_(True),
                    SystemMap.deleted_at.is_(None),
                )
            )
            if existing.scalar_one_or_none():
                raise ValueError("A default map already exists for this organisation")

        map_obj = SystemMap(
            organisation_id=organisation_id,
            name=name,
            description=description,
            owner_team_id=owner_team_id,
            visibility=visibility,
            default_map=default_map,
            account_id=account_id,
        )
        session.add(map_obj)
        await session.flush()
        return map_obj

    async def get_map_by_id(
        self,
        session: AsyncSession,
        *,
        organisation_id: uuid.UUID,
        map_id: uuid.UUID,
        include_deleted: bool = False,
    ) -> SystemMap | None:
        """Get a system map by ID."""
        stmt = select(SystemMap).where(
            SystemMap.id == map_id,
            SystemMap.organisation_id == organisation_id,
        )
        stmt = include_soft_deleted(stmt) if include_deleted else stmt.where(SystemMap.deleted_at.is_(None))
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_maps_by_org(
        self,
        session: AsyncSession,
        *,
        organisation_id: uuid.UUID,
        include_deleted: bool = False,
        visibility: str | None = None,
        owner_team_id: uuid.UUID | None = None,
        name: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[SystemMap]:
        """List system maps for an organisation."""
        stmt = select(SystemMap).where(SystemMap.organisation_id == organisation_id)
        stmt = include_soft_deleted(stmt) if include_deleted else stmt.where(SystemMap.deleted_at.is_(None))
        if visibility:
            stmt = stmt.where(SystemMap.visibility == visibility)
        if owner_team_id:
            stmt = stmt.where(SystemMap.owner_team_id == owner_team_id)
        if name:
            stmt = stmt.where(SystemMap.name.ilike(f"%{name}%"))
        stmt = stmt.order_by(SystemMap.created_at.desc()).limit(limit).offset(offset)
        result = await session.execute(stmt)
        return result.scalars().all()

    async def get_default_map(
        self,
        session: AsyncSession,
        *,
        organisation_id: uuid.UUID,
    ) -> SystemMap | None:
        """Get the default map for an organisation."""
        stmt = select(SystemMap).where(
            SystemMap.organisation_id == organisation_id,
            SystemMap.default_map.is_(True),
            SystemMap.deleted_at.is_(None),
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    async def set_default_map(
        self,
        session: AsyncSession,
        *,
        organisation_id: uuid.UUID,
        map_id: uuid.UUID,
    ) -> SystemMap | None:
        """Set a map as the default for its organisation (clears previous default)."""
        # Clear existing default
        await session.execute(
            update(SystemMap)
            .where(
                SystemMap.organisation_id == organisation_id,
                SystemMap.default_map.is_(True),
            )
            .values(default_map=False)
        )
        # Set new default
        map_obj = await self.get_map_by_id(session, organisation_id=organisation_id, map_id=map_id)
        if map_obj:
            map_obj.default_map = True
            await session.flush()
        return map_obj

    async def soft_delete_map(
        self,
        session: AsyncSession,
        *,
        organisation_id: uuid.UUID,
        map_id: uuid.UUID,
        deleted_by: uuid.UUID | None = None,
    ) -> bool:
        """Soft-delete a system map."""
        map_obj = await self.get_map_by_id(session, organisation_id=organisation_id, map_id=map_id)
        if not map_obj:
            return False
        map_obj.deleted_at = datetime.now(UTC)
        if deleted_by:
            map_obj.deleted_by = deleted_by
        await session.flush()
        return True

    # ─── SystemMapRevision CRUD ───

    async def create_revision(
        self,
        session: AsyncSession,
        *,
        organisation_id: uuid.UUID,
        system_map_id: uuid.UUID,
        revision_number: int,
        status: str = "draft",
        content_json: dict[str, Any] | None = None,
    ) -> SystemMapRevision:
        """Create a new revision for a system map."""
        revision = SystemMapRevision(
            organisation_id=organisation_id,
            system_map_id=system_map_id,
            revision_number=revision_number,
            status=status,
            content_json=content_json or {},
        )
        session.add(revision)
        await session.flush()
        return revision

    async def get_revision_by_id(
        self,
        session: AsyncSession,
        *,
        organisation_id: uuid.UUID,
        revision_id: uuid.UUID,
        include_deleted: bool = False,
    ) -> SystemMapRevision | None:
        """Get a revision by ID."""
        stmt = select(SystemMapRevision).where(
            SystemMapRevision.id == revision_id,
            SystemMapRevision.organisation_id == organisation_id,
        )
        stmt = include_soft_deleted(stmt) if include_deleted else stmt.where(SystemMapRevision.deleted_at.is_(None))
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_revision_by_number(
        self,
        session: AsyncSession,
        *,
        organisation_id: uuid.UUID,
        system_map_id: uuid.UUID,
        revision_number: int,
    ) -> SystemMapRevision | None:
        """Get a revision by its number within a map."""
        stmt = select(SystemMapRevision).where(
            SystemMapRevision.system_map_id == system_map_id,
            SystemMapRevision.revision_number == revision_number,
            SystemMapRevision.organisation_id == organisation_id,
            SystemMapRevision.deleted_at.is_(None),
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_published_revision(
        self,
        session: AsyncSession,
        *,
        organisation_id: uuid.UUID,
        system_map_id: uuid.UUID,
    ) -> SystemMapRevision | None:
        """Get the published revision for a system map."""
        stmt = select(SystemMapRevision).where(
            SystemMapRevision.system_map_id == system_map_id,
            SystemMapRevision.status == "published",
            SystemMapRevision.organisation_id == organisation_id,
            SystemMapRevision.deleted_at.is_(None),
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_revisions(
        self,
        session: AsyncSession,
        *,
        organisation_id: uuid.UUID,
        system_map_id: uuid.UUID,
        include_deleted: bool = False,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[SystemMapRevision]:
        """List revisions for a system map."""
        stmt = (
            select(SystemMapRevision)
            .where(
                SystemMapRevision.system_map_id == system_map_id,
                SystemMapRevision.organisation_id == organisation_id,
            )
            .order_by(SystemMapRevision.revision_number.desc())
            .limit(limit)
            .offset(offset)
        )
        stmt = include_soft_deleted(stmt) if include_deleted else stmt.where(SystemMapRevision.deleted_at.is_(None))
        if status:
            stmt = stmt.where(SystemMapRevision.status == status)
        result = await session.execute(stmt)
        return result.scalars().all()

    async def publish_revision(
        self,
        session: AsyncSession,
        *,
        organisation_id: uuid.UUID,
        system_map_id: uuid.UUID,
        revision_id: uuid.UUID,
    ) -> SystemMapRevision | None:
        """Publish a revision (marks as published, updates map's published_revision_id)."""
        # Verify revision belongs to map and org
        revision = await self.get_revision_by_id(session, organisation_id=organisation_id, revision_id=revision_id)
        if not revision or revision.system_map_id != system_map_id:
            return None

        # Archive any currently published revision for this map
        await session.execute(
            update(SystemMapRevision)
            .where(
                SystemMapRevision.system_map_id == system_map_id,
                SystemMapRevision.status == "published",
                SystemMapRevision.organisation_id == organisation_id,
            )
            .values(status="archived")
        )

        # Publish the new revision
        revision.status = "published"
        revision.published_at = datetime.now(UTC)
        await session.flush()

        # Update the map's published_revision_id
        map_obj = await self.get_map_by_id(session, organisation_id=organisation_id, map_id=system_map_id)
        if map_obj:
            map_obj.published_revision_id = revision_id
            await session.flush()

        return revision

    # ─── SystemMapNode CRUD ───

    async def create_node(
        self,
        session: AsyncSession,
        *,
        organisation_id: uuid.UUID,
        revision_id: uuid.UUID,
        node_key: str,
        node_type: str,
        display_name: str,
        config_json: dict[str, Any] | None = None,
        pipeline_id: uuid.UUID | None = None,
        connector_instance_id: uuid.UUID | None = None,
        owner_team_id: uuid.UUID | None = None,
        eval_id: uuid.UUID | None = None,
        position_x: Decimal = Decimal(0),
        position_y: Decimal = Decimal(0),
    ) -> SystemMapNode:
        """Create a node within a revision."""
        node = SystemMapNode(
            organisation_id=organisation_id,
            revision_id=revision_id,
            node_key=node_key,
            node_type=node_type,
            display_name=display_name,
            config_json=config_json or {},
            pipeline_id=pipeline_id,
            connector_instance_id=connector_instance_id,
            owner_team_id=owner_team_id,
            eval_id=eval_id,
            position_x=position_x,
            position_y=position_y,
        )
        session.add(node)
        await session.flush()
        return node

    async def get_node_by_id(
        self,
        session: AsyncSession,
        *,
        organisation_id: uuid.UUID,
        node_id: uuid.UUID,
        include_deleted: bool = False,
    ) -> SystemMapNode | None:
        """Get a node by ID."""
        stmt = select(SystemMapNode).where(
            SystemMapNode.id == node_id,
            SystemMapNode.organisation_id == organisation_id,
        )
        stmt = include_soft_deleted(stmt) if include_deleted else stmt.where(SystemMapNode.deleted_at.is_(None))
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_node_by_key(
        self,
        session: AsyncSession,
        *,
        organisation_id: uuid.UUID,
        revision_id: uuid.UUID,
        node_key: str,
    ) -> SystemMapNode | None:
        """Get a node by its key within a revision."""
        stmt = select(SystemMapNode).where(
            SystemMapNode.revision_id == revision_id,
            SystemMapNode.node_key == node_key,
            SystemMapNode.organisation_id == organisation_id,
            SystemMapNode.deleted_at.is_(None),
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_nodes_in_revision(
        self,
        session: AsyncSession,
        *,
        organisation_id: uuid.UUID,
        revision_id: uuid.UUID,
        include_deleted: bool = False,
    ) -> Sequence[SystemMapNode]:
        """List all nodes in a revision."""
        stmt = (
            select(SystemMapNode)
            .where(
                SystemMapNode.revision_id == revision_id,
                SystemMapNode.organisation_id == organisation_id,
            )
            .order_by(SystemMapNode.node_key)
        )
        stmt = include_soft_deleted(stmt) if include_deleted else stmt.where(SystemMapNode.deleted_at.is_(None))
        result = await session.execute(stmt)
        return result.scalars().all()

    async def update_node(
        self,
        session: AsyncSession,
        *,
        organisation_id: uuid.UUID,
        node_id: uuid.UUID,
        **kwargs: Any,
    ) -> SystemMapNode | None:
        """Update a node's fields.

        Unknown keys raise ``ValueError`` instead of being silently discarded:
        a typo in a caller's kwargs must fail loudly rather than produce a
        no-op update that looks successful.
        """
        unknown = set(kwargs) - _UPDATABLE_NODE_FIELDS
        if unknown:
            raise ValueError(f"Unknown SystemMapNode field(s): {', '.join(sorted(unknown))}")
        node = await self.get_node_by_id(session, organisation_id=organisation_id, node_id=node_id)
        if not node:
            return None
        for key, value in kwargs.items():
            setattr(node, key, value)
        await session.flush()
        return node

    async def delete_node(
        self,
        session: AsyncSession,
        *,
        organisation_id: uuid.UUID,
        node_id: uuid.UUID,
    ) -> bool:
        """Soft-delete a node."""
        node = await self.get_node_by_id(session, organisation_id=organisation_id, node_id=node_id)
        if not node:
            return False
        node.deleted_at = datetime.now(UTC)
        await session.flush()
        return True

    # ─── SystemMapRelationship CRUD ───

    async def create_relationship(
        self,
        session: AsyncSession,
        *,
        organisation_id: uuid.UUID,
        revision_id: uuid.UUID,
        source_node_key: str,
        target_node_key: str,
        relationship_type: str,
        config_json: dict[str, Any] | None = None,
    ) -> SystemMapRelationship:
        """Create a relationship (edge) within a revision."""
        rel = SystemMapRelationship(
            organisation_id=organisation_id,
            revision_id=revision_id,
            source_node_key=source_node_key,
            target_node_key=target_node_key,
            relationship_type=relationship_type,
            config_json=config_json or {},
        )
        session.add(rel)
        await session.flush()
        return rel

    async def list_relationships_in_revision(
        self,
        session: AsyncSession,
        *,
        organisation_id: uuid.UUID,
        revision_id: uuid.UUID,
        include_deleted: bool = False,
    ) -> Sequence[SystemMapRelationship]:
        """List all relationships in a revision."""
        stmt = (
            select(SystemMapRelationship)
            .where(
                SystemMapRelationship.revision_id == revision_id,
                SystemMapRelationship.organisation_id == organisation_id,
            )
            .order_by(SystemMapRelationship.source_node_key, SystemMapRelationship.target_node_key)
        )
        stmt = include_soft_deleted(stmt) if include_deleted else stmt.where(SystemMapRelationship.deleted_at.is_(None))
        result = await session.execute(stmt)
        return result.scalars().all()

    async def delete_relationship(
        self,
        session: AsyncSession,
        *,
        organisation_id: uuid.UUID,
        relationship_id: uuid.UUID,
    ) -> bool:
        """Soft-delete a relationship."""
        stmt = select(SystemMapRelationship).where(
            SystemMapRelationship.id == relationship_id,
            SystemMapRelationship.organisation_id == organisation_id,
        )
        result = await session.execute(stmt)
        rel = result.scalar_one_or_none()
        if not rel:
            return False
        rel.deleted_at = datetime.now(UTC)
        await session.flush()
        return True

    # ─── SystemMapLoopConfig CRUD ───

    async def create_loop_config(
        self,
        session: AsyncSession,
        *,
        organisation_id: uuid.UUID,
        revision_id: uuid.UUID,
        node_key: str,
        target_signal: str,
        observation_window_seconds: int,
        action_pipeline_id: uuid.UUID,
        budget_limit_usd: Decimal,
        cooldown_seconds: int,
        outcome_metric: str,
    ) -> SystemMapLoopConfig:
        """Create a loop config for an improvement_loop node."""
        loop_config = SystemMapLoopConfig(
            organisation_id=organisation_id,
            revision_id=revision_id,
            node_key=node_key,
            target_signal=target_signal,
            observation_window_seconds=observation_window_seconds,
            action_pipeline_id=action_pipeline_id,
            budget_limit_usd=budget_limit_usd,
            cooldown_seconds=cooldown_seconds,
            outcome_metric=outcome_metric,
        )
        session.add(loop_config)
        await session.flush()
        return loop_config

    async def get_loop_config_by_id(
        self,
        session: AsyncSession,
        *,
        organisation_id: uuid.UUID,
        loop_config_id: uuid.UUID,
        include_deleted: bool = False,
    ) -> SystemMapLoopConfig | None:
        """Get a loop config by ID."""
        stmt = select(SystemMapLoopConfig).where(
            SystemMapLoopConfig.id == loop_config_id,
            SystemMapLoopConfig.organisation_id == organisation_id,
        )
        stmt = include_soft_deleted(stmt) if include_deleted else stmt.where(SystemMapLoopConfig.deleted_at.is_(None))
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_loop_config_by_node_key(
        self,
        session: AsyncSession,
        *,
        organisation_id: uuid.UUID,
        revision_id: uuid.UUID,
        node_key: str,
    ) -> SystemMapLoopConfig | None:
        """Get a loop config by its node key within a revision."""
        stmt = select(SystemMapLoopConfig).where(
            SystemMapLoopConfig.revision_id == revision_id,
            SystemMapLoopConfig.node_key == node_key,
            SystemMapLoopConfig.organisation_id == organisation_id,
            SystemMapLoopConfig.deleted_at.is_(None),
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_loop_configs_in_revision(
        self,
        session: AsyncSession,
        *,
        organisation_id: uuid.UUID,
        revision_id: uuid.UUID,
        include_deleted: bool = False,
    ) -> Sequence[SystemMapLoopConfig]:
        """List all loop configs in a revision."""
        stmt = (
            select(SystemMapLoopConfig)
            .where(
                SystemMapLoopConfig.revision_id == revision_id,
                SystemMapLoopConfig.organisation_id == organisation_id,
            )
            .order_by(SystemMapLoopConfig.node_key)
        )
        stmt = include_soft_deleted(stmt) if include_deleted else stmt.where(SystemMapLoopConfig.deleted_at.is_(None))
        result = await session.execute(stmt)
        return result.scalars().all()

    # ─── SystemMapAttributionBinding CRUD ───

    async def create_attribution_binding(
        self,
        session: AsyncSession,
        *,
        organisation_id: uuid.UUID,
        pipeline_id: uuid.UUID,
        system_map_id: uuid.UUID,
        node_key: str,
        binding_kind: str,
        trigger_id: uuid.UUID | None = None,
    ) -> SystemMapAttributionBinding:
        """Create an attribution binding."""
        binding = SystemMapAttributionBinding(
            organisation_id=organisation_id,
            pipeline_id=pipeline_id,
            trigger_id=trigger_id,
            system_map_id=system_map_id,
            node_key=node_key,
            binding_kind=binding_kind,
        )
        session.add(binding)
        await session.flush()
        return binding

    async def get_attribution_binding_by_id(
        self,
        session: AsyncSession,
        *,
        organisation_id: uuid.UUID,
        binding_id: uuid.UUID,
        include_deleted: bool = False,
    ) -> SystemMapAttributionBinding | None:
        """Get an attribution binding by ID."""
        stmt = select(SystemMapAttributionBinding).where(
            SystemMapAttributionBinding.id == binding_id,
            SystemMapAttributionBinding.organisation_id == organisation_id,
        )
        stmt = (
            include_soft_deleted(stmt)
            if include_deleted
            else stmt.where(SystemMapAttributionBinding.deleted_at.is_(None))
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_attribution_bindings(
        self,
        session: AsyncSession,
        *,
        organisation_id: uuid.UUID,
        pipeline_id: uuid.UUID | None = None,
        trigger_id: uuid.UUID | None = None,
        system_map_id: uuid.UUID | None = None,
        binding_kind: str | None = None,
        include_deleted: bool = False,
    ) -> Sequence[SystemMapAttributionBinding]:
        """List attribution bindings with optional filters."""
        stmt = select(SystemMapAttributionBinding).where(SystemMapAttributionBinding.organisation_id == organisation_id)
        stmt = (
            include_soft_deleted(stmt)
            if include_deleted
            else stmt.where(SystemMapAttributionBinding.deleted_at.is_(None))
        )
        if pipeline_id:
            stmt = stmt.where(SystemMapAttributionBinding.pipeline_id == pipeline_id)
        if trigger_id:
            stmt = stmt.where(SystemMapAttributionBinding.trigger_id == trigger_id)
        if system_map_id:
            stmt = stmt.where(SystemMapAttributionBinding.system_map_id == system_map_id)
        if binding_kind:
            stmt = stmt.where(SystemMapAttributionBinding.binding_kind == binding_kind)
        stmt = stmt.order_by(SystemMapAttributionBinding.created_at.desc())
        result = await session.execute(stmt)
        return result.scalars().all()

    async def get_trigger_binding(
        self,
        session: AsyncSession,
        *,
        organisation_id: uuid.UUID,
        pipeline_id: uuid.UUID,
        node_key: str,
    ) -> SystemMapAttributionBinding | None:
        """Get the trigger binding for a specific pipeline and node."""
        stmt = select(SystemMapAttributionBinding).where(
            SystemMapAttributionBinding.organisation_id == organisation_id,
            SystemMapAttributionBinding.pipeline_id == pipeline_id,
            SystemMapAttributionBinding.node_key == node_key,
            SystemMapAttributionBinding.binding_kind == "trigger",
            SystemMapAttributionBinding.deleted_at.is_(None),
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_action_pipeline_binding(
        self,
        session: AsyncSession,
        *,
        organisation_id: uuid.UUID,
        pipeline_id: uuid.UUID,
        node_key: str,
    ) -> SystemMapAttributionBinding | None:
        """Get the action_pipeline binding for a specific pipeline and node."""
        stmt = select(SystemMapAttributionBinding).where(
            SystemMapAttributionBinding.organisation_id == organisation_id,
            SystemMapAttributionBinding.pipeline_id == pipeline_id,
            SystemMapAttributionBinding.node_key == node_key,
            SystemMapAttributionBinding.binding_kind == "action_pipeline",
            SystemMapAttributionBinding.deleted_at.is_(None),
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    async def delete_attribution_binding(
        self,
        session: AsyncSession,
        *,
        organisation_id: uuid.UUID,
        binding_id: uuid.UUID,
    ) -> bool:
        """Soft-delete an attribution binding."""
        binding = await self.get_attribution_binding_by_id(
            session, organisation_id=organisation_id, binding_id=binding_id
        )
        if not binding:
            return False
        binding.deleted_at = datetime.now(UTC)
        await session.flush()
        return True

    # ─── SystemMapLoopAdmissionAttempt CRUD ───

    async def create_admission_attempt(
        self,
        session: AsyncSession,
        *,
        organisation_id: uuid.UUID,
        loop_config_id: uuid.UUID,
        idempotency_key: str,
        status: str = "pending",
    ) -> SystemMapLoopAdmissionAttempt:
        """Create or get an admission attempt (idempotent on idempotency_key).

        The fast path reuses an existing row; concurrent creators race on the
        ``(loop_config_id, idempotency_key)`` unique constraint, so the insert
        runs in a savepoint and an ``IntegrityError`` re-reads the winner
        instead of surfacing a 500. A soft-deleted attempt still occupies the
        unique key, so both reads opt out of the global soft-delete filter.
        """
        existing_attempt = await self._get_admission_attempt(
            session,
            organisation_id=organisation_id,
            loop_config_id=loop_config_id,
            idempotency_key=idempotency_key,
        )
        if existing_attempt:
            return existing_attempt

        attempt = SystemMapLoopAdmissionAttempt(
            organisation_id=organisation_id,
            loop_config_id=loop_config_id,
            idempotency_key=idempotency_key,
            status=status,
        )
        try:
            async with session.begin_nested():
                session.add(attempt)
        except IntegrityError:
            existing_attempt = await self._get_admission_attempt(
                session,
                organisation_id=organisation_id,
                loop_config_id=loop_config_id,
                idempotency_key=idempotency_key,
            )
            if existing_attempt is None:
                raise
            return existing_attempt
        return attempt

    async def _get_admission_attempt(
        self,
        session: AsyncSession,
        *,
        organisation_id: uuid.UUID,
        loop_config_id: uuid.UUID,
        idempotency_key: str,
    ) -> SystemMapLoopAdmissionAttempt | None:
        stmt = include_soft_deleted(
            select(SystemMapLoopAdmissionAttempt).where(
                SystemMapLoopAdmissionAttempt.loop_config_id == loop_config_id,
                SystemMapLoopAdmissionAttempt.idempotency_key == idempotency_key,
                SystemMapLoopAdmissionAttempt.organisation_id == organisation_id,
            )
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    async def update_admission_attempt_status(
        self,
        session: AsyncSession,
        *,
        organisation_id: uuid.UUID,
        attempt_id: uuid.UUID,
        status: str,
    ) -> SystemMapLoopAdmissionAttempt | None:
        """Update an admission attempt's status."""
        stmt = select(SystemMapLoopAdmissionAttempt).where(
            SystemMapLoopAdmissionAttempt.id == attempt_id,
            SystemMapLoopAdmissionAttempt.organisation_id == organisation_id,
        )
        result = await session.execute(stmt)
        attempt = result.scalar_one_or_none()
        if not attempt:
            return None
        attempt.status = status
        await session.flush()
        return attempt

    # ─── SystemMapLoopReservation CRUD ───

    async def create_reservation(
        self,
        session: AsyncSession,
        *,
        organisation_id: uuid.UUID,
        loop_config_id: uuid.UUID,
        run_id: uuid.UUID,
        idempotency_key: str,
        cooldown_until: datetime,
        status: str = "active",
    ) -> SystemMapLoopReservation:
        """Create or get a reservation (idempotent on idempotency_key).

        Concurrent creators race on ``(loop_config_id, idempotency_key)``: the
        insert is wrapped in a savepoint and an ``IntegrityError`` re-reads the
        winning row. Soft-deleted reservations still occupy the unique key, so
        the reads opt out of the global soft-delete filter.
        """
        existing_reservation = await self._get_reservation_by_key(
            session,
            organisation_id=organisation_id,
            loop_config_id=loop_config_id,
            idempotency_key=idempotency_key,
        )
        if existing_reservation:
            return existing_reservation

        reservation = SystemMapLoopReservation(
            organisation_id=organisation_id,
            loop_config_id=loop_config_id,
            run_id=run_id,
            idempotency_key=idempotency_key,
            cooldown_until=cooldown_until,
            status=status,
        )
        try:
            async with session.begin_nested():
                session.add(reservation)
        except IntegrityError:
            existing_reservation = await self._get_reservation_by_key(
                session,
                organisation_id=organisation_id,
                loop_config_id=loop_config_id,
                idempotency_key=idempotency_key,
            )
            if existing_reservation is None:
                raise
            return existing_reservation
        return reservation

    async def _get_reservation_by_key(
        self,
        session: AsyncSession,
        *,
        organisation_id: uuid.UUID,
        loop_config_id: uuid.UUID,
        idempotency_key: str,
    ) -> SystemMapLoopReservation | None:
        stmt = include_soft_deleted(
            select(SystemMapLoopReservation).where(
                SystemMapLoopReservation.loop_config_id == loop_config_id,
                SystemMapLoopReservation.idempotency_key == idempotency_key,
                SystemMapLoopReservation.organisation_id == organisation_id,
            )
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_reservation_by_id(
        self,
        session: AsyncSession,
        *,
        organisation_id: uuid.UUID,
        reservation_id: uuid.UUID,
        include_deleted: bool = False,
    ) -> SystemMapLoopReservation | None:
        """Get a reservation by ID."""
        stmt = select(SystemMapLoopReservation).where(
            SystemMapLoopReservation.id == reservation_id,
            SystemMapLoopReservation.organisation_id == organisation_id,
        )
        stmt = (
            include_soft_deleted(stmt) if include_deleted else stmt.where(SystemMapLoopReservation.deleted_at.is_(None))
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_active_reservation_for_run(
        self,
        session: AsyncSession,
        *,
        organisation_id: uuid.UUID,
        loop_config_id: uuid.UUID,
        run_id: uuid.UUID,
    ) -> SystemMapLoopReservation | None:
        """Get the active reservation for a specific run."""
        stmt = select(SystemMapLoopReservation).where(
            SystemMapLoopReservation.loop_config_id == loop_config_id,
            SystemMapLoopReservation.run_id == run_id,
            SystemMapLoopReservation.organisation_id == organisation_id,
            SystemMapLoopReservation.status == "active",
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_active_reservations(
        self,
        session: AsyncSession,
        *,
        organisation_id: uuid.UUID,
        loop_config_id: uuid.UUID | None = None,
        before: datetime | None = None,
    ) -> Sequence[SystemMapLoopReservation]:
        """List active reservations, optionally filtered by loop_config and cooldown expiry."""
        stmt = select(SystemMapLoopReservation).where(
            SystemMapLoopReservation.organisation_id == organisation_id,
            SystemMapLoopReservation.status == "active",
        )
        if loop_config_id:
            stmt = stmt.where(SystemMapLoopReservation.loop_config_id == loop_config_id)
        if before:
            stmt = stmt.where(SystemMapLoopReservation.cooldown_until <= before)
        result = await session.execute(stmt)
        return result.scalars().all()

    async def update_reservation_status(
        self,
        session: AsyncSession,
        *,
        organisation_id: uuid.UUID,
        reservation_id: uuid.UUID,
        status: str,
        consumed_at: datetime | None = None,
        released_at: datetime | None = None,
    ) -> SystemMapLoopReservation | None:
        """Update a reservation's status and timestamps."""
        stmt = select(SystemMapLoopReservation).where(
            SystemMapLoopReservation.id == reservation_id,
            SystemMapLoopReservation.organisation_id == organisation_id,
        )
        result = await session.execute(stmt)
        reservation = result.scalar_one_or_none()
        if not reservation:
            return None
        reservation.status = status
        if consumed_at:
            reservation.consumed_at = consumed_at
        if released_at:
            reservation.released_at = released_at
        await session.flush()
        return reservation

    # ─── Validation Helpers ───

    async def validate_publish(
        self,
        session: AsyncSession,
        *,
        organisation_id: uuid.UUID,
        revision_id: uuid.UUID,
    ) -> tuple[bool, list[str]]:
        """Validate a revision before publishing.

        Returns (is_valid, list_of_errors). Checks:
        1. Acyclicity of 'flow' edges (DFS on revision's relationships).
        2. Loop config completeness: every 'improvement_loop' node must have a
           system_map_loop_configs row with all six required parameters.
        3. Pipeline binding resolution: all referenced pipelines exist and are same-org.
        """
        errors: list[str] = []

        # 1. Acyclicity check on 'flow' edges
        relationships = await self.list_relationships_in_revision(
            session, organisation_id=organisation_id, revision_id=revision_id
        )
        flow_edges = [r for r in relationships if r.relationship_type == "flow"]
        if not self._is_acyclic(flow_edges):
            errors.append("Revision contains cycles in 'flow' relationships")

        # 2. Loop config completeness
        nodes = await self.list_nodes_in_revision(session, organisation_id=organisation_id, revision_id=revision_id)
        loop_nodes = [n for n in nodes if n.node_type == "improvement_loop"]
        for loop_node in loop_nodes:
            loop_config = await self.get_loop_config_by_node_key(
                session, organisation_id=organisation_id, revision_id=revision_id, node_key=loop_node.node_key
            )
            if not loop_config:
                errors.append(f"Improvement loop node '{loop_node.node_key}' missing loop config")
            else:
                required = (
                    ("target_signal", loop_config.target_signal),
                    ("observation_window_seconds", loop_config.observation_window_seconds),
                    ("action_pipeline_id", loop_config.action_pipeline_id),
                    ("budget_limit_usd", loop_config.budget_limit_usd),
                    ("cooldown_seconds", loop_config.cooldown_seconds),
                    ("outcome_metric", loop_config.outcome_metric),
                )
                missing = [
                    name for name, value in required if value is None or (isinstance(value, str) and not value.strip())
                ]
                if missing:
                    errors.append(f"Loop config for '{loop_node.node_key}' is incomplete: missing {', '.join(missing)}")

        # 3. Pipeline binding resolution
        for node in nodes:
            if node.pipeline_id:
                # Check pipeline exists and is same org
                from modulo.db.models.pipeline import Pipeline

                stmt = select(Pipeline).where(
                    Pipeline.id == node.pipeline_id,
                    Pipeline.organisation_id == organisation_id,
                    Pipeline.deleted_at.is_(None),
                )
                result = await session.execute(stmt)
                pipeline = result.scalar_one_or_none()
                if not pipeline:
                    msg = (
                        f"Node '{node.node_key}' references pipeline {node.pipeline_id} "
                        "which does not exist or is not in the same organisation"
                    )
                    errors.append(msg)

        return len(errors) == 0, errors

    def _is_acyclic(self, flow_edges: list[SystemMapRelationship]) -> bool:
        """Check if the flow graph is acyclic using DFS."""
        # Build adjacency list
        graph: dict[str, list[str]] = {}
        all_nodes: set[str] = set()
        for edge in flow_edges:
            graph.setdefault(edge.source_node_key, []).append(edge.target_node_key)
            all_nodes.add(edge.source_node_key)
            all_nodes.add(edge.target_node_key)

        visited: set[str] = set()
        rec_stack: set[str] = set()

        def dfs(node: str) -> bool:
            visited.add(node)
            rec_stack.add(node)
            for neighbor in graph.get(node, []):
                if neighbor not in visited:
                    if not dfs(neighbor):
                        return False
                elif neighbor in rec_stack:
                    return False
            rec_stack.remove(node)
            return True

        if not all_nodes:
            return True
        return all(not (node not in visited and not dfs(node)) for node in all_nodes)

    async def get_full_revision_graph(
        self,
        session: AsyncSession,
        *,
        organisation_id: uuid.UUID,
        revision_id: uuid.UUID,
    ) -> dict[str, Any] | None:
        """Get a complete revision graph with nodes, relationships, and loop configs."""
        revision = await self.get_revision_by_id(session, organisation_id=organisation_id, revision_id=revision_id)
        if not revision:
            return None

        nodes = await self.list_nodes_in_revision(session, organisation_id=organisation_id, revision_id=revision_id)
        relationships = await self.list_relationships_in_revision(
            session, organisation_id=organisation_id, revision_id=revision_id
        )
        loop_configs = await self.list_loop_configs_in_revision(
            session, organisation_id=organisation_id, revision_id=revision_id
        )

        return {
            "revision": revision,
            "nodes": nodes,
            "relationships": relationships,
            "loop_configs": loop_configs,
        }
