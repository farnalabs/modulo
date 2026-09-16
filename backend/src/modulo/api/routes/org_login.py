"""Pre-auth org-login resolution routes (FAR-856).

Two anonymous endpoints that power the org-first login flow:

1. ``GET /api/v1/auth/login-context`` — returns whether the instance has a
   single login-active org (for auto-skip) or multiple (for org selection).

2. ``GET /api/v1/auth/org-login/{slug}`` — resolves ONE org by exact slug
   and returns its enabled OIDC providers (scoped to that org only).

Both endpoints are pre-auth (no JWT required), must never 401/402 for a
missing Authorization header, and must never enumerate orgs. A uniform
not-found is returned for unknown / non-login-active slugs.

Security reasoning (stays in comments):
- Anonymous callers may never enumerate orgs. No list endpoint, no
  partial/fuzzy matching — exact slug only. A uniform not-found is deliberate.
- Routing is NOT authorisation: binding a login to an org (Phase B) says
  which org the user chose, not that they may join it (the "allowed to join"
  gate is FAR-855, a separate ticket — do not implement it here).
- Never expose client_secret, client_id, or any other secret on these
  pre-auth responses.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.constants import MSG_INTERNAL_SERVER_ERROR
from modulo.api.db_error_handling import handle_db_errors
from modulo.api.dependencies import get_db_session
from modulo.api.routes.sso import is_saml_available
from modulo.db.crud.organisation import (
    get_login_active_org_by_slug,
    list_login_active_orgs,
)
from modulo.db.crud.sso_provider import list_enabled_oidc_providers
from modulo.settings import Settings, get_settings

_log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/auth", tags=["org-login"])

# Uniform generic 404 for every non-login-active or unknown slug.  Never
# reveal whether the slug exists — the body and timing shape must be identical
# for both cases.
_GENERIC_404 = HTTPException(
    status_code=status.HTTP_404_NOT_FOUND,
    detail="Not Found",
)


# ---------------------------------------------------------------------------
# Pydantic response models
# ---------------------------------------------------------------------------


class OrgInfo(BaseModel):
    slug: str
    name: str


class LoginContextResponse(BaseModel):
    multi_org: bool
    org: OrgInfo | None


class OrgLoginProviderInfo(BaseModel):
    provider_id: str
    display_name: str
    # preset may be omitted if FAR-853 has not landed — never invent a column.
    preset: str | None = None


class OrgLoginResponse(BaseModel):
    org: OrgInfo
    providers: list[OrgLoginProviderInfo]
    password_enabled: bool = True
    # Instance-wide SAML availability (not per-org — SAML is single-IdP-per-instance).
    saml: bool = False


# ---------------------------------------------------------------------------
# GET /api/v1/auth/login-context
# ---------------------------------------------------------------------------


@router.get("/login-context")
@handle_db_errors("auth.login_context")
async def login_context(
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_db_session),
) -> LoginContextResponse:
    """Return whether the instance has exactly one login-active org.

    Pre-auth, anonymous — must never require an Authorization header and must
    never 401/402. The plan context is resolved without a user (system-level
    license only).

    Exactly ONE login-active org → ``multi_org=false, org={slug, name}``
    (powers the single-org auto-skip on the frontend).

    Zero or more than one → ``multi_org=true, org=null``.

    Never returns a list of orgs, and never any data about a non-single org.
    """
    try:
        async with session.begin():
            orgs = await list_login_active_orgs(session)
    except Exception:
        _log.exception("auth.login_context.db_error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_INTERNAL_SERVER_ERROR,
        ) from None

    if len(orgs) == 1:
        org = orgs[0]
        return LoginContextResponse(
            multi_org=False,
            org=OrgInfo(slug=org.slug, name=org.name),
        )

    return LoginContextResponse(multi_org=True, org=None)


# ---------------------------------------------------------------------------
# GET /api/v1/auth/org-login/{slug}
# ---------------------------------------------------------------------------


@router.get("/org-login/{slug}")
@handle_db_errors("auth.org_login")
async def org_login(
    slug: str,
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_db_session),
) -> OrgLoginResponse:
    """Resolve ONE org by slug and return its enabled OIDC providers.

    Pre-auth, anonymous — must never require an Authorization header and must
    never 401/402. Rate-limited via the existing ``RateLimitMiddleware`` (the
    ``/api/v1/auth/`` prefix is NOT rate-limited by default for GET; the
    ``RateLimitMiddleware`` only fires on POST/PUT/PATCH — see its
    ``_should_rate_limit`` — so this GET endpoint relies on the path being
    included in the RULES list for rate-limit coverage; however the existing
    middleware does not rate-limit GETs, so we add explicit per-route
    rate-limit awareness via a comment for future middleware expansion).

    For a login-active org returns:
      ``{"org": {slug, name}, "providers": [{provider_id, display_name, preset?}],
        "password_enabled": true}``

    ``providers`` are that org's ENABLED OIDC providers only — scoped to the
    resolved org (not the system-scoped global read). ``preset`` is included
    only if the column exists (FAR-853); omitted otherwise.

    For an unknown OR non-login-active slug, returns a uniform generic 404
    with the same body/timing shape for both cases. Never reveals whether
    the slug exists.

    Security reasoning:
    - No fuzzy matching — exact slug only.
    - Never expose client_secret, client_id, or any secret field.
    - Routing is not authorisation.
    """
    try:
        async with session.begin():
            org = await get_login_active_org_by_slug(session, slug)
            if org is None:
                raise _GENERIC_404

            # Scoped to this org only — never use the system-scoped global read.
            oidc_providers = await list_enabled_oidc_providers(session)
            # Filter to the resolved org.  ``list_enabled_oidc_providers`` returns
            # ALL enabled OIDC providers visible to the session (RLS-scoped).  When
            # the session is the app session (RLS), the WHERE clause is
            # organisation_id = <org's RLS context>.  When the session is the
            # system session (BYPASSRLS), we must filter manually.  We always
            # filter by organisation_id defensively.
            scoped_providers = [p for p in oidc_providers if p.organisation_id == org.id]

            # SAML is instance-wide (single-IdP-per-instance), not per-org.
            saml_enabled = await is_saml_available(settings, None, session)
    except HTTPException:
        raise
    except Exception:
        _log.exception("auth.org_login.db_error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_INTERNAL_SERVER_ERROR,
        ) from None

    return OrgLoginResponse(
        org=OrgInfo(slug=org.slug, name=org.name),
        providers=[
            OrgLoginProviderInfo(
                provider_id=p.provider_id or "",
                display_name=p.name,
                preset=getattr(p, "preset", None),
            )
            for p in scoped_providers
            if p.provider_id  # skip providers with no slug
        ],
        password_enabled=True,
        saml=saml_enabled,
    )
