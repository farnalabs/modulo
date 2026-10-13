"""Architecture test: the unit suite's auth-stack autouse gate actually gates.

``tests/unit/conftest.py`` installs two autouse fixtures (``_patch_verify_identity``
and ``_provisioned_system_engine``) whose bodies used to import the auth/app
stack on EVERY unit test — pure-logic packages included. Both now run only
when ``_verify_identity_patch_needed`` says the test can reach that stack.
A gate that cannot fail is not a guard: if the skip path still patched, if
the source-mention scan silently read the wrong module, or if the gate
stopped consulting ``sys.modules``, pure-logic sessions regress to the eager
import — or, worse, an auth-exercising test loses its patch. These tests pin:

- the SKIP path: ``_verify_identity`` is untouched AND the auth module is
  NOT imported as a side effect of the fixture body;
- the PATCH path via a ``sys.modules`` marker (stack already loaded);
- the PATCH path via source mention (the lazy in-body-import shape);
- ``_provisioned_system_engine``'s skip path (its body imports
  ``modulo.api.dependencies``, which executes ``modulo.auth.dependencies``);
- the source-scan helper itself: token read from the module's own file and
  cached per ``(path, token)``.

``tests/architecture/`` is used deliberately: it is outside ``tests/unit/``,
so the unit-level autouse fixtures are not active here — the fixture
functions are invoked directly with a synthetic request node instead.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

import modulo.auth.dependencies as _auth_deps
import tests.unit.conftest as _unit_conftest
from tests.conftest import _REAL_VERIFY_IDENTITY

_FAKE_MODNAME = "gate_probe_mod"


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


def _drop_stack_markers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Simulate a session that has not loaded the auth/app stack yet."""
    for marker in _unit_conftest._AUTH_STACK_MARKERS:
        monkeypatch.delitem(sys.modules, marker, raising=False)


def test_mentions_helper_reads_the_file_and_caches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Token is read from the module's source; results are cached per file."""
    request = _fake_request(tmp_path, monkeypatch, "target = 'modulo.api'\n")

    assert _unit_conftest._test_module_mentions(request.node, "modulo.api")
    assert not _unit_conftest._test_module_mentions(request.node, "modulo.auth")

    registered_file = sys.modules[_FAKE_MODNAME].__file__
    assert _unit_conftest._MENTION_CACHE.get((registered_file, "modulo.api")) is True

    # Cache: rewriting the file must NOT change the answer — a re-read would
    # flip this to False, so the assertion fails if the cache is bypassed.
    (tmp_path / f"{_FAKE_MODNAME}.py").write_text("nothing relevant here\n", encoding="utf-8")
    assert _unit_conftest._test_module_mentions(request.node, "modulo.api")


def test_patch_gate_skips_without_hint_and_does_not_import_auth(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No stack loaded, no token in source -> no patch, no import, no side effect."""
    request = _fake_request(tmp_path, monkeypatch, "value = 1\n")
    _drop_stack_markers(monkeypatch)

    _unit_conftest._patch_verify_identity.__wrapped__(request, monkeypatch)

    assert _auth_deps._verify_identity is _REAL_VERIFY_IDENTITY
    assert _unit_conftest._AUTH_DEPS_MODULE not in sys.modules


def test_patch_gate_patches_when_stack_already_loaded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``sys.modules`` marker alone is sufficient — source need not mention it."""
    request = _fake_request(tmp_path, monkeypatch, "value = 1\n")
    assert _unit_conftest._AUTH_DEPS_MODULE in sys.modules

    _unit_conftest._patch_verify_identity.__wrapped__(request, monkeypatch)

    assert _auth_deps._verify_identity is not _REAL_VERIFY_IDENTITY


def test_patch_gate_patches_on_source_mention_of_lazy_import(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stack not loaded yet, but the source names it -> patch anyway.

    This is the lazy in-body-import shape: the fixture decides BEFORE the
    test body imports the stack, so the source scan is the only signal.
    """
    source = "def go():\n    from modulo.api.main import app\n    return app\n"
    request = _fake_request(tmp_path, monkeypatch, source)
    _drop_stack_markers(monkeypatch)

    _unit_conftest._patch_verify_identity.__wrapped__(request, monkeypatch)

    assert _auth_deps._verify_identity is not _REAL_VERIFY_IDENTITY


def test_provisioned_engine_gate_skips_without_hint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The system-settings stub's body must not import the stack when gated off.

    ``_provisioned_system_engine``'s body does
    ``from modulo.api import dependencies`` — which executes
    ``modulo.auth.dependencies`` — so a skip must leave BOTH markers out of
    ``sys.modules`` (otherwise a pure-logic session regains the import via
    this fixture even with ``_patch_verify_identity`` gated).
    """
    request = _fake_request(tmp_path, monkeypatch, "value = 1\n")
    _drop_stack_markers(monkeypatch)

    _unit_conftest._provisioned_system_engine.__wrapped__(request, monkeypatch)

    assert "modulo.api.dependencies" not in sys.modules
    assert _unit_conftest._AUTH_DEPS_MODULE not in sys.modules


def test_gate_predicate_is_conservative_on_mention(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A comment/docstring mention still counts — false positives are safe."""
    source = "# touches modulo.api in comments only\nvalue = 1\n"
    request = _fake_request(tmp_path, monkeypatch, source)
    _drop_stack_markers(monkeypatch)

    needed = _unit_conftest._verify_identity_patch_needed(request)

    assert needed is True
