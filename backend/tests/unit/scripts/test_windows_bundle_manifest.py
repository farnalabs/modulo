"""Contract-validation tests for the Windows native bundle manifest (FAR-2231)."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[4]
_VALIDATOR_PATH = _REPO_ROOT / "scripts" / "validate_windows_bundle_manifest.py"
_VALIDATOR_SPEC = importlib.util.spec_from_file_location("windows_bundle_manifest_validator", _VALIDATOR_PATH)
assert _VALIDATOR_SPEC is not None
assert _VALIDATOR_SPEC.loader is not None
_VALIDATOR_MODULE = importlib.util.module_from_spec(_VALIDATOR_SPEC)
_VALIDATOR_SPEC.loader.exec_module(_VALIDATOR_MODULE)
validate = _VALIDATOR_MODULE.validate
main = _VALIDATOR_MODULE.main
_SCHEMA = _REPO_ROOT / "scripts" / "windows-bundle-manifest.schema.json"
_BUILDER = _REPO_ROOT / "scripts" / "build-windows-bundle.ps1"
_PWSH = shutil.which("pwsh")


_POSTGRES_BINARIES = ("initdb.exe", "postgres.exe", "pg_isready.exe", "psql.exe", "pg_dump.exe")
_REDIS_BINARIES = ("redis-server.exe", "redis-cli.exe")
_SCAFFOLDING_ENTRIES = (
    "pyinstaller",
    "pyinstaller-work",
    "pyinstaller-spec",
    "pyinstaller-entrypoint.py",
)


def _bundle(tmp_path: Path, *, include_native: bool = True) -> tuple[Path, Path]:
    bundle = tmp_path / "bundle"
    (bundle / "frontend-dist").mkdir(parents=True)
    (bundle / "frontend-dist" / "index.html").write_text("<html />", encoding="utf-8")
    (bundle / "modulo.exe").write_bytes(b"contract-only-launcher")
    metadata: dict[str, dict[str, dict[str, int | str]]] = {"postgresql": {}, "redis": {}}
    for service, binaries in {"postgresql": _POSTGRES_BINARIES, "redis": _REDIS_BINARIES}.items():
        for binary in binaries:
            path = bundle / ("postgres" if service == "postgresql" else "redis") / "bin" / binary
            if include_native:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(f"temporary contract input: {binary}".encode())
                metadata[service][binary] = {
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "size_bytes": path.stat().st_size,
                }
    manifest = {
        "schema_version": 1,
        "version": "0.3.323",
        "architecture": "amd64",
        "runtime_support": {"status": "unsupported", "reason": "Windows launcher support is pending FAR-2229."},
        "launcher": {"path": "modulo.exe", "entry_point": "modulo.cli.main:cli", "start_command": "modulo start"},
        "frontend": {"dist_path": "frontend-dist"},
        "native_requirements": {
            "postgresql": {
                "required": True,
                "relative_path": "postgres/bin",
                "binaries": list(_POSTGRES_BINARIES),
                "metadata": metadata["postgresql"],
            },
            "redis": {
                "required": True,
                "relative_path": "redis/bin",
                "binaries": list(_REDIS_BINARIES),
                "metadata": metadata["redis"],
            },
        },
        "reproducibility": {
            "tool_versions": {"pnpm": "11.28.8", "python": "3.12", "pyinstaller": "6.0"},
            "validated_at_utc": "2026-01-01T00:00:00+00:00",
        },
    }
    manifest_path = bundle / "modulo-runtime.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return bundle, manifest_path


def test_valid_manifest_is_contract_validation_only(tmp_path: Path) -> None:
    bundle, manifest_path = _bundle(tmp_path)

    manifest = validate(manifest_path, _SCHEMA, bundle)

    assert manifest["runtime_support"]["status"] == "unsupported"
    assert set(manifest["native_requirements"]["postgresql"]["binaries"]) == set(_POSTGRES_BINARIES)


def test_validate_rejects_manifest_path_outside_bundle(tmp_path: Path) -> None:
    bundle, _ = _bundle(tmp_path)
    outside_manifest = tmp_path / "manifest.json"
    outside_manifest.write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match="manifest path must be beneath the allowed local root"):
        validate(outside_manifest, _SCHEMA, bundle)


def test_validate_rejects_schema_path_outside_repository_scripts(tmp_path: Path) -> None:
    bundle, manifest_path = _bundle(tmp_path)
    outside_schema = tmp_path / "schema.json"
    outside_schema.write_text(_SCHEMA.read_text(encoding="utf-8"), encoding="utf-8")

    with pytest.raises(ValueError, match="schema path must be beneath the allowed local root"):
        validate(manifest_path, outside_schema, bundle)


def test_validate_does_not_mutate_the_manifest_it_validated(tmp_path: Path) -> None:
    bundle, manifest_path = _bundle(tmp_path)
    before = manifest_path.read_text(encoding="utf-8")

    returned = validate(manifest_path, _SCHEMA, bundle)

    assert manifest_path.read_text(encoding="utf-8") == before
    assert returned["reproducibility"]["validated_at_utc"] == "2026-01-01T00:00:00+00:00"


def test_print_inventory_is_the_builder_contract(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--print-inventory"]) == 0

    printed = json.loads(capsys.readouterr().out)
    assert printed == {
        "postgresql": {"relative_path": "postgres/bin", "binaries": list(_POSTGRES_BINARIES)},
        "redis": {"relative_path": "redis/bin", "binaries": list(_REDIS_BINARIES)},
    }


def test_forbidden_scaffolding_contract_is_pinned() -> None:
    assert set(_VALIDATOR_MODULE.FORBIDDEN_BUNDLE_ENTRIES) == set(_SCAFFOLDING_ENTRIES)


@pytest.mark.parametrize("entry", _SCAFFOLDING_ENTRIES)
def test_validate_rejects_bundle_build_scaffolding(tmp_path: Path, entry: str) -> None:
    bundle, manifest_path = _bundle(tmp_path)
    (bundle / entry).mkdir()

    with pytest.raises(ValueError, match="build scaffolding"):
        validate(manifest_path, _SCHEMA, bundle)


def test_check_bundle_cli_reports_scaffolding(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    bundle, _ = _bundle(tmp_path)
    (bundle / "pyinstaller-work").mkdir()

    exit_code = main(["--check-bundle", str(bundle)])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "pyinstaller-work" in captured.err
    assert captured.err.startswith("error: ")


def test_validate_rejects_nested_bundle_build_scaffolding(tmp_path: Path) -> None:
    bundle, manifest_path = _bundle(tmp_path)
    nested = bundle / "postgres" / "bin" / "pyinstaller-work"
    nested.mkdir(parents=True)

    with pytest.raises(ValueError, match="pyinstaller-work"):
        validate(manifest_path, _SCHEMA, bundle)


def test_cli_rejection_prints_one_error_line_without_a_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle, manifest_path = _bundle(tmp_path)
    (bundle / "redis" / "bin" / "redis-server.exe").unlink()

    exit_code = main(["--manifest", str(manifest_path), "--schema", str(_SCHEMA), "--bundle", str(bundle)])

    captured = capsys.readouterr()
    lines = [line for line in captured.err.splitlines() if line.strip()]
    assert exit_code == 1
    assert len(lines) == 1
    assert lines[0].startswith("error: ")
    assert "Traceback" not in captured.err


def test_missing_native_runtime_input_fails_closed(tmp_path: Path) -> None:
    bundle, manifest_path = _bundle(tmp_path)
    (bundle / "redis" / "bin" / "redis-server.exe").unlink()

    with pytest.raises(ValueError, match="redis binary is missing"):
        validate(manifest_path, _SCHEMA, bundle)


def test_manifest_schema_rejects_wrong_architecture(tmp_path: Path) -> None:
    bundle, manifest_path = _bundle(tmp_path)
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    data["architecture"] = "linux-amd64"
    manifest_path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(ValueError, match="amd64"):
        validate(manifest_path, _SCHEMA, bundle)


@pytest.mark.parametrize(
    "path",
    [
        "../modulo.exe",
        r"..\modulo.exe",
        r"subdir\..\..\escape.exe",
        r"C:\\outside\\modulo.exe",
        r"\outside\modulo.exe",
    ],
)
def test_manifest_rejects_rooted_or_traversing_launcher_path(tmp_path: Path, path: str) -> None:
    bundle, manifest_path = _bundle(tmp_path)
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    data["launcher"]["path"] = path
    manifest_path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(ValueError, match="launcher path"):
        validate(manifest_path, _SCHEMA, bundle)


@pytest.mark.parametrize(
    "path",
    [
        r"..\outside",
        r"postgres\..\..\escape",
    ],
)
def test_manifest_rejects_backslash_path_traversal_on_relative_path(tmp_path: Path, path: str) -> None:
    bundle, manifest_path = _bundle(tmp_path)
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    data["native_requirements"]["postgresql"]["relative_path"] = path
    manifest_path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(ValueError, match="relative path"):
        validate(manifest_path, _SCHEMA, bundle)


def test_manifest_rejects_a_native_layout_that_diverges_from_the_inventory(tmp_path: Path) -> None:
    bundle, manifest_path = _bundle(tmp_path)
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    data["native_requirements"]["postgresql"]["relative_path"] = "pg/bin"
    manifest_path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(ValueError, match="relative_path must be"):
        validate(manifest_path, _SCHEMA, bundle)


def test_manifest_rejects_missing_metadata_entry_for_listed_binary(tmp_path: Path) -> None:
    bundle, manifest_path = _bundle(tmp_path)
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    del data["native_requirements"]["postgresql"]["metadata"]["initdb.exe"]
    manifest_path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(ValueError, match="metadata is missing from manifest"):
        validate(manifest_path, _SCHEMA, bundle)


def test_manifest_rejects_missing_required_binary_from_inventory(tmp_path: Path) -> None:
    bundle, manifest_path = _bundle(tmp_path)
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    data["native_requirements"]["postgresql"]["binaries"].remove("initdb.exe")
    manifest_path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(ValueError, match="missing required postgresql binary from inventory"):
        validate(manifest_path, _SCHEMA, bundle)


def test_manifest_rejects_missing_required_redis_binary_from_inventory(tmp_path: Path) -> None:
    bundle, manifest_path = _bundle(tmp_path)
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    data["native_requirements"]["redis"]["binaries"].remove("redis-server.exe")
    manifest_path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(ValueError, match="missing required redis binary from inventory"):
        validate(manifest_path, _SCHEMA, bundle)


def test_manifest_rejects_binary_path_separator(tmp_path: Path) -> None:
    bundle, manifest_path = _bundle(tmp_path)
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    data["native_requirements"]["redis"]["binaries"][0] = "..\\redis-server.exe"
    manifest_path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(ValueError, match="binary name"):
        validate(manifest_path, _SCHEMA, bundle)


def test_manifest_rejects_changed_native_hash(tmp_path: Path) -> None:
    bundle, manifest_path = _bundle(tmp_path)
    (bundle / "postgres" / "bin" / "initdb.exe").write_bytes(b"changed")

    with pytest.raises(ValueError, match="hash does not match"):
        validate(manifest_path, _SCHEMA, bundle)


# ---------------------------------------------------------------------------
# Builder output-path guard: exercised through the real script so the guard that
# actually runs in the build is the guard that is proven.
# ---------------------------------------------------------------------------


def _fake_repo(tmp_path: Path) -> Path:
    """A throwaway repo root holding only what the guard needs to be reachable."""
    fake = tmp_path / "fake-repo"
    scripts = fake / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(_BUILDER, scripts / _BUILDER.name)
    shutil.copy2(_SCHEMA, scripts / _SCHEMA.name)
    shutil.copy2(_VALIDATOR_PATH, scripts / _VALIDATOR_PATH.name)
    (fake / "frontend").mkdir()
    (fake / "frontend" / "package.json").write_text(json.dumps({"version": "9.9.9"}), encoding="utf-8")
    (fake / "keep-me.txt").write_text("must survive a refused build", encoding="utf-8")
    return fake


def _clean_environment() -> dict[str, str]:
    environment = dict(os.environ)
    environment.pop("MODULO_WINDOWS_POSTGRES_DIR", None)
    environment.pop("MODULO_WINDOWS_REDIS_DIR", None)
    return environment


_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_ERROR_RENDER_GUTTER = re.compile(r"^\s*\|\s?")


def _plain(text: str) -> str:
    """Flatten PowerShell's wrapped error render: strip ANSI and the '     | ' gutter."""
    without_ansi = _ANSI_ESCAPE.sub("", text)
    lines = [_ERROR_RENDER_GUTTER.sub("", line) for line in without_ansi.splitlines()]
    return " ".join(" ".join(lines).split())


def _run_builder(
    script: Path,
    *arguments: str,
    workdir: Path,
) -> subprocess.CompletedProcess[str]:
    assert _PWSH is not None
    return subprocess.run(  # noqa: S603 - fixed argv, the repo's own builder script under test
        [_PWSH, "-NoProfile", "-NonInteractive", "-File", str(script), *arguments],
        cwd=str(workdir),
        env=_clean_environment(),
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )


@pytest.mark.skipif(_PWSH is None, reason="pwsh 7 is required to exercise the bundle builder")
@pytest.mark.parametrize(
    ("output_directory", "expected_refusal"),
    [
        (".", "is the repository root"),
        ("..", "not strictly beneath the repository root"),
        ("scripts/../../..", "not strictly beneath the repository root"),
        (".git/hooks", "inside a .git directory"),
    ],
)
def test_builder_refuses_an_unsafe_output_directory(
    tmp_path: Path, output_directory: str, expected_refusal: str
) -> None:
    fake = _fake_repo(tmp_path)

    result = _run_builder(
        fake / "scripts" / _BUILDER.name,
        "-OutputDirectory",
        output_directory,
        "-ValidateOnly",
        workdir=fake,
    )

    combined = result.stdout + result.stderr
    normalised = _plain(combined)
    assert result.returncode != 0
    assert expected_refusal in normalised
    assert (fake / "keep-me.txt").exists()


@pytest.mark.skipif(_PWSH is None, reason="pwsh 7 is required to exercise the bundle builder")
def test_builder_refuses_reparse_output_ancestor_without_touching_repo(
    tmp_path: Path,
) -> None:
    fake = _fake_repo(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    link = fake / "build"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"directory symlink unavailable: {exc}")

    result = _run_builder(
        fake / "scripts" / _BUILDER.name,
        "-OutputDirectory",
        "build/windows-bundle",
        "-ValidateOnly",
        workdir=fake,
    )

    normalised = _plain(result.stdout + result.stderr)
    assert result.returncode != 0
    assert "reparse point" in normalised
    assert not (outside / "windows-bundle").exists()
    assert (fake / "keep-me.txt").exists()


@pytest.mark.skipif(_PWSH is None, reason="pwsh 7 is required to exercise the bundle builder")
def test_builder_allows_a_safe_output_directory_and_writes_nothing_in_validate_only(tmp_path: Path) -> None:
    fake = _fake_repo(tmp_path)

    result = _run_builder(
        fake / "scripts" / _BUILDER.name,
        "-OutputDirectory",
        "build/windows-bundle",
        "-ValidateOnly",
        workdir=fake,
    )

    combined = result.stdout + result.stderr
    normalised = _plain(combined)
    assert "refusing OutputDirectory" not in normalised
    assert "Not checked:" in normalised
    assert not (fake / "build" / "windows-bundle").exists()
