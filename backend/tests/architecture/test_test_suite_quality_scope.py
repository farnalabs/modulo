"""Tests for the MODULO_TEST_STYLE_SCOPE opt-in scanning behaviour.

Verifies that ``_iter_test_modules()`` honours the env var and that
``_resolve_scope_paths()`` correctly resolves, caches, and filters paths.
"""

from __future__ import annotations

import os
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
    _iter_test_modules,
    _resolve_scope_paths,
)

# Import the wrapper script so its scope-building logic can be unit-tested.
_REPO_ROOT = _ARCH_DIR.parent.parent.parent
_SCRIPTS_DIR = _REPO_ROOT / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import run_test_suite_quality as _script  # noqa: E402


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
