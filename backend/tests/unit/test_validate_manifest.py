"""Pytest port of tools/tests/test-validate-manifest.ps1 (FAR-300).

Runs scripts/run_validate_manifest.py against a temp fixture repo so the real
frontend/src/manifest.yaml is never mutated.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

SCRIPT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))),
    "scripts",
    "run_validate_manifest.py",
)


def _write(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(content)


@pytest.fixture
def repo(tmp_path):
    router_dir = os.path.join(tmp_path, "frontend", "src", "router")
    _write(
        os.path.join(router_dir, "index.ts"),
        "import { createRouter } from 'vue-router'\n"
        "const router = createRouter({\n"
        "  routes: [\n"
        "    { path: '/', name: 'dashboard', component: {} },\n"
        "    { path: '/users', name: 'admin-users', component: {} },\n"
        "    { path: '/runs/:id', name: 'run-detail', component: {} },\n"
        "  ]\n"
        "})\n"
        "export default router\n",
    )

    locales_dir = os.path.join(tmp_path, "frontend", "src", "locales")
    _write(
        os.path.join(locales_dir, "en-US.js"),
        "export default {\n"
        '  "nav": {\n'
        '    "dashboard": "Dashboard",\n'
        '    "users": "Users"\n'
        "  },\n"
        '  "common": {\n'
        '    "save": "Save",\n'
        '    "cancel": "Cancel"\n'
        "  }\n"
        "}\n",
    )

    _write(
        os.path.join(tmp_path, "docs", "product-map", "feat-users.md"),
        "---\nid: feat-users\n---\n",
    )

    views_dir = os.path.join(tmp_path, "frontend", "src", "views")
    _write(
        os.path.join(views_dir, "DashboardView.vue"),
        "<template>\n"
        "  <div>\n"
        '    <h1 data-testid="dashboard-title">Dashboard</h1>\n'
        '    <div data-testid="dashboard-metrics-overview">metrics</div>\n'
        "  </div>\n"
        "</template>\n",
    )
    _write(
        os.path.join(views_dir, "AdminUsersView.vue"),
        '<template>\n  <div>\n    <div data-testid="users-table">table</div>\n  </div>\n</template>\n',
    )
    return tmp_path


def _run(repo: str, manifest_content: str) -> tuple[int, str]:
    manifest_path = os.path.join(repo, "frontend", "src", "manifest.yaml")
    _write(manifest_path, manifest_content)
    result = subprocess.run(  # noqa: S603 - runs the trusted local validator against a fixture repo
        [sys.executable, SCRIPT, "--repo-root", repo, "--ci"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result.returncode, result.stdout


WELL_FORMED = """\
schema_version: 1
routes:
  /:
    name: dashboard
    testid: page-dashboard
    breadcrumb: Dashboard
    parent: null
    product_map: null
    i18n_key: nav.dashboard
    sidebar_group: core
    sidebar_order: 1
    type: page
  /users:
    name: admin-users
    testid: page-users
    breadcrumb: Users
    parent: null
    product_map: null
    i18n_key: nav.users
    sidebar_group: core
    sidebar_order: 2
    type: list_page
elements:
  /:
    - testid: dashboard-title
      type: heading
      label: Dashboard Title
      dynamic_testid: false
    - testid: dashboard-metrics-overview
      type: section
      label: Metrics
      dynamic_testid: false
  /users:
    - testid: users-table
      type: table
      label: Users Table
      dynamic_testid: false
"""


def test_passes_well_formed_manifest(repo):
    code, output = _run(repo, WELL_FORMED)
    assert code == 0
    assert "All rules passed" not in output  # --ci prints only issues
    assert output == ""


def test_catches_missing_route_name(repo):
    manifest = """\
schema_version: 1
routes:
  /bogus:
    name: does-not-exist
    testid: page-bogus
    breadcrumb: Bogus
    parent: null
    product_map: null
    i18n_key: nav.dashboard
    sidebar_group: core
    sidebar_order: 1
    type: page
elements: {}
"""
    code, output = _run(repo, manifest)
    assert code == 1
    assert "does-not-exist" in output


def test_catches_missing_element_testid(repo):
    manifest = """\
schema_version: 1
routes:
  /:
    name: dashboard
    testid: page-dashboard
    breadcrumb: Dashboard
    parent: null
    product_map: null
    i18n_key: nav.dashboard
    sidebar_group: core
    sidebar_order: 1
    type: page
elements:
  /:
    - testid: nonexistent-element
      type: button
      label: Ghost
      dynamic_testid: false
"""
    code, output = _run(repo, manifest)
    assert code == 1
    assert "nonexistent-element" in output


def test_catches_orphaned_elements(repo):
    manifest = """\
schema_version: 1
routes:
  /:
    name: dashboard
    testid: page-dashboard
    breadcrumb: Dashboard
    parent: null
    product_map: null
    i18n_key: nav.dashboard
    sidebar_group: core
    sidebar_order: 1
    type: page
elements:
  /orphan-route:
    - testid: dashboard-title
      type: heading
      label: Orphan
      dynamic_testid: false
"""
    code, output = _run(repo, manifest)
    assert code == 1
    assert "orphan-route" in output


def test_catches_circular_parent_chain(repo):
    manifest = """\
schema_version: 1
routes:
  /a:
    name: dashboard
    testid: page-a
    breadcrumb: A
    parent: /b
    product_map: null
    i18n_key: nav.dashboard
    sidebar_group: core
    sidebar_order: 1
    type: page
  /b:
    name: admin-users
    testid: page-b
    breadcrumb: B
    parent: /a
    product_map: null
    i18n_key: nav.users
    sidebar_group: core
    sidebar_order: 2
    type: list_page
elements: {}
"""
    code, output = _run(repo, manifest)
    assert code == 1
    assert "Circular" in output
