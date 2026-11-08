"""Direct unit tests for the fallback-chain graph validation (FAR-2329).

The multi-hop cycle guard in ``_validate_fallback_ids`` previously had no
direct coverage: every MCP-level test mocked the helper out. These tests call
it with a session stub so the real DFS, the self-reference branch and the
malformed-legacy-row defence are all exercised.
"""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from modulo.api.routes.model_backends import _validate_fallback_ids

ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


def _session(*, found_ids: list[uuid.UUID], rows: list[SimpleNamespace]) -> AsyncMock:
    """Session stub: the membership query returns ``found_ids``; the full-row
    graph-build query returns ``rows``."""
    found_result = MagicMock()
    found_result.scalars.return_value = iter(found_ids)
    graph_result = MagicMock()
    graph_result.scalars.return_value = iter(rows)

    def _execute(stmt: object, *args: object, **kwargs: object) -> MagicMock:
        # The membership query filters with ``IN (...)``; the graph query on
        # ``select(ModelBackend)`` renders every column and has no IN.
        return found_result if " IN " in str(stmt) else graph_result

    session = AsyncMock()
    session.execute = AsyncMock(side_effect=_execute)
    return session


def _row(backend_id: uuid.UUID, fallbacks: list[object]) -> SimpleNamespace:
    return SimpleNamespace(id=backend_id, fallback_backend_ids=fallbacks)


async def test_clean_chain_is_accepted() -> None:
    a, b = uuid.uuid4(), uuid.uuid4()
    session = _session(found_ids=[b], rows=[_row(a, []), _row(b, [])])

    await _validate_fallback_ids(session, ORG_ID, [b], backend_id=a)


async def test_multi_hop_cycle_is_rejected() -> None:
    a, b = uuid.uuid4(), uuid.uuid4()
    # Updating A to fall back to B, while B already falls back to A.
    session = _session(found_ids=[b], rows=[_row(a, []), _row(b, [str(a)])])

    with pytest.raises(HTTPException) as exc_info:
        await _validate_fallback_ids(session, ORG_ID, [b], backend_id=a)

    assert exc_info.value.status_code == 422
    assert "cannot contain a cycle" in exc_info.value.detail[0]["msg"]


async def test_self_reference_is_rejected() -> None:
    a = uuid.uuid4()
    session = _session(found_ids=[a], rows=[_row(a, [])])

    with pytest.raises(HTTPException) as exc_info:
        await _validate_fallback_ids(session, ORG_ID, [a], backend_id=a)

    assert exc_info.value.status_code == 422
    assert "cannot reference itself" in exc_info.value.detail[0]["msg"]


async def test_malformed_legacy_fallback_value_does_not_500() -> None:
    """A non-UUID value in the legacy JSON column must be skipped, not crash.

    B's stored chain carries a corrupt legacy entry AND a real edge back to A,
    so the cycle must still be detected once the corrupt value is ignored.
    """
    a, b = uuid.uuid4(), uuid.uuid4()
    session = _session(found_ids=[b], rows=[_row(a, []), _row(b, ["not-a-uuid", str(a)])])

    with pytest.raises(HTTPException) as exc_info:
        await _validate_fallback_ids(session, ORG_ID, [b], backend_id=a)

    assert exc_info.value.status_code == 422
    assert "cannot contain a cycle" in exc_info.value.detail[0]["msg"]
