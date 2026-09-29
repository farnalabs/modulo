"""Pipeline archive/unarchive round-trip against a real Postgres row.

Regression guard for the staging E2E 422: ``POST /pipelines/{id}/archive`` and
``/unarchive`` returned 422 ``Data validation failed.`` because the UPDATE flush
expires the server-computed ``updated_at`` (``onupdate=func.current_timestamp()``).
After the endpoint's transaction commits, ``_pipeline_response`` ->
``PipelineResponse.model_validate`` performs attribute extraction on the ORM row,
which lazy-loads the expired column outside the async greenlet; Pydantic wraps
that failure as a ``ValidationError``, which ``handle_db_errors`` maps to 422.
Both endpoints now refresh the flushed row inside the transaction, mirroring
``update_pipeline_endpoint``.

The unit test in ``tests/unit/api/test_pipelines_routes_coverage.py`` mocks the
session and asserts ``session.refresh`` is awaited; it cannot reproduce the
expired-attribute path. This test drives the real endpoint against a real DB row
so the round-trip (200 + ``archived_at`` flip + readable ``updated_at``) is
exercised end-to-end.
"""

from __future__ import annotations

import os
import uuid

import pytest
from httpx import AsyncClient

from modulo.auth.jwt import create_access_token

os.environ.setdefault("MODULO_AUTH_RATE_LIMIT_ENABLED", "false")
os.environ.setdefault("REDIS_URL", "")

pytestmark = pytest.mark.integration

_VALID_32 = "a" * 32


def _auth_headers(org_id: uuid.UUID, user_id: uuid.UUID) -> dict[str, str]:
    token = create_access_token(
        subject=f"user-{user_id.hex[:8]}",
        secret_key=_VALID_32,
        organisation_id=str(org_id),
        account_id=str(user_id),
        org_role="admin",
        client_kind="browser",
    )
    return {"Authorization": f"Bearer {token}"}


async def test_archive_unarchive_roundtrip_on_real_row(
    integration_client: AsyncClient,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    """Archive then unarchive a real row: both must return 200, not 422.

    Without the in-transaction ``session.refresh`` the archive/unarchive
    responses fail on the expired ``updated_at``; with it both return 200 and
    the response carries the DB-computed timestamp.
    """
    headers = _auth_headers(test_org, test_user)
    create = await integration_client.post(
        "/api/v1/pipelines",
        headers=headers,
        json={"name": f"archive-roundtrip-{uuid.uuid4().hex[:8]}"},
    )
    assert create.status_code == 201, create.text
    pipeline_id = create.json()["id"]

    archived = await integration_client.post(
        f"/api/v1/pipelines/{pipeline_id}/archive",
        headers=headers,
    )
    assert archived.status_code == 200, archived.text
    archived_body = archived.json()
    assert archived_body["archived_at"] is not None
    # The exact attribute whose lazy-load produced the 422 regression.
    assert archived_body["updated_at"]

    restored = await integration_client.post(
        f"/api/v1/pipelines/{pipeline_id}/unarchive",
        headers=headers,
    )
    assert restored.status_code == 200, restored.text
    restored_body = restored.json()
    assert restored_body["archived_at"] is None
    assert restored_body["updated_at"]
