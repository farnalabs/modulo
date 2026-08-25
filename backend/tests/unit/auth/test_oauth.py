"""Unit tests for OAuth 2.0 authorization code flow (modulo.auth.oauth)."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Self
from unittest.mock import AsyncMock, MagicMock

import jwt as pyjwt
import pytest
from jwt import InvalidTokenError as JWTError

from modulo.auth.oauth import (
    InvalidClientError,
    InvalidGrantError,
    InvalidScopeError,
    OAuthAccessTokenClaims,
    UnauthorizedClientError,
    _hash_secret,
    blacklist_oauth_token_family,
    check_oauth_token_family_valid,
    clamp_oauth_role,
    compute_pkce_challenge,
    consume_authorization_code,
    consume_consent_state,
    create_authorization_code,
    create_consent_state,
    create_oauth_access_token,
    create_oauth_client,
    create_oauth_token_family,
    decode_oauth_access_token,
    delete_oauth_client,
    generate_client_credentials,
    get_oauth_client_by_client_id,
    list_oauth_clients,
    normalize_scopes,
    rotate_oauth_token_family,
    scopes_required_role,
    validate_client_scopes,
    validate_client_secret,
    validate_pkce_method,
    verify_pkce,
)

_SECRET_KEY = "abcdefghijklmnopqrstuvwxyz0123456789ab"
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_OTHER_ORG = uuid.UUID("11111111-1111-1111-1111-111111111111")
_ACCOUNT_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_CODE_VERIFIER = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
_CODE_CHALLENGE = compute_pkce_challenge(_CODE_VERIFIER)


def _make_session_mock() -> AsyncMock:
    """Create an AsyncMock session with begin() returning an async context manager."""

    class _AsyncSessionContextManager:
        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(
            self,
            exc_type: object = None,
            exc_val: object = None,
            exc_tb: object = None,
        ) -> bool:
            return False

    cm = _AsyncSessionContextManager()
    session = AsyncMock()
    session.begin = MagicMock(return_value=cm)
    return session


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_session(key_return: object = None) -> AsyncMock:
    session = AsyncMock()
    scalar_result = MagicMock()
    scalar_result.scalar_one_or_none.return_value = key_return
    scalars_result = MagicMock()
    scalars_result.scalars.return_value.all.return_value = []
    scalars_result.scalar_one_or_none.return_value = key_return
    session.execute = AsyncMock(return_value=scalars_result)
    return session


def _make_oauth_client(**overrides: object) -> MagicMock:
    c = MagicMock()
    c.id = uuid.uuid4()
    c.client_id = overrides.get("client_id", "test_client_123")
    c.client_secret_hash = overrides.get("client_secret_hash", _hash_secret("supersecret"))
    c.name = overrides.get("name", "Test Client")
    c.scopes = overrides.get("scopes", "trigger:run hitl:review")
    c.redirect_uris = overrides.get("redirect_uris", "http://localhost/callback")
    c.organisation_id = overrides.get("organisation_id", _ORG_ID)
    c.created_by = overrides.get("created_by")
    c.created_at = overrides.get("created_at", datetime(2025, 1, 1, tzinfo=UTC))
    return c


# ---------------------------------------------------------------------------
# generate_client_credentials
# ---------------------------------------------------------------------------


class TestGenerateClientCredentials:
    def test_returns_three_values(self) -> None:
        cid, secret, hashed = generate_client_credentials()
        assert isinstance(cid, str)
        assert isinstance(secret, str)
        assert isinstance(hashed, str)

    def test_client_id_is_16_hex_chars(self) -> None:
        cid, _, _ = generate_client_credentials()
        assert len(cid) == 16
        int(cid, 16)  # raises if not hex

    def test_secret_is_40_chars_urlsafe(self) -> None:
        _, secret, _ = generate_client_credentials()
        assert len(secret) == 40

    def test_hash_is_sha256_of_secret(self) -> None:
        _, secret, hashed = generate_client_credentials()
        assert hashed == _hash_secret(secret)

    def test_unique_each_call(self) -> None:
        c1, s1, _ = generate_client_credentials()
        c2, s2, _ = generate_client_credentials()
        assert c1 != c2
        assert s1 != s2


# ---------------------------------------------------------------------------
# _hash_secret
# ---------------------------------------------------------------------------


class TestHashSecret:
    def test_hexdigest_length(self) -> None:
        assert len(_hash_secret("anything")) == 64

    def test_deterministic(self) -> None:
        # Golden value pins the exact digest so a change in algorithm or
        # encoding fails loudly instead of silently altering client hashes.
        assert _hash_secret("foo") == "2c26b46b68ffc68ff99b453c1d30413413422d706483bfa0f98a5e886266e7ae"

    def test_different_inputs_differ(self) -> None:
        assert _hash_secret("foo") != _hash_secret("bar")


# ---------------------------------------------------------------------------
# create_oauth_client / get_oauth_client_by_client_id / list / delete
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestOAuthClientCRUD:
    async def test_create_returns_client_and_raw_secret(self) -> None:
        session = AsyncMock()
        session.add = MagicMock()
        session.flush = AsyncMock()

        client, raw_secret = await create_oauth_client(
            session,
            org_id=_ORG_ID,
            name="My App",
            scopes="trigger:run",
            redirect_uris="http://localhost/cb",
        )

        assert client.name == "My App"
        assert client.scopes == "trigger:run"
        assert client.organisation_id == _ORG_ID
        assert len(raw_secret) == 40
        assert session.add.call_args.args[0] is client

    async def test_get_by_client_id_returns_client(self) -> None:
        fake_client = _make_oauth_client(client_id="myid")
        session = _make_session(fake_client)

        result = await get_oauth_client_by_client_id(session, "myid")
        assert result is fake_client

    async def test_get_by_client_id_returns_none(self) -> None:
        session = _make_session(None)
        result = await get_oauth_client_by_client_id(session, "nonexistent")
        assert result is None

    async def test_list_returns_dicts(self) -> None:
        c = _make_oauth_client()
        c.created_at = datetime(2025, 1, 1, tzinfo=UTC)
        mock_scalars = MagicMock()
        mock_scalars.__iter__.return_value = iter([c])
        result_mock = MagicMock()
        result_mock.scalars.return_value = mock_scalars
        session = AsyncMock()
        session.execute = AsyncMock(return_value=result_mock)

        result = await list_oauth_clients(session, _ORG_ID)
        assert len(result) == 1
        assert result[0]["client_id"] == c.client_id
        assert result[0]["name"] == c.name
        assert result[0]["scopes"] == c.scopes.split()

    async def test_list_empty(self) -> None:
        session = _make_session(None)
        result = await list_oauth_clients(session, _ORG_ID)
        assert result == []

    async def test_delete_existing_returns_true(self) -> None:
        fake = _make_oauth_client()
        session = _make_session(fake)
        session.delete = AsyncMock()

        ok = await delete_oauth_client(session, fake.client_id, _ORG_ID)
        assert ok is True
        session.delete.assert_awaited_once_with(fake)

    async def test_delete_missing_returns_false(self) -> None:
        session = _make_session(None)
        ok = await delete_oauth_client(session, "absent", _ORG_ID)
        assert ok is False

    async def test_delete_wrong_org_returns_false(self) -> None:
        session = _make_session(None)

        ok = await delete_oauth_client(session, "any_client", _ORG_ID)
        assert ok is False


# ---------------------------------------------------------------------------
# validate_client_secret
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestValidateClientSecret:
    async def test_valid_credentials(self) -> None:
        secret = "supersecret"
        hashed = _hash_secret(secret)
        fake = _make_oauth_client(client_secret_hash=hashed)
        session = _make_session(fake)

        client = await validate_client_secret(session, fake.client_id, secret)
        assert client is fake

    async def test_unknown_client_raises(self) -> None:
        session = _make_session(None)
        with pytest.raises(InvalidClientError, match="Unknown"):
            await validate_client_secret(session, "bad_id", "secret")

    async def test_wrong_secret_raises(self) -> None:
        fake = _make_oauth_client(client_secret_hash=_hash_secret("correct-horse"))
        session = _make_session(fake)
        with pytest.raises(InvalidClientError, match="mismatch"):
            await validate_client_secret(session, fake.client_id, "wrong-secret")


# ---------------------------------------------------------------------------
# Authorization code lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestAuthorizationCode:
    async def test_create_code_returns_string(self) -> None:
        session = AsyncMock()
        session.add = MagicMock()
        session.flush = AsyncMock()

        code = await create_authorization_code(
            session,
            client_id="cid",
            org_id=_ORG_ID,
            scopes="trigger:run",
            redirect_uri="http://localhost/callback",
            account_id=_ACCOUNT_ID,
            code_challenge=_CODE_CHALLENGE,
        )
        assert isinstance(code, str)
        assert len(code) > 0
        created = session.add.call_args.args[0]
        assert created.account_id == _ACCOUNT_ID
        assert created.code_challenge == _CODE_CHALLENGE
        assert created.code_challenge_method == "S256"

    async def test_create_code_rejects_plain_pkce_method(self) -> None:
        session = AsyncMock()
        with pytest.raises(InvalidGrantError, match="S256"):
            await create_authorization_code(
                session,
                client_id="cid",
                org_id=_ORG_ID,
                scopes="trigger:run",
                redirect_uri="http://localhost/callback",
                account_id=_ACCOUNT_ID,
                code_challenge=_CODE_CHALLENGE,
                code_challenge_method="plain",
            )

    async def test_create_code_rejects_empty_pkce_method(self) -> None:
        session = AsyncMock()
        with pytest.raises(InvalidGrantError, match="S256"):
            await create_authorization_code(
                session,
                client_id="cid",
                org_id=_ORG_ID,
                scopes="trigger:run",
                redirect_uri="http://localhost/callback",
                account_id=_ACCOUNT_ID,
                code_challenge=_CODE_CHALLENGE,
                code_challenge_method="",
            )

    def _make_code_record(
        self,
        client: MagicMock,
        *,
        used: bool = False,
        expired: bool = False,
        challenge: str = _CODE_CHALLENGE,
        method: str = "S256",
    ) -> MagicMock:
        code_record = MagicMock()
        code_record.code = "authcode123"
        code_record.client_id = client.client_id
        code_record.redirect_uri = "http://localhost/callback"
        code_record.used = used
        code_record.code_challenge = challenge
        code_record.code_challenge_method = method
        code_record.expires_at = (
            datetime.now(UTC) - timedelta(minutes=1) if expired else datetime.now(UTC) + timedelta(minutes=10)
        )
        return code_record

    def _session_returning(self, client: MagicMock, code_record: MagicMock) -> AsyncMock:
        call_count = 0

        async def _execute(*args: object, **kwargs: object) -> MagicMock:
            nonlocal call_count
            result = MagicMock()
            result.scalar_one_or_none.return_value = client if call_count == 0 else code_record
            call_count += 1
            return result

        session = _make_session_mock()
        session.execute = AsyncMock(side_effect=_execute)
        session.flush = AsyncMock()
        return session

    async def test_consume_valid_code_with_pkce(self) -> None:
        secret = "validsecret"
        hashed = _hash_secret(secret)
        client = _make_oauth_client(client_secret_hash=hashed)
        code_record = self._make_code_record(client)

        session = self._session_returning(client, code_record)
        result = await consume_authorization_code(
            session,
            code="authcode123",
            client_id=client.client_id,
            redirect_uri="http://localhost/callback",
            client_secret=secret,
            code_verifier=_CODE_VERIFIER,
        )
        assert result is code_record
        assert code_record.used is True

    async def test_consume_missing_verifier_raises(self) -> None:
        secret = "s"
        hashed = _hash_secret(secret)
        client = _make_oauth_client(client_secret_hash=hashed)
        code_record = self._make_code_record(client)

        session = self._session_returning(client, code_record)
        with pytest.raises(InvalidGrantError, match="code_verifier"):
            await consume_authorization_code(
                session,
                code="authcode123",
                client_id=client.client_id,
                redirect_uri="http://localhost/callback",
                client_secret=secret,
                code_verifier=None,
            )
        # PKCE failure must NOT consume the code — a legitimate retry stays possible.
        assert code_record.used is False

    async def test_consume_mismatched_verifier_raises(self) -> None:
        secret = "s"
        hashed = _hash_secret(secret)
        client = _make_oauth_client(client_secret_hash=hashed)
        code_record = self._make_code_record(client)

        session = self._session_returning(client, code_record)
        with pytest.raises(InvalidGrantError, match="PKCE"):
            await consume_authorization_code(
                session,
                code="authcode123",
                client_id=client.client_id,
                redirect_uri="http://localhost/callback",
                client_secret=secret,
                code_verifier="completely-wrong-verifier-value",
            )
        assert code_record.used is False

    async def test_consume_code_without_stored_challenge_raises(self) -> None:
        secret = "s"
        hashed = _hash_secret(secret)
        client = _make_oauth_client(client_secret_hash=hashed)
        code_record = self._make_code_record(client, challenge=None)

        session = self._session_returning(client, code_record)
        with pytest.raises(InvalidGrantError, match="code_challenge"):
            await consume_authorization_code(
                session,
                code="authcode123",
                client_id=client.client_id,
                redirect_uri="http://localhost/callback",
                client_secret=secret,
                code_verifier=_CODE_VERIFIER,
            )

    async def test_consume_expired_code(self) -> None:
        secret = "s"
        hashed = _hash_secret(secret)
        client = _make_oauth_client(client_secret_hash=hashed)
        code_record = self._make_code_record(client, expired=True)

        session = self._session_returning(client, code_record)
        with pytest.raises(InvalidGrantError, match="expired"):
            await consume_authorization_code(
                session,
                code="oldcode",
                client_id=client.client_id,
                redirect_uri="http://localhost/callback",
                client_secret=secret,
                code_verifier=_CODE_VERIFIER,
            )

    async def test_consume_already_used_code(self) -> None:
        secret = "s"
        hashed = _hash_secret(secret)
        client = _make_oauth_client(client_secret_hash=hashed)
        code_record = self._make_code_record(client, used=True)

        session = self._session_returning(client, code_record)
        with pytest.raises(InvalidGrantError, match="already been used"):
            await consume_authorization_code(
                session,
                code="usedcode",
                client_id=client.client_id,
                redirect_uri="http://localhost/callback",
                client_secret=secret,
                code_verifier=_CODE_VERIFIER,
            )

    async def test_consume_wrong_client_raises(self) -> None:
        secret = "s"
        hashed = _hash_secret(secret)
        client = _make_oauth_client(client_secret_hash=hashed)
        code_record = self._make_code_record(client)
        code_record.client_id = "other-client-id"

        session = self._session_returning(client, code_record)
        with pytest.raises(InvalidGrantError, match="different client"):
            await consume_authorization_code(
                session,
                code="other-code",
                client_id=client.client_id,
                redirect_uri="http://localhost/callback",
                client_secret=secret,
                code_verifier=_CODE_VERIFIER,
            )


# ---------------------------------------------------------------------------
# OAuth access token creation & validation
# ---------------------------------------------------------------------------


class TestOAuthAccessToken:
    def test_create_and_decode_roundtrip(self) -> None:
        token = create_oauth_access_token(
            "myclient",
            _SECRET_KEY,
            organisation_id=str(_ORG_ID),
            account_id=str(_ACCOUNT_ID),
            scopes=["trigger:run", "hitl:review"],
            token_family="fam-1",
            token_sequence=0,
        )
        claims = decode_oauth_access_token(token, _SECRET_KEY)
        assert claims.client_id == "myclient"
        assert claims.organisation_id == _ORG_ID
        assert claims.account_id == _ACCOUNT_ID
        assert set(claims.scopes) == {"trigger:run", "hitl:review"}
        assert claims.token_family == "fam-1"
        assert claims.token_sequence == 0

    def test_decode_wrong_key_raises(self) -> None:
        token = create_oauth_access_token(
            "c",
            _SECRET_KEY,
            organisation_id=str(_ORG_ID),
            account_id=str(_ACCOUNT_ID),
            scopes=[],
            token_family="f",
            token_sequence=0,
        )
        with pytest.raises(JWTError):
            decode_oauth_access_token(token, "x" * 32)

    @pytest.mark.parametrize(
        ("claims_overrides", "match"),
        [
            ({"purpose": "access"}, "purpose"),
            ({"sub": None}, "sub"),
            ({"org_id": None}, "org_id"),
            ({"account_id": None}, "account_id"),
            ({"token_family": None}, "token_family"),
            ({"token_sequence": None}, "token_sequence"),
            ({"org_id": "not-a-uuid"}, "org_id"),
        ],
    )
    def test_decode_rejects_missing_or_invalid_claims(self, claims_overrides: dict, match: str) -> None:
        base_claims: dict = {
            "sub": "c",
            "org_id": str(_ORG_ID),
            "account_id": str(_ACCOUNT_ID),
            "scopes": "",
            "purpose": "oauth_access",
            "token_family": "f",
            "token_sequence": 0,
            "iat": datetime.now(UTC),
            "exp": datetime.now(UTC) + timedelta(hours=1),
        }
        for k, v in claims_overrides.items():
            if v is None:
                base_claims.pop(k, None)
            else:
                base_claims[k] = v
        token = pyjwt.encode(base_claims, _SECRET_KEY, algorithm="HS256")
        with pytest.raises(JWTError, match=match):
            decode_oauth_access_token(token, _SECRET_KEY)

    def test_decode_expired_token_raises(self) -> None:
        claims = {
            "sub": "c",
            "org_id": str(_ORG_ID),
            "account_id": str(_ACCOUNT_ID),
            "scopes": "",
            "purpose": "oauth_access",
            "token_family": "f",
            "token_sequence": 0,
            "iat": datetime.now(UTC) - timedelta(hours=2),
            "exp": datetime.now(UTC) - timedelta(hours=1),
        }
        token = pyjwt.encode(claims, _SECRET_KEY, algorithm="HS256")
        with pytest.raises(JWTError):
            decode_oauth_access_token(token, _SECRET_KEY)

    def test_claims_dataclass(self) -> None:
        claims = OAuthAccessTokenClaims(
            client_id="cid",
            organisation_id=_ORG_ID,
            account_id=_ACCOUNT_ID,
            scopes=["a", "b"],
            token_family="tf",
            token_sequence=1,
        )
        assert claims.client_id == "cid"
        assert claims.organisation_id == _ORG_ID
        assert claims.account_id == _ACCOUNT_ID
        assert claims.scopes == ["a", "b"]


# ---------------------------------------------------------------------------
# Token family management
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestTokenFamily:
    async def test_create_family(self) -> None:
        session = AsyncMock()
        session.add = MagicMock()
        session.flush = AsyncMock()

        fam_id, seq = await create_oauth_token_family(session, client_id="cid", org_id=_ORG_ID)
        assert isinstance(fam_id, str)
        assert seq == 0

    async def test_rotate_increments_sequence(self) -> None:
        family = MagicMock()
        family.family_id = uuid.uuid4()
        family.client_id = "cid"
        family.organisation_id = _ORG_ID
        family.is_blacklisted = False
        family.max_sequence = 0

        session = _make_session(family)

        _fam_id, new_seq = await rotate_oauth_token_family(
            session,
            family_id=str(family.family_id),
            current_sequence=0,
            client_id="cid",
            org_id=_ORG_ID,
        )
        assert new_seq == 1
        assert family.max_sequence == 1

    async def test_rotate_out_of_order_blacklists(self) -> None:
        family = MagicMock()
        family.family_id = uuid.uuid4()
        family.client_id = "cid"
        family.organisation_id = _ORG_ID
        family.is_blacklisted = False
        family.max_sequence = 1

        session = _make_session(family)

        with pytest.raises(InvalidGrantError, match="Token family rotated"):
            await rotate_oauth_token_family(
                session,
                family_id=str(family.family_id),
                current_sequence=0,
                client_id="cid",
                org_id=_ORG_ID,
            )
        assert family.is_blacklisted is True

    async def test_rotate_blacklisted_family_raises(self) -> None:
        family = MagicMock()
        family.family_id = uuid.uuid4()
        family.client_id = "cid"
        family.organisation_id = _ORG_ID
        family.is_blacklisted = True
        family.max_sequence = 0

        session = _make_session(family)

        with pytest.raises(InvalidGrantError, match="blacklisted"):
            await rotate_oauth_token_family(
                session,
                family_id=str(family.family_id),
                current_sequence=0,
                client_id="cid",
                org_id=_ORG_ID,
            )

    async def test_rotate_missing_family_raises(self) -> None:
        session = _make_session(None)
        with pytest.raises(InvalidGrantError, match="not found"):
            await rotate_oauth_token_family(
                session,
                family_id=str(uuid.uuid4()),
                current_sequence=0,
                client_id="cid",
                org_id=_ORG_ID,
            )

    async def test_blacklist_family(self) -> None:
        family = MagicMock()
        family.family_id = uuid.uuid4()
        family.client_id = "cid"
        family.organisation_id = _ORG_ID
        family.is_blacklisted = False

        session = _make_session(family)
        session.flush = AsyncMock()

        await blacklist_oauth_token_family(
            session,
            family_id=str(family.family_id),
            client_id="cid",
            org_id=_ORG_ID,
        )
        assert family.is_blacklisted is True

    async def test_check_family_valid(self) -> None:
        family = MagicMock()
        family.family_id = uuid.uuid4()
        family.client_id = "cid"
        family.organisation_id = _ORG_ID
        family.is_blacklisted = False

        session = _make_session(family)
        assert (
            await check_oauth_token_family_valid(
                session,
                family_id=str(family.family_id),
                client_id="cid",
                org_id=_ORG_ID,
            )
            is True
        )

    async def test_check_family_blacklisted(self) -> None:
        session = _make_session(None)
        assert (
            await check_oauth_token_family_valid(
                session,
                family_id=str(uuid.uuid4()),
                client_id="cid",
                org_id=_ORG_ID,
            )
            is False
        )


# ---------------------------------------------------------------------------
# Scope helpers
# ---------------------------------------------------------------------------


class TestNormalizeScopes:
    def test_valid_scopes(self) -> None:
        assert normalize_scopes("trigger:run hitl:review") == ["hitl:review", "trigger:run"]

    def test_empty_string(self) -> None:
        assert not normalize_scopes("")

    def test_whitespace_only(self) -> None:
        assert not normalize_scopes("   ")

    def test_unknown_scope_raises(self) -> None:
        with pytest.raises(InvalidScopeError, match="unknown:scope"):
            normalize_scopes("trigger:run unknown:scope")

    def test_single_scope(self) -> None:
        assert normalize_scopes("library:browse") == ["library:browse"]


class TestValidateClientScopes:
    def test_intersection_returns_sorted(self) -> None:
        client = _make_oauth_client(scopes="trigger:run hitl:review library:browse")
        result = validate_client_scopes(client, ["hitl:review", "trigger:run"])
        assert result == ["hitl:review", "trigger:run"]

    def test_no_overlap_raises(self) -> None:
        client = _make_oauth_client(scopes="library:browse")
        with pytest.raises(UnauthorizedClientError, match="None of the requested"):
            validate_client_scopes(client, ["trigger:run"])

    def test_partial_overlap(self) -> None:
        client = _make_oauth_client(scopes="trigger:run library:browse")
        result = validate_client_scopes(client, ["trigger:run", "hitl:review"])
        assert result == ["trigger:run"]


# ---------------------------------------------------------------------------
# PKCE (RFC 7636)
# ---------------------------------------------------------------------------


class TestPKCE:
    def test_compute_challenge_matches_rfc7636_example(self) -> None:
        # RFC 7636 §A.1/A.4 known test vector.
        assert compute_pkce_challenge(_CODE_VERIFIER) == _CODE_CHALLENGE

    def test_compute_challenge_is_urlsafe_and_unpadded(self) -> None:
        challenge = compute_pkce_challenge("some-verifier-value")
        assert challenge == challenge.rstrip("=")
        assert all(c.isalnum() or c in "-_" for c in challenge)

    def test_verify_pkce_matches(self) -> None:
        assert verify_pkce(_CODE_VERIFIER, _CODE_CHALLENGE, "S256") is None

    def test_verify_pkce_mismatch_raises(self) -> None:
        with pytest.raises(InvalidGrantError, match="PKCE"):
            verify_pkce("wrong-verifier", _CODE_CHALLENGE, "S256")

    def test_verify_pkce_missing_verifier_raises(self) -> None:
        with pytest.raises(InvalidGrantError, match="code_verifier"):
            verify_pkce("", _CODE_CHALLENGE, "S256")

    def test_verify_pkce_plain_method_raises(self) -> None:
        with pytest.raises(InvalidGrantError, match="S256"):
            verify_pkce(_CODE_VERIFIER, _CODE_CHALLENGE, "plain")

    def test_verify_pkce_missing_challenge_raises(self) -> None:
        with pytest.raises(InvalidGrantError, match="code_challenge"):
            verify_pkce(_CODE_VERIFIER, "", "S256")

    @pytest.mark.parametrize("method", ["plain", "", None, "  "])
    def test_validate_pkce_method_rejects_non_s256(self, method: str | None) -> None:
        with pytest.raises(InvalidGrantError, match="S256"):
            validate_pkce_method(method)

    def test_validate_pkce_method_accepts_s256_case_insensitive(self) -> None:
        assert validate_pkce_method("s256") == "S256"


# ---------------------------------------------------------------------------
# Consent-state store
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestConsentState:
    async def test_create_consent_state_persists_row(self) -> None:
        session = AsyncMock()
        session.add = MagicMock()
        session.flush = AsyncMock()

        await create_consent_state(
            session,
            state="state-abc",
            client_id="cid",
            redirect_uri="http://localhost/callback",
            scopes=["trigger:run"],
            code_challenge=_CODE_CHALLENGE,
            org_id=_ORG_ID,
        )
        row = session.add.call_args.args[0]
        assert row.state == "state-abc"
        assert row.client_id == "cid"
        assert row.scopes == ["trigger:run"]
        assert row.code_challenge == _CODE_CHALLENGE
        assert row.account_id is None

    async def test_consume_consent_state_claims_and_returns_row(self) -> None:
        session = _make_session_mock()
        state_row = MagicMock()
        state_row.state = "state-abc"
        state_row.redirect_uri = "http://localhost/callback"
        state_row.client_id = "cid"
        state_row.scopes = ["trigger:run"]
        state_row.code_challenge = _CODE_CHALLENGE
        state_row.organisation_id = _ORG_ID

        result = MagicMock()
        result.scalar_one_or_none.return_value = state_row
        session.execute = AsyncMock(return_value=result)

        consumed = await consume_consent_state(
            session,
            state="state-abc",
            _org_id=_ORG_ID,
            account_id=_ACCOUNT_ID,
        )
        assert consumed is state_row
        # The atomic UPDATE must filter on unconsumed + unexpired.
        statement = session.execute.call_args.args[0]
        assert "consumed" in str(statement)

    async def test_consume_consent_state_missing_returns_none(self) -> None:
        session = _make_session_mock()
        result = MagicMock()
        result.scalar_one_or_none.return_value = None
        session.execute = AsyncMock(return_value=result)

        assert await consume_consent_state(session, state="unknown", _org_id=_ORG_ID, account_id=_ACCOUNT_ID) is None


# ---------------------------------------------------------------------------
# Scope → role helpers
# ---------------------------------------------------------------------------


class TestScopeRoleHelpers:
    def test_hitl_review_requires_operator(self) -> None:
        assert scopes_required_role(["hitl:review"]) == "operator"
        assert scopes_required_role(["trigger:run", "hitl:review"]) == "operator"

    def test_other_scopes_require_runner(self) -> None:
        assert scopes_required_role(["trigger:run"]) == "runner"
        assert scopes_required_role(["library:browse"]) == "runner"
        assert scopes_required_role([]) == "runner"

    def test_clamp_keeps_lower_role(self) -> None:
        # live operator, scope runner → runner (token never exceeds its scopes)
        assert clamp_oauth_role("runner", "operator") == "runner"
        # live viewer, scope operator (demoted) → clamped to viewer
        assert clamp_oauth_role("operator", "viewer") == "viewer"
        # equal → unchanged
        assert clamp_oauth_role("operator", "operator") == "operator"

    def test_clamp_unknown_role_fails_to_live(self) -> None:
        assert clamp_oauth_role("not-a-role", "operator") == "operator"
