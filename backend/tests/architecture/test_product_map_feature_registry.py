"""Architecture test: the product-map feature registry states gaps honestly.

``frontend/src/manifest.yaml`` registers every ``feat-*`` feature behind a
``description`` and, for features whose coverage is mid-flight, an explicit
``status:`` plus a ``behaviours:`` checklist. ADR 008 and
``docs/product-map/README.md`` make that registry the single source of truth for
what the product ships (Remy's ``search_documentation`` indexes each route's
``product_map`` refs, so a behaviour that is silently unchecked is invisible to
the assistant just as much as a missing route).

The quickest way the checklist rots is in the direction nobody tests: a system
that ships a behaviour without ticking it (a feature that is fully covered but
still parked at ``status: partial`` with ``[ ]`` checkboxes), or a checklist that
claims completion while real behaviour is still unchecked (``status: covered``
with ``[ ]`` items). ``test_product_map.py`` pins the reference graph; this
suite pins the honesty of the checklist itself:

- ``status`` is one of ``covered`` / ``partial`` / ``gap``;
- a ``behaviours`` checklist is a non-empty list of ``[x]``/``[ ]`` items;
- no all-checked feature is marked ``partial`` (it is ``covered``);
- no feature with an unchecked item is marked ``covered`` (it is ``partial``
  or ``gap``);
- every feature with a ``status`` other than ``covered`` names its gaps
  explicitly via ``behaviours``/``deferrals`` so the partiality is auditable.
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
MANIFEST_PATH = REPO_ROOT / "frontend" / "src" / "manifest.yaml"

_VALID_STATUSES = frozenset({"covered", "partial", "gap"})
_CHECKED = "[x]"
_UNCHECKED = "[ ]"


def _load_features() -> dict:
    with MANIFEST_PATH.open() as handle:
        data = yaml.safe_load(handle)
    assert isinstance(data, dict), "manifest.yaml root must be a mapping"
    features = data.get("features")
    assert isinstance(features, dict), "manifest.yaml must declare a 'features' mapping"
    assert features, "manifest.yaml must register at least one feature"
    return features


def test_every_feature_has_description():
    blank = {
        feat: spec
        for feat, spec in _load_features().items()
        if not isinstance(spec, dict) or not str(spec.get("description") or "").strip()
    }
    assert not blank, "features without a description (Remy's feature search has nothing to match):\n" + "\n".join(
        f"  {feat} -> {spec!r}" for feat, spec in sorted(blank.items())
    )


def test_status_values_are_controlled():
    bogus = {
        feat: spec["status"]
        for feat, spec in _load_features().items()
        if isinstance(spec, dict) and "status" in spec and spec["status"] not in _VALID_STATUSES
    }
    assert not bogus, (
        "feature 'status' must be one of covered / partial / gap (or omitted when the "
        "feature is a route-described, fully covered surface):\n"
        + "\n".join(f"  {feat} -> {status!r}" for feat, status in sorted(bogus.items()))
    )


def test_behaviours_is_a_non_empty_checked_list():
    bad: dict[str, str] = {}
    for feat, spec in _load_features().items():
        if not isinstance(spec, dict) or "behaviours" not in spec:
            continue
        behaviours = spec["behaviours"]
        if not isinstance(behaviours, list) or not behaviours:
            bad[feat] = "<behaviours must be a non-empty list>"
            continue
        for index, item in enumerate(behaviours):
            if not isinstance(item, str) or not item.startswith((_CHECKED, _UNCHECKED)):
                bad[f"{feat}[{index}]"] = f"{item!r}"
    assert not bad, "each behaviour is a checklist item starting with '[x]' or '[ ]':\n" + "\n".join(
        f"  {key} -> {value}" for key, value in sorted(bad.items())
    )


def test_all_checked_features_are_not_partial():
    """An all-``[x]`` checklist means the listed behaviours ship — mark it covered."""
    stale = []
    for feat, spec in _load_features().items():
        if not isinstance(spec, dict) or spec.get("status") != "partial":
            continue
        behaviours = spec.get("behaviours")
        if behaviours and all(str(item).startswith(_CHECKED) for item in behaviours):
            stale.append(f"  {feat} -> status: partial with every behaviour checked")
    assert not stale, (
        "feature checklist is fully checked but the feature is still parked at "
        "'status: partial' (the gaps it once tracked have shipped):\n" + "\n".join(stale)
    )


def test_partial_or_gap_features_name_their_gaps():
    """A feature declared ``partial``/``gap`` must say why — via unchecked
    behaviours or a ``deferrals`` list. A bare status with nothing unchecked is a
    silent gap: reviewers cannot tell what is missing. Features that omit
    ``status`` entirely are route-described, fully covered surfaces and are not
    subject to this check."""
    silent = []
    for feat, spec in _load_features().items():
        if not isinstance(spec, dict) or spec.get("status") not in {"partial", "gap"}:
            continue
        behaviours = spec.get("behaviours") or []
        deferrals = spec.get("deferrals") or []
        has_unchecked = any(str(item).startswith(_UNCHECKED) for item in behaviours)
        if not has_unchecked and not deferrals:
            silent.append(f"  {feat} -> status={spec.get('status')!r} with no unchecked behaviour or deferral")
    assert not silent, (
        "non-covered features must expose their gaps (unchecked behaviours and/or "
        "deferrals) so partiality is auditable:\n" + "\n".join(silent)
    )


def test_checked_features_cannot_leave_unchecked_items():
    """Reverse of the partiality checks: ``status: covered`` must not coexist
    with an unchecked behaviour item — claiming completion while behaviour is
    still unshipped is the failure mode that hides gaps."""
    contradictory = []
    for feat, spec in _load_features().items():
        if not isinstance(spec, dict) or spec.get("status") != "covered":
            continue
        behaviours = spec.get("behaviours") or []
        if any(str(item).startswith(_UNCHECKED) for item in behaviours):
            contradictory.append(f"  {feat} -> status: covered but behaviour unchecked")
    assert not contradictory, "feature with unchecked behaviour items cannot claim 'status: covered':\n" + "\n".join(
        contradictory
    )
