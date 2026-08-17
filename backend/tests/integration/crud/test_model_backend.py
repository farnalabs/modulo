"""Integration tests for ModelBackend CRUD.

RLS is set to test_org; all ORM changes are rolled back after each test.
"""

import uuid
from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from modulo.auth.jwt import create_access_token
from modulo.db.crud.model_backend import (
    create_model_backend,
    delete_model_backend,
    get_model_backend,
    list_model_backends,
    update_model_backend,
)
from modulo.db.models.model_backend import ModelBackend

pytestmark = pytest.mark.integration

_VALID_32 = "a" * 32
_VALID_FERNET_KEY = "vK-xU7GqHLflg_GqzJ1FqWI7pHWoHSIyukf4wx-tMHI="


def _mb_kwargs(test_org: uuid.UUID, test_user: uuid.UUID, *, suffix: str = "") -> dict:
    return {
        "org_id": test_org,
        "name": f"TestBackend{suffix}",
        "display_name": "Test Backend",
        "provider": "anthropic",
        "model_id": "stub-model",
        "credentials_ciphertext": b"fake-encrypted-key",
        "account_id": test_user,
    }


async def test_create_model_backend(rls_session: AsyncSession, test_org: uuid.UUID, test_user: uuid.UUID) -> None:
    mb = await create_model_backend(rls_session, **_mb_kwargs(test_org, test_user))
    assert mb.id is not None
    assert mb.provider == "anthropic"
    assert mb.organisation_id == test_org


async def test_get_model_backend_returns_existing(
    rls_session: AsyncSession,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    mb = await create_model_backend(rls_session, **_mb_kwargs(test_org, test_user, suffix="-fetch"))
    fetched = await get_model_backend(rls_session, mb.id)
    assert fetched is not None
    assert fetched.id == mb.id


async def test_get_model_backend_returns_none_for_unknown(rls_session: AsyncSession) -> None:
    assert await get_model_backend(rls_session, uuid.uuid4()) is None


async def test_list_model_backends_pagination(
    rls_session: AsyncSession,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    for i in range(3):
        await create_model_backend(
            rls_session,
            **_mb_kwargs(test_org, test_user, suffix=f"-list-{i}-{uuid.uuid4().hex[:4]}"),
        )
    page1 = await list_model_backends(rls_session, org_id=test_org, page=1, page_size=2)
    assert page1.total >= 3
    assert len(page1.items) == 2
    assert page1.page == 1


async def test_update_model_backend(rls_session: AsyncSession, test_org: uuid.UUID, test_user: uuid.UUID) -> None:
    mb = await create_model_backend(rls_session, **_mb_kwargs(test_org, test_user, suffix="-upd"))
    updated = await update_model_backend(rls_session, mb.id, {"display_name": "Updated Name"})
    assert updated is not None
    assert updated.display_name == "Updated Name"


async def test_update_model_backend_unknown_returns_none(rls_session: AsyncSession) -> None:
    assert await update_model_backend(rls_session, uuid.uuid4(), {"display_name": "x"}) is None


async def test_delete_model_backend(rls_session: AsyncSession, test_org: uuid.UUID, test_user: uuid.UUID) -> None:
    mb = await create_model_backend(rls_session, **_mb_kwargs(test_org, test_user, suffix="-del"))
    assert await delete_model_backend(rls_session, mb.id) is True
    assert await get_model_backend(rls_session, mb.id) is None


async def test_delete_model_backend_unknown_returns_false(rls_session: AsyncSession) -> None:
    assert await delete_model_backend(rls_session, uuid.uuid4()) is False


class TestListModelBackendsTierFiltering:
    """Server-side tier filtering for list_model_backends."""

    async def _create_with_tier(
        self,
        rls_session: AsyncSession,
        test_org: uuid.UUID,
        test_user: uuid.UUID,
        tier: str,
        suffix: str,
    ) -> None:
        await create_model_backend(
            rls_session,
            tier=tier,
            **_mb_kwargs(test_org, test_user, suffix=suffix),
        )

    async def test_default_excludes_in_dev(
        self,
        rls_session: AsyncSession,
        test_org: uuid.UUID,
        test_user: uuid.UUID,
    ) -> None:
        await self._create_with_tier(rls_session, test_org, test_user, "in_dev", "-tier-dev")
        await self._create_with_tier(rls_session, test_org, test_user, "preview", "-tier-prev")
        await self._create_with_tier(rls_session, test_org, test_user, "native", "-tier-nat")
        result = await list_model_backends(rls_session, org_id=test_org)
        assert result.total == 2
        assert all(i.tier != "in_dev" for i in result.items)

    async def test_explicit_excluded_tiers_in_dev(
        self,
        rls_session: AsyncSession,
        test_org: uuid.UUID,
        test_user: uuid.UUID,
    ) -> None:
        await self._create_with_tier(rls_session, test_org, test_user, "in_dev", "-tier2-dev")
        await self._create_with_tier(rls_session, test_org, test_user, "native", "-tier2-nat")
        result = await list_model_backends(rls_session, org_id=test_org, excluded_tiers=["in_dev"])
        assert result.total == 1
        assert result.items[0].tier == "native"

    async def test_excluded_tiers_none_defaults_to_in_dev(
        self,
        rls_session: AsyncSession,
        test_org: uuid.UUID,
        test_user: uuid.UUID,
    ) -> None:
        await self._create_with_tier(rls_session, test_org, test_user, "in_dev", "-tier3-dev")
        await self._create_with_tier(rls_session, test_org, test_user, "native", "-tier3-nat")
        result = await list_model_backends(rls_session, org_id=test_org, excluded_tiers=None)
        assert result.total == 1
        assert result.items[0].tier == "native"

    async def test_excluded_tiers_empty_skips_filter(
        self,
        rls_session: AsyncSession,
        test_org: uuid.UUID,
        test_user: uuid.UUID,
    ) -> None:
        await self._create_with_tier(rls_session, test_org, test_user, "in_dev", "-tier4-dev")
        await self._create_with_tier(rls_session, test_org, test_user, "native", "-tier4-nat")
        result = await list_model_backends(rls_session, org_id=test_org, excluded_tiers=[])
        assert result.total == 2

    async def test_excluded_tiers_preview(
        self,
        rls_session: AsyncSession,
        test_org: uuid.UUID,
        test_user: uuid.UUID,
    ) -> None:
        await self._create_with_tier(rls_session, test_org, test_user, "in_dev", "-tier5-dev")
        await self._create_with_tier(rls_session, test_org, test_user, "preview", "-tier5-prev")
        await self._create_with_tier(rls_session, test_org, test_user, "native", "-tier5-nat")
        result = await list_model_backends(rls_session, org_id=test_org, excluded_tiers=["preview"])
        assert result.total == 2
        assert all(i.tier != "preview" for i in result.items)
        tiers = {i.tier for i in result.items}
        assert tiers == {"in_dev", "native"}


# ---------------------------------------------------------------------------
# Real-endpoint round-trip: PATCH fallback_backend_ids through the HTTP API
# against the real database. Uses its own org so the shared test_org is never
# polluted with committed rows (the CRUD integration tests above assert exact
# org-wide counts).
# ---------------------------------------------------------------------------


async def _seed_org_and_admin(db_engine: AsyncEngine) -> tuple[uuid.UUID, uuid.UUID]:
    org_id = uuid.uuid4()
    account_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text("INSERT INTO organisations (id, name, slug, settings_json) VALUES (:id, :name, :slug, '{}'::json)"),
            {"id": str(org_id), "name": f"MB-{org_id.hex[:8]}", "slug": f"mb-{org_id.hex[:8]}"},
        )
        await conn.execute(
            text(
                "INSERT INTO accounts (id, email, display_name, password_hash, "
                "auth_provider, active) VALUES (:id, :email, :name, 'hash', 'local', true)",
            ),
            {"id": str(account_id), "email": f"mb-{account_id.hex[:8]}@test.local", "name": "MB User"},
        )
        await conn.execute(
            text(
                "INSERT INTO org_memberships (id, account_id, organisation_id, role) "
                "VALUES (:mid, :aid, :oid, 'admin')",
            ),
            {"mid": str(uuid.uuid4()), "aid": str(account_id), "oid": str(org_id)},
        )
    return org_id, account_id


async def _seed_backends(db_engine: AsyncEngine, org_id: uuid.UUID, user_id: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID]:
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session:
        primary = await create_model_backend(
            session,
            org_id=org_id,
            name=f"primary-{uuid.uuid4().hex[:6]}",
            display_name="Primary",
            provider="openai",
            model_id="gpt-4o",
            credentials_ciphertext=b"encrypted",
            account_id=user_id,
        )
        fallback = await create_model_backend(
            session,
            org_id=org_id,
            name=f"fallback-{uuid.uuid4().hex[:6]}",
            display_name="Fallback",
            provider="anthropic",
            model_id="claude",
            credentials_ciphertext=b"encrypted",
            account_id=user_id,
        )
        await session.commit()
        return primary.id, fallback.id


def _token(org_id: uuid.UUID, user_id: uuid.UUID) -> str:
    return create_access_token(
        subject=f"user-{user_id.hex[:8]}",
        secret_key=_VALID_32,
        organisation_id=str(org_id),
        account_id=str(user_id),
        org_role="admin",
    )


@pytest_asyncio.fixture
async def model_backend_client(db_url: str, app_engine: AsyncEngine) -> AsyncGenerator[AsyncClient, None]:
    """HTTP client wired to the real (testcontainer) database via the app_engine role."""
    from modulo.api.dependencies import _get_engine, get_db_session
    from modulo.api.main import app
    from modulo.settings import Settings, get_settings

    settings = Settings(
        database_url=db_url,
        secret_key=_VALID_32,
        fernet_key=_VALID_FERNET_KEY,
        modulo_csrf_enabled=False,
        modulo_auth_rate_limit_enabled=False,
        redis_url="",
        modulo_admin_password="",
    )

    async def override_session() -> AsyncGenerator[AsyncSession, None]:
        factory = async_sessionmaker(app_engine, expire_on_commit=False)
        async with factory() as session:
            yield session

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[_get_engine] = lambda: app_engine
    app.dependency_overrides[get_db_session] = override_session

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", timeout=30.0) as client:
        yield client

    app.dependency_overrides.clear()


async def test_update_model_backend_with_fallback_round_trips_real_endpoint(
    db_engine: AsyncEngine,
    model_backend_client: AsyncClient,
) -> None:
    """PATCH fallback_backend_ids through the real endpoint against the real DB.

    Regression: the update route previously passed raw ``uuid.UUID`` objects
    through ``update_model_backend`` into the ``JSON`` column, whose default
    serializer raises ``TypeError: Object of type UUID is not JSON
    serializable`` — the flush 500'd (via the generic ``except Exception``).
    The route now stringifies the ids (mirroring the create path); this test
    proves the real payload shape round-trips through storage and back out the
    endpoint.
    """
    org_id, user_id = await _seed_org_and_admin(db_engine)
    primary_id, fallback_id = await _seed_backends(db_engine, org_id, user_id)

    headers = {"Authorization": f"Bearer {_token(org_id, user_id)}"}
    resp = await model_backend_client.patch(
        f"/api/v1/model-backends/{primary_id}",
        json={"fallback_backend_ids": [str(fallback_id)]},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["fallback_backend_ids"] == [str(fallback_id)]

    # The JSON column must store stringified ids, not raw uuid.UUID objects.
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session:
        row = await session.get(ModelBackend, primary_id)
        assert row is not None
        assert row.fallback_backend_ids == [str(fallback_id)]
        assert all(isinstance(fid, str) for fid in row.fallback_backend_ids)

    # The delete-protection 409 is enforced against the stored reference.
    del_resp = await model_backend_client.delete(
        f"/api/v1/model-backends/{fallback_id}",
        headers=headers,
    )
    assert del_resp.status_code == 409
    assert "primary-" in del_resp.json()["detail"]


async def _seed_pipeline_snapshot_run(
    db_engine: AsyncEngine,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    backend_id: uuid.UUID,
    run_status: str,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Seed a pipeline, a snapshot pinned to ``backend_id``, and a run in
    ``run_status`` in the given org, returning ``(snapshot_id, run_id)``.

    Inserted via the raw ``db_engine`` (superuser) connection so the rows are
    owned by the seeded org and remain visible to the RLS-context endpoint.
    """
    pipeline_id, snapshot_id, run_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO pipelines (id, organisation_id, name, account_id, "
                "max_concurrent_runs, lock_wait_timeout_seconds, node_timeout_seconds, "
                "run_context_defaults, graph_nodes_json) "
                "VALUES (:id, :oid, :name, :uid, 10, 300, 300, '{}'::json, '[]'::json)",
            ),
            {
                "id": str(pipeline_id),
                "oid": str(org_id),
                "name": f"snap-pipe-{uuid.uuid4().hex[:6]}",
                "uid": str(user_id),
            },
        )
        await conn.execute(
            text(
                "INSERT INTO pipeline_snapshots (id, pipeline_id, organisation_id, "
                "snapshot_version, graph_json, connector_bindings_json, "
                "schema_pins_json, prompt_pins_json, model_backend_pins_json, "
                "run_context_defaults, config_json) "
                "VALUES (:id, :pid, :oid, 1, '{}'::json, '[]'::json, "
                "'[]'::json, '[]'::json, :mb_pins::json, '{}'::json, '{}'::json)",
            ),
            {
                "id": str(snapshot_id),
                "pid": str(pipeline_id),
                "oid": str(org_id),
                "mb_pins": str([{"model_backend_id": str(backend_id)}]),
            },
        )
        await conn.execute(
            text(
                "INSERT INTO runs (id, organisation_id, pipeline_id, snapshot_id, "
                "trigger_type, langgraph_thread_id, run_number, input_hash, status) "
                "VALUES (:id, :oid, :pid, :sid, 'manual', :thread, 1, :hash, :status)",
            ),
            {
                "id": str(run_id),
                "oid": str(org_id),
                "pid": str(pipeline_id),
                "sid": str(snapshot_id),
                "thread": str(uuid.uuid4()),
                "hash": "0" * 64,
                "status": run_status,
            },
        )
    return snapshot_id, run_id


async def test_delete_model_backend_blocked_by_non_terminal_snapshot_run_real_endpoint(
    db_engine: AsyncEngine,
    model_backend_client: AsyncClient,
) -> None:
    """PRD §8.1 deletion protection — the REAL query path: a snapshot pinned to
    a backend and tied to a NON-terminal run hard-deletes are rejected (409).

    Unlike the unit tests (which patch ``_list_snapshots_referencing_backend``
    and therefore never exercise the terminal-vs-non-terminal filter), this
    drives the actual JOIN of ``PipelineSnapshot`` -> ``Run`` with a real
    ``awaiting_human`` run row, proving the ``NOT IN TERMINAL_STATUSES`` filter
    and the ``model_backend_pins_json`` match end-to-end.
    """
    org_id, user_id = await _seed_org_and_admin(db_engine)
    _, backend_id = await _seed_backends(db_engine, org_id, user_id)
    await _seed_pipeline_snapshot_run(db_engine, org_id, user_id, backend_id, run_status="awaiting_human")

    headers = {"Authorization": f"Bearer {_token(org_id, user_id)}"}
    resp = await model_backend_client.delete(f"/api/v1/model-backends/{backend_id}", headers=headers)
    assert resp.status_code == 409, resp.text
    assert "non-terminal run" in resp.json()["detail"]


async def test_delete_model_backend_allowed_when_snapshot_runs_terminal_real_endpoint(
    db_engine: AsyncEngine,
    model_backend_client: AsyncClient,
) -> None:
    """A backend pinned only by snapshots tied to TERMINAL runs hard-deletes
    (204) via the real delete endpoint + real query.

    This is the distinguishing security behaviour the unit tests mock around:
    with a real ``complete`` run row the query must return nothing so the
    deletion protection does NOT block. A regression where the ``NOT IN
    TERMINAL_STATUSES`` filter were inverted would (correctly) fail this test.
    """
    org_id, user_id = await _seed_org_and_admin(db_engine)
    _, backend_id = await _seed_backends(db_engine, org_id, user_id)
    await _seed_pipeline_snapshot_run(db_engine, org_id, user_id, backend_id, run_status="complete")

    headers = {"Authorization": f"Bearer {_token(org_id, user_id)}"}
    resp = await model_backend_client.delete(f"/api/v1/model-backends/{backend_id}", headers=headers)
    assert resp.status_code == 204, resp.text


async def test_update_model_backend_self_reference_rejected_real_endpoint(
    db_engine: AsyncEngine,
    model_backend_client: AsyncClient,
) -> None:
    """A backend must not reference itself as a fallback (422) via the real endpoint.

    A self-referencing chain would permanently block deletion (the
    delete-protection scan reports the backend referencing itself), so it is
    rejected before any DB write.
    """
    org_id, user_id = await _seed_org_and_admin(db_engine)
    primary_id, _ = await _seed_backends(db_engine, org_id, user_id)

    headers = {"Authorization": f"Bearer {_token(org_id, user_id)}"}
    resp = await model_backend_client.patch(
        f"/api/v1/model-backends/{primary_id}",
        json={"fallback_backend_ids": [str(primary_id)]},
        headers=headers,
    )
    assert resp.status_code == 422, resp.text
    assert "cannot reference itself" in resp.text
