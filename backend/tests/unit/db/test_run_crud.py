"""Unit tests for the per-run API-key TTL reader (FAR-296 Phase 3b).

``get_run_api_key_ttl_seconds`` computes the short-TTL for a script-mode
sandbox's runner-role API key:

    TTL = min(max(settings_floor, node_timeout_seconds + 300), org_max)

where ``settings_floor`` comes from ``RUN_API_KEY_DEFAULT_TTL_SECONDS``
(``run_api_key_default_ttl_seconds``, default 900) and ``org_max`` from the
org's ``settings_json.run_api_key_max_ttl_seconds`` (default 3600). These
tests prove the setting drives the floor (a hardcoded 900 regresses the test)
and that both reads fail open to their defaults.

Mock/fake based — no Postgres.
"""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.db.crud.run import get_run_api_key_ttl_seconds

_ORG_ID = uuid.uuid4()


def _session_factory() -> MagicMock:
    """Async-context-manager session whose org read resolves to a non-dict
    settings_json (so ``_read_org_int_limit`` fails open -> org_max stays at
    the 3600 default)."""
    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=session)
    session.execute = AsyncMock(return_value=MagicMock())
    return MagicMock(return_value=session)


# ---------------------------------------------------------------------------
# get_run_api_key_ttl_seconds — setting-driven floor
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_run_api_key_ttl_uses_settings_floor() -> None:
    """RUN_API_KEY_DEFAULT_TTL_SECONDS drives the floor, not a hardcoded 900.

    With floor=1800 and a small node timeout (30s), the TTL must be at least
    1800. If the function hardcodes 900 the result is 900 and this fails.
    """
    settings = SimpleNamespace(run_api_key_default_ttl_seconds=1800)
    with patch("modulo.settings.get_settings", return_value=settings):
        ttl = await get_run_api_key_ttl_seconds(_session_factory(), _ORG_ID, node_timeout_seconds=30)
    assert ttl == 1800


@pytest.mark.asyncio
async def test_get_run_api_key_ttl_floor_still_allows_node_timeout_to_win() -> None:
    """The setting is a FLOOR — a larger node-timeout term still raises the TTL."""
    settings = SimpleNamespace(run_api_key_default_ttl_seconds=900)
    with patch("modulo.settings.get_settings", return_value=settings):
        ttl = await get_run_api_key_ttl_seconds(_session_factory(), _ORG_ID, node_timeout_seconds=2000)
    assert ttl == 2300  # min(max(900, 2000+300), 3600)


@pytest.mark.asyncio
async def test_get_run_api_key_ttl_falls_back_when_settings_read_fails() -> None:
    """A settings read failure falls back to the default 900 floor (fail-open)."""
    with patch("modulo.settings.get_settings", side_effect=RuntimeError("settings boom")):
        ttl = await get_run_api_key_ttl_seconds(_session_factory(), _ORG_ID, node_timeout_seconds=30)
    assert ttl == 900
