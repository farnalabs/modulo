"""Guard test: every runtime-config key must be classified (FAR-1135).

The defect FAR-1135 exists to fix is structural: a key declared
``hot_reloadable=True`` whose governed code never reads the store accepts
an override via ``PUT /api/v1/admin/runtime-config`` and silently does
nothing. This test makes that impossible to ship:

- every hot-reloadable key must name at least one store-reading consumer
  (``module:function``), and every registered consumer must resolve to a
  real callable;
- every other key must carry an explicit ``boot_reason``;
- an unclassified new key — in either direction — fails here.
"""

from __future__ import annotations

import importlib
from typing import Any

import pytest

from modulo.core.runtime_config.store import (
    _KEY_CONFIG,
    BOOT_ONLY_REASONS,
    HOT_RELOADABLE_KEYS,
    KEY_CONSUMERS,
    KNOWN_KEYS,
)


def _resolve_consumer(ref: str) -> Any:
    """Import ``module[:attr.attr...]`` and return the final attribute."""
    module_name, _, attr_path = ref.partition(":")
    assert module_name, f"consumer ref {ref!r} has no module part"
    assert attr_path, f"consumer ref {ref!r} has no attribute part"
    obj: Any = importlib.import_module(module_name)
    for part in attr_path.split("."):
        assert hasattr(obj, part), f"consumer ref {ref!r}: {type(obj).__name__} has no attribute {part!r}"
        obj = getattr(obj, part)
    return obj


class TestKeyClassification:
    def test_every_known_key_is_classified(self) -> None:
        """Each key is either hot-with-consumers or boot-only-with-reason."""
        classified = set(KEY_CONSUMERS) | set(BOOT_ONLY_REASONS)
        unclassified = set(KNOWN_KEYS) - classified
        assert not unclassified, (
            "Unclassified runtime-config key(s): "
            f"{sorted(unclassified)} — add consumers= (hot-reloadable) or "
            "boot_reason= to _KEY_CONFIG in store.py"
        )

    def test_classification_is_a_partition(self) -> None:
        """No key is both hot-reloadable-with-consumers and boot-only."""
        overlap = set(KEY_CONSUMERS) & set(BOOT_ONLY_REASONS)
        assert not overlap, f"Key(s) classified both hot and boot-only: {sorted(overlap)}"

    def test_hot_keys_have_consumers(self) -> None:
        """hot_reloadable=True requires a non-empty consumer list."""
        hot_without_consumers = {k for k in HOT_RELOADABLE_KEYS if not KEY_CONSUMERS.get(k)}
        assert not hot_without_consumers, (
            f"hot_reloadable key(s) with no registered store-reading consumer: "
            f"{sorted(hot_without_consumers)} — an override for these would "
            "silently do nothing (the FAR-1135 defect)"
        )

    def test_consumers_imply_hot_flag(self) -> None:
        """A key with consumers must be flagged hot_reloadable (consistency)."""
        cold_with_consumers = set(KEY_CONSUMERS) - set(HOT_RELOADABLE_KEYS)
        assert not cold_with_consumers, f"Key(s) have consumers but hot_reloadable=False: {sorted(cold_with_consumers)}"

    def test_boot_keys_have_reasons(self) -> None:
        """Every non-hot key carries a non-empty boot_reason."""
        for key in KNOWN_KEYS:
            if key in HOT_RELOADABLE_KEYS:
                continue
            reason = BOOT_ONLY_REASONS.get(key)
            assert reason, f"boot-only key {key} has no boot_reason in _KEY_CONFIG"
            assert reason.strip(), f"boot-only key {key} has a blank boot_reason"

    def test_all_registered_consumers_resolve(self) -> None:
        """Every consumer ref names a real importable callable."""
        for key, refs in KEY_CONSUMERS.items():
            assert refs, f"{key} has an empty consumer list"
            for ref in refs:
                consumer = _resolve_consumer(ref)
                assert callable(consumer), f"consumer {ref!r} for {key} is not callable"

    def test_hot_flag_agrees_with_key_config(self) -> None:
        """HOT_RELOADABLE_KEYS is derived from _KeyConfig.hot_reloadable."""
        derived = {k for k, v in _KEY_CONFIG.items() if v.hot_reloadable}
        assert derived == set(HOT_RELOADABLE_KEYS)

    @pytest.mark.parametrize("key", sorted(HOT_RELOADABLE_KEYS))
    def test_each_hot_key_has_a_resolvable_consumer(self, key: str) -> None:
        """Per-key so a failure names exactly which key is unbridged."""
        refs = KEY_CONSUMERS.get(key)
        assert refs, f"{key} is hot_reloadable but has no consumers registered"
        for ref in refs:
            assert callable(_resolve_consumer(ref)), f"consumer {ref!r} for {key} is not callable"
