"""Unit tests for the new helper functions in backend/scripts/generate-sbom.py."""

from __future__ import annotations

from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path

for parent in Path(__file__).resolve().parents:
    script_path = parent / "backend" / "scripts" / "generate-sbom.py"
    if script_path.exists():
        break
else:
    raise RuntimeError("Could not find backend/scripts/generate-sbom.py")

_loader = SourceFileLoader("generate_sbom", str(script_path))
mod = module_from_spec(spec_from_loader("generate_sbom", _loader))
_loader.exec_module(mod)


def test_package_name_and_version_node_modules_path():
    # For node_modules paths the version is not stripped from the name; the
    # version is taken from the info dict instead.
    name, version = mod._package_name_and_version("node_modules/foo/bar@1.2.3", {"version": "1.2.3"})
    assert name == "foo/bar@1.2.3"
    assert version == "1.2.3"


def test_package_name_and_version_node_modules_path_empty_info_version():
    name, version = mod._package_name_and_version("node_modules/@scope/pkg@2.0.0", {"version": ""})
    assert name == "@scope/pkg@2.0.0"
    assert version == ""


def test_package_name_and_version_plain_key_with_version():
    name, version = mod._package_name_and_version("@scope/pkg@3.1.4", {"version": ""})
    assert name == "@scope/pkg"
    assert version == "3.1.4"


def test_package_name_and_version_uses_info_version_when_key_has_none():
    name, version = mod._package_name_and_version("left-pad@1.0.0", {"version": "9.9.9"})
    assert name == "left-pad"
    assert version == "9.9.9"


def test_package_name_and_version_info_version_only():
    name, version = mod._package_name_and_version("left-pad", {"version": "4.5.6"})
    assert name == "left-pad"
    assert version == "4.5.6"


def test_package_name_and_version_key_with_peers_is_stripped():
    name, version = mod._package_name_and_version("left-pad@1.0.0(peer@2.0.0)", {"version": ""})
    assert name == "left-pad"
    assert version == "1.0.0"


def test_package_name_and_version_no_version_anywhere():
    name, version = mod._package_name_and_version("weird-pkg", {"version": ""})
    assert name == "weird-pkg"
    assert version == ""


def test_parse_uv_lock_returns_components(tmp_path):
    lock = tmp_path / "uv.lock"
    lock.write_text(
        "\n".join(
            [
                "version = 1",
                'requires-python = ">=3.12"',
                "[[package]]",
                'name = "requests"',
                'version = "2.31.0"',
                "[[package]]",
                'name = "modulo"',
                'version = "0.1.0"',
                "[[package]]",
                'name = "left-pad"',
                'version = "1.0.0"',
            ]
        )
    )
    components = mod.parse_uv_lock(lock)
    names = [c["name"] for c in components]
    assert "requests" in names
    assert "left-pad" in names
    # The project's own package is skipped and results are name-sorted.
    assert "modulo" not in names
    assert names == sorted(names, key=str.lower)
    for c in components:
        assert c["purl"].startswith("pkg:pypi/")


def test_parse_pnpm_lock_returns_components(tmp_path):
    lock = tmp_path / "pnpm-lock.yaml"
    lock.write_text(
        "\n".join(
            [
                "lockfileVersion: '9.0'",
                "packages:",
                "  left-pad@1.0.0:",
                "    version: 1.0.0",
                "  '@scope/pkg@2.0.0':",
                "    version: 2.0.0",
                "  '':",
                "    version: ignored",
            ]
        )
    )
    components = mod.parse_pnpm_lock(lock)
    names = {c["name"] for c in components}
    assert "left-pad" in names
    assert "@scope/pkg" in names
    for c in components:
        assert c["purl"].startswith("pkg:npm/")


def test_generate_sbom_builds_cyclonedx_document():
    components = [{"name": "x", "version": "1.0"}]
    doc = mod.generate_sbom(components, "1.2.3", "2026-01-01T00:00:00Z", "Farnalabs", "modulo")
    assert doc["bomFormat"] == "CycloneDX"
    assert doc["specVersion"] == "1.5"
    assert doc["metadata"]["component"]["name"] == "modulo"
    assert doc["metadata"]["component"]["version"] == "1.2.3"
    assert doc["components"] == components
    assert doc["serialNumber"].startswith("urn:uuid:")
