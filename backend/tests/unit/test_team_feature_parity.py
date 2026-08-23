"""Catalog-vs-enforcement parity for team-tier feature flags.

Every ``team``-tier feature declared in the catalog must actually be enforced
somewhere in the codebase: either a ``require_feature("<name>")`` /
``feature_enabled("<name>")`` / ``get_flag("<name>")`` check in
``backend/src/modulo`` (route gate or service level), a
``feature-name="<name>"`` / ``featureEnabled("<name>")`` reference in the
frontend, or a service-level check.

A small, explicit set of team flags are declared in the catalog but not yet
enforced anywhere. Those are tracked in ``KNOWN_UNENFORCED_TEAM_FLAGS`` so the
test documents the gap without failing on it — but fails loudly the moment the
gap changes (a new unenforced team flag appears, or one of the known gaps gets
closed without being removed from the set).
"""

from __future__ import annotations

from pathlib import Path

from modulo.core.feature_flags import _KNOWN_FLAGS

# Repo root: this file lives at backend/tests/unit/test_team_feature_parity.py
_REPO_ROOT = Path(__file__).resolve().parents[3]

_BACKEND_SRC = _REPO_ROOT / "backend" / "src" / "modulo"
_FRONTEND_SRC = _REPO_ROOT / "frontend" / "src"

# Team-tier flags declared in the catalog but intentionally not yet enforced
# anywhere (backend route gate, frontend FeatureGate, or service check).
# ``scim`` (ctx.feature_enabled("scim") in auth/scim_auth.py),
# ``external_secrets`` (registry.get_flag("external_secrets") in
# core/secrets_backend/__init__.py) and ``pipeline_delete``
# (planStore.featureEnabled('pipeline_delete') in the frontend pipeline
# views) ARE enforced — scim/external_secrets at the SERVICE level,
# pipeline_delete in the FRONTEND — so the detector correctly classifies them
# as enforced and they do NOT belong in this gap set.
KNOWN_UNENFORCED_TEAM_FLAGS: set[str] = {
    "checkpoint_encryption",
    "audit_crypto_chain",
    "community_registry",
    "prompt_optimization",
    "schema_union_types",
    "migration_cli",
}


def _team_flag_names() -> set[str]:
    return {flag.name for flag in _KNOWN_FLAGS if flag.tier == "team"}


def _backend_files_text() -> str:
    if not _BACKEND_SRC.is_dir():
        return ""
    parts: list[str] = []
    for path in sorted(_BACKEND_SRC.rglob("*.py")):
        parts.append(path.read_text(encoding="utf-8"))
    return "\n".join(parts)


def _frontend_files_text() -> str:
    if not _FRONTEND_SRC.is_dir():
        return ""
    parts: list[str] = []
    for path in sorted(_FRONTEND_SRC.rglob("*")):
        if path.is_file() and path.suffix in {".vue", ".ts", ".tsx", ".js"}:
            parts.append(path.read_text(encoding="utf-8"))
    return "\n".join(parts)


def _is_enforced_in_backend(name: str, backend_text: str) -> bool:
    return (
        f'require_feature("{name}")' in backend_text
        or f'feature_enabled("{name}")' in backend_text
        or f'get_flag("{name}")' in backend_text
    )


def _is_enforced_in_frontend(name: str, frontend_text: str) -> bool:
    return (
        f'feature-name="{name}"' in frontend_text
        or f'featureEnabled("{name}")' in frontend_text
        or f"featureEnabled('{name}')" in frontend_text
    )


def _unenforced_team_flags() -> set[str]:
    backend_text = _backend_files_text()
    frontend_text = _frontend_files_text()
    unenforced: set[str] = set()
    for name in _team_flag_names():
        if name in KNOWN_UNENFORCED_TEAM_FLAGS:
            continue
        backend = _is_enforced_in_backend(name, backend_text)
        frontend = _is_enforced_in_frontend(name, frontend_text)
        if not backend and not frontend:
            unenforced.add(name)
    return unenforced


def test_every_non_known_team_flag_is_enforced() -> None:
    unenforced = _unenforced_team_flags()
    assert unenforced == set(), (
        f"Team-tier flags are declared in the catalog but not enforced anywhere: {sorted(unenforced)}. "
        "Add a require_feature/feature_enabled/get_flag check in backend/src/modulo, a FeatureGate/featureEnabled "
        "reference in frontend/src/, or a service-level check. If the flag is intentionally not yet "
        "shipped, add it to KNOWN_UNENFORCED_TEAM_FLAGS instead."
    )


def test_known_unenforced_set_matches_catalog_gap() -> None:
    """The KNOWN_UNENFORCED_TEAM_FLAGS set must exactly match the current gap.

    Fails if a known gap was closed (enforced) without removing it from the
    set, or if the catalog gained a new team flag that needs to be classified.
    """
    team_flags = _team_flag_names()
    unknown_known = KNOWN_UNENFORCED_TEAM_FLAGS - team_flags
    assert not unknown_known, (
        f"KNOWN_UNENFORCED_TEAM_FLAGS references flags not in the catalog: {sorted(unknown_known)}"
    )

    actually_unenforced = set()
    backend_text = _backend_files_text()
    frontend_text = _frontend_files_text()
    for name in team_flags:
        backend = _is_enforced_in_backend(name, backend_text)
        frontend = _is_enforced_in_frontend(name, frontend_text)
        if not backend and not frontend:
            actually_unenforced.add(name)

    assert actually_unenforced == KNOWN_UNENFORCED_TEAM_FLAGS, (
        f"Gap mismatch: expected unenforced set {sorted(KNOWN_UNENFORCED_TEAM_FLAGS)} "
        f"but catalog shows {sorted(actually_unenforced)}. If a flag was enforced, remove it "
        "from KNOWN_UNENFORCED_TEAM_FLAGS. If a new flag is unenforced, add it."
    )
