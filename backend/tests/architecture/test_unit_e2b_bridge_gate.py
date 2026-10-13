"""Architecture test: the unit suite's e2b-dispatch autouse gate actually gates.

``tests/unit/conftest.py``'s autouse ``_e2b_provider_bridge`` fixture installs
the mock-sandbox bridge over the four ``node_runner`` provider seams. Its body
calls ``install_bridge``, which imports
``modulo.core.pipeline_engine.node_runner`` — a ~4.5 s chain measured
2026-10-13 — and paying that on the FIRST setup of every unit session pinned
pure-logic packages onto the dispatch stack they never touch. The install is
now gated by ``_e2b_bridge_needed``.

A gate that cannot fail is not a guard, so these tests pin:

- the SKIP path: no stack marker, no source token -> ``install_bridge`` never
  runs, and ``node_runner`` is NOT left imported as a side effect (the fixture
  still seeds ``E2B_API_KEY``, which is deliberately ungated);
- the INSTALL path via the ``sys.modules`` marker (dispatch stack already
  loaded — the case where installing is free because the import is paid);
- the INSTALL path via source mention (the lazy in-body-import shape, where
  the fixture decides BEFORE the body imports the dispatch);
- the conservative-widening property (a comment/docstring mention still
  installs — a false positive costs a cached import, a false negative costs a
  live-sandbox attempt);
- the REACHING-SET coverage pin: every ``tests/unit`` module naming a seam
  token (``node_runner`` / ``_sandbox_agent_impl`` / ``install_fake_dispatch``
  / ``fake_file_io`` / the four ``_build_*_provider`` builders) also carries a
  predicate token, so the source scan cannot silently miss a module that
  patches or drives the dispatch.

``tests/architecture/`` is used deliberately: it is outside ``tests/unit/``,
so the unit-level autouse fixtures are not active here — the fixture function
is invoked directly with a synthetic request node instead.
"""

from __future__ import annotations

import os
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import tests.unit.conftest as _unit_conftest

_E2B_FAKE_MODNAME = "e2b_gate_probe_mod"

_UNIT_DIR = Path(__file__).resolve().parents[1] / "unit"


def _fake_request(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str) -> types.SimpleNamespace:
    """A synthetic FixtureRequest whose ``node.obj`` resolves to *source*.

    Registers a throwaway module whose ``__file__`` points at a temp file
    holding *source*, so ``_test_module_mentions`` reads exactly that text.
    Registered through ``monkeypatch.setitem`` so ``sys.modules`` is restored
    at teardown.
    """
    path = tmp_path / f"{_E2B_FAKE_MODNAME}.py"
    path.write_text(source, encoding="utf-8")
    mod = types.ModuleType(_E2B_FAKE_MODNAME)
    mod.__file__ = str(path)
    monkeypatch.setitem(sys.modules, _E2B_FAKE_MODNAME, mod)
    obj = types.SimpleNamespace(__module__=_E2B_FAKE_MODNAME)
    return types.SimpleNamespace(node=types.SimpleNamespace(obj=obj))


def _drop_stack_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    """Simulate a session that has not loaded the dispatch stack yet."""
    monkeypatch.delitem(sys.modules, _unit_conftest._E2B_BRIDGE_MARKER, raising=False)


def test_bridge_gate_skips_without_hint_and_does_not_import_node_runner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No stack loaded, no token in source -> no install, no import, no side effect.

    The real ``install_bridge`` is left in place on purpose: it imports
    ``node_runner`` unconditionally, so ``node_runner`` being absent from
    ``sys.modules`` AFTER the fixture ran is itself the proof that the install
    did not run (no stub could prove that).
    """
    request = _fake_request(tmp_path, monkeypatch, "value = 1\n")
    _drop_stack_marker(monkeypatch)

    _unit_conftest._e2b_provider_bridge.__wrapped__(request, monkeypatch)

    assert _unit_conftest._E2B_BRIDGE_MARKER not in sys.modules
    # The env seed is deliberately ungated (it is not the cost): pinned here
    # so a future "gate the whole body" edit has to update this consciously.
    assert os.environ["E2B_API_KEY"] == "bridge-test-key"
    assert not _unit_conftest._e2b_bridge_needed(request)


def test_bridge_gate_installs_when_stack_already_loaded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``sys.modules`` marker alone is sufficient — source need not mention it.

    This is the case where the dispatch chain is already loaded (module-level
    import in the test module, a conftest, or ``import modulo.api.main``), so
    ``install_bridge``'s import is a cache hit and installing is free.
    """
    request = _fake_request(tmp_path, monkeypatch, "value = 1\n")
    monkeypatch.setitem(sys.modules, _unit_conftest._E2B_BRIDGE_MARKER, types.ModuleType("nr_stub"))
    install_spy = MagicMock()
    monkeypatch.setattr(_unit_conftest, "install_bridge", install_spy)

    _unit_conftest._e2b_provider_bridge.__wrapped__(request, monkeypatch)

    install_spy.assert_called_once()
    assert _unit_conftest._e2b_bridge_needed(request)


def test_bridge_gate_installs_on_source_mention_of_lazy_import(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stack not loaded yet, but the source names it -> install anyway.

    The lazy in-body-import shape: the fixture decides BEFORE the test body
    imports the dispatch, so the source scan is the only signal.
    """
    source = "def go():\n    from modulo.core.pipeline_engine import node_runner\n    return node_runner\n"
    request = _fake_request(tmp_path, monkeypatch, source)
    _drop_stack_marker(monkeypatch)
    install_spy = MagicMock()
    monkeypatch.setattr(_unit_conftest, "install_bridge", install_spy)

    _unit_conftest._e2b_provider_bridge.__wrapped__(request, monkeypatch)

    install_spy.assert_called_once()


def test_bridge_gate_is_conservative_on_mention(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A comment/docstring mention still installs — false positives are safe."""
    source = "# drives the node_runner seams in comments only\nvalue = 1\n"
    request = _fake_request(tmp_path, monkeypatch, source)
    _drop_stack_marker(monkeypatch)

    assert _unit_conftest._e2b_bridge_needed(request) is True


def test_reaching_set_carries_a_predicate_token() -> None:
    """Every ``tests/unit`` module that names a seam token is gate-covered.

    The seam tokens are the reaching set: a module naming one patches, pulls or
    drives the dispatch seams the bridge installs. The predicate's source scan
    only looks for ``pipeline_engine`` / ``node_runner``, so a seam-naming
    module carrying NEITHER token would be skipped by the gate while still
    reaching the dispatch — the under-patch this suite exists to prevent.
    Enumerated 2026-10-13: 100 seam-token modules, all carrying a predicate
    token.
    """
    missing: list[str] = []
    for path in sorted(_UNIT_DIR.rglob("*.py")):
        text = path.read_text(encoding="utf-8", errors="replace")
        if not any(token in text for token in _unit_conftest._E2B_BRIDGE_SEAM_TOKENS):
            continue
        if any(token in text for token in _unit_conftest._E2B_BRIDGE_MENTION_TOKENS):
            continue
        missing.append(str(path.relative_to(_UNIT_DIR.parent.parent)))
    assert not missing, f"seam-token modules without a gate predicate token: {missing}"


def test_seam_token_set_covers_the_seams_the_bridge_actually_patches() -> None:
    """The token set is tied to reality, not to prose.

    ``install_bridge`` patches four ``_build_*_provider`` seams; each of those
    names must be a seam token, or the coverage scan above would happily
    green-light a future dispatch test that patches one of them without a
    predicate token. Likewise ``install_fake_dispatch`` (the conftest's own
    dispatch fake) must stay in the set.
    """
    bridge_source = (_UNIT_DIR / "_e2b_sandbox_bridge.py").read_text(encoding="utf-8", errors="replace")
    conftest_source = (_UNIT_DIR / "pipeline_engine" / "conftest.py").read_text(encoding="utf-8", errors="replace")

    for seam in (
        "_build_dispatch_provider",
        "_build_file_io_provider",
        "_build_log_tail_provider",
        "_build_isolation_provider",
    ):
        assert seam in bridge_source
        assert seam in _unit_conftest._E2B_BRIDGE_SEAM_TOKENS
    assert "install_fake_dispatch" in conftest_source
    assert "install_fake_dispatch" in _unit_conftest._E2B_BRIDGE_SEAM_TOKENS
