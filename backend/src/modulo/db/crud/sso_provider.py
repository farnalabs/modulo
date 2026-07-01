import json
import uuid

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.core.audit_logger import append_audit_event
from modulo.db.models.sso_provider import SsoProvider


async def list_providers(session: AsyncSession) -> list[SsoProvider]:
    result = await session.execute(select(SsoProvider).order_by(SsoProvider.created_at))
    return list(result.scalars().all())


async def get_provider(session: AsyncSession, provider_id: uuid.UUID) -> SsoProvider | None:
    result = await session.execute(select(SsoProvider).where(SsoProvider.id == provider_id))
    return result.scalar_one_or_none()


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
    org_id: uuid.UUID,
    actor_user_id: uuid.UUID | None = None,
) -> SsoProvider:
    result = await session.execute(select(exists().where(SsoProvider.name == name)))
    if result.scalar():
        raise ValueError(f"An SSO provider with name '{name}' already exists")

    provider = SsoProvider(
        provider_type=provider_type,
        name=name,
        client_id=client_id,
        client_secret=client_secret,
        discovery_url=discovery_url,
        metadata_url=metadata_url,
        metadata_xml=metadata_xml,
        entity_id=entity_id,
        scopes=json.dumps(scopes) if scopes else None,
        enabled=enabled,
        auto_provision=auto_provision,
        default_role=default_role,
    )
    session.add(provider)
    await session.flush()

    await append_audit_event(
        session,
        org_id=org_id,
        event_type="sso_provider.created",
        actor_user_id=actor_user_id,
        resource_type="sso_provider",
        resource_id=provider.id,
        payload_json={"provider_name": name},
    )

    return provider


async def update_provider(
    session: AsyncSession,
    provider_id: uuid.UUID,
    *,
    actor_user_id: uuid.UUID | None = None,
    **updates: str | bool | list[str] | None,
) -> SsoProvider | None:
    provider = await get_provider(session, provider_id)
    if provider is None:
        return None

    if "scopes" in updates and updates["scopes"] is not None:
        updates["scopes"] = json.dumps(updates["scopes"])

    for key, value in updates.items():
        if value is not None and hasattr(provider, key):
            setattr(provider, key, value)
        elif key in updates and updates[key] is None:
            setattr(provider, key, None)

    await session.flush()

    await append_audit_event(
        session,
        org_id=provider.organisation_id,
        event_type="sso_provider.updated",
        actor_user_id=actor_user_id,
        resource_type="sso_provider",
        resource_id=provider.id,
        payload_json={"provider_name": provider.name},
    )

    return provider


async def delete_provider(
    session: AsyncSession,
    provider_id: uuid.UUID,
    *,
    actor_user_id: uuid.UUID | None = None,
) -> bool:
    provider = await get_provider(session, provider_id)
    if provider is None:
        return False

    provider_name = provider.name
    provider_org_id = provider.organisation_id
    provider_id_val = provider.id

    await session.delete(provider)
    await session.flush()

    await append_audit_event(
        session,
        org_id=provider_org_id,
        event_type="sso_provider.deleted",
        actor_user_id=actor_user_id,
        resource_type="sso_provider",
        resource_id=provider_id_val,
        payload_json={"provider_name": provider_name},
    )

    return True


async def toggle_provider(
    session: AsyncSession,
    provider_id: uuid.UUID,
    *,
    actor_user_id: uuid.UUID | None = None,
) -> SsoProvider | None:
    provider = await get_provider(session, provider_id)
    if provider is None:
        return None
    provider.enabled = not provider.enabled
    await session.flush()

    await append_audit_event(
        session,
        org_id=provider.organisation_id,
        event_type="sso_provider.toggled",
        actor_user_id=actor_user_id,
        resource_type="sso_provider",
        resource_id=provider.id,
        payload_json={"provider_name": provider.name},
    )

    return provider


async def set_group_mappings(
    session: AsyncSession,
    provider_id: uuid.UUID,
    mappings: list[dict[str, object]],
) -> SsoProvider | None:
    provider = await get_provider(session, provider_id)
    if provider is None:
        return None
    provider.group_mappings = mappings
    await session.flush()
    return provider
