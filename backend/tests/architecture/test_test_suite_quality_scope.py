"""Tests for the MODULO_TEST_STYLE_SCOPE opt-in scanning behaviour.

Verifies that ``_iter_test_modules()`` honours the env var and that
``_resolve_scope_paths()`` correctly resolves, caches, and filters paths; that
the baseline staleness ratchets (the self-asserting-BDD baseline and the
duplicate-test-body baseline) only judge baseline entries whose module the
active scope actually scanned; and that both ratchets FIRE — raise naming the
stale entry — when given a planted stale baseline entry under no scope.
"""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

# Ensure the architecture package is importable.
_ARCH_DIR = Path(__file__).resolve().parent
if str(_ARCH_DIR) not in sys.path:
    sys.path.insert(0, str(_ARCH_DIR))

from test_test_suite_quality import (  # noqa: E402
    EXCLUDED_PACKAGES,
    TESTS,
    _duplicate_test_body_baseline_candidates,
    _iter_test_modules,
    _read_duplicate_test_body_baseline,
    _resolve_scope_paths,
    _self_asserting_bdd_baseline_candidates,
)
from test_test_suite_quality import (  # noqa: E402
    test_duplicate_test_body_baseline_has_no_stale_entries as _dup_stale_baseline_check,
)
from test_test_suite_quality import (  # noqa: E402
    test_self_asserting_bdd_baseline_has_no_stale_entries as _stale_baseline_check,
)

# Import the wrapper script so its scope-building logic can be unit-tested.
_REPO_ROOT = _ARCH_DIR.parent.parent.parent
_SCRIPTS_DIR = _REPO_ROOT / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import run_test_suite_quality as _script  # noqa: E402

# The checker functions live in (and read their baselines via globals of) the
# scanner module itself, so the firing tests below monkeypatch the READER there
# — patching the name imported into this file would not affect the check.
import test_test_suite_quality as _scanner  # noqa: E402


@pytest.fixture(autouse=True)
def _clear_scope_cache() -> Iterator[None]:
    """Clear the functools.cache on _resolve_scope_paths around every test.

    The scanner caches scope resolution keyed on a one-time env read. Under
    pytest-xdist a stale scoped frozenset could otherwise leak from a scope test
    into a worker sibling (the main scanner tests never clear the cache) and make
    those tests scan only the stale subset — a vacuous pass. Clearing before and
    after each test keeps the cache honest regardless of declaration order.
    """
    _resolve_scope_paths.cache_clear()
    yield
    _resolve_scope_paths.cache_clear()


class TestResolveScopePaths:
    """Unit tests for the scope-resolution helper."""

    def test_unset_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When the env var is absent, resolution returns None (full scan)."""
        monkeypatch.delenv("MODULO_TEST_STYLE_SCOPE", raising=False)
        result = _resolve_scope_paths()
        assert result is None

    def test_empty_string_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An empty env var value means full scan."""
        monkeypatch.setenv("MODULO_TEST_STYLE_SCOPE", "")
        _resolve_scope_paths.cache_clear()
        result = _resolve_scope_paths()
        assert result is None

    def test_whitespace_only_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Whitespace-only entries collapse to nothing → full scan (None)."""
        monkeypatch.setenv("MODULO_TEST_STYLE_SCOPE", "   \t  ")
        _resolve_scope_paths.cache_clear()
        result = _resolve_scope_paths()
        assert result is None

    def test_single_existing_file(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A single real .py file resolves into the scope set."""
        target = TESTS / "architecture" / "test_test_suite_quality.py"
        monkeypatch.setenv("MODULO_TEST_STYLE_SCOPE", str(target))
        _resolve_scope_paths.cache_clear()
        result = _resolve_scope_paths()
        assert result is not None
        assert target.resolve() in result

    def test_nonexistent_file_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A path that does not exist on disk is silently dropped from scope."""
        fake = TESTS / "architecture" / "nonexistent_file_xyz.py"
        monkeypatch.setenv("MODULO_TEST_STYLE_SCOPE", str(fake))
        _resolve_scope_paths.cache_clear()
        result = _resolve_scope_paths()
        assert result is not None
        assert not result

    def test_non_py_file_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A path that exists but is not .py is silently dropped from scope."""
        conftest = TESTS / "conftest.py"
        if conftest.exists():
            # Rename the suffix to something non-.py for the test
            fake = conftest.with_suffix(".txt")
            monkeypatch.setenv("MODULO_TEST_STYLE_SCOPE", str(fake))
            _resolve_scope_paths.cache_clear()
            result = _resolve_scope_paths()
            assert result is not None
            assert not result

    def test_multiple_paths(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """os.pathsep-separated paths are all resolved."""
        file_a = TESTS / "architecture" / "test_test_suite_quality.py"
        file_b = TESTS / "architecture" / "__init__.py"
        combined = os.pathsep.join([str(file_a), str(file_b)])
        monkeypatch.setenv("MODULO_TEST_STYLE_SCOPE", combined)
        _resolve_scope_paths.cache_clear()
        result = _resolve_scope_paths()
        assert result is not None
        assert file_a.resolve() in result
        assert file_b.resolve() in result


class TestIterTestModulesScope:
    """Integration tests for _iter_test_modules() with scope set."""

    def test_full_scan_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Without the env var, _iter_test_modules yields the full set."""
        monkeypatch.delenv("MODULO_TEST_STYLE_SCOPE", raising=False)
        _resolve_scope_paths.cache_clear()
        all_modules = list(_iter_test_modules())
        assert len(all_modules) > 100

    def test_scoped_yields_only_target(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """With scope set to one file, only that file is yielded."""
        target = TESTS / "architecture" / "test_test_suite_quality.py"
        monkeypatch.setenv("MODULO_TEST_STYLE_SCOPE", str(target))
        _resolve_scope_paths.cache_clear()
        scoped = list(_iter_test_modules())
        yielded_paths = [p.resolve() for p in scoped]
        assert target.resolve() in yielded_paths
        # Only one file should be yielded (the target itself)
        assert len(scoped) == 1

    def test_scoped_excludes_others(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Scoped scan must not yield files outside the scope set."""
        target = TESTS / "architecture" / "test_test_suite_quality.py"
        monkeypatch.setenv("MODULO_TEST_STYLE_SCOPE", str(target))
        _resolve_scope_paths.cache_clear()
        scoped = list(_iter_test_modules())
        for path in scoped:
            assert path.resolve() == target.resolve()

    def test_empty_scope_yields_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Scope set to only nonexistent files yields nothing."""
        monkeypatch.setenv("MODULO_TEST_STYLE_SCOPE", "/nonexistent/fake.py")
        _resolve_scope_paths.cache_clear()
        scoped = list(_iter_test_modules())
        assert not scoped

    def test_whitespace_only_yields_full_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Whitespace-only scope collapses to full scan."""
        monkeypatch.setenv("MODULO_TEST_STYLE_SCOPE", "   \t  ")
        _resolve_scope_paths.cache_clear()
        scoped = list(_iter_test_modules())
        assert len(scoped) > 100

    def test_excluded_packages_still_excluded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Even in scoped mode, EXCLUDED_PACKAGES paths are filtered out."""
        # Create a temporary path that looks like it's in an excluded package
        for pkg in EXCLUDED_PACKAGES:
            excluded_dir = TESTS / pkg
            if excluded_dir.is_dir():
                # Find a .py file in there
                py_files = list(excluded_dir.rglob("*.py"))
                if py_files:
                    target = py_files[0]
                    monkeypatch.setenv("MODULO_TEST_STYLE_SCOPE", str(target))
                    _resolve_scope_paths.cache_clear()
                    scoped = list(_iter_test_modules())
                    assert not scoped
                    return
        # If no excluded package dirs exist, test passes vacuously


class TestDeadFixtureLensUnderScope:
    """End-to-end control for the dead-fixture lens in scoped ``--changed-files``
    mode.

    The helper-level regression test in the scanner module
    (``test_dead_fixture_lens_resolves_requesters_across_the_whole_tree``) pins
    the ``_fixture_used_names`` / ``_dead_fixture_violations`` contract, but it
    never drives ``test_no_dead_fixtures`` itself — so a regression in the
    wiring between those helpers and the lens (e.g. feeding the used-name set
    the scoped files only, the original bug) would slip past it. This class
    exercises the real lens end-to-end under ``MODULO_TEST_STYLE_SCOPE``.

    Regression: scoping the scan to a changed ``bdd/conftest.py`` flagged its
    live ``unauth_client`` fixture as dead, because the lens built its "used"
    set from the changed files alone while the requesters live in unchanged
    modules.
    """

    def test_scoped_to_a_conftest_does_not_flag_cross_file_fixture(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Scoped to ``bdd/conftest.py``, the real lens must pass: its live
        ``unauth_client`` is requested only by unchanged modules, so resolving
        requesters from the scoped files alone (the pre-fix wiring) would flag
        it and make this raise."""
        target = TESTS / "bdd" / "conftest.py"
        monkeypatch.setenv("MODULO_TEST_STYLE_SCOPE", str(target))
        _resolve_scope_paths.cache_clear()

        scoped_paths = [p.resolve() for p in _iter_test_modules()]
        assert scoped_paths == [target.resolve()], "the scope must narrow the scan to the changed conftest alone"
        assert "def unauth_client(" in target.read_text(encoding="utf-8"), (
            "regression fixture: bdd/conftest.py must define the cross-file fixture this test relies on"
        )

        # The real assertion: the end-to-end lens must pass. On the pre-fix
        # wiring (used names built from the scoped iteration) this raises,
        # naming unauth_client as a fixture no test requests.
        _scanner.test_no_dead_fixtures()


class TestBuildScopeEnv:
    """Unit tests for the wrapper's scope-building logic (backend/-prefix strip).

    These lock in the bug fix from commit 6d3e9a94: scope_files are repo-relative
    (``backend/tests/...``) but pytest runs with cwd=backend/, so the script must
    strip the leading ``backend/`` prefix or every entry resolves to the bogus
    ``BACKEND/backend/tests/...`` and is silently dropped (vacuous pass).
    """

    def test_strips_backend_prefix(self) -> None:
        """Repo-relative 'backend/tests/...' must strip the backend/ prefix."""
        out = _script._build_scope_env(["backend/tests/architecture/foo.py"])
        assert out == str(_script.BACKEND / "tests" / "architecture" / "foo.py")

    def test_preserves_non_backend_path(self) -> None:
        """Paths already without the backend/ prefix are joined as-is."""
        out = _script._build_scope_env(["tests/foo.py"])
        assert out == str(_script.BACKEND / "tests" / "foo.py")

    def test_resolves_to_real_file(self) -> None:
        """The strip must produce a path that actually exists on disk."""
        rel = "backend/tests/architecture/test_test_suite_quality.py"
        out = _script._build_scope_env([rel])
        assert Path(out).exists()

    def test_scope_has_real_files_detects_missing(self) -> None:
        """The vacuous-pass guard flags a scope with no real files."""
        assert not _script._scope_has_real_files("/nonexistent/fake.py:/also/gone.py")

    def test_scope_has_real_files_detects_present(self) -> None:
        """The guard is satisfied when at least one scope entry exists."""
        rel = "backend/tests/architecture/test_test_suite_quality.py"
        assert _script._scope_has_real_files(_script._build_scope_env([rel]))


class TestBaselineStalenessUnderScope:
    """The stale-baseline ratchet must only judge modules the scoped scan
    actually visited.

    Regression: the ``--changed-files`` wrapper sets ``MODULO_TEST_STYLE_SCOPE``
    to the changed test files, and every baseline entry lives in a BDD step
    module — so a scoped run whose changed set excluded those steps read the
    whole baseline as "no longer violating" and failed with phantom stale
    entries unrelated to the change under review (observed while gating an
    unrelated branch; unscoped the same test passed).

    These tests pin their own baseline fixture through the scanner's reader
    (``_scanner._read_self_asserting_bdd_baseline``) rather than relying on
    real entries: the FAR-1578 sweep has emptied the production baseline, so
    planting an out-of-scope bdd/ entry keeps the filter regression
    deterministically exercised instead of passing vacuously.
    """

    def test_scoped_run_ignores_baseline_entries_it_never_scanned(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Scope = one non-BDD file: no baseline entry is in scope, so both
        the candidate set and the real stale check must come up empty rather
        than reporting the un-scanned entries as phantom-stale."""
        target = TESTS / "architecture" / "test_test_suite_quality.py"
        planted = "bdd/steps/planted_steps.py:test_planted_out_of_scope"
        monkeypatch.setattr(_scanner, "_read_self_asserting_bdd_baseline", lambda: {planted})
        assert _read_baseline() == {planted}
        assert not any(key.startswith("architecture/") for key in _read_baseline()), (
            "regression fixture: the planted baseline lists only bdd/ modules, so this scope excludes them all"
        )
        monkeypatch.setenv("MODULO_TEST_STYLE_SCOPE", str(target))
        _resolve_scope_paths.cache_clear()
        assert not _candidates()
        _stale_baseline_check()  # the real assertion; fails if out-of-scope entries count as stale

    def test_scoped_run_fires_on_scanned_stale_entry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The filter must not blind the ratchet for modules INSIDE the scope:
        a baseline entry whose module is in scope but whose step no longer
        self-asserts (simulated by a planted reader fixture) is still judged
        stale — the candidate set alone proves nothing, so the check itself
        must raise."""
        module = TESTS / "unit" / "api" / "test_csrf.py"
        planted = "unit/api/test_csrf.py:test_not_self_asserting"
        assert planted not in _read_baseline(), "regression fixture: the planted entry must not exist already"
        monkeypatch.setenv("MODULO_TEST_STYLE_SCOPE", str(module))
        _resolve_scope_paths.cache_clear()
        monkeypatch.setattr(_scanner, "_read_self_asserting_bdd_baseline", lambda: {planted})
        # The real assertion: the check itself must raise, naming the planted entry.
        with pytest.raises(AssertionError, match=re.escape("unit/api/test_csrf.py:")) as exc_info:
            _stale_baseline_check()
        assert planted in str(exc_info.value)

    def test_unscoped_run_still_judges_every_baseline_entry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Without a scope the candidate set must be the WHOLE baseline — the
        filter may never blind the tree-wide ratchet. The fixture is planted
        through the scanner's reader so the test does not depend on the
        production baseline being non-empty."""
        planted = {
            "bdd/steps/planted_steps.py:test_planted_one",
            "bdd/steps/planted_steps.py:test_planted_two",
        }
        monkeypatch.setattr(_scanner, "_read_self_asserting_bdd_baseline", lambda: set(planted))
        monkeypatch.delenv("MODULO_TEST_STYLE_SCOPE", raising=False)
        _resolve_scope_paths.cache_clear()
        assert _candidates() == _read_baseline()
        assert _read_baseline() == planted


class TestDuplicateBaselineStalenessUnderScope:
    """Same ratchet discipline for the duplicate-test-body baseline.

    Regression: the scoped ``--changed-files`` wrapper iterates only the changed
    test files, but the stale check compared the TREE-WIDE duplicate baseline
    against duplicates found in the scoped iteration — every out-of-scope
    baseline pair (the whole 36-entry list) read as "no longer a duplicate" and
    the scoped gate failed deterministically on unrelated modules.
    """

    def test_scoped_run_ignores_baseline_entries_it_never_scanned(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Scope = one file holding no baseline duplicate pairs: the candidate
        set must be empty and the real stale check must pass rather than
        reporting all out-of-scope entries."""
        target = TESTS / "architecture" / "test_test_suite_quality_scope.py"
        assert _read_dup_baseline(), "regression fixture: the duplicate baseline must be non-empty"
        assert not any(key.startswith("architecture/") for key in _read_dup_baseline()), (
            "regression fixture: the baseline lists no architecture/ modules, so this scope excludes them all"
        )
        monkeypatch.setenv("MODULO_TEST_STYLE_SCOPE", str(target))
        _resolve_scope_paths.cache_clear()
        assert not _dup_candidates()
        _dup_stale_baseline_check()  # the real assertion; fails if out-of-scope entries count as stale

    def test_scoped_run_passes_for_in_scope_current_pairs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Scope = a module whose baseline pairs are still duplicates: the
        stale check must pass (its entries are found by the scanned tree)."""
        module = TESTS / "unit" / "api" / "test_csrf.py"
        assert any(key.startswith("unit/api/test_csrf.py:") for key in _read_dup_baseline()), (
            "regression fixture: the duplicate baseline must list test_csrf.py entries"
        )
        monkeypatch.setenv("MODULO_TEST_STYLE_SCOPE", str(module))
        _resolve_scope_paths.cache_clear()
        _dup_stale_baseline_check()

    def test_scoped_run_fires_on_in_scope_stale_entry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The filter must not blind the ratchet for modules INSIDE the scope:
        a baseline entry whose module is in scope but whose duplicate pair does
        not exist in the tree (simulated by a planted reader fixture) is still
        judged stale — the scoped gate shrinking the baseline is not how this
        works; the planted entry proves the check itself fires."""
        module = TESTS / "unit" / "api" / "test_csrf.py"
        planted = "unit/api/test_csrf.py:TestNotADuplicatePair:test_not_a_duplicate_a=test_not_a_duplicate_b"
        assert planted not in _read_dup_baseline(), "regression fixture: the planted entry must not exist already"
        monkeypatch.setenv("MODULO_TEST_STYLE_SCOPE", str(module))
        _resolve_scope_paths.cache_clear()
        monkeypatch.setattr(_scanner, "_read_duplicate_test_body_baseline", lambda: {planted})
        # The real assertion: the check itself must raise, naming the planted entry.
        with pytest.raises(AssertionError, match=re.escape("unit/api/test_csrf.py:")) as exc_info:
            _dup_stale_baseline_check()
        assert planted in str(exc_info.value)

    def test_unscoped_run_still_judges_every_duplicate_baseline_entry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Without a scope the candidate set must be the WHOLE baseline — the
        filter may never blind the tree-wide ratchet."""
        monkeypatch.delenv("MODULO_TEST_STYLE_SCOPE", raising=False)
        _resolve_scope_paths.cache_clear()
        assert _dup_candidates() == _read_dup_baseline()
        assert _read_dup_baseline()


def _read_baseline() -> set[str]:
    return _scanner._read_self_asserting_bdd_baseline()


def _candidates() -> set[str]:
    return _self_asserting_bdd_baseline_candidates()


def _read_dup_baseline() -> set[str]:
    return _read_duplicate_test_body_baseline()


def _dup_candidates() -> set[str]:
    return _duplicate_test_body_baseline_candidates()


class TestRatchetsFireOnStaleEntries:
    """Unscoped firing coverage for both stale-baseline ratchets (FAR-1596).

    The regression scope-tests above prove the ratchets DO run in the
    scoped/unscoped gate configurations, but none of them could fail for a
    reader that lets a stale entry survive: every candidate-only or pass-only
    assertion is blind to the ``stale = candidates - keys`` arithmetic being
    silently made empty. These tests hand the ratchet a planted (nonexistent)
    baseline entry under NO scope and assert the check actually raises —
    naming the planted entry — so deleting or neutering the stale-set
    arithmetic fails loudly instead of passing vacuously.
    """

    _PLANTED_BDD = "unit/api/test_csrf.py:test_not_self_asserting"
    _PLANTED_DUP = "unit/api/test_csrf.py:TestNotADuplicatePair:test_not_a_duplicate_a=test_not_a_duplicate_b"

    def test_self_asserting_bdd_ratchet_fires_on_planted_stale_entry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MODULO_TEST_STYLE_SCOPE", raising=False)
        _resolve_scope_paths.cache_clear()
        monkeypatch.setattr(_scanner, "_read_self_asserting_bdd_baseline", lambda: {self._PLANTED_BDD})
        with pytest.raises(AssertionError, match="no longer violate the lens") as exc_info:
            _stale_baseline_check()
        assert self._PLANTED_BDD in str(exc_info.value)

    def test_duplicate_body_ratchet_fires_on_planted_stale_entry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MODULO_TEST_STYLE_SCOPE", raising=False)
        _resolve_scope_paths.cache_clear()
        monkeypatch.setattr(_scanner, "_read_duplicate_test_body_baseline", lambda: {self._PLANTED_DUP})
        with pytest.raises(AssertionError, match="no longer duplicates") as exc_info:
            _dup_stale_baseline_check()
        assert self._PLANTED_DUP in str(exc_info.value)
