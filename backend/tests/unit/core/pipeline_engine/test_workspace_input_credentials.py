"""Tests for managed workspace input credentials (FAR-797).

Covers:
* CloneCredential repr/str secret redaction.
* resolve_clone_credential: null connector → None, unknown connector → error.
* assert_clone_credential_is_read_only: push-capable → refused, read-only → ok.
* build_provisioning_credential_scripts: chmod 600, mktemp, no secret in argv,
  teardown removes + asserts absence, shlex.quote on adversarial host.
* is_forge_allowlisted: true/false cases.
"""

from __future__ import annotations

import shlex
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core.pipeline_engine.workspace_input_credentials import (
    CloneCredential,
    CredentialResolutionError,
    assert_clone_credential_is_read_only,
    build_provisioning_credential_scripts,
    is_forge_allowlisted,
    resolve_clone_credential,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ADVERSARIAL_SECRET = "ghp_ABCdef123!@#$%^&*()_+={}[]|\\:'\";<>?,./~`"


def _make_cred(
    *,
    kind: str = "token",
    host: str = "github.com",
    username: str | None = "x-access-token",
    secret: str = _ADVERSARIAL_SECRET,
) -> CloneCredential:
    return CloneCredential(kind=kind, host=host, username=username, secret=secret)


# ---------------------------------------------------------------------------
# CloneCredential repr/str — secret redaction
# ---------------------------------------------------------------------------


class TestCloneCredentialRedaction:
    def test_repr_hides_secret(self) -> None:
        cred = _make_cred()
        r = repr(cred)
        assert "REDACTED" in r
        assert _ADVERSARIAL_SECRET not in r

    def test_str_matches_repr(self) -> None:
        cred = _make_cred()
        assert str(cred) == repr(cred)

    def test_repr_contains_kind_and_host(self) -> None:
        cred = _make_cred(kind="ssh", host="gitlab.com")
        assert "kind='ssh'" in repr(cred)
        assert "host='gitlab.com'" in repr(cred)

    def test_frozen(self) -> None:
        cred = _make_cred()
        with pytest.raises(AttributeError):
            cred.host = "evil.com"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# is_forge_allowlisted
# ---------------------------------------------------------------------------


class TestForgeAllowlist:
    def test_github_com(self) -> None:
        assert is_forge_allowlisted("github.com") is True

    def test_gitlab_com(self) -> None:
        assert is_forge_allowlisted("gitlab.com") is True

    def test_bitbucket_org(self) -> None:
        assert is_forge_allowlisted("bitbucket.org") is True

    def test_codeberg_org(self) -> None:
        assert is_forge_allowlisted("codeberg.org") is True

    def test_unknown_host(self) -> None:
        assert is_forge_allowlisted("evil.example.com") is False

    def test_empty_string(self) -> None:
        assert is_forge_allowlisted("") is False


# ---------------------------------------------------------------------------
# resolve_clone_credential
# ---------------------------------------------------------------------------


class TestResolveCloneCredential:
    @pytest.mark.asyncio
    async def test_null_connector_returns_none(self) -> None:
        session = AsyncMock()
        result = await resolve_clone_credential(session, connector_instance_id=None, host="github.com")
        assert result is None

    @pytest.mark.asyncio
    async def test_unknown_connector_raises(self) -> None:
        session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        session.execute.return_value = mock_result

        fake_id = uuid.uuid4()
        with pytest.raises(CredentialResolutionError, match="not found"):
            await resolve_clone_credential(session, connector_instance_id=fake_id, host="github.com")

    @pytest.mark.asyncio
    async def test_unsupported_connector_type_raises(self) -> None:
        session = AsyncMock()
        ci = MagicMock()
        ci.id = uuid.uuid4()
        ci.connector_type_id = "jira"  # not in _TOKEN_CREDENTIAL_TYPES

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = ci
        session.execute.return_value = mock_result

        with pytest.raises(CredentialResolutionError, match="does not support"):
            await resolve_clone_credential(session, connector_instance_id=ci.id, host="github.com")

    @pytest.mark.asyncio
    async def test_github_connector_resolves_token(self) -> None:
        session = AsyncMock()
        ci = MagicMock()
        ci.id = uuid.uuid4()
        ci.connector_type_id = "github"

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = ci
        session.execute.return_value = mock_result

        fake_token = "ghp_test123abc"
        mock_settings = MagicMock()
        mock_settings.fernet_key = "dGVzdA=="
        mock_backend = AsyncMock()
        mock_backend.get_secret.return_value = f'{{"token": "{fake_token}"}}'

        with (
            patch(
                "modulo.core.pipeline_engine.workspace_input_credentials._get_settings",
                return_value=mock_settings,
            ),
            patch(
                "modulo.core.pipeline_engine.workspace_input_credentials._create_secrets_backend",
                return_value=mock_backend,
            ),
        ):
            cred = await resolve_clone_credential(session, connector_instance_id=ci.id, host="github.com")

        assert cred is not None
        assert cred.kind == "token"
        assert cred.host == "github.com"
        assert cred.username == "x-access-token"
        assert cred.secret == fake_token

    @pytest.mark.asyncio
    async def test_missing_token_key_raises(self) -> None:
        session = AsyncMock()
        ci = MagicMock()
        ci.id = uuid.uuid4()
        ci.connector_type_id = "github"

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = ci
        session.execute.return_value = mock_result

        mock_settings = MagicMock()
        mock_settings.fernet_key = "dGVzdA=="
        mock_backend = AsyncMock()
        mock_backend.get_secret.return_value = '{"api_key": "wrong_key"}'

        with (
            patch(
                "modulo.core.pipeline_engine.workspace_input_credentials._get_settings",
                return_value=mock_settings,
            ),
            patch(
                "modulo.core.pipeline_engine.workspace_input_credentials._create_secrets_backend",
                return_value=mock_backend,
            ),
            pytest.raises(CredentialResolutionError, match="no 'token'"),
        ):
            await resolve_clone_credential(session, connector_instance_id=ci.id, host="github.com")

    @pytest.mark.asyncio
    async def test_gitlab_connector_resolves_token(self) -> None:
        session = AsyncMock()
        ci = MagicMock()
        ci.id = uuid.uuid4()
        ci.connector_type_id = "gitlab"

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = ci
        session.execute.return_value = mock_result

        fake_token = "glpat-xyz789"
        mock_settings = MagicMock()
        mock_settings.fernet_key = "dGVzdA=="
        mock_backend = AsyncMock()
        mock_backend.get_secret.return_value = f'{{"token": "{fake_token}"}}'

        with (
            patch(
                "modulo.core.pipeline_engine.workspace_input_credentials._get_settings",
                return_value=mock_settings,
            ),
            patch(
                "modulo.core.pipeline_engine.workspace_input_credentials._create_secrets_backend",
                return_value=mock_backend,
            ),
        ):
            cred = await resolve_clone_credential(session, connector_instance_id=ci.id, host="gitlab.com")

        assert cred is not None
        assert cred.kind == "token"
        assert cred.username == "oauth2"


# ---------------------------------------------------------------------------
# assert_clone_credential_is_read_only
# ---------------------------------------------------------------------------


class TestAssertReadOnly:
    @pytest.mark.asyncio
    async def test_ssh_kind_passes(self) -> None:
        cred = _make_cred(kind="ssh")
        await assert_clone_credential_is_read_only(cred)

    @pytest.mark.asyncio
    async def test_unprobed_host_raises(self) -> None:
        cred = _make_cred(host="gitlab.com")
        with pytest.raises(CredentialResolutionError, match="no API probe"):
            await assert_clone_credential_is_read_only(cred)

    @pytest.mark.asyncio
    async def test_github_readonly_token_passes(self) -> None:
        cred = _make_cred(host="github.com")
        mock_client = AsyncMock()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {"x-oauth-scopes": "read:user, read:repo"}
        mock_client.get.return_value = mock_response

        await assert_clone_credential_is_read_only(cred, http_client=mock_client)

    @pytest.mark.asyncio
    async def test_github_push_token_refused(self) -> None:
        cred = _make_cred(host="github.com")
        mock_client = AsyncMock()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {"x-oauth-scopes": "repo, read:user"}
        mock_client.get.return_value = mock_response

        with pytest.raises(CredentialResolutionError, match="push-capable"):
            await assert_clone_credential_is_read_only(cred, http_client=mock_client)

    @pytest.mark.asyncio
    async def test_github_invalid_token_refused(self) -> None:
        cred = _make_cred(host="github.com")
        mock_client = AsyncMock()
        mock_response = MagicMock()
        mock_response.status_code = 401
        mock_response.headers = {}
        mock_client.get.return_value = mock_response

        with pytest.raises(CredentialResolutionError, match="invalid"):
            await assert_clone_credential_is_read_only(cred, http_client=mock_client)

    @pytest.mark.asyncio
    async def test_github_server_error_refused(self) -> None:
        cred = _make_cred(host="github.com")
        mock_client = AsyncMock()
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.headers = {}
        mock_client.get.return_value = mock_response

        with pytest.raises(CredentialResolutionError, match="500"):
            await assert_clone_credential_is_read_only(cred, http_client=mock_client)

    @pytest.mark.asyncio
    async def test_github_no_client_raises(self) -> None:
        cred = _make_cred(host="github.com")
        with pytest.raises(CredentialResolutionError, match="http_client is required"):
            await assert_clone_credential_is_read_only(cred, http_client=None)


# ---------------------------------------------------------------------------
# build_provisioning_credential_scripts
# ---------------------------------------------------------------------------


class TestBuildProvisioningScripts:
    def test_null_cred_returns_empty(self) -> None:
        setup, teardown = build_provisioning_credential_scripts(cred=None, host="github.com")
        assert setup == ""
        assert teardown == ""

    def test_setup_contains_chmod_600(self) -> None:
        cred = _make_cred()
        setup, _ = build_provisioning_credential_scripts(cred=cred, host="github.com")
        assert "chmod 600" in setup

    def test_setup_contains_mktemp_dev_shm(self) -> None:
        cred = _make_cred()
        setup, _ = build_provisioning_credential_scripts(cred=cred, host="github.com")
        assert "mktemp /dev/shm/.modulo-cred" in setup

    def test_secret_not_in_setup_argv(self) -> None:
        """The secret must never appear as a command-line argument.

        It IS present in the heredoc body (script text), but not as an
        argument to any command.  We verify that every non-heredoc line
        does not contain the secret.
        """
        cred = _make_cred()
        setup, _ = build_provisioning_credential_scripts(cred=cred, host="github.com")
        in_heredoc = False
        for line in setup.splitlines():
            if "_CRED_EOF_" in line:
                in_heredoc = not in_heredoc
                continue
            if in_heredoc:
                continue
            assert _ADVERSARIAL_SECRET not in line

    def test_secret_embedded_in_heredoc(self) -> None:
        """The secret IS in the generated script text (inside a heredoc)."""
        cred = _make_cred()
        setup, _ = build_provisioning_credential_scripts(cred=cred, host="github.com")
        assert _ADVERSARIAL_SECRET in setup

    def test_teardown_removes_files(self) -> None:
        _, teardown = build_provisioning_credential_scripts(cred=_make_cred(), host="github.com")
        assert 'rm -f "$_modulo_cred_file"' in teardown
        assert 'rm -f "$_modulo_askpass"' in teardown

    def test_teardown_asserts_absence(self) -> None:
        _, teardown = build_provisioning_credential_scripts(cred=_make_cred(), host="github.com")
        assert 'test ! -e "$_modulo_cred_file"' in teardown
        assert 'test ! -e "$_modulo_askpass"' in teardown

    def test_scripts_are_set_e(self) -> None:
        cred = _make_cred()
        setup, teardown = build_provisioning_credential_scripts(cred=cred, host="github.com")
        assert "set -e" in setup
        assert "set -e" in teardown

    def test_adversarial_host_is_shlex_quoted(self) -> None:
        adversarial_host = "github.com; rm -rf /"
        cred = _make_cred()
        setup, _ = build_provisioning_credential_scripts(cred=cred, host=adversarial_host)
        quoted = shlex.quote(adversarial_host)
        # The shlex-quoted version should appear in the script.
        assert quoted in setup
        # Verify the echo line uses single-quoted host (shlex.quote wraps
        # in single quotes) so the semicolons are literal, not interpreted.
        echo_lines = [ln for ln in setup.splitlines() if "echo" in ln and "host=" in ln]
        assert len(echo_lines) > 0
        for line in echo_lines:
            # The quoted form protects the semicolons — verify it's there.
            assert quoted in line

    def test_username_in_setup(self) -> None:
        cred = _make_cred(username="x-access-token")
        setup, _ = build_provisioning_credential_scripts(cred=cred, host="github.com")
        assert "x-access-token" in setup

    def test_git_askpass_configured(self) -> None:
        cred = _make_cred()
        setup, _ = build_provisioning_credential_scripts(cred=cred, host="github.com")
        assert "GIT_ASKPASS" in setup

    def test_setup_has_shebang(self) -> None:
        cred = _make_cred()
        setup, _ = build_provisioning_credential_scripts(cred=cred, host="github.com")
        assert setup.startswith("#!/bin/sh\n")

    def test_teardown_has_shebang(self) -> None:
        _, teardown = build_provisioning_credential_scripts(cred=_make_cred(), host="github.com")
        assert teardown.startswith("#!/bin/sh\n")
