"""Unit tests for org-vault credential references (FAR-1640).

The resolution seam is security-sensitive: it must fail CLOSED with a typed
error naming only the offending key, and must never return an empty string or
fall back to treating a reference as a literal credential.
"""

from __future__ import annotations

import pytest

from modulo.core.secrets_backend import (
    CredentialReferenceError,
    SecretsBackend,
    is_secret_ref,
    parse_secret_ref,
    resolve_credential,
    resolve_credential_reference,
)


class _FakeBackend(SecretsBackend):
    """In-memory backend; ``error`` models an unreadable/misconfigured vault."""

    def __init__(self, values: dict[str, str], *, error: Exception | None = None) -> None:
        self._values = values
        self._error = error
        self.calls: list[str] = []

    async def get_secret(self, key: str) -> str:
        self.calls.append(key)
        if self._error is not None:
            raise self._error
        if key not in self._values:
            # FernetSecretsBackend raises KeyError for a key absent under the
            # caller's org (a foreign-org key is indistinguishable from missing).
            raise KeyError(key)
        return self._values[key]

    async def set_secret(self, key: str, value: str) -> None:  # pragma: no cover - unused
        raise AssertionError("set_secret must not be called during resolution")

    async def delete_secret(self, key: str) -> None:  # pragma: no cover - unused
        raise AssertionError("delete_secret must not be called during resolution")


class TestIsSecretRef:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            pytest.param("secretref://k", True, id="token"),
            pytest.param("  secretref://k  ", True, id="whitespace-trimmed"),
            pytest.param("literal", False, id="literal"),
            pytest.param("", False, id="empty"),
            pytest.param(None, False, id="none"),
            pytest.param(123, False, id="non-string"),
        ],
    )
    def test_detects_marker(self, value: object, expected: bool) -> None:
        assert is_secret_ref(value) is expected


class TestParseSecretRef:
    def test_parses_key(self) -> None:
        assert parse_secret_ref("secretref://vault/openai") == "vault/openai"

    def test_trims_surrounding_whitespace(self) -> None:
        assert parse_secret_ref(" secretref://k ") == "k"

    def test_non_secretref_reference_raises(self) -> None:
        with pytest.raises(CredentialReferenceError) as exc_info:
            parse_secret_ref("vault/openai")
        assert not exc_info.value.key

    def test_empty_key_raises(self) -> None:
        with pytest.raises(CredentialReferenceError) as exc_info:
            parse_secret_ref("secretref://   ")
        assert not exc_info.value.key

    def test_key_with_whitespace_raises(self) -> None:
        with pytest.raises(CredentialReferenceError) as exc_info:
            parse_secret_ref("secretref://vault/open ai")
        assert exc_info.value.key == "vault/open ai"


class TestResolveCredentialReference:
    async def test_resolves_value_under_backend(self) -> None:
        backend = _FakeBackend({"vault/openai": "sk-secret"})
        value = await resolve_credential_reference(backend, "secretref://vault/openai")
        assert value == "sk-secret"
        # The marker is stripped; the backend receives the bare key.
        assert backend.calls == ["vault/openai"]

    async def test_missing_key_fails_closed_naming_the_key(self) -> None:
        backend = _FakeBackend({})
        with pytest.raises(CredentialReferenceError) as exc_info:
            await resolve_credential_reference(backend, "secretref://vault/missing")
        error = exc_info.value
        assert error.key == "vault/missing"
        assert "vault/missing" in str(error)
        assert "not found" in error.reason

    async def test_empty_resolved_value_fails_closed(self) -> None:
        backend = _FakeBackend({"vault/empty": ""})
        with pytest.raises(CredentialReferenceError) as exc_info:
            await resolve_credential_reference(backend, "secretref://vault/empty")
        assert exc_info.value.key == "vault/empty"
        assert "empty" in exc_info.value.reason

    async def test_unreadable_vault_fails_closed(self) -> None:
        backend = _FakeBackend({}, error=RuntimeError("vault is down"))
        with pytest.raises(CredentialReferenceError) as exc_info:
            await resolve_credential_reference(backend, "secretref://vault/openai")
        assert exc_info.value.key == "vault/openai"
        assert "could not be read" in exc_info.value.reason
        # The underlying message must not leak into the typed reason.
        assert "vault is down" not in str(exc_info.value)

    async def test_validation_detail_names_only_the_key(self) -> None:
        backend = _FakeBackend({})
        with pytest.raises(CredentialReferenceError) as exc_info:
            await resolve_credential_reference(backend, "secretref://vault/missing")
        detail = exc_info.value.validation_detail(["body", "api_key"])
        assert detail[0]["type"] == "credential_reference_error"
        assert detail[0]["loc"] == ["body", "api_key"]
        assert "vault/missing" in detail[0]["msg"]


class TestResolveCredential:
    async def test_literal_passes_through_without_touching_backend(self) -> None:
        backend = _FakeBackend({})
        assert await resolve_credential(backend, "sk-literal") == "sk-literal"
        assert not backend.calls

    async def test_secretref_value_is_resolved(self) -> None:
        backend = _FakeBackend({"k": "resolved"})
        assert await resolve_credential(backend, "secretref://k") == "resolved"
        assert backend.calls == ["k"]


class TestBackendOrgScopeFlags:
    """FAR-1640: only the Fernet backend partitions secrets by organisation."""

    def test_base_and_external_backends_are_not_org_scoped(self) -> None:
        from modulo.core.secrets_backend import SecretsBackend
        from modulo.core.secrets_backend.aws import AWSSecretsManagerBackend
        from modulo.core.secrets_backend.vault import VaultSecretsBackend

        assert SecretsBackend.organisation_scoped is False
        assert AWSSecretsManagerBackend.organisation_scoped is False
        assert VaultSecretsBackend.organisation_scoped is False

    def test_fernet_backend_is_org_scoped(self) -> None:
        from modulo.core.secrets_backend.fernet import FernetSecretsBackend

        assert FernetSecretsBackend.organisation_scoped is True


class TestMultiOrgExternalBackendGuard:
    """FAR-1640: an external (non-org-scoped) backend is refused in multi-org.

    The external backends resolve a global key namespace, so on a licensed
    multi-org deployment the same key name could source another organisation's
    secret. The reference path must fail closed BEFORE any backend read.
    """

    @pytest.fixture
    def multi_org(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("modulo.core.secrets_backend.references._multi_org_enabled", lambda: True)

    @pytest.fixture
    def single_org(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("modulo.core.secrets_backend.references._multi_org_enabled", lambda: False)

    async def test_unscoped_backend_refused_in_multi_org_without_reading(self, multi_org: None) -> None:
        backend = _FakeBackend({"kv/x": "sk-secret"})
        with pytest.raises(CredentialReferenceError) as exc_info:
            await resolve_credential_reference(backend, "secretref://kv/x")
        error = exc_info.value
        assert error.key == "kv/x"
        assert "not organisation-scoped" in error.reason
        # Fail closed: the backend is never read, so no cross-org value is fetched.
        assert not backend.calls

    async def test_unscoped_backend_allowed_in_single_org(self, single_org: None) -> None:
        backend = _FakeBackend({"kv/x": "sk-secret"})
        value = await resolve_credential_reference(backend, "secretref://kv/x")
        assert value == "sk-secret"
        assert backend.calls == ["kv/x"]

    async def test_org_scoped_backend_allowed_in_multi_org(self, multi_org: None) -> None:
        backend = _FakeBackend({"kv/x": "sk-secret"})
        backend.organisation_scoped = True
        value = await resolve_credential_reference(backend, "secretref://kv/x")
        assert value == "sk-secret"
        assert backend.calls == ["kv/x"]

    async def test_literal_unaffected_by_multi_org_guard(self, multi_org: None) -> None:
        backend = _FakeBackend({})
        assert await resolve_credential(backend, "sk-literal") == "sk-literal"
        assert not backend.calls
