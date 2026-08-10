"""Unit tests for the flaky-test quarantine plugin (tests/quarantine_plugin.py).

The plugin reads ``.quarantine.yml`` from the repo root and marks each listed
test xfail so a known-flaky test does not fail CI. These tests lock in the
behaviour the plugin's ``pytest_collection_modifyitems`` hook must keep, so a
future refactor cannot silently break the quarantine mechanism.
"""

from __future__ import annotations

import warnings
from pathlib import Path

from tests import quarantine_plugin


class _FakeItem:
    def __init__(self, nodeid: str) -> None:
        self.nodeid = nodeid
        self._markers: list = []

    def add_marker(self, marker) -> None:
        self._markers.append(marker)


class _FakeConfig:
    def __init__(self, qfile: Path) -> None:
        self._qfile = qfile

    def getini(self, name: str) -> str:
        assert name == "quarantine_file"
        return str(self._qfile)


def _write_quarantine(path: Path, entries: list[dict]) -> None:
    import yaml

    path.write_text(yaml.safe_dump({"quarantine": entries}))


def test_applies_xfail_with_reason(tmp_path: Path) -> None:
    qfile = tmp_path / ".quarantine.yml"
    _write_quarantine(
        qfile,
        [{"test_id": "tests/a.py::test_flaky", "reason": "race", "expiry": "2099-01-01"}],
    )
    items = [_FakeItem("tests/a.py::test_flaky"), _FakeItem("tests/a.py::test_ok")]

    quarantine_plugin.pytest_collection_modifyitems(_FakeConfig(qfile), items)

    assert len(items[0]._markers) == 1
    marker = items[0]._markers[0]
    assert marker.name == "xfail"
    assert marker.kwargs["reason"] == "race"
    assert items[1]._markers == []


def test_unlisted_items_untouched(tmp_path: Path) -> None:
    qfile = tmp_path / ".quarantine.yml"
    _write_quarantine(
        qfile,
        [{"test_id": "tests/other.py::test_x", "reason": "flaky", "expiry": "2099-01-01"}],
    )
    items = [_FakeItem("tests/a.py::test_ok")]

    quarantine_plugin.pytest_collection_modifyitems(_FakeConfig(qfile), items)

    assert items[0]._markers == []


def test_default_reason_used_when_omitted(tmp_path: Path) -> None:
    qfile = tmp_path / ".quarantine.yml"
    _write_quarantine(qfile, [{"test_id": "tests/a.py::test_flaky", "expiry": "2099-01-01"}])
    items = [_FakeItem("tests/a.py::test_flaky")]

    quarantine_plugin.pytest_collection_modifyitems(_FakeConfig(qfile), items)

    assert items[0]._markers[0].kwargs["reason"] == "Quarantined (flaky test)"


def test_expired_quarantine_emits_warning(tmp_path: Path) -> None:
    qfile = tmp_path / ".quarantine.yml"
    _write_quarantine(
        qfile,
        [{"test_id": "tests/a.py::test_flaky", "reason": "still flaky", "expiry": "2020-01-01"}],
    )
    items = [_FakeItem("tests/a.py::test_flaky")]

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        quarantine_plugin.pytest_collection_modifyitems(_FakeConfig(qfile), items)

    assert any("QUARANTINE EXPIRED" in str(w.message) for w in caught)


def test_missing_file_is_noop(tmp_path: Path) -> None:
    items = [_FakeItem("tests/a.py::test_ok")]

    quarantine_plugin.pytest_collection_modifyitems(_FakeConfig(tmp_path / "nope.yml"), items)

    assert items[0]._markers == []


def test_empty_quarantine_section_is_noop(tmp_path: Path) -> None:
    qfile = tmp_path / ".quarantine.yml"
    qfile.write_text("quarantine:\n")
    items = [_FakeItem("tests/a.py::test_ok")]

    quarantine_plugin.pytest_collection_modifyitems(_FakeConfig(qfile), items)

    assert items[0]._markers == []


def test_invalid_expiry_ignored(tmp_path: Path) -> None:
    qfile = tmp_path / ".quarantine.yml"
    _write_quarantine(
        qfile,
        [{"test_id": "tests/a.py::test_flaky", "reason": "flaky", "expiry": "not-a-date"}],
    )
    items = [_FakeItem("tests/a.py::test_flaky")]

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        quarantine_plugin.pytest_collection_modifyitems(_FakeConfig(qfile), items)

    assert items[0]._markers[0].name == "xfail"
    assert not any("QUARANTINE EXPIRED" in str(w.message) for w in caught)


def test_default_path_points_at_repo_root_quarantine_file() -> None:
    assert Path(__file__).resolve().parents[3] / ".quarantine.yml" == quarantine_plugin.QUARANTINE_PATH
