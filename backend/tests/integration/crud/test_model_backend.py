"""Integration tests for ModelBackend CRUD.

RLS is set to test_org; all ORM changes are rolled back after each test.
"""

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.crud.model_backend import (
    create_model_backend,
    delete_model_backend,
    get_model_backend,
    list_model_backends,
    update_model_backend,
)

pytestmark = pytest.mark.integration


def _mb_kwargs(test_org: uuid.UUID, test_user: uuid.UUID, *, suffix: str = "") -> dict:
    return {
        "org_id": test_org,
        "name": f"TestBackend{suffix}",
        "display_name": "Test Backend",
        "provider": "anthropic",
        "model_id": "stub-model",
        "credentials_ciphertext": b"fake-encrypted-key",
        "created_by": test_user,
    }


async def test_create_model_backend(rls_session: AsyncSession, test_org: uuid.UUID, test_user: uuid.UUID) -> None:
    mb = await create_model_backend(rls_session, **_mb_kwargs(test_org, test_user))
    assert mb.id is not None
    assert mb.provider == "anthropic"
    assert mb.organisation_id == test_org


async def test_get_model_backend_returns_existing(
    rls_session: AsyncSession, test_org: uuid.UUID, test_user: uuid.UUID
) -> None:
    mb = await create_model_backend(rls_session, **_mb_kwargs(test_org, test_user, suffix="-fetch"))
    fetched = await get_model_backend(rls_session, mb.id)
    assert fetched is not None
    assert fetched.id == mb.id


async def test_get_model_backend_returns_none_for_unknown(rls_session: AsyncSession) -> None:
    assert await get_model_backend(rls_session, uuid.uuid4()) is None


async def test_list_model_backends_pagination(
    rls_session: AsyncSession, test_org: uuid.UUID, test_user: uuid.UUID
) -> None:
    for i in range(3):
        await create_model_backend(
            rls_session,
            **_mb_kwargs(test_org, test_user, suffix=f"-list-{i}-{uuid.uuid4().hex[:4]}"),
        )
    page1 = await list_model_backends(rls_session, page=1, page_size=2)
    assert page1.total >= 3
    assert len(page1.items) == 2
    assert page1.page == 1


async def test_update_model_backend(rls_session: AsyncSession, test_org: uuid.UUID, test_user: uuid.UUID) -> None:
    mb = await create_model_backend(rls_session, **_mb_kwargs(test_org, test_user, suffix="-upd"))
    updated = await update_model_backend(rls_session, mb.id, {"display_name": "Updated Name"})
    assert updated is not None
    assert updated.display_name == "Updated Name"


async def test_update_model_backend_unknown_returns_none(rls_session: AsyncSession) -> None:
    assert await update_model_backend(rls_session, uuid.uuid4(), {"display_name": "x"}) is None


async def test_delete_model_backend(rls_session: AsyncSession, test_org: uuid.UUID, test_user: uuid.UUID) -> None:
    mb = await create_model_backend(rls_session, **_mb_kwargs(test_org, test_user, suffix="-del"))
    assert await delete_model_backend(rls_session, mb.id) is True
    assert await get_model_backend(rls_session, mb.id) is None


async def test_delete_model_backend_unknown_returns_false(rls_session: AsyncSession) -> None:
    assert await delete_model_backend(rls_session, uuid.uuid4()) is False
