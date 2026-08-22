"""Integration tests for the product-analytics identity & rotation endpoints.

Covers the full round-trip of a real ``RotateRequest`` through
``POST /api/v1/product-analytics/rotate`` and ``GET /api/v1/product-analytics/identity``:

* the auth gate (system-admin only → 403 otherwise),
* the 401 paths (wrong old secret / bad HMAC),
* the 400 path (out-of-order sequence number → monotonicity guard),
* the 429 path (rotation rate limit exceeded),
* that the secret value is NEVER returned by the identity endpoint, and
* sequence monotonicity across successive rotations.
"""

from __future__ import annotations

import time
import uuid
from collections import defaultdict
from collections.abc import AsyncGenerator
from unittest.mock import patch

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import delete, or_
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from test_metrics_ingest import integration_client  # noqa: F401

from modulo.api.routes.product_analytics_identity import _SEQUENCE_KEY_PREFIX
from modulo.auth.jwt import create_access_token
from modulo.core.product_analytics.hmac_verify import sign_rotation_request
from modulo.core.product_analytics.instance_identity import (
    _INSTANCE_ID_KEY,
    _SECRET_KEY,
    get_or_create_instance_identity,
)
from modulo.db.models.system_config import SystemConfig

pytestmark = pytest.mark.integration

_VALID_32 = "a" * 32


def _token(org_id: uuid.UUID, user_id: uuid.UUID, role: str, is_system_admin: bool = False) -> str:
    return create_access_token(
        subject=f"user-{user_id.hex[:8]}",
        secret_key=_VALID_32,
        organisation_id=str(org_id) if org_id else "",
        account_id=str(user_id),
        org_role=role,
        is_system_admin=is_system_admin,
    )


async def _mint_and_get_secret(app_engine: AsyncEngine) -> tuple[str, str]:
    """Mint (or read) the instance identity and return ``(instance_id, secret)``."""
    factory = async_sessionmaker(app_engine, expire_on_commit=False)
    async with factory() as session, session.begin():
        instance_id, secret = await get_or_create_instance_identity(session)
        return str(instance_id), secret


def _rotate_body(secret: str, instance_id: str, sequence: int, timestamp: float | None = None) -> dict:
    """Build a valid-shaped RotateRequest body signed with ``secret``."""
    ts = timestamp if timestamp is not None else time.time()
    payload = str(instance_id).encode("utf-8")
    mac = sign_rotation_request(secret, payload, ts, sequence)
    return {
        "old_secret": secret,
        "timestamp": ts,
        "sequence": sequence,
        "hmac_digest": mac,
    }


@pytest_asyncio.fixture(autouse=True)
async def _reset_rate_limiter() -> AsyncGenerator[None, None]:
    """Each test starts with a clean in-memory rotation rate-limit window."""
    import modulo.api.routes.product_analytics_identity as pa

    with patch.object(pa, "_rotation_timestamps", defaultdict(list)):
        yield


@pytest_asyncio.fixture(autouse=True)
async def _fresh_instance_identity(db_engine) -> AsyncGenerator[None, None]:
    """Give every test a genuinely fresh TOFU instance-identity state.

    The instance identity (instance id + secret) and the per-instance rotation
    sequence live in GLOBAL ``system_config`` rows keyed by constant values, so
    they survive across tests in the shared session-scoped DB. A rotation test
    leaves ``product_analytics_last_sequence_<instance_id>`` behind; the next
    rotate test then mints the SAME (stale) instance and its first ``seq=1`` is
    rejected with ``Sequence must be > N``. Deleting the three key families
    before every test restores the pristine instance the TOFU/rotation tests
    assume.
    """
    async with db_engine.begin() as conn:
        await conn.execute(
            delete(SystemConfig).where(
                or_(
                    SystemConfig.key == _INSTANCE_ID_KEY,
                    SystemConfig.key == _SECRET_KEY,
                    SystemConfig.key.like(_SEQUENCE_KEY_PREFIX + "%"),
                )
            )
        )
    yield


class TestIdentityAuthGate:
    async def test_identity_requires_system_admin(
        self,
        integration_client: AsyncClient,
        test_org: uuid.UUID,
        test_user: uuid.UUID,
    ) -> None:
        token = _token(test_org, test_user, "admin", is_system_admin=False)
        resp = await integration_client.get(
            "/api/v1/product-analytics/identity",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 403

    async def test_identity_returns_instance_id_and_never_the_secret(
        self,
        integration_client: AsyncClient,
        app_engine: AsyncEngine,
        test_org: uuid.UUID,
        test_user: uuid.UUID,
    ) -> None:
        instance_id, secret = await _mint_and_get_secret(app_engine)
        token = _token(test_org, test_user, "admin", is_system_admin=True)
        resp = await integration_client.get(
            "/api/v1/product-analytics/identity",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["instance_id"] == instance_id
        assert body["secret_exists"] is True
        # The secret value must never leave the identity endpoint.
        assert "secret" not in body
        assert secret not in resp.text


class TestRotateAuthGate:
    async def test_rotate_requires_system_admin(
        self,
        integration_client: AsyncClient,
        app_engine: AsyncEngine,
        test_org: uuid.UUID,
        test_user: uuid.UUID,
    ) -> None:
        instance_id, _secret = await _mint_and_get_secret(app_engine)
        token = _token(test_org, test_user, "admin", is_system_admin=False)
        resp = await integration_client.post(
            "/api/v1/product-analytics/rotate",
            json=_rotate_body("bogus", instance_id, sequence=1),
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 403


class TestRotateUnauthorized:
    async def test_rotate_401_on_wrong_secret(
        self,
        integration_client: AsyncClient,
        app_engine: AsyncEngine,
        test_org: uuid.UUID,
        test_user: uuid.UUID,
    ) -> None:
        instance_id, _secret = await _mint_and_get_secret(app_engine)
        token = _token(test_org, test_user, "admin", is_system_admin=True)
        resp = await integration_client.post(
            "/api/v1/product-analytics/rotate",
            json=_rotate_body("not-the-real-secret", instance_id, sequence=1),
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 401

    async def test_rotate_401_on_bad_hmac(
        self,
        integration_client: AsyncClient,
        app_engine: AsyncEngine,
        test_org: uuid.UUID,
        test_user: uuid.UUID,
    ) -> None:
        instance_id, secret = await _mint_and_get_secret(app_engine)
        token = _token(test_org, test_user, "admin", is_system_admin=True)
        body = _rotate_body(secret, instance_id, sequence=1)
        body["hmac_digest"] = "deadbeef" * 8  # tamper with the signature
        resp = await integration_client.post(
            "/api/v1/product-analytics/rotate",
            json=body,
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 401


class TestRotateSequenceMonotonicity:
    async def test_rotate_400_on_out_of_order_sequence(
        self,
        integration_client: AsyncClient,
        app_engine: AsyncEngine,
        test_org: uuid.UUID,
        test_user: uuid.UUID,
    ) -> None:
        instance_id, secret = await _mint_and_get_secret(app_engine)
        token = _token(test_org, test_user, "admin", is_system_admin=True)

        # First rotation succeeds (seq=1) and returns the new secret.
        resp1 = await integration_client.post(
            "/api/v1/product-analytics/rotate",
            json=_rotate_body(secret, instance_id, sequence=1),
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp1.status_code == 200, resp1.text
        new_secret = resp1.json()["new_secret"]
        assert new_secret != secret

        # Replaying the same sequence (1) must be rejected as out-of-order.
        resp2 = await integration_client.post(
            "/api/v1/product-analytics/rotate",
            json=_rotate_body(new_secret, instance_id, sequence=1),
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp2.status_code == 400

    async def test_rotate_happy_path_monotonic_and_stores_new_secret(
        self,
        integration_client: AsyncClient,
        app_engine: AsyncEngine,
        test_org: uuid.UUID,
        test_user: uuid.UUID,
    ) -> None:
        instance_id, secret = await _mint_and_get_secret(app_engine)
        token = _token(test_org, test_user, "admin", is_system_admin=True)

        current = secret
        for seq in (1, 2, 3):
            resp = await integration_client.post(
                "/api/v1/product-analytics/rotate",
                json=_rotate_body(current, instance_id, sequence=seq),
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp.status_code == 200, f"sequence {seq}: {resp.text}"
            new_secret = resp.json()["new_secret"]
            assert new_secret != current

            # The rotated value must actually be persisted (read it back via the
            # idempotent get_or_create — it must NOT regenerate a new secret).
            _, stored = await _mint_and_get_secret(app_engine)
            assert stored == new_secret
            current = new_secret

        # The most recent secret must never be exposed by the identity endpoint.
        resp = await integration_client.get(
            "/api/v1/product-analytics/identity",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "secret" not in body
        assert "new_secret" not in body
        assert current not in resp.text


class TestRotateRateLimit:
    async def test_rotate_429_after_rate_limit(
        self,
        integration_client: AsyncClient,
        app_engine: AsyncEngine,
        test_org: uuid.UUID,
        test_user: uuid.UUID,
    ) -> None:
        instance_id, _secret = await _mint_and_get_secret(app_engine)
        token = _token(test_org, test_user, "admin", is_system_admin=True)

        statuses: list[int] = []
        for i in range(6):
            # Bogus secret → 401, but each call still consumes a rate-limit slot
            # before auth is checked, so the 6th must be rejected at the limiter.
            resp = await integration_client.post(
                "/api/v1/product-analytics/rotate",
                json=_rotate_body("bogus", instance_id, sequence=i + 1),
                headers={"Authorization": f"Bearer {token}"},
            )
            statuses.append(resp.status_code)

        # First five reach auth (401); the sixth is blocked by the rate limiter.
        assert statuses[-1] == 429
        assert statuses.count(401) == 5
