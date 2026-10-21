import os
from pathlib import Path
from typing import Any

import yaml

Manifest = dict[str, Any]

_MANIFEST: Manifest | None = None

_MERGE_TAG = "tag:yaml.org,2002:merge"


class _NoDuplicateKeysLoader(yaml.SafeLoader):
    """SafeLoader that rejects literal duplicate mapping keys.

    PyYAML silently lets the last duplicate key win (``yaml.safe_load``), so a
    mistyped route/element/sidebar-group name in ``manifest.yaml`` can silently
    override an existing entry instead of erroring. Merge keys (``<<: *anchor``)
    are legitimate override points and are exempt — the check runs on the raw
    mapping node before anchor flattening.
    """


def _construct_no_duplicate_mapping(loader: _NoDuplicateKeysLoader, node: yaml.MappingNode) -> object:
    seen: set[Any] = set()
    for key_node, _ in node.value:
        if getattr(key_node, "tag", None) == _MERGE_TAG:
            continue
        key = loader.construct_object(key_node, deep=True)
        if key in seen:
            raise yaml.constructor.ConstructorError(
                None,
                None,
                f"duplicate mapping key {key!r} in {getattr(node, 'start_mark', '')}",
                node.start_mark,
            )
        seen.add(key)
    loader.flatten_mapping(node)
    return loader.construct_mapping(node, deep=True)


_NoDuplicateKeysLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_no_duplicate_mapping)


def _load_manifest_yaml(path: Path) -> Any:
    """Load *path* via the duplicate-key-rejecting SafeLoader.

    Uses the loader-instance API (``SafeLoader(stream).get_single_data()``)
    rather than ``yaml.load(...)`` so the strict ``yaml.safe_load`` semgrep rule
    and bandit's S506 both stay satisfied — the loader subclasses ``yaml.SafeLoader``
    and can never instantiate arbitrary objects.
    """
    with path.open() as f:
        loader = _NoDuplicateKeysLoader(f)
        try:
            return loader.get_single_data()
        finally:
            loader.dispose()


def get_manifest_path() -> Path:
    """Locate ``manifest.yaml`` without depending on the current directory.

    Resolution order: the ``MANIFEST_PATH`` environment variable when set, the
    container path ``/app/manifest.yaml`` when it exists, then the in-repo
    ``frontend/src/manifest.yaml`` resolved relative to this module.
    """
    env_path = os.environ.get("MANIFEST_PATH")
    if env_path:
        return Path(env_path)
    docker_path = Path("/app/manifest.yaml")
    if docker_path.exists():
        return docker_path
    # Path: backend/src/modulo/core/manifest.py -> 5 parents = project root
    return Path(__file__).parent.parent.parent.parent.parent / "frontend" / "src" / "manifest.yaml"


def load_manifest() -> Manifest:
    """Load and cache the manifest from :func:`get_manifest_path`.

    A missing file yields an empty manifest structure, so callers never have to
    handle ``None``. A file that exists but is not a YAML mapping raises
    ``RuntimeError`` — a broken manifest must fail loudly rather than silently
    serving an empty one.
    """
    global _MANIFEST
    path = get_manifest_path()
    if path.exists():
        try:
            loaded = _load_manifest_yaml(path)
            if not isinstance(loaded, dict):
                raise ValueError("manifest root must be a mapping")
            _MANIFEST = loaded
        except (yaml.YAMLError, OSError, ValueError) as exc:
            raise RuntimeError(f"Failed to load manifest from {path}: {exc}") from exc
    else:
        _MANIFEST = {"routes": {}, "elements": {}, "sidebar_groups": {}}
    return _MANIFEST


def get_manifest() -> Manifest:
    """Return the cached manifest, loading it on first use.

    The returned mapping is the shared cache entry: callers must treat it as
    read-only rather than mutating it in place.
    """
    if _MANIFEST is None:
        return load_manifest()
    return _MANIFEST
