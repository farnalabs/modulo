"""Tests for trigger config_json key validation (FAR-1144, FAR-1394).

Covers:
* ``_validate_trigger_config_keys`` (write-time gate): accepted keys pass
  through; unrecognised keys are rejected with a clear 400 listing the
  offending key(s) and the set of recognised keys.
* ``_RECOGNISED_TRIGGER_CONFIG_KEYS`` stays in sync with the engine's
  ``cfg.get()`` read sites — a key added to one and not the other is a bug.
* No-delivery-streak config keys (FAR-1394): each key the streak engine reads
  in ``core/trigger_streak.py`` is accepted by the write path for both engine
  trigger types (ongoing, cron), and a misspelled streak key still 400s.
* ``_merge_trigger_config`` with ``None``-removal: ``{"events": null}``
  removes the dead key from the merged result.
* Post-merge validation: an update that would *leave* an unread key in the
  merged result is rejected.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from fastapi import HTTPException

from modulo.api.routes.triggers import (
    _RECOGNISED_TRIGGER_CONFIG_KEYS,
    _merge_trigger_config,
    _validate_trigger_config_keys,
)
from modulo.core.trigger_engine import _RECOGNISED_TRIGGER_CONFIG_KEYS as _ENGINE_KEYS
from modulo.core.trigger_streak import _streak_auto_deactivate_enabled, _streak_config

_ENGINE_SOURCE_DIR = Path(__file__).resolve().parents[3] / "src" / "modulo"

#: Source files whose ``cfg``/``config`` locals hold a trigger's ``config_json``.
#: ``polling.py`` and ``pre_guardrail.py`` also call ``.get()`` but on a
#: *connector* config and a *guardrail-definition* config respectively — not the
#: trigger ``config_json`` — so they are deliberately excluded.
#: ``core/trigger_streak.py`` is included (FAR-1394): the no-delivery-streak
#: engine reads its per-trigger config there, and omitting it is exactly how
#: the streak keys stayed out of both recognised-key sets while every sync
#: test passed.
_TRIGGER_CONFIG_SOURCES: tuple[str, ...] = (
    "core/trigger_engine/__init__.py",
    "core/cron_helpers.py",
    "core/trigger_engine/agent_signal.py",
    "core/trigger_engine/slack_app_mention.py",
    "core/trigger_streak.py",
)

#: Local variable names bound to a trigger's ``config_json`` at the read sites.
_CONFIG_VAR_NAMES: frozenset[str] = frozenset({"cfg", "config"})


def _module_string_constants(tree: ast.Module) -> dict[str, str]:
    """Top-level ``NAME = "literal"`` assignments, for resolving ``.get(NAME)``
    read sites whose key references a module constant
    (e.g. ``STREAK_AUTO_DEACTIVATE_CONFIG_KEY``).
    """
    constants: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    constants[target.id] = node.value.value
    return constants


def _receiver_is_trigger_config(node: ast.expr) -> bool:
    """True when the ``.get()`` receiver is a trigger ``config_json`` local.

    Matches a bare ``cfg``/``config`` name and the inline fallback form
    ``(config or {})`` used by ``_streak_auto_deactivate_enabled``.
    """
    if isinstance(node, ast.Name):
        return node.id in _CONFIG_VAR_NAMES
    if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or) and node.values:
        first = node.values[0]
        return isinstance(first, ast.Name) and first.id in _CONFIG_VAR_NAMES
    return False


def _trigger_config_read_site_keys() -> set[str]:
    """Statically collect every ``cfg.get(<key>)`` / ``config.get(<key>)`` read
    across the trigger-config source files, where ``<key>`` is a string literal
    or a reference to a module-level string constant.
    """
    keys: set[str] = set()
    for rel in _TRIGGER_CONFIG_SOURCES:
        tree = ast.parse((_ENGINE_SOURCE_DIR / rel).read_text(encoding="utf-8"))
        constants = _module_string_constants(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            func = node.func
            if not isinstance(func, ast.Attribute) or func.attr != "get":
                continue
            if not _receiver_is_trigger_config(func.value):
                continue
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                keys.add(first.value)
            elif isinstance(first, ast.Name) and first.id in constants:
                keys.add(constants[first.id])
    return keys


class TestValidateTriggerConfigKeys:
    def test_none_config_passes(self) -> None:
        """None config is a no-op — there are no keys to mis-declare."""
        result = _validate_trigger_config_keys(None)
        assert result is None

    def test_empty_config_passes(self) -> None:
        """Empty dict is a no-op."""
        result = _validate_trigger_config_keys({})
        assert result is None

    def test_accepted_keys_pass(self) -> None:
        """Every key the engine reads is accepted."""
        for key in _RECOGNISED_TRIGGER_CONFIG_KEYS:
            result = _validate_trigger_config_keys({key: "value"})
            assert result is None

    def test_unrecognised_key_raises_400(self) -> None:
        """A key the engine does not read is rejected with 400."""
        with pytest.raises(HTTPException) as exc_info:
            _validate_trigger_config_keys({"events": ["pull_request"]})
        assert exc_info.value.status_code == 400
        assert "events" in exc_info.value.detail

    def test_multiple_unrecognised_keys_listed(self) -> None:
        """All offending keys appear in the error detail."""
        with pytest.raises(HTTPException) as exc_info:
            _validate_trigger_config_keys({"events": ["push"], "filter_by": "branch"})
        detail = exc_info.value.detail
        assert "events" in detail
        assert "filter_by" in detail

    def test_error_detail_names_recognised_keys(self) -> None:
        """The error message tells the caller what keys ARE valid."""
        with pytest.raises(HTTPException) as exc_info:
            _validate_trigger_config_keys({"events": ["push"]})
        detail = exc_info.value.detail
        assert "accepted_events" in detail
        assert "event_filters" in detail

    def test_mix_of_valid_and_invalid_rejects(self) -> None:
        """A mix of valid and invalid keys is rejected (not just the invalid ones)."""
        with pytest.raises(HTTPException):
            _validate_trigger_config_keys(
                {
                    "hmac_secret": "secret",
                    "unknown_key": "value",
                }
            )

    def test_recommended_fix_in_error_message(self) -> None:
        """The error message suggests the correct key for event filtering."""
        with pytest.raises(HTTPException) as exc_info:
            _validate_trigger_config_keys({"events": ["push"]})
        detail = exc_info.value.detail
        assert "'accepted_events'" in detail
        assert "'event_filters'" in detail

    def test_context_param_appears_in_error(self) -> None:
        """The context parameter labels the source in the error message."""
        with pytest.raises(HTTPException) as exc_info:
            _validate_trigger_config_keys({"events": ["push"]}, context="merged config_json")
        assert "merged config_json" in exc_info.value.detail

    def test_error_suggests_null_removal(self) -> None:
        """The error message tells the caller how to remove a stale key."""
        with pytest.raises(HTTPException) as exc_info:
            _validate_trigger_config_keys({"events": ["push"]})
        assert "null" in exc_info.value.detail.lower()


class TestMergeTriggerConfigNoneRemoval:
    def test_none_removes_stored_key(self) -> None:
        """{'events': null} removes the events key from the merged result."""
        stored = {"events": ["pull_request"], "hmac_secret": "secret"}
        merged = _merge_trigger_config(stored, {"events": None})
        assert "events" not in merged
        assert merged["hmac_secret"] == "secret"

    def test_none_on_absent_key_is_noop(self) -> None:
        """{'nonexistent': null} on a key that doesn't exist is a no-op."""
        stored = {"hmac_secret": "secret"}
        merged = _merge_trigger_config(stored, {"nonexistent": None})
        assert merged == {"hmac_secret": "secret"}

    def test_none_removes_only_targeted_key(self) -> None:
        """Only the targeted key is removed; siblings stay intact."""
        stored = {"events": ["push"], "event_filters": {"a": "b"}, "hmac_secret": "s"}
        merged = _merge_trigger_config(stored, {"events": None})
        assert "events" not in merged
        assert "event_filters" in merged
        assert merged["hmac_secret"] == "s"

    def test_multiple_none_removals(self) -> None:
        """Multiple keys can be removed in a single update."""
        stored = {"events": ["push"], "event_filters": {"a": "b"}, "hmac_secret": "s"}
        merged = _merge_trigger_config(stored, {"events": None, "event_filters": None})
        assert "events" not in merged
        assert "event_filters" not in merged
        assert merged["hmac_secret"] == "s"


class TestPostMergeValidation:
    def test_update_leaving_unread_key_in_merged_result_is_rejected(self) -> None:
        """An update that leaves a stored unread key in the merged result
        is rejected by post-merge validation.
        """
        # Simulate: stored config has 'events', incoming does not touch it
        stored = {"events": ["pull_request"], "hmac_secret": "secret"}
        merged = _merge_trigger_config(stored, {"hmac_secret": "new"})
        # merged still contains 'events' — post-merge validation rejects it
        with pytest.raises(HTTPException) as exc_info:
            _validate_trigger_config_keys(merged, context="merged config_json")
        assert exc_info.value.status_code == 400
        assert "events" in exc_info.value.detail
        assert "merged config_json" in exc_info.value.detail

    def test_update_with_none_removal_passes_post_merge_validation(self) -> None:
        """An update that removes the unread key via null passes post-merge
        validation.
        """
        stored = {"events": ["pull_request"], "hmac_secret": "secret"}
        merged = _merge_trigger_config(stored, {"events": None, "hmac_secret": "new"})
        # merged no longer contains 'events' — validation passes
        result = _validate_trigger_config_keys(merged, context="merged config_json")
        assert result is None

    def test_update_introducing_new_unread_key_is_rejected(self) -> None:
        """An update introducing a new unread key is rejected."""
        stored = {"hmac_secret": "secret"}
        merged = _merge_trigger_config(stored, {"events": ["push"]})
        with pytest.raises(HTTPException) as exc_info:
            _validate_trigger_config_keys(merged, context="merged config_json")
        assert "events" in exc_info.value.detail


class TestRecognisedKeysSync:
    def test_triggers_route_and_engine_keys_match(self) -> None:
        """The write-time gate (triggers.py) and load-time gate (engine) use
        the same set of recognised keys.  A key added to one and not the
        other is a bug — the write-time gate rejects a key the engine reads,
        or the engine silently ignores a key the write-time gate accepted.
        """
        assert _RECOGNISED_TRIGGER_CONFIG_KEYS == _ENGINE_KEYS

    def test_engine_keys_match_actual_read_sites(self) -> None:
        """``_RECOGNISED_TRIGGER_CONFIG_KEYS`` matches the engine's *actual*
        ``cfg.get()`` read sites, not just the route-side mirror.

        The equality test above only proves the two hard-coded sets agree with
        each other; this static scan over the trigger-config source files closes
        the drift window in both directions: a key the engine reads but the set
        omits (the write-time gate would reject a legitimate config), and a
        stale key the set keeps but no read site ever touches.
        """
        read_sites = _trigger_config_read_site_keys()
        engine_keys = set(_ENGINE_KEYS)
        assert read_sites == engine_keys, (
            f"engine reads keys missing from _RECOGNISED_TRIGGER_CONFIG_KEYS: {sorted(read_sites - engine_keys)}; "
            f"_RECOGNISED_TRIGGER_CONFIG_KEYS lists keys the engine never reads: {sorted(engine_keys - read_sites)}"
        )


#: The no-delivery-streak config keys the streak engine actually reads —
#: enumerated from ``core/trigger_streak.py`` (FAR-1394), NOT derived from the
#: recognised-key sets (a set-derived list is satisfied while BOTH sets are
#: wrong, which is how every streak setting became DB-write-only):
#:
#: * ``max_no_delivery_streak``       — ``_streak_config`` threshold read
#:   (trigger_streak.py:473)
#: * ``max_consecutive_failures``     — legacy threshold fallback, STILL read
#:   by ``_streak_config``
#:   (trigger_streak.py:475) — the engine honours it, so the write gate must
#:   accept it while that read exists
#: * ``no_delivery_min_window_hours`` — ``_streak_config`` per-trigger
#:   wall-clock window (trigger_streak.py:488)
#: * ``no_delivery_auto_deactivate``  — ``_streak_auto_deactivate_enabled``
#:   (trigger_streak.py:505, constant defined at :128; added by FAR-1387)
_STREAK_CONFIG_KEYS: tuple[tuple[str, object], ...] = (
    ("max_no_delivery_streak", 5),
    ("max_consecutive_failures", 3),
    ("no_delivery_min_window_hours", 24),
    ("no_delivery_auto_deactivate", True),
)


class TestStreakConfigKeysAccepted:
    """FAR-1394 — every config key the streak engine reads passes the write gate.

    The set-equality test above is satisfied while BOTH recognised-key sets are
    wrong, so these tests pin the keys against the engine's own read sites
    (hard-coded from ``core/trigger_streak.py``, independent of the sets):
    each fails with HTTPException 400 if the key is dropped from the sets.
    """

    @pytest.mark.parametrize(("key", "value"), _STREAK_CONFIG_KEYS, ids=[k for k, _ in _STREAK_CONFIG_KEYS])
    def test_streak_key_accepted_by_write_gate(self, key: str, value: object) -> None:
        """The create-time write gate accepts each streak key."""
        assert _validate_trigger_config_keys({key: value}) is None

    @pytest.mark.parametrize(("key", "value"), _STREAK_CONFIG_KEYS, ids=[k for k, _ in _STREAK_CONFIG_KEYS])
    def test_streak_key_accepted_in_merged_config(self, key: str, value: object) -> None:
        """The update-time post-merge gate accepts each streak key too."""
        merged = _merge_trigger_config({"hmac_secret": "secret"}, {key: value})
        assert _validate_trigger_config_keys(merged, context="merged config_json") is None

    @pytest.mark.parametrize("trigger_type", ["ongoing", "cron"])
    def test_streak_config_accepted_and_honoured_for_engine_types(self, trigger_type: str) -> None:
        """Acceptance holds for both trigger types the streak engine covers
        (FAR-190 ongoing, FAR-1387 cron), and the engine consumes the values
        for that type — the gate and the engine can no longer disagree.
        """
        config = {
            "max_no_delivery_streak": 7,
            "no_delivery_min_window_hours": 6,
            "no_delivery_auto_deactivate": True,
        }
        assert _validate_trigger_config_keys(config) is None
        threshold, window = _streak_config(config, trigger_type=trigger_type)
        assert threshold == 7
        assert window == 6
        assert _streak_auto_deactivate_enabled(config) is True

    def test_legacy_threshold_key_still_read_by_engine(self) -> None:
        """``max_consecutive_failures`` stays recognised while the engine's
        legacy fallback read exists (trigger_streak.py:475).
        """
        assert _validate_trigger_config_keys({"max_consecutive_failures": 4}) is None
        threshold, _window = _streak_config({"max_consecutive_failures": 4})
        assert threshold == 4

    def test_misspelled_streak_key_still_rejected(self) -> None:
        """A near-miss streak key is still rejected with 400 (negative control)."""
        with pytest.raises(HTTPException) as exc_info:
            _validate_trigger_config_keys({"max_no_delivery_streek": 5})
        assert exc_info.value.status_code == 400
        assert "max_no_delivery_streek" in exc_info.value.detail
