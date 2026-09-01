"""CycloneDX SBOM generator for Modulo releases.

Parses Python (uv.lock) and JavaScript (pnpm-lock.yaml) dependency files
and produces a CycloneDX v1.5 JSON SBOM.

Usage:
    python backend/scripts/generate-sbom.py
    python backend/scripts/generate-sbom.py --output sbom.json
"""

import argparse
import json
import os
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path

import yaml

try:
    import tomllib
except ImportError:
    import tomli as tomllib  # type: ignore[no-redef]


def _safe_output_path(path: Path) -> Path:
    """Resolve *path* and require it to stay within the working directory."""
    resolved = os.path.realpath(str(path))
    base = os.path.realpath(Path.cwd())
    if resolved != base and not resolved.startswith(base + os.sep):
        raise ValueError(f"output path {str(path)!r} resolves outside the working directory")
    return Path(resolved)


def parse_uv_lock(lock_path: Path) -> list[dict]:
    with lock_path.open("rb") as f:
        data = tomllib.load(f)

    components = []
    for pkg in data.get("package", []):
        name = pkg.get("name", "")
        version = pkg.get("version", "")
        if not name or not version:
            continue
        if name == "modulo":
            continue

        purl = f"pkg:pypi/{name}@{version}"
        components.append(
            {
                "type": "library",
                "bom-ref": purl,
                "name": name,
                "version": version,
                "purl": purl,
                "evidence": {
                    "identity": {
                        "field": "purl",
                        "confidence": 1.0,
                        "methods": [{"technique": "manifest-analysis", "confidence": 1.0, "value": "uv.lock"}],
                    }
                },
            }
        )

    return sorted(components, key=lambda c: c["name"].lower())


def parse_pnpm_lock(lock_path: Path) -> list[dict]:
    with lock_path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    components = []
    for key, info in data.get("packages", {}).items():
        if not key or not isinstance(info, dict):
            continue

        version = info.get("version", "")
        key_without_peers = key.split("(", 1)[0].strip()

        if "node_modules/" in key_without_peers:
            name = key_without_peers.split("node_modules/")[-1]
        else:
            match = re.match(r"^(.*?)@([^@]+)$", key_without_peers)
            if match:
                name, version_from_key = match.group(1), match.group(2)
                name, version = name.strip(), version or version_from_key
            else:
                name = key_without_peers

        if not name or not version:
            continue

        purl = f"pkg:npm/{name}@{version}"
        components.append(
            {
                "type": "library",
                "bom-ref": purl,
                "name": name,
                "version": version,
                "purl": purl,
                "evidence": {
                    "identity": {
                        "field": "purl",
                        "confidence": 1.0,
                        "methods": [{"technique": "manifest-analysis", "confidence": 1.0, "value": "pnpm-lock.yaml"}],
                    }
                },
            }
        )

    return sorted(components, key=lambda c: c["name"].lower())


def generate_sbom(components: list[dict], version: str, timestamp: str, supplier: str, product: str) -> dict:
    serial = str(uuid.uuid4())

    return {
        "$schema": "https://cyclonedx.org/schema/bom-1.5.schema.json",
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "serialNumber": f"urn:uuid:{serial}",
        "version": 1,
        "metadata": {
            "timestamp": timestamp,
            "tools": [
                {
                    "vendor": supplier,
                    "name": "modulo-release-script",
                    "version": version,
                }
            ],
            "component": {
                "type": "application",
                "name": product,
                "version": version,
                "supplier": {"name": supplier},
            },
        },
        "components": components,
    }


def main():
    parser = argparse.ArgumentParser(description="Generate CycloneDX SBOM for Modulo")
    parser.add_argument("--output", "-o", help="Output file path (default: stdout)")
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parent.parent.parent

    # Read version from pyproject.toml
    pyproject_path = repo_root / "backend" / "pyproject.toml"
    with pyproject_path.open("rb") as f:
        pyproject = tomllib.load(f)
    version = pyproject.get("project", {}).get("version", "0.0.0")

    # Read SBOM config (supplier, product name)
    config_path = repo_root / "backend" / "scripts" / "sbom-config.json"
    with config_path.open(encoding="utf-8") as f:
        config = json.load(f)
    supplier = config.get("supplier", "Farnalabs")
    product = config.get("product", "Modulo")

    # Parse Python dependencies
    uv_lock_path = repo_root / "backend" / "uv.lock"
    python_components = []
    if uv_lock_path.exists():
        python_components = parse_uv_lock(uv_lock_path)

    # Parse JavaScript dependencies
    pnpm_lock_path = repo_root / "frontend" / "pnpm-lock.yaml"
    js_components = []
    if pnpm_lock_path.exists():
        js_components = parse_pnpm_lock(pnpm_lock_path)

    all_components = python_components + js_components
    timestamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    bom = generate_sbom(all_components, version, timestamp, supplier, product)

    output = json.dumps(bom, indent=2, ensure_ascii=False)

    if args.output:
        output_path = _safe_output_path(Path(args.output))
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(output + "\n", encoding="utf-8")
    else:
        print(output)


if __name__ == "__main__":
    main()
