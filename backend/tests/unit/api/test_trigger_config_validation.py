"""Tests for trigger config_json key validation (FAR-1144).

Covers:
* ``_validate_trigger_config_keys`` (write-time gate): accepted keys pass
  through; unrecognised keys are rejected with a clear 400 listing the
  offending key(s) and the set of recognised keys.
* ``_RECOGNISED_TRIGGER_CONFIG_KEYS`` stays in sync with the engine's
  ``cfg.get()`` read sites — a key added to one and not the other is a bug.
* ``_merge_trigger_config`` with ``None``-removal: ``{"events": null}``
  removes the dead key from the merged result.
* Post-merge validation: an update that would *leave* an unread key in the
  merged result is rejected.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from modulo.api.routes.triggers import (
    _RECOGNISED_TRIGGER_CONFIG_KEYS,
    _merge_trigger_config,
    _validate_trigger_config_keys,
)
from modulo.core.trigger_engine import _RECOGNISED_TRIGGER_CONFIG_KEYS as _ENGINE_KEYS


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
