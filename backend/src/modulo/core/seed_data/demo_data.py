"""Demo-org seed framework (FAR-450 foundation ticket).

GATED behind ``MODULO_SEED_DEMO_ORGS`` (see ``modulo.settings``). Each demo
organisation carries its OWN signed ``license_key`` in
``Organisation.settings_json`` so it resolves the intended tier (community or
team) via ``core.feature_flags.resolve_plan_context`` — a bare ``plan_id``
without a valid signed license silently downgrades to community, so the
per-org license is mandatory.

This is the FOUNDATION ticket: it builds the framework only. The module-level
``DEMO_ORGS`` list is empty by default; follow-up tickets populate it with
concrete organisations and seed their entities (the ``full`` flag gates which
entities get seeded — ignored for now).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any, TypedDict

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from modulo.core.license import parse_and_verify
from modulo.core.license_signing import (
    LicenseSigningError,
    encode_license_key,
    generate_team_license,
)
from modulo.db.models.account import Account
from modulo.db.models.org_membership import OrgMembership
from modulo.db.models.organisation import Organisation
from modulo.settings import get_settings

_log = logging.getLogger(__name__)


# Demo organisations to seed at boot. Empty by default (FAR-450 foundation) —
# follow-up tickets append dicts of the form:
#   {"slug": str, "tier": "community" | "team", "full": bool,
#    "email": str, "password": str}
#
# Demo emails MUST be dedicated addresses — never reuse an existing real
# account's email, or the seed refuses to attach it (see seed_demo_org).
class DemoOrgSpec(TypedDict, total=False):
    slug: str
    tier: str
    full: bool
    email: str
    password: str


DEMO_ORGS: list[DemoOrgSpec] = []

# Far-future expiry (~10y) for community demo licenses so they never expire
# mid-eval.
_COMMUNITY_LICENSE_YEARS = 10


def _community_license_key(slug: str, private_key_hex: str, org_id: str) -> str:
    """Sign a community-tier license key for *slug* bound to *org_id*."""
    expires_at = (datetime.now(UTC) + timedelta(days=365 * _COMMUNITY_LICENSE_YEARS)).isoformat()
    payload: dict[str, Any] = {
        "tier": "community",
        "features": [],
        "expires_at": expires_at,
        "org_id": org_id,
    }
    return encode_license_key(payload, private_key_hex)


async def seed_demo_org(
    session: AsyncSession,
    *,
    slug: str,
    tier: str,
    full: bool,
    admin_email: str,
    admin_password: str,
) -> None:
    """Idempotently create a demo organisation + admin account + membership.

    All three entities are created only if absent (slug / email / account+org
    existence guards), so the function is safe to call on every boot. The org is
    stamped with a per-org signed ``license_key`` (community or team) bound to
    the actual ``org.id``, so the tier resolves correctly through
    ``resolve_plan_context``.

    ``full`` is accepted but IGNORED for now — demo entities are seeded in
    follow-up tickets. It is persisted under ``settings_json["demo"]["full"]``
    for future use.

    Fails closed: when no signing private key is configured the function raises
    BEFORE any DB write (for both tiers). When the admin email collides with a
    pre-existing account that is not already an admin member of this org, it
    raises to avoid cross-tenant escalation (attaching a real account — e.g. a
    superuser — to the demo org).
    """
    private_key = get_settings().modulo_license_private_key or ""
    if not private_key:
        raise LicenseSigningError(
            f"No license signing private key configured for demo org {slug!r} (set MODULO_LICENSE_PRIVATE_KEY)."
        )

    # 1. Organisation (idempotent by slug, with concurrency safety)
    org_result = await session.execute(select(Organisation).where(Organisation.slug == slug))
    org = org_result.scalar_one_or_none()
    if org is None:
        org = Organisation(name=slug, slug=slug, settings_json={})
        try:
            # Insert inside a savepoint so a concurrent boot that already
            # committed this slug (unique violation) only rolls back the failed
            # insert — NOT the whole transaction, which session.rollback() would
            # do (discarding unrelated in-flight writes from concurrent work).
            async with session.begin_nested():
                session.add(org)
                await session.flush()
        except IntegrityError:
            # Another boot won the race; use the row it committed. The failed
            # pending insert is discarded automatically by the savepoint.
            org_result = await session.execute(select(Organisation).where(Organisation.slug == slug))
            org = org_result.scalar_one_or_none()
            if org is None:
                raise
            _log.info("demo_org.recovered_after_conflict", extra={"slug": slug})
        else:
            _log.info("demo_org.created", extra={"slug": slug})
    else:
        _log.info("demo_org.exists", extra={"slug": slug})

    # 2. Admin account (idempotent by email, with collision guard)
    account_result = await session.execute(select(Account).where(Account.email == admin_email))
    account = account_result.scalar_one_or_none()
    if account is None:
        from modulo.auth.passwords import hash_password

        account = Account(
            email=admin_email,
            display_name=admin_email.split("@", 1)[0],
            password_hash=hash_password(admin_password),
            auth_provider="local",
        )
        session.add(account)
        await session.flush()
        _log.info("demo_account.created", extra={"email": admin_email})
    else:
        # Refuse to attach a pre-existing account unless it is already the
        # admin member we created on a prior boot. A real account (e.g. a
        # superuser) must never be silently bound to a demo org.
        member_result = await session.execute(
            select(OrgMembership).where(
                OrgMembership.account_id == account.id,
                OrgMembership.organisation_id == org.id,
                OrgMembership.role == "admin",
            )
        )
        if member_result.scalar_one_or_none() is None:
            raise ValueError(
                f"demo org email {admin_email!r} collides with an existing account "
                f"— refusing to attach (use a dedicated demo email)"
            )
        _log.info("demo_account.exists", extra={"email": admin_email})

    # 3. Membership (idempotent by account + org)
    mem_result = await session.execute(
        select(OrgMembership).where(
            OrgMembership.account_id == account.id,
            OrgMembership.organisation_id == org.id,
        )
    )
    membership = mem_result.scalar_one_or_none()
    if membership is None:
        membership = OrgMembership(
            account_id=account.id,
            organisation_id=org.id,
            role="admin",
        )
        session.add(membership)
        _log.info("demo_membership.created", extra={"email": admin_email, "slug": slug})
    else:
        _log.info("demo_membership.exists", extra={"email": admin_email, "slug": slug})

    # 4. Per-org signed license (community | team), bound to the real org id.
    #    Idempotent: reuse an existing valid key whose tier matches, otherwise
    #    (re)compute. Only write back when something actually changed.
    org_id = str(org.id)
    existing_key = org.settings_json.get("license_key") if org.settings_json else None
    new_key: str | None = None
    if existing_key:
        parsed = parse_and_verify(existing_key)
        if parsed.valid and parsed.license_data is not None and parsed.license_data.tier == tier:
            new_key = existing_key  # still valid for this tier — keep it

    if new_key is None:
        if tier == "team":
            new_key = generate_team_license(org_name=slug, org_id=org_id, private_key_hex=private_key)
        else:
            new_key = _community_license_key(slug, private_key, org_id)

    base = dict(org.settings_json or {})
    new_settings: dict[str, Any] = dict(base)
    new_settings["license_key"] = new_key
    demo = dict(base.get("demo") or {})
    demo["tier"] = tier
    demo["full"] = bool(full)
    new_settings["demo"] = demo

    if new_settings != base:
        org.settings_json = new_settings
        await session.flush()


async def seed_demo_orgs(factory: async_sessionmaker[AsyncSession]) -> None:
    """Seed every demo org listed in ``DEMO_ORGS`` (gated by the caller).

    Each spec is seeded in its own transaction for isolation: a failure on one
    org (bad spec, missing key, email collision) is logged and skipped without
    aborting the remaining orgs. Idempotent — re-running never duplicates.
    """
    if not DEMO_ORGS:
        _log.info("demo_orgs.empty")
        return

    for spec in DEMO_ORGS:
        try:
            async with factory() as session, session.begin():
                await seed_demo_org(
                    session,
                    slug=spec["slug"],
                    tier=spec["tier"],
                    full=spec.get("full", False),
                    admin_email=spec["email"],
                    admin_password=spec["password"],
                )
        except (ValueError, LicenseSigningError, IntegrityError):
            _log.exception("demo_org.seed_failed", extra={"slug": spec.get("slug")})
            continue
