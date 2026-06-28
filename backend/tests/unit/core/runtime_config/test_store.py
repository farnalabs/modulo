"""Tests for RuntimeConfigStore."""

from __future__ import annotations

import os
from unittest.mock import patch

from modulo.core.runtime_config.store import (
    DEFAULT_VALUES,
    HOT_RELOADABLE_KEYS,
    KNOWN_KEYS,
    ConfigEntry,
    RuntimeConfigStore,
    get_runtime_config_store,
)


class TestRuntimeConfigStore:
    """Unit tests for the RuntimeConfigStore singleton."""

    def _purge_singleton(self) -> None:
        """Reset the module-level singleton for test isolation."""
        import modulo.core.runtime_config.store as store_mod

        store_mod._store = None

    # ----------------------------------------------------------------
    # Singleton
    # ----------------------------------------------------------------

    def test_singleton_returns_same_instance(self) -> None:
        self._purge_singleton()
        s1 = get_runtime_config_store()
        s2 = get_runtime_config_store()
        assert s1 is s2

    def test_singleton_is_runtime_config_store(self) -> None:
        self._purge_singleton()
        store = get_runtime_config_store()
        assert isinstance(store, RuntimeConfigStore)

    # ----------------------------------------------------------------
    # Initial state
    # ----------------------------------------------------------------

    def test_known_keys_are_loaded(self) -> None:
        self._purge_singleton()
        store = get_runtime_config_store()
        # get_all should return one entry per KNOWN_KEYS
        all_items = store.get_all()
        assert len(all_items) == len(KNOWN_KEYS)
        keys_in_items = {item.key for item in all_items}
        assert keys_in_items == set(KNOWN_KEYS)

    def test_default_value_when_no_env_and_no_override(self) -> None:
        self._purge_singleton()
        with patch.dict(os.environ, {}, clear=True):
            store = get_runtime_config_store()
            # REDIS_URL has a default
            val = store.get("REDIS_URL")
            assert val == DEFAULT_VALUES["REDIS_URL"]

    def test_provenance_is_default_when_no_env_and_no_override(self) -> None:
        self._purge_singleton()
        with patch.dict(os.environ, {}, clear=True):
            store = get_runtime_config_store()
            entry = next(item for item in store.get_all() if item.key == "REDIS_URL")
            assert entry.provenance == "default"
            assert entry.env_value is None
            assert entry.override_value is None
            assert entry.current_value == DEFAULT_VALUES["REDIS_URL"]

    # ----------------------------------------------------------------
    # Get: override > env > default
    # ----------------------------------------------------------------

    def test_get_uses_env_when_set(self) -> None:
        self._purge_singleton()
        with patch.dict(os.environ, {"REDIS_URL": "redis://override:6379"}, clear=True):
            store = get_runtime_config_store()
            val = store.get("REDIS_URL")
            assert val == "redis://override:6379"

    def test_get_uses_override_over_env(self) -> None:
        self._purge_singleton()
        with patch.dict(os.environ, {"REDIS_URL": "redis://env:6379"}, clear=True):
            store = get_runtime_config_store()
            store.set_override("REDIS_URL", "redis://override:6379")
            val = store.get("REDIS_URL")
            assert val == "redis://override:6379"

    def test_get_uses_override_when_no_env(self) -> None:
        self._purge_singleton()
        with patch.dict(os.environ, {}, clear=True):
            store = get_runtime_config_store()
            store.set_override("REDIS_URL", "redis://override:6379")
            val = store.get("REDIS_URL")
            assert val == "redis://override:6379"

    def test_get_returns_default_when_nothing_set(self) -> None:
        self._purge_singleton()
        with patch.dict(os.environ, {}, clear=True):
            store = get_runtime_config_store()
            val = store.get("REDIS_URL")
            assert val == DEFAULT_VALUES["REDIS_URL"]

    def test_get_returns_none_for_unknown_key(self) -> None:
        self._purge_singleton()
        with patch.dict(os.environ, {}, clear=True):
            store = get_runtime_config_store()
            val = store.get("THIS_KEY_DOES_NOT_EXIST")
            assert val is None

    # ----------------------------------------------------------------
    # set_override / clear_override
    # ----------------------------------------------------------------

    def test_set_override(self) -> None:
        self._purge_singleton()
        with patch.dict(os.environ, {}, clear=True):
            store = get_runtime_config_store()
            store.set_override("MODULO_LOG_LEVEL", "DEBUG")
            assert store.get("MODULO_LOG_LEVEL") == "DEBUG"

    def test_clear_override(self) -> None:
        self._purge_singleton()
        with patch.dict(os.environ, {}, clear=True):
            store = get_runtime_config_store()
            store.set_override("MODULO_LOG_LEVEL", "DEBUG")
            store.clear_override("MODULO_LOG_LEVEL")
            assert store.get("MODULO_LOG_LEVEL") == DEFAULT_VALUES["MODULO_LOG_LEVEL"]

    def test_clear_override_nonexistent_does_not_raise(self) -> None:
        self._purge_singleton()
        store = get_runtime_config_store()
        store.clear_override("NONEXISTENT")

    def test_overrides_return_provenance_override(self) -> None:
        self._purge_singleton()
        with patch.dict(os.environ, {}, clear=True):
            store = get_runtime_config_store()
            store.set_override("MODULO_DEMO_MODE", "true")
            entry = next(item for item in store.get_all() if item.key == "MODULO_DEMO_MODE")
            assert entry.provenance == "override"
            assert entry.override_value == "true"
            assert entry.current_value == "true"

    def test_clear_override_reverts_provenance(self) -> None:
        self._purge_singleton()
        with patch.dict(os.environ, {}, clear=True):
            store = get_runtime_config_store()
            store.set_override("MODULO_DEMO_MODE", "true")
            store.clear_override("MODULO_DEMO_MODE")
            entry = next(item for item in store.get_all() if item.key == "MODULO_DEMO_MODE")
            assert entry.provenance == "default"
            assert entry.current_value == DEFAULT_VALUES["MODULO_DEMO_MODE"]

    # ----------------------------------------------------------------
    # clear_all_overrides
    # ----------------------------------------------------------------

    def test_clear_all_overrides(self) -> None:
        self._purge_singleton()
        with patch.dict(os.environ, {}, clear=True):
            store = get_runtime_config_store()
            store.set_override("MODULO_LOG_LEVEL", "DEBUG")
            store.set_override("MODULO_DEMO_MODE", "true")
            store.clear_all_overrides()
            assert store.get("MODULO_LOG_LEVEL") == DEFAULT_VALUES["MODULO_LOG_LEVEL"]
            assert store.get("MODULO_DEMO_MODE") == DEFAULT_VALUES["MODULO_DEMO_MODE"]

    # ----------------------------------------------------------------
    # reload()
    # ----------------------------------------------------------------

    def test_reload_picks_up_new_env_vars(self) -> None:
        self._purge_singleton()
        with patch.dict(os.environ, {}, clear=True):
            store = get_runtime_config_store()
            # Initially no env value
            assert store.get("MODULO_LOG_LEVEL") == DEFAULT_VALUES["MODULO_LOG_LEVEL"]
        # Now set env var outside the context manager scope
        with patch.dict(os.environ, {"MODULO_LOG_LEVEL": "TRACE"}, clear=True):
            store.reload()
            assert store.get("MODULO_LOG_LEVEL") == "TRACE"

    def test_reload_detects_removed_env_vars(self) -> None:
        self._purge_singleton()
        with patch.dict(os.environ, {"MODULO_LOG_LEVEL": "TRACE"}, clear=True):
            store = get_runtime_config_store()
            assert store.get("MODULO_LOG_LEVEL") == "TRACE"
        # Remove env var
        with patch.dict(os.environ, {}, clear=True):
            store.reload()
            assert store.get("MODULO_LOG_LEVEL") == DEFAULT_VALUES["MODULO_LOG_LEVEL"]

    def test_reload_does_not_clear_overrides(self) -> None:
        self._purge_singleton()
        with patch.dict(os.environ, {}, clear=True):
            store = get_runtime_config_store()
            store.set_override("MODULO_LOG_LEVEL", "WARN")
            store.reload()
            # Override still wins
            assert store.get("MODULO_LOG_LEVEL") == "WARN"

    # ----------------------------------------------------------------
    # get_all() correctness
    # ----------------------------------------------------------------

    def test_get_all_returns_entries_with_all_fields(self) -> None:
        self._purge_singleton()
        with patch.dict(os.environ, {}, clear=True):
            store = get_runtime_config_store()
            entries = store.get_all()
            for entry in entries:
                assert isinstance(entry, ConfigEntry)
                assert entry.key in KNOWN_KEYS
                assert isinstance(entry.hot_reloadable, bool)

    def test_get_all_env_provenance(self) -> None:
        self._purge_singleton()
        with patch.dict(os.environ, {"MODULO_LOG_LEVEL": "ERROR"}, clear=True):
            store = get_runtime_config_store()
            entry = next(item for item in store.get_all() if item.key == "MODULO_LOG_LEVEL")
            assert entry.provenance == "environment"
            assert entry.env_value == "ERROR"
            assert entry.current_value == "ERROR"

    def test_get_all_drift_indicator(self) -> None:
        """When env var changes after reload, current_value should reflect the change."""
        self._purge_singleton()
        with patch.dict(os.environ, {"MODULO_LOG_LEVEL": "ERROR"}, clear=True):
            store = get_runtime_config_store()
            entry = next(item for item in store.get_all() if item.key == "MODULO_LOG_LEVEL")
            assert entry.current_value == "ERROR"
            assert entry.provenance == "environment"

    # ----------------------------------------------------------------
    # Provenance calculation
    # ----------------------------------------------------------------

    def test_provenance_default(self) -> None:
        self._purge_singleton()
        with patch.dict(os.environ, {}, clear=True):
            store = get_runtime_config_store()
            entry = next(item for item in store.get_all() if item.key == "REDIS_URL")
            assert entry.provenance == "default"

    def test_provenance_environment(self) -> None:
        self._purge_singleton()
        with patch.dict(os.environ, {"REDIS_URL": "redis://env:6379"}, clear=True):
            store = get_runtime_config_store()
            entry = next(item for item in store.get_all() if item.key == "REDIS_URL")
            assert entry.provenance == "environment"

    def test_provenance_override(self) -> None:
        self._purge_singleton()
        with patch.dict(os.environ, {}, clear=True):
            store = get_runtime_config_store()
            store.set_override("REDIS_URL", "redis://ovr:6379")
            entry = next(item for item in store.get_all() if item.key == "REDIS_URL")
            assert entry.provenance == "override"

    def test_provenance_override_precedes_env(self) -> None:
        self._purge_singleton()
        with patch.dict(os.environ, {"REDIS_URL": "redis://env:6379"}, clear=True):
            store = get_runtime_config_store()
            store.set_override("REDIS_URL", "redis://ovr:6379")
            entry = next(item for item in store.get_all() if item.key == "REDIS_URL")
            assert entry.provenance == "override"

    # ----------------------------------------------------------------
    # Hot-reloadable flag
    # ----------------------------------------------------------------

    def test_hot_reloadable_keys_flagged(self) -> None:
        self._purge_singleton()
        with patch.dict(os.environ, {}, clear=True):
            store = get_runtime_config_store()
            entries = store.get_all()
            for entry in entries:
                if entry.key in HOT_RELOADABLE_KEYS:
                    assert entry.hot_reloadable is True, f"{entry.key} should be hot-reloadable"
                else:
                    assert entry.hot_reloadable is False, f"{entry.key} should NOT be hot-reloadable"

    # ----------------------------------------------------------------
    # Keys with no default return None
    # ----------------------------------------------------------------

    def test_keys_without_default_return_none(self) -> None:
        self._purge_singleton()
        with patch.dict(os.environ, {}, clear=True):
            store = get_runtime_config_store()
            # DATABASE_URL, SECRET_KEY, FERNET_KEY have no defaults
            for key in ("DATABASE_URL", "SECRET_KEY", "FERNET_KEY"):
                assert store.get(key) is None
