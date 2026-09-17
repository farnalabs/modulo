"""Reference-integrity guard for feature flags.

Prevents two classes of drift:

1. **Phantom flags** — a flag name referenced by a frontend gate
   (``feature-name="X"`` / ``featureEnabled('X')``), a backend gate
   (``require_feature("X")`` / ``feature_enabled("X")`` / ``get_flag("X")`` /
   ``resolve_flag("X")``), or the manifest (``feature_flag: X``) that does NOT
   exist in ``_KNOWN_FLAGS``. These silently fail-closed.

2. **Orphan flags** — a flag in ``_KNOWN_FLAGS`` that has zero references
   in production code outside the two registry files (``feature_flags.py`` and
   ``catalog.py``). These are dead weight — registered but never gated.
"""

from __future__ import annotations

import re
from pathlib import Path

from modulo.core.feature_flags import _KNOWN_FLAGS
from modulo.core.seed_data.catalog import FLAGS

_REPO_ROOT = Path(__file__).resolve().parents[3]

_BACKEND_SRC = _REPO_ROOT / "backend" / "src" / "modulo"
_FRONTEND_SRC = _REPO_ROOT / "frontend" / "src"
_MANIFEST = _FRONTEND_SRC / "manifest.yaml"

# Registry files — references here don't count as "enforced".
_REGISTRY_FILES: set[Path] = {
    _REPO_ROOT / "backend" / "src" / "modulo" / "core" / "feature_flags.py",
    _REPO_ROOT / "backend" / "src" / "modulo" / "core" / "seed_data" / "catalog.py",
}

# Flags registered in _KNOWN_FLAGS but intentionally not yet gated anywhere
# outside the registry.  These are deferred features — tracked here so the
# orphan check documents the gap without failing on it.  When a deferred flag
# gains a gate, remove it from this set; when a new flag is registered
# without a gate, add it here.
KNOWN_DEFERRED_FLAGS: set[str] = {
    "eval_maturity",
}

# Patterns for backend gate references (production code only).
_BACKEND_PATTERNS = [
    re.compile(r'require_feature\("([a-z_]+)"\)'),
    re.compile(r'feature_enabled\("([a-z_]+)"\)'),
    re.compile(r'get_flag\("([a-z_]+)"\)'),
    re.compile(r'resolve_flag\("([a-z_]+)"\)'),
]

# Patterns for frontend gate references (production code only).
_FRONTEND_PATTERNS = [
    re.compile(r'feature-name="([a-z_]+)"'),
    re.compile(r"featureEnabled\('([a-z_]+)'\)"),
    re.compile(r'featureEnabled\("([a-z_]+)"\)'),
]

_MANIFEST_PATTERN = re.compile(r"feature_flag:\s*([a-z_]+)")


def _collect_backend_refs() -> set[str]:
    """Collect flag names from backend production code (excludes tests)."""
    names: set[str] = set()
    if not _BACKEND_SRC.is_dir():
        return names
    for path in sorted(_BACKEND_SRC.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for pat in _BACKEND_PATTERNS:
            names.update(pat.findall(text))
    return names


def _collect_frontend_refs() -> set[str]:
    """Collect flag names from frontend production code (excludes tests)."""
    names: set[str] = set()
    if not _FRONTEND_SRC.is_dir():
        return names
    for path in sorted(_FRONTEND_SRC.rglob("*")):
        if not path.is_file() or path.suffix not in {".vue", ".ts", ".tsx", ".js"}:
            continue
        # Skip test files — test fixtures like "nonexistent" are not phantom gates.
        if "__tests__" in path.parts or path.name.endswith(".spec.ts"):
            continue
        text = path.read_text(encoding="utf-8")
        for pat in _FRONTEND_PATTERNS:
            names.update(pat.findall(text))
    return names


def _collect_manifest_refs() -> set[str]:
    names: set[str] = set()
    if not _MANIFEST.is_file():
        return names
    text = _MANIFEST.read_text(encoding="utf-8")
    names.update(_MANIFEST_PATTERN.findall(text))
    return names


def _non_registry_text() -> str:
    """Concatenate all non-registry production source files for flag-name searching."""
    parts: list[str] = []
    # Backend non-registry .py files
    if _BACKEND_SRC.is_dir():
        for path in sorted(_BACKEND_SRC.rglob("*.py")):
            if path in _REGISTRY_FILES:
                continue
            parts.append(path.read_text(encoding="utf-8"))
    # Frontend .vue/.ts files (excluding tests)
    if _FRONTEND_SRC.is_dir():
        for path in sorted(_FRONTEND_SRC.rglob("*")):
            if not path.is_file() or path.suffix not in {".vue", ".ts", ".tsx", ".js"}:
                continue
            if "__tests__" in path.parts or path.name.endswith(".spec.ts"):
                continue
            parts.append(path.read_text(encoding="utf-8"))
    # Manifest
    if _MANIFEST.is_file():
        parts.append(_MANIFEST.read_text(encoding="utf-8"))
    return "\n".join(parts)


def _known_flag_names() -> set[str]:
    return {flag.name for flag in _KNOWN_FLAGS}


# ── Tests ─────────────────────────────────────────────────────────────────


def test_no_phantom_flags() -> None:
    """Every flag name referenced by a production gate must exist in _KNOWN_FLAGS."""
    known = _known_flag_names()
    refs = _collect_backend_refs() | _collect_frontend_refs() | _collect_manifest_refs()
    phantoms = sorted(refs - known)
    assert not phantoms, (
        f"Flag names referenced in production code but NOT in _KNOWN_FLAGS (phantom flags): {phantoms}. "
        "These fail-closed silently. Add them to _KNOWN_FLAGS or remove the reference."
    )


def test_no_orphan_flags() -> None:
    """Every flag in _KNOWN_FLAGS must have at least one reference outside registry files.

    Flags that are registered but intentionally deferred (no gate yet) are
    tracked in ``KNOWN_DEFERRED_FLAGS``. If a deferred flag gains a gate,
    remove it from the set; if a new flag is registered without a gate,
    add it here.
    """
    known = _known_flag_names()
    non_registry = _non_registry_text()
    orphans: list[str] = []
    for name in sorted(known):
        if name in KNOWN_DEFERRED_FLAGS:
            continue
        # A flag is "referenced" if its quoted name appears anywhere in
        # non-registry production source (backend routes, frontend views,
        # manifest).
        if f'"{name}"' not in non_registry and f"'{name}'" not in non_registry:
            orphans.append(name)
    assert not orphans, (
        f"Flags in _KNOWN_FLAGS with zero external references (orphans): {orphans}. "
        "These are registered but never gated. Delete them or add a gate "
        "or add them to KNOWN_DEFERRED_FLAGS."
    )


def test_deferred_set_matches_known_gaps() -> None:
    """KNOWN_DEFERRED_FLAGS must exactly match the current gap.

    Fails if a deferred flag gains a gate (remove from KNOWN_DEFERRED_FLAGS)
    or if a new flag is registered without a gate (add to KNOWN_DEFERRED_FLAGS).
    """
    known = _known_flag_names()
    non_registry = _non_registry_text()
    actually_deferred: list[str] = []
    for name in sorted(known):
        if f'"{name}"' not in non_registry and f"'{name}'" not in non_registry:
            actually_deferred.append(name)
    assert set(actually_deferred) == KNOWN_DEFERRED_FLAGS, (
        f"Deferred-flag gap mismatch: expected {sorted(KNOWN_DEFERRED_FLAGS)} "
        f"but found {actually_deferred}. If a flag gained a gate, remove it "
        "from KNOWN_DEFERRED_FLAGS. If a new flag is ungated, add it."
    )


def test_catalog_derives_from_known_flags() -> None:
    """catalog.FLAGS must be derived from _KNOWN_FLAGS — same names, same tiers."""
    known = {flag.name: flag.tier for flag in _KNOWN_FLAGS}
    seeded = {entry["name"]: entry["tier_id"] for entry in FLAGS}
    assert known == seeded, (
        f"catalog.FLAGS is out of sync with _KNOWN_FLAGS. "
        f"Missing from catalog: {sorted(set(known) - set(seeded))}. "
        f"Extra in catalog: {sorted(set(seeded) - set(known))}. "
        "catalog.FLAGS must be derived from _KNOWN_FLAGS."
    )
