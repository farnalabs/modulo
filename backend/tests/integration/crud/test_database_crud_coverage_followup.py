"""Real async database coverage for the remaining FAR-366 database paths."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.crud.connector_instance import (
    delete_connector_instance,
    get_connector_instance,
    update_connector_instance,
)
from modulo.db.crud.org_membership import create_membership, resolve_live_role_and_kind
from modulo.db.crud.rating import (
    get_rating_aggregate,
    list_abuse_reports,
    list_ratings_for_primitive,
    review_abuse_report,
    submit_abuse_report,
    submit_rating,
)
from modulo.db.models.account import Account
from modulo.db.models.library_primitive import LibraryPrimitive
from modulo.db.models.primitive_rating import PrimitiveRating

pytestmark = pytest.mark.integration


async def test_connector_update_get_and_delete_use_tenant_session(
    rls_session: AsyncSession,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    from modulo.db.crud.connector_instance import create_connector_instance

    connector = await create_connector_instance(
        rls_session,
        org_id=test_org,
        name=f"followup-{uuid.uuid4().hex}",
        connector_type_id="filesystem",
        account_id=test_user,
        credentials_ciphertext=b"cipher",
    )
    assert await get_connector_instance(rls_session, connector.id) is connector
    updated = await update_connector_instance(rls_session, connector.id, {"name": "updated"})
    assert updated is connector
    assert connector.name == "updated"
    assert await update_connector_instance(rls_session, uuid.uuid4(), {"name": "missing"}) is None
    assert await delete_connector_instance(rls_session, connector.id) is True
    assert await delete_connector_instance(rls_session, connector.id) is False


async def test_membership_create_and_live_role_kind_filters_inactive_account(
    rls_session: AsyncSession,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    account = Account(
        id=uuid.uuid4(),
        email=f"followup-{uuid.uuid4().hex}@example.com",
        display_name="Followup account",
        password_hash="hash",
        active=True,
    )
    rls_session.add(account)
    await rls_session.flush()
    membership = await create_membership(
        rls_session,
        account_id=account.id,
        org_id=test_org,
        role="runner",
    )
    assert membership.role == "runner"
    assert await resolve_live_role_and_kind(rls_session, str(account.id), str(test_org)) == ("runner", "human")

    account.active = False
    await rls_session.flush()
    assert await resolve_live_role_and_kind(rls_session, str(account.id), str(test_org)) is None


async def test_rating_and_abuse_report_crud_uses_real_org_rows(
    rls_session: AsyncSession,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    owner = Account(
        id=uuid.uuid4(),
        email=f"owner-{uuid.uuid4().hex}@example.com",
        display_name="Primitive owner",
        password_hash="hash",
        active=True,
    )
    rls_session.add(owner)
    await rls_session.flush()
    primitive = LibraryPrimitive(
        organisation_id=test_org,
        source="registry",
        primitive_type="agent",
        name="Rated primitive",
        slug=f"rated-{uuid.uuid4().hex}",
        author="tester",
        version="1.0.0",
        content_json={},
        source_url="https://example.com/primitive",
        checksum="checksum",
        verified=True,
        download_count=0,
        account_id=owner.id,
    )
    rls_session.add(primitive)
    await rls_session.flush()
    copy = LibraryPrimitive(
        organisation_id=test_org,
        source="local",
        primitive_type="agent",
        name="Copied primitive",
        slug=f"copy-{uuid.uuid4().hex}",
        author="tester",
        version="1.0.0",
        content_json={},
        account_id=test_user,
        forked_from=primitive.id,
    )
    rls_session.add(copy)
    await rls_session.flush()

    rating = await submit_rating(
        rls_session,
        org_id=test_org,
        primitive_id=primitive.id,
        thumbs_up=True,
        comment="Useful",
        account_id=test_user,
    )
    assert rating.primitive_id == primitive.id
    assert await get_rating_aggregate(rls_session, primitive.id) == (5, 1)
    ratings = await list_ratings_for_primitive(rls_session, primitive.id, page_size=1)
    assert ratings.items == [rating]
    assert ratings.has_more is False

    report = await submit_abuse_report(
        rls_session,
        org_id=test_org,
        primitive_id=primitive.id,
        rating_id=rating.id,
        reporter_account_id=test_user,
        reason="Needs review",
    )
    reports = await list_abuse_reports(rls_session, test_org, status_filter="pending")
    assert reports.items == [report]
    reviewed = await review_abuse_report(
        rls_session,
        report.id,
        org_id=test_org,
        new_status="reviewed",
        reviewer_account_id=test_user,
    )
    assert reviewed is report
    assert report.status == "reviewed"
    assert (
        await review_abuse_report(
            rls_session,
            report.id,
            org_id=uuid.uuid4(),
            new_status="dismissed",
        )
        is None
    )
    assert await rls_session.scalar(select(PrimitiveRating.id).where(PrimitiveRating.id == rating.id)) == rating.id
