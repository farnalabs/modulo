"""FAR-1257: the HITL review-window resolution chain.

Three lenses:

* **Precedence + bounds** — ``resolve_hitl_review_window_seconds`` must return
  ``pipeline_override`` when present, else ``org_default``, else the
  INSTANCE/env default, and every returned value must sit inside the shipped
  60..604800 envelope. There is no "0 = disabled": the safety net is
  unconditional at every layer.
* **Instance-default parity** — with neither override set, the resolved window
  must reproduce the shipped ~75 min (``DEFAULT_EXPIRY_SECONDS`` + the env
  ``hitl_review_cancel_grace_seconds``), and must track a changed env grace.
* **Tolerant org-default read** — ``org_hitl_review_window`` returns None for
  absent/malformed ``settings_json`` instead of raising, and never lets a
  bool/non-int masquerade as a window.
"""

from __future__ import annotations

import pytest

from modulo.core.hitl_manager import DEFAULT_EXPIRY_SECONDS
from modulo.db.crud.hitl_review_config import (
    HITL_REVIEW_WINDOW_MAX_SECONDS,
    HITL_REVIEW_WINDOW_MIN_SECONDS,
    ORG_HITL_REVIEW_WINDOW_KEY,
    org_hitl_review_window,
    resolve_hitl_review_window_seconds,
)
from modulo.settings import get_settings


@pytest.fixture(autouse=True)
def _default_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the env layer so the instance default is deterministic in tests."""
    monkeypatch.setattr(get_settings(), "hitl_review_cancel_grace_seconds", 3600)


class TestPrecedence:
    def test_pipeline_override_wins_over_org_default(self) -> None:
        assert resolve_hitl_review_window_seconds(120, 600) == 120

    def test_org_default_used_when_pipeline_has_no_override(self) -> None:
        assert resolve_hitl_review_window_seconds(None, 600) == 600

    def test_instance_default_when_both_absent(self) -> None:
        assert resolve_hitl_review_window_seconds(None, None) == DEFAULT_EXPIRY_SECONDS + 3600

    def test_non_int_pipeline_override_falls_through_to_org_default(self) -> None:
        assert resolve_hitl_review_window_seconds("nonsense", 600) == 600  # type: ignore[arg-type]

    def test_bool_override_is_not_treated_as_an_int(self) -> None:
        # ``isinstance(True, int)`` is True — a JSON true must not become a
        # 60s window, it must fall through to the next layer.
        assert resolve_hitl_review_window_seconds(True, 600) == 600  # type: ignore[arg-type]

    def test_non_int_org_default_falls_through_to_instance_default(self) -> None:
        assert resolve_hitl_review_window_seconds(None, {"nested": 1}) == DEFAULT_EXPIRY_SECONDS + 3600  # type: ignore[arg-type]


class TestBounds:
    @pytest.mark.parametrize("value", [HITL_REVIEW_WINDOW_MIN_SECONDS, HITL_REVIEW_WINDOW_MAX_SECONDS, 4500])
    def test_in_range_values_pass_through(self, value: int) -> None:
        assert resolve_hitl_review_window_seconds(value, None) == value

    @pytest.mark.parametrize("value", [0, 1, 59, -100])
    def test_below_floor_clamps_to_floor(self, value: int) -> None:
        # 0 is NOT "disabled" — the safety net always resolves to >= 60s.
        assert resolve_hitl_review_window_seconds(value, None) == HITL_REVIEW_WINDOW_MIN_SECONDS

    @pytest.mark.parametrize("value", [604801, 10_000_000])
    def test_above_ceiling_clamps_to_ceiling(self, value: int) -> None:
        assert resolve_hitl_review_window_seconds(value, None) == HITL_REVIEW_WINDOW_MAX_SECONDS

    @pytest.mark.parametrize("pipeline_override", [None, 0, 5, 999_999])
    def test_result_is_always_in_envelope(self, pipeline_override: int | None) -> None:
        for org_default in (None, 0, 7_000_000):
            resolved = resolve_hitl_review_window_seconds(pipeline_override, org_default)
            assert HITL_REVIEW_WINDOW_MIN_SECONDS <= resolved <= HITL_REVIEW_WINDOW_MAX_SECONDS


class TestInstanceDefaultParity:
    def test_default_reproduces_shipped_seventy_five_minutes(self) -> None:
        """Unset everywhere must reproduce today's ~75 min (4500s at defaults)."""
        settings = get_settings()
        assert resolve_hitl_review_window_seconds(None, None) == (
            DEFAULT_EXPIRY_SECONDS + int(settings.hitl_review_cancel_grace_seconds)
        )
        assert resolve_hitl_review_window_seconds(None, None) == 4500

    def test_default_tracks_the_env_grace(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An operator override of the env knob reaches the resolved default."""
        monkeypatch.setattr(get_settings(), "hitl_review_cancel_grace_seconds", 180)
        assert resolve_hitl_review_window_seconds(None, None) == DEFAULT_EXPIRY_SECONDS + 180

    def test_expiry_seconds_constant_is_the_fifteen_minute_claim_ttl(self) -> None:
        # Pins the shipped arithmetic: _DEFAULT_EXPIRY_MINUTES (15) * 60.
        assert DEFAULT_EXPIRY_SECONDS == 900

    def test_unusable_instance_default_falls_back_to_the_envelope_floor(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The instance default is always an in-range int under shipped settings,
        but the floor fallback is the hard guarantee: were the derived default
        ever to become unusable, the resolver must still return an in-envelope
        minimum rather than None/0. Pins that final branch."""
        monkeypatch.setattr(
            "modulo.db.crud.hitl_review_config._instance_default_review_window_seconds",
            lambda: None,
        )
        assert resolve_hitl_review_window_seconds(None, None) == HITL_REVIEW_WINDOW_MIN_SECONDS


class TestOrgDefaultRead:
    @pytest.mark.parametrize("settings_json", [None, "not-a-dict", [], 42])
    def test_non_dict_settings_json_returns_none(self, settings_json: object) -> None:
        assert org_hitl_review_window(settings_json) is None

    def test_absent_key_returns_none(self) -> None:
        assert org_hitl_review_window({"sandbox_concurrency_limit": 4}) is None

    @pytest.mark.parametrize("malformed", ["120", 120.0, True, None, ["120"], {"x": 1}])
    def test_malformed_value_returns_none(self, malformed: object) -> None:
        """Absent/malformed must degrade to 'no org default', never raise."""
        assert org_hitl_review_window({ORG_HITL_REVIEW_WINDOW_KEY: malformed}) is None

    @pytest.mark.parametrize(
        ("value", "expected"),
        [(60, 60), (4500, 4500), (604800, 604800), (1, 60), (999999, 604800)],
    )
    def test_int_value_is_read_and_clamped_into_the_envelope(self, value: int, expected: int) -> None:
        assert org_hitl_review_window({ORG_HITL_REVIEW_WINDOW_KEY: value}) == expected

    def test_other_settings_keys_are_irrelevant(self) -> None:
        settings = {"license_key": "abc", "retention_days": 30}
        assert org_hitl_review_window(settings) is None


class TestConstants:
    def test_envelope_matches_the_shipped_bounds(self) -> None:
        assert HITL_REVIEW_WINDOW_MIN_SECONDS == 60
        assert HITL_REVIEW_WINDOW_MAX_SECONDS == 604800

    def test_settings_key_is_the_documented_one(self) -> None:
        assert ORG_HITL_REVIEW_WINDOW_KEY == "hitl_review_window_seconds"
