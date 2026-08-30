"""Unit tests for OAuth refresh-token JWT lifecycle (modulo.auth.oauth).

Covers ``create_oauth_refresh_token`` / ``decode_oauth_refresh_token`` and the
ADR 017 live-role re-check (``verify_live_role_covers_scopes``) that guards the
MCP refresh endpoint (``api/mcp_server.py``). These pure functions previously
had no direct unit coverage — they were only exercised indirectly through mocks
in the MCP OAuth BDD tests.

The decode path is security-sensitive: every malformed/missing claim must be
rejected fail-closed exactly as documented, mirroring the access-token tests in
``test_oauth.py``.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import jwt as pyjwt
import pytest
from jwt import InvalidTokenError as JWTError

import modulo.auth.dependencies as auth_dependencies
from modulo.auth.oauth import (
    InvalidGrantError,
    create_oauth_refresh_token,
    decode_oauth_refresh_token,
    verify_live_role_covers_scopes,
)

_SECRET_KEY = "abcdefghijklmnopqrstuvwxyz0123456789ab"
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_ACCOUNT_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")


# ---------------------------------------------------------------------------
# create_oauth_refresh_token / decode_oauth_refresh_token
# ---------------------------------------------------------------------------


class TestCreateAndDecodeRefreshToken:
    def test_roundtrip_returns_all_claims(self) -> None:
        token = create_oauth_refresh_token(
            "myclient",
            _SECRET_KEY,
            organisation_id=str(_ORG_ID),
            account_id=str(_ACCOUNT_ID),
            scopes=["trigger:run", "hitl:review"],
            token_family="fam-1",
            token_sequence=3,
        )
        claims = decode_oauth_refresh_token(token, _SECRET_KEY)

        assert claims.client_id == "myclient"
        assert claims.organisation_id == _ORG_ID
        assert claims.account_id == _ACCOUNT_ID
        assert set(claims.scopes) == {"trigger:run", "hitl:review"}
        assert claims.token_family == "fam-1"
        assert claims.token_sequence == 3

    def test_roundtrip_empty_scopes_parses_to_empty_list(self) -> None:
        token = create_oauth_refresh_token(
            "myclient",
            _SECRET_KEY,
            organisation_id=str(_ORG_ID),
            account_id=str(_ACCOUNT_ID),
            scopes=[],
            token_family="fam-1",
            token_sequence=0,
        )
        claims = decode_oauth_refresh_token(token, _SECRET_KEY)
        assert not claims.scopes

    def test_default_ttl_is_30_days(self) -> None:
        before = datetime.now(UTC)
        token = create_oauth_refresh_token(
            "myclient",
            _SECRET_KEY,
            organisation_id=str(_ORG_ID),
            account_id=str(_ACCOUNT_ID),
            scopes=[],
            token_family="fam-1",
            token_sequence=0,
        )
        after = datetime.now(UTC) + timedelta(days=30)
        payload = pyjwt.decode(token, _SECRET_KEY, algorithms=["HS256"])
        assert payload["exp"] > before.timestamp()
        assert payload["exp"] <= after.timestamp() + 1

    def test_decode_rejects_expired_token(self) -> None:
        token = create_oauth_refresh_token(
            "myclient",
            _SECRET_KEY,
            organisation_id=str(_ORG_ID),
            account_id=str(_ACCOUNT_ID),
            scopes=[],
            token_family="fam-1",
            token_sequence=0,
            expires_delta=timedelta(seconds=-1),
        )
        with pytest.raises(JWTError):
            decode_oauth_refresh_token(token, _SECRET_KEY)

    def test_decode_wrong_key_raises(self) -> None:
        token = create_oauth_refresh_token(
            "myclient",
            _SECRET_KEY,
            organisation_id=str(_ORG_ID),
            account_id=str(_ACCOUNT_ID),
            scopes=[],
            token_family="fam-1",
            token_sequence=0,
        )
        with pytest.raises(JWTError):
            decode_oauth_refresh_token(token, "x" * 32)

    def test_decode_rejects_access_token(self) -> None:
        # A token minted for the access purpose must not be accepted as a
        # refresh token (the purposes are separate claim spaces).
        access = pyjwt.encode(
            {
                "purpose": "oauth_access",
                "sub": "c",
                "org_id": str(_ORG_ID),
                "account_id": str(_ACCOUNT_ID),
                "scopes": "",
                "token_family": "f",
                "token_sequence": 0,
                "exp": datetime.now(UTC) + timedelta(hours=1),
            },
            _SECRET_KEY,
            algorithm="HS256",
        )
        with pytest.raises(JWTError, match="purpose"):
            decode_oauth_refresh_token(access, _SECRET_KEY)

    @pytest.mark.parametrize(
        ("claims_overrides", "match"),
        [
            ({"purpose": "not_refresh"}, "purpose"),
            ({"sub": None}, "sub"),
            ({"sub": 42}, "(?i)sub"),
            ({"sub": ""}, "sub"),
            ({"org_id": None}, "org_id"),
            ({"org_id": "not-a-uuid"}, "org_id"),
            ({"account_id": None}, "account_id"),
            ({"account_id": ""}, "account_id"),
            ({"token_family": None}, "token_family"),
            ({"token_family": ""}, "token_family"),
            ({"token_sequence": None}, "token_sequence"),
            ({"token_sequence": "high"}, "token_sequence"),
        ],
        ids=[
            "purpose_not_refresh",
            "sub_missing",
            "sub_not_string",
            "sub_empty",
            "org_id_missing",
            "org_id_not_uuid",
            "account_id_missing",
            "account_id_empty",
            "token_family_missing",
            "token_family_empty",
            "token_sequence_missing",
            "token_sequence_not_int",
        ],
    )
    def test_decode_rejects_missing_or_malformed_claims(self, claims_overrides: dict[str, Any], match: str) -> None:
        base_claims: dict[str, Any] = {
            "purpose": "oauth_refresh",
            "sub": "c",
            "org_id": str(_ORG_ID),
            "account_id": str(_ACCOUNT_ID),
            "scopes": "trigger:run",
            "token_family": "f",
            "token_sequence": 0,
            "iat": datetime.now(UTC),
            "exp": datetime.now(UTC) + timedelta(days=1),
        }
        for k, v in claims_overrides.items():
            if v is None:
                base_claims.pop(k, None)
            else:
                base_claims[k] = v
        token = pyjwt.encode(base_claims, _SECRET_KEY, algorithm="HS256")
        with pytest.raises(JWTError, match=match):
            decode_oauth_refresh_token(token, _SECRET_KEY)

    def test_decode_accepts_missing_scopes_as_empty(self) -> None:
        base_claims: dict[str, Any] = {
            "purpose": "oauth_refresh",
            "sub": "c",
            "org_id": str(_ORG_ID),
            "account_id": str(_ACCOUNT_ID),
            "token_family": "f",
            "token_sequence": 0,
            "exp": datetime.now(UTC) + timedelta(days=1),
        }
        token = pyjwt.encode(base_claims, _SECRET_KEY, algorithm="HS256")
        assert not decode_oauth_refresh_token(token, _SECRET_KEY).scopes


# ---------------------------------------------------------------------------
# verify_live_role_covers_scopes (ADR 017 live-role re-check)
# ---------------------------------------------------------------------------


class TestVerifyLiveRoleCoversScopes:
    async def test_active_membership_covers_scopes_returns_live_role(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def _resolve(session: object, account_id: str, org_id: str) -> str:
            return "operator"

        monkeypatch.setattr(auth_dependencies, "resolve_role_from_membership", _resolve)
        session = AsyncMock()

        live_role = await verify_live_role_covers_scopes(
            session,
            account_id=_ACCOUNT_ID,
            org_id=_ORG_ID,
            scopes=["hitl:review", "trigger:run"],
        )
        assert live_role == "operator"

    async def test_no_membership_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def _resolve(session: object, account_id: str, org_id: str) -> None:
            return None

        monkeypatch.setattr(auth_dependencies, "resolve_role_from_membership", _resolve)
        session = AsyncMock()

        with pytest.raises(InvalidGrantError, match="no active membership"):
            await verify_live_role_covers_scopes(
                session,
                account_id=_ACCOUNT_ID,
                org_id=_ORG_ID,
                scopes=["trigger:run"],
            )

    async def test_live_role_below_scope_role_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # hitl:review requires operator (ADR 017); a viewer membership must deny.
        async def _resolve(session: object, account_id: str, org_id: str) -> str:
            return "viewer"

        monkeypatch.setattr(auth_dependencies, "resolve_role_from_membership", _resolve)
        session = AsyncMock()

        with pytest.raises(InvalidGrantError, match="does not cover"):
            await verify_live_role_covers_scopes(
                session,
                account_id=_ACCOUNT_ID,
                org_id=_ORG_ID,
                scopes=["hitl:review"],
            )
