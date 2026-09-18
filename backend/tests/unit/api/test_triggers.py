"""Unit tests for the trigger config merge (masked round-trip guard).

Covers :func:`modulo.api.routes.triggers._merge_trigger_config`, which merges a
PATCH ``config_json`` into the stored trigger config without persisting the DOM
mask. The trigger GET emits a RECURSIVE mask (``mask_config_json`` on nested
``headers`` / ``operations`` / list values), so the PATCH merge must skip masked
echoes at every depth — not just top-level exact-equality.
"""

from modulo.api.middleware.sensitive_mask import SENSITIVE_VALUE_MASK
from modulo.api.routes.triggers import _merge_trigger_config


class TestMergeTriggerConfigMaskRoundTrip:
    def test_nested_masked_value_does_not_overwrite_stored(self) -> None:
        """A nested masked value (``headers.Authorization``) must NOT clobber the
        stored secret on a read-modify-write round-trip.

        Regression for the major finding: the old merge was top-level only and
        used exact equality, so a ``'Bearer ••••••'`` PATCH-back wholesale
        replaced the nested stored dict.
        """
        stored = {"headers": {"Authorization": "Bearer real-secret"}, "poll_query": "SELECT 1"}
        incoming = {"headers": {"Authorization": f"Bearer {SENSITIVE_VALUE_MASK}"}}
        result = _merge_trigger_config(stored, incoming)
        assert result["headers"]["Authorization"] == "Bearer real-secret"
        assert result["poll_query"] == "SELECT 1"

    def test_nested_masked_list_value_does_not_overwrite_stored(self) -> None:
        """A list under a nested key with masked elements preserves the stored list."""
        stored = {"forwarding": {"tokens": ["real-A", "real-B"]}}
        incoming = {"forwarding": {"tokens": [SENSITIVE_VALUE_MASK, SENSITIVE_VALUE_MASK]}}
        result = _merge_trigger_config(stored, incoming)
        assert result["forwarding"]["tokens"] == ["real-A", "real-B"]

    def test_top_level_masked_value_does_not_overwrite_stored(self) -> None:
        stored = {"hmac_secret": "real-hmac"}
        incoming = {"hmac_secret": SENSITIVE_VALUE_MASK}
        result = _merge_trigger_config(stored, incoming)
        assert result["hmac_secret"] == "real-hmac"

    def test_new_value_overwrites_stored(self) -> None:
        """A genuinely new (non-masked) value still writes through."""
        stored = {"poll_query": "SELECT 1"}
        incoming = {"poll_query": "SELECT 2"}
        result = _merge_trigger_config(stored, incoming)
        assert result["poll_query"] == "SELECT 2"

    def test_none_clears_key(self) -> None:
        """An explicit ``None`` clears the key; sibling keys stay intact."""
        stored = {"poll_query": "SELECT 1", "keep": True}
        incoming = {"poll_query": None}
        result = _merge_trigger_config(stored, incoming)
        assert "poll_query" not in result
        assert result["keep"] is True

    def test_missing_key_leaves_it_intact(self) -> None:
        stored = {"poll_query": "SELECT 1"}
        incoming = {"active": True}
        result = _merge_trigger_config(stored, incoming)
        assert result["poll_query"] == "SELECT 1"
        assert result["active"] is True
