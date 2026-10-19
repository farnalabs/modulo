"""Intended-public read surface (FAR-1975).

These are endpoints the application deliberately serves without
authentication: their handlers declare no auth dependency, so the route
definitions themselves are the record of the intended-public read surface.
The tests pin the NEGATIVE properties a future change must not quietly widen:
reachable with no credentials at all, and carrying only tenant-free public
catalogue data.

``/api/v1/deployment``, ``/api/v1/license``, ``/api/v1/auth/sso/providers``
and ``/healthz/ready`` have their own anonymous-contract tests; this module
covers the public catalogues (``/api/v1/manifest``,
``/api/v1/connectors/types``, ``/api/v1/registry/*``) plus the liveness probe.
"""

from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient

from modulo.api.main import app


@pytest.fixture(autouse=True)
def _no_dependency_overrides() -> Generator[None, None, None]:
    """Guarantee the client is genuinely anonymous.

    Another test module's overrides must not be able to make these requests
    authenticated, or the test passes for the wrong reason. Save and clear
    the app's overrides for the duration of each test, then restore them.
    """
    saved = dict(app.dependency_overrides)
    app.dependency_overrides.clear()
    yield
    app.dependency_overrides.clear()
    app.dependency_overrides.update(saved)


def _anonymous_client() -> TestClient:
    """A client with NO auth overrides — an unauthenticated caller."""
    return TestClient(app)


class TestAnonymousPublicReads:
    def test_healthz_is_anonymous(self) -> None:
        resp = _anonymous_client().get("/healthz")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}

    def test_manifest_is_anonymous(self) -> None:
        resp = _anonymous_client().get("/api/v1/manifest")
        assert resp.status_code == 200
        body = resp.json()
        for key in ("routes", "elements", "sidebar_groups"):
            assert key in body

    def test_connector_types_are_anonymous(self) -> None:
        resp = _anonymous_client().get("/api/v1/connectors/types")
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert items
        for item in items:
            assert set(item) == {"id", "display_name"}

    def test_registry_list_is_anonymous(self) -> None:
        resp = _anonymous_client().get("/api/v1/registry/primitives")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] > 0
        slugs = [item["entry"]["slug"] for item in body["items"]]
        assert "modulo/prd-input-schema" in slugs

    def test_registry_detail_is_anonymous(self) -> None:
        resp = _anonymous_client().get("/api/v1/registry/primitives/modulo/prd-input-schema")
        assert resp.status_code == 200
        assert resp.json()["entry"]["slug"] == "modulo/prd-input-schema"

    def test_registry_pull_is_anonymous(self) -> None:
        resp = _anonymous_client().get("/api/v1/registry/pull/modulo/prd-input-schema")
        assert resp.status_code == 200
        assert resp.json()["slug"] == "modulo/prd-input-schema"

    def test_registry_slug_lookup_is_a_catalogue_key_only(self) -> None:
        """A path-shaped slug is a catalogue key, never a route/file expansion."""
        resp = _anonymous_client().get("/api/v1/registry/primitives/modulo/does-not-exist")
        assert resp.status_code == 404
        resp = _anonymous_client().get("/api/v1/registry/pull/modulo/does-not-exist")
        assert resp.status_code == 404
