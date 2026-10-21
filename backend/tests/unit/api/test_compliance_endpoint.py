"""Tests for the compliance evidence-index route and its permission key (FAR-2036).

Three things here are load-bearing:

* the permission key resolves to the role we intend (admin) - not merely that
  it exists, because a valid-but-wrong key silently downgrades a gate;
* the key is not one edit away from any existing key, so a typo cannot inherit
  another key's role (the ``runner.status`` / ``run.status`` failure mode);
* the organisation comes from the authenticated principal and nowhere else.
"""

from __future__ import annotations

import inspect
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from modulo.api.main import app
from modulo.api.routes.compliance import (
    _CODE_EVIDENCE_INDEX_READ,
    get_evidence_index_endpoint,
)
from modulo.auth.jwt import TenantPrincipal
from modulo.auth.permissions import PERMISSIONS, PermissionConfigurationError, resolve_required
from modulo.compliance.artifacts import INDEXED_ARTIFACT_TYPES
from modulo.compliance.evidence_index import EvidenceIndex, build_evidence_index
from modulo.compliance.pack import load_mapping_pack
from modulo.db.models.account import Account
from modulo.db.models.audit_event import AuditChainHead, AuditEvent
from modulo.db.models.base import Base
from modulo.db.models.organisation import Organisation
from tests.unit.api.route_introspection import (
    get_all_apiroutes,
    get_permission_tag,
    get_resolved_min_role,
)

_NEEDED_TABLES = {
    "accounts",
    "organisations",
    "pipelines",
    "pipeline_snapshots",
    "runs",
    "evals",
    "eval_results",
    "hitl_claims",
    "audit_events",
    "audit_chain_heads",
}

_ROUTE_PATH = "/api/v1/compliance/evidence-index"


def _one_edit_apart(left: str, right: str) -> bool:
    """True when *left* can become *right* with a single insert/delete/change."""
    if left == right:
        return False
    if abs(len(left) - len(right)) > 1:
        return False
    if len(left) == len(right):
        return sum(1 for a, b in zip(left, right, strict=True) if a != b) == 1
    shorter, longer = (left, right) if len(left) < len(right) else (right, left)
    index = 0
    while index < len(shorter) and shorter[index] == longer[index]:
        index += 1
    return shorter[index:] == longer[index + 1 :]


class TestPermissionKey:
    def test_key_resolves_to_admin(self) -> None:
        assert PERMISSIONS[_CODE_EVIDENCE_INDEX_READ] == "admin"
        assert resolve_required(_CODE_EVIDENCE_INDEX_READ) == "admin"

    def test_key_is_not_one_edit_from_an_existing_key(self) -> None:
        near_misses = [
            other
            for other in PERMISSIONS
            if other != _CODE_EVIDENCE_INDEX_READ and _one_edit_apart(other, _CODE_EVIDENCE_INDEX_READ)
        ]
        assert not near_misses, (
            f"a one-character typo of the compliance key would silently resolve to another key's role: {near_misses}"
        )

    def test_dropping_the_namespace_fails_closed(self) -> None:
        with pytest.raises(PermissionConfigurationError):
            resolve_required("evidence_index.read")

    def test_the_viewer_level_evidence_read_key_is_a_different_key(self) -> None:
        # ``evidence.read`` guards the run-evidence browse surface at viewer
        # level. The compliance index is a different, admin-level surface and
        # must not be routed through it.
        assert PERMISSIONS["evidence.read"] == "viewer"
        assert PERMISSIONS[_CODE_EVIDENCE_INDEX_READ] != PERMISSIONS["evidence.read"]


class TestRouteWiring:
    def _route(self):
        matches = [route for route in get_all_apiroutes(app) if route.path == _ROUTE_PATH]
        assert len(matches) == 1, [route.path for route in matches]
        return matches[0]

    def test_autonomy_event_constants_match_the_write_site(self) -> None:
        """A rename at the write site must break this test, not empty the index."""
        from modulo.api.routes import pipelines
        from modulo.compliance import evidence_index as evidence_index_module

        assert evidence_index_module.AUTONOMY_PROMOTION_EVENT == pipelines.AUTONOMY_PROMOTION_DECIDED_EVENT
        assert evidence_index_module.AUTONOMY_DEMOTION_EVENT == pipelines.AUTONOMY_DEMOTED_EVENT

    def test_route_is_registered_as_a_get(self) -> None:
        assert "GET" in (self._route().methods or set())

    def test_route_carries_the_admin_permission_tag(self) -> None:
        tag = get_permission_tag(self._route())

        assert tag is not None
        assert tag["permission"] == _CODE_EVIDENCE_INDEX_READ
        assert get_resolved_min_role(_CODE_EVIDENCE_INDEX_READ) == "admin"

    def test_endpoint_takes_no_organisation_from_the_caller(self) -> None:
        parameters = set(inspect.signature(get_evidence_index_endpoint).parameters)

        assert "organisation_id" not in parameters
        assert "org_id" not in parameters


@pytest.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine("sqlite+aiosqlite://", echo=False)
    tables = [table for table in Base.metadata.sorted_tables if table.name in _NEEDED_TABLES]
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=tables))
        await conn.exec_driver_sql("PRAGMA foreign_keys = OFF")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as db_session:
        yield db_session
    await engine.dispose()


async def _seed_minimal_org(session: AsyncSession) -> tuple[uuid.UUID, uuid.UUID]:
    org_id = uuid.uuid4()
    account_id = uuid.uuid4()
    session.add(Organisation(id=org_id, name="Compliance Org", slug="compliance"))
    session.add(Account(id=account_id, email="admin@compliance.example", display_name="Admin"))
    await session.flush()
    event = AuditEvent(
        id=uuid.uuid4(),
        organisation_id=org_id,
        event_type="pipeline_created",
        payload_json={},
        created_at=datetime(2026, 1, 15, 9, 0, 0, tzinfo=UTC),
    )
    session.add(event)
    await session.flush()
    session.add(
        AuditChainHead(
            id=uuid.uuid4(),
            organisation_id=org_id,
            last_event_hash="e" * 64,
            last_event_id=event.id,
            event_count=1,
        )
    )
    await session.commit()
    return org_id, account_id


class TestEndpointBehaviour:
    async def test_response_is_scoped_to_the_principals_organisation(self, session: AsyncSession) -> None:
        org_id, account_id = await _seed_minimal_org(session)
        principal = TenantPrincipal(
            username="admin@compliance.example",
            organisation_id=org_id,
            account_id=account_id,
            org_role="admin",
        )

        index = await get_evidence_index_endpoint(session=session, principal=principal)

        assert index.organisation_id == str(org_id)

    async def test_response_records_the_pack_version(self, session: AsyncSession) -> None:
        org_id, account_id = await _seed_minimal_org(session)
        principal = TenantPrincipal(
            username="admin@compliance.example",
            organisation_id=org_id,
            account_id=account_id,
            org_role="admin",
        )
        pack = load_mapping_pack()

        index = await get_evidence_index_endpoint(session=session, principal=principal)

        assert index.pack_version == pack.pack_version
        assert index.pack_content_hash == pack.content_hash()

    async def test_response_accounts_for_every_mapped_artefact_type(self, session: AsyncSession) -> None:
        org_id, account_id = await _seed_minimal_org(session)
        principal = TenantPrincipal(
            username="admin@compliance.example",
            organisation_id=org_id,
            account_id=account_id,
            org_role="admin",
        )

        index = await get_evidence_index_endpoint(session=session, principal=principal)

        assert tuple(entry.artifact_type for entry in index.artifacts) == INDEXED_ARTIFACT_TYPES

    async def test_missing_artefacts_are_surfaced_not_dropped(self, session: AsyncSession) -> None:
        org_id, account_id = await _seed_minimal_org(session)
        principal = TenantPrincipal(
            username="admin@compliance.example",
            organisation_id=org_id,
            account_id=account_id,
            org_role="admin",
        )

        index = await get_evidence_index_endpoint(session=session, principal=principal)

        # Only the audit chain was seeded; the rest is reported as absent.
        assert not index.complete
        assert "audit_chain" not in index.missing_artifact_types
        assert "hitl_decision" in index.missing_artifact_types

    async def test_generator_is_callable_outside_the_route(self, session: AsyncSession) -> None:
        """The index is a library function, not route-private logic."""
        org_id, _account_id = await _seed_minimal_org(session)

        index = await build_evidence_index(session, org_id)

        assert index.organisation_id == str(org_id)

    async def test_index_round_trips_through_json(self, session: AsyncSession) -> None:
        """The response model must serialise - it is served over the wire."""
        org_id, account_id = await _seed_minimal_org(session)
        principal = TenantPrincipal(
            username="admin@compliance.example",
            organisation_id=org_id,
            account_id=account_id,
            org_role="admin",
        )
        index = await get_evidence_index_endpoint(session=session, principal=principal)

        payload = index.model_dump_json()
        restored = EvidenceIndex.model_validate_json(payload)

        assert restored.pack_content_hash == index.pack_content_hash
        assert restored.organisation_id == str(org_id)
        assert tuple(entry.artifact_type for entry in restored.artifacts) == INDEXED_ARTIFACT_TYPES
