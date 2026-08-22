"""Signed team license activates every team-tier feature flag.

Signs a team license via ``generate_team_license``, resolves it to a
``LicenseKeyTier`` plan context, and asserts every ``team``-tier flag in the
catalog is active. This catches drift between the explicit ``TEAM_FEATURES``
list and the tier-rank mechanism: a valid team license must activate the whole
paid tier, not only the three flags named in ``TEAM_FEATURES``.
"""

from __future__ import annotations

from collections.abc import Generator

import pytest

from modulo.core.feature_flags import _KNOWN_FLAGS, LicenseKeyTier
from modulo.core.license import _LICENSE_PUBLIC_KEY_HEX, parse_and_verify, set_public_key
from modulo.core.license_signing import generate_team_license
from modulo.core.registry.crypto import generate_keypair

_ORIGINAL_KEY = _LICENSE_PUBLIC_KEY_HEX


@pytest.fixture(autouse=True)
def _reset_public_key() -> Generator[None, None, None]:
    yield
    set_public_key(_ORIGINAL_KEY)


def _team_flag_names() -> list[str]:
    return [flag.name for flag in _KNOWN_FLAGS if flag.tier == "team"]


def test_signed_team_license_activates_all_team_flags() -> None:
    kp = generate_keypair()
    set_public_key(kp["public_key"])

    key = generate_team_license("Parity Customer", private_key_hex=kp["private_key"])
    validation = parse_and_verify(key)
    assert validation.valid is True
    assert validation.license_data is not None
    assert validation.license_data.tier == "team"

    plan = LicenseKeyTier(validation.license_data)
    for name in _team_flag_names():
        assert plan.feature_enabled(name), (
            f"Team license did not activate team-tier flag '{name}'. "
            "The tier-rank mechanism or license resolution regressed."
        )


def test_team_license_tier_rank_activates_beyond_explicit_features() -> None:
    """The tier-rank, not the explicit features list, is the source of team access.

    The signed license's explicit features list (``TEAM_FEATURES``) is only a
    subset of the full team tier. A team license must still activate every
    team flag via tier ranking.
    """
    kp = generate_keypair()
    set_public_key(kp["public_key"])

    key = generate_team_license("Parity Customer", private_key_hex=kp["private_key"])
    validation = parse_and_verify(key)
    assert validation.valid is True
    assert validation.license_data is not None

    # Confirm the explicit features list does NOT already contain every team flag,
    # so the tier-rank mechanism is genuinely what grants the rest.
    explicit = set(validation.license_data.features)
    all_team = set(_team_flag_names())
    assert explicit < all_team, "TEAM_FEATURES already enumerates the whole team tier - drift guard is vacuous"

    plan = LicenseKeyTier(validation.license_data)
    for name in all_team:
        assert plan.feature_enabled(name)
