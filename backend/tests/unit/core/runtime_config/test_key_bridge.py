"""Tests for the FAR-1135 store-override bridges (key_bridge)."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any

import pytest

from modulo.core.runtime_config import key_bridge
from modulo.core.runtime_config.key_bridge import (
    get_override,
    override_int_or,
    override_or,
)
from modulo.core.runtime_config.store import RuntimeConfigStore


@pytest.fixture(autouse=True)
def _clean_store() -> Iterator[None]:
    RuntimeConfigStore.reset()
    yield
    RuntimeConfigStore.reset()
    key_bridge._BOOT_LOG_LEVEL = None


class TestGetOverride:
    def test_returns_none_when_unset(self) -> None:
        assert get_override("MODULO_SCIM_TOKEN") is None

    def test_returns_override_value(self) -> None:
        from modulo.core.runtime_config.store import get_runtime_config_store

        get_runtime_config_store().set_override("MODULO_SCIM_TOKEN", "hot-token")
        assert get_override("MODULO_SCIM_TOKEN") == "hot-token"

    def test_empty_string_override_is_not_none(self) -> None:
        """Explicitly clearing SCIM to "" must disable it, not fall back."""
        from modulo.core.runtime_config.store import get_runtime_config_store

        get_runtime_config_store().set_override("MODULO_SCIM_TOKEN", "")
        value = get_override("MODULO_SCIM_TOKEN")
        assert value is not None  # distinguish "" (set) from None (unset)
        assert not value  # the override really is the empty string

    def test_empty_key_returns_none(self) -> None:
        assert get_override("") is None


class TestOverrideOr:
    def test_falls_back_when_no_override(self) -> None:
        assert override_or("MODULO_RATELIMIT_BYPASS_TOKEN", "boot-token") == "boot-token"

    def test_override_wins_over_fallback(self) -> None:
        from modulo.core.runtime_config.store import get_runtime_config_store

        get_runtime_config_store().set_override("MODULO_RATELIMIT_BYPASS_TOKEN", "hot-token")
        assert override_or("MODULO_RATELIMIT_BYPASS_TOKEN", "boot-token") == "hot-token"

    def test_clear_reverts_to_fallback(self) -> None:
        from modulo.core.runtime_config.store import get_runtime_config_store

        store = get_runtime_config_store()
        store.set_override("MODULO_RATELIMIT_BYPASS_TOKEN", "hot-token")
        store.clear_override("MODULO_RATELIMIT_BYPASS_TOKEN")
        assert override_or("MODULO_RATELIMIT_BYPASS_TOKEN", "boot-token") == "boot-token"


class TestOverrideIntOr:
    def test_parses_integer_override(self) -> None:
        from modulo.core.runtime_config.store import get_runtime_config_store

        get_runtime_config_store().set_override("MODULO_MAX_LOCAL_CONCURRENCY", "7")
        assert override_int_or("MODULO_MAX_LOCAL_CONCURRENCY", 2) == 7

    def test_falls_back_when_unset(self) -> None:
        assert override_int_or("MODULO_MAX_LOCAL_CONCURRENCY", 2) == 2

    def test_invalid_override_falls_back(self) -> None:
        from modulo.core.runtime_config.store import get_runtime_config_store

        get_runtime_config_store().set_override("MODULO_MAX_LOCAL_CONCURRENCY", "not-a-number")
        assert override_int_or("MODULO_MAX_LOCAL_CONCURRENCY", 2) == 2


class TestApplyLogLevel:
    def test_apply_sets_root_level_from_override(self) -> None:
        from modulo.core.runtime_config.store import get_runtime_config_store

        root = logging.getLogger()
        original = root.level
        try:
            get_runtime_config_store().set_override("MODULO_LOG_LEVEL", "ERROR")
            key_bridge.apply_log_level()
            assert root.level == logging.ERROR
        finally:
            root.setLevel(original)

    def test_clear_restores_pre_override_level(self) -> None:
        from modulo.core.runtime_config.store import get_runtime_config_store

        root = logging.getLogger()
        original = root.level
        try:
            get_runtime_config_store().set_override("MODULO_LOG_LEVEL", "ERROR")
            key_bridge.apply_log_level()
            get_runtime_config_store().clear_override("MODULO_LOG_LEVEL")
            key_bridge.apply_log_level()
            assert root.level == original
        finally:
            root.setLevel(original)

    def test_invalid_override_keeps_current_level(self, caplog: pytest.LogCaptureFixture) -> None:
        from modulo.core.runtime_config.store import get_runtime_config_store

        root = logging.getLogger()
        original = root.level
        try:
            get_runtime_config_store().set_override("MODULO_LOG_LEVEL", "NOT_A_LEVEL")
            with caplog.at_level(logging.WARNING, logger="modulo.core.runtime_config.key_bridge"):
                key_bridge.apply_log_level()
            assert root.level == original
            assert any("invalid MODULO_LOG_LEVEL" in r.getMessage() for r in caplog.records)
        finally:
            root.setLevel(original)

    def test_noop_when_no_override(self) -> None:
        root = logging.getLogger()
        original = root.level
        key_bridge.apply_log_level()
        assert root.level == original


class TestConsumerWiring:
    """Prove-the-fix: the registered consumers actually read the override."""

    def test_local_provider_reads_concurrency_override(self) -> None:
        from modulo.core.runtime_config.store import get_runtime_config_store
        from modulo.core.runtime_provider.local import create_local_provider_from_env

        get_runtime_config_store().set_override("MODULO_MAX_LOCAL_CONCURRENCY", "5")
        provider = create_local_provider_from_env()
        assert provider._max_concurrency == 5

    async def test_scim_token_override_wins_over_settings(self) -> None:
        import uuid

        from fastapi import HTTPException
        from fastapi.security import HTTPAuthorizationCredentials

        from modulo.auth.scim_auth import get_scim_principal
        from modulo.core.runtime_config.store import get_runtime_config_store
        from modulo.settings import Settings

        settings = Settings(
            database_url="postgresql+asyncpg://localhost/test",
            secret_key="a" * 32,
            fernet_key="a" * 32,
            modulo_scim_token="boot-token",
            modulo_scim_default_org_id=str(uuid.uuid4()),
        )
        store = get_runtime_config_store()
        store.set_override("MODULO_SCIM_TOKEN", "hot-token")
        try:
            # The hot token authenticates even though Settings says boot-token.
            hot = HTTPAuthorizationCredentials(scheme="Bearer", credentials="hot-token")
            principal = await get_scim_principal(hot, settings, None)  # type: ignore[arg-type]
            assert principal is not None

            # The boot token is rejected while the override is active.
            boot = HTTPAuthorizationCredentials(scheme="Bearer", credentials="boot-token")
            with pytest.raises(HTTPException) as exc:
                await get_scim_principal(boot, settings, None)  # type: ignore[arg-type]
            assert exc.value.status_code == 401
        finally:
            store.clear_override("MODULO_SCIM_TOKEN")

        # After clear, the Settings value applies again.
        principal = await get_scim_principal(
            HTTPAuthorizationCredentials(scheme="Bearer", credentials="boot-token"),
            settings,
            None,  # type: ignore[arg-type]
        )
        assert principal is not None

    async def test_scim_token_empty_override_disables_scim(self) -> None:
        import uuid

        from fastapi import HTTPException
        from fastapi.security import HTTPAuthorizationCredentials

        from modulo.auth.scim_auth import get_scim_principal
        from modulo.core.runtime_config.store import get_runtime_config_store
        from modulo.settings import Settings

        settings = Settings(
            database_url="postgresql+asyncpg://localhost/test",
            secret_key="a" * 32,
            fernet_key="a" * 32,
            modulo_scim_token="boot-token",
            modulo_scim_default_org_id=str(uuid.uuid4()),
        )
        store = get_runtime_config_store()
        store.set_override("MODULO_SCIM_TOKEN", "")
        try:
            with pytest.raises(HTTPException) as exc:
                await get_scim_principal(
                    HTTPAuthorizationCredentials(scheme="Bearer", credentials="boot-token"),
                    settings,
                    None,  # type: ignore[arg-type]
                )
            assert exc.value.status_code == 501
        finally:
            store.clear_override("MODULO_SCIM_TOKEN")

    def _boot_settings(self, **extra: object) -> Any:
        from modulo.settings import Settings

        values: dict[str, Any] = {
            "database_url": "postgresql+asyncpg://localhost/test",
            "secret_key": "a" * 32,
            "fernet_key": "a" * 32,
        }
        values.update(extra)
        return Settings(**values)

    def test_public_url_override_wins_in_frontend_url_resolver(self) -> None:
        """FAR-1159 prove-the-fix: a runtime MODULO_PUBLIC_URL override reaches
        the frontend-URL resolver; clearing it reverts to the boot Settings."""
        from modulo.api.frontend_url import resolve_frontend_url
        from modulo.core.runtime_config.store import get_runtime_config_store

        settings = self._boot_settings(modulo_public_url="https://boot.example.com")
        store = get_runtime_config_store()
        store.set_override("MODULO_PUBLIC_URL", "https://hot.example.com")
        try:
            # Fails without the bridge: the resolver would read Settings only.
            assert resolve_frontend_url(settings) == "https://hot.example.com"
        finally:
            store.clear_override("MODULO_PUBLIC_URL")
        assert resolve_frontend_url(settings) == "https://boot.example.com"

    def test_public_url_override_wins_in_hitl_run_link(self) -> None:
        """FAR-1159 prove-the-fix: the HITL email run link honours the override."""
        import uuid

        from modulo.core.hitl_email_alerts import _run_link
        from modulo.core.runtime_config.store import get_runtime_config_store

        settings = self._boot_settings(modulo_public_url="https://boot.example.com")
        run_id = uuid.uuid4()
        store = get_runtime_config_store()
        store.set_override("MODULO_PUBLIC_URL", "https://hot.example.com")
        try:
            # Fails without the bridge: the link would be built from Settings only.
            assert _run_link(settings, run_id) == f"https://hot.example.com/runs/{run_id}"
        finally:
            store.clear_override("MODULO_PUBLIC_URL")
        assert _run_link(settings, run_id) == f"https://boot.example.com/runs/{run_id}"

    def test_e2b_registration_gate_reads_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """FAR-1159 prove-the-fix: the build_hub registration gate resolves
        MODULO_E2B_API_KEY via the bridge — an override registers E2B with no
        env var, and clearing it fails closed (provider not registered)."""
        from modulo.core.runtime_config.store import get_runtime_config_store
        from modulo.core.runtime_provider import build_hub
        from modulo.core.runtime_provider.e2b import E2BRuntimeProvider

        monkeypatch.delenv("MODULO_E2B_API_KEY", raising=False)
        monkeypatch.delenv("MODULO_DOCKER_HOST", raising=False)
        monkeypatch.delenv("DOCKER_HOST", raising=False)
        store = get_runtime_config_store()
        store.set_override("MODULO_E2B_API_KEY", "hot-key")
        try:
            # Fails without the bridge: the gate reads os.environ directly.
            hub = build_hub()
            provider = hub.get("e2b")
            assert isinstance(provider, E2BRuntimeProvider)
            assert provider._api_key == "hot-key"
        finally:
            store.clear_override("MODULO_E2B_API_KEY")
        # Fail-closed after clear: no override, no env -> E2B not registered.
        assert build_hub().get("e2b") is None

    def test_e2b_constructor_reads_override_and_fails_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """FAR-1159: the E2B provider constructor resolves the override and
        still refuses (typed ValueError) when no key exists at all."""
        from modulo.core.runtime_config.store import get_runtime_config_store
        from modulo.core.runtime_provider.e2b import E2BRuntimeProvider

        monkeypatch.delenv("MODULO_E2B_API_KEY", raising=False)
        store = get_runtime_config_store()

        # Fail-closed: neither override nor env -> typed refusal.
        with pytest.raises(ValueError, match="E2B API key is required"):
            E2BRuntimeProvider()

        store.set_override("MODULO_E2B_API_KEY", "hot-key")
        try:
            # Fails without the bridge: the constructor reads os.environ only.
            provider = E2BRuntimeProvider()
            assert provider._api_key == "hot-key"
        finally:
            store.clear_override("MODULO_E2B_API_KEY")

        # Fail-closed again once the override is cleared.
        with pytest.raises(ValueError, match="E2B API key is required"):
            E2BRuntimeProvider()
