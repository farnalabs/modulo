"""Architecture test: the SSO first-org slot may be claimed in one sanctioned place.

FAR-1098. ``_set_default_rls_org`` (``backend/src/modulo/auth/sso.py``)
resolves the app-fallback RLS org as the GLOBAL first row of ``organisations``
(``created_at ASC LIMIT 1``). Every integration test module shares ONE
Postgres, so that first row is a global resource: a module that backdates an
``Organisation`` (the decade offset ``315_360_000``) claims the slot, and
every OTHER module's app-fallback tests then resolve the winner's org and
fail closed (``_SAML_NOT_FOUND``, 404). This exact collision kept the Deploy
pipeline red for ~2 days across three fix attempts (PRs #832/#833/#835, with
#838 the centralising fix).

The guard scans every file under ``backend/tests/integration/`` for
org-backdating claims and enforces:

1. No NEW claimants: every claimant file must be either a ``conftest.py``
   (the sanctioned shared-fixture layer, where PR #838 centralises the
   deterministic first-org fixture) or one of the grandfathered legacy
   claimants whose app-fallback tests currently depend on owning the slot.
2. At most ONE claiming conftest: two shared fixtures fighting for the same
   global slot is the same collision one layer up.
3. The legacy registry stays honest: each registered path must still exist.

Known limitation (conscious scope): the scanner matches the established
``created_at_offset_seconds=`` keyword convention at org-named call sites.
An org backdate that bypasses the convention entirely (e.g. a raw inline
``INSERT INTO organisations`` with a hand-computed ``created_at``) is NOT
detected. Provider backdating (``_create_saml_provider``) is deliberately
not flagged: it claims the legacy ``sso_providers.created_at`` singleton
ordering, a different global resource.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

# backend/tests/architecture/ -> backend/
_BACKEND_ROOT = Path(__file__).resolve().parents[2]
_INTEGRATION_ROOT = _BACKEND_ROOT / "tests" / "integration"

# An org-backdating claim: a call to a helper whose name carries an ``org``
# segment (``_create_org``, ``create_organisation``, ``org_factory``) with a
# nonzero positive ``created_at_offset_seconds`` keyword. ``[^)]*`` spans
# newlines so multi-line fixture calls match; the kwarg must sit inside the
# call's own parentheses (no ``)`` between). Provider helpers such as
# ``_create_saml_provider`` contain no ``org`` segment and never match.
_ORG_BACKDATE_CALL_RE = re.compile(
    r"(?:\b\w*_org\w*|\borg\w*)\s*\([^)]*?created_at_offset_seconds\s*=\s*([0-9][0-9_]*)",
    re.IGNORECASE,
)

# Grandfathered claimants (FAR-1098 legacy): these two modules' app-fallback
# tests currently DEPEND on owning the global first-org slot, so they cannot
# stop claiming until PR #838 centralises the claim into the auth conftest.
# Prune these entries once that lands.
_LEGACY_FIRST_ORG_CLAIMANTS: tuple[str, ...] = (
    "auth/test_saml_rls_resolution.py",
    "auth/test_oidc_rls_resolution.py",
)


def _find_org_backdate_offsets(source: str) -> list[int]:
    """Return the nonzero positive ``created_at_offset_seconds`` literals passed
    to org-named helpers in ``source``.

    Underscore digit separators (``315_360_000``) are normalised before the
    integer parse; zero and negative (future-dated) offsets never claim the
    first-org slot and are excluded.
    """
    offsets: list[int] = []
    for match in _ORG_BACKDATE_CALL_RE.finditer(source):
        value = int(match.group(1).replace("_", ""))
        if value > 0:
            offsets.append(value)
    return offsets


@pytest.fixture(scope="module")
def integration_claimants() -> dict[str, list[int]]:
    """Map each integration file claiming the first-org slot to its offsets.

    The path keys are posix-relative to the integration root. Fails loudly
    when the tree is missing or empty so the guard cannot pass vacuously.
    """
    assert _INTEGRATION_ROOT.is_dir(), f"integration tree missing: {_INTEGRATION_ROOT}"
    files = sorted(_INTEGRATION_ROOT.rglob("*.py"))
    assert files, f"no python files found under {_INTEGRATION_ROOT}"
    claims: dict[str, list[int]] = {}
    for path in files:
        rel = path.relative_to(_INTEGRATION_ROOT).as_posix()
        offsets = _find_org_backdate_offsets(path.read_text(encoding="utf-8", errors="replace"))
        if offsets:
            claims[rel] = offsets
    return claims


def test_first_org_claims_only_in_sanctioned_files(integration_claimants: dict[str, list[int]]) -> None:
    """No integration file outside the sanctioned layers may claim the slot.

    A claiming test module steals the global first-org slot from every other
    module's app-fallback tests on the shared Postgres.
    """
    conftest_claims = {rel for rel in integration_claimants if Path(rel).name == "conftest.py"}
    legacy_claims = {rel for rel in integration_claimants if rel in set(_LEGACY_FIRST_ORG_CLAIMANTS)}
    offenders = sorted(set(integration_claimants) - conftest_claims - legacy_claims)
    assert not offenders, (
        "FAR-1098: new first-org slot claim(s) detected in integration "
        f"fixtures: {offenders}. _set_default_rls_org resolves the GLOBAL "
        "first org (Organisation.created_at ASC LIMIT 1) on ONE shared "
        "Postgres, so a backdated Organisation in a test module steals the "
        "slot from every other module's app-fallback tests (404 "
        "_SAML_NOT_FOUND). Claim the slot only from a shared conftest.py "
        "fixture (see PR #838); if you truly must grandfather another file, "
        "add it to _LEGACY_FIRST_ORG_CLAIMANTS with a written justification."
    )


def test_at_most_one_conftest_first_org_claim(integration_claimants: dict[str, list[int]]) -> None:
    """At most one conftest may claim the slot - two shared fixtures fighting
    for ``Organisation.created_at ASC LIMIT 1`` is the same collision the
    test-module claimants caused, one layer up."""
    conftest_claims = sorted(rel for rel in integration_claimants if Path(rel).name == "conftest.py")
    assert len(conftest_claims) <= 1, (
        "FAR-1098: more than one conftest claims the global first-org slot: "
        f"{conftest_claims}. Keep exactly one deterministic first-org fixture."
    )


def test_legacy_first_org_registry_entries_exist() -> None:
    """Every grandfathered registry entry must still exist on disk, so a
    deleted or renamed claimant cannot leave a silently gameable allowlist
    slot behind."""
    missing = [rel for rel in _LEGACY_FIRST_ORG_CLAIMANTS if not (_INTEGRATION_ROOT / rel).is_file()]
    assert not missing, f"Stale _LEGACY_FIRST_ORG_CLAIMANTS entries (files gone): {missing}. Prune the registry."


def test_scanner_detects_org_backdate_but_not_provider_backdate() -> None:
    """Discrimination proof (in-source, no filesystem): the scanner matches
    the org claim shape (single-line and multi-line) and rejects the provider
    claim shape and zero offsets."""
    org_claim = 'org_a_id, slug = await _create_org(db_engine, "a", created_at_offset_seconds=315_360_000)'
    provider_claim = (
        "saml_a = await _create_saml_provider(db_engine, org_id=org_a_id, created_at_offset_seconds=315_360_000)"
    )
    zero_offset = 'org_b_id, slug = await _create_org(db_engine, "b", created_at_offset_seconds=0)'
    multiline = 'await _create_org(\n    db_engine,\n    "a",\n    created_at_offset_seconds=63_072_000,\n)'

    assert _find_org_backdate_offsets(org_claim) == [315_360_000]
    assert not _find_org_backdate_offsets(provider_claim)
    assert not _find_org_backdate_offsets(zero_offset)
    assert _find_org_backdate_offsets(multiline) == [63_072_000]
