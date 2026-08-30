"""Unit tests for OIDC ID token verification (modulo.auth.oidc_verify)."""

import base64
import json
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import jwt as pyjwt
import pytest
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from modulo.auth.oidc_verify import (
    OidcVerifyError,
    clear_jwks_cache,
    verify_id_token,
    verify_id_token_with_discovery,
)

_DISCOVERY_URL = "https://example.com/.well-known/openid-configuration"
_JWKS_URI = "https://example.com/.well-known/jwks"
_CLIENT_ID = "test-client-id"
_ISSUER = "https://example.com"
_TOKEN_ENDPOINT = "https://example.com/token"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _gen_rsa_keypair() -> tuple[rsa.RSAPrivateKey, rsa.RSAPublicKey]:
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
        backend=default_backend(),
    )
    return private_key, private_key.public_key()


def _create_id_token(
    private_key: rsa.RSAPrivateKey,
    *,
    sub: str = "user123",
    issuer: str = _ISSUER,
    audience: str = _CLIENT_ID,
    exp_offset: timedelta | None = None,
    kid: str = "test-key-1",
) -> str:
    now = datetime.now(UTC)
    claims = {
        "sub": sub,
        "iss": issuer,
        "aud": audience,
        "email": f"{sub}@example.com",
        "iat": now,
        "exp": now + (exp_offset if exp_offset is not None else timedelta(hours=1)),
    }
    pem_key = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return str(pyjwt.encode(claims, pem_key, algorithm="RS256", headers={"kid": kid}))


_RSA_KEYPAIR: tuple[rsa.RSAPrivateKey, rsa.RSAPublicKey] = _gen_rsa_keypair()
_ID_TOKEN: str = _create_id_token(_RSA_KEYPAIR[0])


def _pubkey_to_jwk(public_key: rsa.RSAPublicKey, kid: str = "test-key-1") -> dict:
    pub_numbers = public_key.public_numbers()

    def _int_to_base64url(num: int) -> str:
        byte_len = (num.bit_length() + 7) // 8
        num_bytes = num.to_bytes(byte_len, byteorder="big")
        return base64.urlsafe_b64encode(num_bytes).rstrip(b"=").decode()

    return {
        "kty": "RSA",
        "n": _int_to_base64url(pub_numbers.n),
        "e": _int_to_base64url(pub_numbers.e),
        "alg": "RS256",
        "kid": kid,
        "use": "sig",
    }


def _pubkey_to_ec_jwk(public_key: ec.EllipticCurvePublicKey, kid: str = "test-ec-key-1") -> dict:
    numbers = public_key.public_numbers()

    def _coordinate_to_base64url(value: int) -> str:
        return base64.urlsafe_b64encode(value.to_bytes(32, byteorder="big")).rstrip(b"=").decode()

    return {
        "kty": "EC",
        "crv": "P-256",
        "x": _coordinate_to_base64url(numbers.x),
        "y": _coordinate_to_base64url(numbers.y),
        "alg": "ES256",
        "kid": kid,
        "use": "sig",
    }


def _create_ec_id_token(private_key: ec.EllipticCurvePrivateKey, kid: str = "test-ec-key-1") -> str:
    now = datetime.now(UTC)
    claims = {
        "sub": "ec-user",
        "iss": _ISSUER,
        "aud": _CLIENT_ID,
        "iat": now,
        "exp": now + timedelta(hours=1),
    }
    return str(pyjwt.encode(claims, private_key, algorithm="ES256", headers={"kid": kid}))


def _discovery_doc(*, jwks_uri: str = _JWKS_URI, issuer: str = _ISSUER) -> dict:
    return {
        "issuer": issuer,
        "authorization_endpoint": f"{issuer}/auth",
        "token_endpoint": _TOKEN_ENDPOINT,
        "jwks_uri": jwks_uri,
        "userinfo_endpoint": f"{issuer}/userinfo",
        "scopes_supported": ["openid", "profile", "email"],
    }


def _make_resp(status: int = 200, json_data: object = None) -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status
    resp.json.return_value = json_data or {}
    if status < 400:
        resp.raise_for_status.return_value = None
    else:
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "Error",
            request=MagicMock(),
            response=resp,
        )
    return resp


def _make_httpx_mock(get_map: dict[str, MagicMock], post_map: dict[str, MagicMock] | None = None) -> MagicMock:
    """Create an AsyncMock httpx client with configured get/post responses."""
    mock_client = AsyncMock()

    async def _get(url: str, **kwargs: object) -> MagicMock:
        if url in get_map:
            return get_map[url]
        return _make_resp(404)

    async def _post(url: str, **kwargs: object) -> MagicMock:
        if post_map and url in post_map:
            return post_map[url]
        return _make_resp(404)

    mock_client.get = _get
    mock_client.post = _post
    return mock_client


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_cache() -> None:
    clear_jwks_cache()


@pytest.fixture
def keypair() -> tuple[rsa.RSAPrivateKey, rsa.RSAPublicKey]:
    return _RSA_KEYPAIR


@pytest.fixture
def jwk_data(keypair: tuple[rsa.RSAPrivateKey, rsa.RSAPublicKey]) -> dict:
    return _pubkey_to_jwk(keypair[1])


@pytest.fixture
def discovery_resp() -> MagicMock:
    return _make_resp(json_data=_discovery_doc())


@pytest.fixture
def jwks_resp(jwk_data: dict) -> MagicMock:
    return _make_resp(json_data={"keys": [jwk_data]})


# ---------------------------------------------------------------------------
# JWKS fetching and caching
# ---------------------------------------------------------------------------


class TestJwksFetching:
    async def test_fetches_and_caches_jwks(
        self,
        keypair: tuple[rsa.RSAPrivateKey, rsa.RSAPublicKey],
        discovery_resp: MagicMock,
        jwks_resp: MagicMock,
    ) -> None:
        private_key, _ = keypair
        id_token = _create_id_token(private_key)
        mock_client = _make_httpx_mock(
            {_DISCOVERY_URL: discovery_resp, _JWKS_URI: jwks_resp},
        )

        with patch("httpx.AsyncClient") as cls:
            cls.return_value.__aenter__.return_value = mock_client

            claims1 = await verify_id_token_with_discovery(id_token, _DISCOVERY_URL, _CLIENT_ID)
            assert claims1["sub"] == "user123"

            claims2 = await verify_id_token_with_discovery(id_token, _DISCOVERY_URL, _CLIENT_ID)
            assert claims2["sub"] == "user123"

    async def test_raises_on_missing_jwks_uri(self) -> None:
        resp = _make_resp(json_data={"issuer": _ISSUER})
        mock_client = _make_httpx_mock({_DISCOVERY_URL: resp})

        with patch("httpx.AsyncClient") as cls:
            cls.return_value.__aenter__.return_value = mock_client
            with pytest.raises(OidcVerifyError, match="No jwks_uri"):
                await verify_id_token_with_discovery("token", _DISCOVERY_URL, "cid")

    async def test_raises_on_missing_issuer(self) -> None:
        resp = _make_resp(json_data={"jwks_uri": _JWKS_URI})
        mock_client = _make_httpx_mock({_DISCOVERY_URL: resp})

        with patch("httpx.AsyncClient") as cls:
            cls.return_value.__aenter__.return_value = mock_client
            with pytest.raises(OidcVerifyError, match="No issuer"):
                await verify_id_token_with_discovery("token", _DISCOVERY_URL, "cid")

    async def test_raises_on_empty_jwks(
        self,
        discovery_resp: MagicMock,
        keypair: tuple[rsa.RSAPrivateKey, rsa.RSAPublicKey],
    ) -> None:
        private_key, _ = keypair
        id_token = _create_id_token(private_key)
        empty_resp = _make_resp(json_data={"keys": []})
        mock_client = _make_httpx_mock(
            {_DISCOVERY_URL: discovery_resp, _JWKS_URI: empty_resp},
        )

        with patch("httpx.AsyncClient") as cls:
            cls.return_value.__aenter__.return_value = mock_client
            with pytest.raises(OidcVerifyError, match="No keys"):
                await verify_id_token_with_discovery(id_token, _DISCOVERY_URL, _CLIENT_ID)


# ---------------------------------------------------------------------------
# ID token verification — valid cases
# ---------------------------------------------------------------------------


class TestVerifyValidToken:
    async def test_verify_valid_token(
        self,
        keypair: tuple[rsa.RSAPrivateKey, rsa.RSAPublicKey],
        discovery_resp: MagicMock,
        jwks_resp: MagicMock,
    ) -> None:
        private_key, _ = keypair
        id_token = _create_id_token(private_key)
        mock_client = _make_httpx_mock(
            {_DISCOVERY_URL: discovery_resp, _JWKS_URI: jwks_resp},
        )

        with patch("httpx.AsyncClient") as cls:
            cls.return_value.__aenter__.return_value = mock_client
            claims = await verify_id_token_with_discovery(id_token, _DISCOVERY_URL, _CLIENT_ID)
            assert claims["sub"] == "user123"
            assert claims["email"] == "user123@example.com"
            assert claims["iss"] == _ISSUER
            assert claims["aud"] == _CLIENT_ID

    async def test_verify_direct_jwks_uri(
        self,
        keypair: tuple[rsa.RSAPrivateKey, rsa.RSAPublicKey],
        jwks_resp: MagicMock,
    ) -> None:
        private_key, _ = keypair
        id_token = _create_id_token(private_key)
        mock_client = _make_httpx_mock(
            {_DISCOVERY_URL: _make_resp(json_data=_discovery_doc()), _JWKS_URI: jwks_resp},
        )

        with patch("httpx.AsyncClient") as cls:
            cls.return_value.__aenter__.return_value = mock_client
            claims = await verify_id_token(id_token, _JWKS_URI, _CLIENT_ID, _ISSUER)
            assert claims["sub"] == "user123"

    async def test_verify_es256_token(self) -> None:
        private_key = ec.generate_private_key(ec.SECP256R1())
        id_token = _create_ec_id_token(private_key)
        jwks_resp = _make_resp(json_data={"keys": [_pubkey_to_ec_jwk(private_key.public_key())]})
        mock_client = _make_httpx_mock({_JWKS_URI: jwks_resp})

        with patch("httpx.AsyncClient") as cls:
            cls.return_value.__aenter__.return_value = mock_client
            claims = await verify_id_token(id_token, _JWKS_URI, _CLIENT_ID, _ISSUER)

        assert claims["sub"] == "ec-user"


# ---------------------------------------------------------------------------
# ID token verification — invalid signature
# ---------------------------------------------------------------------------


class TestVerifyInvalidSignature:
    async def test_fails_with_wrong_key(self, discovery_resp: MagicMock) -> None:
        correct_private, _ = _gen_rsa_keypair()
        _, wrong_public = _gen_rsa_keypair()
        id_token = _create_id_token(correct_private)
        wrong_jwk = _pubkey_to_jwk(wrong_public)
        jwks_resp = _make_resp(json_data={"keys": [wrong_jwk]})
        mock_client = _make_httpx_mock(
            {_DISCOVERY_URL: discovery_resp, _JWKS_URI: jwks_resp},
        )

        with patch("httpx.AsyncClient") as cls:
            cls.return_value.__aenter__.return_value = mock_client
            with pytest.raises(OidcVerifyError, match="ID token verification failed"):
                await verify_id_token_with_discovery(id_token, _DISCOVERY_URL, _CLIENT_ID)

    async def test_fails_with_tampered_token(
        self,
        keypair: tuple[rsa.RSAPrivateKey, rsa.RSAPublicKey],
        discovery_resp: MagicMock,
        jwks_resp: MagicMock,
    ) -> None:
        private_key, _ = keypair
        id_token = _create_id_token(private_key)
        parts = id_token.split(".")
        tampered_payload = base64.urlsafe_b64encode(b'{"sub":"hacker"}').rstrip(b"=").decode()
        tampered = f"{parts[0]}.{tampered_payload}.{parts[2]}"
        mock_client = _make_httpx_mock(
            {_DISCOVERY_URL: discovery_resp, _JWKS_URI: jwks_resp},
        )

        with patch("httpx.AsyncClient") as cls:
            cls.return_value.__aenter__.return_value = mock_client
            with pytest.raises(OidcVerifyError, match="ID token verification failed"):
                await verify_id_token_with_discovery(tampered, _DISCOVERY_URL, _CLIENT_ID)


# ---------------------------------------------------------------------------
# Claim validation: iss, aud, exp
# ---------------------------------------------------------------------------


class TestClaimValidation:
    async def test_fails_on_wrong_issuer(
        self,
        keypair: tuple[rsa.RSAPrivateKey, rsa.RSAPublicKey],
        discovery_resp: MagicMock,
        jwks_resp: MagicMock,
    ) -> None:
        private_key, _ = keypair
        id_token = _create_id_token(private_key, issuer="https://evil.com")
        mock_client = _make_httpx_mock(
            {_DISCOVERY_URL: discovery_resp, _JWKS_URI: jwks_resp},
        )

        with patch("httpx.AsyncClient") as cls:
            cls.return_value.__aenter__.return_value = mock_client
            with pytest.raises(OidcVerifyError, match="verification failed"):
                await verify_id_token_with_discovery(id_token, _DISCOVERY_URL, _CLIENT_ID)

    async def test_fails_on_wrong_audience(
        self,
        keypair: tuple[rsa.RSAPrivateKey, rsa.RSAPublicKey],
        discovery_resp: MagicMock,
        jwks_resp: MagicMock,
    ) -> None:
        private_key, _ = keypair
        id_token = _create_id_token(private_key, audience="wrong-client")
        mock_client = _make_httpx_mock(
            {_DISCOVERY_URL: discovery_resp, _JWKS_URI: jwks_resp},
        )

        with patch("httpx.AsyncClient") as cls:
            cls.return_value.__aenter__.return_value = mock_client
            with pytest.raises(OidcVerifyError, match="verification failed"):
                await verify_id_token_with_discovery(id_token, _DISCOVERY_URL, _CLIENT_ID)

    async def test_fails_on_expired_token(
        self,
        keypair: tuple[rsa.RSAPrivateKey, rsa.RSAPublicKey],
        discovery_resp: MagicMock,
        jwks_resp: MagicMock,
    ) -> None:
        private_key, _ = keypair
        id_token = _create_id_token(private_key, exp_offset=timedelta(hours=-2))
        mock_client = _make_httpx_mock(
            {_DISCOVERY_URL: discovery_resp, _JWKS_URI: jwks_resp},
        )

        with patch("httpx.AsyncClient") as cls:
            cls.return_value.__aenter__.return_value = mock_client
            with pytest.raises(OidcVerifyError, match="verification failed"):
                await verify_id_token_with_discovery(id_token, _DISCOVERY_URL, _CLIENT_ID)


# ---------------------------------------------------------------------------
# JWKS rotation handling
# ---------------------------------------------------------------------------


class TestJwksRotation:
    async def test_retries_with_new_key_after_kid_miss(
        self,
        keypair: tuple[rsa.RSAPrivateKey, rsa.RSAPublicKey],
        discovery_resp: MagicMock,
    ) -> None:
        private_key, _ = keypair
        _, old_public = _gen_rsa_keypair()
        old_jwk = _pubkey_to_jwk(old_public, kid="old-key")
        new_jwk = _pubkey_to_jwk(keypair[1], kid="new-key")
        id_token = _create_id_token(private_key, kid="new-key")

        call_count = 0

        async def _get(url: str, **kwargs: object) -> MagicMock:
            nonlocal call_count
            if url == _DISCOVERY_URL:
                return discovery_resp
            call_count += 1
            if call_count == 1:
                return _make_resp(json_data={"keys": [old_jwk]})
            return _make_resp(json_data={"keys": [new_jwk]})

        mock_client = AsyncMock()
        mock_client.get = _get
        mock_client.post = AsyncMock()  # not used in verify_id_token_with_discovery

        with patch("httpx.AsyncClient") as cls:
            cls.return_value.__aenter__.return_value = mock_client
            claims = await verify_id_token_with_discovery(id_token, _DISCOVERY_URL, _CLIENT_ID)
            assert claims["sub"] == "user123"
            assert call_count == 2

    async def test_retries_after_signature_failure(
        self,
        keypair: tuple[rsa.RSAPrivateKey, rsa.RSAPublicKey],
        discovery_resp: MagicMock,
    ) -> None:
        private_key, _ = keypair
        _, wrong_public = _gen_rsa_keypair()
        wrong_jwk = _pubkey_to_jwk(wrong_public, kid="test-key-1")
        correct_jwk = _pubkey_to_jwk(keypair[1], kid="test-key-1")
        id_token = _create_id_token(private_key, kid="test-key-1")

        call_count = 0

        async def _get(url: str, **kwargs: object) -> MagicMock:
            nonlocal call_count
            if url == _DISCOVERY_URL:
                return discovery_resp
            call_count += 1
            if call_count == 1:
                return _make_resp(json_data={"keys": [wrong_jwk]})
            return _make_resp(json_data={"keys": [correct_jwk]})

        mock_client = AsyncMock()
        mock_client.get = _get

        with patch("httpx.AsyncClient") as cls:
            cls.return_value.__aenter__.return_value = mock_client
            claims = await verify_id_token_with_discovery(id_token, _DISCOVERY_URL, _CLIENT_ID)
            assert claims["sub"] == "user123"
            assert call_count == 2


# ---------------------------------------------------------------------------
# Malformed / edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    async def test_fails_on_malformed_token(self) -> None:
        for bad in ("not-a-jwt", "no.dots", ""):
            with pytest.raises(OidcVerifyError, match="Invalid JWT format"):
                await verify_id_token(bad, _JWKS_URI, _CLIENT_ID, _ISSUER)

    def test_cache_clear_works(self) -> None:
        from modulo.auth.oidc_verify import _cache_set, _jwks_cache

        _cache_set("https://example.com/jwks", [{"kty": "RSA"}])
        assert len(_jwks_cache) == 1
        clear_jwks_cache()
        assert not _jwks_cache

    def test_cache_expires_after_ttl(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import time

        import modulo.auth.oidc_verify as oidc_verify

        oidc_verify._cache_set("https://example.com/jwks", [{"kty": "RSA"}])
        future = time.time() + oidc_verify._JWKS_CACHE_TTL + 1

        monkeypatch.setattr(time, "time", lambda: future)

        assert oidc_verify._cache_get("https://example.com/jwks") is None
        assert "https://example.com/jwks" not in oidc_verify._jwks_cache

    async def test_fails_on_discovery_http_error(self) -> None:
        mock_client = _make_httpx_mock({_DISCOVERY_URL: _make_resp(500)})

        with patch("httpx.AsyncClient") as cls:
            cls.return_value.__aenter__.return_value = mock_client
            with pytest.raises(OidcVerifyError, match="Failed to fetch discovery document"):
                await verify_id_token_with_discovery("token", _DISCOVERY_URL, "cid")

    async def test_fails_on_non_dict_discovery(self) -> None:
        resp = _make_resp(json_data=["not", "a", "dict"])
        mock_client = _make_httpx_mock({_DISCOVERY_URL: resp})

        with patch("httpx.AsyncClient") as cls:
            cls.return_value.__aenter__.return_value = mock_client
            with pytest.raises(OidcVerifyError, match="not a JSON object"):
                await verify_id_token_with_discovery("token", _DISCOVERY_URL, "cid")

    async def test_fails_on_jwks_http_error(self) -> None:
        mock_client = _make_httpx_mock({_JWKS_URI: _make_resp(500)})

        with patch("httpx.AsyncClient") as cls:
            cls.return_value.__aenter__.return_value = mock_client
            with pytest.raises(OidcVerifyError, match="Failed to fetch JWKS"):
                await verify_id_token(_ID_TOKEN, _JWKS_URI, _CLIENT_ID, _ISSUER)

    async def test_fails_on_non_dict_jwks(self) -> None:
        resp = _make_resp(json_data=["keys"])
        mock_client = _make_httpx_mock({_JWKS_URI: resp})

        with patch("httpx.AsyncClient") as cls:
            cls.return_value.__aenter__.return_value = mock_client
            with pytest.raises(OidcVerifyError, match="not a JSON object"):
                await verify_id_token(_ID_TOKEN, _JWKS_URI, _CLIENT_ID, _ISSUER)

    async def test_unsupported_alg_rejected(self) -> None:
        now = int(datetime.now(UTC).timestamp())
        payload = {"sub": "u", "iss": _ISSUER, "aud": _CLIENT_ID, "iat": now, "exp": now + 3600}

        def _b64(data: bytes) -> str:
            return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

        header = _b64(json.dumps({"alg": "none", "typ": "JWT"}).encode())
        body = _b64(json.dumps(payload).encode())
        token = f"{header}.{body}."

        mock_client = _make_httpx_mock({_JWKS_URI: _make_resp(json_data={"keys": []})})

        with patch("httpx.AsyncClient") as cls:
            cls.return_value.__aenter__.return_value = mock_client
            with pytest.raises(OidcVerifyError, match="Unsupported JWT algorithm"):
                await verify_id_token(token, _JWKS_URI, _CLIENT_ID, _ISSUER)

    async def test_fails_on_unparseable_header(self) -> None:
        # First segment decodes to something that is not a JSON object.
        malformed_header = base64.urlsafe_b64encode(b"not-json!").rstrip(b"=").decode()
        token = f"{malformed_header}.cGF5bG9hZA.s2lnbmF0dXJl"

        with pytest.raises(OidcVerifyError, match="Failed to decode JWT header"):
            await verify_id_token(token, _JWKS_URI, _CLIENT_ID, _ISSUER)

    async def test_fails_to_build_key_from_malformed_jwk(
        self,
        keypair: tuple[rsa.RSAPrivateKey, rsa.RSAPublicKey],
    ) -> None:
        private_key, _ = keypair
        id_token = _create_id_token(private_key)
        # An unknown ``alg`` makes PyJWK key construction fail.
        bad_jwk = {"kty": "RSA", "kid": "test-key-1", "alg": "NOTREAL"}
        resp = _make_resp(json_data={"keys": [bad_jwk]})
        mock_client = _make_httpx_mock({_JWKS_URI: resp})

        with patch("httpx.AsyncClient") as cls:
            cls.return_value.__aenter__.return_value = mock_client
            with pytest.raises(OidcVerifyError, match="Failed to construct key from JWK"):
                await verify_id_token(id_token, _JWKS_URI, _CLIENT_ID, _ISSUER)

    async def test_kidless_token_falls_back_to_first_key(
        self,
        keypair: tuple[rsa.RSAPrivateKey, rsa.RSAPublicKey],
    ) -> None:
        # Providers may omit `kid`; then the first key in the set must be used.
        private_key, _ = keypair
        now = datetime.now(UTC)
        token = pyjwt.encode(
            {"sub": "kidless", "iss": _ISSUER, "aud": _CLIENT_ID, "iat": now, "exp": now + timedelta(hours=1)},
            private_key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            ),
            algorithm="RS256",
        )
        jwks_resp = _make_resp(json_data={"keys": [_pubkey_to_jwk(keypair[1])]})
        mock_client = _make_httpx_mock({_JWKS_URI: jwks_resp})

        with patch("httpx.AsyncClient") as cls:
            cls.return_value.__aenter__.return_value = mock_client
            claims = await verify_id_token(token, _JWKS_URI, _CLIENT_ID, _ISSUER)

        assert claims["sub"] == "kidless"


# ---------------------------------------------------------------------------
# Integration with sso.py callback flow
# ---------------------------------------------------------------------------


class TestOidcCallbackIntegration:
    async def test_callback_calls_verify(
        self,
        keypair: tuple[rsa.RSAPrivateKey, rsa.RSAPublicKey],
    ) -> None:
        from modulo.auth.sso import oidc_process_callback, sign_state
        from modulo.settings import Settings

        private_key, public_key = keypair
        jwk = _pubkey_to_jwk(public_key)
        id_token = _create_id_token(private_key)

        settings = Settings(
            database_url="postgresql+asyncpg://localhost/test",
            secret_key="a" * 32,
            fernet_key="a" * 32,
            modulo_oidc_providers=json.dumps(
                [
                    {
                        "provider_id": "testprovider",
                        "client_id": _CLIENT_ID,
                        "client_secret": "secret",
                        "discovery_url": _DISCOVERY_URL,
                    }
                ]
            ),
        )

        session = AsyncMock()
        signed = sign_state("testprovider:test-state", settings.secret_key)

        discovery_doc = _discovery_doc()
        disc_resp = _make_resp(json_data=discovery_doc)
        token_resp = _make_resp(json_data={"id_token": id_token, "access_token": "at"})
        jwks_resp = _make_resp(json_data={"keys": [jwk]})

        mock_client = _make_httpx_mock(
            get_map={_DISCOVERY_URL: disc_resp, _JWKS_URI: jwks_resp},
            post_map={_TOKEN_ENDPOINT: token_resp},
        )

        with (
            patch("modulo.auth.sso.jit_provision_user", new_callable=AsyncMock) as mock_jit,
            patch("modulo.auth.sso.issue_sso_tokens", new_callable=AsyncMock) as mock_tok,
            patch("httpx.AsyncClient") as cls,
        ):
            cls.return_value.__aenter__.return_value = mock_client
            mock_jit.return_value = (MagicMock(), uuid.uuid4(), "runner")
            mock_tok.return_value = {"access_token": "at", "refresh_token": "rt", "token_type": "bearer"}

            result = await oidc_process_callback(
                "auth-code",
                signed,
                settings,
                session,
                "http://localhost/callback",
            )
            assert result["access_token"] == "at"

    async def test_callback_fails_on_bad_signature(
        self,
        keypair: tuple[rsa.RSAPrivateKey, rsa.RSAPublicKey],
    ) -> None:
        from modulo.auth.sso import oidc_process_callback, sign_state
        from modulo.settings import Settings

        wrong_private, _ = _gen_rsa_keypair()
        _, correct_public = keypair
        jwk = _pubkey_to_jwk(correct_public)
        id_token = _create_id_token(wrong_private)

        settings = Settings(
            database_url="postgresql+asyncpg://localhost/test",
            secret_key="a" * 32,
            fernet_key="a" * 32,
            modulo_oidc_providers=json.dumps(
                [
                    {
                        "provider_id": "testprovider",
                        "client_id": _CLIENT_ID,
                        "client_secret": "secret",
                        "discovery_url": _DISCOVERY_URL,
                    }
                ]
            ),
        )

        session = AsyncMock()
        signed = sign_state("testprovider:state", settings.secret_key)

        discovery_doc = _discovery_doc()
        disc_resp = _make_resp(json_data=discovery_doc)
        token_resp = _make_resp(json_data={"id_token": id_token})
        jwks_resp = _make_resp(json_data={"keys": [jwk]})

        mock_client = _make_httpx_mock(
            get_map={_DISCOVERY_URL: disc_resp, _JWKS_URI: jwks_resp},
            post_map={_TOKEN_ENDPOINT: token_resp},
        )

        with (
            patch("modulo.auth.sso.jit_provision_user", new_callable=AsyncMock),
            patch("modulo.auth.sso.issue_sso_tokens", new_callable=AsyncMock),
            patch("httpx.AsyncClient") as cls,
        ):
            cls.return_value.__aenter__.return_value = mock_client

            with pytest.raises(ValueError, match="ID token verification failed"):
                await oidc_process_callback(
                    "auth-code",
                    signed,
                    settings,
                    session,
                    "http://localhost/callback",
                )

    async def test_callback_fails_on_discovery_http_error(self) -> None:
        from modulo.auth.sso import oidc_process_callback, sign_state
        from modulo.settings import Settings

        settings = Settings(
            database_url="postgresql+asyncpg://localhost/test",
            secret_key="a" * 32,
            fernet_key="a" * 32,
            modulo_oidc_providers=json.dumps(
                [
                    {
                        "provider_id": "testprovider",
                        "client_id": _CLIENT_ID,
                        "client_secret": "secret",
                        "discovery_url": _DISCOVERY_URL,
                    }
                ]
            ),
        )

        session = AsyncMock()
        signed = sign_state("testprovider:test-state", settings.secret_key)

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=httpx.ConnectError("Connection refused"))
        mock_client.post = AsyncMock()

        with patch("httpx.AsyncClient") as cls:
            cls.return_value.__aenter__.return_value = mock_client

            with pytest.raises(ValueError, match="Failed to fetch discovery document"):
                await oidc_process_callback(
                    "auth-code",
                    signed,
                    settings,
                    session,
                    "http://localhost/callback",
                )

    async def test_callback_fails_on_code_exchange_http_error(self) -> None:
        from modulo.auth.sso import oidc_process_callback, sign_state
        from modulo.settings import Settings

        settings = Settings(
            database_url="postgresql+asyncpg://localhost/test",
            secret_key="a" * 32,
            fernet_key="a" * 32,
            modulo_oidc_providers=json.dumps(
                [
                    {
                        "provider_id": "testprovider",
                        "client_id": _CLIENT_ID,
                        "client_secret": "secret",
                        "discovery_url": _DISCOVERY_URL,
                    }
                ]
            ),
        )

        session = AsyncMock()
        signed = sign_state("testprovider:test-state", settings.secret_key)

        disc_resp = _make_resp(json_data=_discovery_doc())
        token_resp = _make_resp(500)
        mock_client = _make_httpx_mock(
            get_map={_DISCOVERY_URL: disc_resp},
            post_map={_TOKEN_ENDPOINT: token_resp},
        )

        with patch("httpx.AsyncClient") as cls:
            cls.return_value.__aenter__.return_value = mock_client

            with pytest.raises(ValueError, match="Failed to exchange authorization code"):
                await oidc_process_callback(
                    "auth-code",
                    signed,
                    settings,
                    session,
                    "http://localhost/callback",
                )

    async def test_callback_fails_on_provisioning_runtime_error(self) -> None:
        from modulo.auth.sso import oidc_process_callback, sign_state
        from modulo.settings import Settings

        settings = Settings(
            database_url="postgresql+asyncpg://localhost/test",
            secret_key="a" * 32,
            fernet_key="a" * 32,
            modulo_oidc_providers=json.dumps(
                [
                    {
                        "provider_id": "testprovider",
                        "client_id": _CLIENT_ID,
                        "client_secret": "secret",
                        "discovery_url": _DISCOVERY_URL,
                    }
                ]
            ),
        )

        session = AsyncMock()
        signed = sign_state("testprovider:test-state", settings.secret_key)

        disc_resp = _make_resp(json_data=_discovery_doc())
        token_resp = _make_resp(json_data={"id_token": "header.payload.sig"})

        mock_client = _make_httpx_mock(
            get_map={_DISCOVERY_URL: disc_resp},
            post_map={_TOKEN_ENDPOINT: token_resp},
        )

        with (
            patch("modulo.auth.sso.verify_id_token", new_callable=AsyncMock) as mock_verify,
            patch("modulo.auth.sso.jit_provision_user", new_callable=AsyncMock) as mock_jit,
            patch("modulo.auth.sso.issue_sso_tokens", new_callable=AsyncMock),
            patch("httpx.AsyncClient") as cls,
        ):
            cls.return_value.__aenter__.return_value = mock_client
            mock_verify.return_value = {
                "email": "user@example.com",
                "name": "Test User",
                "sub": "abc123",
            }
            mock_jit.side_effect = RuntimeError("No organisation exists")

            with pytest.raises(ValueError, match="No organisation exists"):
                await oidc_process_callback(
                    "auth-code",
                    signed,
                    settings,
                    session,
                    "http://localhost/callback",
                )
