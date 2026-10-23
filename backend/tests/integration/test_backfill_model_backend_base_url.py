"""Real-Postgres tests for the FAR-2067 legacy base_url backfill CLI.

These prove the destructive contract end to end: a dry run writes nothing, the
dry-run report matches what ``--apply`` then does, the API key survives, a
second run is a no-op, and one organisation's run can neither see nor write
another organisation's rows (the CLI sets RLS context per org and the secrets
backend is org-scoped).

Docker / testcontainers is required; when it is unavailable the integration
conftest skips these loudly (and fails hard in CI).
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from modulo.cli.backfill_model_backend_base_url import BackfillResult, run_backfill
from modulo.settings import get_settings

pytestmark = pytest.mark.integration

_HTTP_LEGACY = "http://legacy.internal.example/v1"
_HTTPS_LEGACY = "https://legacy.internal.example/v1"
_CURRENT_ENDPOINT = "https://current.internal.example/v1"


def _fernet() -> Fernet:
    return Fernet(get_settings().fernet_key.encode())


async def _create_org(engine: AsyncEngine) -> uuid.UUID:
    org_id = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO organisations (id, name, slug, settings_json) VALUES (:id, :name, :slug, '{}'::json)"),
            {"id": str(org_id), "name": f"Backfill {org_id.hex[:8]}", "slug": f"backfill-{org_id.hex[:8]}"},
        )
    return org_id


async def _create_account(engine: AsyncEngine) -> uuid.UUID:
    account_id = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO accounts (id, email, display_name, password_hash, auth_provider, active) "
                "VALUES (:id, :email, :name, 'hash', 'local', true)"
            ),
            {"id": str(account_id), "email": f"backfill-{account_id.hex[:12]}@example.com", "name": "Backfill"},
        )
    return account_id


async def _create_backend(
    engine: AsyncEngine,
    org_id: uuid.UUID,
    account_id: uuid.UUID,
    *,
    provider: str = "openai",
    default_params: dict[str, Any] | None = None,
    api_key: str = "sk-LEGACY-KEY",
    seed_secret: dict[str, Any] | None = None,
) -> uuid.UUID:
    """Insert a legacy-shaped backend (ciphertext-only credential by default)."""
    backend_id = uuid.uuid4()
    ciphertext = _fernet().encrypt(api_key.encode())
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO model_backends (id, organisation_id, name, display_name, provider, model_id, "
                "credentials_ciphertext, account_id, default_params, cost_tracking, visibility, tier) "
                "VALUES (:id, :oid, :name, 'Backfill', :provider, 'model-1', :ciphertext, :aid, "
                "CAST(:params AS jsonb), 'enabled', 'org', 'native')"
            ),
            {
                "id": str(backend_id),
                "oid": str(org_id),
                "name": f"backend-{backend_id.hex[:10]}",
                "provider": provider,
                "ciphertext": ciphertext,
                "aid": str(account_id),
                "params": json.dumps(default_params or {}),
            },
        )
        if seed_secret is not None:
            await conn.execute(
                text(
                    "INSERT INTO secrets (id, organisation_id, key, encrypted_value) VALUES (:id, :oid, :key, :value)"
                ),
                {
                    "id": str(uuid.uuid4()),
                    "oid": str(org_id),
                    "key": str(backend_id),
                    "value": _fernet().encrypt(json.dumps(seed_secret).encode()),
                },
            )
    return backend_id


def _session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, autobegin=False)


async def _legacy_count(engine: AsyncEngine, org_id: uuid.UUID) -> int:
    async with engine.connect() as conn:
        result = await conn.execute(
            text("SELECT count(*) FROM model_backends WHERE organisation_id = :oid AND default_params ? 'base_url'"),
            {"oid": str(org_id)},
        )
        return int(result.scalar_one())


async def _default_params(engine: AsyncEngine, backend_id: uuid.UUID) -> dict[str, Any]:
    async with engine.connect() as conn:
        result = await conn.execute(
            text("SELECT default_params FROM model_backends WHERE id = :id"),
            {"id": str(backend_id)},
        )
        params = result.scalar_one()
    return json.loads(params) if isinstance(params, str) else params


async def _secret(engine: AsyncEngine, org_id: uuid.UUID, backend_id: uuid.UUID) -> dict[str, Any]:
    async with engine.connect() as conn:
        result = await conn.execute(
            text("SELECT encrypted_value FROM secrets WHERE organisation_id = :oid AND key = :key"),
            {"oid": str(org_id), "key": str(backend_id)},
        )
        encrypted = result.scalar_one()
    return json.loads(_fernet().decrypt(encrypted).decode())


async def _run(
    engine: AsyncEngine,
    *,
    apply: bool,
    org_id: uuid.UUID | None = None,
) -> BackfillResult:
    return await run_backfill(
        apply=apply,
        organisation_id=org_id,
        settings=get_settings(),
        session_factory=_session_factory(engine),
    )


@pytest_asyncio.fixture
async def org_and_backend(db_engine: AsyncEngine) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    org_id = await _create_org(db_engine)
    account_id = await _create_account(db_engine)
    backend_id = await _create_backend(
        db_engine,
        org_id,
        account_id,
        default_params={"base_url": _HTTPS_LEGACY, "temperature": 0.2},
        api_key="sk-ORG-KEY",
        seed_secret={"api_key": "sk-ORG-KEY"},
    )
    return org_id, backend_id, account_id


async def test_dry_run_writes_nothing_then_apply_migrates(
    db_engine: AsyncEngine,
    app_engine: AsyncEngine,
    org_and_backend: tuple[uuid.UUID, uuid.UUID, uuid.UUID],
) -> None:
    org_id, backend_id, _ = org_and_backend

    dry = await _run(app_engine, apply=False, org_id=org_id)
    assert [row.outcome for row in dry.rows] == ["migrate"]
    assert dry.rows[0].host == "legacy.internal.example"
    assert not dry.requires_attention

    # Dry run wrote neither the secret nor default_params.
    assert await _legacy_count(db_engine, org_id) == 1
    assert await _default_params(db_engine, backend_id) == {"base_url": _HTTPS_LEGACY, "temperature": 0.2}
    assert await _secret(db_engine, org_id, backend_id) == {"api_key": "sk-ORG-KEY"}

    applied = await _run(app_engine, apply=True, org_id=org_id)
    assert [row.outcome for row in applied.rows] == ["migrate"]

    assert await _legacy_count(db_engine, org_id) == 0
    assert await _default_params(db_engine, backend_id) == {"temperature": 0.2}
    # The API key survives and the effective endpoint is unchanged.
    assert await _secret(db_engine, org_id, backend_id) == {
        "api_key": "sk-ORG-KEY",
        "base_url": _HTTPS_LEGACY,
    }

    again = await _run(app_engine, apply=True, org_id=org_id)
    assert again.considered == 0
    assert not again.requires_attention


async def test_each_classification_applies_its_own_rule(
    db_engine: AsyncEngine,
    app_engine: AsyncEngine,
) -> None:
    org_id = await _create_org(db_engine)
    account_id = await _create_account(db_engine)
    http_id = await _create_backend(
        db_engine,
        org_id,
        account_id,
        default_params={"base_url": _HTTP_LEGACY},
        seed_secret={"api_key": "sk-HTTP"},
    )
    invalid_id = await _create_backend(
        db_engine,
        org_id,
        account_id,
        default_params={"base_url": "ftp://legacy.internal.example/v1"},
        seed_secret={"api_key": "sk-INVALID"},
    )
    incapable_id = await _create_backend(
        db_engine,
        org_id,
        account_id,
        provider="azure_openai",
        default_params={"base_url": _HTTPS_LEGACY},
        seed_secret={"api_key": "sk-INCAPPABLE"},
    )
    dead_key_id = await _create_backend(
        db_engine,
        org_id,
        account_id,
        default_params={"base_url": "   "},
        seed_secret={"api_key": "sk-DEAD"},
    )

    dry = await _run(app_engine, apply=False, org_id=org_id)
    by_backend = {row.backend_id: row for row in dry.rows}
    assert by_backend[http_id].outcome == "strip-insecure"
    assert by_backend[invalid_id].outcome == "reject-invalid"
    assert by_backend[incapable_id].outcome == "drop-incapable"
    assert by_backend[dead_key_id].outcome == "skip"
    assert dry.requires_attention

    applied = await _run(app_engine, apply=True, org_id=org_id)
    # The dry-run classification is exactly what apply did.
    assert {row.backend_id: row.outcome for row in applied.rows} == {row.backend_id: row.outcome for row in dry.rows}
    assert await _legacy_count(db_engine, org_id) == 0
    # None of these rows may be auto-blessed or narrowed: credentials untouched.
    assert await _secret(db_engine, org_id, http_id) == {"api_key": "sk-HTTP"}
    assert await _secret(db_engine, org_id, invalid_id) == {"api_key": "sk-INVALID"}
    assert await _secret(db_engine, org_id, incapable_id) == {"api_key": "sk-INCAPPABLE"}
    assert await _secret(db_engine, org_id, dead_key_id) == {"api_key": "sk-DEAD"}


async def test_recovers_a_ciphertext_only_api_key_before_migrating(
    db_engine: AsyncEngine,
    app_engine: AsyncEngine,
) -> None:
    org_id = await _create_org(db_engine)
    account_id = await _create_account(db_engine)
    backend_id = await _create_backend(
        db_engine,
        org_id,
        account_id,
        default_params={"base_url": _HTTPS_LEGACY},
        api_key="sk-CIPHERTEXT-ONLY",
        seed_secret=None,
    )

    result = await _run(app_engine, apply=True, org_id=org_id)
    assert [row.outcome for row in result.rows] == ["migrate"]
    assert await _secret(db_engine, org_id, backend_id) == {
        "api_key": "sk-CIPHERTEXT-ONLY",
        "base_url": _HTTPS_LEGACY,
    }
    assert await _legacy_count(db_engine, org_id) == 0


async def test_preserves_an_already_stored_credential_endpoint(
    db_engine: AsyncEngine,
    app_engine: AsyncEngine,
) -> None:
    org_id = await _create_org(db_engine)
    account_id = await _create_account(db_engine)
    backend_id = await _create_backend(
        db_engine,
        org_id,
        account_id,
        default_params={"base_url": _HTTPS_LEGACY},
        seed_secret={"api_key": "sk-KEY", "base_url": _CURRENT_ENDPOINT},
    )

    result = await _run(app_engine, apply=True, org_id=org_id)
    assert [row.outcome for row in result.rows] == ["migrate"]
    assert "preserved" in (result.rows[0].detail or "")
    # The credential endpoint wins; the effective endpoint never changes.
    assert await _secret(db_engine, org_id, backend_id) == {
        "api_key": "sk-KEY",
        "base_url": _CURRENT_ENDPOINT,
    }
    assert await _legacy_count(db_engine, org_id) == 0


async def test_scoped_run_cannot_see_or_write_another_org(
    db_engine: AsyncEngine,
    app_engine: AsyncEngine,
) -> None:
    org_a = await _create_org(db_engine)
    org_b = await _create_org(db_engine)
    account_a = await _create_account(db_engine)
    account_b = await _create_account(db_engine)
    endpoint_a = "https://a.internal.example/v1"
    endpoint_b = "https://b.internal.example/v1"
    backend_a = await _create_backend(
        db_engine,
        org_a,
        account_a,
        default_params={"base_url": endpoint_a},
        seed_secret={"api_key": "sk-A"},
    )
    backend_b = await _create_backend(
        db_engine,
        org_b,
        account_b,
        default_params={"base_url": endpoint_b},
        seed_secret={"api_key": "sk-B"},
    )

    result_a = await _run(app_engine, apply=True, org_id=org_a)
    assert [row.backend_id for row in result_a.rows] == [backend_a]
    assert await _legacy_count(db_engine, org_a) == 0
    # Org B was not visible to the scoped run and is untouched.
    assert await _legacy_count(db_engine, org_b) == 1
    assert await _secret(db_engine, org_b, backend_b) == {"api_key": "sk-B"}

    result_b = await _run(app_engine, apply=True, org_id=org_b)
    assert [row.backend_id for row in result_b.rows] == [backend_b]
    assert await _legacy_count(db_engine, org_b) == 0

    # Each org's secret was written under its own tenant with its own values.
    assert await _secret(db_engine, org_a, backend_a) == {"api_key": "sk-A", "base_url": endpoint_a}
    assert await _secret(db_engine, org_b, backend_b) == {"api_key": "sk-B", "base_url": endpoint_b}
