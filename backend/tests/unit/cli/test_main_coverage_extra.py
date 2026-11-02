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
    import json
    import zipfile

    with zipfile.ZipFile(report) as archive:
        names = archive.namelist()
        manifest = json.loads(archive.read("report.json"))
    assert "report.json" in names
    assert "MANIFEST.txt" in names
    assert "doctor.txt" in names
    assert manifest["format"] == "modulo-support-bundle/v1"
    assert manifest["bundle_format_version"] == 1


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


# ---------------------------------------------------------------------------
# FAR-2305 review follow-up: a refused report archive must not surface as a
# traceback / exit 1 ("the install is unhealthy").
# ---------------------------------------------------------------------------


def test_doctor_report_refusal_exits_four_with_an_actionable_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from modulo.support.redaction import RedactionSelfCheckError

    def _refuse(*_args: object, **_kwargs: object) -> Path:
        raise RedactionSelfCheckError("MANIFEST.txt", "a seeded secret value survived the seed-exact layer")

    monkeypatch.setattr("modulo.launcher.doctor_report.build_report", _refuse)
    report = tmp_path / "report.zip"

    result = CliRunner().invoke(cli_main.cli, ["doctor", "--data-dir", str(tmp_path), "--report", str(report)])

    assert result.exit_code == cli_main.DOCTOR_REPORT_EXIT_CODE
    assert cli_main.DOCTOR_REPORT_EXIT_CODE == 4
    assert "MANIFEST.txt" in result.output
    assert "seed-exact layer" in result.output
    assert not report.is_file()


def test_doctor_report_build_error_exits_four(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from modulo.support.bundle import BundleBuildError

    def _fail(*_args: object, **_kwargs: object) -> Path:
        raise BundleBuildError("the archive could not be assembled")

    monkeypatch.setattr("modulo.launcher.doctor_report.build_report", _fail)
    report = tmp_path / "report.zip"

    result = CliRunner().invoke(cli_main.cli, ["doctor", "--data-dir", str(tmp_path), "--report", str(report)])

    assert result.exit_code == 4
    assert "could not be assembled" in result.output
    assert not report.is_file()
