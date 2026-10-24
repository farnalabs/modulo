"""FAR-2104: the licence screen's audience is ONE definition, mechanically pinned.

The bug this guards: ``/settings/license`` was system-admin-only while the
licence read/write endpoints were org-admin, so the organisation administrator
who bought the licence could see the renewal warning but could not act on it.
The renewal notice added a third, independent role check.

The fix makes ``org.license.manage`` the single definition of "can manage the
licence", consumed by:

* the manifest route entry (``required_permissions``) — which drives the router
  guard and the sidebar item;
* the authenticated shell's renewal-notice audience
  (``composables/useLicenceAccess.ts``);
* the licence page's org-scoped controls.

This module reads the artefacts (the live FastAPI app, the manifest, the
frontend sources) and asserts they agree, so a future change cannot reintroduce
a parallel role check without failing here. It intentionally asserts AGREEMENT,
not a hard-coded role: the role that backs the permission lives in the registry
and is free to change.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from modulo.api.main import app
from tests.unit.api.route_introspection import get_all_apiroutes, get_permission_tag

REPO_ROOT = Path(__file__).resolve().parents[4]
FRONTEND_SRC = REPO_ROOT / "frontend" / "src"
LICENCE_ACCESS_TS = FRONTEND_SRC / "composables" / "useLicenceAccess.ts"
LICENCE_VIEW_VUE = FRONTEND_SRC / "views" / "SettingsLicenseView.vue"
APP_LAYOUT_VUE = FRONTEND_SRC / "components" / "AppLayout.vue"
MANIFEST_YAML = FRONTEND_SRC / "manifest.yaml"

LICENCE_PATH = "/settings/license"
LICENCE_READ_PATH = "/api/v1/admin/license"
ORG_LICENCE_WRITE_PATH = "/api/v1/admin/orgs/{org_id}/license"


def _frontend_constant(name: str) -> str:
    """Read an exported string constant from the shared access composable."""
    source = LICENCE_ACCESS_TS.read_text(encoding="utf-8")
    match = re.search(rf"export const {name} = '([^']+)'", source)
    assert match is not None, f"{name} must be exported as a string literal from {LICENCE_ACCESS_TS}"
    return match.group(1)


def _route(method: str, path: str) -> Any:
    for route in get_all_apiroutes(app):
        if route.path != path:
            continue
        if method not in (route.methods or set()):
            continue
        return route
    raise AssertionError(f"{method} {path} must be registered on the app")


def _manifest() -> dict[str, Any]:
    return yaml.safe_load(MANIFEST_YAML.read_text(encoding="utf-8"))


def test_licence_read_is_reachable_by_the_org_admin_permission() -> None:
    """The read admits exactly the principals the org-scoped writes admit."""
    org_permission = _frontend_constant("ORG_LICENSE_MANAGE_PERMISSION")
    tag = get_permission_tag(_route("GET", LICENCE_READ_PATH))
    assert tag is not None, "GET /api/v1/admin/license must carry a permission gate"
    assert tag["permission"] == org_permission
    # A strict `system` gate would lock the org admin out; a tenant-only gate
    # would lock out a system admin whose org role is below admin.
    assert tag["permission_kind"] in {"tenant", "system_or_org"}


def test_org_licence_writes_use_the_same_permission_as_the_read() -> None:
    org_permission = _frontend_constant("ORG_LICENSE_MANAGE_PERMISSION")
    for method in ("PUT", "DELETE"):
        tag = get_permission_tag(_route(method, ORG_LICENCE_WRITE_PATH))
        assert tag is not None, f"{method} {ORG_LICENCE_WRITE_PATH} must carry a permission gate"
        assert tag["permission"] == org_permission


def test_system_licence_install_uses_the_system_permission() -> None:
    """The instance-wide install stays a strict system-admin control."""
    system_permission = _frontend_constant("SYSTEM_CONFIG_MANAGE_PERMISSION")
    tag = get_permission_tag(_route("POST", LICENCE_READ_PATH))
    assert tag is not None, "POST /api/v1/admin/license must carry a permission gate"
    assert tag["permission"] == system_permission
    assert tag["permission_kind"] == "system"


def test_manifest_route_is_gated_by_the_endpoint_permission_not_system_admin() -> None:
    manifest = _manifest()
    entry = manifest["routes"][LICENCE_PATH]
    assert entry["required_permissions"] == [_frontend_constant("ORG_LICENSE_MANAGE_PERMISSION")]
    assert not entry.get("system_admin_only")
    group = manifest["sidebar_groups"][entry["sidebar_group"]]
    assert not group.get("system_admin_only"), (
        "the licence page cannot live in a system-admin-only group if an org admin must reach it"
    )
    # A lapsed licence resolves to the community tier; the page that installs a
    # replacement key must therefore be reachable at the community floor.
    assert entry["required_tier"] == "community"


def test_shell_notice_audience_is_derived_from_the_permission() -> None:
    """The banner's audience comes from the composable, never a role check."""
    source = APP_LAYOUT_VUE.read_text(encoding="utf-8")
    assert "useLicenceAccess" in source
    assert "canManageOrgLicence" in source
    assert 'const canManageLicence = computed(() => userRole.value === "admin")' not in source


def test_licence_page_uses_the_shared_access_derivation() -> None:
    source = LICENCE_VIEW_VUE.read_text(encoding="utf-8")
    assert "useLicenceAccess" in source
    assert "canManageOrgLicence" in source
    assert "canManageSystemLicence" in source
