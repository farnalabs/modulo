"""FAR-1764: the set of connector types the hub can actually build.

``GET /api/v1/connectors/types`` offers ``buildable_connector_type_ids()`` so
the Admin UI only lists types ``_build_connector`` can instantiate. These tests
keep that advertised set honest in both directions:

* every ``case`` arm in ``_build_connector``'s source is in
  ``_HUB_BUILT_TYPE_IDS`` AND vice versa (source-shape pin);
* every advertised id actually CONSTRUCTS a connector (behavioural pin, using
  the shared build-case matrix from ``_fixtures.BUILD_CASES``);
* the enum's abstract labels (``ci-runner``, ``custom``) — which have no hub
  arm — stay out of the advertised set, while the hub-native CI-runner ids
  stay in it.
"""

import inspect
import re
from typing import Any
from unittest.mock import patch

import pytest

from modulo.core.connector_hub import (
    _HUB_BUILT_TYPE_IDS,
    _build_connector,
    buildable_connector_type_ids,
)
from modulo.core.plugin_registry import PluginManifest, PluginRegistry
from tests.unit.connector_hub._fixtures import BUILD_CASES

# ShellConnector is deprecated (ADR 044) and warns on construction.
pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

# Build fixtures for the hub-arm ids that are NOT in the shared
# ``BUILD_CASES`` matrix (rest needs a base_url + auth-mode creds;
# ticket-tracker needs a provider; linear simply has no matrix row). Combined
# with the matrix, this covers every id in ``_HUB_BUILT_TYPE_IDS``.
_EXTRA_BUILD_FIXTURES: list[tuple[str, dict[str, Any], dict[str, Any]]] = [
    ("rest", {"base_url": "https://api.example.com"}, {"auth_mode": "bearer", "token": "rest_token"}),
    ("ticket-tracker", {"provider": "github"}, {"token": "tt_token"}),
    ("linear", {}, {"token": "linear_token"}),
]
_BUILD_FIXTURES: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {
    row[0]: (row[1], row[2]) for row in [*BUILD_CASES, *_EXTRA_BUILD_FIXTURES]
}


def _match_arm_ids() -> set[str]:
    """The ``case "<id>"`` literals in ``_build_connector``'s source.

    ``match`` offers no runtime introspection, so the source is read directly.
    Comment lines are dropped first so prose that merely MENTIONS ``case "x"``
    cannot be mistaken for an arm.
    """
    raw = inspect.getsource(_build_connector)
    code = "\n".join(line for line in raw.splitlines() if not line.lstrip().startswith("#"))
    return set(re.findall(r'^\s*case\s+"([^"]+)"', code, re.MULTILINE))


def test_hub_built_type_ids_match_build_connector_case_arms() -> None:
    """``_HUB_BUILT_TYPE_IDS`` mirrors the ``case "<id>"`` arms, exactly.

    The constant is the advertised set; this pin stops the two drifting —
    adding a hub arm without updating the constant (or vice versa) fails here.
    """
    assert _match_arm_ids() == set(_HUB_BUILT_TYPE_IDS)


def test_every_hub_built_type_id_actually_builds() -> None:
    """Every advertised id constructs a connector (not just parses as an arm)."""
    for type_id in sorted(_HUB_BUILT_TYPE_IDS):
        fixture = _BUILD_FIXTURES.get(type_id)
        assert fixture is not None, f"no build fixture for hub type {type_id!r} — add a row to _BUILD_FIXTURES"
        config, creds = fixture
        assert _build_connector(type_id, config, creds) is not None


@pytest.mark.parametrize("type_id", ["ci-runner", "custom"])
def test_unbuildable_enum_labels_are_not_advertised(type_id: str) -> None:
    """``ci-runner`` / ``custom`` are enum labels with no hub arm — the hub
    raises ``Unknown connector type`` for both, so neither may be offered."""
    assert type_id not in buildable_connector_type_ids()
    with pytest.raises(ValueError, match="Unknown connector type"):
        _build_connector(type_id, {}, {})


@pytest.mark.parametrize("type_id", ["github_actions_ci", "gitlab_ci", "woodpecker"])
def test_hub_native_ci_runner_ids_are_advertised(type_id: str) -> None:
    """The creatable CI-runner connector ids stay in the offered set —
    dropping one would re-open FAR-1764 (no way to create a CI connector)."""
    assert type_id in _HUB_BUILT_TYPE_IDS
    assert type_id in buildable_connector_type_ids()


def test_buildable_ids_include_plugin_registered_types() -> None:
    """The plugin-registry fallback is part of what the hub can build, so
    plugin-registered ids are offered too."""
    registry = PluginRegistry()
    registry.register_connector_type(
        "my_custom_connector",
        lambda config, creds: None,
        PluginManifest(PLUGIN_ID="pkg-buildable-test", display_name="Demo", description="", version="1"),
    )
    with patch("modulo.core.connector_hub.get_plugin_registry", return_value=registry):
        assert "my_custom_connector" in buildable_connector_type_ids()
