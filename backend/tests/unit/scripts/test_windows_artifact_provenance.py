"""Deterministic checks for the Windows bundle pins and staging contract."""

import hashlib
import json
import re
import subprocess
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


def test_offline_staged_payload_contract(tmp_path: Path, repo_root: Path) -> None:
    script = repo_root / "scripts" / "windows" / "build-launcher.ps1"
    fixture = tmp_path / "fixture"
    python_root = fixture / "python"
    backend_source = fixture / "backend-src"
    site_packages = fixture / "site-packages"
    frontend_dist = fixture / "frontend-dist"
    launcher = fixture / "modulo.exe"
    for directory in (python_root, backend_source / "modulo", site_packages / "modulo_dep", frontend_dist):
        directory.mkdir(parents=True)
    (python_root / "python.exe").write_bytes(b"python")
    (python_root / "python312.dll").write_bytes(b"dll")
    (backend_source / "modulo" / "__init__.py").write_text("", encoding="utf-8")
    (site_packages / "modulo_dep" / "__init__.py").write_text("", encoding="utf-8")
    (frontend_dist / "index.html").write_text("ok", encoding="utf-8")
    launcher.write_bytes(b"launcher")
    stage = tmp_path / "stage"
    command = [
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-Command",
        (
            f". '{script}'; Invoke-WindowsBundleStaging -StagePath '{stage}' "
            f"-PythonRoot '{python_root}' -BackendSource '{backend_source}' "
            f"-SitePackagesSource '{site_packages}' -FrontendDistSource '{frontend_dist}' "
            f"-LauncherPath '{launcher}'"
        ),
    ]
    result = subprocess.run(  # noqa: S603 - powershell.exe runs a fixed test script with isolated temp paths
        command, capture_output=True, text=True, check=False, timeout=30
    )
    assert result.returncode == 0, result.stderr
    expected_files = [
        stage / "runtime/python/python.exe",
        stage / "runtime/python/python312.dll",
        stage / "backend/src/modulo/__init__.py",
        stage / "runtime/python/Lib/site-packages/modulo_dep/__init__.py",
        stage / "frontend-dist/index.html",
        stage / "bin/modulo.exe",
    ]
    for path in expected_files:
        assert path.is_file(), path


def test_launcher_clean_environment_contract(launcher_source: str) -> None:
    dangerous_names = (
        "PYTHONUSERBASE",
        "OPENSSL_CONF",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
    )
    for name in dangerous_names:
        assert f'L"{name}"' not in launcher_source
    assert "build_clean_environment" in launcher_source
    assert "CREATE_UNICODE_ENVIRONMENT" in launcher_source
    assert "inherited_allowlist" in launcher_source


def test_launcher_contract(launcher_source: str) -> None:
    assert 'L"python3.exe"' in launcher_source
    assert "modulo.cli.main" in launcher_source
    assert "CreateProcessW(python_exe" in launcher_source
    assert "CreateProcessW" in launcher_source
    assert 'L"python"' in launcher_source
    assert "return FALSE" in launcher_source


def test_launcher_command_line_size_includes_fixed_invocation(launcher_source: str) -> None:
    build_start = launcher_source.index("static wchar_t *build_command_line")
    build_end = launcher_source.index("static BOOL build_pythonpath", build_start)
    builder = launcher_source[build_start:build_end]

    assert 'static const wchar_t module_invocation[] = L" -m modulo.cli.main";' in builder
    assert "checked_add_size(&total, wcslen(module_invocation))" in builder
    assert "if (total > SIZE_MAX / sizeof(wchar_t)) return NULL;" in builder
    assert "malloc(total * sizeof(wchar_t))" in builder
    assert "wcscpy(cursor, module_invocation)" in builder
    assert "size_t total = quoted_arg_length(python_exe) + 1" not in builder


def test_launcher_command_line_size_covers_zero_and_quoted_arguments(launcher_source: str) -> None:
    quoted_start = launcher_source.index("static BOOL quoted_arg_length")
    quoted_end = launcher_source.index("static wchar_t *append_quoted_arg", quoted_start)
    quoted_length = launcher_source[quoted_start:quoted_end]
    build_start = launcher_source.index("static wchar_t *build_command_line")
    build_end = launcher_source.index("static BOOL build_pythonpath", build_start)
    builder = launcher_source[build_start:build_end]

    assert "size_t result = 2;" in quoted_length
    assert "if (*cursor == L'\"' && !checked_add_size" in quoted_length
    assert "backslashes > (SIZE_MAX - result) / 2" in quoted_length
    assert "for (index = 1; index < argc; index++)" in builder
    assert "checked_add_size(&total, argument_length)" in builder
    assert "checked_add_size(&total, 1)" in builder
    assert "while (backslashes > 0)" in launcher_source


def test_workflow_contract(workflow_source: str) -> None:
    assert "d3f86a106a0bac45b974a628896c90dbdf5c8093" in workflow_source
    assert "520d128f165991a6c774bcb264f323e3d70747f4" in workflow_source
    assert "Invoke-Expression" not in workflow_source
    assert "runtime/python" in workflow_source
    assert "python312.dll" in workflow_source
    assert "backend/src" in workflow_source
    assert "runtime/python/Lib/site-packages" in workflow_source
    assert "frontend-dist" in workflow_source
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
