import json
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

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
) -> SsoProvider:
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
    return provider


async def update_provider(
    session: AsyncSession,
    provider_id: uuid.UUID,
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
    return provider


async def delete_provider(session: AsyncSession, provider_id: uuid.UUID) -> bool:
    provider = await get_provider(session, provider_id)
    if provider is None:
        return False
    await session.delete(provider)
    await session.flush()
    return True


async def toggle_provider(session: AsyncSession, provider_id: uuid.UUID) -> SsoProvider | None:
    provider = await get_provider(session, provider_id)
    if provider is None:
        return None
    provider.enabled = not provider.enabled
    await session.flush()
    return provider
