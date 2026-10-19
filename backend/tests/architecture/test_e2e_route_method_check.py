"""Architecture guard: the E2E route-method check must actually detect drift.

FAR-1829 rotated ``PATCH /api/v1/pipelines/{id}/graph`` to ``PUT`` and left two
callers stale in ways a path-only grep could not see: a bare method argument to
``apiFetch`` in ``frontend/tests/e2e/setup/realstack-api.ts`` and a
``route.request().method() === 'PATCH'`` interceptor filter in
``frontend/tests/e2e/pipeline-editor.spec.ts``. Both shapes run under
``playwright test --grep "@regression"`` on staging before a production deploy,
so a stale descriptor either blocks the deploy or - worse, for an interceptor -
keeps passing while asserting nothing.

These tests pin the *detection*: the checker's extraction and matching are
exercised against synthetic stale/live descriptors with an explicit live route
table. They deliberately fail if the checker stops reporting staleness - a
"clean" result from an extractor that found nothing is the failure mode this
guard exists to prevent, so each stale case also asserts the descriptor was
extracted before asserting the violation.
"""

from __future__ import annotations

import sys
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT_PATH = REPO_ROOT / "scripts" / "run_check_e2e_route_methods.py"

_loader = SourceFileLoader("run_check_e2e_route_methods", str(SCRIPT_PATH))
_checker = module_from_spec(spec_from_loader(_loader.name, _loader))
# Register before exec: ``HttpCall`` is a dataclass, and dataclasses resolve the
# defining module through ``sys.modules`` while processing the class.
sys.modules[_loader.name] = _checker
_loader.exec_module(_checker)

# The post-FAR-1829 live shapes this guard must reason about: the graph route
# serves GET + PUT (never PATCH), and the pipeline route serves GET + DELETE.
LIVE_ROUTES = _checker.build_live_routes(
    {
        "/api/v1/pipelines/{pipeline_id}/graph": {"get": {}, "put": {}},
        "/api/v1/pipelines/{pipeline_id}": {"get": {}, "delete": {}},
    }
)

FILENAME = "frontend/tests/e2e/synthetic.spec.ts"


def _calls(source: str) -> list:
    return _checker.extract_http_calls(source, FILENAME)


def _violations(source: str) -> list:
    return _checker.find_violations(_calls(source), LIVE_ROUTES)


def test_stale_api_fetch_method_is_reported() -> None:
    source = "await apiFetch(apiBase, token, 'PATCH', `/api/v1/pipelines/${pipelineId}/graph`, {})\n"

    calls = _calls(source)

    assert len(calls) == 1
    assert calls[0].kind == "apiFetch"
    assert calls[0].method == "PATCH"
    assert calls[0].path == "/api/v1/pipelines/{param}/graph"

    violations = _violations(source)

    assert len(violations) == 1
    assert "not PATCH" in violations[0].detail
    assert violations[0].call.method == "PATCH"


def test_live_api_fetch_method_is_clean() -> None:
    source = "await apiFetch(apiBase, token, 'PUT', `/api/v1/pipelines/${pipelineId}/graph`, {})\n"

    calls = _calls(source)

    assert len(calls) == 1
    assert calls[0].method == "PUT"
    assert not _violations(source)


def test_stale_interceptor_method_is_reported() -> None:
    source = (
        "await page.route(`**/api/v1/pipelines/${id}/graph`, (route) => {\n"
        "  if (route.request().method() === 'PATCH') {\n"
        "    return route.fulfill({ status: 200 })\n"
        "  }\n"
        "})\n"
    )

    calls = _calls(source)

    assert len(calls) == 1
    assert calls[0].kind == "interceptor"
    assert calls[0].path == "/api/v1/pipelines/{param}/graph"

    violations = _violations(source)

    assert len(violations) == 1
    assert violations[0].call.kind == "interceptor"
    assert "GET, PUT" in violations[0].detail


def test_live_interceptor_method_is_clean() -> None:
    source = (
        "await page.route('**/api/v1/pipelines/abc', (route) => {\n"
        "  if (route.request().method() === 'DELETE') {\n"
        "    return route.fulfill({ status: 204 })\n"
        "  }\n"
        "})\n"
    )

    calls = _calls(source)

    assert len(calls) == 1
    assert calls[0].method == "DELETE"
    assert not _violations(source)


def test_stale_direct_fetch_method_is_reported() -> None:
    source = "await fetch(apiBase + '/api/v1/pipelines/' + id + '/graph', { method: 'PATCH' })\n"

    calls = _calls(source)

    assert len(calls) == 1
    assert calls[0].kind == "fetch"
    assert calls[0].path == "/api/v1/pipelines/{param}/graph"

    violations = _violations(source)

    assert len(violations) == 1
    assert violations[0].call.kind == "fetch"


def test_multiline_generic_api_fetch_is_extracted() -> None:
    source = (
        "const res = await apiFetch<{ items: Array<{ id: string }> }>(\n"
        "  apiBase,\n"
        "  token,\n"
        "  'GET',\n"
        "  `/api/v1/pipelines/${pipelineId}/snapshots?page_size=50`,\n"
        ")\n"
    )

    calls = _calls(source)

    assert len(calls) == 1
    assert calls[0].method == "GET"
    assert calls[0].path == "/api/v1/pipelines/{param}/snapshots"


def test_query_string_interpolation_is_not_a_path_segment() -> None:
    source = "await apiFetch(apiBase, token, 'GET', `/api/v1/pipelines${query}`)\n"

    calls = _calls(source)

    assert len(calls) == 1
    assert calls[0].path == "/api/v1/pipelines"


def test_interceptor_combined_with_url_condition_is_not_guessed() -> None:
    # The effective path lives in ``url.includes(...)``, not in the broad
    # route registration, so the checker must stay silent rather than invent a
    # stale descriptor from ``/api/v1/**``.
    source = (
        "await page.route('**/api/v1/**', (route) => {\n"
        "  const url = route.request().url()\n"
        "  if (route.request().method() === 'PATCH' && url.includes('/review')) {\n"
        "    return route.fulfill({ status: 200 })\n"
        "  }\n"
        "})\n"
    )

    assert not _calls(source)
