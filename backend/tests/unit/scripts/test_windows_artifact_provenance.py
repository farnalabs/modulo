"""
Test suite for Windows native artifact provenance validation.

Validates:
1. bundle-versions-windows.env file format and required fields
2. SHA256 checksum format (64 hex characters)
3. URL reachability (HEAD requests)
4. Version consistency with Linux bundle pins where applicable
"""

import re
from pathlib import Path

import pytest
import requests

pytestmark = pytest.mark.filterwarnings("ignore::pytest.PytestUnknownMarkWarning")


def parse_pins_file(pins_file: Path) -> dict:
    """Parse a bundle-versions file into a dict."""
    pins = {}
    content = pins_file.read_text(encoding="utf-8")
    for line_num, raw_line in enumerate(content.splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        assert "=" in line, f"Line {line_num}: expected VAR=value format, got: {line}"
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        assert key, f"Line {line_num}: empty key"
        assert value, f"Line {line_num}: empty value for {key}"
        assert key not in pins, f"Line {line_num}: duplicate key {key}"
        pins[key] = value
    return pins


@pytest.fixture(scope="session")
def windows_pins_file():
    """Path to the Windows bundle versions file."""
    repo_root = Path(__file__).parent.parent.parent.parent.parent
    return repo_root / "scripts" / "bundle-versions-windows.env"


@pytest.fixture(scope="session")
def windows_pins(windows_pins_file):
    """Parsed pins from the Windows file."""
    return parse_pins_file(windows_pins_file)


@pytest.fixture(scope="session")
def linux_pins_file():
    """Path to the Linux bundle versions file."""
    repo_root = Path(__file__).parent.parent.parent.parent.parent
    return repo_root / "scripts" / "bundle-versions.env"


@pytest.fixture(scope="session")
def linux_pins(linux_pins_file):
    """Parsed pins from the Linux file."""
    return parse_pins_file(linux_pins_file)


@pytest.fixture(scope="session")
def launcher_source():
    """Path to the launcher C source file."""
    repo_root = Path(__file__).parent.parent.parent.parent.parent
    return repo_root / "scripts" / "windows" / "modulo-launcher.c"


@pytest.fixture(scope="session")
def build_script():
    """Path to the launcher build script."""
    repo_root = Path(__file__).parent.parent.parent.parent.parent
    return repo_root / "scripts" / "windows" / "build-launcher.ps1"


class TestBundleVersionsWindowsFormat:
    """Test the bundle-versions-windows.env file format."""

    def test_required_valkey_fields(self, windows_pins):
        """Valkey version, URL, and SHA256 must be present."""
        assert "VALKEY_VERSION" in windows_pins
        assert "VALKEY_ZIP_URL" in windows_pins
        assert "VALKEY_ZIP_SHA256" in windows_pins
        assert "VALKEY_SERVER_EXE" in windows_pins
        assert "VALKEY_CLI_EXE" in windows_pins

    def test_required_python_fields(self, windows_pins):
        """Python-build-standalone version, URL, and SHA256 must be present."""
        assert "PBS_RELEASE" in windows_pins
        assert "PBS_CPYTHON_VERSION" in windows_pins
        assert "PBS_WIN_X64_TARBALL" in windows_pins
        assert "PBS_WIN_X64_URL" in windows_pins
        assert "PBS_WIN_X64_SHA256" in windows_pins

    def test_required_postgres_fields(self, windows_pins):
        """PostgreSQL version, URL, and SHA256 must be present."""
        assert "PG_MAJOR" in windows_pins
        assert "PG_VERSION" in windows_pins
        assert "PG_WIN_X64_ZIP_URL" in windows_pins
        assert "PG_WIN_X64_ZIP_SHA256" in windows_pins
        assert "PG_SERVER_EXE" in windows_pins
        assert "PG_CTL_EXE" in windows_pins
        assert "PG_DUMP_EXE" in windows_pins
        assert "PG_ISREADY_EXE" in windows_pins
        assert "PG_PSQL_EXE" in windows_pins

    def test_sha256_format(self, windows_pins):
        """All SHA256 values must be 64 lowercase hex characters."""
        sha256_keys = [k for k in windows_pins if k.endswith("_SHA256")]
        assert sha256_keys, "No SHA256 keys found"
        hex_pattern = re.compile(r"^[a-f0-9]{64}$")
        for key in sha256_keys:
            value = windows_pins[key]
            assert hex_pattern.match(value), (
                f"{key}: invalid SHA256 format (expected 64 lowercase hex chars), got: {value}"
            )

    def test_url_format(self, windows_pins):
        """All URL values must be valid HTTPS URLs."""
        url_keys = [k for k in windows_pins if k.endswith("_URL")]
        assert url_keys, "No URL keys found"
        url_pattern = re.compile(r"^https://")
        for key in url_keys:
            value = windows_pins[key]
            assert url_pattern.match(value), f"{key}: must be HTTPS URL, got: {value}"

    def test_version_format(self, windows_pins):
        """Version fields should follow semantic versioning (major.minor.patch)."""
        version_keys = [k for k in windows_pins if k.endswith("_VERSION")]
        version_pattern = re.compile(r"^\d+\.\d+(?:\.\d+)?$")
        for key in version_keys:
            value = windows_pins[key]
            assert version_pattern.match(value), f"{key}: expected semantic version (X.Y.Z), got: {value}"

    def test_pbs_release_format(self, windows_pins):
        """PBS_RELEASE should be YYYYMMDD format."""
        assert "PBS_RELEASE" in windows_pins
        release_pattern = re.compile(r"^\d{8}$")
        assert release_pattern.match(windows_pins["PBS_RELEASE"]), (
            f"PBS_RELEASE: expected YYYYMMDD format, got: {windows_pins['PBS_RELEASE']}"
        )

    def test_pg_major_format(self, windows_pins):
        """PG_MAJOR should be a major version number."""
        assert "PG_MAJOR" in windows_pins
        assert windows_pins["PG_MAJOR"].isdigit(), f"PG_MAJOR: expected integer, got: {windows_pins['PG_MAJOR']}"


class TestBundleVersionsWindowsConsistency:
    """Test consistency between Windows and Linux bundle pins."""

    def test_pbs_release_matches_linux(self, windows_pins, linux_pins):
        """PBS_RELEASE should match Linux bundle for version parity."""
        assert windows_pins["PBS_RELEASE"] == linux_pins["PBS_RELEASE"], (
            f"PBS_RELEASE mismatch: Windows={windows_pins['PBS_RELEASE']}, Linux={linux_pins['PBS_RELEASE']}"
        )

    def test_pbs_cpython_version_matches_linux(self, windows_pins, linux_pins):
        """PBS_CPYTHON_VERSION should match Linux bundle."""
        assert windows_pins["PBS_CPYTHON_VERSION"] == linux_pins["PBS_CPYTHON_VERSION"], (
            f"PBS_CPYTHON_VERSION mismatch: Windows={windows_pins['PBS_CPYTHON_VERSION']}, "
            f"Linux={linux_pins['PBS_CPYTHON_VERSION']}"
        )

    def test_pg_major_matches_linux(self, windows_pins, linux_pins):
        """PG_MAJOR should match Linux bundle (both PostgreSQL 16)."""
        assert windows_pins["PG_MAJOR"] == linux_pins["PG_MAJOR"], (
            f"PG_MAJOR mismatch: Windows={windows_pins['PG_MAJOR']}, Linux={linux_pins['PG_MAJOR']}"
        )

    def test_pg_version_matches_linux(self, windows_pins, linux_pins):
        """PG_VERSION should match Linux bundle."""
        assert windows_pins["PG_VERSION"] == linux_pins["PG_VERSION"], (
            f"PG_VERSION mismatch: Windows={windows_pins['PG_VERSION']}, Linux={linux_pins['PG_VERSION']}"
        )


class TestArtifactUrlReachability:
    """Test that pinned artifact URLs are reachable (HEAD requests)."""

    def test_valkey_url_head(self, windows_pins):
        """Valkey ZIP URL should respond to HEAD."""
        url = windows_pins["VALKEY_ZIP_URL"]
        response = requests.head(url, allow_redirects=True, timeout=30)
        assert response.status_code == 200, f"Valkey URL HEAD failed: {response.status_code}"
        # Verify content-length is reasonable (> 1MB)
        content_length = response.headers.get("content-length")
        if content_length:
            assert int(content_length) > 1_000_000, f"Valkey artifact too small: {content_length} bytes"

    def test_python_url_head(self, windows_pins):
        """python-build-standalone URL should respond to HEAD."""
        url = windows_pins["PBS_WIN_X64_URL"]
        response = requests.head(url, allow_redirects=True, timeout=30)
        assert response.status_code == 200, f"Python URL HEAD failed: {response.status_code}"
        content_length = response.headers.get("content-length")
        if content_length:
            assert int(content_length) > 10_000_000, f"Python artifact too small: {content_length} bytes"

    def test_postgres_url_head(self, windows_pins):
        """PostgreSQL URL should respond to HEAD."""
        url = windows_pins["PG_WIN_X64_ZIP_URL"]
        response = requests.head(url, allow_redirects=True, timeout=30)
        assert response.status_code == 200, f"PostgreSQL URL HEAD failed: {response.status_code}"
        content_length = response.headers.get("content-length")
        if content_length:
            assert int(content_length) > 50_000_000, f"PostgreSQL artifact too small: {content_length} bytes"


class TestLauncherSource:
    """Test the native launcher C source file."""

    def test_launcher_exists(self, launcher_source):
        """Launcher source file must exist."""
        assert launcher_source.exists(), "modulo-launcher.c not found"

    def test_launcher_has_required_functions(self, launcher_source):
        """Launcher must contain key functions."""
        content = launcher_source.read_text(encoding="utf-8")
        required_functions = [
            "get_executable_dir",
            "path_append",
            "set_env",
            "prepend_path",
            "build_pythonpath",
            "build_command_line",
            "wmain",
        ]
        for func in required_functions:
            assert func in content, f"Missing required function: {func}"

    def test_launcher_sets_required_env_vars(self, launcher_source):
        """Launcher must set required environment variables."""
        content = launcher_source.read_text(encoding="utf-8")
        required_vars = [
            "MODULO_BUNDLED_BIN_DIR",
            "PYTHONPATH",
            "PATH",
        ]
        for var in required_vars:
            assert var in content, f"Missing environment variable setup: {var}"

    def test_launcher_execs_python_module(self, launcher_source):
        """Launcher must exec python3.exe -m modulo.cli.main."""
        content = launcher_source.read_text(encoding="utf-8")
        assert "python3.exe" in content
        assert "modulo.cli.main" in content

    def test_launcher_includes_bundle_paths(self, launcher_source):
        """Launcher must reference all required bundle paths."""
        content = launcher_source.read_text(encoding="utf-8")
        required_paths = [
            'L"backend"',
            'L"runtime"',
            'L"bin"',
            'L"pg"',
            'L"redis"',
        ]
        for path in required_paths:
            assert path in content, f"Missing bundle path reference: {path}"


class TestBuildScript:
    """Test the launcher build script."""

    def test_build_script_exists(self, build_script):
        """Build script must exist."""
        assert build_script.exists(), "build-launcher.ps1 not found"

    def test_build_script_has_required_params(self, build_script):
        """Build script must have required parameters."""
        content = build_script.read_text(encoding="utf-8")
        assert "param(" in content
        assert "Compiler" in content
        assert "OutputPath" in content
        assert "Debug" in content

    def test_build_script_verifies_output(self, build_script):
        """Build script must verify the output executable."""
        content = build_script.read_text(encoding="utf-8")
        assert "modulo.exe" in content
        assert "--help" in content or "Verify" in content


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
