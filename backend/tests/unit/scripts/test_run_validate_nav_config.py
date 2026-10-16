"""Unit tests for scripts/run_validate_nav_config.py (FAR-52 nav structure gate).

Deliverable 1 (group-id / sidebar_order uniqueness, presence, referential
sanity) and deliverable 3 (manifest <-> navigation.ts route-id parity). Every
rule gets a violated-rule FAIL case against a seeded temp tree plus a PASS
case against the real manifest.
"""

from __future__ import annotations

from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path

for parent in Path(__file__).resolve().parents:
    script_path = parent / "scripts" / "run_validate_nav_config.py"
    if script_path.exists():
        break
else:
    raise RuntimeError("Could not find repo root (scripts/run_validate_nav_config.py)")

_loader = SourceFileLoader("run_validate_nav_config", str(script_path))
mod = module_from_spec(spec_from_loader("run_validate_nav_config", _loader))
_loader.exec_module(mod)

_VALID_MANIFEST = """\
routes:
    /:
        name: dashboard
        breadcrumb: Dashboard
        sidebar_group: build
        sidebar_order: 1
        type: page
    /settings:
        name: settings
        breadcrumb: Settings
        sidebar_group: build
        sidebar_order: 2
        type: page
sidebar_groups:
    build:
        label: BUILD
        order: 1
        default_expanded: true
"""

_VALID_NAV = """\
const routeConfigMap: Record<string, { icon: string; labelKey: string }> = {
  dashboard: { icon: 'LayoutDashboard', labelKey: 'components.SidebarNav.item_dashboard' },
  settings: { icon: 'Settings', labelKey: 'components.SidebarNav.item_settings' },
}

const groupLabelKeyMap: Record<string, string> = {
  build: 'components.SidebarNav.group_build',
}
"""


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _seed(tmp_path: Path, monkeypatch, manifest: str, nav: str) -> None:
    monkeypatch.setattr(mod, "REPO_ROOT", str(tmp_path))
    _write(tmp_path / mod._MANIFEST_PATH, manifest)
    _write(tmp_path / mod._NAV_PATH, nav)


def test_main_ok_on_real_manifest():
    """PASS case: the shipped manifest + navigation.ts satisfy every rule."""
    assert mod._main([]) == 0


def test_main_ok_on_valid_seed(tmp_path, monkeypatch, capsys):
    _seed(tmp_path, monkeypatch, _VALID_MANIFEST, _VALID_NAV)

    assert mod._main([]) == 0
    assert "run_validate_nav_config: OK" in capsys.readouterr().err


def test_unknown_sidebar_group_fails(tmp_path, monkeypatch, capsys):
    manifest = _VALID_MANIFEST.replace("sidebar_group: build", "sidebar_group: nope", 1)
    _seed(tmp_path, monkeypatch, manifest, _VALID_NAV)

    assert mod._main([]) == 1
    err = capsys.readouterr().err
    assert "undeclared sidebar_group" in err
    assert "'nope'" in err


def test_missing_sidebar_order_fails(tmp_path, monkeypatch, capsys):
    manifest = _VALID_MANIFEST.replace("        sidebar_order: 2\n", "")
    _seed(tmp_path, monkeypatch, manifest, _VALID_NAV)

    assert mod._main([]) == 1
    assert "must carry an integer sidebar_order" in capsys.readouterr().err


def test_non_integer_sidebar_order_fails(tmp_path, monkeypatch, capsys):
    manifest = _VALID_MANIFEST.replace("sidebar_order: 2", 'sidebar_order: "2"')
    _seed(tmp_path, monkeypatch, manifest, _VALID_NAV)

    assert mod._main([]) == 1
    err = capsys.readouterr().err
    assert "must carry an integer sidebar_order" in err
    assert "'2'" in err


def test_duplicate_sidebar_order_within_group_fails(tmp_path, monkeypatch, capsys):
    manifest = _VALID_MANIFEST.replace("sidebar_order: 2", "sidebar_order: 1")
    _seed(tmp_path, monkeypatch, manifest, _VALID_NAV)

    assert mod._main([]) == 1
    err = capsys.readouterr().err
    assert "duplicate sidebar_order 1" in err
    assert "'/settings'" in err


def test_group_missing_default_expanded_fails(tmp_path, monkeypatch, capsys):
    manifest = _VALID_MANIFEST.replace("        default_expanded: true\n", "")
    _seed(tmp_path, monkeypatch, manifest, _VALID_NAV)

    assert mod._main([]) == 1
    assert "must carry a boolean 'default_expanded'" in capsys.readouterr().err


def test_duplicate_group_id_key_fails(tmp_path, monkeypatch, capsys):
    manifest = _VALID_MANIFEST + (
        "    build:\n        label: DUPLICATE\n        order: 2\n        default_expanded: false\n"
    )
    _seed(tmp_path, monkeypatch, manifest, _VALID_NAV)

    assert mod._main([]) == 1
    err = capsys.readouterr().err
    assert "duplicate mapping key 'build'" in err
    assert "sidebar_groups" in err


def test_nav_route_missing_from_route_config_fails(tmp_path, monkeypatch, capsys):
    nav = _VALID_NAV.replace(
        "  dashboard: { icon: 'LayoutDashboard', labelKey: 'components.SidebarNav.item_dashboard' },\n",
        "",
    )
    _seed(tmp_path, monkeypatch, _VALID_MANIFEST, nav)

    assert mod._main([]) == 1
    err = capsys.readouterr().err
    assert "has no routeConfigMap entry" in err
    assert "'dashboard'" in err


def test_route_config_entry_without_manifest_route_fails(tmp_path, monkeypatch, capsys):
    nav = _VALID_NAV.replace(
        "  settings: { icon: 'Settings', labelKey: 'components.SidebarNav.item_settings' },",
        "  ghost-page: { icon: 'Settings', labelKey: 'components.SidebarNav.item_ghost' },",
    )
    _seed(tmp_path, monkeypatch, _VALID_MANIFEST, nav)

    assert mod._main([]) == 1
    err = capsys.readouterr().err
    assert "dead icon entry" in err
    assert "'ghost-page'" in err


def test_nav_route_without_name_fails(tmp_path, monkeypatch, capsys):
    manifest = _VALID_MANIFEST.replace("        name: settings\n", "")
    _seed(tmp_path, monkeypatch, manifest, _VALID_NAV)

    assert mod._main([]) == 1
    err = capsys.readouterr().err
    assert "has no route 'name'" in err
    assert "'/settings'" in err


def test_missing_manifest_fails(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(mod, "REPO_ROOT", str(tmp_path))
    _write(tmp_path / mod._NAV_PATH, _VALID_NAV)

    assert mod._main([]) == 1
    assert "not found" in capsys.readouterr().err


def test_missing_navigation_config_fails(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(mod, "REPO_ROOT", str(tmp_path))
    _write(tmp_path / mod._MANIFEST_PATH, _VALID_MANIFEST)

    assert mod._main([]) == 1
    assert "not found" in capsys.readouterr().err


def test_detail_page_duplicate_order_is_not_a_conflict(tmp_path, monkeypatch, capsys):
    """detail_page routes are skipped by buildSidebarGroups, so their
    sidebar_order never competes with a rendered nav entry's slot."""
    detail_route = (
        "    /errors/:id:\n"
        "        name: error-detail\n"
        "        sidebar_group: build\n"
        "        sidebar_order: 1\n"
        "        type: detail_page\n"
        "\n"
        "sidebar_groups:"
    )
    manifest = _VALID_MANIFEST.replace("sidebar_groups:", detail_route, 1)
    _seed(tmp_path, monkeypatch, manifest, _VALID_NAV)

    assert mod._main([]) == 0
    assert "run_validate_nav_config: OK" in capsys.readouterr().err


def test_blank_manifest_fails_closed(tmp_path, monkeypatch, capsys):
    """A whitespace-only manifest loads as None — the gate must FAIL, not fall open."""
    _seed(tmp_path, monkeypatch, "\n\n", _VALID_NAV)

    assert mod._main([]) == 1
    assert "manifest root must be a mapping" in capsys.readouterr().err


def test_non_mapping_manifest_fails_closed(tmp_path, monkeypatch, capsys):
    """A list document is not a mapping — the gate must FAIL, not fall open."""
    _seed(tmp_path, monkeypatch, "- just\n- a\n- list\n", _VALID_NAV)

    assert mod._main([]) == 1
    assert "manifest root must be a mapping" in capsys.readouterr().err


def test_invalid_yaml_reports_controlled_failure(tmp_path, monkeypatch, capsys):
    """Malformed YAML surfaces the controlled FAIL, never a raw ParserError."""
    _seed(tmp_path, monkeypatch, "routes: [unclosed\n", _VALID_NAV)

    assert mod._main([]) == 1
    assert "invalid YAML" in capsys.readouterr().err
