"""Architecture test: the ``_verify_identity`` patch-leak guard actually fires.

``tests/conftest.py`` installs a root-level autouse guard that fails a test's
setup when a leaked fixture has replaced
``modulo.auth.dependencies._verify_identity`` with anything other than the real
function captured at conftest import.

A guard that cannot fail is not a guard: if the fixture body is disconnected,
the captured reference is taken too late, or the identity comparison is swapped
for equality, every test stays green while the FAR-1631 leak class returns
silently. These tests pin all three:

- the module-level check raises on a replaced symbol and passes on the real one;
- the autouse fixture *body* is wired to that check (``__wrapped__`` reaches the
  raw function behind pytest's fixture wrapper);
- the comparison is by IDENTITY, not ``==`` — an object that is equal to
  everything but is not the real function is still rejected.

``tests/architecture/`` is used deliberately: it is outside ``tests/unit/``, so
the unit-level autouse ``_patch_verify_identity`` (which mocks the same symbol)
is not active here and the real function is present at test start.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

import modulo.auth.dependencies as _auth_dependencies
from tests.conftest import (
    _REAL_VERIFY_IDENTITY,
    _assert_real_verify_identity,
    _guard_real_verify_identity,
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
