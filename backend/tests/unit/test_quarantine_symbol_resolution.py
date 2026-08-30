"""Unit tests for the ``.quarantine.yml`` symbol-resolution helpers.

These lock in the behaviour introduced alongside the quarantine registry safety
net in ``tests/architecture/test_test_suite_safety_nets.py``. The real repo
``.quarantine.yml`` holds only commented examples, so the lens iterates an empty
list and the symbol-resolution code paths (``_stripped_symbol`` /
``_collect_definitions`` / ``_resolves_definitions`` / ``_resolve_quarantine_target``
/ ``_is_bdd_scenario_module``) never run in CI. A regression such as
``_collect_definitions`` returning ``set()`` or ``_resolves_definitions`` always
returning ``True`` would be silently invisible — these tests exercise the helpers
against synthetic temp modules so that can no longer happen.

They also prove the improvement over a file-existence-only check: a registry
entry pointing at a real file but a renamed/missing symbol is now FLAGGED, which
the previous check (file existence alone) would have let through.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import yaml

from tests.architecture import test_test_suite_safety_nets as sns


def _write_module(path: Path, body: str) -> None:
    path.write_text(textwrap.dedent(body))


# --- _stripped_symbol -------------------------------------------------------


def test_stripped_symbol_strips_per_segment():
    # Finding 3: the whole-nodeid strip previously dropped the method component
    # of a parametrized class nodeid, degrading Class::method to a bare class.
    assert sns._stripped_symbol("test_foo[abc]") == "test_foo"
    assert sns._stripped_symbol("TestFoo[param]::test_bar") == "TestFoo::test_bar"
    assert sns._stripped_symbol("TestFoo::test_bar[param]") == "TestFoo::test_bar"
    assert sns._stripped_symbol("a::b::c[d]") == "a::b::c"


# --- _collect_definitions ---------------------------------------------------


def test_collect_definitions_module_level_functions(tmp_path: Path) -> None:
    mod = tmp_path / "test_x.py"
    _write_module(
        mod,
        """
        def test_alpha(): ...
        def test_beta(): ...
        def helper(): ...
        """,
    )
    syms = sns._collect_definitions(mod)
    assert "test_alpha" in syms
    assert "test_beta" in syms
    assert "helper" not in syms


def test_collect_definitions_class_methods(tmp_path: Path) -> None:
    mod = tmp_path / "test_cls.py"
    _write_module(
        mod,
        """
        class TestFoo:
            def test_one(self): ...
            def test_two(self): ...
        class TestBar:
            def test_three(self): ...
        """,
    )
    syms = sns._collect_definitions(mod)
    assert "TestFoo" in syms
    assert "TestFoo::test_one" in syms
    assert "TestFoo::test_two" in syms
    assert "TestBar::test_three" in syms


def test_collect_definitions_missing_file_is_empty(tmp_path: Path) -> None:
    assert not sns._collect_definitions(tmp_path / "nope.py")


# --- _resolves_definitions --------------------------------------------------


def test_resolves_definitions_bare_function() -> None:
    syms = {"test_alpha", "TestFoo"}
    # bare function resolves
    assert sns._resolves_definitions("test_alpha", syms) is True
    # bare class reference MUST be rejected (pytest never emits file.py::TestFoo)
    assert sns._resolves_definitions("TestFoo", syms) is False


def test_resolves_definitions_class_method() -> None:
    syms = {"TestFoo", "TestFoo::test_one"}
    assert sns._resolves_definitions("TestFoo::test_one", syms) is True
    # renamed method -> class present but exact method missing
    assert sns._resolves_definitions("TestFoo::test_renamed", syms) is False
    # missing class
    assert sns._resolves_definitions("TestMissing::test_one", syms) is False


def test_resolves_definitions_parametrized() -> None:
    assert sns._resolves_definitions("test_alpha[abc]", {"test_alpha"}) is True
    assert sns._resolves_definitions("TestFoo::test_one[param]", {"TestFoo", "TestFoo::test_one"}) is True


def test_resolves_definitions_exact_membership_three_segments() -> None:
    # Finding 5: containment must not match a partial substring.
    syms = {"TestFoo::TestCaseKind::nodes"}
    assert sns._resolves_definitions("TestFoo::TestCaseKind::nodes", syms) is True
    assert sns._resolves_definitions("TestFoo::TestCaseKind::node", syms) is False


# --- _resolve_quarantine_target ---------------------------------------------


def test_resolve_quarantine_target_rejects_empty_path(tmp_path: Path) -> None:
    # Finding 4: a degenerate '::test_foo' resolved to a directory before.
    path, test_part, error = sns._resolve_quarantine_target("::test_foo", backend=tmp_path, repo=tmp_path)
    assert error
    assert path is None
    assert test_part is None


def test_resolve_quarantine_target_rejects_directory(tmp_path: Path) -> None:
    (tmp_path / "pkg").mkdir()
    path, _test_part, error = sns._resolve_quarantine_target("pkg::test_x", backend=tmp_path, repo=tmp_path)
    assert error
    assert path is None


def test_resolve_quarantine_target_missing_separator(tmp_path: Path) -> None:
    _path, _test_part, error = sns._resolve_quarantine_target("tests/x.py", backend=tmp_path, repo=tmp_path)
    assert error


def test_resolve_quarantine_target_resolves_real_file(tmp_path: Path) -> None:
    mod = tmp_path / "test_thing.py"
    mod.write_text("def test_ok(): ...\n")
    path, test_part, error = sns._resolve_quarantine_target("test_thing.py::test_ok", backend=tmp_path, repo=tmp_path)
    assert error == ""
    assert path == mod
    assert test_part == "test_ok"


# --- _is_bdd_scenario_module ------------------------------------------------


def test_is_bdd_scenario_module_detects_scenarios_call(tmp_path: Path) -> None:
    mod = tmp_path / "test_bdd_steps.py"
    _write_module(
        mod,
        """
        from pytest_bdd import scenarios, given
        scenarios("*.feature")

        @given("a thing")
        def _(): ...
        """,
    )
    assert sns._is_bdd_scenario_module(mod) is True


def test_is_bdd_scenario_module_false_for_plain_module(tmp_path: Path) -> None:
    mod = tmp_path / "test_plain.py"
    _write_module(mod, "def test_alpha(): ...\n")
    assert sns._is_bdd_scenario_module(mod) is False


# --- lens-level behaviour via _quarantine_violations ------------------------


def _write_registry(path: Path, entries: list[dict]) -> None:
    path.write_text(yaml.safe_dump({"quarantine": entries}))


def test_lens_flags_renamed_symbol(tmp_path: Path) -> None:
    # Real file, but the symbol was renamed -> must be flagged. This is the
    # dead-safety-net case; a file-existence-only check would have missed it.
    mod = tmp_path / "test_thing.py"
    _write_module(mod, "def test_current(): ...\n")
    qfile = tmp_path / ".quarantine.yml"
    _write_registry(
        qfile,
        [{"test_id": "test_thing.py::test_renamed", "reason": "flaky", "expiry": "2099-01-01"}],
    )
    violations = sns._quarantine_violations(qfile, tmp_path, tmp_path)
    assert any("test_thing.py::test_renamed" in v and "not found" in v for v in violations)


def test_lens_passes_for_valid_symbol(tmp_path: Path) -> None:
    mod = tmp_path / "test_thing.py"
    _write_module(mod, "def test_current(): ...\n")
    qfile = tmp_path / ".quarantine.yml"
    _write_registry(
        qfile,
        [{"test_id": "test_thing.py::test_current", "reason": "flaky", "expiry": "2099-01-01"}],
    )
    violations = sns._quarantine_violations(qfile, tmp_path, tmp_path)
    assert not any("test_thing.py::test_current" in v for v in violations)


def test_lens_skips_bdd_symbol_validation(tmp_path: Path) -> None:
    # Finding 2: BDD scenario functions are injected at runtime and absent from
    # the AST, so symbol validation must be skipped for BDD modules. The entry
    # must not raise a spurious "not found" violation.
    mod = tmp_path / "test_bdd_steps.py"
    _write_module(
        mod,
        """
        from pytest_bdd import scenarios, given
        scenarios("*.feature")

        @given("a thing")
        def _(): ...
        """,
    )
    qfile = tmp_path / ".quarantine.yml"
    _write_registry(
        qfile,
        [
            {
                "test_id": "test_bdd_steps.py::test_paginated_csv_export_loads_events",
                "reason": "flaky",
                "expiry": "2099-01-01",
            }
        ],
    )
    violations = sns._quarantine_violations(qfile, tmp_path, tmp_path)
    assert not any("not found" in v for v in violations)
    # but the expiry/reason completeness checks still apply
    assert not any("missing required" in v for v in violations)


def test_lens_flags_bdd_missing_file(tmp_path: Path) -> None:
    # BDD skip only applies when the file exists; a missing file is still wrong.
    qfile = tmp_path / ".quarantine.yml"
    _write_registry(
        qfile,
        [
            {
                "test_id": "test_missing_bdd.py::test_something",
                "reason": "flaky",
                "expiry": "2099-01-01",
            }
        ],
    )
    violations = sns._quarantine_violations(qfile, tmp_path, tmp_path)
    assert any("file not found" in v for v in violations)


@pytest.mark.parametrize(
    "test_id,valid",
    [
        ("test_thing.py::test_current", True),
        ("test_thing.py::test_renamed", False),
        ("test_thing.py::TestCls::test_method", True),
        ("test_thing.py::TestCls", False),  # bare class never a pytest nodeid
    ],
)
def test_lens_matrix(tmp_path: Path, test_id: str, valid: bool) -> None:
    mod = tmp_path / "test_thing.py"
    _write_module(
        mod,
        """
        def test_current(): ...
        class TestCls:
            def test_method(self): ...
        """,
    )
    qfile = tmp_path / ".quarantine.yml"
    _write_registry(qfile, [{"test_id": test_id, "reason": "flaky", "expiry": "2099-01-01"}])
    violations = sns._quarantine_violations(qfile, tmp_path, tmp_path)
    flagged = any(test_id in v for v in violations)
    assert flagged is not valid
