"""Integration tests for error tracking CRUD against real Postgres.

Regression coverage for the FOR UPDATE / lazy="joined" outer-join bug:
upsert_error_group issues SELECT ... FOR UPDATE on ErrorGroup, whose
``sample_event`` relationship is lazy="joined", so the emitted SQL is a
LEFT OUTER JOIN to error_events. An unqualified FOR UPDATE then tries to lock
the nullable (right) side of the outer join, which Postgres rejects with
asyncpg.exceptions.FeatureNotSupportedError — silently breaking error ingestion
in production.
"""

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.crud.error_tracking import upsert_error_group

pytestmark = pytest.mark.integration


async def test_upsert_error_group_creates_group(rls_session: AsyncSession, test_org: uuid.UUID) -> None:
    group = await upsert_error_group(
        rls_session,
        org_id=test_org,
        fingerprint="deadbeef" * 8,
        level="error",
    )
    assert group.id is not None
    assert group.count == 1
    assert group.level_peak == "error"


async def test_upsert_error_group_increments_existing_group(
    rls_session: AsyncSession, test_org: uuid.UUID
) -> None:
    fingerprint = "deadbeef" * 8
    first = await upsert_error_group(
        rls_session,
        org_id=test_org,
        fingerprint=fingerprint,
        level="error",
    )
    second = await upsert_error_group(
        rls_session,
        org_id=test_org,
        fingerprint=fingerprint,
        level="critical",
    )
    assert second.id == first.id
    assert second.count == 2
    assert second.level_peak == "critical"
