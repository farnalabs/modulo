"""Architecture test: run the test-suite QA lenses over the repo-root ``tests/`` package.

The sibling ``test_test_suite_quality`` module scans ``backend/tests`` (all ~1.5k
modules) with ~100 AST-scanning QA lenses, but the repo-root ``tests/`` package —
the script-facing tests and the connector conformance harness
(``tests/connectors/``, ``tests/unit/scripts/``) — is a separate tree rooted next
to ``backend/`` that no lens ever reaches. Test-quality regressions there (dead
assertions, unlabelled parametrize matrices, unbounded hangs, module-state leaks,
...) could therefore land silently while the gate stayed green.

This module re-runs the shared enforcement lenses pointed at the repo-root
``tests/`` package. The two trees are disjoint, so re-pointing ``_shared.TESTS``
for the duration of a single test (and restoring it in a ``finally``) cannot
disturb the backend-tests scan in any worker or ordering: pytest executes test
functions sequentially per worker, the mutation window is confined to this one
test, and the ``_parse``/``_all_nodes`` caches are keyed by absolute path, so the
two scans share cache storage without cross-talk.

Under a scoped ``MODULO_TEST_STYLE_SCOPE`` run the wrapper intentionally scans
only changed backend test files, so this module (like the main scanner in scoped
mode) resolves to an empty scan — the full-suite enforcement runs on every CI
architecture-test pass, which is the gate that matters here.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Ensure the architecture package is importable, exactly like the scope-tests
# sibling module.
_ARCH_DIR = Path(__file__).resolve().parent
if str(_ARCH_DIR) not in sys.path:
    sys.path.insert(0, str(_ARCH_DIR))

import test_test_suite_quality as _shared  # noqa: E402

#: Repo-root ``tests/`` package (sibling of ``backend/tests``) that the main
#: scanner never reaches: ``_shared.TESTS`` is ``backend/tests``, so its
#: ``.parent.parent`` is the repo root.
ROOT_TESTS = _shared.TESTS.parent.parent / "tests"

#: Enforcement lenses (``test_no_*``), excluding their ``*_lens_flags_*``
#: self-tests. Reuse the exact set the main suite enforces, so the root package
#: and ``backend/tests`` are held to the same standard.
_ENFORCEMENT_LENSES = sorted(
    name
    for name in dir(_shared)
    if name.startswith("test_no_")
    and callable(getattr(_shared, name))
    and getattr(getattr(_shared, name), "__module__", None) == _shared.__name__
)


def _clear_shared_caches() -> None:
    """Drop the shared per-path parse caches so a re-pointed scan never blends
    stale ``backend/tests`` results into the root-package scan (or vice versa)."""
    _shared._parse.cache_clear()
    _shared._all_nodes.cache_clear()
    _shared._resolve_scope_paths.cache_clear()


def test_root_test_package_passes_all_lenses():
    """Every QA lens must hold for the repo-root ``tests/`` package too."""
    if not ROOT_TESTS.is_dir():
        pytest.fail(f"repo-root tests/ package is missing: {ROOT_TESTS}")
    if not any(ROOT_TESTS.rglob("*.py")):
        pytest.fail(f"repo-root tests/ contains no .py modules to scan: {ROOT_TESTS}")

    original_tests = _shared.TESTS
    _shared.TESTS = ROOT_TESTS
    try:
        _clear_shared_caches()
        failures: dict[str, str] = {}
        for name in _ENFORCEMENT_LENSES:
            try:
                getattr(_shared, name)()
            except AssertionError as exc:
                failures[name] = str(exc)
    finally:
        _shared.TESTS = original_tests
        _clear_shared_caches()

    assert not failures, (
        f"{len(failures)} QA lens(es) flag the repo-root tests/ package;\n"
        "fix the violations listed (or the lens needs a documenting exception "
        "before it belongs in the shared module).\n\n"
        + "\n".join(f"===== {name} =====\n{message}" for name, message in failures.items())
    )
