"""Tests for create_secrets_backend factory."""

import contextlib
import importlib
from collections.abc import Iterator
from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet

from modulo.core.feature_flags import _KNOWN_FLAGS, FeatureFlagRegistry
from modulo.core.secrets_backend import create_secrets_backend, validate_key
from modulo.core.secrets_backend.fernet import FernetSecretsBackend

_KEY = Fernet.generate_key().decode()


class _ExternalBackendSpec:
    """How to configure and stub one optional external backend in the factory."""

    def __init__(self, env: dict[str, str], module: str, class_name: str, sdk_patch: str) -> None:
        self.env = env
        self.module = module
        self.class_name = class_name
        self.sdk_patch = sdk_patch


_EXTERNAL_BACKENDS: dict[str, _ExternalBackendSpec] = {
    "vault": _ExternalBackendSpec(
        env={"VAULT_ADDR": "http://vault:8200", "VAULT_TOKEN": "x"},
        module="modulo.core.secrets_backend.vault",
        class_name="VaultSecretsBackend",
        sdk_patch="modulo.core.secrets_backend.vault._hvac",
    ),
    "aws": _ExternalBackendSpec(
        env={"AWS_REGION": "us-east-1", "AWS_ACCESS_KEY_ID": "x", "AWS_SECRET_ACCESS_KEY": "y"},
        module="modulo.core.secrets_backend.aws",
        class_name="AWSSecretsManagerBackend",
        sdk_patch="modulo.core.secrets_backend.aws._boto3",
    ),
}


@contextlib.contextmanager
def _stub_external_backend(monkeypatch: pytest.MonkeyPatch, name: str) -> Iterator[type]:
    """Configure *name*'s env vars and stub its optional SDK, yielding the class.

    The backend constructor reads its configuration from the environment and
    refuses to build without the optional SDK module, so every external-backend
    factory test needs both; keeping that setup in one place stops the env/patch
    block being copy-pasted into each test.
    """
    spec = _EXTERNAL_BACKENDS[name]
    for key, value in spec.env.items():
        monkeypatch.setenv(key, value)
    with patch(f"{spec.module}._MODULE_AVAILABLE", True), patch(spec.sdk_patch):
        yield getattr(importlib.import_module(spec.module), spec.class_name)


@pytest.fixture(params=sorted(_EXTERNAL_BACKENDS))
def external_backend(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[str, type]]:
    """Yield ``(backend_name, expected_class)`` for each optional external backend."""
    with _stub_external_backend(monkeypatch, request.param) as expected_cls:
        yield request.param, expected_cls


@pytest.mark.parametrize("backend_name", [None, "fernet", "  Fernet  ", "FERNET"])
def test_fernet_backend_created(backend_name: str | None) -> None:
    backend = create_secrets_backend(fernet_key=_KEY, backend_name=backend_name)
    assert isinstance(backend, FernetSecretsBackend)


def test_external_backend_created_by_name(external_backend: tuple[str, type]) -> None:
    backend_name, expected_cls = external_backend

    backend = create_secrets_backend(fernet_key=_KEY, backend_name=backend_name)

    assert isinstance(backend, expected_cls)


def test_unknown_backend_raises_value_error() -> None:
    with pytest.raises(ValueError, match="Unknown"):
        create_secrets_backend(fernet_key=_KEY, backend_name="nonexistent")


@pytest.mark.parametrize("fernet_key", [pytest.param(_KEY, id="fernet-key"), pytest.param(None, id="no-fernet-key")])
def test_external_backend_constructed_regardless_of_fernet_key(
    external_backend: tuple[str, type], fernet_key: str | None
) -> None:
    """FAR-2059 / FAR-2100: a configured external backend is always constructed.

    External backends are available on every tier and there is no fernet
    fallback any more, so the factory neither requires a fernet key for them nor
    consults licence/tier state (it has no such input) - a licence lapse must
    not switch a vault/aws deployment to fernet.
    """
    backend_name, expected_cls = external_backend

    backend = create_secrets_backend(fernet_key=fernet_key, backend_name=backend_name)

    assert isinstance(backend, expected_cls)


def test_unconfigured_vault_backend_fails_hard_not_silently(monkeypatch: pytest.MonkeyPatch) -> None:
    """A genuinely unconfigured external backend fails in its constructor.

    The factory warns about the missing configuration but never substitutes a
    different backend: a silent switch is exactly the failure this ticket
    removes.
    """
    with _stub_external_backend(monkeypatch, "vault"):
        monkeypatch.delenv("VAULT_ADDR", raising=False)
        with pytest.raises(ValueError, match="VAULT_ADDR is not set"):
            create_secrets_backend(fernet_key=_KEY, backend_name="vault")


def test_external_secrets_flag_is_not_registered() -> None:
    """FAR-2100: the entitlement is retired — there is no runtime surface to gate."""
    registry = FeatureFlagRegistry()
    assert registry.get_flag("external_secrets") is None
    assert "external_secrets" not in {flag.name for flag in _KNOWN_FLAGS}


def test_external_secrets_flag_absent_from_seed_catalog() -> None:
    """The tier catalog seed is derived from ``_KNOWN_FLAGS``; the flag is gone there too."""
    from modulo.core.seed_data.catalog import FLAGS

    assert "external_secrets" not in {entry["name"] for entry in FLAGS}


@pytest.mark.parametrize(
    ("tier", "has_license_key"),
    [
        pytest.param("community", False, id="community"),
        pytest.param("team", True, id="team"),
    ],
)
def test_external_secrets_flag_absent_on_every_tier(tier: str, has_license_key: bool) -> None:
    registry = FeatureFlagRegistry(current_tier=tier, has_license_key=has_license_key)

    assert registry.get_flag("external_secrets") is None


def test_external_secrets_licence_helper_is_removed() -> None:
    """The factory must not consult licence state for backend construction."""
    import modulo.core.secrets_backend as secrets_module

    assert not hasattr(secrets_module, "_check_external_secrets_licensed")


def test_env_var_used_when_no_backend_name(monkeypatch: pytest.MonkeyPatch) -> None:
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
def test_backend_name_normalized(monkeypatch: pytest.MonkeyPatch, name: str, source: str) -> None:
    """Both ``backend_name`` and ``MODULO_SECRETS_BACKEND`` are lowercased/stripped before matching."""
    with _stub_external_backend(monkeypatch, "vault") as expected_cls:
        if source == "env":
            monkeypatch.setenv("MODULO_SECRETS_BACKEND", name)
            backend = create_secrets_backend(fernet_key=None)
        else:
            backend = create_secrets_backend(fernet_key=None, backend_name=name)

        assert isinstance(backend, expected_cls), f"Expected {expected_cls.__name__} for name={name!r}"


def test_fernet_key_required_when_backend_is_fernet() -> None:
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
def test_validate_key_rejects_blank_keys(key: str | None) -> None:
    """Empty, None and whitespace-only keys are all rejected as non-empty violations."""
    with pytest.raises(ValueError, match="non-empty"):
        validate_key(key)  # type: ignore[arg-type]


def test_validate_key_strips_whitespace() -> None:
    assert validate_key("  my-key  ") == "my-key"
