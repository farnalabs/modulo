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
import logging
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from modulo.api.dependencies import get_db_session
from modulo.api.main import app
from modulo.api.routes import compliance
from modulo.api.routes.compliance import (
    _CODE_EVIDENCE_INDEX_READ,
    _EVIDENCE_INDEX_STATEMENT_TIMEOUT_MS,
    _LOG_PREFIX,
    _MSG_TIMEOUT,
    _bound_statement_timeout,
    get_evidence_index_endpoint,
)
from modulo.auth.dependencies import get_current_tenant_user
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


async def _seed_minimal_org(session: AsyncSession, slug: str = "compliance") -> tuple[uuid.UUID, uuid.UUID]:
    org_id = uuid.uuid4()
    account_id = uuid.uuid4()
    session.add(Organisation(id=org_id, name=f"Compliance Org {slug}", slug=slug))
    session.add(Account(id=account_id, email=f"admin-{slug}@compliance.example", display_name="Admin"))
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


@pytest.fixture
def client_for(session: AsyncSession):
    """Build a real HTTP client with only the credential/session overridden.

    Module-scoped so both the authorisation tests and the audit-period
    parameter tests drive the real route (query-param aliases and FastAPI's
    422 validation only exist there).
    """

    def _make(role: str, org_id: uuid.UUID, account_id: uuid.UUID) -> AsyncClient:
        principal = TenantPrincipal(
            username=f"{role}@compliance.example",
            organisation_id=org_id,
            account_id=account_id,
            org_role=role,
        )

        async def _principal() -> TenantPrincipal:
            return principal

        app.dependency_overrides[get_current_tenant_user] = _principal
        app.dependency_overrides[get_db_session] = lambda: session
        return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")

    yield _make
    app.dependency_overrides.clear()


class TestRequestLevelAuthorisation:
    """Drive the real HTTP route, not the handler.

    Every behaviour test above calls ``get_evidence_index_endpoint`` directly,
    which bypasses ``require_permission`` entirely - so none of them could fail
    if the permission gate were removed or downgraded. These go through the app
    with only the credential and session dependencies overridden.
    """

    async def test_a_viewer_is_forbidden(self, client_for, session: AsyncSession) -> None:
        org_id, account_id = await _seed_minimal_org(session)
        client = client_for("viewer", org_id, account_id)

        async with client:
            response = await client.get(_ROUTE_PATH)

        assert response.status_code == 403
        # The denial must come from THIS key's gate, not from some unrelated
        # failure that happens to answer 403.
        assert _CODE_EVIDENCE_INDEX_READ in response.text

    async def test_an_operator_is_forbidden(self, client_for, session: AsyncSession) -> None:
        """The key is admin-only - one step below admin must still be denied."""
        org_id, account_id = await _seed_minimal_org(session)
        client = client_for("operator", org_id, account_id)

        async with client:
            response = await client.get(_ROUTE_PATH)

        assert response.status_code == 403

    async def test_an_admin_succeeds(self, client_for, session: AsyncSession) -> None:
        org_id, account_id = await _seed_minimal_org(session)
        client = client_for("admin", org_id, account_id)

        async with client:
            response = await client.get(_ROUTE_PATH)

        assert response.status_code == 200
        body = response.json()
        assert body["organisation_id"] == str(org_id)
        assert body["pack_version"] == load_mapping_pack().pack_version


class TestAuditPeriodParameters:
    """FAR-2055: ``from`` / ``to`` scope the index and are recorded in it.

    Both the direct handler call (unit-level) and the real HTTP route are
    exercised: the query-parameter aliases and their 422 validation only exist
    at the HTTP boundary, so a handler-only test could not see them.
    """

    async def test_direct_call_records_the_period(self, session: AsyncSession) -> None:
        org_id, account_id = await _seed_minimal_org(session)
        principal = TenantPrincipal(
            username="admin@compliance.example",
            organisation_id=org_id,
            account_id=account_id,
            org_role="admin",
        )
        period_from = datetime(2026, 1, 15, 9, 0, 0, tzinfo=UTC)
        period_to = datetime(2026, 1, 15, 10, 0, 0, tzinfo=UTC)

        index = await get_evidence_index_endpoint(
            session=session,
            principal=principal,
            period_from=period_from,
            period_to=period_to,
        )

        assert index.period_from == period_from.isoformat()
        assert index.period_to == period_to.isoformat()

    async def test_direct_call_defaults_to_full_history(self, session: AsyncSession) -> None:
        org_id, account_id = await _seed_minimal_org(session)
        principal = TenantPrincipal(
            username="admin@compliance.example",
            organisation_id=org_id,
            account_id=account_id,
            org_role="admin",
        )

        index = await get_evidence_index_endpoint(session=session, principal=principal)

        assert index.period_from is None
        assert index.period_to is None

    async def test_http_route_accepts_from_and_to(self, client_for, session: AsyncSession) -> None:
        org_id, account_id = await _seed_minimal_org(session)
        client = client_for("admin", org_id, account_id)

        async with client:
            response = await client.get(
                _ROUTE_PATH,
                params={
                    "from": "2026-01-15T09:00:00Z",
                    "to": "2026-01-15T10:00:00Z",
                },
            )

        assert response.status_code == 200
        body = response.json()
        assert body["period_from"] == "2026-01-15T09:00:00+00:00"
        assert body["period_to"] == "2026-01-15T10:00:00+00:00"

    async def test_http_route_rejects_an_inverted_period(self, client_for, session: AsyncSession) -> None:
        org_id, account_id = await _seed_minimal_org(session)
        client = client_for("admin", org_id, account_id)

        async with client:
            response = await client.get(
                _ROUTE_PATH,
                params={
                    "from": "2026-01-16T00:00:00Z",
                    "to": "2026-01-15T00:00:00Z",
                },
            )

        assert response.status_code == 422
        assert "audit period" in response.text

    async def test_http_route_rejects_an_unparseable_datetime(self, client_for, session: AsyncSession) -> None:
        org_id, account_id = await _seed_minimal_org(session)
        client = client_for("admin", org_id, account_id)

        async with client:
            response = await client.get(_ROUTE_PATH, params={"from": "not-a-date"})

        assert response.status_code == 422

    async def test_the_period_never_moves_the_tenant_boundary(self, client_for, session: AsyncSession) -> None:
        """A period parameter must not become an organisation selector."""
        mine_org, mine_account = await _seed_minimal_org(session, slug="period-mine")
        other_org, _other_account = await _seed_minimal_org(session, slug="period-other")
        client = client_for("admin", mine_org, mine_account)

        async with client:
            response = await client.get(
                _ROUTE_PATH,
                params={
                    "from": "2026-01-15T09:00:00Z",
                    "to": "2026-01-15T10:00:00Z",
                },
            )

        assert response.status_code == 200
        assert response.json()["organisation_id"] == str(mine_org)
        assert response.json()["organisation_id"] != str(other_org)


def _admin_principal(org_id: uuid.UUID, account_id: uuid.UUID) -> TenantPrincipal:
    return TenantPrincipal(
        username="admin@compliance.example",
        organisation_id=org_id,
        account_id=account_id,
        org_role="admin",
    )


class _CancelledDriverError(Exception):
    """A driver error carrying a SQLSTATE through the chain walk (no PG needed)."""

    def __init__(self, sqlstate: str | None) -> None:
        super().__init__(f"driver failure (sqlstate={sqlstate})")
        if sqlstate is not None:
            self.sqlstate = sqlstate


def _dbapi_error(sqlstate: str | None) -> DBAPIError:
    """A SQLAlchemy DBAPIError wrapping a driver error with *sqlstate*."""
    return DBAPIError("SELECT 1", {}, _CancelledDriverError(sqlstate))


class _DialectSession:
    """Minimal stand-in for ``AsyncSession``: reports a dialect, records executes.

    ``_bound_statement_timeout`` reads ONLY ``session.connection().dialect.name``
    and then issues ``session.execute``, so this exercises the real function's
    branch and SQL/params rather than a mocked return value.
    """

    def __init__(self, dialect_name: str) -> None:
        self.dialect_name = dialect_name
        self.executed: list[tuple[str, dict[str, object] | None]] = []

    async def connection(self) -> SimpleNamespace:
        return SimpleNamespace(dialect=SimpleNamespace(name=self.dialect_name))

    async def execute(self, statement: object, params: dict[str, object] | None = None) -> None:
        self.executed.append((str(statement), params))


class TestStatementTimeoutBound:
    """FAR-2055: the Postgres-only, transaction-local statement_timeout bound.

    The bound is what stops an org-wide aggregate from pinning a pooled
    connection, so its two halves - the emitted ``set_config`` on Postgres and
    its ABSENCE elsewhere - are both pinned here.
    """

    async def test_postgres_issues_the_transaction_local_set_config(self) -> None:
        fake = _DialectSession("postgresql")

        await _bound_statement_timeout(cast("AsyncSession", fake))

        assert len(fake.executed) == 1
        statement, params = fake.executed[0]
        assert statement == "SELECT set_config('statement_timeout', :ms, true)"
        # ``true`` above is ``is_local``: a pooled connection must not carry the
        # bound into the next request. The value is the documented 30s bound.
        assert params == {"ms": "30000"}
        assert _EVIDENCE_INDEX_STATEMENT_TIMEOUT_MS == 30_000

    async def test_sqlite_issues_no_set_config(self) -> None:
        fake = _DialectSession("sqlite")

        await _bound_statement_timeout(cast("AsyncSession", fake))

        assert not fake.executed

    async def test_the_endpoint_applies_the_bound_inside_its_transaction(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Wiring, not just the helper: the endpoint must actually call it.

        A helper-only test would still pass if the endpoint stopped calling it,
        or called it outside ``session.begin()`` (where a transaction-local
        ``set_config`` would silently have no effect).
        """
        org_id, account_id = await _seed_minimal_org(session)
        observed_in_transaction: list[bool] = []
        real_bound = compliance._bound_statement_timeout

        async def _spy(bound_session: AsyncSession) -> None:
            observed_in_transaction.append(bound_session.in_transaction())
            await real_bound(bound_session)

        monkeypatch.setattr(compliance, "_bound_statement_timeout", _spy)

        await get_evidence_index_endpoint(session=session, principal=_admin_principal(org_id, account_id))

        assert observed_in_transaction == [True]

    async def test_the_sqlite_endpoint_emits_no_statement_timeout_statement(self, session: AsyncSession) -> None:
        """Observed at the driver: the SQLite request carries no timeout DDL."""
        org_id, account_id = await _seed_minimal_org(session)
        engine = session.get_bind()
        statements: list[str] = []

        def _record_execute(_conn, _cursor, statement, _parameters, _context, _executemany) -> None:
            statements.append(statement)

        event.listen(engine, "before_cursor_execute", _record_execute)
        try:
            index = await get_evidence_index_endpoint(session=session, principal=_admin_principal(org_id, account_id))
        finally:
            event.remove(engine, "before_cursor_execute", _record_execute)

        assert index.organisation_id == str(org_id)
        # The listener must have observed real statements, or the absence
        # assertion below would pass vacuously.
        assert statements
        assert not any("statement_timeout" in statement for statement in statements)


class TestStatementTimeoutFailureArm:
    """FAR-2055: a SQLSTATE 57014 cancel degrades to an actionable 503.

    The handler's own ``except DBAPIError`` arm is exercised (the real route
    function, with the index build raising the failure) - not a mock's return
    value - so deleting the arm fails these tests.
    """

    async def test_a_statement_timeout_cancel_is_an_actionable_503(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        org_id, account_id = await _seed_minimal_org(session)

        async def _raise_timeout(*_args: object, **_kwargs: object) -> None:
            raise _dbapi_error("57014")

        monkeypatch.setattr(compliance, "build_evidence_index", _raise_timeout)
        caplog.set_level(logging.ERROR, logger="modulo.api.db_error_reporting")

        with pytest.raises(HTTPException) as raised:
            await get_evidence_index_endpoint(session=session, principal=_admin_principal(org_id, account_id))

        assert raised.value.status_code == 503
        detail = str(raised.value.detail)
        # The remedy must name the knobs that actually narrow the scan.
        assert detail == _MSG_TIMEOUT
        assert "'from'" in detail
        assert "'to'" in detail

        # Operationally visible: the timeout arm records its own structured
        # failure at ERROR (the generic DB path's db_transient record does not
        # run for this arm), tagged with a reason distinct from a real outage.
        records = [record for record in caplog.records if getattr(record, "service_unavailable", None)]
        assert len(records) == 1
        structured = records[0].service_unavailable
        assert structured["reason"] == "statement_timeout"
        assert structured["route"] == _LOG_PREFIX
        assert records[0].levelno == logging.ERROR

    async def test_a_non_timeout_db_error_does_not_get_the_period_remedy(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A genuine outage keeps the generic path - the arm must discriminate."""
        org_id, account_id = await _seed_minimal_org(session)

        async def _raise_unavailable(*_args: object, **_kwargs: object) -> None:
            raise _dbapi_error("08006")

        monkeypatch.setattr(compliance, "build_evidence_index", _raise_unavailable)
        caplog.set_level(logging.ERROR, logger="modulo.api.db_error_reporting")

        with pytest.raises(HTTPException) as raised:
            await get_evidence_index_endpoint(session=session, principal=_admin_principal(org_id, account_id))

        assert raised.value.status_code == 503
        assert str(raised.value.detail) != _MSG_TIMEOUT
        assert str(raised.value.detail) == "Database temporarily unavailable."

        records = [record for record in caplog.records if getattr(record, "service_unavailable", None)]
        assert len(records) == 1
        assert records[0].service_unavailable["reason"] == "db_transient"

    async def test_a_cancel_without_a_sqlstate_is_not_treated_as_a_timeout(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No SQLSTATE means no evidence of a cancel - the generic path applies."""
        org_id, account_id = await _seed_minimal_org(session)

        async def _raise_opaque(*_args: object, **_kwargs: object) -> None:
            raise _dbapi_error(None)

        monkeypatch.setattr(compliance, "build_evidence_index", _raise_opaque)

        with pytest.raises(HTTPException) as raised:
            await get_evidence_index_endpoint(session=session, principal=_admin_principal(org_id, account_id))

        assert str(raised.value.detail) != _MSG_TIMEOUT
        assert str(raised.value.detail) == "Database temporarily unavailable."
