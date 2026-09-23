"""Tests for the load-time warning on unrecognised trigger config keys (FAR-1144).

Covers ``_warn_unrecognised_config_keys`` in ``modulo.core.trigger_engine``:
* A trigger with only recognised keys emits no warning.
* A trigger with an unrecognised key (e.g. ``events``) emits a WARNING log.
* The warning names the offending key(s) and the set of recognised keys.
"""

from __future__ import annotations

import logging
import uuid

import pytest

from modulo.core.trigger_engine import _warn_unrecognised_config_keys


class TestWarnUnrecognisedConfigKeys:
    def test_recognised_keys_no_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        """Recognised keys produce no warning."""
        trigger_id = uuid.uuid4()
        with caplog.at_level(logging.WARNING, logger="modulo.core.trigger_engine"):
            _warn_unrecognised_config_keys(trigger_id, {"hmac_secret": "s", "accepted_events": ["push"]})
        assert "does not read" not in caplog.text

    def test_unrecognised_key_warns(self, caplog: pytest.LogCaptureFixture) -> None:
        """An unrecognised key triggers a WARNING."""
        trigger_id = uuid.uuid4()
        with caplog.at_level(logging.WARNING, logger="modulo.core.trigger_engine"):
            _warn_unrecognised_config_keys(trigger_id, {"events": ["push"]})
        assert "does not read" in caplog.text
        assert "events" in caplog.text

    def test_warning_names_all_unrecognised_keys(self, caplog: pytest.LogCaptureFixture) -> None:
        """Multiple unrecognised keys are all listed in the warning."""
        trigger_id = uuid.uuid4()
        with caplog.at_level(logging.WARNING, logger="modulo.core.trigger_engine"):
            _warn_unrecognised_config_keys(trigger_id, {"events": ["push"], "filter_by": "branch"})
        assert "events" in caplog.text
        assert "filter_by" in caplog.text

    def test_empty_config_no_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        """Empty config produces no warning."""
        trigger_id = uuid.uuid4()
        with caplog.at_level(logging.WARNING, logger="modulo.core.trigger_engine"):
            _warn_unrecognised_config_keys(trigger_id, {})
        assert "does not read" not in caplog.text

    def test_warning_includes_recognised_keys(self, caplog: pytest.LogCaptureFixture) -> None:
        """The warning message tells the operator what keys ARE valid."""
        trigger_id = uuid.uuid4()
        with caplog.at_level(logging.WARNING, logger="modulo.core.trigger_engine"):
            _warn_unrecognised_config_keys(trigger_id, {"events": ["push"]})
        assert "accepted_events" in caplog.text
        assert "event_filters" in caplog.text
