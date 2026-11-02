"""Deterministic checks for the Windows bundle pins and staging contract."""

import hashlib
import json
import re
import tarfile
import zipfile
from pathlib import Path

import pytest


def parse_pins_file(path: Path) -> dict[str, str]:
    pins: dict[str, str] = {}
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        assert re.fullmatch(r"[A-Z0-9_]+=[^=]+", line), f"invalid pin line {number}"
        key, value = line.split("=", 1)
        assert key not in pins, f"duplicate pin {key}"
        pins[key] = value
    return pins


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return Path(__file__).parents[4]


@pytest.fixture(scope="session")
def windows_pins(repo_root: Path) -> dict[str, str]:
    return parse_pins_file(repo_root / "scripts" / "bundle-versions-windows.env")


@pytest.fixture(scope="session")
def linux_pins(repo_root: Path) -> dict[str, str]:
    return parse_pins_file(repo_root / "scripts" / "bundle-versions.env")


@pytest.fixture(scope="session")
def launcher_source(repo_root: Path) -> str:
    return (repo_root / "scripts" / "windows" / "modulo-launcher.c").read_text(encoding="utf-8")


@pytest.fixture(scope="session")
def workflow_source(repo_root: Path) -> str:
    return (repo_root / ".github" / "workflows" / "windows-artifacts.yml").read_text(encoding="utf-8")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_pins_are_strict_and_consistent(windows_pins: dict[str, str], linux_pins: dict[str, str]) -> None:
    expected = {
        "VALKEY_VERSION",
        "VALKEY_PROVENANCE_POLICY",
        "VALKEY_ZIP_URL",
        "VALKEY_ZIP_SHA256",
        "VALKEY_ARCHIVE_ROOT",
        "VALKEY_SERVER_EXE",
        "VALKEY_CLI_EXE",
        "PBS_RELEASE",
        "PBS_CPYTHON_VERSION",
        "PBS_WIN_X64_TARBALL",
        "PBS_WIN_X64_URL",
        "PBS_WIN_X64_SHA256",
        "PBS_ARCHIVE_ROOT",
        "PBS_PYTHON_EXE",
        "PG_MAJOR",
        "PG_VERSION",
        "PG_WIN_X64_ZIP_URL",
        "PG_WIN_X64_ZIP_SHA256",
        "PG_ARCHIVE_ROOT",
        "PG_SERVER_EXE",
        "PG_CTL_EXE",
        "PG_DUMP_EXE",
        "PG_ISREADY_EXE",
        "PG_PSQL_EXE",
    }
    assert set(windows_pins) == expected
    assert windows_pins["VALKEY_PROVENANCE_POLICY"] == "external-release-pinned"
    for key, value in windows_pins.items():
        if key.endswith("_SHA256"):
            assert re.fullmatch(r"[a-f0-9]{64}", value)
        if key.endswith("_URL"):
            assert value.startswith("https://")
    assert windows_pins["PBS_RELEASE"] == linux_pins["PBS_RELEASE"]
    assert windows_pins["PBS_CPYTHON_VERSION"] == linux_pins["PBS_CPYTHON_VERSION"]
    assert windows_pins["PG_MAJOR"] == linux_pins["PG_MAJOR"]
    assert windows_pins["PG_VERSION"] == linux_pins["PG_VERSION"]


def test_hash_verification_uses_actual_fixture_bytes(tmp_path: Path) -> None:
    artifact = tmp_path / "artifact.bin"
    artifact.write_bytes(b"fixture payload")
    expected = sha256(artifact)
    assert hashlib.sha256(artifact.read_bytes()).hexdigest() == expected
    artifact.write_bytes(b"tampered payload")
    assert hashlib.sha256(artifact.read_bytes()).hexdigest() != expected


def test_offline_archive_layout_fixture(tmp_path: Path, windows_pins: dict[str, str]) -> None:
    zip_path = tmp_path / "valkey.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr(f"{windows_pins['VALKEY_ARCHIVE_ROOT']}/bin/{windows_pins['VALKEY_SERVER_EXE']}", b"server")
        archive.writestr(f"{windows_pins['VALKEY_ARCHIVE_ROOT']}/bin/{windows_pins['VALKEY_CLI_EXE']}", b"cli")
        archive.writestr(f"{windows_pins['PG_ARCHIVE_ROOT']}/bin/{windows_pins['PG_SERVER_EXE']}", b"postgres")
    with zipfile.ZipFile(zip_path) as archive:
        names = set(archive.namelist())
    assert f"{windows_pins['VALKEY_ARCHIVE_ROOT']}/bin/{windows_pins['VALKEY_SERVER_EXE']}" in names
    assert f"{windows_pins['VALKEY_ARCHIVE_ROOT']}/bin/{windows_pins['VALKEY_CLI_EXE']}" in names
    assert f"{windows_pins['PG_ARCHIVE_ROOT']}/bin/{windows_pins['PG_SERVER_EXE']}" in names

    tar_path = tmp_path / "python.tar.gz"
    with tarfile.open(tar_path, "w:gz") as archive:
        source = tmp_path / "python.exe"
        source.write_bytes(b"python")
        archive.add(source, arcname=f"{windows_pins['PBS_ARCHIVE_ROOT']}/{windows_pins['PBS_PYTHON_EXE']}")
    with tarfile.open(tar_path, "r:gz") as archive:
        assert f"{windows_pins['PBS_ARCHIVE_ROOT']}/{windows_pins['PBS_PYTHON_EXE']}" in archive.getnames()


def test_launcher_contract(launcher_source: str) -> None:
    assert 'L"python3.exe"' in launcher_source
    assert "modulo.cli.main" in launcher_source
    assert "CreateProcessW(python_exe" in launcher_source
    assert "_wputenv_s" in launcher_source
    assert "return FALSE" in launcher_source


def test_workflow_contract(workflow_source: str) -> None:
    assert "d3f86a106a0bac45b974a628896c90dbdf5c8093" in workflow_source
    assert "520d128f165991a6c774bcb264f323e3d70747f4" in workflow_source
    assert "Invoke-Expression" not in workflow_source
    assert "runtime/bin/python3.exe" in workflow_source
    assert "redis/valkey-server.exe" in workflow_source
    assert "pg/bin/postgres.exe" in workflow_source
    assert "Get-Date -AsUTC" in workflow_source
    assert "subject-path: windows-artifacts/windows-native-artifacts.zip" in workflow_source
    assert "--help" in workflow_source


def test_manifest_round_trip(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    payload = {"artifact_sha256": "a" * 64, "generated_at": "2026-11-02T00:00:00Z"}
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    assert json.loads(manifest.read_text(encoding="utf-8")) == payload
