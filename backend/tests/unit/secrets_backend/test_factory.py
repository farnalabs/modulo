"""Tests for create_secrets_backend factory."""

from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet

from modulo.core.license import LicenseData, LicenseValidation
from modulo.core.secrets_backend import _check_external_secrets_licensed, create_secrets_backend, validate_key
from modulo.core.secrets_backend.fernet import FernetSecretsBackend

_KEY = Fernet.generate_key().decode()


@pytest.mark.parametrize("backend_name", [None, "fernet", "  Fernet  ", "FERNET"])
def test_fernet_backend_created(backend_name):
    backend = create_secrets_backend(fernet_key=_KEY, backend_name=backend_name)
    assert isinstance(backend, FernetSecretsBackend)


def test_vault_backend_created_by_name(monkeypatch: pytest.MonkeyPatch):
    from modulo.core.secrets_backend.vault import VaultSecretsBackend

    monkeypatch.setenv("VAULT_ADDR", "http://vault:8200")
    monkeypatch.setenv("VAULT_TOKEN", "x")
    with (
        patch("modulo.core.secrets_backend._check_external_secrets_licensed", return_value=True),
        patch("modulo.core.secrets_backend.vault._MODULE_AVAILABLE", True),
        patch("modulo.core.secrets_backend.vault._hvac"),
    ):
        backend = create_secrets_backend(fernet_key=_KEY, backend_name="vault")
        assert isinstance(backend, VaultSecretsBackend)


def test_aws_backend_created_by_name(monkeypatch: pytest.MonkeyPatch):
    from modulo.core.secrets_backend.aws import AWSSecretsManagerBackend

    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "x")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "y")
    with (
        patch("modulo.core.secrets_backend._check_external_secrets_licensed", return_value=True),
        patch("modulo.core.secrets_backend.aws._MODULE_AVAILABLE", True),
        patch("modulo.core.secrets_backend.aws._boto3"),
    ):
        backend = create_secrets_backend(fernet_key=_KEY, backend_name="aws")
        assert isinstance(backend, AWSSecretsManagerBackend)


def test_unknown_backend_raises_value_error():
    with pytest.raises(ValueError, match="Unknown"):
        create_secrets_backend(fernet_key=_KEY, backend_name="nonexistent")


@pytest.mark.parametrize("backend_name", ["vault", "aws"])
def test_configured_external_backend_is_not_downgraded_when_unlicensed(
    monkeypatch: pytest.MonkeyPatch, backend_name: str
) -> None:
    """FAR-2059: a licence lapse must not switch a configured backend to fernet.

    The entitlement gates CHANGING the configuration, never the runtime
    resolution path. ``_check_external_secrets_licensed`` is patched to False
    (the lapsed state) to prove the factory ignores it for construction.
    """
    if backend_name == "vault":
        from modulo.core.secrets_backend.vault import VaultSecretsBackend

        monkeypatch.setenv("VAULT_ADDR", "http://vault:8200")
        monkeypatch.setenv("VAULT_TOKEN", "x")
        with (
            patch("modulo.core.secrets_backend._check_external_secrets_licensed", return_value=False),
            patch("modulo.core.secrets_backend.vault._MODULE_AVAILABLE", True),
            patch("modulo.core.secrets_backend.vault._hvac"),
        ):
            backend = create_secrets_backend(fernet_key=_KEY, backend_name=backend_name)
        assert isinstance(backend, VaultSecretsBackend)
        return

    from modulo.core.secrets_backend.aws import AWSSecretsManagerBackend

    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "x")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "y")
    with (
        patch("modulo.core.secrets_backend._check_external_secrets_licensed", return_value=False),
        patch("modulo.core.secrets_backend.aws._MODULE_AVAILABLE", True),
        patch("modulo.core.secrets_backend.aws._boto3"),
    ):
        backend = create_secrets_backend(fernet_key=_KEY, backend_name=backend_name)
    assert isinstance(backend, AWSSecretsManagerBackend)


@pytest.mark.parametrize("backend_name", ["vault", "aws"])
def test_external_backend_does_not_require_a_fernet_key(monkeypatch: pytest.MonkeyPatch, backend_name: str) -> None:
    """No fernet fallback exists any more, so the external path needs no fernet key."""
    if backend_name == "vault":
        from modulo.core.secrets_backend.vault import VaultSecretsBackend

        monkeypatch.setenv("VAULT_ADDR", "http://vault:8200")
        monkeypatch.setenv("VAULT_TOKEN", "x")
        with (
            patch("modulo.core.secrets_backend._check_external_secrets_licensed", return_value=False),
            patch("modulo.core.secrets_backend.vault._MODULE_AVAILABLE", True),
            patch("modulo.core.secrets_backend.vault._hvac"),
        ):
            backend = create_secrets_backend(fernet_key=None, backend_name=backend_name)
        assert isinstance(backend, VaultSecretsBackend)
        return

    from modulo.core.secrets_backend.aws import AWSSecretsManagerBackend

    monkeypatch.setenv("AWS_REGION", "us-east-1")
    with (
        patch("modulo.core.secrets_backend._check_external_secrets_licensed", return_value=False),
        patch("modulo.core.secrets_backend.aws._MODULE_AVAILABLE", True),
        patch("modulo.core.secrets_backend.aws._boto3"),
    ):
        backend = create_secrets_backend(fernet_key=None, backend_name=backend_name)
    assert isinstance(backend, AWSSecretsManagerBackend)


def test_unconfigured_vault_backend_fails_hard_not_silently(monkeypatch: pytest.MonkeyPatch) -> None:
    """A genuinely unconfigured external backend fails in its constructor.

    The factory warns about the missing configuration but never substitutes a
    different backend: a silent switch is exactly the failure this ticket
    removes.
    """
    monkeypatch.delenv("VAULT_ADDR", raising=False)
    with (
        patch("modulo.core.secrets_backend.vault._MODULE_AVAILABLE", True),
        patch("modulo.core.secrets_backend.vault._hvac"),
        pytest.raises(ValueError, match="VAULT_ADDR is not set"),
    ):
        create_secrets_backend(fernet_key=_KEY, backend_name="vault")


def _team_license() -> LicenseData:
    return LicenseData(
        tier="team",
        features=[],
        expires_at="",
        org_id="",
        raw_payload={},
        raw_key="",
    )


def test_external_secrets_licensed_when_license_file_present(monkeypatch: pytest.MonkeyPatch):
    """A stored team-tier license unlocks the external_secrets flag."""
    monkeypatch.delenv("MODULO_LICENSE_KEY", raising=False)
    with patch("modulo.core.license.get_license", return_value=_team_license()):
        assert _check_external_secrets_licensed() is True


def test_external_secrets_not_licensed_when_community(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("MODULO_LICENSE_KEY", raising=False)
    with patch("modulo.core.license.get_license", return_value=None):
        assert _check_external_secrets_licensed() is False


def test_external_secrets_licensed_via_env_key(monkeypatch: pytest.MonkeyPatch):
    """With no stored license, a valid MODULO_LICENSE_KEY enables the flag."""
    validation = LicenseValidation(valid=True, license_data=_team_license())
    monkeypatch.setenv("MODULO_LICENSE_KEY", "some-key")
    with (
        patch("modulo.core.license.get_license", return_value=None),
        patch("modulo.core.license.parse_and_verify", return_value=validation),
    ):
        assert _check_external_secrets_licensed() is True


def test_external_secrets_not_licensed_with_invalid_env_key(monkeypatch: pytest.MonkeyPatch):
    invalid = LicenseValidation(valid=False, error="Signature verification failed")
    monkeypatch.setenv("MODULO_LICENSE_KEY", "bad-key")
    with (
        patch("modulo.core.license.get_license", return_value=None),
        patch("modulo.core.license.parse_and_verify", return_value=invalid),
    ):
        assert _check_external_secrets_licensed() is False


def test_external_secrets_license_valid_without_data_is_not_enabled(monkeypatch: pytest.MonkeyPatch):
    """A valid signature that carries no license_data must NOT unlock external secrets."""
    validation = LicenseValidation(valid=True, license_data=None)
    monkeypatch.setenv("MODULO_LICENSE_KEY", "some-key")
    with (
        patch("modulo.core.license.get_license", return_value=None),
        patch("modulo.core.license.parse_and_verify", return_value=validation),
    ):
        assert _check_external_secrets_licensed() is False


def test_env_var_used_when_no_backend_name(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MODULO_SECRETS_BACKEND", "fernet")
    backend = create_secrets_backend(fernet_key=_KEY)
    assert isinstance(backend, FernetSecretsBackend)


@pytest.mark.parametrize(
    ("name", "source"),
    [
        pytest.param("  Vault  ", "arg", id="backend_name-whitespace-case"),
        pytest.param("  vault  ", "arg", id="backend_name-whitespace"),
        pytest.param("  VAULT  ", "env", id="env_var-whitespace-case"),
        pytest.param("vault", "env", id="env_var-plain"),
    ],
)
def test_backend_name_normalized(monkeypatch: pytest.MonkeyPatch, name: str, source: str):
    """Both ``backend_name`` and ``MODULO_SECRETS_BACKEND`` are lowercased/stripped before matching."""
    from modulo.core.secrets_backend.vault import VaultSecretsBackend

    monkeypatch.setenv("VAULT_ADDR", "http://vault:8200")
    monkeypatch.setenv("VAULT_TOKEN", "x")
    with (
        patch("modulo.core.secrets_backend._check_external_secrets_licensed", return_value=True),
        patch("modulo.core.secrets_backend.vault._MODULE_AVAILABLE", True),
        patch("modulo.core.secrets_backend.vault._hvac"),
    ):
        if source == "env":
            monkeypatch.setenv("MODULO_SECRETS_BACKEND", name)
            backend = create_secrets_backend(fernet_key=None)
        else:
            backend = create_secrets_backend(fernet_key=None, backend_name=name)
        assert isinstance(backend, VaultSecretsBackend), f"Expected VaultSecretsBackend for name={name!r}"


@pytest.mark.parametrize(
    ("backend_name", "env_vars", "module_patch", "lib_patch"),
    [
        pytest.param(
            "vault",
            {"VAULT_ADDR": "http://vault:8200", "VAULT_TOKEN": "x"},
            "modulo.core.secrets_backend.vault",
            "modulo.core.secrets_backend.vault._hvac",
            id="vault",
        ),
        pytest.param(
            "aws",
            {"AWS_REGION": "us-east-1", "AWS_ACCESS_KEY_ID": "x", "AWS_SECRET_ACCESS_KEY": "y"},
            "modulo.core.secrets_backend.aws",
            "modulo.core.secrets_backend.aws._boto3",
            id="aws",
        ),
    ],
)
def test_fernet_key_optional_for_external_backend(
    monkeypatch: pytest.MonkeyPatch, backend_name, env_vars, module_patch, lib_patch
):
    if backend_name == "vault":
        from modulo.core.secrets_backend.vault import VaultSecretsBackend as expected_cls  # noqa: N813
    else:
        from modulo.core.secrets_backend.aws import AWSSecretsManagerBackend as expected_cls  # noqa: N813
    for k, v in env_vars.items():
        monkeypatch.setenv(k, v)
    with (
        patch("modulo.core.secrets_backend._check_external_secrets_licensed", return_value=True),
        patch(f"{module_patch}._MODULE_AVAILABLE", True),
        patch(lib_patch),
    ):
        backend = create_secrets_backend(fernet_key=None, backend_name=backend_name)
        assert isinstance(backend, expected_cls)


def test_fernet_key_required_when_backend_is_fernet():
    with pytest.raises(ValueError, match="fernet_key is required"):
        create_secrets_backend(fernet_key=None)


@pytest.mark.parametrize(
    "key",
    [
        pytest.param("", id="empty"),
        pytest.param(None, id="none"),
        pytest.param("   ", id="whitespace-only"),
    ],
)
def test_validate_key_rejects_blank_keys(key):
    """Empty, None and whitespace-only keys are all rejected as non-empty violations."""
    with pytest.raises(ValueError, match="non-empty"):
        validate_key(key)


def test_validate_key_strips_whitespace():
    assert validate_key("  my-key  ") == "my-key"
