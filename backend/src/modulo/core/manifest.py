import os
from pathlib import Path

import yaml

_MANIFEST: dict | None = None


def get_manifest_path() -> Path:
    env_path = os.environ.get("MANIFEST_PATH")
    if env_path:
        return Path(env_path)
    docker_path = Path("/app/manifest.yaml")
    if docker_path.exists():
        return docker_path
    # Path: backend/src/modulo/core/manifest.py -> 5 parents = project root
    return Path(__file__).parent.parent.parent.parent.parent / "frontend" / "src" / "manifest.yaml"


def load_manifest() -> dict:
    global _MANIFEST
    path = get_manifest_path()
    if path.exists():
        with open(path) as f:
            _MANIFEST = yaml.safe_load(f)
    else:
        _MANIFEST = {"routes": {}, "elements": {}, "sidebar_groups": {}}
    return _MANIFEST


def get_manifest() -> dict:
    if _MANIFEST is None:
        return load_manifest()
    return _MANIFEST
