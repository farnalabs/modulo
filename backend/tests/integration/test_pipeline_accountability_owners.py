"""FAR-1161 pipeline accountability owners — eligibility, API round-trip, audit.

Integration coverage against the real app on Testcontainers Postgres
(app role => RLS applies):

* eligibility invariant rejects: inactive account, out-of-org account,
  out-of-team account on a ``visibility='team'`` pipeline; accepts a valid
  active org/team member (fail closed, 422 with a specific detail);
* API round-trip: POST create with both owners -> GET read-back -> PATCH
  change/clear, real endpoint + real payload shape (no mocked client);
* audit: ``pipeline.business_owner_changed`` /
  ``pipeline.reliability_owner_changed`` rows written on owner change/clear.

The migration-applies assertions live in
``test_migration_0256_pipeline_accountability_owners.py``.
"""

from __future__ import annotations

import json
import os
import uuid
from typing import Any

import pytest
import pytest_asyncio
from fastapi import HTTPException
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from modulo.auth.jwt import create_access_token
from modulo.db.crud.pipeline_owner import validate_accountability_owner

os.environ.setdefault("MODULO_AUTH_RATE_LIMIT_ENABLED", "false")
os.environ.setdefault("REDIS_URL", "")

pytestmark = pytest.mark.integration

_VALID_32 = "a" * 32


def _token(org_id: uuid.UUID, user_id: uuid.UUID) -> str:
    return create_access_token(
        subject=f"user-{user_id.hex[:8]}",
        secret_key=_VALID_32,
        organisation_id=str(org_id),
        account_id=str(user_id),
        org_role="admin",
        client_kind="browser",
    )


def _auth_headers(org_id: uuid.UUID, user_id: uuid.UUID) -> dict[str, str]:
    return {"Authorization": f"Bearer {_token(org_id, user_id)}"}


async def _seed_account(
    db_engine: AsyncEngine,
    *,
    org_id: uuid.UUID | None,
    tag: str,
    active: bool = True,
) -> uuid.UUID:
    """Insert an account, optionally with an org membership row in ``org_id``."""
    account_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO accounts (id, email, display_name, password_hash, "
                "auth_provider, active) "
                "VALUES (:id, :email, :name, 'hash', 'local', :active)"
            ),
            {
                "id": str(account_id),
                "email": f"far1161-{tag}-{account_id.hex[:12]}@example.com",
                "name": f"FAR-1161 {tag}",
                "active": active,
            },
        )
        if org_id is not None:
            await conn.execute(
                text(
                    "INSERT INTO org_memberships (id, account_id, organisation_id, role) "
                    "VALUES (:mid, :aid, :oid, 'operator')"
                ),
                {"mid": str(uuid.uuid4()), "aid": str(account_id), "oid": str(org_id)},
            )
    return account_id


@pytest_asyncio.fixture(scope="module")
async def owners_data(db_engine: AsyncEngine, test_org: uuid.UUID, test_user: uuid.UUID) -> dict[str, Any]:
    """Seeded principals for the eligibility matrix (committed, module-scoped).

    * ``eligible``       — active account + org membership + team membership
    * ``inactive``       — account.active = false + org membership
    * ``outsider``       — account exists, NO membership in test_org
    * ``out_of_team``    — active org member, NOT a member of the team
    * ``team_id``        — owner team for visibility='team' pipelines
    """
    eligible = await _seed_account(db_engine, org_id=test_org, tag="eligible")
    inactive = await _seed_account(db_engine, org_id=test_org, tag="inactive", active=False)
    outsider = await _seed_account(db_engine, org_id=None, tag="outsider")
    out_of_team = await _seed_account(db_engine, org_id=test_org, tag="noteam")

    from modulo.db.crud.team import create_team
    from modulo.db.crud.team_membership import add_team_member

    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.organisation_id', :oid, true)"),
            {"oid": str(test_org)},
        )
        team = await create_team(
            session, org_id=test_org, name=f"FAR-1161 Team {uuid.uuid4().hex[:6]}", account_id=test_user
        )
        await add_team_member(session, org_id=test_org, team_id=team.id, account_id=eligible, role="runner")
        team_id = team.id

    return {
        "eligible": eligible,
        "inactive": inactive,
        "outsider": outsider,
        "out_of_team": out_of_team,
        "team_id": team_id,
    }


async def _reject_detail(
    rls_session: AsyncSession,
    *,
    owner: uuid.UUID,
    field: str,
    org_id: uuid.UUID,
    visibility: str = "org",
    owner_team_id: uuid.UUID | None = None,
) -> str:
    """Run the eligibility helper expecting a 422; return the detail message."""
    with pytest.raises(HTTPException) as exc_info:
        await validate_accountability_owner(
            rls_session,
            owner_account_id=owner,
            field=field,
            org_id=org_id,
            visibility=visibility,
            owner_team_id=owner_team_id,
        )
    assert exc_info.value.status_code == 422
    return str(exc_info.value.detail)


# ---------------------------------------------------------------------------
# Eligibility invariant (direct helper, real DB)
# ---------------------------------------------------------------------------


async def test_eligibility_accepts_active_org_member(
    rls_session: AsyncSession,
    test_org: uuid.UUID,
    owners_data: dict[str, Any],
) -> None:
    # Fail-closed helper returns None on acceptance (raises HTTPException on
    # every rejection) — assert the return so a silent-raise regression fails.
    result = await validate_accountability_owner(
        rls_session,
        owner_account_id=owners_data["eligible"],
        field="business_owner_id",
        org_id=test_org,
        visibility="org",
        owner_team_id=None,
    )
    assert result is None


async def test_eligibility_rejects_inactive_account(
    rls_session: AsyncSession,
    test_org: uuid.UUID,
    owners_data: dict[str, Any],
) -> None:
    detail = await _reject_detail(
        rls_session,
        owner=owners_data["inactive"],
        field="business_owner_id",
        org_id=test_org,
    )
    assert "deactivated" in detail
    assert "business_owner_id" in detail


async def test_eligibility_rejects_out_of_org_account(
    rls_session: AsyncSession,
    test_org: uuid.UUID,
    owners_data: dict[str, Any],
) -> None:
    detail = await _reject_detail(
        rls_session,
        owner=owners_data["outsider"],
        field="reliability_owner_id",
        org_id=test_org,
    )
    assert "not a member of this organisation" in detail
    assert "reliability_owner_id" in detail


async def test_eligibility_rejects_out_of_team_member_on_team_pipeline(
    rls_session: AsyncSession,
    test_org: uuid.UUID,
    owners_data: dict[str, Any],
) -> None:
    detail = await _reject_detail(
        rls_session,
        owner=owners_data["out_of_team"],
        field="business_owner_id",
        org_id=test_org,
        visibility="team",
        owner_team_id=owners_data["team_id"],
    )
    assert "not a member of the pipeline's owner team" in detail


async def test_eligibility_accepts_team_member_on_team_pipeline(
    rls_session: AsyncSession,
    test_org: uuid.UUID,
    owners_data: dict[str, Any],
) -> None:
    result = await validate_accountability_owner(
        rls_session,
        owner_account_id=owners_data["eligible"],
        field="business_owner_id",
        org_id=test_org,
        visibility="team",
        owner_team_id=owners_data["team_id"],
    )
    assert result is None


async def test_eligibility_none_is_always_accepted(
    rls_session: AsyncSession,
    test_org: uuid.UUID,
) -> None:
    # Clearing an owner (None) never needs eligibility — it is always allowed.
    result = await validate_accountability_owner(
        rls_session,
        owner_account_id=None,
        field="business_owner_id",
        org_id=test_org,
        visibility="team",
        owner_team_id=None,
    )
    assert result is None


# ---------------------------------------------------------------------------
# API round-trip (real endpoint + real payload, no mocked client)
# ---------------------------------------------------------------------------


async def test_api_create_with_both_owners_and_read_back(
    integration_client: AsyncClient,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
    owners_data: dict[str, Any],
) -> None:
    headers = _auth_headers(test_org, test_user)
    name = f"owners-roundtrip-{uuid.uuid4().hex[:8]}"

    create = await integration_client.post(
        "/api/v1/pipelines",
        headers=headers,
        json={
            "name": name,
            "business_owner_id": str(owners_data["eligible"]),
            "reliability_owner_id": str(owners_data["out_of_team"]),
        },
    )
    assert create.status_code == 201, create.text
    created = create.json()
    assert created["business_owner_id"] == str(owners_data["eligible"])
    assert created["reliability_owner_id"] == str(owners_data["out_of_team"])

    read = await integration_client.get(
        f"/api/v1/pipelines/{created['id']}",
        headers=headers,
    )
    assert read.status_code == 200, read.text
    body = read.json()
    assert body["business_owner_id"] == str(owners_data["eligible"])
    assert body["reliability_owner_id"] == str(owners_data["out_of_team"])


async def test_api_patch_changes_and_clears_owners_with_audit(
    integration_client: AsyncClient,
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
    owners_data: dict[str, Any],
) -> None:
    headers = _auth_headers(test_org, test_user)
    name = f"owners-audit-{uuid.uuid4().hex[:8]}"

    create = await integration_client.post(
        "/api/v1/pipelines",
        headers=headers,
        json={
            "name": name,
            "business_owner_id": str(owners_data["eligible"]),
            "reliability_owner_id": str(owners_data["eligible"]),
        },
    )
    assert create.status_code == 201, create.text
    pipeline_id = create.json()["id"]

    # Change business owner + CLEAR reliability owner in one PATCH.
    patch = await integration_client.patch(
        f"/api/v1/pipelines/{pipeline_id}",
        headers=headers,
        json={
            "business_owner_id": str(owners_data["out_of_team"]),
            "reliability_owner_id": None,
        },
    )
    assert patch.status_code == 200, patch.text
    patched = patch.json()
    assert patched["business_owner_id"] == str(owners_data["out_of_team"])
    assert patched["reliability_owner_id"] is None

    async with db_engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT event_type, payload_json FROM audit_events "
                    "WHERE resource_id = CAST(:rid AS uuid) "
                    "AND event_type IN "
                    "('pipeline.business_owner_changed', 'pipeline.reliability_owner_changed') "
                    "ORDER BY created_at"
                ),
                {"rid": pipeline_id},
            )
        ).all()

    event_types = [row[0] for row in rows]
    # create (assignment) + patch (change / clear)
    assert event_types.count("pipeline.business_owner_changed") == 2
    assert event_types.count("pipeline.reliability_owner_changed") == 2

    payloads = [row[1] if isinstance(row[1], dict) else json.loads(str(row[1])) for row in rows]
    business_payloads = [
        p for p, et in zip(payloads, event_types, strict=True) if et == "pipeline.business_owner_changed"
    ]
    # Second business event = the PATCH change: eligible -> out_of_team.
    assert business_payloads[1]["previous_owner_id"] == str(owners_data["eligible"])
    assert business_payloads[1]["new_owner_id"] == str(owners_data["out_of_team"])

    reliability_payloads = [
        p for p, et in zip(payloads, event_types, strict=True) if et == "pipeline.reliability_owner_changed"
    ]
    # Second reliability event = the PATCH clear: eligible -> None.
    assert reliability_payloads[1]["previous_owner_id"] == str(owners_data["eligible"])
    assert reliability_payloads[1]["new_owner_id"] is None


async def test_api_create_rejects_out_of_org_owner(
    integration_client: AsyncClient,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
    owners_data: dict[str, Any],
) -> None:
    headers = _auth_headers(test_org, test_user)
    resp = await integration_client.post(
        "/api/v1/pipelines",
        headers=headers,
        json={
            "name": f"owners-outsider-{uuid.uuid4().hex[:8]}",
            "business_owner_id": str(owners_data["outsider"]),
        },
    )
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert "not a member of this organisation" in detail
    assert "business_owner_id" in detail


async def test_api_create_rejects_inactive_owner(
    integration_client: AsyncClient,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
    owners_data: dict[str, Any],
) -> None:
    headers = _auth_headers(test_org, test_user)
    resp = await integration_client.post(
        "/api/v1/pipelines",
        headers=headers,
        json={
            "name": f"owners-inactive-{uuid.uuid4().hex[:8]}",
            "reliability_owner_id": str(owners_data["inactive"]),
        },
    )
    assert resp.status_code == 422, resp.text
    assert "deactivated" in resp.json()["detail"]


async def test_api_team_pipeline_rejects_out_of_team_owner(
    integration_client: AsyncClient,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
    owners_data: dict[str, Any],
) -> None:
    headers = _auth_headers(test_org, test_user)
    resp = await integration_client.post(
        "/api/v1/pipelines",
        headers=headers,
        json={
            "name": f"owners-team-out-{uuid.uuid4().hex[:8]}",
            "visibility": "team",
            "owner_team_id": str(owners_data["team_id"]),
            "business_owner_id": str(owners_data["out_of_team"]),
        },
    )
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert "not a member of the pipeline's owner team" in detail
    assert "business_owner_id" in detail


async def test_api_team_pipeline_accepts_team_member_owner(
    integration_client: AsyncClient,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
    owners_data: dict[str, Any],
) -> None:
    headers = _auth_headers(test_org, test_user)
    resp = await integration_client.post(
        "/api/v1/pipelines",
        headers=headers,
        json={
            "name": f"owners-team-ok-{uuid.uuid4().hex[:8]}",
            "visibility": "team",
            "owner_team_id": str(owners_data["team_id"]),
            "business_owner_id": str(owners_data["eligible"]),
            "reliability_owner_id": str(owners_data["eligible"]),
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["business_owner_id"] == str(owners_data["eligible"])
    assert body["reliability_owner_id"] == str(owners_data["eligible"])


async def test_api_patch_rejects_out_of_team_owner_on_team_pipeline(
    integration_client: AsyncClient,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
    owners_data: dict[str, Any],
) -> None:
    headers = _auth_headers(test_org, test_user)
    create = await integration_client.post(
        "/api/v1/pipelines",
        headers=headers,
        json={
            "name": f"owners-team-patch-{uuid.uuid4().hex[:8]}",
            "visibility": "team",
            "owner_team_id": str(owners_data["team_id"]),
            "business_owner_id": str(owners_data["eligible"]),
        },
    )
    assert create.status_code == 201, create.text
    pipeline_id = create.json()["id"]

    patch = await integration_client.patch(
        f"/api/v1/pipelines/{pipeline_id}",
        headers=headers,
        json={"reliability_owner_id": str(owners_data["out_of_team"])},
    )
    assert patch.status_code == 422, patch.text
    assert "not a member of the pipeline's owner team" in patch.json()["detail"]
