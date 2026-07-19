import os
from pathlib import Path
from typing import Any

import yaml

Manifest = dict[str, Any]

_MANIFEST: Manifest | None = None


def get_manifest_path() -> Path:
    env_path = os.environ.get("MANIFEST_PATH")
    if env_path:
        return Path(env_path)
    docker_path = Path("/app/manifest.yaml")
    if docker_path.exists():
        return docker_path
    # Path: backend/src/modulo/core/manifest.py -> 5 parents = project root
    return Path(__file__).parent.parent.parent.parent.parent / "frontend" / "src" / "manifest.yaml"


def load_manifest() -> Manifest:
    global _MANIFEST
    path = get_manifest_path()
    if path.exists():
        try:
            with open(path) as f:
                loaded = yaml.safe_load(f)
                if not isinstance(loaded, dict):
                    raise ValueError("manifest root must be a mapping")
                _MANIFEST = loaded
        except (yaml.YAMLError, OSError, ValueError) as exc:
            raise RuntimeError(f"Failed to load manifest from {path}: {exc}") from exc
    else:
        _MANIFEST = {"routes": {}, "elements": {}, "sidebar_groups": {}}
    return _MANIFEST


def get_manifest() -> Manifest:
    if _MANIFEST is None:
        return load_manifest()
    return _MANIFEST
