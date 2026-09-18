"""Extra coverage for the new ``modulo`` CLI commands (FAR-676): the doctor
``--report`` capture/sink, ``modulo env --raw``, and the ``logs`` rotate /
missing-file paths.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

import modulo.cli.main as cli_main


def test_doctor_report_builds_archive(tmp_path: Path):
    report = tmp_path / "report.zip"
    result = CliRunner().invoke(cli_main.cli, ["doctor", "--data-dir", str(tmp_path), "--report", str(report)])
    # Uninitialized data dir -> doctor exits 3, but the report archive is built.
    assert result.exit_code == 3
    assert report.is_file()
    import zipfile

    with zipfile.ZipFile(report) as archive:
        names = archive.namelist()
    assert "report.json" in names
    assert "doctor-output.txt" in names


def test_logs_rotate_child_refused(tmp_path: Path):
    result = CliRunner().invoke(cli_main.cli, ["logs", "--rotate", "postgres", "--data-dir", str(tmp_path)])
    assert result.exit_code == 0
    assert "rotation applies to the app log only" in result.output


def test_logs_missing_file(tmp_path: Path):
    result = CliRunner().invoke(cli_main.cli, ["logs", "app", "--data-dir", str(tmp_path)])
    assert result.exit_code == 1
    assert "no app log file" in result.output


def test_env_raw():
    result = CliRunner().invoke(cli_main.cli, ["env", "--raw"])
    # --raw succeeds (the warning is printed to stderr); settings must load.
    assert result.exit_code == 0


def test_env_settings_unavailable(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("modulo.settings.get_settings", lambda: (_ for _ in ()).throw(RuntimeError("settings boom")))
    result = CliRunner().invoke(cli_main.cli, ["env"])
    assert result.exit_code == 1
    assert "settings unavailable" in result.output
