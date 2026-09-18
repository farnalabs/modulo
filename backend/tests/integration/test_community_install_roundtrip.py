"""DB-backed round-trip test for community-library install (FAR-363).

Proves the relaxed ``ck_library_primitives_source_fields`` registry arm accepts
a real ``source='registry'`` row written by ``install_community_entry`` with
NULL ``average_rating`` / ``review_count`` / ``ed25519_signature`` (the values
migration 0123 and the model registry arm now allow). Runs against a real
Postgres (testcontainers) with real Alembic migrations applied, and exercises
the full HTTP install endpoint end-to-end.

The blob fetch is monkeypatched (the verifiable content is local), but every
other layer — auth, RLS, the insert through
``create_library_primitive``, and the CHECK-constraint enforcement — is real.
"""

import uuid
from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from modulo.core.library_service import community as community_module
from modulo.core.library_sync.models import SINGLETON_ID, LibrarySyncState

# Reuse the auth/seed helpers defined in an existing integration module instead
# of duplicating them here — the duplicated copies tripped SonarCloud's
# new-code duplication gate (83 new dup lines). The shared ``integration_client``
# ASGI fixture is inherited from ``tests/integration/conftest.py`` (the same
# one every other integration module resolves), so no local copy is needed
# here — pytest fixtures are created automatically when tests request them and
# must never be invoked directly.
from tests.integration.test_guardrail_config_api import (
    _auth_headers,
    _seed_org,
    _seed_user,
)

pytestmark = pytest.mark.integration

ENTRY_ID = "entry-1"
CONTENT_SHA = "a" * 64
MANIFEST = {
    "schema_version": "1",
    "generated_at": "2026-08-22T00:00:00Z",
    "entries": [
        {
            "id": ENTRY_ID,
            "type": "agent",
            "slug": "code-reviewer",
            "author": "acme",
            "version": "1.0.0",
            "content_sha256": CONTENT_SHA,
            "license": "MIT",
            "status": "published",
            "published_at": "2026-08-22T00:00:00Z",
        }
    ],
    "revoked": [],
    "signature": {"algorithm": "ed25519", "value": "deadbeef"},
}

BLOB_CONTENT = {"description": "A test agent", "name": "code-reviewer"}


async def _seed_sync_state(db_engine: AsyncEngine) -> None:
    """Seed the instance-global sync-state singleton with the cached manifest."""
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session:
        await session.execute(delete(LibrarySyncState).where(LibrarySyncState.id == SINGLETON_ID))
        session.add(
            LibrarySyncState(
                id=SINGLETON_ID,
                manifest_json=MANIFEST,
                catalog_json=[],
                last_success_at=None,
                last_error=None,
            )
        )
        await session.commit()


@pytest_asyncio.fixture
async def org(db_engine: AsyncEngine) -> uuid.UUID:
    return await _seed_org(db_engine, "Community-Install")


@pytest_asyncio.fixture
async def user(db_engine: AsyncEngine, org: uuid.UUID) -> uuid.UUID:
    return await _seed_user(db_engine, org, "community-install@test.local")


@pytest_asyncio.fixture
async def seeded(db_engine: AsyncEngine) -> AsyncGenerator[None, None]:
    await _seed_sync_state(db_engine)
    yield


@pytest.fixture
def fake_blob(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _blob(_sha256: str) -> dict:
        return dict(BLOB_CONTENT)

    monkeypatch.setattr(community_module, "_fetch_blob", _blob)


async def test_install_round_trip_returns_201_and_persists_registry_row(
    integration_client: AsyncClient,
    db_engine: AsyncEngine,
    org: uuid.UUID,
    user: uuid.UUID,
    seeded: None,
    fake_blob: None,
) -> None:
    """A real POST /install inserts a relaxed, source='registry' row (201)."""
    resp = await integration_client.post(
        f"/api/v1/libraries/community/{ENTRY_ID}/install",
        json={"target_team_id": None},
        headers=_auth_headers(org, user),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["source"] == "registry"
    assert body["slug"] == "code-reviewer"
    assert body["version"] == "1.0.0"
    assert body["visibility"] == "org"
    assert body["verified"] is True
    assert body["download_count"] == 0
    # The relaxed registry arm allows these to be NULL.
    assert body["average_rating"] is None
    assert body["review_count"] is None
    assert body["ed25519_signature"] is None

    async with db_engine.connect() as conn:
        row = await conn.execute(
            text(
                "SELECT source, average_rating, review_count, ed25519_signature "
                "FROM library_primitives WHERE slug = :slug AND version = :version AND deleted_at IS NULL",
            ),
            {"slug": "code-reviewer", "version": "1.0.0"},
        )
        persisted = row.first()
    assert persisted is not None
    assert persisted[0] == "registry"
    assert persisted[1] is None
    assert persisted[2] is None
    assert persisted[3] is None


async def test_install_is_idempotent_returns_409(
    integration_client: AsyncClient,
    org: uuid.UUID,
    user: uuid.UUID,
    seeded: None,
    fake_blob: None,
) -> None:
    """A second install of the same slug+version returns 409, not a duplicate row."""
    first = await integration_client.post(
        f"/api/v1/libraries/community/{ENTRY_ID}/install",
        json={"target_team_id": None},
        headers=_auth_headers(org, user),
    )
    assert first.status_code == 201, first.text

    second = await integration_client.post(
        f"/api/v1/libraries/community/{ENTRY_ID}/install",
        json={"target_team_id": None},
        headers=_auth_headers(org, user),
    )
    assert second.status_code == 409, second.text
