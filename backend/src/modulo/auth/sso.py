"""OIDC and SAML 2.0 SSO support with JIT account provisioning."""

import base64
import hmac
import json
import logging
import secrets
import time
import urllib.parse
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
from defusedxml import ElementTree
from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.auth.jwt import CLIENT_KIND_BROWSER, create_access_token, create_refresh_token
from modulo.auth.oauth import compute_pkce_challenge
from modulo.auth.oidc_verify import OidcVerifyError, verify_id_token
from modulo.auth.saml_handler import ModuloSamlAuth, SamlAuthError
from modulo.auth.secret_storage import decode_stored_secret
from modulo.core.feature_flags import resolve_sso_unrestricted_provisioning
from modulo.core.runtime_config.key_bridge import get_public_url
from modulo.core.ssrf import (
    derive_oidc_allowed_hosts,
    pinned_async_client,
    require_url_host_in_allowlist,
    validate_outbound_url_async,
    validate_outbound_url_preflight,
)
from modulo.db.crud.account import create_account, get_account_by_email, update_last_login
from modulo.db.crud.invitations import consume_invitation, get_live_for_email
from modulo.db.crud.org_membership import (
    create_membership,
    get_membership_by_account_and_org,
    reactivate_membership,
)
from modulo.db.crud.sso_provider import get_enabled_saml_provider, get_provider_by_provider_id
from modulo.db.crud.team_membership import add_team_member, get_membership_by_team_and_account, update_member_role
from modulo.db.crud.token_family import create_family
from modulo.db.models.account import Account
from modulo.db.models.invitation import Invitation
from modulo.db.models.organisation import Organisation
from modulo.db.models.sso_provider import SsoProvider
from modulo.db.rls import set_rls_org
from modulo.settings import Settings

_log = logging.getLogger(__name__)

# Default OIDC scopes requested when a provider configures none (same default
# in every branch of the scope resolution below).
OIDC_DEFAULT_SCOPES = "openid email profile"


# ---------------------------------------------------------------------------
# SSO callback failure vocabulary + typed error (FAR-1791)
# ---------------------------------------------------------------------------
# The IdP navigates the BROWSER to the SSO callback routes (the OIDC callback
# and the SAML ACS), so a failure must send it back to the login page with a
# small, WHITELISTED error code the SPA maps to friendly copy — never a raw
# JSON error page. The vocabulary is FIXED: exception text is NEVER placed in
# the URL, and the frontend falls back to the ``auth_failed`` copy for an
# unknown code.
SSO_ERROR_NOT_INVITED = "not_invited"
SSO_ERROR_AUTH_FAILED = "auth_failed"
SSO_ERROR_PROVIDER_UNAVAILABLE = "provider_unavailable"


class SsoCallbackError(HTTPException):
    """A browser-facing SSO callback failure → a 303 redirect to the login page.

    Raised by the OIDC callback / SAML ACS routes instead of RETURNING the
    redirect, so the ``audited_system("saml_acs_attempted", ...)`` dependency
    observes the failure and records ``outcome="error"`` for a failed sign-in
    rather than a dishonest ``"success"`` (FAR-1791). The registered handler
    (``modulo.api.exception_handlers.sso_callback_error_handler``) turns it into
    the actual 303 response.

    Subclasses ``HTTPException`` — deliberately NOT ``ValueError``:

    * ``ValueError`` would be re-caught by the callback routes' own
      ``except ValueError`` arm (the same ValueError-subclass trap
      :class:`SsoProvisioningDeniedError` documents). Keeping the two error
      types independent means a denial and a generic auth failure can never be
      conflated by exception ordering.
    * ``handle_db_errors`` passes ``HTTPException`` through unchanged
      (``_translate_wrapped_exception``'s ``except HTTPException: raise`` arm),
      so the error reaches the app's registered handler instead of being
      mis-mapped to a generic 500.

    ``code`` is drawn from the fixed vocabulary above (exception text never
    reaches the URL); ``slug`` is a DB-resolved org slug (or ``None``), so the
    redirect target is assembled only from server-owned values and cannot be
    abused as an open redirect.
    """

    def __init__(self, code: str, slug: str | None = None) -> None:
        super().__init__(status_code=status.HTTP_303_SEE_OTHER, detail=code)
        self.code = code
        self.slug = slug


# ---------------------------------------------------------------------------
# State signing (CSRF protection for OIDC redirect flow)
# ---------------------------------------------------------------------------


def sign_state(state: str, secret_key: str) -> str:
    """HMAC-SHA256 sign a state value for OIDC anti-forgery."""
    sig = hmac.new(secret_key.encode(), state.encode(), "sha256").hexdigest()[:16]
    return f"{state}:{sig}"


def verify_state(signed: str, secret_key: str) -> str | None:
    """Verify a signed state value. Returns the original on success, None on tamper."""
    parts = signed.rsplit(":", 1)
    if len(parts) != 2:
        return None
    state, sig = parts
    expected = hmac.new(secret_key.encode(), state.encode(), "sha256").hexdigest()[:16]
    if not hmac.compare_digest(expected, sig):
        return None
    return state


# ---------------------------------------------------------------------------
# SAML RelayState signing (defense-in-depth for per-provider SAML)
# ---------------------------------------------------------------------------
# RelayState is NEVER the routing key — routing is by the ACS URL path
# ({provider_id}). Its absence must NOT fail routing (IdP-initiated SSO
# may not send it). This is an additional integrity signal, not the
# primary control. The per-provider Entity ID + audience check (FAR-1010)
# contain assertions to the correct org; RelayState is defense-in-depth
# against tampering within that containment.

_RELAY_STATE_MAX_AGE_SECONDS = 600  # 10 minutes
_RELAY_STATE_FUTURE_SKEW_SECONDS = 60  # tolerance for IdP/SP clock skew


def sign_saml_relay_state(provider_id: str, secret_key: str) -> str:
    """HMAC-sign a SAML RelayState payload containing the provider_id and a timestamp.

    Reuses the existing ``sign_state`` primitive (the same HMAC-SHA256
    helper OIDC already uses). The signed payload is a base64url-encoded
    JSON object ``{"pid": provider_id, "ts": <unix_epoch>}`` to avoid
    encoding issues across URL-redirect and form-post boundaries.

    The payload is intentionally compact — it travels through the
    IdP's opaque relay field and must survive truncation by some IdPs.
    """
    payload = json.dumps({"pid": provider_id, "ts": int(time.time())}, separators=(",", ":"))
    encoded = base64.urlsafe_b64encode(payload.encode()).rstrip(b"=").decode()
    return sign_state(encoded, secret_key)


def verify_saml_relay_state(
    signed: str,
    secret_key: str,
    *,
    max_age_seconds: int = _RELAY_STATE_MAX_AGE_SECONDS,
) -> dict[str, object] | None:
    """Verify a signed SAML RelayState. Returns the decoded payload on success, None on failure.

    Checks the HMAC signature (via ``verify_state``) and the timestamp
    expiry. A timestamp more than ``_RELAY_STATE_FUTURE_SKEW_SECONDS`` in
    the future is also rejected, so a clock-skew or replay window cannot be
    abused by a token minted ahead of time. Returns a dict with ``"pid"``
    (provider_id) and ``"ts"`` (Unix timestamp) on success.

    Absence-tolerance is handled by the CALLER — this function is never
    called when RelayState is absent.
    """
    state = verify_state(signed, secret_key)
    if state is None:
        return None
    try:
        padded = state + "=" * (-len(state) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded))
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    ts = payload.get("ts")
    if not isinstance(ts, (int, float)):
        return None
    if ts - time.time() > _RELAY_STATE_FUTURE_SKEW_SECONDS:
        return None
    if time.time() - ts > max_age_seconds:
        return None
    return payload


async def _set_default_rls_org(session: AsyncSession) -> None:
    """Set RLS org context to the first Organisation so pre-auth SSO routes can read the sso_providers table.

    The sso_providers table is OrgScoped, so Postgres RLS filters rows by
    ``app.organisation_id``. Pre-auth routes (no user/org claim) would otherwise
    get ZERO rows and silently fall through to the empty env fallback. We point
    RLS at the first org (single-org self-hosted assumption) so the global,
    org-unfiltered provider lookups resolve correctly.
    """
    result = await session.execute(select(Organisation).order_by(Organisation.created_at).limit(1))
    org = result.scalar_one_or_none()
    if org is not None:
        await set_rls_org(session, org.id)


async def _scope_app_session_org(
    app_session: AsyncSession,
    db_provider: SsoProvider | None,
) -> uuid.UUID | None:
    """Scope the app session RLS for JIT writes and return the ``default_org_id``.

    The app session runs as ``modulo_app`` (NOBYPASSRLS), so the JIT writes
    (Account / OrgMembership) must be RLS-scoped to the org the user is being
    provisioned into. When a DB provider was resolved (its ``organisation_id``
    is the org owning that provider), scope to it and return it as the default
    org. Otherwise fall back to the first-org single-org behaviour (``None``
    tells ``jit_provision_user`` to resolve the first org itself).
    """
    if db_provider is not None and db_provider.organisation_id is not None:
        await set_rls_org(app_session, db_provider.organisation_id)
        return db_provider.organisation_id
    await _set_default_rls_org(app_session)
    return None


# ---------------------------------------------------------------------------
# JIT account provisioning
# ---------------------------------------------------------------------------


async def _resolve_jit_org_id(
    session: AsyncSession,
    default_org_id: uuid.UUID | None,
) -> uuid.UUID:
    """The JIT target org: the caller's default, else the first Organisation."""
    if default_org_id is not None:
        return default_org_id
    result = await session.execute(select(Organisation).order_by(Organisation.created_at).limit(1))
    org = result.scalar_one_or_none()
    if org is None:
        raise RuntimeError("No organisation exists — cannot JIT provision account")
    return org.id


async def _jit_existing_member_role(
    session: AsyncSession,
    email: str,
    org_id: uuid.UUID,
) -> str | None:
    """Role of the identity's existing LIVE membership in the org, else ``None``."""
    existing = await get_account_by_email(session, email)
    if existing is None:
        return None
    membership = await get_membership_by_account_and_org(session, existing.id, org_id)
    if membership is None or membership.deactivated_at is not None:
        return None
    return membership.role


async def _jit_join_unlocked(
    provider: SsoProvider,
    email: str,
    email_verified: bool,
    org_id: uuid.UUID,
) -> bool:
    """Resolve the self-service join gate, including the mode-3 operator flag."""
    unlocked, mode = _sso_jit_mode_allows_auto(provider, email, email_verified)
    if unlocked and mode == "unrestricted":
        # Mode 3 is DANGEROUS and gated behind the operator-level flag; OFF
        # fails CLOSED here (falling back to member/invitation-only access).
        unlocked = await resolve_sso_unrestricted_provisioning(org_id=org_id)
        if not unlocked:
            _log.info(
                "sso.jit_unrestricted_flag_off",
                extra={"organisation_id": str(org_id), "provider_id": provider.provider_id},
            )
    return unlocked


async def _consume_jit_invitation(
    session: AsyncSession,
    invitation: Invitation,
    org_id: uuid.UUID,
) -> None:
    """CAS-consume the invitation that granted the join, then audit its use."""
    if not await consume_invitation(session, invitation):
        # CAS lost the race — abort atomically, identical to accept-invite.
        raise SsoProvisioningDeniedError(Constants.MSG_SSO_JOIN_DENIED, org_id=org_id)
    await _audit_invite_consumed_fail_open(session, org_id, invitation.id)


async def jit_provision_user(
    session: AsyncSession,
    settings: Settings,
    email: str,
    display_name: str,
    auth_provider: str,
    sso_subject: str,
    default_org_id: uuid.UUID | None = None,
    *,
    sso_provider: SsoProvider | None = None,
    email_verified: bool = False,
) -> tuple[Account, uuid.UUID, str]:
    """Find or create an Account + OrgMembership for an SSO-authenticated identity.

    Returns (account, org_id, org_role). The target org is resolved BEFORE any
    ``create_account`` write so a zero-org deployment (env-path JIT, no provider
    org) raises a clean ``RuntimeError`` ("No organisation exists") instead of
    letting a raw RLS-scoped INSERT fail with a 500.

    FAR-855 join gate (applies ONLY when ``sso_provider`` is a DB provider row —
    the governed configuration surface; the env-var fallback keeps the legacy
    behaviour and logs):
    - ``auto_provision=false`` (DEFAULT): "invitation only" — the user must
      already hold an ACTIVE membership in the org, or a valid pending
      Invitation for (email, org) must exist and is consumed (CAS, granting
      the role it specifies). Otherwise DENY.
    - ``auto_provision=true`` + non-empty ``allowed_domains``: a VERIFIED
      email domain in the EXACT (case-insensitive, no implicit subdomain)
      allowlist auto-provisions with ``provider.default_role`` (fallback
      ``settings.modulo_sso_default_role`` — closes FAR-839); otherwise
      falls back to the invitation path. A pending invitation always wins
      over the domain path so it grants the role it specifies.
    - ``auto_provision=true`` + EMPTY ``allowed_domains``: "anyone who
      authenticates" — DANGEROUS, gated behind the operator-level
      ``sso_unrestricted_provisioning`` feature flag (default OFF; OFF fails
      CLOSED at sign-in, keeping only existing members and invitations).
    An existing membership's role is NEVER changed. Denial happens BEFORE any
    Account/membership write so a denied identity leaves no orphan row, and
    raises :class:`SsoProvisioningDeniedError` (a ``ValueError`` subclass) so the
    SSO callback surfaces a clean 401 rather than a 500.
    """
    org_id = await _resolve_jit_org_id(session, default_org_id)

    if sso_provider is None:
        # Legacy env-var provider path: no DB row means no policy surface to
        # configure allowed_domains/auto_provision against. Kept as-is with a
        # loud log until env-var providers are migrated to DB rows.
        _log.warning("sso.jit_policy_not_applicable", extra={"auth_provider": auth_provider})
        account, org_role, _invitation_used = await _provision_membership(
            session, settings, email, display_name, auth_provider, sso_subject, org_id
        )
        return account, org_id, org_role

    # Pre-provision eligibility check: NOTHING is written below unless the
    # identity is allowed to join (fail-closed — a denial leaves no orphan row).
    existing_role = await _jit_existing_member_role(session, email, org_id)
    if existing_role is not None:
        # Already a member (e.g. SCIM/admin-provisioned): never change their role.
        account = await _upsert_sso_identity(session, email, display_name, auth_provider, sso_subject)
        return account, org_id, existing_role

    invitation = await get_live_for_email(session, org_id=org_id, email=email)

    unlocked = await _jit_join_unlocked(sso_provider, email, email_verified, org_id)
    if not unlocked and invitation is None:
        # No self-service path (mode 1, or mode 2/3 whose gate did not match)
        # and no pending invitation: fail closed before any write.
        await _deny_join(sso_provider, org_id)

    # A pending invitation always wins over the self-service path: it grants
    # the membership/role it specifies (including reactivating a tombstoned
    # member with that role). Consumption is the final CAS guard, in the same
    # transaction (mirrors accept-invite: a lost race aborts the whole
    # transaction) and only happens when the invitation actually granted or
    # restored the join - an existing live member keeps their role and never
    # burns an invitation.
    account, org_role, invitation_used = await _provision_membership(
        session,
        settings,
        email,
        display_name,
        auth_provider,
        sso_subject,
        org_id,
        provider=sso_provider,
        invitation=invitation,
    )

    if invitation is not None and invitation_used:
        await _consume_jit_invitation(session, invitation, org_id)

    return account, org_id, org_role


class Constants:
    """Small holder for user-facing SSO messages (kept near the join gate)."""

    MSG_SSO_JOIN_DENIED = "You have not been invited to this organisation."


class SsoProvisioningDeniedError(ValueError):
    """FAR-855 join-gate denial.

    Subclass of ``ValueError`` so SSO callers can distinguish a policy denial
    from a generic authentication failure. The message is user-safe and
    reveals nothing beyond what the login page already showed.

    ``org_id`` carries the org the identity was denied entry to (known at the
    denial site) so the callback route can resolve that org's slug and send the
    browser back to the org-scoped login page (FAR-1791). It is optional — the
    message stays the sole positional argument, so existing raisers and tests
    that construct ``SsoProvisioningDeniedError("...")`` keep working.
    """

    def __init__(self, message: str, *, org_id: uuid.UUID | None = None) -> None:
        super().__init__(message)
        self.org_id = org_id


def _sso_verified_domain(email: str) -> str | None:
    """Return the lowercased domain part of ``email`` (after the LAST ``@``)."""
    if "@" not in email:
        return None
    return email.rsplit("@", 1)[1].strip().lower() or None


def _sso_jit_mode_allows_auto(provider: SsoProvider, email: str, email_verified: bool) -> tuple[bool, str]:
    """Resolve the FAR-855 policy matrix for self-service (non-invitation) joins.

    Returns ``(self_service_allowed, mode)`` where ``mode`` is one of
     ``"invitation_only"``, ``"domain_allowlist"``, ``"unrestricted"``. Note the
     invitation path is still consulted when this returns False — an invitation
     grants the join even in the fail-closed states (mode 3 additionally
     requires the operator-level ``sso_unrestricted_provisioning`` flag,
     checked by the CALLER because it needs an async DB read).
    """
    mode = "invitation_only"
    if not provider.auto_provision:
        return False, mode

    domain = _sso_verified_domain(email)
    domains = [str(d).strip().lower() for d in (provider.allowed_domains or []) if str(d).strip()]
    if domains:
        mode = "domain_allowlist"
        if domain and email_verified and domain in domains:
            return True, mode
        return False, mode

    mode = "unrestricted"
    return email_verified, mode


async def _deny_join(  # NOSONAR S7503 - awaited helper preserves the SSO denial seam
    provider: SsoProvider, org_id: uuid.UUID
) -> None:
    """Structured, PII-free denial log + fail-closed user-facing raise."""
    _log.info(
        "sso.jit_join_denied",
        extra={
            "provider_id": getattr(provider, "provider_id", None),
            "provider_name": getattr(provider, "name", None),
            "organisation_id": str(org_id),
        },
    )
    raise SsoProvisioningDeniedError(Constants.MSG_SSO_JOIN_DENIED, org_id=org_id) from None


async def _audit_invite_consumed_fail_open(session: AsyncSession, org_id: uuid.UUID, invitation_id: uuid.UUID) -> None:
    """Record the SSO-consumed invitation; audit failure never fails the login.

    FAR-1825: ``append_audit_event`` takes the per-org audit-chain-head row lock
    (``FOR UPDATE``) and the SSO JIT transaction now bounds its lock waits
    (C17 ``get_live_for_email``). A contended chain head can therefore raise
    SQLSTATE 55P03.

    The append is already savepoint-contained: every attempt inside
    ``append_audit_event`` runs in its own ``session.begin_nested()``, so a
    55P03 rolls back only that attempt and never poisons the caller's
    transaction. This outer savepoint is explicit defence-in-depth: it states
    the fail-open guarantee locally so the token-issue writes that follow
    cannot be lost if that inner seam changes. It is not what prevents the
    poisoning today. Mirrors the REST twin in ``api/routes/auth.py``.
    """
    from modulo.core.audit_logger import append_audit_event

    try:
        # Defence-in-depth: ``append_audit_event`` already savepoints each
        # attempt; this outer savepoint makes the fail-open guarantee explicit
        # here rather than dependent on that inner seam.
        async with session.begin_nested():
            await append_audit_event(
                session,
                org_id=org_id,
                event_type="invite_consumed",
                resource_type="invitation",
                resource_id=invitation_id,
                payload_json={"invitation_id": str(invitation_id), "via": "sso"},
            )
    except Exception:
        _log.warning("sso.jit_invite_audit_failed", exc_info=True)


def _sso_default_role(provider: SsoProvider, settings: Settings) -> str:
    """FAR-839: SSO join role = provider.default_role, else the app setting."""
    return provider.default_role or settings.modulo_sso_default_role


async def _upsert_sso_identity(
    session: AsyncSession,
    email: str,
    display_name: str,
    auth_provider: str,
    sso_subject: str,
) -> Account:
    """Create the account for a JIT join, or bind the SSO identity to an existing one."""
    account = await get_account_by_email(session, email)
    if account is not None:
        account.sso_subject = sso_subject
        account.auth_provider = auth_provider
        await session.flush()
        return account
    account = await create_account(
        session,
        email=email,
        display_name=display_name,
        password_hash=None,
        auth_provider=auth_provider,
    )
    account.sso_subject = sso_subject
    await session.flush()
    _log.info(
        "sso.jit_provisioned",
        extra={"email": email, "auth_provider": auth_provider, "sso_subject": sso_subject},
    )
    return account


async def _provision_membership(
    session: AsyncSession,
    settings: Settings,
    email: str,
    display_name: str,
    auth_provider: str,
    sso_subject: str,
    org_id: uuid.UUID,
    *,
    provider: SsoProvider | None = None,
    invitation: Invitation | None = None,
) -> tuple[Account, str, bool]:
    """Legacy + gated provisioning core: upsert account, ensure membership.

    ``invitation`` (FAR-855) grants the invitation's role — including
    reactivating a tombstoned membership with that role (accept-invite
    semantics). With ``provider`` present the role falls back to
    ``provider.default_role`` then ``settings.modulo_sso_default_role``.
    An existing LIVE membership is left untouched; a tombstoned one is
    reactivated with the join role (an invitation to a deactivated member
    must restore their access, mirroring accept-invite).

    Returns ``(account, role, invitation_used)`` where ``invitation_used`` is
    True only when the invitation actually granted the join (created the
    membership or reactivated a tombstoned one) - the caller CAS-consumes the
    invitation in exactly that case, so an existing live member never burns
    one.
    """
    account = await _upsert_sso_identity(session, email, display_name, auth_provider, sso_subject)
    existing = await get_membership_by_account_and_org(session, account.id, org_id)
    if existing is not None and existing.deactivated_at is None:
        return account, existing.role, False

    if invitation is not None:
        role = invitation.org_role
    elif provider is not None:
        role = _sso_default_role(provider, settings)
    else:
        role = settings.modulo_sso_default_role

    _log.info("sso.jit_membership")
    if existing is not None and existing.deactivated_at is not None:
        await reactivate_membership(session, existing, role)
        return account, role, invitation is not None
    membership = await create_membership(session, account_id=account.id, org_id=org_id, role=role)
    return account, membership.role, invitation is not None


# ---------------------------------------------------------------------------
# Token issuance (same shape as existing LoginResponse)
# ---------------------------------------------------------------------------


async def apply_group_mappings(
    session: AsyncSession,
    account: Account,
    org_id: uuid.UUID,
    idp_groups: list[str],
    group_mappings: list[dict[str, str]],
) -> None:
    """Apply SSO group-to-team mappings for a JIT-provisioned account."""
    for mapping in group_mappings:
        if not isinstance(mapping, dict):
            _log.warning("sso.non_dict_mapping", extra={"mapping_type": type(mapping).__name__})
            continue
        idp_group = mapping.get("idp_group", "")
        if idp_group not in idp_groups:
            continue
        try:
            team_id = uuid.UUID(mapping["team_id"])
        except (ValueError, KeyError) as exc:
            _log.warning("sso.invalid_team_mapping", extra={"error": str(exc)})
            continue
        team_role = mapping.get("team_role", "viewer")

        existing = await get_membership_by_team_and_account(session, team_id, account.id)
        if existing is not None:
            if existing.role != team_role:
                await update_member_role(session, existing.id, team_role)
        else:
            await add_team_member(
                session,
                org_id=org_id,
                team_id=team_id,
                account_id=account.id,
                role=team_role,
            )


async def _lookup_provider_by_client_id(session: AsyncSession, client_id: str, org_id: uuid.UUID) -> SsoProvider | None:
    result = await session.execute(
        select(SsoProvider)
        .where(
            SsoProvider.client_id == client_id,
            SsoProvider.organisation_id == org_id,
        )
        .limit(1)
    )
    return result.scalar_one_or_none()


async def _lookup_provider_by_entity_id(session: AsyncSession, entity_id: str, org_id: uuid.UUID) -> SsoProvider | None:
    result = await session.execute(
        select(SsoProvider)
        .where(
            SsoProvider.entity_id == entity_id,
            SsoProvider.organisation_id == org_id,
        )
        .limit(1)
    )
    return result.scalar_one_or_none()


async def issue_sso_tokens(
    account: Account, org_id: uuid.UUID, org_role: str, session: AsyncSession, settings: Settings
) -> dict[str, str]:
    """Issue access + refresh tokens for an SSO-authenticated account."""
    await update_last_login(session, account.id)
    family = await create_family(session, account.id, org_id)

    access_token = create_access_token(
        account.email,
        settings.secret_key,
        organisation_id=str(org_id),
        account_id=str(account.id),
        org_role=org_role,
        ttl_minutes=settings.modulo_access_token_minutes,
        # FAR-634: SSO completes a browser redirect flow — explicit browser
        # class, same as the password login mint.
        client_kind=CLIENT_KIND_BROWSER,
    )
    refresh_token = create_refresh_token(
        account.email,
        settings.secret_key,
        organisation_id=str(org_id),
        account_id=str(account.id),
        org_role=org_role,
        token_family=str(family.family_id),
        token_sequence=0,
        ttl_hours=settings.modulo_refresh_token_ttl_hours,
        client_kind=CLIENT_KIND_BROWSER,
    )
    return {"access_token": access_token, "refresh_token": refresh_token, "token_type": "bearer"}  # nosec B105 — OAuth token_type label, not a credential


# ---------------------------------------------------------------------------
# OIDC helpers
# ---------------------------------------------------------------------------


async def _read_system_oidc_provider(
    system_session: AsyncSession | None,
    provider_id: str,
) -> SsoProvider | None:
    """Global (cross-org) OIDC provider read via the ``modulo_system`` role.

    Returns ``None`` when the system role is unprovisioned (falls back to the
    regular engine running as ``modulo_app`` NOBYPASSRLS, which returns zero
    rows) or the provider genuinely does not exist.
    """
    if system_session is None:
        return None
    async with system_session.begin():
        return await get_provider_by_provider_id(system_session, provider_id)


async def _read_oidc_provider_with_fallback(
    provider_id: str,
    system_session: AsyncSession | None,
    app_session: AsyncSession | None,
) -> SsoProvider | None:
    """Read the provider via the system role, else the app single-org fallback.

    FAR-1058 parity: the app fallback must run inside an active transaction.
    ``set_rls_org`` requires one (it raises ``RuntimeError`` otherwise), and the
    caller's app session may hand us the resolver with NO transaction open
    (``autobegin=False``). Open a scoped transaction when the caller has none
    (its LOCAL binding reverts at commit, so the binding cannot leak into a
    later caller transaction); read in place when the caller already holds one
    (the OIDC login / callback routes always wrap in ``session.begin()``).
    """
    db_provider = await _read_system_oidc_provider(system_session, provider_id)
    if db_provider is not None or app_session is None:
        return db_provider
    if app_session.in_transaction():
        await _set_default_rls_org(app_session)
        return await get_provider_by_provider_id(app_session, provider_id)
    async with app_session.begin():
        await _set_default_rls_org(app_session)
        return await get_provider_by_provider_id(app_session, provider_id)


async def _validate_oidc_discovery_url(discovery_url: str, provider_id: str) -> None:
    """SSRF-validate the DB provider's discovery URL, naming the provider on reject."""
    try:
        await validate_outbound_url_async(discovery_url)
    except ValueError as exc:
        raise ValueError(f"Rejected OIDC discovery_url for provider '{provider_id}': {exc}") from None


async def _oidc_config_from_db_provider(
    db_provider: SsoProvider | None,
    provider_id: str,
    settings: Settings,
) -> tuple[str | None, str | None, str | None, list[str] | str | None, SsoProvider | None] | None:
    """Config tuple from a usable DB provider row, else ``None`` (fall through to env)."""
    if db_provider is None or db_provider.provider_type != "oidc" or not db_provider.enabled:
        return None
    discovery_url = db_provider.discovery_url
    if discovery_url:
        await _validate_oidc_discovery_url(discovery_url, provider_id)
    return (
        db_provider.client_id,
        # NOSCOPE: instance-global OIDC IdP config, NOT tenant data. The
        # provider is read through the modulo_system role (BYPASSRLS) for
        # pre-auth SSO routes that carry no user/org claim, so there is no
        # per-request RLS org to bind the decrypt to — org-scoping here would
        # be a cross-context error, not a hardening gain. Kept on the pure
        # helper deliberately; the no-unscoped-decrypt gate allows it via the
        # nosemgrep below.
        # nosemgrep: no-unscoped-decrypt
        decode_stored_secret(db_provider.client_secret, settings.fernet_key) if db_provider.client_secret else None,
        discovery_url,
        json.loads(db_provider.scopes) if db_provider.scopes else None,
        db_provider,
    )


async def _resolve_oidc_provider(
    provider_id: str,
    system_session: AsyncSession | None,
    app_session: AsyncSession | None,
    settings: Settings,
) -> tuple[str | None, str | None, str | None, list[str] | str | None, SsoProvider | None]:
    """Resolve OIDC IdP config: system (global) read, then app single-org, then env.

    The pre-auth SSO routes have no user/org claim. Provider config is
    instance-global (not tenant data), so the primary read goes through the
    ``modulo_system`` role (BYpassRLS) which sees every org. When the system
    read returns nothing (system role unprovisioned -> zero rows, or the
    provider genuinely absent), we fall back to the app session scoped to the
    first org (single-org self-hosted behaviour) before the env-var fallback.

    Returns ``(client_id, client_secret, discovery_url, scopes, db_provider)``.
    ``db_provider`` is the ``SsoProvider`` row when the config came from the DB
    (used for JIT org placement and SSRF validation); ``None`` for the env path.
    Returns all-``None`` when the provider is not configured at all.
    """
    db_provider = await _read_oidc_provider_with_fallback(provider_id, system_session, app_session)
    db_config = await _oidc_config_from_db_provider(db_provider, provider_id, settings)
    if db_config is not None:
        return db_config

    providers = _parse_oidc_providers(settings)
    provider = next((p for p in providers if p["provider_id"] == provider_id), None)
    if not provider:
        return None, None, None, None, None
    return (
        provider["client_id"],
        provider["client_secret"],
        provider["discovery_url"],
        provider.get("scopes"),
        None,
    )


# FAR-1982: an authorization request must be bound to the request that started
# it. ``nonce`` binds the ID token to the request (verified in
# ``verify_id_token``); the PKCE ``code_verifier``/``code_challenge`` pair binds
# the authorization-code exchange to the client instance that began the flow.
# Both are generated per-login and returned alongside the state so the route can
# correlate them across the IdP round-trip.
_OIDC_NONCE_BYTES = 32
_PKCE_VERIFIER_BYTES = 64  # token_urlsafe(64) -> 86 chars, within RFC 7636's 43..128


def _generate_pkce_verifier() -> str:
    """Generate an RFC 7636 §4.1 code verifier (unreserved chars, 43-128 long)."""
    return secrets.token_urlsafe(_PKCE_VERIFIER_BYTES)


async def oidc_get_authorize_url(
    provider_id: str,
    settings: Settings,
    redirect_uri: str,
    system_session: AsyncSession | None,
    app_session: AsyncSession | None = None,
) -> tuple[str, str, str, str]:
    """Build the OIDC authorization URL and return (url, raw_state, nonce, code_verifier).

    Resolves IdP config from the sso_providers DB table first (preferred, since
    the admin UI writes there); falls back to env-var providers for backward
    compatibility. Provider resolution uses the system session (``modulo_system``
    role, instance-global) so multi-org deployments resolve the correct provider
    regardless of org; the app session is used only for the single-org fallback.

    FAR-1982: also mints a per-login ``nonce`` (sent to the IdP and checked in
    the returned ID token) and a PKCE ``code_verifier`` (whose S256 challenge is
    sent to the IdP and whose verifier is presented at the token exchange).
    """
    client_id, _client_secret, discovery_url, scopes, _db_provider = await _resolve_oidc_provider(
        provider_id, system_session, app_session, settings
    )

    if client_id is None:
        raise ValueError(f"OIDC provider '{provider_id}' not configured")
    if not client_id or not discovery_url:
        raise ValueError(f"OIDC provider '{provider_id}' is missing client_id or discovery_url")

    try:
        if _db_provider is not None:
            disc = await _fetch_discovery_pinned(discovery_url)
        else:
            disc = await _fetch_discovery(discovery_url)
    except httpx.HTTPError as exc:
        raise ValueError(f"Failed to fetch discovery document: {exc}") from None
    auth_endpoint = disc.get("authorization_endpoint")
    if not isinstance(auth_endpoint, str) or not auth_endpoint:
        raise ValueError("No authorization_endpoint in discovery document")
    raw_issuer = disc.get("issuer")
    issuer_for_hosts = raw_issuer if isinstance(raw_issuer, str) else None
    _enforce_oidc_endpoint_host(auth_endpoint, discovery_url, issuer_for_hosts, "authorization")

    raw_state = str(uuid.uuid4())
    signed = sign_state(f"{provider_id}:{raw_state}", settings.secret_key)
    # FAR-1982: per-login nonce (ID-token binding) and PKCE verifier (code
    # exchange binding). Only the S256 challenge leaves this server; the
    # verifier stays in the correlation cookie until the callback.
    nonce = secrets.token_urlsafe(_OIDC_NONCE_BYTES)
    code_verifier = _generate_pkce_verifier()

    if isinstance(scopes, list):
        scope = " ".join(scopes) if scopes else OIDC_DEFAULT_SCOPES
    elif isinstance(scopes, str):
        scope = scopes.strip() or OIDC_DEFAULT_SCOPES
    else:
        scope = OIDC_DEFAULT_SCOPES
    params = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "response_type": "code",
            "scope": scope,
            "redirect_uri": redirect_uri,
            "state": signed,
            "nonce": nonce,
            "code_challenge": compute_pkce_challenge(code_verifier),
            "code_challenge_method": "S256",
        }
    )
    return f"{auth_endpoint}?{params}", raw_state, nonce, code_verifier


def _oidc_provider_id_from_state(state: str, settings: Settings) -> str:
    """Verify the signed state and return the provider id embedded in it."""
    state_data = verify_state(state, settings.secret_key)
    if not state_data:
        _log.warning(
            "sso.csrf_state_mismatch", extra={"state_prefix": state[:20] + "..." if len(state) > 20 else state}
        )
        raise ValueError("Invalid state parameter — possible CSRF")
    return state_data.split(":", 1)[0] if ":" in state_data else state_data


async def _resolve_oidc_client_config(
    provider_id: str,
    system_session: AsyncSession | None,
    app_session: AsyncSession | None,
    settings: Settings,
) -> tuple[str, str, str, SsoProvider | None]:
    """Resolve and validate the provider's client config for the callback leg."""
    client_id, client_secret, discovery_url, _scopes, db_provider = await _resolve_oidc_provider(
        provider_id, system_session, app_session, settings
    )
    if client_id is None:
        raise ValueError(f"OIDC provider '{provider_id}' not found")
    if not client_id or not client_secret or not discovery_url:
        raise ValueError(f"OIDC provider '{provider_id}' is missing required configuration")
    return client_id, client_secret, discovery_url, db_provider


async def _fetch_oidc_discovery_document(
    discovery_url: str,
    db_provider: SsoProvider | None,
) -> dict[str, object]:
    """Fetch the discovery document (pinned for a DB provider), mapping HTTP errors."""
    try:
        if db_provider is not None:
            return await _fetch_discovery_pinned(discovery_url)
        return await _fetch_discovery(discovery_url)
    except httpx.HTTPError as exc:
        raise ValueError(f"Failed to fetch discovery document: {exc}") from None


def _oidc_token_endpoint(disc: dict[str, object], discovery_url: str) -> str:
    """Extract and host-validate the token endpoint from the discovery document."""
    token_endpoint = disc.get("token_endpoint")
    if not isinstance(token_endpoint, str) or not token_endpoint:
        raise ValueError("No token_endpoint in discovery document")
    raw_issuer = disc.get("issuer")
    issuer_for_hosts = raw_issuer if isinstance(raw_issuer, str) else None
    _enforce_oidc_endpoint_host(token_endpoint, discovery_url, issuer_for_hosts, "token")
    return token_endpoint


async def _exchange_oidc_code(
    token_endpoint: str,
    client_id: str,
    client_secret: str,
    code: str,
    redirect_uri: str,
    code_verifier: str,
) -> dict[str, object]:
    """Redeem the authorization code, mapping HTTP errors to a ValueError."""
    try:
        return await _exchange_code(
            token_endpoint,
            client_id,
            client_secret,
            code,
            redirect_uri,
            code_verifier,
        )
    except httpx.HTTPError as exc:
        raise ValueError(f"Failed to exchange authorization code: {exc}") from None


async def _verify_oidc_id_token(
    disc: dict[str, object],
    token_data: dict[str, object],
    client_id: str,
    discovery_url: str,
    expected_nonce: str,
) -> dict[str, Any]:
    """Require, host-validate and signature-verify the ID token; return its claims."""
    id_token = token_data.get("id_token")
    if not isinstance(id_token, str) or not id_token:
        raise ValueError("OIDC token response is missing a valid id_token")

    jwks_uri = disc.get("jwks_uri")
    issuer = disc.get("issuer")
    if not isinstance(jwks_uri, str) or not jwks_uri or not isinstance(issuer, str) or not issuer:
        raise ValueError(
            "OIDC provider discovery document is missing jwks_uri or issuer — "
            "cannot verify ID token signature. Check provider configuration."
        )
    _enforce_oidc_endpoint_host(jwks_uri, discovery_url, issuer, "jwks")

    try:
        return await verify_id_token(id_token, jwks_uri, client_id, issuer, expected_nonce=expected_nonce)
    except OidcVerifyError as exc:
        raise ValueError(str(exc)) from None


def _oidc_identity_from_claims(claims: dict[str, Any], provider_id: str) -> tuple[str, str, str]:
    """(email, display name, SSO subject) from verified OIDC claims."""
    email = claims.get("email", "") or claims.get("sub", "")
    if not email:
        raise ValueError("OIDC provider did not return an email or sub claim — cannot provision account")
    name = claims.get("name", "") or claims.get("preferred_username", "") or email.split("@")[0]
    sso_subject = f"{provider_id}:{claims.get('sub', email)}"
    return email, name, sso_subject


async def _oidc_jit_provision(
    app_session: AsyncSession,
    settings: Settings,
    email: str,
    name: str,
    sso_subject: str,
    default_org_id: uuid.UUID | None,
    db_provider: SsoProvider | None,
    claims: dict[str, Any],
) -> tuple[Account, uuid.UUID, str]:
    """JIT-provision the OIDC identity, mapping provisioning failures to ValueError."""
    try:
        return await jit_provision_user(
            app_session,
            settings,
            email,
            name,
            "oidc",
            sso_subject,
            default_org_id=default_org_id,
            sso_provider=db_provider,
            # FAR-855: a domain-allowlist join requires a VERIFIED email — the
            # IdP's email_verified claim; a missing claim is NOT verified.
            email_verified=bool(claims.get("email_verified")),
        )
    except RuntimeError as exc:
        raise ValueError(str(exc)) from None


async def _apply_oidc_group_mappings(
    app_session: AsyncSession,
    claims: dict[str, Any],
    account: Account,
    org_id: uuid.UUID,
    client_id: str,
) -> None:
    """Apply IdP group-to-team mappings for the provisioned account, when configured."""
    raw_groups = claims.get("groups", [])
    if not isinstance(raw_groups, list):
        raw_groups = []
    idp_groups: list[str] = raw_groups
    if not idp_groups:
        return
    db_provider_for_groups = await _lookup_provider_by_client_id(app_session, client_id, org_id)
    if db_provider_for_groups is not None and db_provider_for_groups.group_mappings:
        await apply_group_mappings(app_session, account, org_id, idp_groups, db_provider_for_groups.group_mappings)


async def oidc_process_callback(
    code: str,
    state: str,
    settings: Settings,
    system_session: AsyncSession | None,
    app_session: AsyncSession | None,
    redirect_uri: str,
    *,
    expected_nonce: str,
    code_verifier: str,
) -> dict[str, str]:
    """Exchange auth code for tokens, JIT provision account, return JWT pair.

    Provider resolution uses the system session (``modulo_system`` role,
    instance-global) so the correct provider is resolved across orgs. JIT
    provisioning writes via the app session, RLS-scoped to the resolved
    provider's org (or the first-org fallback for the env/single-org path).

    FAR-1982: ``code_verifier`` is the PKCE verifier minted by the login leg
    (proof the exchange is made by the client instance that started the flow)
    and ``expected_nonce`` is the per-login nonce to require on the ID token.
    Both are REQUIRED — a callback that cannot present them must not exchange.
    """
    provider_id = _oidc_provider_id_from_state(state, settings)
    client_id, client_secret, discovery_url, db_provider = await _resolve_oidc_client_config(
        provider_id, system_session, app_session, settings
    )
    disc = await _fetch_oidc_discovery_document(discovery_url, db_provider)
    token_endpoint = _oidc_token_endpoint(disc, discovery_url)
    token_data = await _exchange_oidc_code(
        token_endpoint,
        client_id,
        client_secret,
        code,
        redirect_uri,
        code_verifier,
    )
    claims = await _verify_oidc_id_token(disc, token_data, client_id, discovery_url, expected_nonce)
    email, name, sso_subject = _oidc_identity_from_claims(claims, provider_id)

    if app_session is None:
        raise RuntimeError("No app session available for OIDC JIT provisioning")

    default_org_id = await _scope_app_session_org(app_session, db_provider)
    account, org_id, org_role = await _oidc_jit_provision(
        app_session, settings, email, name, sso_subject, default_org_id, db_provider, claims
    )
    await _apply_oidc_group_mappings(app_session, claims, account, org_id, client_id)

    return await issue_sso_tokens(account, org_id, org_role, app_session, settings)


def _parse_oidc_providers(settings: Settings) -> list[dict[str, str]]:
    if not settings.modulo_oidc_providers:
        return []
    try:
        entries = json.loads(settings.modulo_oidc_providers)
    except (json.JSONDecodeError, TypeError) as exc:
        _log.warning("sso.oidc_invalid_json", extra={"error": str(exc)})
        return []
    if not isinstance(entries, list):
        _log.warning("sso.oidc_not_array", extra={"type": type(entries).__name__})
        return []
    valid = []
    required_fields = ("provider_id", "client_id", "client_secret", "discovery_url")
    for entry in entries:
        if isinstance(entry, dict) and not any(key not in entry for key in required_fields):
            valid.append(entry)
        else:
            safe_entry = (
                {key: value for key, value in entry.items() if key != "client_secret"}
                if isinstance(entry, dict)
                else {"invalid_type": type(entry).__name__}
            )
            _log.warning("sso.oidc_entry_missing_fields", extra={"entry": str(safe_entry)})
    return valid


# public alias for backwards compatibility
parse_oidc_providers = _parse_oidc_providers


def _require_json_object(value: object, context: str) -> dict[str, object]:
    """Validate and precisely type an object decoded from JSON."""
    if not isinstance(value, dict):
        raise ValueError(f"{context} must be a JSON object")

    result: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise ValueError(f"{context} contains a non-string key")
        result[key] = item
    return result


async def _fetch_discovery(discovery_url: str) -> dict[str, object]:
    # Pinned client: SSRF-validates + pins the connect so the base discovery
    # fetch on the env-var path is protected exactly like the DB-provider path.
    async with await pinned_async_client(discovery_url) as client:
        resp = await client.get(discovery_url, timeout=httpx.Timeout(10.0, connect=5.0))
        resp.raise_for_status()
        try:
            decoded = resp.json()
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON in discovery document: {exc}") from None
        return _require_json_object(decoded, "OIDC discovery document")


async def _fetch_discovery_pinned(discovery_url: str) -> dict[str, object]:
    async with await pinned_async_client(discovery_url) as client:
        resp = await client.get(discovery_url, timeout=httpx.Timeout(10.0, connect=5.0))
        resp.raise_for_status()
        try:
            decoded = resp.json()
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON in discovery document: {exc}") from None
        return _require_json_object(decoded, "OIDC discovery document")


def _enforce_oidc_endpoint_host(url: str, discovery_url: str, issuer: str | None, purpose: str) -> None:
    """Reject a remote-supplied OIDC endpoint that points at an unsafe host.

    NOTE (FAR-506): the exact-host allowlist was an over-restrictive secondary
    boundary that broke legitimate multi-host IdPs. Google's real discovery
    document returns ``token_endpoint`` on oauth2.googleapis.com and
    ``jwks_uri`` on www.googleapis.com — sibling hosts of the discovery/issuer
    host (accounts.google.com). The **pinned client** is the real SSRF boundary:
    it pins the validated non-internal IP, closes the DNS-rebinding window, and
    blocks metadata/loopback. This check is therefore defense-in-depth only and
    rejects what a pinned client could never safely reach anyway — a non-HTTPS
    URL, an embedded-userinfo URL, or a literal-IP internal/loopback/metadata
    host. The discovery/issuer-host allowlist is kept as a preferred-log nicety:
    a cross-host (multi-host IdP) endpoint is logged as a warning, never
    rejected.
    """
    if urllib.parse.urlparse(url).scheme != "https":
        raise ValueError(f"Rejected OIDC {purpose} endpoint: URL must use https:// scheme")
    try:
        validate_outbound_url_preflight(url)
    except ValueError as exc:
        raise ValueError(f"Rejected OIDC {purpose} endpoint: {exc}") from None

    # Preferred-log nicety: warn (but do NOT reject) when a derived endpoint lives
    # on a sibling host. Legitimate for multi-host IdPs (Google etc.).
    try:
        require_url_host_in_allowlist(url, derive_oidc_allowed_hosts(discovery_url, issuer))
    except ValueError:
        _log.warning("sso.oidc_endpoint_cross_host", extra={"purpose": purpose, "url": url})


async def _exchange_code(
    token_endpoint: str,
    client_id: str,
    client_secret: str,
    code: str,
    redirect_uri: str,
    code_verifier: str,
) -> dict[str, object]:
    # Pinned client: the token endpoint is a remote-supplied URL. Pinning
    # validates + pins the connect so the ``client_secret`` in the POST body is
    # only ever disclosed to a validated, allowlisted host — never a hostile or
    # internal/metadata target. The caller enforces the host allowlist before
    # calling here.
    #
    # FAR-1982: ``code_verifier`` is presented here so the token endpoint binds
    # the authorization code to this client instance (RFC 7636 §4.5).
    async with await pinned_async_client(token_endpoint) as client:
        resp = await client.post(
            token_endpoint,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": client_id,
                "client_secret": client_secret,
                "code_verifier": code_verifier,
            },
            headers={"Accept": "application/json"},
            timeout=httpx.Timeout(15.0, connect=5.0),
        )
        resp.raise_for_status()
        try:
            decoded = resp.json()
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON in OIDC token response: {exc}") from None
        return _require_json_object(decoded, "OIDC token response")


def _decode_id_token_claims(id_token: str) -> dict[str, object]:
    """Decode ID token claims without signature verification.

    The code exchange with the token endpoint is over HTTPS (transport-level
    security), but this does not protect against a compromised token endpoint
    or a malicious IdP. In production, verify the JWT signature using the
    provider's JWKS endpoint and validate the ``iss`` and ``aud`` claims.
    """
    _log.warning("sso.id_token_no_verify")
    parts = id_token.split(".")
    if len(parts) != 3:
        return {}
    try:
        pad = (4 - len(parts[1]) % 4) % 4
        padded = parts[1] + "=" * pad
        decoded = json.loads(base64.urlsafe_b64decode(padded))
        return _require_json_object(decoded, "OIDC ID token claims")
    except ValueError as exc:
        _log.warning("sso.id_token_decode_failed", extra={"error": str(exc)})
        return {}


# ---------------------------------------------------------------------------
# SAML helpers
# ---------------------------------------------------------------------------


async def _read_system_saml_provider(system_session: AsyncSession | None) -> SsoProvider | None:
    """Global (cross-org) SAML provider read via the ``modulo_system`` role.

    Returns ``None`` when the system role is unprovisioned (returns zero rows)
    or no enabled SAML provider exists. Pre-auth SAML is a single-provider flow,
    so the first enabled provider globally is the one used.
    """
    if system_session is None:
        return None
    async with system_session.begin():
        return await get_enabled_saml_provider(system_session)


async def _read_system_saml_provider_by_slug(
    system_session: AsyncSession | None,
    provider_id: str,
) -> SsoProvider | None:
    """Global (cross-org) SAML provider read by slug via the ``modulo_system`` role.

    Returns ``None`` when the system role is unprovisioned (zero rows), the slug
    does not match any provider, or the matched provider is not an enabled SAML
    provider.
    """
    if system_session is None:
        return None
    async with system_session.begin():
        provider = await get_provider_by_provider_id(system_session, provider_id)
        if provider is not None and provider.provider_type == "saml" and provider.enabled:
            return provider
    return None


async def _resolve_saml_provider(
    system_session: AsyncSession | None,
    app_session: AsyncSession | None,
    provider_id: str | None,
) -> SsoProvider | None:
    """Look up the DB SAML provider row: system (global) read, then app single-org.

    When ``provider_id`` is given, resolves a SPECIFIC provider by its globally
    unique slug (FAR-1001). When ``None``, resolves the first enabled SAML
    provider globally (legacy single-tenant convenience).

    The pre-auth SAML routes have no user/org claim. IdP config is
    instance-global, so the primary read goes through the ``modulo_system`` role
    (BYPASSRLS) which sees every org. When the system read returns nothing, fall
    back to the app session scoped to the first org (single-org self-hosted
    behaviour). Returns ``None`` when no DB row applies, so the caller can use
    the env-var fallback path.
    """
    if provider_id is not None:
        db_saml = await _read_system_saml_provider_by_slug(system_session, provider_id)
        if db_saml is None and app_session is not None:
            await _set_default_rls_org(app_session)
            provider_by_slug = await get_provider_by_provider_id(app_session, provider_id)
            if provider_by_slug is not None and provider_by_slug.provider_type == "saml" and provider_by_slug.enabled:
                db_saml = provider_by_slug
        return db_saml
    db_saml = await _read_system_saml_provider(system_session)
    if db_saml is None and app_session is not None:
        await _set_default_rls_org(app_session)
        db_saml = await get_enabled_saml_provider(app_session)
    return db_saml


async def _saml_config_from_db(
    provider_id: str | None,
    db_saml: SsoProvider,
    settings: Settings,
) -> tuple[str, str, str | None, str | None, SsoProvider]:
    """Build the resolved SAML config tuple from a DB provider row.

    Admin-configured ``metadata_url`` is SSRF-validated and fetched with
    explicit error handling; the connect is pinned to the validated address
    rather than re-resolved at request time (FAR-517). ``entity_id`` falls
    back to the per-provider SP URL (FAR-1001) or the legacy env setting for
    the singleton flow.
    """
    idp_metadata = db_saml.metadata_xml or None
    if not idp_metadata and db_saml.metadata_url:
        try:
            await validate_outbound_url_async(db_saml.metadata_url)
        except ValueError as exc:
            raise ValueError(f"Rejected SAML metadata_url for provider: {exc}") from None
        try:
            async with await pinned_async_client(db_saml.metadata_url) as client:
                resp = await client.get(db_saml.metadata_url, timeout=httpx.Timeout(15.0, connect=5.0))
                resp.raise_for_status()
                idp_metadata = resp.text
        except httpx.HTTPError as exc:
            raise ValueError("Failed to fetch SAML IdP metadata from provider metadata_url") from exc
    if not idp_metadata:
        raise ValueError("SAML provider is missing IdP metadata (set metadata_xml or metadata_url)")
    # Per-provider entity_id default (FAR-1001): when the DB provider
    # has no entity_id set, default to {public_url}/saml/{provider_id}
    # for per-provider routes, or the legacy env setting for singleton.
    if provider_id is not None:
        default_entity = f"{get_public_url(settings).rstrip('/')}/api/v1/auth/saml/{provider_id}"
    else:
        default_entity = settings.modulo_saml_entity_id or "modulo"
    entity_id = db_saml.entity_id or default_entity
    sp_key = settings.modulo_saml_sp_private_key or None
    sp_cert = settings.modulo_saml_sp_x509_cert or None
    return idp_metadata, entity_id, sp_key, sp_cert, db_saml


async def _saml_config_from_env(settings: Settings) -> tuple[str, str, str | None, str | None, None]:
    """Fetch env-var SAML IdP metadata and build the resolved config tuple.

    Availability is a CONFIGURATION question (``modulo_saml_enabled`` plus IdP
    metadata), never a licence question (FAR-2059). Sign-in is an
    authentication concern: refusing to build the config on a lapsed licence
    locks every administrator out of the instance — and therefore out of the
    licence screen — which is precisely the state the product promises can
    never happen. Earning the Team tier is enforced where it belongs:
    ``require_feature("sso")`` gates the admin configuration surface, and the
    post-auth plan gates licensed runtime features.
    """
    try:
        idp_metadata = await _saml_fetch_idp_metadata(settings)
    except (httpx.HTTPError, ValueError) as exc:
        raise ValueError(f"Failed to fetch IdP metadata: {exc}") from None
    return (
        idp_metadata,
        settings.modulo_saml_entity_id,
        settings.modulo_saml_sp_private_key or None,
        settings.modulo_saml_sp_x509_cert or None,
        None,
    )


async def _resolve_saml_config(
    system_session: AsyncSession | None,
    app_session: AsyncSession | None,
    settings: Settings,
    *,
    provider_id: str | None = None,
) -> tuple[str, str, str | None, str | None, SsoProvider | None]:
    """Resolve SAML IdP config: system (global) read, then app single-org, then env.

    When ``provider_id`` is given, resolves a SPECIFIC provider by its globally
    unique slug (FAR-1001). When ``None``, resolves the first enabled SAML
    provider globally (legacy single-tenant convenience).

    Returns ``(idp_metadata_xml, entity_id, sp_private_key, sp_x509_cert,
    db_provider)``. ``entity_id`` is ``db_saml.entity_id or
    settings.modulo_saml_entity_id`` (used by the SP handler). ``db_provider`` is
    the row when config came from the DB (used for JIT org placement); ``None``
    for the env path.
    """
    db_saml = await _resolve_saml_provider(system_session, app_session, provider_id)
    if db_saml is not None:
        return await _saml_config_from_db(provider_id, db_saml, settings)

    # Env-var fallback: CONFIGURATION decides availability, never licence state
    # (FAR-2059). An instance whose only sign-in path is env-configured SAML
    # must still be able to complete SAML login on a lapsed licence; otherwise
    # the lapse locks every administrator out of the licence screen with no way
    # back in. The Team entitlement is enforced at the admin configuration
    # surface (``require_feature("sso")``) and post-auth, not here.
    if not settings.modulo_saml_enabled:
        raise ValueError("SAML is not enabled")

    return await _saml_config_from_env(settings)


async def saml_get_auth_url(
    settings: Settings,
    acs_url: str,
    system_session: AsyncSession | None,
    app_session: AsyncSession | None = None,
    *,
    provider_id: str | None = None,
    relay_state: str | None = None,
) -> tuple[str, str]:
    """Generate a SAML AuthnRequest using python3-saml and return (IdP redirect URL, _).

    python3-saml handles proper XML construction, signing (if SP key configured),
    and encoding. The second return value (request_id) is no longer used by the
    caller but kept for API compatibility.

    When ``provider_id`` is given, resolves the SPECIFIC provider by its globally
    unique slug (FAR-1001). When ``None``, resolves the first enabled SAML
    provider globally (legacy single-tenant convenience).

    When ``relay_state`` is given (a signed HMAC token from
    ``sign_saml_relay_state``), it is embedded in the IdP redirect URL as
    the RelayState parameter — defense-in-depth for per-provider SAML.
    Absence is tolerated (IdP-initiated SSO may not send it).

    Resolves IdP config through the sso_providers DB table first (preferred, since
    the admin UI writes there); falls back to env-var config for backward
    compatibility. Provider resolution uses the system session (``modulo_system``
    role, instance-global) so multi-org deployments resolve the correct IdP.
    """
    idp_metadata, entity_id, sp_key, sp_cert, _db_saml = await _resolve_saml_config(
        system_session, app_session, settings, provider_id=provider_id
    )
    handler = ModuloSamlAuth(
        entity_id=entity_id,
        acs_url=acs_url,
        idp_metadata_xml=idp_metadata,
        sp_private_key=sp_key,
        sp_x509_cert=sp_cert,
    )
    try:
        auth_url = handler.get_auth_url(return_to=relay_state)
    except Exception as exc:
        raise ValueError(f"Failed to generate SAML AuthnRequest: {exc}") from None
    return auth_url, ""


def _decode_saml_response(saml_response: str) -> bytes:
    """Decode a base64 SAML Response, normalising padding for urlsafe input.

    Raises ValueError on base64 decode failure (mirrors the existing error
    contract so the ACS route returns 401).
    """
    try:
        return base64.b64decode(saml_response, validate=False)
    except ValueError as exc:
        raise ValueError(f"Invalid base64 SAML response: {exc}") from None


def _validate_saml_response_destination(saml_response: str, acs_url: str) -> None:
    """Validate the SAML Response ``Destination`` matches the configured ACS URL.

    SAML 2.0 Core §4.1.1 requires the Response ``Destination`` to match the
    SP's ACS endpoint. A mismatched Destination is a replay/misdelivery signal
    and MUST be rejected. The attribute may be legitimately absent from some
    IdPs, in which case validation is skipped here (python3-saml strict mode
    only enforces it when the attribute is present; strict mode IS enabled —
    python3-saml defaults ``strict=True``, verified by the FAR-1000 spike).

    NOTE (per-provider SAML, FAR-1000 design): once per-provider ACS URLs
    land, this function must be called with the RESOLVED PROVIDER's ACS URL
    instead of the single instance-wide one — a hardcoded legacy ACS URL
    here would reject responses destined for a provider-specific endpoint.
    """
    try:
        raw = _decode_saml_response(saml_response)
    except ValueError:
        return
    try:
        root = ElementTree.fromstring(raw)
    except ElementTree.ParseError:
        return
    destination = root.get("Destination")
    if destination is None:
        return
    if destination.rstrip("/") != acs_url.rstrip("/"):
        _log.warning(
            "sso.saml_destination_mismatch",
            extra={"expected": acs_url, "actual": destination},
        )
        raise ValueError("SAML response Destination does not match the ACS URL")


def _saml_idp_entity_id(idp_metadata: str) -> str:
    """Parse the IdP metadata, mapping parse failures to a ValueError."""
    try:
        _, idp_entity_id = _saml_parse_idp_metadata(idp_metadata)
    except (ElementTree.ParseError, ValueError) as exc:
        raise ValueError(f"Failed to parse IdP metadata: {exc}") from None
    return idp_entity_id


def _saml_acs_url(settings: Settings, provider_id: str | None) -> str:
    """The ACS URL for this response: per-provider when named, else the legacy singleton."""
    # Per-provider ACS URL (FAR-1001) when provider_id is given; legacy
    # singleton ACS URL for the single-tenant convenience path.
    public_url = get_public_url(settings).rstrip("/")
    if provider_id:
        return f"{public_url}/api/v1/auth/saml/acs/{provider_id}"
    return f"{public_url}/api/v1/auth/saml/acs"


def _saml_process_result(handler: ModuloSamlAuth, saml_response: str) -> dict[str, Any]:
    """Run python3-saml validation, mapping signature failures to a ValueError."""
    try:
        return handler.process_response(saml_response)
    except SamlAuthError as exc:
        _log.warning("sso.saml_signature_validation_failed", extra={"error": str(exc)})
        raise ValueError(str(exc)) from None


def _saml_attributes(result: dict[str, Any]) -> tuple[str, dict[str, str]]:
    """(name_id, flattened attribute map) from a validated SAML assertion."""
    name_id = result["name_id"]
    raw_attrs = result["attributes"]
    return name_id, {attr_name: ",".join(values) for attr_name, values in raw_attrs.items()}


def _saml_identity(name_id: str, attrs: dict[str, str], idp_entity_id: str) -> tuple[str, str, str]:
    """(email, display name, SSO subject) from a validated SAML assertion."""
    email = attrs.get("email", "") or attrs.get("Email", "") or name_id or ""
    if not email:
        raise ValueError("SAML provider did not return an email attribute — cannot provision account")
    display_name = (
        attrs.get("displayName", "")
        or attrs.get("cn", "")
        or attrs.get("firstName", "")
        or (email.split("@")[0] if "@" in email else email)
    )
    sso_subject = f"saml:{idp_entity_id}:{name_id}"
    return email, display_name, sso_subject


def _saml_groups(attrs: dict[str, str]) -> list[str]:
    """The first non-empty group attribute, split into individual group names."""
    for group_attr in ("groups", "memberOf", "Group"):
        raw = attrs.get(group_attr, "")
        if raw:
            return [g.strip() for g in raw.split(",") if g.strip()]
    return []


async def _apply_saml_group_mappings(
    app_session: AsyncSession,
    saml_groups: list[str],
    account: Account,
    org_id: uuid.UUID,
    idp_entity_id: str,
) -> None:
    """Apply SAML group-to-team mappings for the provisioned account, when configured."""
    if not saml_groups:
        return
    db_provider = await _lookup_provider_by_entity_id(app_session, idp_entity_id, org_id)
    if db_provider is not None and db_provider.group_mappings:
        await apply_group_mappings(app_session, account, org_id, saml_groups, db_provider.group_mappings)


async def _saml_jit_provision(
    app_session: AsyncSession,
    settings: Settings,
    email: str,
    display_name: str,
    sso_subject: str,
    default_org_id: uuid.UUID | None,
    db_saml: SsoProvider | None,
) -> tuple[Account, uuid.UUID, str]:
    """JIT-provision the SAML identity, mapping provisioning failures to ValueError."""
    try:
        return await jit_provision_user(
            app_session,
            settings,
            email,
            display_name,
            "saml",
            sso_subject,
            default_org_id=default_org_id,
            sso_provider=db_saml,
            email_verified=True,
        )
    except RuntimeError as exc:
        raise ValueError(str(exc)) from None


async def _issue_saml_tokens(
    account: Account,
    org_id: uuid.UUID,
    org_role: str,
    app_session: AsyncSession,
    settings: Settings,
) -> dict[str, str]:
    """Issue the SSO token pair, mapping issuance failures to a ValueError."""
    try:
        return await issue_sso_tokens(account, org_id, org_role, app_session, settings)
    except RuntimeError as exc:
        raise ValueError(str(exc)) from None


async def saml_process_response(
    saml_response: str,
    settings: Settings,
    system_session: AsyncSession | None,
    app_session: AsyncSession | None,
    *,
    provider_id: str | None = None,
) -> dict[str, str]:
    """Validate a SAML Response using python3-saml and issue tokens.

    python3-saml provides XML signature verification using the IdP's X.509
    certificate from metadata (the critical security gap in the old
    implementation), plus condition validation, audience restriction, and
    clock-skew management.

    When ``provider_id`` is given, resolves the SPECIFIC provider by its globally
    unique slug (FAR-1001). When ``None``, resolves the first enabled SAML
    provider globally (legacy single-tenant convenience).

    Resolves IdP metadata from the sso_providers DB table first (preferred, since
    the admin UI writes there); falls back to env-var config for backward
    compatibility. Provider resolution uses the system session (``modulo_system``
    role, instance-global); JIT provisioning writes via the app session,
    RLS-scoped to the resolved provider's org (or first-org fallback).

    Replay posture (accepted, FAR-1006 review): responses are processed as
    HTTP-POST bearer assertions without ``InResponseTo``/AuthnRequest-ID
    correlation. There is no cross-request store of outstanding AuthnRequest
    IDs, so an observed (network-captured, TLS-terminated) Response could be
    replayed within its ``NotOnOrAfter`` window. This matches the standard
    web-SSO bearer choice python3-saml itself implements; hardening it
    (per-runtime AuthnRequest store) is tracked as a future defence-in-depth
    item, not required for correctness here.
    """
    idp_metadata, entity_id, sp_key, sp_cert, db_saml = await _resolve_saml_config(
        system_session, app_session, settings, provider_id=provider_id
    )
    idp_entity_id = _saml_idp_entity_id(idp_metadata)
    acs_url = _saml_acs_url(settings, provider_id)
    _validate_saml_response_destination(saml_response, acs_url)
    handler = ModuloSamlAuth(
        entity_id=entity_id,
        acs_url=acs_url,
        idp_metadata_xml=idp_metadata,
        sp_private_key=sp_key,
        sp_x509_cert=sp_cert,
    )
    result = _saml_process_result(handler, saml_response)
    name_id, attrs = _saml_attributes(result)
    email, display_name, sso_subject = _saml_identity(name_id, attrs, idp_entity_id)

    if app_session is None:
        raise RuntimeError("No app session available for SAML JIT provisioning")

    default_org_id = await _scope_app_session_org(app_session, db_saml)
    account, org_id, org_role = await _saml_jit_provision(
        app_session, settings, email, display_name, sso_subject, default_org_id, db_saml
    )
    await _apply_saml_group_mappings(app_session, _saml_groups(attrs), account, org_id, idp_entity_id)
    return await _issue_saml_tokens(account, org_id, org_role, app_session, settings)


def _parse_saml_datetime(value: str) -> datetime:
    """Parse a SAML timestamp, handling both timezone-aware and naive formats.

    SAML 2.0 timestamps SHOULD include timezone (``Z`` suffix or offset),
    but some IdPs omit it. We treat a naive timestamp as UTC.
    """
    normalized = value.replace("Z", "+00:00")
    dt = datetime.fromisoformat(normalized)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


async def _saml_fetch_idp_metadata(settings: Settings) -> str:
    if settings.modulo_saml_idp_metadata_xml:
        return settings.modulo_saml_idp_metadata_xml
    if settings.modulo_saml_idp_metadata_url:
        async with await pinned_async_client(settings.modulo_saml_idp_metadata_url) as client:
            resp = await client.get(settings.modulo_saml_idp_metadata_url, timeout=httpx.Timeout(15.0, connect=5.0))
            resp.raise_for_status()
            return resp.text
    raise ValueError("SAML IdP metadata not configured (set MODULO_SAML_IDP_METADATA_URL or _XML)")


def _saml_parse_idp_metadata(
    xml_str: str,
) -> tuple[str, str]:
    """Parse IdP metadata XML. Returns (sso_url, entity_id)."""
    root = ElementTree.fromstring(xml_str)
    md_ns = "urn:oasis:names:tc:SAML:2.0:metadata"

    entity_id = root.get("entityID", "")

    sso_descriptor = root.find(f"{{{md_ns}}}IDPSSODescriptor")
    if sso_descriptor is None:
        raise ValueError("No IDPSSODescriptor in IdP metadata")

    sso_service = sso_descriptor.find(
        f"{{{md_ns}}}SingleSignOnService[@Binding='urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect']"
    )
    if sso_service is None:
        sso_service = sso_descriptor.find(f"{{{md_ns}}}SingleSignOnService")
    sso_url = sso_service.get("Location", "") if sso_service is not None else ""
    if not sso_url:
        raise ValueError("No SAML SingleSignOnService with Location found in IdP metadata")

    return sso_url, entity_id
