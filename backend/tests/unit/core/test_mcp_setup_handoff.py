"""Unit tests for the MCP setup handoff token lifecycle.

Covers: token generation/hashing, setup URL construction, TTL metadata, and
one-time consumption (no raw token ever stored, hash-only lookup, expiry gating).
"""

import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from modulo.core.mcp_setup_handoff import (
    HANDOFF_TTL_MINUTES,
    _generate_token,
    _hash_token,
    consume_handoff,
    create_handoff,
)

_ORG_ID = uuid.uuid4()
_RESOURCE_ID = uuid.uuid4()
_CREATED_BY = uuid.uuid4()


def _make_session() -> AsyncMock:
    session = AsyncMock()
    # session.add is a sync call; an AsyncMock would emit an un-awaited-coroutine
    # RuntimeWarning, which this repo's pytest config escalates to an error.
    session.add = MagicMock()
    return session


def test_generate_token_is_urlsafe_and_high_entropy() -> None:
    token = _generate_token()
    assert len(token) >= 32
    # url-safe base64 alphabet (secrets.token_urlsafe)
    assert token.replace("-", "").replace("_", "").isalnum()


def test_generate_token_is_unique() -> None:
    assert len({_generate_token() for _ in range(100)}) == 100


def test_hash_token_is_sha256_hexdigest() -> None:
    token = "raw-token-value"
    assert _hash_token(token) == hashlib.sha256(token.encode()).hexdigest()
    assert len(_hash_token(token)) == 64


async def test_create_handoff_stores_hash_not_raw_token() -> None:
    session = _make_session()
    settings = MagicMock()
    settings.modulo_public_url = "https://app.example.com/"
    with patch("modulo.core.mcp_setup_handoff.get_settings", return_value=settings):
        result = await create_handoff(
            session, org_id=_ORG_ID, resource_type="model-backend", resource_id=_RESOURCE_ID, created_by=_CREATED_BY
        )

    record = session.add.call_args.args[0]
    raw_token = result["setup_url"].split("token=")[1]
    assert record.organisation_id == _ORG_ID
    assert record.resource_type == "model-backend"
    assert record.resource_id == _RESOURCE_ID
    assert record.created_by == _CREATED_BY
    assert record.token_hash == hashlib.sha256(raw_token.encode()).hexdigest()
    assert raw_token != record.token_hash
    assert record.completed_at is None


async def test_create_handoff_builds_setup_url() -> None:
    session = _make_session()
    settings = MagicMock()
    settings.modulo_public_url = "https://app.example.com"
    with patch("modulo.core.mcp_setup_handoff.get_settings", return_value=settings):
        result = await create_handoff(
            session, org_id=_ORG_ID, resource_type="model-backend", resource_id=_RESOURCE_ID, created_by=_CREATED_BY
        )

    assert result["setup_url"].startswith("https://app.example.com/setup/model-backend/")
    assert str(_RESOURCE_ID) in result["setup_url"]
    # Token must live in the URL FRAGMENT (#token=...), never the query string —
    # query params are sent to the server and leak via access logs / Referer headers.
    assert "token=" in result["setup_url"]
    assert "#token=" in result["setup_url"]
    assert "?token=" not in result["setup_url"]


async def test_create_handoff_strips_trailing_slash_from_public_url() -> None:
    session = _make_session()
    settings = MagicMock()
    settings.modulo_public_url = "https://app.example.com/"
    with patch("modulo.core.mcp_setup_handoff.get_settings", return_value=settings):
        result = await create_handoff(
            session, org_id=_ORG_ID, resource_type="model-backend", resource_id=_RESOURCE_ID, created_by=_CREATED_BY
        )

    assert "//setup/" not in result["setup_url"]
    assert result["setup_url"].startswith("https://app.example.com/setup/")


async def test_create_handoff_ttl_metadata() -> None:
    session = _make_session()
    settings = MagicMock()
    settings.modulo_public_url = "https://app.example.com"
    with patch("modulo.core.mcp_setup_handoff.get_settings", return_value=settings):
        result = await create_handoff(
            session, org_id=_ORG_ID, resource_type="model-backend", resource_id=_RESOURCE_ID, created_by=_CREATED_BY
        )

    assert result["expires_in_minutes"] == HANDOFF_TTL_MINUTES
    expiry = datetime.fromisoformat(result["expires_at"])
    assert expiry > datetime.now(UTC)
    assert expiry <= datetime.now(UTC) + timedelta(minutes=HANDOFF_TTL_MINUTES)


async def test_consume_handoff_returns_record_and_marks_completed() -> None:
    session = _make_session()
    record = MagicMock()
    session.execute = AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: record))

    result = await consume_handoff(session, raw_token="raw-token", resource_type="model-backend", org_id=_ORG_ID)

    assert result is record
    assert record.completed_at is not None


async def test_consume_handoff_returns_none_when_not_found() -> None:
    session = _make_session()
    session.execute = AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: None))

    result = await consume_handoff(session, raw_token="expired-token", resource_type="model-backend", org_id=_ORG_ID)

    assert result is None


async def test_consume_handoff_looks_up_by_hash_not_raw_token() -> None:
    session = _make_session()
    session.execute = AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: None))

    await consume_handoff(session, raw_token="raw-token", resource_type="model-backend", org_id=_ORG_ID)

    stmt = session.execute.await_args.args[0]
    sql = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    assert hashlib.sha256(b"raw-token").hexdigest() in sql
    assert "raw-token" not in sql
    assert "completed_at IS NULL" in sql
    assert "expires_at" in sql
