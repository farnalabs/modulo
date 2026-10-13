"""Architecture test: the unit suite's audit-session-double autouse gate actually gates.

``tests/unit/conftest.py``'s ``_isolated_audit_session`` installs a double over
``modulo.core.audit_coverage._shared_session_factory`` through a STRING
``monkeypatch.setattr`` target — resolving it imports the audit/app stack
(fastapi + sqlalchemy + db.models, ~1.8-3.8 s measured 2026-10-13) on EVERY
unit test, including pure-logic packages that never touch an ``audited(...)``
route. The fixture now runs only when ``_audit_session_double_needed`` says the
test can reach that surface. A gate that cannot fail is not a guard: if the
skip path still patched, if the source-mention scan read the wrong module, or
if the gate stopped consulting ``sys.modules``, pure-logic sessions regress to
the eager import — or, worse, an audit-exercising test LOSES its double and
silently dials the real shared engine (a fail-open append logs-and-continues;
on a live Postgres it writes a real audit row). These tests pin:

- the SKIP path: the factory is untouched AND the audit module is NOT
  imported as a side effect of the fixture body — asserted while the fixture
  generator is suspended at its ``yield`` (closing first unwinds the install,
  so a removed gate would leave the factory restored and read as a false pass);
- the fixture body actually CONSULTS the gate (a spy replaces
  ``_audit_session_double_needed``), so dropping the gate call fails outright;
- the PATCH path via a ``sys.modules`` marker (surface already loaded);
- the PATCH path via source mention (the lazy in-body-import shape);
- the PATCH path's teardown: closing the fixture generator restores the
  real factory (the ``monkeypatch.context()`` ordering contract);
- the conservative-comment-mention shape still installs (false positives
  are safe).

``tests/architecture/`` is used deliberately: it is outside ``tests/unit/``,
so the unit-level autouse fixtures are not active here — the fixture
functions are invoked directly with a synthetic request node instead.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

import modulo.core.audit_coverage as _audit_coverage
import tests.unit.conftest as _unit_conftest

_FAKE_MODNAME = "gate_audit_probe_mod"

#: The real factory captured at import time, before any test patches it.
_REAL_FACTORY = _audit_coverage._shared_session_factory


def _fake_request(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str) -> types.SimpleNamespace:
    """A synthetic FixtureRequest whose ``node.obj`` resolves to *source*.

    Registers a throwaway module named ``_FAKE_MODNAME`` whose ``__file__``
    points at a temp file with the given source, so
    ``_test_module_mentions`` reads exactly that text. Registered through
    ``monkeypatch.setitem`` so ``sys.modules`` is restored at teardown.
    """
    path = tmp_path / f"{_FAKE_MODNAME}.py"
    path.write_text(source, encoding="utf-8")
    mod = types.ModuleType(_FAKE_MODNAME)
    mod.__file__ = str(path)
    monkeypatch.setitem(sys.modules, _FAKE_MODNAME, mod)
    obj = types.SimpleNamespace(__module__=_FAKE_MODNAME)
    return types.SimpleNamespace(node=types.SimpleNamespace(obj=obj))


def _drop_audit_markers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Simulate a session that has not loaded the audited surface yet."""
    for marker in _unit_conftest._AUDIT_SURFACE_MARKERS:
        monkeypatch.delitem(sys.modules, marker, raising=False)


def test_gate_skips_without_hint_and_does_not_import_audit_stack(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No surface loaded, no token in source -> no patch, no import, no side effect.

    The asserts run INSIDE the fixture-body window — while the generator is
    suspended at its ``yield`` — NOT after ``gen.close()``. Closing first
    unwinds the ``monkeypatch.context()`` install, so a fixture with the gate
    removed would leave the factory restored and this test would still pass;
    observing the state the fixture actually installs for the test body is
    what makes the pin discriminating. (The ``sys.modules`` probe alone could
    not: string ``monkeypatch.setattr`` resolves each dotted part through
    ``getattr`` on the already-imported parent package, so the ungated install
    never re-imports the module it patches.)
    """
    request = _fake_request(tmp_path, monkeypatch, "value = 1\n")
    _drop_audit_markers(monkeypatch)

    gen = _unit_conftest._isolated_audit_session.__wrapped__(request, monkeypatch)
    next(gen)  # skip path yields once without installing
    try:
        assert _audit_coverage._shared_session_factory is _REAL_FACTORY
        assert "modulo.core.audit_coverage" not in sys.modules
    finally:
        gen.close()


def test_isolated_audit_session_consults_the_gate_predicate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fixture body must CONSULT ``_audit_session_double_needed``.

    Pins the predicate's USE directly: a spy replaces the predicate and the
    fixture is driven through it, so dropping the gate call from the fixture
    body leaves the spy unconsulted and fails here — independent of the
    monkeypatch string-resolution path the identity assert leans on. Forcing
    the spy True also drives the install path through the fixture's own gate,
    so the assertion below proves the gate's True branch patches.
    """
    request = _fake_request(tmp_path, monkeypatch, "value = 1\n")
    _drop_audit_markers(monkeypatch)
    consulted: list[object] = []

    def _spy(node: object) -> bool:
        consulted.append(node)
        return True

    monkeypatch.setattr(_unit_conftest, "_audit_session_double_needed", _spy)

    gen = _unit_conftest._isolated_audit_session.__wrapped__(request, monkeypatch)
    next(gen)
    try:
        assert consulted == [request]
        assert _audit_coverage._shared_session_factory is not _REAL_FACTORY
    finally:
        gen.close()
    assert _audit_coverage._shared_session_factory is _REAL_FACTORY


def test_gate_patches_when_surface_already_loaded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``sys.modules`` marker alone is sufficient — source need not mention it."""
    request = _fake_request(tmp_path, monkeypatch, "value = 1\n")
    assert "modulo.core.audit_coverage" in sys.modules

    gen = _unit_conftest._isolated_audit_session.__wrapped__(request, monkeypatch)
    next(gen)
    try:
        assert _audit_coverage._shared_session_factory is not _REAL_FACTORY
    finally:
        gen.close()


def test_gate_patches_on_source_mention_of_lazy_import(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Surface not loaded yet, but the source names it -> patch anyway.

    This is the lazy in-body-import shape: the fixture decides BEFORE the
    test body imports the surface, so the source scan is the only signal.
    """
    source = "def go():\n    from modulo.api.main import app\n    return app\n"
    request = _fake_request(tmp_path, monkeypatch, source)
    _drop_audit_markers(monkeypatch)

    gen = _unit_conftest._isolated_audit_session.__wrapped__(request, monkeypatch)
    next(gen)
    try:
        assert _audit_coverage._shared_session_factory is not _REAL_FACTORY
    finally:
        gen.close()


def test_gate_teardown_restores_real_factory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Closing the fixture generator exits the ``monkeypatch.context()``.

    The audit-order rationale (unpatch at fixture teardown, not mid-
    finalization) depends on this: the double must be live for the whole
    test body and gone once the generator closes.
    """
    request = _fake_request(tmp_path, monkeypatch, "value = 1\n")
    assert "modulo.core.audit_coverage" in sys.modules

    gen = _unit_conftest._isolated_audit_session.__wrapped__(request, monkeypatch)
    next(gen)
    assert _audit_coverage._shared_session_factory is not _REAL_FACTORY
    gen.close()
    assert _audit_coverage._shared_session_factory is _REAL_FACTORY


def test_gate_predicate_is_conservative_on_mention(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A comment/docstring mention still counts — false positives are safe."""
    source = "# touches modulo.api in comments only\nvalue = 1\n"
    request = _fake_request(tmp_path, monkeypatch, source)
    _drop_audit_markers(monkeypatch)

    needed = _unit_conftest._audit_session_double_needed(request)

    assert needed is True
