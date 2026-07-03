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
            {"testid": "dashboard-metrics-overview", "type": "section", "label": "Metrics Overview", "dynamic_testid": False}
        ]
    },
    "sidebar_groups": {
        "core": {"label": "Core", "order": 1, "default_expanded": True, "simple_mode": False}
    },
}


def _reset_manifest():
    from modulo.core.manifest import _MANIFEST
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
        expected_groups = {"core", "remy", "settings", "access-control", "cost-management", "system", "monitoring", "extensions"}
        assert set(groups.keys()) == expected_groups

    def test_returns_empty_dicts_when_file_missing(self):
        _reset_manifest()
        from modulo.core.manifest import load_manifest, _MANIFEST

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
        _reset_manifest()
        from modulo.core.manifest import load_manifest

        manifest = load_manifest()
        for path, route in manifest["routes"].items():
            if route["required_tier"] == "community":
                assert route["required_roles"] is None, f"Route {path} should have null roles"

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
        with open(path) as f:
            data = yaml.safe_load(f)
        assert data is not None
        assert data.get("schema_version") == 1
