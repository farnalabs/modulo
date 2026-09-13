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

import os
import shlex
import subprocess
import tempfile
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cryptography.fernet import Fernet

from modulo.core.pipeline_engine.workspace_input_credentials import (
    CloneCredential,
    CredentialResolutionError,
    _decrypt_connector_creds,
    assert_clone_credential_is_read_only,
    build_provisioning_credential_scripts,
    is_forge_allowlisted,
    resolve_clone_credential,
)

# A valid Fernet key for the ciphertext-fallback decode path.
_FERNET_KEY = Fernet.generate_key().decode("utf-8")


def _encrypt(value: str) -> bytes:
    """Encrypt *value* with the test Fernet key (bytes, as stored on a connector)."""
    return Fernet(_FERNET_KEY.encode()).encrypt(value.encode("utf-8"))


def _make_connector_instance(*, connector_type_id: str = "github", ciphertext: bytes | None = None) -> MagicMock:
    """Build a fake ConnectorInstance with an optional credentials_ciphertext fallback."""
    ci = MagicMock()
    ci.id = uuid.uuid4()
    ci.connector_type_id = connector_type_id
    ci.credentials_ciphertext = ciphertext
    return ci


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

    @pytest.mark.asyncio
    async def test_github_empty_scopes_refused(self) -> None:
        # GitHub fine-grained PATs return an EMPTY X-OAuth-Scopes header, which
        # is indistinguishable from "read-only".  We fail closed rather than
        # let a write-capable fine-grained PAT pass the probe.
        cred = _make_cred(host="github.com")
        mock_client = AsyncMock()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {"x-oauth-scopes": ""}
        mock_client.get.return_value = mock_response

        with pytest.raises(CredentialResolutionError, match="no X-OAuth-Scopes"):
            await assert_clone_credential_is_read_only(cred, http_client=mock_client)

    @pytest.mark.asyncio
    async def test_github_probe_network_error_refused(self) -> None:
        # Any exception during the capability probe must fail closed (not leak
        # the raw exception), covering the generic except branch.
        cred = _make_cred(host="github.com")
        mock_client = AsyncMock()
        mock_client.get.side_effect = RuntimeError("connection reset")

        with pytest.raises(CredentialResolutionError, match="Failed to probe GitHub token capability"):
            await assert_clone_credential_is_read_only(cred, http_client=mock_client)


# ---------------------------------------------------------------------------
# _decrypt_connector_creds — secrets-backend + ciphertext fallback
# ---------------------------------------------------------------------------


class TestDecryptConnectorCreds:
    @pytest.mark.asyncio
    async def test_secrets_backend_keyerror_falls_back_to_ciphertext(self) -> None:
        session = AsyncMock()
        ci = _make_connector_instance(ciphertext=_encrypt('{"token": "sk-fallback"}'))
        mock_settings = MagicMock()
        mock_settings.fernet_key = _FERNET_KEY
        mock_backend = AsyncMock()
        mock_backend.get_secret.side_effect = KeyError

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
            creds = await _decrypt_connector_creds(ci, session=session)

        assert creds == {"token": "sk-fallback"}

    @pytest.mark.asyncio
    async def test_secrets_backend_generic_error_falls_back_to_ciphertext(self) -> None:
        session = AsyncMock()
        ci = _make_connector_instance(ciphertext=_encrypt('{"token": "sk-fallback"}'))
        mock_settings = MagicMock()
        mock_settings.fernet_key = _FERNET_KEY
        mock_backend = AsyncMock()
        mock_backend.get_secret.side_effect = RuntimeError("backend down")

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
            creds = await _decrypt_connector_creds(ci, session=session)

        assert creds == {"token": "sk-fallback"}

    @pytest.mark.asyncio
    async def test_secrets_backend_returns_non_dict_raises(self) -> None:
        session = AsyncMock()
        ci = _make_connector_instance()
        mock_settings = MagicMock()
        mock_settings.fernet_key = _FERNET_KEY
        mock_backend = AsyncMock()
        mock_backend.get_secret.return_value = "[1, 2, 3]"

        with (
            patch(
                "modulo.core.pipeline_engine.workspace_input_credentials._get_settings",
                return_value=mock_settings,
            ),
            patch(
                "modulo.core.pipeline_engine.workspace_input_credentials._create_secrets_backend",
                return_value=mock_backend,
            ),
            pytest.raises(CredentialResolutionError, match="not a JSON dict"),
        ):
            await _decrypt_connector_creds(ci, session=session)

    @pytest.mark.asyncio
    async def test_secrets_backend_returns_invalid_json_raises(self) -> None:
        session = AsyncMock()
        ci = _make_connector_instance()
        mock_settings = MagicMock()
        mock_settings.fernet_key = _FERNET_KEY
        mock_backend = AsyncMock()
        mock_backend.get_secret.return_value = "not-json"

        with (
            patch(
                "modulo.core.pipeline_engine.workspace_input_credentials._get_settings",
                return_value=mock_settings,
            ),
            patch(
                "modulo.core.pipeline_engine.workspace_input_credentials._create_secrets_backend",
                return_value=mock_backend,
            ),
            pytest.raises(CredentialResolutionError, match="not valid JSON"),
        ):
            await _decrypt_connector_creds(ci, session=session)

    @pytest.mark.asyncio
    async def test_secrets_backend_raises_credential_error_reraised(self) -> None:
        session = AsyncMock()
        ci = _make_connector_instance()
        mock_settings = MagicMock()
        mock_settings.fernet_key = _FERNET_KEY
        mock_backend = AsyncMock()
        mock_backend.get_secret.side_effect = CredentialResolutionError("upstream boom")

        with (
            patch(
                "modulo.core.pipeline_engine.workspace_input_credentials._get_settings",
                return_value=mock_settings,
            ),
            patch(
                "modulo.core.pipeline_engine.workspace_input_credentials._create_secrets_backend",
                return_value=mock_backend,
            ),
            pytest.raises(CredentialResolutionError, match="upstream boom"),
        ):
            await _decrypt_connector_creds(ci, session=session)

    @pytest.mark.asyncio
    async def test_ciphertext_fallback_missing_raises(self) -> None:
        session = AsyncMock()
        ci = _make_connector_instance(ciphertext=None)
        mock_settings = MagicMock()
        mock_settings.fernet_key = _FERNET_KEY
        mock_backend = AsyncMock()
        mock_backend.get_secret.side_effect = KeyError

        with (
            patch(
                "modulo.core.pipeline_engine.workspace_input_credentials._get_settings",
                return_value=mock_settings,
            ),
            patch(
                "modulo.core.pipeline_engine.workspace_input_credentials._create_secrets_backend",
                return_value=mock_backend,
            ),
            pytest.raises(CredentialResolutionError, match="has no credentials"),
        ):
            await _decrypt_connector_creds(ci, session=session)

    @pytest.mark.asyncio
    async def test_ciphertext_fallback_empty_bytes_raises(self) -> None:
        session = AsyncMock()
        ci = _make_connector_instance(ciphertext=b"")
        mock_settings = MagicMock()
        mock_settings.fernet_key = _FERNET_KEY
        mock_backend = AsyncMock()
        mock_backend.get_secret.side_effect = KeyError

        with (
            patch(
                "modulo.core.pipeline_engine.workspace_input_credentials._get_settings",
                return_value=mock_settings,
            ),
            patch(
                "modulo.core.pipeline_engine.workspace_input_credentials._create_secrets_backend",
                return_value=mock_backend,
            ),
            pytest.raises(CredentialResolutionError, match="has no credentials"),
        ):
            await _decrypt_connector_creds(ci, session=session)

    @pytest.mark.asyncio
    async def test_ciphertext_fallback_decrypt_failure_raises(self) -> None:
        session = AsyncMock()
        ci = _make_connector_instance(ciphertext=b"not-a-valid-fernet-token")
        mock_settings = MagicMock()
        mock_settings.fernet_key = _FERNET_KEY
        mock_backend = AsyncMock()
        mock_backend.get_secret.side_effect = KeyError

        with (
            patch(
                "modulo.core.pipeline_engine.workspace_input_credentials._get_settings",
                return_value=mock_settings,
            ),
            patch(
                "modulo.core.pipeline_engine.workspace_input_credentials._create_secrets_backend",
                return_value=mock_backend,
            ),
            pytest.raises(CredentialResolutionError, match="Failed to decrypt credentials"),
        ):
            await _decrypt_connector_creds(ci, session=session)

    @pytest.mark.asyncio
    async def test_ciphertext_fallback_invalid_json_raises(self) -> None:
        session = AsyncMock()
        ci = _make_connector_instance(ciphertext=_encrypt("not-json-either"))
        mock_settings = MagicMock()
        mock_settings.fernet_key = _FERNET_KEY
        mock_backend = AsyncMock()
        mock_backend.get_secret.side_effect = KeyError

        with (
            patch(
                "modulo.core.pipeline_engine.workspace_input_credentials._get_settings",
                return_value=mock_settings,
            ),
            patch(
                "modulo.core.pipeline_engine.workspace_input_credentials._create_secrets_backend",
                return_value=mock_backend,
            ),
            pytest.raises(CredentialResolutionError, match="decrypted credentials are not valid JSON"),
        ):
            await _decrypt_connector_creds(ci, session=session)

    @pytest.mark.asyncio
    async def test_ciphertext_fallback_scalar_wrapped_as_token(self) -> None:
        session = AsyncMock()
        ci = _make_connector_instance(ciphertext=_encrypt('"bare-secret-value"'))
        mock_settings = MagicMock()
        mock_settings.fernet_key = _FERNET_KEY
        mock_backend = AsyncMock()
        mock_backend.get_secret.side_effect = KeyError

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
            creds = await _decrypt_connector_creds(ci, session=session)

        assert creds == {"token": "bare-secret-value"}


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
        # Teardown recovers the ephemeral paths from the state file and
        # removes each (the original code referenced vanished shell vars).
        assert "_modulo_state_file" in teardown
        assert "while IFS= read -r _modulo_path" in teardown
        assert 'rm -f "$_modulo_path"' in teardown

    def test_teardown_asserts_absence(self) -> None:
        _, teardown = build_provisioning_credential_scripts(cred=_make_cred(), host="github.com")
        assert 'test ! -e "$_modulo_path"' in teardown

    def test_scripts_are_set_e(self) -> None:
        cred = _make_cred()
        setup, teardown = build_provisioning_credential_scripts(cred=cred, host="github.com")
        assert "set -e" in setup
        assert "set -e" in teardown

    def test_adversarial_host_is_not_injected(self) -> None:
        # The host is no longer interpolated into the generated script (the
        # askpass-only flow is host-agnostic), so an adversarial host must
        # never appear in a form that could inject shell commands.
        adversarial_host = "github.com; rm -rf /"
        cred = _make_cred()
        setup, _ = build_provisioning_credential_scripts(cred=cred, host=adversarial_host)
        assert adversarial_host not in setup

    def test_adversarial_username_is_shlex_quoted(self) -> None:
        # The only user-controlled value still interpolated into the script
        # is the git username; it must be shlex-quoted so it cannot inject.
        adversarial_username = "x-access-token; rm -rf /"
        cred = _make_cred(username=adversarial_username)
        setup, _ = build_provisioning_credential_scripts(cred=cred, host="github.com")
        quoted = shlex.quote(adversarial_username)
        assert quoted in setup
        # The raw (unquoted) form must never appear outside the quotes — if it
        # did, the semicolons would be interpreted as separate commands.
        stripped = setup.replace(quoted, "")
        assert adversarial_username not in stripped

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

    def _write_tmp_script(self, text: str) -> Path:
        path = Path(tempfile.gettempdir()) / f"modulo-cred-test-{uuid.uuid4().hex}.sh"
        path.write_text(text)
        path.chmod(0o700)
        return path

    def test_scripts_are_valid_posix(self) -> None:
        """The generated scripts must parse as POSIX sh (catches the original
        unterminated-quote / broken-pipe regressions)."""
        cred = _make_cred()
        setup, teardown = build_provisioning_credential_scripts(cred=cred, host="github.com")
        setup_path = self._write_tmp_script(setup)
        teardown_path = self._write_tmp_script(teardown)
        try:
            assert (
                subprocess.run(  # noqa: S603
                    ["sh", "-n", str(setup_path)],  # noqa: S607
                    capture_output=True,
                    check=False,
                    timeout=30,
                ).returncode
                == 0
            )
            assert (
                subprocess.run(  # noqa: S603
                    ["sh", "-n", str(teardown_path)],  # noqa: S607
                    capture_output=True,
                    check=False,
                    timeout=30,
                ).returncode
                == 0
            )
        finally:
            setup_path.unlink(missing_ok=True)
            teardown_path.unlink(missing_ok=True)

    def test_setup_executes_and_askpass_returns_secret(self) -> None:
        """End-to-end: run the setup script, confirm the askpass helper yields
        the username and password (via the exported credential-file path), then
        run teardown and confirm the ephemeral files are gone.
        """
        secret = _ADVERSARIAL_SECRET
        cred = _make_cred(secret=secret)
        setup, teardown = build_provisioning_credential_scripts(cred=cred, host="github.com")
        setup_path = self._write_tmp_script(setup)
        teardown_path = self._write_tmp_script(teardown)
        env = dict(os.environ)
        env["HOME"] = tempfile.mkdtemp(prefix="modulo-home-")
        try:
            run = subprocess.run(  # noqa: S603
                ["sh", str(setup_path)],  # noqa: S607
                capture_output=True,
                text=True,
                env=env,
                check=False,
                timeout=30,
            )
            assert run.returncode == 0, run.stderr

            # Locate the recorded state file to find the askpass + cred paths.
            state_files = [p for p in Path(tempfile.gettempdir()).iterdir() if p.name.startswith(".modulo-cred-state-")]
            assert state_files, "setup did not record a state file"
            cred_file, askpass_file = [ln.strip() for ln in state_files[0].read_text().splitlines() if ln.strip()]

            assert Path(cred_file).exists()
            # The quoted heredoc writes the secret followed by a single
            # trailing newline (the delimiter is on its own line).
            assert Path(cred_file).read_text() == secret + "\n"

            # In production git invokes GIT_ASKPASS as a child of the setup
            # shell, which exported _modulo_cred_file into its environment.
            # Simulate that by passing the credential-file path to the helper.
            env["_modulo_cred_file"] = cred_file

            # GIT_ASKPASS helper: Username then Password.
            username_out = subprocess.run(  # noqa: S603
                [str(askpass_file), "Username"], capture_output=True, text=True, env=env, check=False, timeout=30
            )
            assert username_out.stdout.strip() == "x-access-token"
            password_out = subprocess.run(  # noqa: S603
                [str(askpass_file), "Password"], capture_output=True, text=True, env=env, check=False, timeout=30
            )
            # GIT_ASKPASS emits the credential-file contents; git strips the
            # trailing newline, so the effective password is the bare secret.
            assert password_out.stdout.rstrip("\n") == secret

            # Teardown must remove the cred file and askpass helper.
            tear = subprocess.run(  # noqa: S603
                ["sh", str(teardown_path)],  # noqa: S607
                capture_output=True,
                text=True,
                env=env,
                check=False,
                timeout=30,
            )
            assert tear.returncode == 0, tear.stderr
            assert not Path(cred_file).exists()
            assert not Path(askpass_file).exists()
            assert not state_files[0].exists()
        finally:
            setup_path.unlink(missing_ok=True)
            teardown_path.unlink(missing_ok=True)
            # Best-effort cleanup of any leftover state files from this run.
            for f in Path(tempfile.gettempdir()).iterdir():
                if f.name.startswith(".modulo-cred-state-"):
                    f.unlink(missing_ok=True)
