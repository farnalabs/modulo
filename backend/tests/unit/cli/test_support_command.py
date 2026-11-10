"""``modulo support`` (FAR-2305): the command the docs page tells users to run.

It must reuse the support-bundle builder (no second implementation), work with
no launcher data dir, and print the per-run ref line plus the bundle path so a
user can paste ONE line and match the file to it.
"""

from __future__ import annotations

import json
import re
import zipfile
from pathlib import Path

import pytest
from click.testing import CliRunner

import farna.cli.main as cli_main

REF_SHAPE = re.compile(r"MSB-[0-9]+-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}")


def _no_db(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("farna.support.bundle._session_factory", lambda sources: None)


def test_support_builds_a_bundle_and_prints_the_ref(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _no_db(monkeypatch)
    out = tmp_path / "bundle.zip"

    result = CliRunner().invoke(cli_main.cli, ["support", "--out", str(out), "--data-dir", str(tmp_path)])

    assert result.exit_code == 0
    assert out.is_file()
    match = REF_SHAPE.search(result.output)
    assert match is not None
    ref = match.group(0)
    with zipfile.ZipFile(out) as archive:
        report = json.loads(archive.read("report.json"))
    assert report["ref"] == ref
    assert str(out) in result.output
    assert "check it before sending" in result.output


def test_support_default_out_path_carries_the_ref(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No --out: the file is ./modulo-support-<ref>.zip, same ref as the archive."""
    _no_db(monkeypatch)
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(cli_main.cli, ["support", "--data-dir", str(tmp_path)])

    assert result.exit_code == 0
    produced = sorted(tmp_path.glob("modulo-support-*.zip"))
    assert len(produced) == 1
    match = REF_SHAPE.search(result.output)
    assert match is not None
    assert match.group(0) in produced[0].name
    with zipfile.ZipFile(produced[0]) as archive:
        report = json.loads(archive.read("report.json"))
    assert report["ref"] == match.group(0)


def test_support_json_payload_carries_ref_and_ref_line(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _no_db(monkeypatch)
    out = tmp_path / "bundle.zip"

    result = CliRunner().invoke(cli_main.cli, ["support", "--out", str(out), "--data-dir", str(tmp_path), "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert REF_SHAPE.fullmatch(str(payload["ref"]))
    assert str(payload["ref"]) in str(payload["ref_line"])
    assert payload["reference"] == "modulo-support-bundle-v1"


def test_launcher_data_dir_or_none_resolves_and_falls_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert cli_main._launcher_data_dir_or_none(tmp_path) == tmp_path

    monkeypatch.setattr("farna.launcher.entry.default_data_dir", lambda: tmp_path)
    assert cli_main._launcher_data_dir_or_none(None) == tmp_path

    monkeypatch.setattr("farna.launcher.entry.default_data_dir", lambda: tmp_path / "missing")
    assert cli_main._launcher_data_dir_or_none(None) is None

    def _boom() -> Path:
        raise RuntimeError("no home directory")

    monkeypatch.setattr("farna.launcher.entry.default_data_dir", _boom)
    assert cli_main._launcher_data_dir_or_none(None) is None


def test_support_command_reports_a_gate_refusal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import farna.support.bundle as bundle

    async def _refuse(*_args: object, **_kwargs: object) -> None:
        raise bundle.RedactionGateError("logs/app.log.tail", ["seeded secret value still present"])

    monkeypatch.setattr(bundle, "build_bundle_async", _refuse)

    result = CliRunner().invoke(
        cli_main.cli, ["support", "--out", str(tmp_path / "b.zip"), "--data-dir", str(tmp_path)]
    )

    assert result.exit_code == 2
    assert "refused: redaction gate refused member 'logs/app.log.tail'" in result.output
    assert "no bundle written" in result.output


def test_support_command_reports_a_write_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import farna.support.bundle as bundle

    async def _fail(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(bundle, "build_bundle_async", _fail)

    result = CliRunner().invoke(
        cli_main.cli, ["support", "--out", str(tmp_path / "b.zip"), "--data-dir", str(tmp_path)]
    )

    assert result.exit_code == 1
    assert "could not write bundle: disk full" in result.output
