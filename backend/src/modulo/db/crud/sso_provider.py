"""CRUD for SSO provider configuration."""

import asyncio
import json
import logging
import re
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.auth.secret_storage import encrypt_stored_secret
from modulo.core.audit_logger import append_audit_event
from modulo.db.crud.base import apply_updates
from modulo.db.models.sso_provider import SsoProvider
from modulo.settings import get_settings

logger = logging.getLogger(__name__)

_UPDATABLE_SSO_FIELDS = frozenset(
    {
        "client_id",
        "client_secret",
        "discovery_url",
        "metadata_url",
        "metadata_xml",
        "entity_id",
        "scopes",
        "enabled",
        "name",
        "auto_provision",
        "default_role",
    }
)

# Audit-record failure log message (best-effort audit — fail open, log loudly).
_LOG_AUDIT_RECORD_FAILED = "Failed to record audit event for SSO provider %s"


def _slugify_provider_id(name: str) -> str:
    """Derive a URL-safe provider_id slug from a human name/slug."""
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", name).strip("-").lower() or "sso"
    return slug[:58]


async def _unique_provider_id(session: AsyncSession, base: str, org_id: uuid.UUID) -> str:
    """Compute a globally-free provider_id slug.

    Scans every ``provider_id`` visible to ``session`` and returns ``base``,
    ``base-2``, ... — the first value not present. When ``session`` runs as the
    ``modulo_system`` role (BYPASSRLS) the scan is instance-global (all orgs),
    matching the GLOBAL partial unique index (migration 0151, FAR-464 option a);
    when it is the app session (RLS-scoped) the scan is limited to the org(s)
    the session can see. ``org_id`` is retained in the signature for call-site
    clarity; the scan itself is RLS-aware so it is correct either way.
    """
    existing = {
        pid for pid in (await session.execute(select(SsoProvider.provider_id))).scalars().all() if pid is not None
    }
    candidate = base
    n = 2
    while candidate in existing:
        candidate = f"{base}-{n}"
        n += 1
    return candidate


async def list_providers(session: AsyncSession, *, org_id: uuid.UUID) -> list[SsoProvider]:
    result = await session.execute(
        select(SsoProvider).where(SsoProvider.organisation_id == org_id).order_by(SsoProvider.created_at)
    )
    return list(result.scalars().all())


async def get_provider(
    session: AsyncSession, provider_id: uuid.UUID, *, org_id: uuid.UUID | None = None
) -> SsoProvider | None:
    conditions = [SsoProvider.id == provider_id]
    if org_id is not None:
        conditions.append(SsoProvider.organisation_id == org_id)
    result = await session.execute(select(SsoProvider).where(*conditions))
    return result.scalar_one_or_none()


async def get_provider_by_provider_id(session: AsyncSession, provider_id: str) -> SsoProvider | None:
    """Resolve a provider by its URL slug (global lookup — no org filter).

    Pre-auth SSO routes have no user/org context, so providers are resolved
    globally through the ``modulo_system`` role (BYPASSRLS). ``provider_id`` is
    GLOBALLY unique (migration 0151, FAR-464 option a), so the system-session
    slug resolution is deterministic. ``.limit(1)`` is a defensive guard: if
    data somehow contains duplicate slugs (e.g. a pre-0151 fixture or a manual
    insert), ``scalar_one_or_none`` would otherwise raise ``MultipleResultsFound``
    and 500 the OIDC login/callback — this coerces it to the first row instead.
    """
    result = await session.execute(select(SsoProvider).where(SsoProvider.provider_id == provider_id).limit(1))
    return result.scalar_one_or_none()


async def get_enabled_saml_provider(session: AsyncSession) -> SsoProvider | None:
    """Return the first enabled SAML provider globally (single-IdP-per-instance).

    Pre-auth SAML is inherently a SINGLE-provider flow: ``/saml/login`` has no
    provider selector, so it resolves to the first enabled SAML provider
    instance-wide and its users JIT into that provider's org. Duplicate SAML
    providers across orgs are NOT supported — a later ticket should add per-org
    SAML selection. ``.limit(1)`` with ``order_by(created_at)`` keeps this
    deterministic (first-enabled-wins).
    """
    result = await session.execute(
        select(SsoProvider)
        .where(SsoProvider.provider_type == "saml", SsoProvider.enabled)
        .order_by(SsoProvider.created_at)
        .limit(1)
    )
    return result.scalar_one_or_none()


async def list_enabled_oidc_providers(session: AsyncSession) -> list[SsoProvider]:
    """List enabled OIDC providers (global lookup — no org filter)."""
    result = await session.execute(select(SsoProvider).where(SsoProvider.provider_type == "oidc", SsoProvider.enabled))
    return list(result.scalars().all())


async def create_provider(
    session: AsyncSession,
    *,
    provider_type: str,
    name: str,
    client_id: str | None = None,
    client_secret: str | None = None,
    discovery_url: str | None = None,
    metadata_url: str | None = None,
    metadata_xml: str | None = None,
    entity_id: str | None = None,
    scopes: list[str] | None = None,
    enabled: bool = True,
    auto_provision: bool = True,
    default_role: str = "runner",
    fernet_key: str,
    org_id: uuid.UUID,
    actor_user_id: uuid.UUID | None = None,
    provider_id: str | None = None,
    system_session: AsyncSession | None = None,
) -> SsoProvider:
    result = await session.execute(
        select(SsoProvider).where(SsoProvider.name == name, SsoProvider.organisation_id == org_id).with_for_update()
    )
    existing = result.scalar_one_or_none()
    if existing:
        msg = f"An SSO provider with name '{name}' already exists in this organisation"
        raise ValueError(msg)

    pid = _slugify_provider_id(provider_id) if provider_id else _slugify_provider_id(name)

    # provider_id is GLOBALLY unique (migration 0151, FAR-464 option a) so a
    # cross-org create must not collide. The caller hands us a modulo_system
    # (BYPASSRLS) session so ALL orgs' existing slugs are visible; we only use
    # it when the system role is actually provisioned (MODULO_SYSTEM_DATABASE_URL
    # set) — otherwise a zero-row fallback read would silently break the
    # intra-org dedupe, so we fall back to the app session (RLS-scoped).
    scan_session = session
    if system_session is not None:
        try:
            if get_settings().modulo_system_database_url:
                scan_session = system_session
        except Exception:
            logger.warning("sso_provider.settings_unavailable", exc_info=True)

    if scan_session is system_session:
        # The modulo_system session factory is built with autobegin=False, so the
        # slug scan needs an explicit transaction — the read paths
        # (_read_system_oidc_provider, _get_enabled_saml_global, ...) all wrap
        # their system reads in `async with system_session.begin():`. Without it,
        # session.execute() raises InvalidRequestError and the admin create
        # endpoint 503s whenever MODULO_SYSTEM_DATABASE_URL is set. The app
        # session already has a transaction begun by the caller, so it is used
        # directly.
        async with scan_session.begin():
            pid = await _unique_provider_id(scan_session, pid, org_id)
    else:
        pid = await _unique_provider_id(scan_session, pid, org_id)

    provider = SsoProvider(
        provider_type=provider_type,
        name=name,
        provider_id=pid,
        client_id=client_id,
        client_secret=encrypt_stored_secret(client_secret, fernet_key) if client_secret is not None else None,
        discovery_url=discovery_url,
        metadata_url=metadata_url,
        metadata_xml=metadata_xml,
        entity_id=entity_id,
        scopes=json.dumps(scopes) if scopes else None,
        enabled=enabled,
        auto_provision=auto_provision,
        default_role=default_role,
        organisation_id=org_id,
    )
    session.add(provider)
    await session.flush()

    try:
        await append_audit_event(
            session,
            org_id=org_id,
            event_type="sso_provider.created",
            actor_user_id=actor_user_id,
            resource_type="sso_provider",
            resource_id=provider.id,
            payload_json={"provider_name": name},
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception(_LOG_AUDIT_RECORD_FAILED, name)

    return provider


async def update_provider(
    session: AsyncSession,
    provider_id: uuid.UUID,
    *,
    org_id: uuid.UUID,
    actor_user_id: uuid.UUID | None = None,
    fernet_key: str,
    **updates: str | bool | list[str] | None,
) -> SsoProvider | None:
    provider = await get_provider(session, provider_id, org_id=org_id)
    if provider is None:
        return None

    if "scopes" in updates and updates["scopes"] is not None and not isinstance(updates["scopes"], str):
        updates["scopes"] = json.dumps(updates["scopes"])

    encrypted_client_secret: bytes | None = None
    if "client_secret" in updates and updates["client_secret"] is not None:
        client_secret = updates["client_secret"]
        if not isinstance(client_secret, str):
            raise TypeError("client_secret must be text")
        encrypted_client_secret = encrypt_stored_secret(client_secret, fernet_key)
        del updates["client_secret"]

    if "name" in updates and updates["name"] is not None:
        result = await session.execute(
            select(SsoProvider)
            .where(
                SsoProvider.name == updates["name"],
                SsoProvider.organisation_id == provider.organisation_id,
                SsoProvider.id != provider_id,
            )
            .with_for_update()
            .limit(1)
        )
        if result.scalar_one_or_none() is not None:
            msg = f"An SSO provider with name '{updates['name']}' already exists in this organisation"
            raise ValueError(msg)

    if encrypted_client_secret is not None:
        provider.client_secret = encrypted_client_secret

    filtered = {k: v for k, v in updates.items() if k in _UPDATABLE_SSO_FIELDS}
    apply_updates(provider, filtered)

    await session.flush()

    try:
        await append_audit_event(
            session,
            org_id=provider.organisation_id,
            event_type="sso_provider.updated",
            actor_user_id=actor_user_id,
            resource_type="sso_provider",
            resource_id=provider.id,
            payload_json={"provider_name": provider.name},
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception(_LOG_AUDIT_RECORD_FAILED, provider.name)

    return provider


async def delete_provider(
    session: AsyncSession,
    provider_id: uuid.UUID,
    *,
    org_id: uuid.UUID,
    actor_user_id: uuid.UUID | None = None,
) -> bool:
    provider = await get_provider(session, provider_id, org_id=org_id)
    if provider is None:
        return False

    provider_name = provider.name
    provider_org_id = provider.organisation_id
    provider_id_val = provider.id

    await session.delete(provider)
    await session.flush()

    try:
        await append_audit_event(
            session,
            org_id=provider_org_id,
            event_type="sso_provider.deleted",
            actor_user_id=actor_user_id,
            resource_type="sso_provider",
            resource_id=provider_id_val,
            payload_json={"provider_name": provider_name},
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception(_LOG_AUDIT_RECORD_FAILED, provider_name)

    return True


async def toggle_provider(
    session: AsyncSession,
    provider_id: uuid.UUID,
    *,
    org_id: uuid.UUID,
    actor_user_id: uuid.UUID | None = None,
) -> SsoProvider | None:
    provider = await get_provider(session, provider_id, org_id=org_id)
    if provider is None:
        return None
    provider.enabled = not provider.enabled
    await session.flush()

    try:
        await append_audit_event(
            session,
            org_id=provider.organisation_id,
            event_type="sso_provider.toggled",
            actor_user_id=actor_user_id,
            resource_type="sso_provider",
            resource_id=provider.id,
            payload_json={"provider_name": provider.name},
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception(_LOG_AUDIT_RECORD_FAILED, provider.name)

    return provider


async def set_group_mappings(
    session: AsyncSession,
    provider_id: uuid.UUID,
    mappings: list[dict[str, object]],
    *,
    org_id: uuid.UUID,
) -> SsoProvider | None:
    provider = await get_provider(session, provider_id, org_id=org_id)
    if provider is None:
        return None
    provider.group_mappings = mappings
    await session.flush()
    return provider
