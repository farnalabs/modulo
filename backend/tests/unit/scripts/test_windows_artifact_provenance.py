"""Deterministic checks for the Windows bundle pins and staging contract."""

import hashlib
import json
import re
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest
import yaml


def resolve_powershell() -> str | None:
    """Return the path of a PowerShell interpreter this host can run, else ``None``.

    ``pwsh`` is PowerShell 7 and is what the Linux CI runner ships; ``powershell``
    is Windows PowerShell 5.1 and is the only one a stock Windows host has. Both
    can dot-source ``build-launcher.ps1``, so either is a usable interpreter -
    hardcoding one name breaks every host that does not have that particular
    binary.
    """
    for name in ("pwsh", "powershell"):
        resolved = shutil.which(name)
        if resolved is not None:
            return resolved
    return None


POWERSHELL = resolve_powershell()
requires_powershell = pytest.mark.skipif(
    POWERSHELL is None,
    reason="no PowerShell interpreter (pwsh or powershell) on PATH; build-launcher.ps1 cannot be exercised",
)


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


@pytest.fixture(scope="session")
def build_script_source(repo_root: Path) -> str:
    return (repo_root / "scripts" / "windows" / "build-launcher.ps1").read_text(encoding="utf-8")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def workflow_step_run(workflow_source: str, step_name: str) -> str:
    """Return the ``run`` body of a named step in the artifact workflow."""
    document = yaml.safe_load(workflow_source)
    for step in document["jobs"]["download-and-verify"]["steps"]:
        if isinstance(step, dict) and step.get("name") == step_name:
            run_body = step.get("run")
            assert isinstance(run_body, str), step_name
            return run_body
    raise AssertionError(f"workflow step not found: {step_name}")


def stage_bundle(
    script: Path,
    stage: Path,
    python_root: Path,
    backend_source: Path,
    site_packages: Path,
    frontend_dist: Path,
    launcher: Path,
    *,
    preserve_existing: bool = False,
    version: str | None = None,
) -> subprocess.CompletedProcess[str]:
    assert POWERSHELL is not None
    command = [POWERSHELL, "-NoProfile", "-NonInteractive"]
    if sys.platform == "win32":
        # Execution policy is a Windows-only concept; pwsh on POSIX has none to bypass.
        command += ["-ExecutionPolicy", "Bypass"]
    invocation = (
        f". '{script}'; Invoke-WindowsBundleStaging -StagePath '{stage}' "
        f"-PythonRoot '{python_root}' -BackendSource '{backend_source}' "
        f"-SitePackagesSource '{site_packages}' -FrontendDistSource '{frontend_dist}' "
        f"-LauncherPath '{launcher}'"
    )
    if version is not None:
        invocation += f" -Version '{version}'"
    if preserve_existing:
        invocation += " -PreserveExisting"
    command += ["-Command", invocation]
    return subprocess.run(  # noqa: S603 - resolved PowerShell runs a fixed test script with isolated temp paths
        command, capture_output=True, text=True, check=False, timeout=60
    )


def snapshot_tree(root: Path) -> dict[str, str]:
    """Map every staged file to its digest, independent of traversal order."""
    return {path.relative_to(root).as_posix(): sha256(path) for path in sorted(root.rglob("*")) if path.is_file()}


def nested_duplicate_directories(root: Path) -> list[str]:
    """Return directories that contain a same-named child directory."""
    return [
        directory.relative_to(root).as_posix()
        for directory in root.rglob("*")
        if directory.is_dir() and (directory / directory.name).is_dir()
    ]


def test_pins_are_strict_and_consistent(
    windows_pins: dict[str, str], linux_pins: dict[str, str], workflow_source: str
) -> None:
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
        # Every value is interpolated into a pwsh command string, so it must fit
        # the workflow's strict charset (no quotes, whitespace, backticks, ; or $).
        assert re.fullmatch(r"[A-Za-z0-9._:/@%+-]+", value), key
    pins_step = workflow_step_run(workflow_source, "Load and validate version pins")
    assert "Unsupported characters in pin value" in pins_step
    assert windows_pins["PBS_RELEASE"] == linux_pins["PBS_RELEASE"]
    assert windows_pins["PBS_CPYTHON_VERSION"] == linux_pins["PBS_CPYTHON_VERSION"]
    assert windows_pins["PG_MAJOR"] == linux_pins["PG_MAJOR"]
    assert windows_pins["PG_VERSION"] == linux_pins["PG_VERSION"]


def test_hash_verification_uses_actual_fixture_bytes(tmp_path: Path, workflow_source: str) -> None:
    artifact = tmp_path / "artifact.bin"
    artifact.write_bytes(b"fixture payload")
    expected = sha256(artifact)
    assert hashlib.sha256(artifact.read_bytes()).hexdigest() == expected
    artifact.write_bytes(b"tampered payload")
    assert hashlib.sha256(artifact.read_bytes()).hexdigest() != expected

    # The digest check above is only meaningful if the workflow verifies each
    # pinned archive against the matching ``*_SHA256`` pin.
    download_step = workflow_step_run(workflow_source, "Download and verify pinned archives")
    assert "Get-FileHash -Algorithm SHA256" in download_step
    assert "if ($actual -ne $Expected.ToLowerInvariant()) { throw" in download_step
    for pin in ("VALKEY_ZIP_SHA256", "PBS_WIN_X64_SHA256", "PG_WIN_X64_ZIP_SHA256"):
        assert f"steps.pins.outputs.{pin}" in download_step, pin


def test_offline_archive_layout_fixture(tmp_path: Path, windows_pins: dict[str, str], workflow_source: str) -> None:
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

    # The fixture layout above is only a contract if the workflow extracts and
    # verifies exactly the pinned archive roots and executable filenames.
    stage_step = workflow_step_run(workflow_source, "Stage exact bundle layout")
    for pin in (
        "VALKEY_ARCHIVE_ROOT",
        "VALKEY_SERVER_EXE",
        "VALKEY_CLI_EXE",
        "PBS_ARCHIVE_ROOT",
        "PBS_PYTHON_EXE",
        "PG_ARCHIVE_ROOT",
        "PG_SERVER_EXE",
        "PG_CTL_EXE",
        "PG_DUMP_EXE",
        "PG_ISREADY_EXE",
        "PG_PSQL_EXE",
    ):
        assert f"steps.pins.outputs.{pin}" in stage_step, pin
    verify_step = workflow_step_run(workflow_source, "Verify staged binaries and launcher")
    assert "redis/${{ steps.pins.outputs.VALKEY_SERVER_EXE }}" in verify_step
    assert "pg/bin/${{ steps.pins.outputs.PG_SERVER_EXE }}" in verify_step
    assert "redis/valkey-server.exe" not in verify_step
    assert "pg/bin/postgres.exe" not in verify_step


@requires_powershell
def test_offline_staged_payload_contract(tmp_path: Path, repo_root: Path) -> None:
    """The staging function produces the payload layout the launcher expects."""
    assert POWERSHELL is not None  # guaranteed by requires_powershell
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
    result = stage_bundle(script, stage, python_root, backend_source, site_packages, frontend_dist, launcher)
    assert result.returncode == 0, result.stderr
    expected_files = [
        stage / "runtime/python/python.exe",
        stage / "runtime/python/python312.dll",
        stage / "runtime/bin/python3.exe",
        stage / "runtime/bin/python312.dll",
        stage / "backend/src/modulo/__init__.py",
        stage / "runtime/python/Lib/site-packages/modulo_dep/__init__.py",
        stage / "frontend-dist/index.html",
        stage / "bin/modulo.exe",
    ]
    for path in expected_files:
        assert path.is_file(), path


@requires_powershell
def test_staged_bundle_writes_the_runtime_marker_the_installer_requires(tmp_path: Path, repo_root: Path) -> None:
    """modulo.iss refuses to compile without modulo-runtime.json at the bundle root."""
    assert POWERSHELL is not None  # guaranteed by requires_powershell
    script = repo_root / "scripts" / "windows" / "build-launcher.ps1"
    fixture = tmp_path / "fixture"
    python_root = fixture / "python"
    backend_source = fixture / "backend-src"
    site_packages = fixture / "site-packages"
    frontend_dist = fixture / "frontend-dist"
    launcher = fixture / "modulo.exe"
    for directory in (python_root, backend_source / "modulo", site_packages, frontend_dist):
        directory.mkdir(parents=True)
    (python_root / "python.exe").write_bytes(b"python")
    (python_root / "python312.dll").write_bytes(b"dll")
    (backend_source / "modulo" / "__init__.py").write_text("", encoding="utf-8")
    (frontend_dist / "index.html").write_text("ok", encoding="utf-8")
    launcher.write_bytes(b"launcher")
    stage = tmp_path / "stage"
    result = stage_bundle(
        script, stage, python_root, backend_source, site_packages, frontend_dist, launcher, version="1.2.3"
    )
    assert result.returncode == 0, result.stderr
    marker = json.loads((stage / "modulo-runtime.json").read_text(encoding="utf-8"))
    assert marker["contract"] == "native-windows-v1"
    assert marker["version"] == "1.2.3"
    assert (stage / "bin" / "modulo.exe").is_file()
    assert not (stage / "modulo.exe").exists()


@requires_powershell
def test_staging_twice_is_byte_identical_and_never_nests(tmp_path: Path, repo_root: Path) -> None:
    """Re-staging in preserve-existing mode must be idempotent for every payload."""
    assert POWERSHELL is not None  # guaranteed by requires_powershell
    script = repo_root / "scripts" / "windows" / "build-launcher.ps1"
    fixture = tmp_path / "fixture"
    python_root = fixture / "python"
    # Name the backend source ``src`` so the historical push was
    # Copy-Item -Destination backend/src, which nested backend/src/src.
    backend_source = fixture / "src"
    site_packages = fixture / "site-packages"
    frontend_dist = fixture / "frontend-dist"
    launcher = fixture / "modulo.exe"
    for directory in (
        python_root / "DLLs",
        python_root / "Lib" / "encodings",
        backend_source / "modulo" / "core",
        site_packages / "modulo_dep",
        frontend_dist / "assets",
    ):
        directory.mkdir(parents=True)
    (python_root / "python.exe").write_bytes(b"python")
    (python_root / "python312.dll").write_bytes(b"dll")
    (python_root / "DLLs" / "vcruntime140.dll").write_bytes(b"runtime")
    (python_root / "Lib" / "encodings" / "utf_8.py").write_bytes(b"codec")
    (backend_source / "modulo" / "__init__.py").write_text("", encoding="utf-8")
    (backend_source / "modulo" / "core" / "engine.py").write_text("engine", encoding="utf-8")
    (site_packages / "modulo_dep" / "__init__.py").write_text("", encoding="utf-8")
    (frontend_dist / "index.html").write_text("ok", encoding="utf-8")
    (frontend_dist / "assets" / "app.js").write_text("app", encoding="utf-8")
    launcher.write_bytes(b"launcher")
    stage = tmp_path / "stage"

    first = stage_bundle(script, stage, python_root, backend_source, site_packages, frontend_dist, launcher)
    assert first.returncode == 0, first.stderr
    initial = snapshot_tree(stage)
    assert initial
    assert (stage / "backend/src/modulo/core/engine.py").is_file()

    second = stage_bundle(
        script,
        stage,
        python_root,
        backend_source,
        site_packages,
        frontend_dist,
        launcher,
        preserve_existing=True,
    )
    assert second.returncode == 0, second.stderr
    assert snapshot_tree(stage) == initial
    assert not nested_duplicate_directories(stage)
    assert not (stage / "backend/src/src").exists()
    assert not (stage / "runtime/python/python").exists()
    assert not (stage / "runtime/python/Lib/site-packages/site-packages").exists()
    assert not (stage / "frontend-dist/frontend-dist").exists()


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


def test_workflow_contract(workflow_source: str, build_script_source: str) -> None:
    assert "d3f86a106a0bac45b974a628896c90dbdf5c8093" in workflow_source
    assert "520d128f165991a6c774bcb264f323e3d70747f4" in workflow_source
    assert "Invoke-Expression" not in workflow_source
    assert "runtime/python" in workflow_source
    assert workflow_source.count("Invoke-WindowsBundleStaging") == 1
    assert "python312.dll" in build_script_source
    assert "backend/src" in workflow_source
    assert "runtime/python/Lib/site-packages" in workflow_source
    assert "frontend-dist" in workflow_source
    assert "runtime/bin/python3.exe" in workflow_source
    assert "redis/${{ steps.pins.outputs.VALKEY_SERVER_EXE }}" in workflow_source
    assert "pg/bin/${{ steps.pins.outputs.PG_SERVER_EXE }}" in workflow_source
    assert "Get-Date -AsUTC" in workflow_source
    assert "subject-path: windows-artifacts/windows-native-artifacts.zip" in workflow_source
    assert "--help" in workflow_source


def test_manifest_round_trip(tmp_path: Path, workflow_source: str) -> None:
    manifest_step = workflow_step_run(workflow_source, "Create manifest and payload archive")
    # Every manifest field must be sourced from a pin or a verified download,
    # never from a literal, or the provenance record could drift from the payload.
    sources = (
        "steps.pins.outputs.VALKEY_VERSION",
        "steps.downloads.outputs.valkey_sha256",
        "steps.pins.outputs.PBS_CPYTHON_VERSION",
        "steps.pins.outputs.PBS_RELEASE",
        "steps.downloads.outputs.python_sha256",
        "steps.pins.outputs.PG_VERSION",
        "steps.downloads.outputs.postgres_sha256",
    )
    for source in sources:
        assert "${{ " + source + " }}" in manifest_step, source
    for section in ("valkey", "python", "postgres", "generated_at", "generated_by"):
        assert f"{section} =" in manifest_step, section
    assert 'generated_by = "github-actions-${{ github.run_id }}"' in manifest_step
    assert "ConvertTo-Json -Depth 5" in manifest_step

    # The emitted manifest shape survives the JSON round trip the workflow relies on.
    payload = {
        "valkey": {"version": "9.1.3", "archive_sha256": "a" * 64, "provenance": "external-release-pinned"},
        "python": {"version": "3.12.14", "release": "20260901", "archive_sha256": "b" * 64},
        "postgres": {"version": "16.15", "archive_sha256": "c" * 64},
        "generated_at": "2026-11-02T00:00:00Z",
        "generated_by": "github-actions-1",
    }
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    assert json.loads(manifest.read_text(encoding="utf-8")) == payload
