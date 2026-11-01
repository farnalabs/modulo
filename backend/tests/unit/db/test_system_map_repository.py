"""Unit tests for the System Maps repository (FAR-2236).

Most of the repository is exercised against a mocked ``AsyncSession``:
``execute`` returns pre-shaped result objects, so the tests assert the
query/return contract without a live database. Every public method is driven
through both its success and its not-found/error arms so the changed-lines
coverage gate sees the full branch surface.

The overview aggregates are the exception: their correctness lives in the SQL
predicates (canonical failure semantics, the half-open window, benign
exclusion, budget/cooldown evidence), and a mocked session cannot execute a
predicate. Those tests run the real queries against an in-memory SQLite
database with the real models, which is the repo's established pattern for
predicate-level repository coverage.
"""

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Self, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import Table
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from modulo.db.models.base import Base
from modulo.db.models.organisation import Organisation
from modulo.db.models.pipeline import Pipeline
from modulo.db.models.run import Run
from modulo.db.models.run_daily_facts import RunDailyFact
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
from modulo.db.repositories.system_map import SystemMapRepository

_ORG = uuid.UUID("11111111-1111-1111-1111-111111111111")
_MAP = uuid.UUID("22222222-2222-2222-2222-222222222222")
_REV = uuid.UUID("33333333-3333-3333-3333-333333333333")
_NODE = uuid.UUID("44444444-4444-4444-4444-444444444444")
_TEAM = uuid.UUID("55555555-5555-5555-5555-555555555555")
_ACCOUNT = uuid.UUID("66666666-6666-6666-6666-666666666666")
_PIPELINE = uuid.UUID("77777777-7777-7777-7777-777777777777")


class _NestedCtx:
    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False


def _result(
    *,
    one: object = None,
    rows: list | None = None,
    first: object = None,
    iterable: list | None = None,
) -> MagicMock:
    value = MagicMock()
    value.scalar_one_or_none.return_value = one
    scalars = MagicMock()
    scalars.all.return_value = [] if rows is None else rows
    value.scalars.return_value = scalars
    value.first.return_value = first
    value.__iter__ = MagicMock(return_value=iter([] if iterable is None else iterable))
    return value


def _session(*results: MagicMock, add_error: Exception | None = None) -> MagicMock:
    db = MagicMock()
    if results:
        db.execute = AsyncMock(side_effect=list(results))
    else:
        db.execute = AsyncMock(return_value=_result())
    db.flush = AsyncMock()
    db.add = MagicMock(side_effect=add_error) if add_error is not None else MagicMock()
    db.begin_nested = MagicMock(return_value=_NestedCtx())
    return db


def _map(map_id: uuid.UUID = _MAP, **kwargs: object) -> SystemMap:
    fields = {
        "organisation_id": _ORG,
        "name": "Map",
        "account_id": _ACCOUNT,
        "default_map": False,
        "visibility": "org",
    }
    fields.update(kwargs)
    obj = SystemMap(**fields)
    obj.id = map_id
    return obj


def _revision(rev_id: uuid.UUID = _REV, map_id: uuid.UUID = _MAP) -> SystemMapRevision:
    obj = SystemMapRevision(
        organisation_id=_ORG,
        system_map_id=map_id,
        revision_number=1,
        status="draft",
        content_json={},
    )
    obj.id = rev_id
    return obj


def _node(node_key: str = "n1", node_type: str = "pipeline", **kwargs: object) -> SystemMapNode:
    return SystemMapNode(
        organisation_id=_ORG,
        revision_id=_REV,
        node_key=node_key,
        node_type=node_type,
        display_name=node_key,
        **kwargs,
    )


def _relationship(source: str, target: str, relationship_type: str = "flow") -> SystemMapRelationship:
    obj = SystemMapRelationship(
        organisation_id=_ORG,
        revision_id=_REV,
        source_node_key=source,
        target_node_key=target,
        relationship_type=relationship_type,
        config_json={},
    )
    obj.id = uuid.uuid4()
    return obj


def _loop_config(node_key: str = "loop", **kwargs: object) -> SystemMapLoopConfig:
    fields = {
        "organisation_id": _ORG,
        "revision_id": _REV,
        "node_key": node_key,
        "target_signal": "error_rate",
        "observation_window_seconds": 60,
        "action_pipeline_id": _PIPELINE,
        "budget_limit_usd": Decimal(10),
        "cooldown_seconds": 30,
        "outcome_metric": "error_rate",
    }
    fields.update(kwargs)
    return SystemMapLoopConfig(**fields)


@pytest.fixture
def repo() -> SystemMapRepository:
    return SystemMapRepository(session_factory=MagicMock())


class TestSystemMapCrud:
    def test_apply_tenant_filter_is_identity(self, repo: SystemMapRepository) -> None:
        stmt = MagicMock()
        assert repo.apply_tenant_filter(stmt, _ORG) is stmt

    async def test_create_map_rejects_second_default(self, repo: SystemMapRepository) -> None:
        db = _session(_result(one=_map()))
        with pytest.raises(ValueError, match="default map already exists"):
            await repo.create_map(db, organisation_id=_ORG, name="m", default_map=True, account_id=_ACCOUNT)

    async def test_create_map_default_when_none(self, repo: SystemMapRepository) -> None:
        db = _session(_result(one=None))
        created = await repo.create_map(db, organisation_id=_ORG, name="m", default_map=True, account_id=_ACCOUNT)
        assert isinstance(created, SystemMap)
        assert created.default_map is True
        db.add.assert_called_once_with(created)
        db.flush.assert_awaited_once()

    async def test_create_map_plain(self, repo: SystemMapRepository) -> None:
        db = _session()
        created = await repo.create_map(db, organisation_id=_ORG, name="m", account_id=_ACCOUNT)
        assert created.default_map is False
        db.flush.assert_awaited_once()

    async def test_get_map_by_id_includes_and_excludes_deleted(self, repo: SystemMapRepository) -> None:
        map_obj = _map()
        assert (
            await repo.get_map_by_id(
                _session(_result(one=map_obj)), organisation_id=_ORG, map_id=_MAP, include_deleted=True
            )
            is map_obj
        )
        assert await repo.get_map_by_id(_session(_result(one=None)), organisation_id=_ORG, map_id=_MAP) is None

    async def test_list_maps_by_org_filters(self, repo: SystemMapRepository) -> None:
        map_obj = _map()
        db = _session(_result(rows=[map_obj]))
        maps = await repo.list_maps_by_org(
            db,
            organisation_id=_ORG,
            include_deleted=True,
            visibility="org",
            owner_team_id=_TEAM,
            name="ma",
            limit=10,
            offset=5,
        )
        assert maps == [map_obj]

    async def test_list_maps_by_org_unfiltered(self, repo: SystemMapRepository) -> None:
        assert not await repo.list_maps_by_org(_session(_result(rows=[])), organisation_id=_ORG)

    async def test_get_default_map(self, repo: SystemMapRepository) -> None:
        map_obj = _map()
        assert await repo.get_default_map(_session(_result(one=map_obj)), organisation_id=_ORG) is map_obj

    async def test_set_default_map_found(self, repo: SystemMapRepository) -> None:
        map_obj = _map()
        db = _session(_result(), _result(one=map_obj))
        result = await repo.set_default_map(db, organisation_id=_ORG, map_id=_MAP)
        assert result is map_obj
        assert map_obj.default_map is True
        db.flush.assert_awaited_once()

    async def test_set_default_map_missing(self, repo: SystemMapRepository) -> None:
        db = _session(_result(), _result(one=None))
        assert await repo.set_default_map(db, organisation_id=_ORG, map_id=_MAP) is None

    async def test_soft_delete_map_variants(self, repo: SystemMapRepository) -> None:
        assert await repo.soft_delete_map(_session(_result(one=None)), organisation_id=_ORG, map_id=_MAP) is False

        with_deleter = _map()
        assert (
            await repo.soft_delete_map(
                _session(_result(one=with_deleter)), organisation_id=_ORG, map_id=_MAP, deleted_by=_ACCOUNT
            )
            is True
        )
        assert with_deleter.deleted_by == _ACCOUNT
        assert with_deleter.deleted_at is not None

        without_deleter = _map()
        assert (
            await repo.soft_delete_map(_session(_result(one=without_deleter)), organisation_id=_ORG, map_id=_MAP)
            is True
        )


class TestRevisionCrud:
    async def test_create_revision(self, repo: SystemMapRepository) -> None:
        db = _session()
        revision = await repo.create_revision(db, organisation_id=_ORG, system_map_id=_MAP, revision_number=2)
        assert revision.revision_number == 2
        assert revision.status == "draft"
        assert not revision.content_json
        db.flush.assert_awaited_once()

    async def test_create_revision_with_content(self, repo: SystemMapRepository) -> None:
        revision = await repo.create_revision(
            _session(),
            organisation_id=_ORG,
            system_map_id=_MAP,
            revision_number=3,
            status="published",
            content_json={"a": 1},
        )
        assert revision.content_json == {"a": 1}

    async def test_get_revision_by_id_variants(self, repo: SystemMapRepository) -> None:
        revision = _revision()
        assert (
            await repo.get_revision_by_id(
                _session(_result(one=revision)), organisation_id=_ORG, revision_id=_REV, include_deleted=True
            )
            is revision
        )
        assert (
            await repo.get_revision_by_id(_session(_result(one=None)), organisation_id=_ORG, revision_id=_REV) is None
        )

    async def test_get_revision_by_number_and_published(self, repo: SystemMapRepository) -> None:
        revision = _revision()
        assert (
            await repo.get_revision_by_number(
                _session(_result(one=revision)), organisation_id=_ORG, system_map_id=_MAP, revision_number=1
            )
            is revision
        )
        assert (
            await repo.get_published_revision(_session(_result(one=revision)), organisation_id=_ORG, system_map_id=_MAP)
            is revision
        )

    async def test_list_revisions_filters(self, repo: SystemMapRepository) -> None:
        revision = _revision()
        db = _session(_result(rows=[revision]))
        assert await repo.list_revisions(
            db, organisation_id=_ORG, system_map_id=_MAP, include_deleted=True, status="draft"
        ) == [revision]
        assert not await repo.list_revisions(_session(_result(rows=[])), organisation_id=_ORG, system_map_id=_MAP)

    async def test_publish_revision_missing_or_foreign(self, repo: SystemMapRepository) -> None:
        assert (
            await repo.publish_revision(
                _session(_result(one=None)), organisation_id=_ORG, system_map_id=_MAP, revision_id=_REV
            )
            is None
        )

        foreign = _revision(map_id=uuid.uuid4())
        assert (
            await repo.publish_revision(
                _session(_result(one=foreign)), organisation_id=_ORG, system_map_id=_MAP, revision_id=_REV
            )
            is None
        )

    async def test_publish_revision_updates_map(self, repo: SystemMapRepository) -> None:
        revision = _revision()
        map_obj = _map()
        db = _session(_result(one=revision), _result(), _result(one=map_obj))
        result = await repo.publish_revision(db, organisation_id=_ORG, system_map_id=_MAP, revision_id=_REV)
        assert result is revision
        assert revision.status == "published"
        assert revision.published_at is not None
        assert map_obj.published_revision_id == _REV
        assert db.flush.await_count == 2

    async def test_publish_revision_without_map_row(self, repo: SystemMapRepository) -> None:
        revision = _revision()
        db = _session(_result(one=revision), _result(), _result(one=None))
        assert await repo.publish_revision(db, organisation_id=_ORG, system_map_id=_MAP, revision_id=_REV) is revision


class TestNodeAndRelationshipCrud:
    async def test_create_node(self, repo: SystemMapRepository) -> None:
        db = _session()
        node = await repo.create_node(
            db,
            organisation_id=_ORG,
            revision_id=_REV,
            node_key="n1",
            node_type="pipeline",
            display_name="Node",
            pipeline_id=_PIPELINE,
            position_x=Decimal(1),
            position_y=Decimal(2),
        )
        assert node.node_key == "n1"
        assert not node.config_json
        assert node.position_x == Decimal(1)
        db.flush.assert_awaited_once()

    async def test_get_node_variants(self, repo: SystemMapRepository) -> None:
        node = _node()
        assert (
            await repo.get_node_by_id(
                _session(_result(one=node)), organisation_id=_ORG, node_id=_NODE, include_deleted=True
            )
            is node
        )
        assert await repo.get_node_by_id(_session(_result(one=None)), organisation_id=_ORG, node_id=_NODE) is None
        assert (
            await repo.get_node_by_key(
                _session(_result(one=node)), organisation_id=_ORG, revision_id=_REV, node_key="n1"
            )
            is node
        )
        assert (
            await repo.get_node_by_map_revision_key(
                _session(_result(one=node)),
                organisation_id=_ORG,
                system_map_id=_MAP,
                revision_id=_REV,
                node_key="n1",
            )
            is node
        )

    async def test_list_nodes_in_revision(self, repo: SystemMapRepository) -> None:
        node = _node()
        assert await repo.list_nodes_in_revision(
            _session(_result(rows=[node])), organisation_id=_ORG, revision_id=_REV, include_deleted=True
        ) == [node]
        assert not (
            await repo.list_nodes_in_revision(_session(_result(rows=[])), organisation_id=_ORG, revision_id=_REV)
        )

    async def test_update_node(self, repo: SystemMapRepository) -> None:
        with pytest.raises(ValueError, match="Unknown SystemMapNode field"):
            await repo.update_node(_session(), organisation_id=_ORG, node_id=_NODE, bogus=1)

        assert await repo.update_node(_session(_result(one=None)), organisation_id=_ORG, node_id=_NODE) is None

        node = _node()
        db = _session(_result(one=node))
        updated = await repo.update_node(db, organisation_id=_ORG, node_id=_NODE, display_name="Renamed")
        assert updated is node
        assert node.display_name == "Renamed"
        db.flush.assert_awaited_once()

    async def test_delete_node_variants(self, repo: SystemMapRepository) -> None:
        assert await repo.delete_node(_session(_result(one=None)), organisation_id=_ORG, node_id=_NODE) is False

        node = _node()
        assert (
            await repo.delete_node(
                _session(_result(one=node)), organisation_id=_ORG, node_id=_NODE, deleted_by=_ACCOUNT
            )
            is True
        )
        assert node.deleted_by == _ACCOUNT

        plain = _node()
        assert await repo.delete_node(_session(_result(one=plain)), organisation_id=_ORG, node_id=_NODE) is True

    async def test_create_relationship(self, repo: SystemMapRepository) -> None:
        db = _session()
        rel = await repo.create_relationship(
            db,
            organisation_id=_ORG,
            revision_id=_REV,
            source_node_key="a",
            target_node_key="b",
            relationship_type="flow",
        )
        assert rel.relationship_type == "flow"
        assert not rel.config_json
        db.flush.assert_awaited_once()

    async def test_list_relationships_in_revision(self, repo: SystemMapRepository) -> None:
        rel = _relationship("a", "b")
        assert await repo.list_relationships_in_revision(
            _session(_result(rows=[rel])), organisation_id=_ORG, revision_id=_REV, include_deleted=True
        ) == [rel]
        assert not (
            await repo.list_relationships_in_revision(
                _session(_result(rows=[])), organisation_id=_ORG, revision_id=_REV
            )
        )

    async def test_delete_relationship_variants(self, repo: SystemMapRepository) -> None:
        assert (
            await repo.delete_relationship(_session(_result(one=None)), organisation_id=_ORG, relationship_id=_NODE)
            is False
        )

        rel = _relationship("a", "b")
        assert (
            await repo.delete_relationship(
                _session(_result(one=rel)), organisation_id=_ORG, relationship_id=_NODE, deleted_by=_ACCOUNT
            )
            is True
        )
        assert rel.deleted_by == _ACCOUNT

        plain = _relationship("a", "b")
        assert await repo.delete_relationship(_session(_result(one=plain)), organisation_id=_ORG, relationship_id=_NODE)


class TestLoopConfigAndBindingCrud:
    async def test_create_loop_config(self, repo: SystemMapRepository) -> None:
        db = _session()
        loop = await repo.create_loop_config(
            db,
            organisation_id=_ORG,
            revision_id=_REV,
            node_key="loop",
            target_signal="error_rate",
            observation_window_seconds=60,
            action_pipeline_id=_PIPELINE,
            budget_limit_usd=Decimal(5),
            cooldown_seconds=30,
            outcome_metric="error_rate",
        )
        assert loop.budget_limit_usd == Decimal(5)
        db.flush.assert_awaited_once()

    async def test_get_loop_config_variants(self, repo: SystemMapRepository) -> None:
        loop = _loop_config()
        assert (
            await repo.get_loop_config_by_id(
                _session(_result(one=loop)), organisation_id=_ORG, loop_config_id=_NODE, include_deleted=True
            )
            is loop
        )
        assert (
            await repo.get_loop_config_by_id(_session(_result(one=None)), organisation_id=_ORG, loop_config_id=_NODE)
            is None
        )
        assert (
            await repo.get_loop_config_by_node_key(
                _session(_result(one=loop)), organisation_id=_ORG, revision_id=_REV, node_key="loop"
            )
            is loop
        )

    async def test_list_loop_configs_in_revision(self, repo: SystemMapRepository) -> None:
        loop = _loop_config()
        assert await repo.list_loop_configs_in_revision(
            _session(_result(rows=[loop])), organisation_id=_ORG, revision_id=_REV, include_deleted=True
        ) == [loop]
        assert not (
            await repo.list_loop_configs_in_revision(_session(_result(rows=[])), organisation_id=_ORG, revision_id=_REV)
        )

    async def test_create_attribution_binding(self, repo: SystemMapRepository) -> None:
        db = _session()
        binding = await repo.create_attribution_binding(
            db,
            organisation_id=_ORG,
            pipeline_id=_PIPELINE,
            system_map_id=_MAP,
            node_key="n1",
            binding_kind="trigger",
        )
        assert binding.binding_kind == "trigger"
        assert binding.trigger_id is None
        db.flush.assert_awaited_once()

    async def test_get_attribution_binding_variants(self, repo: SystemMapRepository) -> None:
        binding = SystemMapAttributionBinding(
            organisation_id=_ORG, pipeline_id=_PIPELINE, system_map_id=_MAP, node_key="n1", binding_kind="trigger"
        )
        assert (
            await repo.get_attribution_binding_by_id(
                _session(_result(one=binding)), organisation_id=_ORG, binding_id=_NODE, include_deleted=True
            )
            is binding
        )
        assert (
            await repo.get_attribution_binding_by_id(
                _session(_result(one=None)), organisation_id=_ORG, binding_id=_NODE
            )
            is None
        )

    async def test_list_attribution_bindings_filters(self, repo: SystemMapRepository) -> None:
        binding = SystemMapAttributionBinding(
            organisation_id=_ORG, pipeline_id=_PIPELINE, system_map_id=_MAP, node_key="n1", binding_kind="trigger"
        )
        db = _session(_result(rows=[binding]))
        result = await repo.list_attribution_bindings(
            db,
            organisation_id=_ORG,
            pipeline_id=_PIPELINE,
            trigger_id=_REV,
            system_map_id=_MAP,
            binding_kind="trigger",
            include_deleted=True,
        )
        assert result == [binding]
        assert not await repo.list_attribution_bindings(_session(_result(rows=[])), organisation_id=_ORG)

    async def test_get_trigger_and_action_bindings(self, repo: SystemMapRepository) -> None:
        binding = SystemMapAttributionBinding(
            organisation_id=_ORG, pipeline_id=_PIPELINE, system_map_id=_MAP, node_key="n1", binding_kind="trigger"
        )
        assert (
            await repo.get_trigger_binding(
                _session(_result(one=binding)), organisation_id=_ORG, pipeline_id=_PIPELINE, node_key="n1"
            )
            is binding
        )
        assert (
            await repo.get_action_pipeline_binding(
                _session(_result(one=binding)), organisation_id=_ORG, pipeline_id=_PIPELINE, node_key="n1"
            )
            is binding
        )

    async def test_delete_attribution_binding_variants(self, repo: SystemMapRepository) -> None:
        assert (
            await repo.delete_attribution_binding(_session(_result(one=None)), organisation_id=_ORG, binding_id=_NODE)
            is False
        )

        binding = SystemMapAttributionBinding(
            organisation_id=_ORG, pipeline_id=_PIPELINE, system_map_id=_MAP, node_key="n1", binding_kind="trigger"
        )
        assert (
            await repo.delete_attribution_binding(
                _session(_result(one=binding)), organisation_id=_ORG, binding_id=_NODE, deleted_by=_ACCOUNT
            )
            is True
        )
        assert binding.deleted_by == _ACCOUNT

        plain = SystemMapAttributionBinding(
            organisation_id=_ORG, pipeline_id=_PIPELINE, system_map_id=_MAP, node_key="n1", binding_kind="trigger"
        )
        assert await repo.delete_attribution_binding(
            _session(_result(one=plain)), organisation_id=_ORG, binding_id=_NODE
        )


class TestAdmissionAttemptCrud:
    async def test_create_attempt_reuses_existing(self, repo: SystemMapRepository) -> None:
        existing = SystemMapLoopAdmissionAttempt(
            organisation_id=_ORG, loop_config_id=_NODE, idempotency_key="k", status="pending"
        )
        db = _session(_result(one=existing))
        assert (
            await repo.create_admission_attempt(db, organisation_id=_ORG, loop_config_id=_NODE, idempotency_key="k")
            is existing
        )
        db.add.assert_not_called()

    async def test_create_attempt_inserts(self, repo: SystemMapRepository) -> None:
        db = _session(_result(one=None))
        attempt = await repo.create_admission_attempt(
            db, organisation_id=_ORG, loop_config_id=_NODE, idempotency_key="k", status="admitted"
        )
        assert attempt.status == "admitted"
        db.add.assert_called_once_with(attempt)

    async def test_create_attempt_integrity_race_returns_winner(self, repo: SystemMapRepository) -> None:
        winner = SystemMapLoopAdmissionAttempt(
            organisation_id=_ORG, loop_config_id=_NODE, idempotency_key="k", status="pending"
        )
        db = _session(_result(one=None), _result(one=winner), add_error=IntegrityError("s", {}, Exception("dup")))
        assert (
            await repo.create_admission_attempt(db, organisation_id=_ORG, loop_config_id=_NODE, idempotency_key="k")
            is winner
        )

    async def test_create_attempt_integrity_race_without_winner_reraises(self, repo: SystemMapRepository) -> None:
        db = _session(_result(one=None), _result(one=None), add_error=IntegrityError("s", {}, Exception("dup")))
        with pytest.raises(IntegrityError):
            await repo.create_admission_attempt(db, organisation_id=_ORG, loop_config_id=_NODE, idempotency_key="k")

    async def test_update_attempt_status(self, repo: SystemMapRepository) -> None:
        assert (
            await repo.update_admission_attempt_status(
                _session(_result(one=None)), organisation_id=_ORG, attempt_id=_NODE, status="rejected"
            )
            is None
        )

        attempt = SystemMapLoopAdmissionAttempt(
            organisation_id=_ORG, loop_config_id=_NODE, idempotency_key="k", status="pending"
        )
        db = _session(_result(one=attempt))
        updated = await repo.update_admission_attempt_status(
            db, organisation_id=_ORG, attempt_id=_NODE, status="rejected"
        )
        assert updated is attempt
        assert attempt.status == "rejected"
        db.flush.assert_awaited_once()


class TestReservationCrud:
    async def test_create_reservation_reuses_existing(self, repo: SystemMapRepository) -> None:
        existing = SystemMapLoopReservation(
            organisation_id=_ORG,
            loop_config_id=_NODE,
            run_id=_REV,
            idempotency_key="k",
            cooldown_until=datetime.now(UTC),
        )
        db = _session(_result(one=existing))
        assert (
            await repo.create_reservation(
                db,
                organisation_id=_ORG,
                loop_config_id=_NODE,
                run_id=_REV,
                idempotency_key="k",
                cooldown_until=datetime.now(UTC),
            )
            is existing
        )
        db.add.assert_not_called()

    async def test_create_reservation_inserts(self, repo: SystemMapRepository) -> None:
        db = _session(_result(one=None))
        reservation = await repo.create_reservation(
            db,
            organisation_id=_ORG,
            loop_config_id=_NODE,
            run_id=_REV,
            idempotency_key="k",
            cooldown_until=datetime.now(UTC),
        )
        assert reservation.status == "active"
        db.add.assert_called_once_with(reservation)

    async def test_create_reservation_integrity_race_returns_winner(self, repo: SystemMapRepository) -> None:
        winner = SystemMapLoopReservation(
            organisation_id=_ORG,
            loop_config_id=_NODE,
            run_id=_REV,
            idempotency_key="k",
            cooldown_until=datetime.now(UTC),
        )
        db = _session(_result(one=None), _result(one=winner), add_error=IntegrityError("s", {}, Exception("dup")))
        assert (
            await repo.create_reservation(
                db,
                organisation_id=_ORG,
                loop_config_id=_NODE,
                run_id=_REV,
                idempotency_key="k",
                cooldown_until=datetime.now(UTC),
            )
            is winner
        )

    async def test_create_reservation_integrity_race_without_winner_reraises(self, repo: SystemMapRepository) -> None:
        db = _session(_result(one=None), _result(one=None), add_error=IntegrityError("s", {}, Exception("dup")))
        with pytest.raises(IntegrityError):
            await repo.create_reservation(
                db,
                organisation_id=_ORG,
                loop_config_id=_NODE,
                run_id=_REV,
                idempotency_key="k",
                cooldown_until=datetime.now(UTC),
            )

    async def test_get_reservation_by_id_variants(self, repo: SystemMapRepository) -> None:
        reservation = SystemMapLoopReservation(
            organisation_id=_ORG,
            loop_config_id=_NODE,
            run_id=_REV,
            idempotency_key="k",
            cooldown_until=datetime.now(UTC),
        )
        assert (
            await repo.get_reservation_by_id(
                _session(_result(one=reservation)), organisation_id=_ORG, reservation_id=_NODE, include_deleted=True
            )
            is reservation
        )
        assert (
            await repo.get_reservation_by_id(_session(_result(one=None)), organisation_id=_ORG, reservation_id=_NODE)
            is None
        )

    async def test_get_active_reservation_for_run(self, repo: SystemMapRepository) -> None:
        reservation = SystemMapLoopReservation(
            organisation_id=_ORG,
            loop_config_id=_NODE,
            run_id=_REV,
            idempotency_key="k",
            cooldown_until=datetime.now(UTC),
        )
        assert (
            await repo.get_active_reservation_for_run(
                _session(_result(one=reservation)), organisation_id=_ORG, loop_config_id=_NODE, run_id=_REV
            )
            is reservation
        )

    async def test_list_active_reservations_filters(self, repo: SystemMapRepository) -> None:
        reservation = SystemMapLoopReservation(
            organisation_id=_ORG,
            loop_config_id=_NODE,
            run_id=_REV,
            idempotency_key="k",
            cooldown_until=datetime.now(UTC),
        )
        db = _session(_result(rows=[reservation]))
        assert await repo.list_active_reservations(
            db, organisation_id=_ORG, loop_config_id=_NODE, before=datetime.now(UTC)
        ) == [reservation]
        assert not await repo.list_active_reservations(_session(_result(rows=[])), organisation_id=_ORG)

    async def test_update_reservation_status_variants(self, repo: SystemMapRepository) -> None:
        assert (
            await repo.update_reservation_status(
                _session(_result(one=None)), organisation_id=_ORG, reservation_id=_NODE, status="consumed"
            )
            is None
        )

        reservation = SystemMapLoopReservation(
            organisation_id=_ORG,
            loop_config_id=_NODE,
            run_id=_REV,
            idempotency_key="k",
            cooldown_until=datetime.now(UTC),
        )
        now = datetime.now(UTC)
        db = _session(_result(one=reservation))
        updated = await repo.update_reservation_status(
            db,
            organisation_id=_ORG,
            reservation_id=_NODE,
            status="consumed",
            consumed_at=now,
            released_at=now,
        )
        assert updated is reservation
        assert reservation.status == "consumed"
        assert reservation.consumed_at == now
        assert reservation.released_at == now

        plain = SystemMapLoopReservation(
            organisation_id=_ORG,
            loop_config_id=_NODE,
            run_id=_REV,
            idempotency_key="k",
            cooldown_until=datetime.now(UTC),
        )
        assert (
            await repo.update_reservation_status(
                _session(_result(one=plain)), organisation_id=_ORG, reservation_id=_NODE, status="released"
            )
            is plain
        )


class TestValidateTeamBinding:
    async def test_missing_team(self, repo: SystemMapRepository) -> None:
        db = _session(_result(first=None))
        error = await repo._validate_team_binding(
            db, organisation_id=_ORG, account_id=_ACCOUNT, org_role="member", team_id=_TEAM
        )
        assert error is not None
        assert "does not exist" in error

    async def test_admin_skips_membership(self, repo: SystemMapRepository) -> None:
        db = _session(_result(first=SimpleNamespace(id=_TEAM)))
        assert (
            await repo._validate_team_binding(
                db, organisation_id=_ORG, account_id=_ACCOUNT, org_role="admin", team_id=_TEAM
            )
            is None
        )
        assert db.execute.await_count == 1

    async def test_member_with_membership(self, repo: SystemMapRepository) -> None:
        db = _session(_result(first=SimpleNamespace(id=_TEAM)), _result(first=SimpleNamespace(id=_TEAM)))
        assert (
            await repo._validate_team_binding(
                db, organisation_id=_ORG, account_id=_ACCOUNT, org_role="member", team_id=_TEAM
            )
            is None
        )

    async def test_member_without_account(self, repo: SystemMapRepository) -> None:
        db = _session(_result(first=SimpleNamespace(id=_TEAM)), _result(first=None))
        error = await repo._validate_team_binding(
            db, organisation_id=_ORG, account_id=None, org_role="member", team_id=_TEAM
        )
        assert error is not None
        assert "not available" in error

    async def test_member_without_membership(self, repo: SystemMapRepository) -> None:
        db = _session(_result(first=SimpleNamespace(id=_TEAM)), _result(first=None))
        error = await repo._validate_team_binding(
            db, organisation_id=_ORG, account_id=_ACCOUNT, org_role="member", team_id=_TEAM
        )
        assert error is not None
        assert "not available" in error


class TestValidateBindings:
    async def test_empty_bindings(self, repo: SystemMapRepository) -> None:
        assert not (
            await repo.validate_bindings(
                _session(),
                organisation_id=_ORG,
                account_id=_ACCOUNT,
                org_role="admin",
                node_bindings=[],
                action_pipeline_ids=[],
            )
        )

    async def test_missing_pipeline(self, repo: SystemMapRepository) -> None:
        db = _session(_result(rows=[]))
        errors = await repo.validate_bindings(
            db,
            organisation_id=_ORG,
            account_id=_ACCOUNT,
            org_role="admin",
            node_bindings=[{"pipeline_id": _PIPELINE}],
            action_pipeline_ids=[],
        )
        assert any("does not exist" in e for e in errors)

    async def test_pipeline_org_scoped_team_binding(self, repo: SystemMapRepository) -> None:
        pipeline = SimpleNamespace(id=_PIPELINE, visibility="team", owner_team_id=_TEAM)
        db = _session(_result(rows=[pipeline]), _result(first=None))
        errors = await repo.validate_bindings(
            db,
            organisation_id=_ORG,
            account_id=_ACCOUNT,
            org_role="admin",
            node_bindings=[{"pipeline_id": _PIPELINE}],
            action_pipeline_ids=[],
        )
        assert any("Pipeline" in e and "not available" in e for e in errors)

    async def test_pipeline_non_team_is_accepted(self, repo: SystemMapRepository) -> None:
        pipeline = SimpleNamespace(id=_PIPELINE, visibility="org", owner_team_id=None)
        db = _session(_result(rows=[pipeline]))
        assert not (
            await repo.validate_bindings(
                db,
                organisation_id=_ORG,
                account_id=_ACCOUNT,
                org_role="admin",
                node_bindings=[{"pipeline_id": _PIPELINE}],
                action_pipeline_ids=[],
            )
        )

    async def test_missing_connector(self, repo: SystemMapRepository) -> None:
        db = _session(_result(rows=[]))
        errors = await repo.validate_bindings(
            db,
            organisation_id=_ORG,
            account_id=_ACCOUNT,
            org_role="admin",
            node_bindings=[{"connector_instance_id": _NODE}],
            action_pipeline_ids=[],
        )
        assert any("connector_instance_id" in e for e in errors)

    async def test_connector_team_binding(self, repo: SystemMapRepository) -> None:
        connector = SimpleNamespace(id=_NODE, visibility="team", owner_team_id=_TEAM)
        db = _session(_result(rows=[connector]), _result(first=None))
        errors = await repo.validate_bindings(
            db,
            organisation_id=_ORG,
            account_id=_ACCOUNT,
            org_role="admin",
            node_bindings=[{"connector_instance_id": _NODE}],
            action_pipeline_ids=[],
        )
        assert any("Connector" in e for e in errors)

    async def test_connector_non_team_is_accepted(self, repo: SystemMapRepository) -> None:
        connector = SimpleNamespace(id=_NODE, visibility="org", owner_team_id=None)
        db = _session(_result(rows=[connector]))
        assert not (
            await repo.validate_bindings(
                db,
                organisation_id=_ORG,
                account_id=_ACCOUNT,
                org_role="admin",
                node_bindings=[{"connector_instance_id": _NODE}],
                action_pipeline_ids=[],
            )
        )

    async def test_missing_eval(self, repo: SystemMapRepository) -> None:
        db = _session(_result(iterable=[]), _result(rows=[]))
        errors = await repo.validate_bindings(
            db,
            organisation_id=_ORG,
            account_id=_ACCOUNT,
            org_role="admin",
            node_bindings=[{"eval_id": _NODE}],
            action_pipeline_ids=[],
        )
        assert any("eval_id" in e for e in errors)

    async def test_eval_outside_organisation(self, repo: SystemMapRepository) -> None:
        db = _session(
            _result(iterable=[SimpleNamespace(id=_NODE, pipeline_id=_PIPELINE)]),
            _result(rows=[]),
        )
        errors = await repo.validate_bindings(
            db,
            organisation_id=_ORG,
            account_id=_ACCOUNT,
            org_role="admin",
            node_bindings=[{"eval_id": _NODE}],
            action_pipeline_ids=[],
        )
        assert any("outside this organisation" in e for e in errors)

    async def test_eval_pipeline_team_binding(self, repo: SystemMapRepository) -> None:
        db = _session(
            _result(iterable=[SimpleNamespace(id=_NODE, pipeline_id=_PIPELINE)]),
            _result(rows=[SimpleNamespace(id=_PIPELINE, visibility="team", owner_team_id=_TEAM)]),
            _result(first=None),
        )
        errors = await repo.validate_bindings(
            db,
            organisation_id=_ORG,
            account_id=_ACCOUNT,
            org_role="admin",
            node_bindings=[{"eval_id": _NODE}],
            action_pipeline_ids=[],
        )
        assert any("Eval pipeline" in e for e in errors)

    async def test_eval_pipeline_non_team_is_accepted(self, repo: SystemMapRepository) -> None:
        db = _session(
            _result(iterable=[SimpleNamespace(id=_NODE, pipeline_id=_PIPELINE)]),
            _result(rows=[SimpleNamespace(id=_PIPELINE, visibility="org", owner_team_id=None)]),
        )
        assert not (
            await repo.validate_bindings(
                db,
                organisation_id=_ORG,
                account_id=_ACCOUNT,
                org_role="admin",
                node_bindings=[{"eval_id": _NODE}],
                action_pipeline_ids=[],
            )
        )

    async def test_team_bound_pipeline_connector_and_eval_are_accepted(self, repo: SystemMapRepository) -> None:
        team_row = _result(first=SimpleNamespace(id=_TEAM))
        pipeline = SimpleNamespace(id=_PIPELINE, visibility="team", owner_team_id=_TEAM)
        connector = SimpleNamespace(id=_NODE, visibility="team", owner_team_id=_TEAM)

        pipeline_db = _session(_result(rows=[pipeline]), team_row)
        assert not (
            await repo.validate_bindings(
                pipeline_db,
                organisation_id=_ORG,
                account_id=_ACCOUNT,
                org_role="admin",
                node_bindings=[{"pipeline_id": _PIPELINE}],
                action_pipeline_ids=[],
            )
        )

        connector_db = _session(_result(rows=[connector]), team_row)
        assert not (
            await repo.validate_bindings(
                connector_db,
                organisation_id=_ORG,
                account_id=_ACCOUNT,
                org_role="admin",
                node_bindings=[{"connector_instance_id": _NODE}],
                action_pipeline_ids=[],
            )
        )

        eval_db = _session(
            _result(iterable=[SimpleNamespace(id=_NODE, pipeline_id=_PIPELINE)]),
            _result(rows=[SimpleNamespace(id=_PIPELINE, visibility="team", owner_team_id=_TEAM)]),
            team_row,
        )
        assert not (
            await repo.validate_bindings(
                eval_db,
                organisation_id=_ORG,
                account_id=_ACCOUNT,
                org_role="admin",
                node_bindings=[{"eval_id": _NODE}],
                action_pipeline_ids=[],
            )
        )

        owner_db = _session(team_row)
        assert not (
            await repo.validate_bindings(
                owner_db,
                organisation_id=_ORG,
                account_id=_ACCOUNT,
                org_role="admin",
                node_bindings=[{"owner_team_id": _TEAM}],
                action_pipeline_ids=[],
            )
        )

    async def test_node_owner_team_error(self, repo: SystemMapRepository) -> None:
        db = _session(_result(first=None))
        errors = await repo.validate_bindings(
            db,
            organisation_id=_ORG,
            account_id=_ACCOUNT,
            org_role="admin",
            node_bindings=[{"owner_team_id": _TEAM}],
            action_pipeline_ids=[],
        )
        assert any("does not exist" in e for e in errors)


class TestValidatePublish:
    async def test_clean_revision_publishes(self, repo: SystemMapRepository) -> None:
        repo.list_nodes_in_revision = AsyncMock(return_value=[_node("a"), _node("b")])  # type: ignore[method-assign]
        repo.list_relationships_in_revision = AsyncMock(return_value=[_relationship("a", "b")])  # type: ignore[method-assign]
        repo.list_loop_configs_in_revision = AsyncMock(return_value=[])  # type: ignore[method-assign]
        repo.validate_bindings = AsyncMock(return_value=[])  # type: ignore[method-assign]
        valid, errors = await repo.validate_publish(_session(), organisation_id=_ORG, revision_id=_REV)
        assert valid is True
        assert errors == []

    async def test_missing_node_reference_and_cycle(self, repo: SystemMapRepository) -> None:
        repo.list_nodes_in_revision = AsyncMock(return_value=[_node("a"), _node("b")])  # type: ignore[method-assign]
        repo.list_relationships_in_revision = AsyncMock(  # type: ignore[method-assign]
            return_value=[_relationship("a", "missing"), _relationship("a", "b"), _relationship("b", "a")]
        )
        repo.list_loop_configs_in_revision = AsyncMock(return_value=[])  # type: ignore[method-assign]
        repo.validate_bindings = AsyncMock(return_value=[])  # type: ignore[method-assign]
        valid, errors = await repo.validate_publish(_session(), organisation_id=_ORG, revision_id=_REV)
        assert valid is False
        assert any("references missing node" in e for e in errors)
        assert any("cycles" in e for e in errors)

    async def test_loop_config_problems(self, repo: SystemMapRepository) -> None:
        repo.list_nodes_in_revision = AsyncMock(  # type: ignore[method-assign]
            return_value=[_node("loop", "improvement_loop"), _node("plain", "pipeline")]
        )
        repo.list_relationships_in_revision = AsyncMock(return_value=[])  # type: ignore[method-assign]
        repo.list_loop_configs_in_revision = AsyncMock(  # type: ignore[method-assign]
            return_value=[
                _loop_config("loop"),
                _loop_config("loop"),
                _loop_config("ghost"),
                _loop_config("plain"),
            ]
        )
        repo.get_loop_config_by_node_key = AsyncMock(return_value=None)  # type: ignore[method-assign]
        repo.validate_bindings = AsyncMock(return_value=[])  # type: ignore[method-assign]
        valid, errors = await repo.validate_publish(_session(), organisation_id=_ORG, revision_id=_REV)
        assert valid is False
        assert any("duplicate node keys" in e for e in errors)
        assert any("references missing node" in e for e in errors)
        assert any("does not target an improvement_loop node" in e for e in errors)
        assert any("missing loop config" in e for e in errors)

    async def test_incomplete_loop_config(self, repo: SystemMapRepository) -> None:
        repo.list_nodes_in_revision = AsyncMock(return_value=[_node("loop", "improvement_loop")])  # type: ignore[method-assign]
        repo.list_relationships_in_revision = AsyncMock(return_value=[])  # type: ignore[method-assign]
        repo.list_loop_configs_in_revision = AsyncMock(return_value=[])  # type: ignore[method-assign]
        incomplete = _loop_config("loop", target_signal=None, budget_limit_usd=None)
        repo.get_loop_config_by_node_key = AsyncMock(return_value=incomplete)  # type: ignore[method-assign]
        repo.validate_bindings = AsyncMock(return_value=[])  # type: ignore[method-assign]
        valid, errors = await repo.validate_publish(_session(), organisation_id=_ORG, revision_id=_REV)
        assert valid is False
        assert any("incomplete" in e for e in errors)

    async def test_binding_errors_are_appended(self, repo: SystemMapRepository) -> None:
        repo.list_nodes_in_revision = AsyncMock(return_value=[_node("p1"), _node("loop", "improvement_loop")])  # type: ignore[method-assign]
        repo.list_relationships_in_revision = AsyncMock(return_value=[])  # type: ignore[method-assign]
        repo.list_loop_configs_in_revision = AsyncMock(return_value=[_loop_config("loop")])  # type: ignore[method-assign]
        repo.get_loop_config_by_node_key = AsyncMock(return_value=_loop_config("loop"))  # type: ignore[method-assign]
        repo.validate_bindings = AsyncMock(return_value=["binding broke"])  # type: ignore[method-assign]
        valid, errors = await repo.validate_publish(_session(), organisation_id=_ORG, revision_id=_REV)
        assert valid is False
        assert errors == ["binding broke"]


class TestAcyclicity:
    def test_empty_graph_is_acyclic(self, repo: SystemMapRepository) -> None:
        assert repo._is_acyclic([]) is True

    def test_chain_is_acyclic(self, repo: SystemMapRepository) -> None:
        assert repo._is_acyclic([_relationship("a", "b"), _relationship("b", "c")]) is True

    def test_cycle_is_detected(self, repo: SystemMapRepository) -> None:
        assert repo._is_acyclic([_relationship("a", "b"), _relationship("b", "a")]) is False

    def test_diamond_is_acyclic(self, repo: SystemMapRepository) -> None:
        edges = [_relationship("a", "b"), _relationship("a", "c"), _relationship("b", "c")]
        assert repo._is_acyclic(edges) is True


_WINDOW_END = datetime(2026, 6, 8, 12, 0, tzinfo=UTC)
_WINDOW_START = _WINDOW_END - timedelta(days=7)
_PIPELINE_B = uuid.UUID("88888888-8888-8888-8888-888888888888")
_LOOP_A = uuid.UUID("99999999-9999-9999-9999-999999999999")
_LOOP_B = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")

_OVERVIEW_TABLES: list[Table] = cast(
    list[Table],
    [
        Organisation.__table__,
        Pipeline.__table__,
        Run.__table__,
        RunDailyFact.__table__,
        SystemMapLoopAdmissionAttempt.__table__,
        SystemMapLoopReservation.__table__,
    ],
)


@pytest.fixture
async def overview_session() -> AsyncGenerator[AsyncSession, None]:
    """A real in-memory database so the overview SQL predicates actually run."""
    engine: AsyncEngine = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=_OVERVIEW_TABLES))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


def _aware(value: datetime) -> datetime:
    """SQLite drops the offset; normalise for comparison against aware bounds."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _add_pipeline(session: AsyncSession, pipeline_id: uuid.UUID, *, max_concurrent_runs: int = 4) -> None:
    session.add(
        Pipeline(
            id=pipeline_id,
            organisation_id=_ORG,
            name=f"pipeline-{pipeline_id.hex[:6]}",
            account_id=_ACCOUNT,
            max_concurrent_runs=max_concurrent_runs,
        )
    )


def _add_run(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    status: str = "pending",
    cancellation_requested: bool = False,
) -> None:
    session.add(
        Run(
            id=run_id,
            organisation_id=_ORG,
            pipeline_id=pipeline_id,
            snapshot_id=uuid.uuid4(),
            trigger_type="manual",
            status=status,
            run_number=uuid.uuid4().int % 2_000_000_000,
            input_hash="a" * 64,
            langgraph_thread_id=f"thread-{run_id.hex}",
            cancellation_requested=cancellation_requested,
        )
    )


def _add_fact(
    session: AsyncSession,
    *,
    pipeline_id: uuid.UUID,
    status: str,
    created_at: datetime,
    benign: bool | None = None,
    telemetry_bytes: int | None = None,
    total_cost_usd: Decimal | None = None,
    completed_at: datetime | None = None,
    run_id: uuid.UUID | None = None,
) -> uuid.UUID:
    fact_run_id = run_id or uuid.uuid4()
    session.add(
        RunDailyFact(
            run_id=fact_run_id,
            organisation_id=_ORG,
            pipeline_id=pipeline_id,
            run_date=created_at.date(),
            trigger_type="manual",
            status=status,
            benign=benign,
            telemetry_bytes=telemetry_bytes,
            total_cost_usd=total_cost_usd,
            completed_at=completed_at,
            created_at=created_at,
        )
    )
    return fact_run_id


async def _pipeline_metrics(session: AsyncSession, *, pipeline_ids: set[uuid.UUID]) -> dict[uuid.UUID, dict]:
    return await SystemMapRepository(session_factory=MagicMock()).get_overview_pipeline_metrics(
        session,
        organisation_id=_ORG,
        pipeline_ids=pipeline_ids,
        window_start=_WINDOW_START,
        window_end=_WINDOW_END,
    )


class TestOverviewPipelineMetrics:
    async def test_empty_pipeline_set_returns_no_metrics(self, overview_session: AsyncSession) -> None:
        assert not await _pipeline_metrics(overview_session, pipeline_ids=set())

    async def test_canonical_failure_semantics_and_benign_exclusion(self, overview_session: AsyncSession) -> None:
        session = overview_session
        _add_pipeline(session, _PIPELINE)
        # Non-benign failures: only failed/stalled count.
        _add_fact(session, pipeline_id=_PIPELINE, status="failed", created_at=_WINDOW_START + timedelta(hours=1))
        _add_fact(session, pipeline_id=_PIPELINE, status="stalled", created_at=_WINDOW_START + timedelta(hours=2))
        # Deliberate terminal outcomes are NOT failures.
        _add_fact(session, pipeline_id=_PIPELINE, status="cancelled", created_at=_WINDOW_START + timedelta(hours=3))
        _add_fact(session, pipeline_id=_PIPELINE, status="rejected", created_at=_WINDOW_START + timedelta(hours=4))
        # A benign failure is a business-expected outcome, never a fault.
        _add_fact(
            session,
            pipeline_id=_PIPELINE,
            status="failed",
            benign=True,
            created_at=_WINDOW_START + timedelta(hours=5),
        )
        # One success.
        _add_fact(session, pipeline_id=_PIPELINE, status="complete", created_at=_WINDOW_START + timedelta(hours=6))
        await session.flush()

        metrics = await _pipeline_metrics(session, pipeline_ids={_PIPELINE})

        item = metrics[_PIPELINE]
        assert item["run_count"] == 6
        # 5 non-benign runs, 2 failures.
        assert item["failure_rate"] == pytest.approx(0.4)

    async def test_all_benign_runs_have_no_defined_failure_rate(self, overview_session: AsyncSession) -> None:
        session = overview_session
        _add_pipeline(session, _PIPELINE)
        _add_fact(session, pipeline_id=_PIPELINE, status="failed", benign=True, created_at=_WINDOW_START)
        await session.flush()

        item = (await _pipeline_metrics(session, pipeline_ids={_PIPELINE}))[_PIPELINE]

        assert item["run_count"] == 1
        assert item["failure_rate"] is None

    async def test_window_is_half_open_inclusive_start_exclusive_end(self, overview_session: AsyncSession) -> None:
        session = overview_session
        _add_pipeline(session, _PIPELINE)
        _add_fact(session, pipeline_id=_PIPELINE, status="complete", created_at=_WINDOW_START)
        _add_fact(session, pipeline_id=_PIPELINE, status="complete", created_at=_WINDOW_END)
        _add_fact(session, pipeline_id=_PIPELINE, status="complete", created_at=_WINDOW_START - timedelta(seconds=1))
        await session.flush()

        item = (await _pipeline_metrics(session, pipeline_ids={_PIPELINE}))[_PIPELINE]

        assert item["run_count"] == 1

    async def test_telemetry_null_is_unobserved_but_zero_is_observed(self, overview_session: AsyncSession) -> None:
        session = overview_session
        _add_pipeline(session, _PIPELINE)
        _add_fact(session, pipeline_id=_PIPELINE, status="complete", created_at=_WINDOW_START, telemetry_bytes=0)
        _add_fact(session, pipeline_id=_PIPELINE, status="complete", created_at=_WINDOW_START, telemetry_bytes=None)
        await session.flush()

        item = (await _pipeline_metrics(session, pipeline_ids={_PIPELINE}))[_PIPELINE]

        assert item["telemetry_coverage"] == pytest.approx(0.5)

    async def test_queue_depth_excludes_cancellation_requested_runs(self, overview_session: AsyncSession) -> None:
        session = overview_session
        _add_pipeline(session, _PIPELINE, max_concurrent_runs=8)
        _add_run(session, run_id=uuid.uuid4(), pipeline_id=_PIPELINE)
        _add_run(session, run_id=uuid.uuid4(), pipeline_id=_PIPELINE)
        _add_run(session, run_id=uuid.uuid4(), pipeline_id=_PIPELINE, cancellation_requested=True)
        _add_run(session, run_id=uuid.uuid4(), pipeline_id=_PIPELINE, status="complete")
        await session.flush()

        item = (await _pipeline_metrics(session, pipeline_ids={_PIPELINE}))[_PIPELINE]

        assert item["queue_depth"] == 2
        assert item["capacity_pressure"] == pytest.approx(0.25)

    async def test_queue_only_pipeline_has_no_floating_capacity_pressure(self, overview_session: AsyncSession) -> None:
        session = overview_session
        # The pipeline row is gone (deleted/purged), so no capacity is observed.
        _add_run(session, run_id=uuid.uuid4(), pipeline_id=_PIPELINE)
        await session.flush()

        item = (await _pipeline_metrics(session, pipeline_ids={_PIPELINE}))[_PIPELINE]

        assert item["queue_depth"] == 1
        assert item["capacity_pressure"] is None
        assert item.get("run_count") is None

    async def test_pipelines_are_isolated(self, overview_session: AsyncSession) -> None:
        session = overview_session
        _add_pipeline(session, _PIPELINE)
        _add_pipeline(session, _PIPELINE_B)
        _add_fact(session, pipeline_id=_PIPELINE, status="failed", created_at=_WINDOW_START)
        _add_fact(session, pipeline_id=_PIPELINE_B, status="complete", created_at=_WINDOW_START)
        await session.flush()

        metrics = await _pipeline_metrics(session, pipeline_ids={_PIPELINE, _PIPELINE_B})

        assert metrics[_PIPELINE]["failure_rate"] == pytest.approx(1.0)
        assert metrics[_PIPELINE_B]["failure_rate"] == pytest.approx(0.0)


def _add_reservation(
    session: AsyncSession,
    *,
    loop_config_id: uuid.UUID,
    run_id: uuid.UUID,
    status: str,
    reserved_at: datetime,
    cooldown_until: datetime | None = None,
) -> None:
    session.add(
        SystemMapLoopReservation(
            organisation_id=_ORG,
            loop_config_id=loop_config_id,
            run_id=run_id,
            idempotency_key=f"key-{uuid.uuid4().hex}",
            reserved_at=reserved_at,
            cooldown_until=cooldown_until or reserved_at + timedelta(hours=1),
            status=status,
        )
    )


def _add_admission(
    session: AsyncSession,
    *,
    loop_config_id: uuid.UUID,
    status: str,
    created_at: datetime,
) -> None:
    session.add(
        SystemMapLoopAdmissionAttempt(
            organisation_id=_ORG,
            loop_config_id=loop_config_id,
            idempotency_key=f"key-{uuid.uuid4().hex}",
            status=status,
            created_at=created_at,
        )
    )


async def _loop_metrics(session: AsyncSession, *, loop_config_ids: set[uuid.UUID]) -> dict[uuid.UUID, dict]:
    return await SystemMapRepository(session_factory=MagicMock()).get_overview_loop_metrics(
        session,
        organisation_id=_ORG,
        loop_config_ids=loop_config_ids,
        window_start=_WINDOW_START,
        window_end=_WINDOW_END,
    )


class TestOverviewLoopMetrics:
    async def test_empty_loop_set_returns_no_metrics(self, overview_session: AsyncSession) -> None:
        assert not await _loop_metrics(overview_session, loop_config_ids=set())

    async def test_latest_admission_wins_within_the_window(self, overview_session: AsyncSession) -> None:
        session = overview_session
        _add_admission(
            session, loop_config_id=_LOOP_A, status="rejected", created_at=_WINDOW_START + timedelta(hours=1)
        )
        _add_admission(
            session, loop_config_id=_LOOP_A, status="admitted", created_at=_WINDOW_START + timedelta(hours=2)
        )
        # Outside the window — must not be reported as the latest admission.
        _add_admission(
            session, loop_config_id=_LOOP_A, status="duplicate", created_at=_WINDOW_END + timedelta(minutes=1)
        )
        await session.flush()

        item = (await _loop_metrics(session, loop_config_ids={_LOOP_A}))[_LOOP_A]

        assert item["last_admission_status"] == "admitted"
        assert _aware(item["last_admission_at"]) == _WINDOW_START + timedelta(hours=2)

    async def test_budget_sums_only_budget_consuming_reservations(self, overview_session: AsyncSession) -> None:
        session = overview_session
        consumed_run = _add_fact(
            session,
            pipeline_id=_PIPELINE,
            status="complete",
            created_at=_WINDOW_START,
            total_cost_usd=Decimal("1.500000"),
        )
        active_run = _add_fact(
            session,
            pipeline_id=_PIPELINE,
            status="complete",
            created_at=_WINDOW_START,
            total_cost_usd=Decimal("2.250000"),
        )
        released_run = _add_fact(
            session,
            pipeline_id=_PIPELINE,
            status="complete",
            created_at=_WINDOW_START,
            total_cost_usd=Decimal("9.000000"),
        )
        expired_run = _add_fact(
            session,
            pipeline_id=_PIPELINE,
            status="complete",
            created_at=_WINDOW_START,
            total_cost_usd=Decimal("9.000000"),
        )
        _add_reservation(
            session,
            loop_config_id=_LOOP_A,
            run_id=consumed_run,
            status="consumed",
            reserved_at=_WINDOW_START + timedelta(hours=1),
        )
        _add_reservation(
            session,
            loop_config_id=_LOOP_A,
            run_id=active_run,
            status="active",
            reserved_at=_WINDOW_START + timedelta(hours=2),
        )
        _add_reservation(
            session,
            loop_config_id=_LOOP_A,
            run_id=released_run,
            status="released",
            reserved_at=_WINDOW_START + timedelta(hours=3),
        )
        _add_reservation(
            session,
            loop_config_id=_LOOP_A,
            run_id=expired_run,
            status="expired",
            reserved_at=_WINDOW_START + timedelta(hours=4),
        )
        await session.flush()

        item = (await _loop_metrics(session, loop_config_ids={_LOOP_A}))[_LOOP_A]

        assert item["budget_used_usd"] == Decimal("3.750000")
        # No budget period is recorded, so the window is not reported as one.
        assert "budget_period_start" not in item

    async def test_reservations_outside_the_window_are_not_budget(self, overview_session: AsyncSession) -> None:
        session = overview_session
        old_run = _add_fact(
            session,
            pipeline_id=_PIPELINE,
            status="complete",
            created_at=_WINDOW_START - timedelta(days=1),
            total_cost_usd=Decimal("5.000000"),
        )
        _add_reservation(
            session,
            loop_config_id=_LOOP_A,
            run_id=old_run,
            status="consumed",
            reserved_at=_WINDOW_START - timedelta(days=1),
        )
        await session.flush()

        item = (await _loop_metrics(session, loop_config_ids={_LOOP_A}))[_LOOP_A]

        assert item["budget_used_usd"] is None
        assert item["cooldown_active"] is False

    async def test_unobserved_cost_leaves_budget_null(self, overview_session: AsyncSession) -> None:
        session = overview_session
        observed_run = _add_fact(
            session,
            pipeline_id=_PIPELINE,
            status="complete",
            created_at=_WINDOW_START,
            total_cost_usd=Decimal("1.000000"),
        )
        unobserved_run = _add_fact(
            session,
            pipeline_id=_PIPELINE,
            status="complete",
            created_at=_WINDOW_START,
            total_cost_usd=None,
        )
        _add_reservation(
            session,
            loop_config_id=_LOOP_A,
            run_id=observed_run,
            status="consumed",
            reserved_at=_WINDOW_START + timedelta(hours=1),
        )
        _add_reservation(
            session,
            loop_config_id=_LOOP_A,
            run_id=unobserved_run,
            status="consumed",
            reserved_at=_WINDOW_START + timedelta(hours=2),
        )
        await session.flush()

        item = (await _loop_metrics(session, loop_config_ids={_LOOP_A}))[_LOOP_A]

        assert item["budget_used_usd"] is None

    async def test_reservation_without_a_fact_leaves_budget_null(self, overview_session: AsyncSession) -> None:
        session = overview_session
        _add_reservation(
            session,
            loop_config_id=_LOOP_A,
            run_id=uuid.uuid4(),
            status="consumed",
            reserved_at=_WINDOW_START + timedelta(hours=1),
        )
        await session.flush()

        item = (await _loop_metrics(session, loop_config_ids={_LOOP_A}))[_LOOP_A]

        assert item["budget_used_usd"] is None

    async def test_no_reservation_yet_returns_no_route_fields(self, overview_session: AsyncSession) -> None:
        session = overview_session
        _add_admission(session, loop_config_id=_LOOP_A, status="pending", created_at=_WINDOW_START)
        await session.flush()

        item = (await _loop_metrics(session, loop_config_ids={_LOOP_A}))[_LOOP_A]

        assert item["last_admission_status"] == "pending"
        assert "cooldown_active" not in item
        assert "budget_used_usd" not in item

    async def test_expired_cooldown_is_explicitly_inactive(self, overview_session: AsyncSession) -> None:
        session = overview_session
        _add_reservation(
            session,
            loop_config_id=_LOOP_A,
            run_id=uuid.uuid4(),
            status="active",
            reserved_at=_WINDOW_START + timedelta(hours=1),
            cooldown_until=_WINDOW_END - timedelta(minutes=1),
        )
        await session.flush()

        item = (await _loop_metrics(session, loop_config_ids={_LOOP_A}))[_LOOP_A]

        assert item["cooldown_active"] is False
        assert "cooldown_until" not in item

    async def test_active_cooldown_reports_until(self, overview_session: AsyncSession) -> None:
        session = overview_session
        cooldown_until = _WINDOW_END + timedelta(hours=2)
        _add_reservation(
            session,
            loop_config_id=_LOOP_A,
            run_id=uuid.uuid4(),
            status="active",
            reserved_at=_WINDOW_START + timedelta(hours=1),
            cooldown_until=cooldown_until,
        )
        await session.flush()

        item = (await _loop_metrics(session, loop_config_ids={_LOOP_A}))[_LOOP_A]

        assert item["cooldown_active"] is True
        assert _aware(item["cooldown_until"]) == cooldown_until

    async def test_cooldown_reports_the_latest_active_until(self, overview_session: AsyncSession) -> None:
        session = overview_session
        earlier = _WINDOW_END + timedelta(hours=1)
        later = _WINDOW_END + timedelta(hours=3)
        # The most recently reserved row cools down sooner; the older row's
        # later deadline must still win, so the reported state is the maximum.
        _add_reservation(
            session,
            loop_config_id=_LOOP_A,
            run_id=uuid.uuid4(),
            status="active",
            reserved_at=_WINDOW_START + timedelta(hours=1),
            cooldown_until=later,
        )
        _add_reservation(
            session,
            loop_config_id=_LOOP_A,
            run_id=uuid.uuid4(),
            status="active",
            reserved_at=_WINDOW_START + timedelta(hours=2),
            cooldown_until=earlier,
        )
        await session.flush()

        item = (await _loop_metrics(session, loop_config_ids={_LOOP_A}))[_LOOP_A]

        assert item["cooldown_active"] is True
        assert _aware(item["cooldown_until"]) == later

    async def test_cooldown_is_current_state_outside_the_budget_window(self, overview_session: AsyncSession) -> None:
        session = overview_session
        cooldown_until = _WINDOW_END + timedelta(hours=3)
        _add_reservation(
            session,
            loop_config_id=_LOOP_A,
            run_id=uuid.uuid4(),
            status="active",
            # Reserved before the budget window but still cooling down now.
            reserved_at=_WINDOW_START - timedelta(days=1),
            cooldown_until=cooldown_until,
        )
        await session.flush()

        item = (await _loop_metrics(session, loop_config_ids={_LOOP_A}))[_LOOP_A]

        assert item["budget_used_usd"] is None
        assert item["cooldown_active"] is True
        assert _aware(item["cooldown_until"]) == cooldown_until

    async def test_loops_are_isolated(self, overview_session: AsyncSession) -> None:
        session = overview_session
        _add_admission(session, loop_config_id=_LOOP_A, status="admitted", created_at=_WINDOW_START)
        _add_reservation(
            session,
            loop_config_id=_LOOP_B,
            run_id=uuid.uuid4(),
            status="active",
            reserved_at=_WINDOW_START,
            cooldown_until=_WINDOW_END + timedelta(hours=1),
        )
        await session.flush()

        metrics = await _loop_metrics(session, loop_config_ids={_LOOP_A, _LOOP_B})

        # No reservation evidence for A, so its cooldown is unreported (null).
        assert "cooldown_active" not in metrics[_LOOP_A]
        assert metrics[_LOOP_B]["cooldown_active"] is True


class TestFullRevisionGraph:
    async def test_missing_revision_returns_none(self, repo: SystemMapRepository) -> None:
        assert (
            await repo.get_full_revision_graph(_session(_result(one=None)), organisation_id=_ORG, revision_id=_REV)
            is None
        )

    async def test_graph_assembles_all_parts(self, repo: SystemMapRepository) -> None:
        revision = _revision()
        node = _node("n1")
        rel = _relationship("n1", "n2")
        loop = _loop_config("loop")
        db = _session(
            _result(one=revision),
            _result(rows=[node]),
            _result(rows=[rel]),
            _result(rows=[loop]),
        )
        graph = await repo.get_full_revision_graph(db, organisation_id=_ORG, revision_id=_REV)
        assert graph == {
            "revision": revision,
            "nodes": [node],
            "relationships": [rel],
            "loop_configs": [loop],
        }
