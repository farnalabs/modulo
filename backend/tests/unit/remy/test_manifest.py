import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

SAMPLE_MANIFEST = {
    "schema_version": 1,
    "routes": {
        "/": {
            "name": "dashboard",
            "testid": "page-dashboard",
            "breadcrumb": "Dashboard",
            "parent": None,
            "product_map": None,
            "i18n_key": "nav.dashboard",
            "sidebar_group": "core",
            "sidebar_order": 1,
            "type": "page",
            "required_tier": "community",
            "required_roles": None,
            "required_permissions": None,
            "deprecated": False,
        }
    },
    "elements": {
        "/": [
            {
                "testid": "dashboard-metrics-overview",
                "type": "section",
                "label": "Metrics Overview",
                "dynamic_testid": False,
            }
        ]
    },
    "sidebar_groups": {"core": {"label": "Core", "order": 1, "default_expanded": True, "simple_mode": False}},
}


def _reset_manifest():
    import modulo.core.manifest as m

    m._MANIFEST = None


class TestManifestLoad:
    def test_load_yaml_successfully(self):
        _reset_manifest()
        from modulo.core.manifest import load_manifest

        manifest = load_manifest()
        assert manifest is not None
        assert "schema_version" in manifest
        assert manifest["schema_version"] == 1
        assert "routes" in manifest
        assert "sidebar_groups" in manifest
        assert "elements" in manifest

    def test_load_yaml_contains_expected_routes(self):
        _reset_manifest()
        from modulo.core.manifest import load_manifest

        manifest = load_manifest()
        routes = manifest.get("routes", {})
        assert "/" in routes
        assert "/admin/users" in routes
        assert "/admin/remy" in routes
        assert "/admin/costs" in routes
        assert "/admin/errors" in routes
        assert "/settings/teams" in routes
        assert "/settings/remy" in routes
        assert "/feedback/inbox" in routes

    def test_manifest_sidebar_groups(self):
        _reset_manifest()
        from modulo.core.manifest import load_manifest

        manifest = load_manifest()
        groups = manifest.get("sidebar_groups", {})
        expected_groups = {
            "build",
            "monitor",
            "improve",
            "configure",
            "admin",
            "system",
        }
        assert set(groups.keys()) == expected_groups

    def test_returns_empty_dicts_when_file_missing(self):
        _reset_manifest()
        from modulo.core.manifest import load_manifest

        with tempfile.TemporaryDirectory() as tmpdir:
            fake_path = Path(tmpdir) / "nonexistent.yaml"
            with patch.dict(os.environ, {"MANIFEST_PATH": str(fake_path)}):
                result = load_manifest()
                assert result == {"routes": {}, "elements": {}, "sidebar_groups": {}}

    def test_get_manifest_returns_cached(self):
        _reset_manifest()
        from modulo.core.manifest import get_manifest

        first = get_manifest()
        assert first is not None
        second = get_manifest()
        assert second is first

    def test_dynamic_route_has_pattern_and_params(self):
        _reset_manifest()
        from modulo.core.manifest import load_manifest

        manifest = load_manifest()
        run_detail = manifest["routes"].get("/runs/:id", {})
        assert run_detail.get("pattern") == "/runs/:id"
        assert "id" in run_detail.get("dynamic_params", [])

        error_detail = manifest["routes"].get("/admin/errors/:id", {})
        assert error_detail.get("pattern") == "/admin/errors/:id"
        assert "id" in error_detail.get("dynamic_params", [])

    def test_yaml_anchors_resolve(self):
        _reset_manifest()
        from modulo.core.manifest import load_manifest

        manifest = load_manifest()
        for path, route in manifest["routes"].items():
            assert "required_tier" in route, f"Route {path} missing required_tier"
            assert "required_roles" in route, f"Route {path} missing required_roles"

    def test_community_routes_have_null_roles(self):
        """Community-tier routes default to null required_roles.

        Exception (FAR-462): a community route MAY declare an EXPLICIT role
        list when the nav item must be role-gated independently of tier — the
        frontend router enforces required_roles as a separate gate from
        required_tier (see frontend/src/router/index.ts route guard and
        frontend/src/config/navigation.ts canSeeItem), so the explicit list
        preserves admin-only navigation while the tier stays community.
        Any explicit list is constrained to exactly ["admin"] by
        test_community_routes_roles_are_admin_only.
        """
        _reset_manifest()
        from modulo.core.manifest import load_manifest

        manifest = load_manifest()
        for path, route in manifest["routes"].items():
            if route["required_tier"] == "community":
                roles = route["required_roles"]
                assert roles is None or isinstance(roles, list), (
                    f"Route {path} should have null roles or an explicit role list"
                )

    def test_community_routes_roles_are_admin_only(self):
        """A community-tier route declaring explicit roles must use exactly ["admin"].

        Pins the FAR-462 escape hatch (/admin/users is community-tier but
        admin-role-gated for nav) so it cannot widen silently to other roles.
        """
        _reset_manifest()
        from modulo.core.manifest import load_manifest

        manifest = load_manifest()
        for path, route in manifest["routes"].items():
            if route["required_tier"] == "community" and route["required_roles"] is not None:
                assert route["required_roles"] == ["admin"], (
                    f"Community route {path} with explicit roles must require exactly ['admin']"
                )

    def test_admin_routes_require_admin_role(self):
        _reset_manifest()
        from modulo.core.manifest import load_manifest

        manifest = load_manifest()
        for path, route in manifest["routes"].items():
            if path.startswith("/admin/") and route["required_tier"] == "team":
                assert route["required_roles"] == ["admin"], f"Route {path} should require admin role"


class TestManifestEndpoint:
    @pytest.mark.asyncio
    async def test_manifest_endpoint_returns_valid_json(self):
        _reset_manifest()
        from modulo.api.routes.manifest import manifest_endpoint

        response = await manifest_endpoint()
        assert isinstance(response, dict)
        assert "routes" in response
        assert "elements" in response
        assert "sidebar_groups" in response

    def test_manifest_yaml_is_valid_yaml(self):
        from modulo.core.manifest import get_manifest_path

        path = get_manifest_path()
        assert path.exists(), f"Manifest file not found at {path}"
        with path.open() as f:
            data = yaml.safe_load(f)
        assert data is not None
        assert data.get("schema_version") == 1


class TestGetManifestPathFiltering:
    def _reset_manifest(self):
        import modulo.core.manifest as m

        m._MANIFEST = None

    def test_returns_full_manifest_when_no_path(self):
        self._reset_manifest()
        from modulo.core.manifest import get_manifest

        manifest = get_manifest()
        assert "routes" in manifest
        assert "elements" in manifest
        assert "sidebar_groups" in manifest
        assert manifest["routes"]

    def test_returns_route_and_elements_for_known_path(self):
        self._reset_manifest()
        from modulo.core.manifest import get_manifest

        manifest = get_manifest()
        path = "/"
        route = manifest.get("routes", {}).get(path)
        elements = manifest.get("elements", {}).get(path, [])
        assert route is not None
        assert route.get("name") == "dashboard"
        assert isinstance(elements, list)

    def test_returns_none_for_unknown_path(self):
        self._reset_manifest()
        from modulo.core.manifest import get_manifest

        manifest = get_manifest()
        path = "/nonexistent/route"
        route = manifest.get("routes", {}).get(path)
        elements = manifest.get("elements", {}).get(path, [])
        assert route is None
        assert elements == []


class TestManifestErrorHandling:
    def _reset_manifest(self):
        import modulo.core.manifest as m

        m._MANIFEST = None

    def test_raises_on_malformed_yaml(self):
        self._reset_manifest()
        from modulo.core.manifest import load_manifest

        with tempfile.TemporaryDirectory() as tmpdir:
            bad_yaml = Path(tmpdir) / "manifest.yaml"
            bad_yaml.write_text("{{{{broken: [yaml")
            with (
                patch.dict(os.environ, {"MANIFEST_PATH": str(bad_yaml)}),
                pytest.raises(RuntimeError, match="Failed to load manifest"),
            ):
                load_manifest()

    def test_returns_empty_on_missing_file(self):
        self._reset_manifest()
        from modulo.core.manifest import load_manifest

        with tempfile.TemporaryDirectory() as tmpdir:
            missing = Path(tmpdir) / "no-such-file.yaml"
            with patch.dict(os.environ, {"MANIFEST_PATH": str(missing)}):
                result = load_manifest()
                assert result == {"routes": {}, "elements": {}, "sidebar_groups": {}}


class TestDuplicateKeyDetection:
    def _reset_manifest(self):
        import modulo.core.manifest as m

        m._MANIFEST = None

    def test_duplicate_route_key_raises(self):
        self._reset_manifest()
        from modulo.core.manifest import load_manifest

        dup_yaml = """
routes:
  /:
    name: dashboard
  /:
    name: dashboard-again
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            p = Path(tmpdir) / "manifest.yaml"
            p.write_text(dup_yaml)
            with (
                patch.dict(os.environ, {"MANIFEST_PATH": str(p)}),
                pytest.raises(RuntimeError, match="duplicate mapping key"),
            ):
                load_manifest()

    def test_duplicate_sidebar_group_key_raises(self):
        self._reset_manifest()
        from modulo.core.manifest import load_manifest

        dup_yaml = """
sidebar_groups:
  core:
    label: BUILD
  core:
    label: DUPLICATE
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            p = Path(tmpdir) / "manifest.yaml"
            p.write_text(dup_yaml)
            with (
                patch.dict(os.environ, {"MANIFEST_PATH": str(p)}),
                pytest.raises(RuntimeError, match="duplicate mapping key"),
            ):
                load_manifest()

    def test_anchor_merge_override_still_works(self):
        """A route overriding an anchored field via merge keys is NOT a duplicate."""
        self._reset_manifest()
        from modulo.core.manifest import load_manifest

        yaml_text = """
x-community: &community
  required_tier: community
  required_roles: null
routes:
  /ok:
    <<: *community
    name: dashboard
    required_roles: ["admin"]
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            p = Path(tmpdir) / "manifest.yaml"
            p.write_text(yaml_text)
            with patch.dict(os.environ, {"MANIFEST_PATH": str(p)}):
                manifest = load_manifest()
        assert manifest["routes"]["/ok"]["required_roles"] == ["admin"]
        assert manifest["routes"]["/ok"]["required_tier"] == "community"
