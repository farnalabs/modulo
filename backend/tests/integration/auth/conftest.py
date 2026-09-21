"""Auth-directory integration fixtures (pre-auth SSO RLS tests).

The app-fallback SSO resolvers (``_resolve_saml_for_route`` /
``_resolve_oidc_provider`` with ``system_session=None``) bind the app
session's RLS org to the globally earliest Organisation via
``_set_default_rls_org``. The integration suite runs against ONE shared
Postgres (``-n 2`` xdist workers, one migrated testcontainer), so that "first
org" is a session-global resource: no single test module may claim it by
backdating its own fixture org — two modules that both do race for the slot
and one inevitably 404s (observed on main runs 35570345586 / 35574044782).

``first_sso_org`` creates that slot once (fixed id, ``ON CONFLICT DO NOTHING``
so both xdist workers converge on the same row), far enough in the past that
it is deterministically the first org for every fallback test.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

# Fixed id + slug: both xdist workers see the same row, so the "first org" is
# not per-worker. Year 2000 is earlier than every other fixture org (created
# "now"; at most backdated a decade), so the ordering is deterministic.
_FIRST_ORG_ID = uuid.UUID("3114f5d9-6c4c-4a22-91e3-9fe7e4824030")
_FIRST_ORG_SLUG = "sso-fallback-first-org"
_FIRST_ORG_CREATED_AT = datetime(2000, 1, 1, tzinfo=UTC)


@pytest_asyncio.fixture(scope="session")
async def first_sso_org(db_engine: AsyncEngine) -> uuid.UUID:
    """The Organisation ``_set_default_rls_org`` resolves for the app fallback.

    Shared by the SAML and OIDC app-fallback tests (and by both xdist workers)
    so they exercise the first-org binding against a single, deterministic org
    instead of racing for the session-global slot.
    """
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO organisations (id, name, slug, settings_json, created_at) "
                "VALUES (:id, :name, :slug, '{}'::json, :created_at) "
                "ON CONFLICT (id) DO NOTHING"
            ),
            {
                "id": str(_FIRST_ORG_ID),
                "name": "SSO Fallback First Org",
                "slug": _FIRST_ORG_SLUG,
                "created_at": _FIRST_ORG_CREATED_AT,
            },
        )
    return _FIRST_ORG_ID
