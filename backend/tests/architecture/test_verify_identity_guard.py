"""Architecture test: the ``_verify_identity`` patch-leak guard actually fires.

``tests/conftest.py`` installs a root-level autouse guard that fails a test's
setup when a leaked fixture has replaced
``modulo.auth.dependencies._verify_identity`` with anything other than the
real function. The real function is captured LAZILY by an import hook — the
moment the auth module body executes, before any importer can patch it —
never by importing the module at conftest load (that eager import cost every
backend session ~2 s even when no test touches auth).

A guard that cannot fail is not a guard: if the fixture body is disconnected,
the snapshot is taken too late (first observation instead of module exec),
the import hook is removed, or the identity comparison is swapped for
equality, every test stays green while the FAR-1631 leak class returns
silently. These tests pin all of it:

- the module-level check raises on a replaced symbol and passes on the real
  one;
- the autouse fixture *body* is wired to that check (``__wrapped__`` reaches
  the raw function behind pytest's fixture wrapper);
- the comparison is by IDENTITY, not ``==`` — an object that is equal to
  everything but is not the real function is still rejected;
- the snapshot exists at TEST-MODULE COLLECTION time (module-level assert
  below), proving the import hook captured it at import — a first-observation
  capture would still hold ``None`` here;
- a session where the auth module was never imported skips the check at zero
  cost (the pure-logic fast path);
- the snapshot-fallback branch (module predated the hook) captures the
  pristine binding at first observation;
- the finder ignores unrelated imports and wraps only the auth module's
  loader; the loader snapshots after delegating ``exec_module``.

``tests/architecture/`` is used deliberately: it is outside ``tests/unit/``, so
the unit-level autouse ``_patch_verify_identity`` (which mocks the same symbol)
is not active here and the real function is present at test start.
"""

from __future__ import annotations

import sys
import types
from unittest.mock import AsyncMock

import pytest

import modulo.auth.dependencies as _auth_dependencies
import tests.conftest as _root_conftest
from tests.conftest import (
    _AUTH_DEPS_MODULE,
    _REAL_VERIFY_IDENTITY,
    _assert_real_verify_identity,
    _AuthDepsSnapshotFinder,
    _AuthDepsSnapshotLoader,
    _guard_real_verify_identity,
)

# Pinned at COLLECTION time, before any fixture setup has run: the import
# hook must have snapshotted the real function during the auth import above.
# If the hook is removed or never fires, ``_REAL_VERIFY_IDENTITY`` is still
# None here and this module fails to collect — the guard suite cannot go
# green with a dead capture path.
assert _root_conftest._REAL_VERIFY_IDENTITY is not None, (
    "the _verify_identity snapshot must be captured at auth-module import "
    "(import hook), not deferred to the first guard observation"
)

_MISMATCH = "was replaced and not restored"


class _EqualityImpostor:
    """Equal to everything, but not the real function (identity check bait)."""

    def __eq__(self, _other: object) -> bool:
        return True


def _replace_verify_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        _auth_dependencies,
        "_verify_identity",
        AsyncMock(return_value=None),
    )


def test_assert_real_verify_identity_passes_for_the_real_function() -> None:
    assert _auth_dependencies._verify_identity is _REAL_VERIFY_IDENTITY
    _assert_real_verify_identity()


def test_assert_real_verify_identity_raises_for_a_replaced_function(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _replace_verify_identity(monkeypatch)
    with pytest.raises(AssertionError, match=_MISMATCH):
        _assert_real_verify_identity()


def test_assert_real_verify_identity_rejects_an_equality_impostor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Identity, not equality: an object ``==`` the real function is rejected."""
    impostor = _EqualityImpostor()
    monkeypatch.setattr(_auth_dependencies, "_verify_identity", impostor)
    assert impostor == _REAL_VERIFY_IDENTITY
    with pytest.raises(AssertionError, match=_MISMATCH):
        _assert_real_verify_identity()


def test_autouse_fixture_body_is_wired_to_the_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prove the autouse fixture body invokes the check (not just the helper)."""
    _replace_verify_identity(monkeypatch)
    with pytest.raises(AssertionError, match=_MISMATCH):
        _guard_real_verify_identity.__wrapped__()


def test_guard_skips_when_auth_module_was_never_imported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pure-logic fast path: no module in ``sys.modules`` -> zero-cost skip.

    With the module absent there is no symbol any fixture could have
    patched, so the guard must return WITHOUT raising — even though a
    snapshot exists (the module was imported earlier in this session, but
    this simulates the pure-logic session shape).
    """
    assert _AUTH_DEPS_MODULE in sys.modules
    monkeypatch.delitem(sys.modules, _AUTH_DEPS_MODULE)
    _assert_real_verify_identity()  # must not raise


def test_guard_captures_pristine_snapshot_at_first_observation_when_hook_missed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fallback branch: module present, snapshot missing -> capture, no raise.

    Simulates a module that predated the import hook without being caught at
    conftest load. The root autouse guard runs before every fixture, so the
    first observation is pristine; the guard must capture it and pass. (The
    import hook normally makes this branch unreachable — it is belt-and-braces.)
    """
    monkeypatch.setattr(_root_conftest, "_REAL_VERIFY_IDENTITY", None)
    _assert_real_verify_identity()
    assert _root_conftest._REAL_VERIFY_IDENTITY is _auth_dependencies._verify_identity


def test_snapshot_finder_ignores_unrelated_imports() -> None:
    """The meta-path finder must not claim (or wrap) any other module."""
    finder = _AuthDepsSnapshotFinder()
    assert finder.find_spec("os") is None
    assert finder.find_spec("modulo") is None
    assert finder.find_spec("modulo.auth") is None


def test_snapshot_finder_wraps_only_the_auth_module_loader() -> None:
    """On the auth module's name, the finder returns the real spec with a
    snapshot-wrapping loader; it must not execute anything itself.

    ``path`` must be the parent package's ``__path__`` — that is what the
    import system passes for a dotted name (a bare ``find_spec(name)`` only
    resolves top-level modules).
    """
    import modulo.auth as _auth_pkg

    finder = _AuthDepsSnapshotFinder()
    spec = finder.find_spec(_AUTH_DEPS_MODULE, _auth_pkg.__path__)
    assert spec is not None
    assert isinstance(spec.loader, _AuthDepsSnapshotLoader)
    assert spec.loader._wrapped is not None


def test_snapshot_loader_snapshots_after_delegated_exec(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The loader delegates exec to the real loader, then snapshots the binding."""
    monkeypatch.setattr(_root_conftest, "_REAL_VERIFY_IDENTITY", None)

    def sentinel() -> None:
        """Stand-in for the module-body binding of ``_verify_identity``."""

    class _DummyLoader:
        def create_module(self, spec: object) -> None:
            return None

        def exec_module(self, module: types.ModuleType) -> None:
            module._verify_identity = sentinel

    module = types.ModuleType("dummy_auth_deps")
    wrapped = _AuthDepsSnapshotLoader(_DummyLoader())
    assert wrapped.create_module(spec=None) is None
    wrapped.exec_module(module)
    # Snapshot taken AFTER the delegated exec saw the module-body binding.
    assert _root_conftest._REAL_VERIFY_IDENTITY is sentinel
