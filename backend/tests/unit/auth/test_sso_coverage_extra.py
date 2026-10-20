"""Extra coverage for ``modulo.auth.sso`` to keep ``modulo.auth`` above its 90% gate.

These target pure-logic branches and network/error paths that the happy-path
SSO tests never exercise (SSRF rejections, malformed discovery/token JSON,
missing-idP-metadata branches, SAML destination/group-mapping edges, and the
non-dict group-mapping guard). All network calls are mocked.
"""

import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.auth.sso import (
    _audit_invite_consumed_fail_open,
    _decode_saml_response,
    _exchange_code,
    _fetch_discovery,
    _fetch_discovery_pinned,
    _parse_oidc_providers,
    _read_system_oidc_provider,
    _read_system_saml_provider,
    _require_json_object,
    _resolve_oidc_provider,
    _resolve_saml_config,
    _saml_parse_idp_metadata,
    apply_group_mappings,
    oidc_get_authorize_url,
    oidc_process_callback,
    saml_get_auth_url,
    saml_process_response,
    sign_state,
)
from modulo.settings import Settings


def _override(**kwargs: object) -> Settings:
    base: dict[str, object] = {
        "database_url": "postgresql+asyncpg://localhost/test",
        "secret_key": "a" * 32,
        "fernet_key": "a" * 32,
        "modulo_license_key": "test-license",
        "modulo_public_url": "https://modulo.example.com",
        "modulo_saml_enabled": True,
        "modulo_oidc_providers": json.dumps(
            [
                {
                    "provider_id": "google",
                    "client_id": "google-client-id",
                    "client_secret": "google-client-secret",
                    "discovery_url": "https://accounts.google.com/.well-known/openid-configuration",
                }
            ]
        ),
    }
    base.update(kwargs)
    return Settings(**base)  # type: ignore[arg-type]


def _mock_session(scalar: object = None) -> AsyncMock:
    session = AsyncMock(spec=AsyncSession)
    result = MagicMock()
    result.scalar_one_or_none.return_value = scalar
    result.scalars.return_value.all.return_value = []
    session.execute.return_value = result
    return session


# ---------------------------------------------------------------------------
# apply_group_mappings — non-dict guard
# ---------------------------------------------------------------------------


async def test_apply_group_mappings_non_dict_mapping() -> None:
    session = _mock_session()
    account = SimpleNamespace(id=uuid.uuid4())
    with (
        patch("modulo.auth.sso.get_membership_by_team_and_account", new_callable=AsyncMock, return_value=None),
        patch("modulo.auth.sso.add_team_member", new_callable=AsyncMock) as add_mock,
    ):
        await apply_group_mappings(session, account, uuid.uuid4(), ["g"], ["not-a-dict"])
    add_mock.assert_not_called()


# ---------------------------------------------------------------------------
# _parse_oidc_providers — empty + non-array branches
# ---------------------------------------------------------------------------


def test_parse_oidc_providers_empty() -> None:
    assert not _parse_oidc_providers(_override(modulo_oidc_providers=""))


def test_parse_oidc_providers_not_array() -> None:
    assert not _parse_oidc_providers(_override(modulo_oidc_providers=json.dumps(123)))


# ---------------------------------------------------------------------------
# _require_json_object — validation branches
# ---------------------------------------------------------------------------


def test_require_json_object_non_dict() -> None:
    with pytest.raises(ValueError, match="must be a JSON object"):
        _require_json_object(123, "ctx")


def test_require_json_object_non_string_key() -> None:
    with pytest.raises(ValueError, match="non-string key"):
        _require_json_object({1: 2}, "ctx")


def test_require_json_object_valid() -> None:
    assert _require_json_object({"a": 1}, "ctx") == {"a": 1}


# ---------------------------------------------------------------------------
# discovery / token JSON decode errors
# ---------------------------------------------------------------------------


async def test_fetch_discovery_json_decode_error() -> None:
    response = MagicMock()
    response.json.side_effect = json.JSONDecodeError("bad", "doc", 0)
    client = AsyncMock()
    client.get.return_value = response
    with patch("modulo.auth.sso.httpx.AsyncClient") as client_type:
        client_type.return_value.__aenter__.return_value = client
        with pytest.raises(ValueError, match="Invalid JSON in discovery document"):
            await _fetch_discovery("https://issuer.example/discovery")


async def test_fetch_discovery_pinned_json_decode_error() -> None:
    response = MagicMock()
    response.json.side_effect = json.JSONDecodeError("bad", "doc", 0)
    client = AsyncMock()
    client.get.return_value = response
    pacer = AsyncMock()
    pacer.return_value.__aenter__.return_value = client
    with (
        patch("modulo.auth.sso.pinned_async_client", pacer),
        pytest.raises(ValueError, match="Invalid JSON in discovery document"),
    ):
        await _fetch_discovery_pinned("https://issuer.example/discovery")


async def test_exchange_code_json_decode_error() -> None:
    response = MagicMock()
    response.json.side_effect = json.JSONDecodeError("bad", "doc", 0)
    client = AsyncMock()
    client.post.return_value = response
    with patch("modulo.auth.sso.httpx.AsyncClient") as client_type:
        client_type.return_value.__aenter__.return_value = client
        with pytest.raises(ValueError, match="Invalid JSON in OIDC token response"):
            await _exchange_code("https://tok", "cid", "csec", "code", "http://cb", "verifier")


# ---------------------------------------------------------------------------
# system session readers — None session short-circuits
# ---------------------------------------------------------------------------


async def test_read_system_oidc_provider_none_session() -> None:
    assert await _read_system_oidc_provider(None, "pid") is None


async def test_read_system_saml_provider_none_session() -> None:
    assert await _read_system_saml_provider(None) is None


# ---------------------------------------------------------------------------
# _resolve_oidc_provider — SSRF rejection of discovery_url
# ---------------------------------------------------------------------------


async def test_resolve_oidc_provider_ssrf_rejects_discovery_url() -> None:
    settings = _override()
    session = _mock_session()
    db_provider = SimpleNamespace(
        provider_type="oidc",
        enabled=True,
        discovery_url="http://169.254.169.254/",
        client_id="c",
        client_secret="s",
        scopes=None,
    )
    with (
        patch("modulo.auth.sso.get_provider_by_provider_id", new_callable=AsyncMock, return_value=db_provider),
        patch(
            "modulo.auth.sso.validate_outbound_url_async",
            new_callable=AsyncMock,
            side_effect=ValueError("blocked"),
        ),
        pytest.raises(ValueError, match="Rejected OIDC discovery_url"),
    ):
        await _resolve_oidc_provider("pid", None, session, settings)


# ---------------------------------------------------------------------------
# oidc_get_authorize_url — missing discovery_url + discovery fetch error
# ---------------------------------------------------------------------------


async def test_authorize_missing_discovery_url() -> None:
    settings = _override()
    session = _mock_session()
    db_provider = SimpleNamespace(
        provider_type="oidc",
        enabled=True,
        discovery_url=None,
        client_id="c",
        client_secret="s",
        scopes=None,
    )
    with (
        patch("modulo.auth.sso.get_provider_by_provider_id", new_callable=AsyncMock, return_value=db_provider),
        pytest.raises(ValueError, match="missing client_id or discovery_url"),
    ):
        await oidc_get_authorize_url("pid", settings, "http://cb", session, session)


async def test_authorize_discovery_fetch_error() -> None:
    settings = _override()
    session = _mock_session()
    with (
        patch("modulo.auth.sso._fetch_discovery", new_callable=AsyncMock, side_effect=httpx.ConnectError("x")),
        pytest.raises(ValueError, match="Failed to fetch discovery document"),
    ):
        await oidc_get_authorize_url("google", settings, "http://cb", session, session)


# ---------------------------------------------------------------------------
# oidc_process_callback — configuration / token-error branches
# ---------------------------------------------------------------------------


async def test_callback_missing_client_secret() -> None:
    settings = _override()
    session = _mock_session()
    signed = sign_state("google:state", settings.secret_key)
    with (
        patch(
            "modulo.auth.sso._resolve_oidc_provider",
            new_callable=AsyncMock,
            return_value=("cid", None, "https://d", None, None),
        ),
        pytest.raises(ValueError, match="missing required configuration"),
    ):
        await oidc_process_callback(
            "code",
            signed,
            settings,
            session,
            session,
            "http://cb",
            expected_nonce="test-nonce",
            code_verifier="test-verifier",
        )


async def test_callback_missing_token_endpoint() -> None:
    settings = _override()
    session = _mock_session()
    signed = sign_state("google:state", settings.secret_key)
    with (
        patch(
            "modulo.auth.sso._resolve_oidc_provider",
            new_callable=AsyncMock,
            return_value=("cid", "csec", "https://d", None, None),
        ),
        patch(
            "modulo.auth.sso._fetch_discovery", new_callable=AsyncMock, return_value={"jwks_uri": "j", "issuer": "i"}
        ),
        pytest.raises(ValueError, match="No token_endpoint"),
    ):
        await oidc_process_callback(
            "code",
            signed,
            settings,
            session,
            session,
            "http://cb",
            expected_nonce="test-nonce",
            code_verifier="test-verifier",
        )


async def test_callback_missing_id_token() -> None:
    settings = _override()
    session = _mock_session()
    signed = sign_state("google:state", settings.secret_key)
    with (
        patch(
            "modulo.auth.sso._resolve_oidc_provider",
            new_callable=AsyncMock,
            return_value=("cid", "csec", "https://d", None, None),
        ),
        patch(
            "modulo.auth.sso._fetch_discovery",
            new_callable=AsyncMock,
            return_value={"token_endpoint": "https://d/token", "jwks_uri": "https://d/jwks", "issuer": "i"},
        ),
        patch("modulo.auth.sso._exchange_code", new_callable=AsyncMock, return_value={}),
        pytest.raises(ValueError, match="missing a valid id_token"),
    ):
        await oidc_process_callback(
            "code",
            signed,
            settings,
            session,
            session,
            "http://cb",
            expected_nonce="test-nonce",
            code_verifier="test-verifier",
        )


async def test_callback_missing_jwks_or_issuer() -> None:
    settings = _override()
    session = _mock_session()
    signed = sign_state("google:state", settings.secret_key)
    with (
        patch(
            "modulo.auth.sso._resolve_oidc_provider",
            new_callable=AsyncMock,
            return_value=("cid", "csec", "https://d", None, None),
        ),
        patch(
            "modulo.auth.sso._fetch_discovery",
            new_callable=AsyncMock,
            return_value={"token_endpoint": "https://d/token"},
        ),
        patch("modulo.auth.sso._exchange_code", new_callable=AsyncMock, return_value={"id_token": "x"}),
        pytest.raises(ValueError, match="missing jwks_uri or issuer"),
    ):
        await oidc_process_callback(
            "code",
            signed,
            settings,
            session,
            session,
            "http://cb",
            expected_nonce="test-nonce",
            code_verifier="test-verifier",
        )


async def test_callback_missing_email() -> None:
    settings = _override()
    session = _mock_session()
    signed = sign_state("google:state", settings.secret_key)
    with (
        patch(
            "modulo.auth.sso._resolve_oidc_provider",
            new_callable=AsyncMock,
            return_value=("cid", "csec", "https://d", None, None),
        ),
        patch(
            "modulo.auth.sso._fetch_discovery",
            new_callable=AsyncMock,
            return_value={"token_endpoint": "https://d/token", "jwks_uri": "https://d/jwks", "issuer": "i"},
        ),
        patch("modulo.auth.sso._exchange_code", new_callable=AsyncMock, return_value={"id_token": "x"}),
        patch("modulo.auth.sso.verify_id_token", new_callable=AsyncMock, return_value={}),
        pytest.raises(ValueError, match="did not return an email"),
    ):
        await oidc_process_callback(
            "code",
            signed,
            settings,
            session,
            session,
            "http://cb",
            expected_nonce="test-nonce",
            code_verifier="test-verifier",
        )


async def test_callback_no_app_session() -> None:
    settings = _override()
    session = _mock_session()
    signed = sign_state("google:state", settings.secret_key)
    with (
        patch(
            "modulo.auth.sso._resolve_oidc_provider",
            new_callable=AsyncMock,
            return_value=("cid", "csec", "https://d", None, None),
        ),
        patch(
            "modulo.auth.sso._fetch_discovery",
            new_callable=AsyncMock,
            return_value={"token_endpoint": "https://d/token", "jwks_uri": "https://d/jwks", "issuer": "i"},
        ),
        patch("modulo.auth.sso._exchange_code", new_callable=AsyncMock, return_value={"id_token": "x"}),
        patch(
            "modulo.auth.sso.verify_id_token",
            new_callable=AsyncMock,
            return_value={"email": "u@e.z", "sub": "s"},
        ),
        pytest.raises(RuntimeError, match="No app session"),
    ):
        await oidc_process_callback(
            "code",
            signed,
            settings,
            session,
            None,
            "http://cb",
            expected_nonce="test-nonce",
            code_verifier="test-verifier",
        )


async def test_callback_groups_not_a_list() -> None:
    settings = _override()
    session = _mock_session()
    signed = sign_state("google:state", settings.secret_key)
    with (
        patch(
            "modulo.auth.sso._resolve_oidc_provider",
            new_callable=AsyncMock,
            return_value=("cid", "csec", "https://d", None, None),
        ),
        patch(
            "modulo.auth.sso._fetch_discovery",
            new_callable=AsyncMock,
            return_value={"token_endpoint": "https://d/token", "jwks_uri": "https://d/jwks", "issuer": "i"},
        ),
        patch("modulo.auth.sso._exchange_code", new_callable=AsyncMock, return_value={"id_token": "x"}),
        patch(
            "modulo.auth.sso.verify_id_token",
            new_callable=AsyncMock,
            return_value={"email": "u@e.z", "sub": "s", "groups": "notalist"},
        ),
        patch(
            "modulo.auth.sso.jit_provision_user",
            new_callable=AsyncMock,
            return_value=(MagicMock(), uuid.uuid4(), "viewer"),
        ),
        patch(
            "modulo.auth.sso.issue_sso_tokens", new_callable=AsyncMock, return_value={"access_token": "a"}
        ) as issue_mock,
    ):
        await oidc_process_callback(
            "code",
            signed,
            settings,
            session,
            session,
            "http://cb",
            expected_nonce="test-nonce",
            code_verifier="test-verifier",
        )
    issue_mock.assert_called_once()


async def test_callback_applies_group_mappings() -> None:
    settings = _override()
    session = _mock_session()
    signed = sign_state("google:state", settings.secret_key)
    db_provider = SimpleNamespace(
        group_mappings=[{"idp_group": "g1", "team_id": str(uuid.uuid4()), "team_role": "editor"}]
    )
    with (
        patch(
            "modulo.auth.sso._resolve_oidc_provider",
            new_callable=AsyncMock,
            return_value=("cid", "csec", "https://d", None, None),
        ),
        patch(
            "modulo.auth.sso._fetch_discovery",
            new_callable=AsyncMock,
            return_value={"token_endpoint": "https://d/token", "jwks_uri": "https://d/jwks", "issuer": "i"},
        ),
        patch("modulo.auth.sso._exchange_code", new_callable=AsyncMock, return_value={"id_token": "x"}),
        patch(
            "modulo.auth.sso.verify_id_token",
            new_callable=AsyncMock,
            return_value={"email": "u@e.z", "sub": "s", "groups": ["g1"]},
        ),
        patch(
            "modulo.auth.sso.jit_provision_user",
            new_callable=AsyncMock,
            return_value=(MagicMock(), uuid.uuid4(), "viewer"),
        ),
        patch(
            "modulo.auth.sso._lookup_provider_by_client_id",
            new_callable=AsyncMock,
            return_value=db_provider,
        ),
        patch("modulo.auth.sso.apply_group_mappings", new_callable=AsyncMock) as mock_apply,
        patch("modulo.auth.sso.issue_sso_tokens", new_callable=AsyncMock, return_value={"access_token": "a"}),
    ):
        await oidc_process_callback(
            "code",
            signed,
            settings,
            session,
            session,
            "http://cb",
            expected_nonce="test-nonce",
            code_verifier="test-verifier",
        )
        mock_apply.assert_awaited_once()


# ---------------------------------------------------------------------------
# _resolve_saml_config — metadata_url SSRF / fetch-error / missing branches
# ---------------------------------------------------------------------------


async def test_resolve_saml_config_metadata_url_ssrf_rejected() -> None:
    settings = _override()
    session = _mock_session()
    db_saml = SimpleNamespace(
        metadata_xml=None,
        metadata_url="http://169.254.169.254/meta",
        entity_id="idp",
        provider_type="saml",
        enabled=True,
    )
    with (
        patch("modulo.auth.sso.get_enabled_saml_provider", new_callable=AsyncMock, return_value=db_saml),
        patch(
            "modulo.auth.sso.validate_outbound_url_async",
            new_callable=AsyncMock,
            side_effect=ValueError("blocked"),
        ),
        pytest.raises(ValueError, match="Rejected SAML metadata_url"),
    ):
        await _resolve_saml_config(None, session, settings)


async def test_resolve_saml_config_metadata_url_fetch_error() -> None:
    settings = _override()
    session = _mock_session()
    db_saml = SimpleNamespace(
        metadata_xml=None,
        metadata_url="https://idp.example/meta",
        entity_id="idp",
        provider_type="saml",
        enabled=True,
    )
    client = AsyncMock()
    client.get.side_effect = httpx.ConnectError("x")
    pacer = AsyncMock()
    pacer.return_value.__aenter__.return_value = client
    with (
        patch("modulo.auth.sso.get_enabled_saml_provider", new_callable=AsyncMock, return_value=db_saml),
        patch("modulo.auth.sso.validate_outbound_url_async", new_callable=AsyncMock),
        patch("modulo.auth.sso.pinned_async_client", pacer),
        pytest.raises(ValueError, match="Failed to fetch SAML IdP metadata"),
    ):
        await _resolve_saml_config(None, session, settings)


async def test_resolve_saml_config_metadata_url_fetch_success() -> None:
    settings = _override()
    session = _mock_session()
    db_saml = SimpleNamespace(
        metadata_xml=None,
        metadata_url="https://idp.example/meta",
        entity_id="idp",
        provider_type="saml",
        enabled=True,
    )
    client = AsyncMock()
    client.get.return_value = MagicMock(text="<metadata/>")
    pacer = AsyncMock()
    pacer.return_value.__aenter__.return_value = client
    with (
        patch("modulo.auth.sso.get_enabled_saml_provider", new_callable=AsyncMock, return_value=db_saml),
        patch("modulo.auth.sso.validate_outbound_url_async", new_callable=AsyncMock),
        patch("modulo.auth.sso.pinned_async_client", pacer),
    ):
        idp_metadata, entity_id, _key, _cert, db = await _resolve_saml_config(None, session, settings)

    assert idp_metadata == "<metadata/>"
    assert entity_id == "idp"
    assert db is db_saml


async def test_resolve_saml_config_missing_metadata() -> None:
    settings = _override()
    session = _mock_session()
    db_saml = SimpleNamespace(
        metadata_xml=None,
        metadata_url=None,
        entity_id="idp",
        provider_type="saml",
        enabled=True,
    )
    with (
        patch("modulo.auth.sso.get_enabled_saml_provider", new_callable=AsyncMock, return_value=db_saml),
        pytest.raises(ValueError, match="missing IdP metadata"),
    ):
        await _resolve_saml_config(None, session, settings)


# ---------------------------------------------------------------------------
# saml_get_auth_url — handler construction error
# ---------------------------------------------------------------------------


async def test_saml_get_auth_url_handler_error() -> None:
    settings = _override()
    session = _mock_session()
    with (
        patch(
            "modulo.auth.sso._resolve_saml_config",
            new_callable=AsyncMock,
            return_value=("metadata", "entity", None, None, None),
        ),
        patch("modulo.auth.sso.ModuloSamlAuth") as mh,
    ):
        mh.return_value.get_auth_url.side_effect = Exception("boom")
        with pytest.raises(ValueError, match="Failed to generate SAML AuthnRequest"):
            await saml_get_auth_url(settings, "http://localhost/acs", session, session)


# ---------------------------------------------------------------------------
# saml_process_response — metadata parse / email / session / jit edges
# ---------------------------------------------------------------------------


_VALID_METADATA = (
    '<md:EntityDescriptor xmlns:md="urn:oasis:names:tc:SAML:2.0:metadata" entityID="idp">'
    "<md:IDPSSODescriptor>"
    '<md:SingleSignOnService Binding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect" '
    'Location="https://idp.example/sso"/>'
    "</md:IDPSSODescriptor></md:EntityDescriptor>"
)


async def test_saml_process_response_metadata_parse_error() -> None:
    settings = _override()
    session = _mock_session()
    with (
        patch(
            "modulo.auth.sso._resolve_saml_config",
            new_callable=AsyncMock,
            return_value=("not-valid-xml", "entity", None, None, None),
        ),
        pytest.raises(ValueError, match="Failed to parse IdP metadata"),
    ):
        await saml_process_response("<resp>", settings, None, session)


async def test_saml_process_response_no_email() -> None:
    settings = _override()
    session = _mock_session()
    handler = MagicMock()
    handler.process_response.return_value = {"name_id": "", "attributes": {}}
    with (
        patch(
            "modulo.auth.sso._resolve_saml_config",
            new_callable=AsyncMock,
            return_value=(_VALID_METADATA, "entity", None, None, None),
        ),
        patch("modulo.auth.sso.ModuloSamlAuth", return_value=handler),
        pytest.raises(ValueError, match="did not return an email"),
    ):
        await saml_process_response("<resp>", settings, None, session)


async def test_saml_process_response_no_app_session() -> None:
    settings = _override()
    handler = MagicMock()
    handler.process_response.return_value = {
        "name_id": "n",
        "attributes": {"email": "u@e.z"},
    }
    with (
        patch(
            "modulo.auth.sso._resolve_saml_config",
            new_callable=AsyncMock,
            return_value=(_VALID_METADATA, "entity", None, None, None),
        ),
        patch("modulo.auth.sso.ModuloSamlAuth", return_value=handler),
        pytest.raises(RuntimeError, match="No app session"),
    ):
        await saml_process_response("<resp>", settings, None, None)


async def test_saml_process_response_jit_runtime_error() -> None:
    settings = _override()
    session = _mock_session()
    handler = MagicMock()
    handler.process_response.return_value = {
        "name_id": "n",
        "attributes": {"email": "u@e.z"},
    }
    with (
        patch(
            "modulo.auth.sso._resolve_saml_config",
            new_callable=AsyncMock,
            return_value=(_VALID_METADATA, "entity", None, None, None),
        ),
        patch("modulo.auth.sso.ModuloSamlAuth", return_value=handler),
        patch(
            "modulo.auth.sso.jit_provision_user",
            new_callable=AsyncMock,
            side_effect=RuntimeError("No organisation exists"),
        ),
        pytest.raises(ValueError, match="No organisation exists"),
    ):
        await saml_process_response("<resp>", settings, None, session)


async def test_saml_process_response_applies_group_mappings() -> None:
    settings = _override()
    session = _mock_session()
    handler = MagicMock()
    handler.process_response.return_value = {
        "name_id": "n",
        "attributes": {"email": "u@e.z", "groups": "g1,g2"},
    }
    db_saml = SimpleNamespace(group_mappings=[{"idp_group": "g1", "team_id": str(uuid.uuid4()), "team_role": "editor"}])
    with (
        patch(
            "modulo.auth.sso._resolve_saml_config",
            new_callable=AsyncMock,
            return_value=(_VALID_METADATA, "entity", None, None, None),
        ),
        patch("modulo.auth.sso.ModuloSamlAuth", return_value=handler),
        patch(
            "modulo.auth.sso.jit_provision_user",
            new_callable=AsyncMock,
            return_value=(MagicMock(), uuid.uuid4(), "viewer"),
        ),
        patch(
            "modulo.auth.sso._lookup_provider_by_entity_id",
            new_callable=AsyncMock,
            return_value=db_saml,
        ),
        patch("modulo.auth.sso.apply_group_mappings", new_callable=AsyncMock) as mock_apply,
        patch("modulo.auth.sso.issue_sso_tokens", new_callable=AsyncMock, return_value={"access_token": "a"}),
    ):
        await saml_process_response("<resp>", settings, None, session)
        mock_apply.assert_awaited_once()


# ---------------------------------------------------------------------------
# _saml_parse_idp_metadata — missing SingleSignOnService
# ---------------------------------------------------------------------------


def test_saml_parse_idp_metadata_no_sso_service() -> None:
    xml = (
        '<md:EntityDescriptor xmlns:md="urn:oasis:names:tc:SAML:2.0:metadata" '
        'entityID="idp"><md:IDPSSODescriptor></md:IDPSSODescriptor></md:EntityDescriptor>'
    )
    with pytest.raises(ValueError, match="No SAML SingleSignOnService"):
        _saml_parse_idp_metadata(xml)


# ---------------------------------------------------------------------------
# _decode_saml_response — invalid base64
# ---------------------------------------------------------------------------


def test_decode_saml_response_invalid_base64() -> None:
    with pytest.raises(ValueError, match="Invalid base64 SAML response"):
        _decode_saml_response("!!!not-base64!!!")


# ---------------------------------------------------------------------------
# _audit_invite_consumed_fail_open — FAR-1825 lock-timeout containment
# ---------------------------------------------------------------------------


class _LockTimeoutDriverError(Exception):
    """Driver-error double carrying an asyncpg ``sqlstate``."""

    def __init__(self, state: str) -> None:
        super().__init__(f"driver error {state}")
        self.sqlstate = state


def _lock_timeout_error() -> OperationalError:
    return OperationalError("SELECT ... FOR UPDATE", {}, _LockTimeoutDriverError("55P03"))


async def test_sso_audit_lock_timeout_is_contained_in_a_savepoint() -> None:
    """A 55P03 from the audit append rolls back ONLY the savepoint.

    ``get_live_for_email`` (C17) is now bounded, so the per-org audit-chain-head
    lock taken by ``append_audit_event`` can raise 55P03 in the same SSO JIT
    transaction. Without a savepoint that error would abort the CALLER's
    transaction, so the swallow below would hide a dead transaction and the
    token-issue writes would fail — contradicting the fail-open contract.
    """
    session = AsyncMock(spec=AsyncSession)
    nested = MagicMock()
    nested.__aenter__ = AsyncMock(return_value=None)
    observed_exception_types: list[type[BaseException] | None] = []

    async def _exit(exc_type: type[BaseException] | None, exc: BaseException | None, tb: object) -> bool:
        observed_exception_types.append(exc_type)
        return False

    nested.__aexit__ = AsyncMock(side_effect=_exit)
    session.begin_nested = MagicMock(return_value=nested)
    audit = AsyncMock(side_effect=_lock_timeout_error())

    with patch("modulo.core.audit_logger.append_audit_event", audit):
        # Fail-open: the helper must NOT re-raise.
        await _audit_invite_consumed_fail_open(session, uuid.uuid4(), uuid.uuid4())

    session.begin_nested.assert_called_once()
    audit.assert_awaited_once()
    # The savepoint observed (and did not suppress) the lock timeout — the
    # mechanism that keeps the outer transaction usable.
    assert observed_exception_types == [OperationalError]


async def test_sso_audit_success_runs_inside_the_savepoint() -> None:
    """The append still happens, inside a savepoint, when the audit succeeds."""
    session = AsyncMock(spec=AsyncSession)
    nested = MagicMock()
    nested.__aenter__ = AsyncMock(return_value=None)
    nested.__aexit__ = AsyncMock(return_value=False)
    session.begin_nested = MagicMock(return_value=nested)
    audit = AsyncMock(return_value=None)

    with patch("modulo.core.audit_logger.append_audit_event", audit):
        await _audit_invite_consumed_fail_open(session, uuid.uuid4(), uuid.uuid4())

    session.begin_nested.assert_called_once()
    audit.assert_awaited_once()
    _, kwargs = audit.await_args
    assert kwargs["event_type"] == "invite_consumed"
    assert kwargs["payload_json"]["via"] == "sso"
