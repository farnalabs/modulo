"""Integration tests for POST /api/v1/metrics/events (FAR-355).

Round-trips the real payload through the real endpoint into the ``metrics_staging``
table against a real Postgres (Testcontainers). This is the missing contract
round-trip for a brand-new RLS-protected write path:

- a staged row is actually written with the correct org / event_type / payload /
  recorded_at,
- the ``rls_org_isolation`` policy confines reads to the authenticated org
  (org B never sees org A's rows, and a request authenticated as org B cannot
  land a row in org A because ``organisation_id`` is taken from the principal),
- the ``pg_insert(...).on_conflict_do_nothing`` dedup honours the real
  ``uq_metrics_staging_org_event_id`` unique constraint,
- the route sanitizer persists ``"unknown"`` for an unmatched route.

These complement the unit tests in ``tests/unit/product_analytics/`` which run
against a mock session that discards the staged payload.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import datetime

import pytest
import pytest_asyncio
from httpx import AsyncClient, Response
from sqlalchemy import JSON, bindparam, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from modulo.auth.jwt import create_access_token

pytestmark = pytest.mark.integration

_VALID_32 = "a" * 32


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


async def _seed_org(db_engine: AsyncEngine, name: str, *, settings_json: dict | None = None) -> uuid.UUID:
    org_id = uuid.uuid4()
    settings = settings_json if settings_json is not None else {}
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO organisations (id, name, slug, settings_json) VALUES (:id, :name, :slug, :sj)"
            ).bindparams(bindparam("sj", settings, type_=JSON)),
            {"id": str(org_id), "name": name, "slug": f"{name}-{org_id.hex[:8]}"},
        )
    return org_id


async def _seed_user(db_engine: AsyncEngine, org_id: uuid.UUID, email: str) -> uuid.UUID:
    account_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        existing = await conn.execute(
            text("SELECT id FROM accounts WHERE email = :email"),
            {"email": email},
        )
        row = existing.first()
        if row is not None:
            account_id = uuid.UUID(str(row[0]))
        else:
            await conn.execute(
                text(
                    "INSERT INTO accounts (id, email, display_name, auth_provider, active, password_hash) "
                    "VALUES (:id, :email, :name, 'local', true, 'hash')"
                ),
                {"id": str(account_id), "email": email, "name": f"Admin {email}"},
            )
        membership = await conn.execute(
            text("SELECT id FROM org_memberships WHERE account_id = :aid AND organisation_id = :oid"),
            {"aid": str(account_id), "oid": str(org_id)},
        )
        if membership.first() is None:
            await conn.execute(
                text(
                    "INSERT INTO org_memberships (id, account_id, organisation_id, role) "
                    "VALUES (:mid, :aid, :oid, 'admin')"
                ),
                {"mid": str(uuid.uuid4()), "aid": str(account_id), "oid": str(org_id)},
            )
    return account_id


def _token(org_id: uuid.UUID, user_id: uuid.UUID, role: str = "admin") -> str:
    return create_access_token(
        subject=f"user-{user_id.hex[:8]}",
        secret_key=_VALID_32,
        organisation_id=str(org_id),
        account_id=str(user_id),
        org_role=role,
    )


# ---------------------------------------------------------------------------
# Client + fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(scope="module")
async def org_a(db_engine: AsyncEngine) -> uuid.UUID:
    return await _seed_org(
        db_engine,
        "MetricsIngest-A",
        settings_json={"product_analytics": {"level": "all"}},
    )


@pytest_asyncio.fixture(scope="module")
async def org_b(db_engine: AsyncEngine) -> uuid.UUID:
    return await _seed_org(
        db_engine,
        "MetricsIngest-B",
        settings_json={"product_analytics": {"level": "all"}},
    )


@pytest_asyncio.fixture(scope="module")
async def user_a(db_engine: AsyncEngine, org_a: uuid.UUID) -> uuid.UUID:
    return await _seed_user(db_engine, org_a, "metrics-a@test.local")


@pytest_asyncio.fixture(scope="module")
async def user_b(db_engine: AsyncEngine, org_b: uuid.UUID) -> uuid.UUID:
    return await _seed_user(db_engine, org_b, "metrics-b@test.local")


@pytest_asyncio.fixture(autouse=True)
async def _clean_metrics_staging(db_engine: AsyncEngine) -> AsyncGenerator[None, None]:
    """Isolate each test against the shared session database.

    Every test in this module stages rows for the same module-scoped ``org_a`` /
    ``org_b``, and several assert on exact ``metrics_staging`` counts. Postgres
    is shared across the session, so a row staged by one test (e.g. ``rt-1`` from
    ``TestRoundTripWrite``) leaks into the next and pollutes those exact-count
    assertions. Truncate the staging table before each test so every test starts
    from a known-empty table.
    """
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(text("TRUNCATE metrics_staging"))
    yield


# ---------------------------------------------------------------------------
# Read helpers — query metrics_staging under RLS scoped to *org_id*
# ---------------------------------------------------------------------------


async def _staged_rows(app_engine: AsyncEngine, org_id: uuid.UUID | None) -> list[tuple]:
    """Return (event_id, event_type, payload, recorded_at) rows visible to a
    session whose RLS context is set to *org_id* (or unscoped when None)."""

    factory = async_sessionmaker(app_engine, expire_on_commit=False)
    async with factory() as session, session.begin():
        if org_id is not None:
            await session.execute(
                text("SELECT set_config('app.organisation_id', :oid, true)"),
                {"oid": str(org_id)},
            )
        result = await session.execute(
            text("SELECT event_id, event_type, payload, recorded_at FROM metrics_staging ORDER BY event_id")
        )
        return [(r[0], r[1], r[2], r[3]) for r in result.all()]


async def _post(client: AsyncClient, token: str, events: list[dict]) -> Response:
    return await client.post(
        "/api/v1/metrics/events",
        json={"events": events},
        headers={"Authorization": f"Bearer {token}"},
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestRoundTripWrite:
    async def test_event_is_written_with_correct_columns(
        self,
        integration_client: AsyncClient,
        app_engine: AsyncEngine,
        org_a: uuid.UUID,
        user_a: uuid.UUID,
    ) -> None:
        token = _token(org_a, user_a)
        payload = {"name": "alpha", "count": 3}
        resp = await _post(
            integration_client,
            token,
            [{"event_id": "rt-1", "event_type": "pipeline_created", "payload": payload}],
        )
        assert resp.status_code == 204, f"Expected 204, got {resp.status_code}: {resp.text}"

        rows = await _staged_rows(app_engine, org_a)
        assert len(rows) == 1, f"exactly one staged row expected, got {rows}"
        event_id, event_type, written_payload, recorded_at = rows[0]
        assert event_id == "rt-1"
        assert event_type == "pipeline_created"
        assert written_payload == payload, "payload must be written verbatim"
        assert isinstance(recorded_at, datetime)
        assert recorded_at.tzinfo is not None, "recorded_at must be timezone-aware"


class TestCrossTenantIsolation:
    async def test_org_b_cannot_read_org_a_rows(
        self,
        integration_client: AsyncClient,
        app_engine: AsyncEngine,
        org_a: uuid.UUID,
        org_b: uuid.UUID,
        user_a: uuid.UUID,
        user_b: uuid.UUID,
    ) -> None:
        token_a = _token(org_a, user_a)
        resp = await _post(
            integration_client,
            token_a,
            [{"event_id": "iso-1", "event_type": "pipeline_created"}],
        )
        assert resp.status_code == 204, f"Expected 204, got {resp.status_code}: {resp.text}"

        # Org A can read its own row. The metrics_staging table is shared across
        # the module-scoped org_a, so assert on the specific event_id rather than
        # an exact row count (other tests in this module also write to org_a).
        rows_a = await _staged_rows(app_engine, org_a)
        assert any(r[0] == "iso-1" for r in rows_a), "org A should be able to read its own staged row iso-1"

        # Org B's scoped session must see ZERO rows — the rls_org_isolation
        # policy must confine reads to the authenticated org.
        rows_b = await _staged_rows(app_engine, org_b)
        assert rows_b == [], "org B must never see org A's staged rows"

    async def test_write_is_scoped_to_authenticated_org(
        self,
        integration_client: AsyncClient,
        app_engine: AsyncEngine,
        org_a: uuid.UUID,
        org_b: uuid.UUID,
        user_a: uuid.UUID,
        user_b: uuid.UUID,
    ) -> None:
        """A request authenticated as org B cannot land a row in org A.

        ``organisation_id`` is taken from the authenticated principal (never the
        request body), so the write path is scoped to the caller — a confused /
        malicious client authenticated as org B cannot inject into org A.
        """
        token_b = _token(org_b, user_b)
        resp = await _post(
            integration_client,
            token_b,
            [{"event_id": "scope-1", "event_type": "pipeline_created"}],
        )
        assert resp.status_code == 204, f"Expected 204, got {resp.status_code}: {resp.text}"

        rows_b = await _staged_rows(app_engine, org_b)
        assert len(rows_b) == 1, "org B should have exactly one staged row"
        assert rows_b[0][0] == "scope-1", "org B's staged row should be scope-1"
        # Org A must remain untouched by org B's write.
        rows_a = await _staged_rows(app_engine, org_a)
        assert all(r[0] != "scope-1" for r in rows_a), "org B's write leaked into org A"


class TestDedup:
    async def test_duplicate_event_id_inserts_once(
        self,
        integration_client: AsyncClient,
        app_engine: AsyncEngine,
        org_a: uuid.UUID,
        user_a: uuid.UUID,
    ) -> None:
        """The ``on_conflict_do_nothing`` must honour the real unique
        constraint ``uq_metrics_staging_org_event_id`` — two inserts with the
        same (org, event_id) yield exactly one staged row."""
        token = _token(org_a, user_a)
        for _ in range(2):
            resp = await _post(
                integration_client,
                token,
                [{"event_id": "dup-1", "event_type": "pipeline_created"}],
            )
            assert resp.status_code == 204, f"Expected 204, got {resp.status_code}: {resp.text}"

        rows = await _staged_rows(app_engine, org_a)
        dup_rows = [r for r in rows if r[0] == "dup-1"]
        assert len(dup_rows) == 1, f"duplicate event_id must collapse to one row, got {dup_rows}"


class TestRouteSanitizerPersisted:
    async def test_unmatched_route_persisted_as_unknown(
        self,
        integration_client: AsyncClient,
        app_engine: AsyncEngine,
        org_a: uuid.UUID,
        user_a: uuid.UUID,
    ) -> None:
        token = _token(org_a, user_a)
        resp = await _post(
            integration_client,
            token,
            [
                {
                    "event_id": "san-1",
                    "event_type": "api_error",
                    "payload": {"route": "/some/unknown/path", "status": 500},
                }
            ],
        )
        assert resp.status_code == 204, f"Expected 204, got {resp.status_code}: {resp.text}"

        rows = await _staged_rows(app_engine, org_a)
        row = next((r for r in rows if r[0] == "san-1"), None)
        assert row is not None, "sanitized event must be staged"
        written_payload = row[2]
        assert written_payload["route"] == "unknown", (
            "unmatched route must be sanitized to 'unknown' in the persisted payload"
        )
        assert written_payload["status"] == 500, "non-route fields must be preserved"


class TestApiErrorDailyCapRealDb:
    async def test_cap_enforced_against_real_row_count(
        self,
        integration_client: AsyncClient,
        app_engine: AsyncEngine,
        org_a: uuid.UUID,
        user_a: uuid.UUID,
    ) -> None:
        """The daily api_error cap is enforced against the real staged count.

        Seed 100 api_error rows for today, then post 5 more — all 5 must be
        skipped (the cap guard ``continue``s before staging), so org A gains no
        new api_error rows.
        """
        token = _token(org_a, user_a)
        seed = [{"event_id": f"capseed-{i}", "event_type": "api_error", "payload": {}} for i in range(100)]
        resp = await _post(integration_client, token, seed)
        assert resp.status_code == 204, f"Expected 204, got {resp.status_code}: {resp.text}"

        before = await _staged_rows(app_engine, org_a)
        before_api_errors = sum(1 for r in before if r[1] == "api_error")

        resp = await _post(
            integration_client,
            token,
            [{"event_id": f"capover-{i}", "event_type": "api_error", "payload": {}} for i in range(5)],
        )
        assert resp.status_code == 204, f"Expected 204, got {resp.status_code}: {resp.text}"

        after = await _staged_rows(app_engine, org_a)
        after_api_errors = sum(1 for r in after if r[1] == "api_error")
        assert after_api_errors == before_api_errors, (
            f"api_error events over the cap must be skipped (before={before_api_errors}, after={after_api_errors})"
        )


class TestRlsBlocksCrossTenantInsert:
    async def test_policy_rejects_forged_org_insert(
        self,
        db_engine: AsyncEngine,
        org_a: uuid.UUID,
        org_b: uuid.UUID,
    ) -> None:
        """The ``rls_org_isolation`` policy must block a cross-tenant INSERT.

        The policy is declared ``USING (organisation_id = app.organisation_id)``
        with no explicit ``WITH CHECK``. For INSERT the ``USING`` clause doubles
        as the WITH CHECK, so a non-superuser session running under org A's RLS
        context must be unable to insert a row stamped with org B's id.

        This is the brand-new infra called out in review: the API scoping test
        (``TestCrossTenantIsolation.test_write_is_scoped_to_authenticated_org``)
        proves the *application* never sends a forged org id, but only a direct
        DB-level assertion proves the *policy itself* rejects it. Mirrors the
        non-superuser ``SET ROLE`` pattern from ``test_rls_isolation.py``.
        """
        role = f"test_rls_insert_{uuid.uuid4().hex[:8]}"
        forged_event = f"forged-{uuid.uuid4().hex[:8]}"

        async with db_engine.connect() as conn:
            await conn.execute(text(f'CREATE ROLE "{role}"'))
            await conn.execute(text(f'GRANT INSERT, SELECT ON metrics_staging, organisations TO "{role}"'))
            await conn.execute(text("COMMIT"))

        try:
            # The insert under org A's RLS context with a forged org B id must be
            # rejected by the policy's WITH CHECK.
            async with db_engine.connect() as conn, conn.begin():
                await conn.execute(text(f'SET LOCAL ROLE "{role}"'))
                await conn.execute(
                    text("SELECT set_config('app.organisation_id', :oid, true)"),
                    {"oid": str(org_a)},
                )
                with pytest.raises(SQLAlchemyError):
                    await conn.execute(
                        text(
                            "INSERT INTO metrics_staging (organisation_id, event_id, event_type) "
                            "VALUES (:oid, :eid, 'pipeline_created')"
                        ),
                        {"oid": str(org_b), "eid": forged_event},
                    )

            # As superuser (RLS bypassed) confirm no row was ever written.
            async with db_engine.connect() as conn, conn.begin():
                written = (
                    await conn.execute(
                        text("SELECT count(*) FROM metrics_staging WHERE event_id = :eid"),
                        {"eid": forged_event},
                    )
                ).scalar()
            assert written == 0, "forged cross-tenant insert must not be written"
        finally:
            async with db_engine.connect() as conn:
                await conn.execute(text(f'DROP OWNED BY "{role}"'))
                await conn.execute(text(f'DROP ROLE IF EXISTS "{role}"'))
                await conn.execute(text("COMMIT"))
