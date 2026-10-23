"""Tests for create_secrets_backend factory."""

from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet

from modulo.core.feature_flags import _KNOWN_FLAGS, FeatureFlagRegistry
from modulo.core.secrets_backend import create_secrets_backend, validate_key
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
        patch("modulo.core.secrets_backend.aws._MODULE_AVAILABLE", True),
        patch("modulo.core.secrets_backend.aws._boto3"),
    ):
        backend = create_secrets_backend(fernet_key=_KEY, backend_name="aws")
        assert isinstance(backend, AWSSecretsManagerBackend)


def test_unknown_backend_raises_value_error():
    with pytest.raises(ValueError, match="Unknown"):
        create_secrets_backend(fernet_key=_KEY, backend_name="nonexistent")


@pytest.mark.parametrize("backend_name", ["vault", "aws"])
def test_configured_external_backend_is_constructed_on_every_tier(
    monkeypatch: pytest.MonkeyPatch, backend_name: str
) -> None:
    """FAR-2059 / FAR-2100: the factory never consults licence or tier state.

    External backends are available on every tier and a configured backend is
    always constructed - a licence lapse must not switch it to fernet.
    """
    if backend_name == "vault":
        from modulo.core.secrets_backend.vault import VaultSecretsBackend

        monkeypatch.setenv("VAULT_ADDR", "http://vault:8200")
        monkeypatch.setenv("VAULT_TOKEN", "x")
        with (
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
            patch("modulo.core.secrets_backend.vault._MODULE_AVAILABLE", True),
            patch("modulo.core.secrets_backend.vault._hvac"),
        ):
            backend = create_secrets_backend(fernet_key=None, backend_name=backend_name)
        assert isinstance(backend, VaultSecretsBackend)
        return

    from modulo.core.secrets_backend.aws import AWSSecretsManagerBackend

    monkeypatch.setenv("AWS_REGION", "us-east-1")
    with (
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


def test_external_secrets_flag_is_not_registered():
    """FAR-2100: the entitlement is retired — there is no runtime surface to gate."""
    registry = FeatureFlagRegistry()
    assert registry.get_flag("external_secrets") is None
    assert "external_secrets" not in {flag.name for flag in _KNOWN_FLAGS}


def test_external_secrets_flag_absent_from_seed_catalog():
    """The tier catalog seed is derived from ``_KNOWN_FLAGS``; the flag is gone there too."""
    from modulo.core.seed_data.catalog import FLAGS

    assert "external_secrets" not in {entry["name"] for entry in FLAGS}


def test_external_secrets_flag_absent_on_community_tier():
    registry = FeatureFlagRegistry(current_tier="community", has_license_key=False)
    assert registry.get_flag("external_secrets") is None


def test_external_secrets_flag_absent_on_team_tier():
    registry = FeatureFlagRegistry(current_tier="team", has_license_key=True)
    assert registry.get_flag("external_secrets") is None


def test_external_secrets_licence_helper_is_removed():
    """The factory must not consult licence state for backend construction."""
    import modulo.core.secrets_backend as secrets_module

    assert not hasattr(secrets_module, "_check_external_secrets_licensed")


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
