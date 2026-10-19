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


def test_reversed_interceptor_comparison_is_detected() -> None:
    # FAR-1847: ``'PATCH' === route.request().method()`` starts at the opening
    # quote, which is blanked in the masked source - a start-offset code check
    # rejected every reversed comparison, leaving this branch dead.
    source = (
        "await page.route('**/api/v1/pipelines/abc', (route) => {\n"
        "  if ('PATCH' === route.request().method()) {\n"
        "    return route.fulfill({ status: 200 })\n"
        "  }\n"
        "})\n"
    )

    calls = _calls(source)

    assert len(calls) == 1
    assert calls[0].kind == "interceptor"
    assert calls[0].method == "PATCH"
    assert calls[0].path == "/api/v1/pipelines/abc"


def test_variable_holding_request_method_is_detected() -> None:
    # FAR-1847: the real shape is ``const m = route.request().method(); if (m ===
    # 'PATCH')`` - a literal-route inline comparison never matched it.
    source = (
        "await page.route('**/api/v1/pipelines/abc/graph', (route) => {\n"
        "  const requestMethod = route.request().method()\n"
        "  if (requestMethod === 'PATCH') {\n"
        "    return route.fulfill({ status: 200 })\n"
        "  }\n"
        "})\n"
    )

    calls = _calls(source)

    assert len(calls) == 1
    assert calls[0].kind == "interceptor"
    assert calls[0].method == "PATCH"
    assert calls[0].path == "/api/v1/pipelines/abc/graph"


def test_renamed_callback_parameter_is_detected() -> None:
    # FAR-1847: the callback parameter is not always named ``route``.
    source = (
        "await page.route('**/api/v1/pipelines/abc', (intercepted) => {\n"
        "  if (intercepted.request().method() === 'PATCH') {\n"
        "    return intercepted.fulfill({ status: 200 })\n"
        "  }\n"
        "})\n"
    )

    calls = _calls(source)

    assert len(calls) == 1
    assert calls[0].kind == "interceptor"
    assert calls[0].method == "PATCH"


def test_page_request_verb_is_detected() -> None:
    # FAR-1847: ``page.request.<verb>(url)`` is an APIRequestContext call used
    # by @regression specs; the verb itself is the method.
    source = "await page.request.delete(`${base}/api/v1/admin/sso/providers/${providerId}`, { headers })\n"

    calls = _calls(source)

    assert len(calls) == 1
    assert calls[0].kind == "request"
    assert calls[0].method == "DELETE"
    assert calls[0].path == "/api/v1/admin/sso/providers/{param}"


def test_bare_request_verb_is_detected() -> None:
    # FAR-1847: ``request.<verb>(url)`` on a bare APIRequestContext is the same
    # shape as ``page.request`` and must be scanned too.
    source = "const resp = await request.post(`${base}/api/v1/admin/users/invite`, { data })\n"

    calls = _calls(source)

    assert len(calls) == 1
    assert calls[0].kind == "request"
    assert calls[0].method == "POST"
    assert calls[0].path == "/api/v1/admin/users/invite"


def test_leading_interpolation_before_api_prefix_is_resolved() -> None:
    # FAR-1847: ``fetch(`${apiBase}/api/v1/pipelines`, { method: 'POST' })``
    # normalised to ``{param}/api/v1/pipelines`` and was rejected for not
    # starting with ``/`` - a real POST was invisible.
    source = "await fetch(`${apiBase}/api/v1/pipelines`, { method: 'POST' })\n"

    calls = _calls(source)

    assert len(calls) == 1
    assert calls[0].kind == "fetch"
    assert calls[0].method == "POST"
    assert calls[0].path == "/api/v1/pipelines"


def test_concatenation_ending_in_a_variable_keeps_a_wildcard() -> None:
    # FAR-1847: ``'/api/v1/pipelines/' + id`` truncated to the collection route,
    # which both false-positived DELETE /api/v1/pipelines and hid the item
    # route behind it.
    source = "await apiFetch(apiBase, token, 'DELETE', '/api/v1/pipelines/' + pipelineId)\n"

    calls = _calls(source)

    assert len(calls) == 1
    assert calls[0].path == "/api/v1/pipelines/{param}"


def test_legitimate_patch_route_resolves_clean() -> None:
    # FAR-1847: legitimate PATCH routes exist (``/api/v1/pipelines/{id}``); the
    # guard must NOT be converted into a blanket "no PATCH anywhere" grep.
    live = _checker.build_live_routes({"/api/v1/pipelines/{pipeline_id}": {"get": {}, "patch": {}}})
    source = "await apiFetch(apiBase, token, 'PATCH', '/api/v1/pipelines/abc')\n"

    calls = _calls(source)

    assert len(calls) == 1
    assert calls[0].method == "PATCH"
    assert not _checker.find_violations(calls, live)


def test_main_reports_setup_error_on_empty_descriptor_set(monkeypatch) -> None:
    # FAR-1847: an extractor that collapses to zero descriptors must not exit 0
    # - a "0 descriptor(s) OK" line is a guard that has stopped guarding.
    monkeypatch.setattr(_checker, "load_live_routes", lambda: LIVE_ROUTES)
    monkeypatch.setattr(_checker, "collect_calls", lambda notes=None: [])

    assert _checker.main() == 2


def test_main_reports_setup_error_when_a_required_file_yields_nothing(monkeypatch) -> None:
    # FAR-1847: the two ticket-named files are the guard's reason to exist; if
    # either yields no descriptor, the scan regressed and must not report clean.
    working = _checker.extract_http_calls(
        "await apiFetch(apiBase, token, 'GET', '/api/v1/pipelines')\n",
        "frontend/tests/e2e/setup/realstack-api.ts",
    )
    monkeypatch.setattr(_checker, "load_live_routes", lambda: LIVE_ROUTES)
    monkeypatch.setattr(_checker, "collect_calls", lambda notes=None: working)

    assert _checker.main() == 2


def test_second_callback_parameter_method_filter_is_detected() -> None:
    # FAR-1847 round 2: ``page.route(url, async (route, request) => ...)`` is a
    # real shape (frontend/tests/e2e/view-modes-admin.spec.ts). The Request is
    # the SECOND parameter, so filtering only on the first silently missed it.
    source = (
        "await page.route('**/api/v1/views**', async (route, request) => {\n"
        "  if (request.method() === 'GET') {\n"
        "    await route.fulfill({ status: 200 })\n"
        "  } else if (request.method() === 'POST') {\n"
        "    await route.fulfill({ status: 201 })\n"
        "  }\n"
        "})\n"
    )

    calls = _calls(source)

    assert [(call.method, call.path) for call in calls] == [
        ("GET", "/api/v1/views"),
        ("POST", "/api/v1/views"),
    ]
    assert all(call.kind == "interceptor" for call in calls)


def test_hoisted_request_object_method_filter_is_detected() -> None:
    # FAR-1847 round 2: ``const request = route.request(); if
    # (request.method() === 'POST')`` is the shape in
    # frontend/tests/e2e/settings-mcp-oauth.spec.ts:93,106.
    source = (
        "await page.route('**/api/v1/mcp/oauth/clients**', async (route) => {\n"
        "  const request = route.request()\n"
        "  if (request.method() === 'POST') {\n"
        "    return route.fulfill({ status: 201 })\n"
        "  }\n"
        "  if (request.method() === 'DELETE') {\n"
        "    return route.fulfill({ status: 200 })\n"
        "  }\n"
        "})\n"
    )

    calls = _calls(source)

    assert [(call.method, call.path) for call in calls] == [
        ("POST", "/api/v1/mcp/oauth/clients"),
        ("DELETE", "/api/v1/mcp/oauth/clients"),
    ]


def test_reversed_second_callback_parameter_comparison_is_detected() -> None:
    # The reversed form of the second-parameter filter must resolve too.
    source = (
        "await page.route('**/api/v1/views**', async (route, request) => {\n"
        "  if ('GET' === request.method()) {\n"
        "    return route.fulfill({ status: 200 })\n"
        "  }\n"
        "})\n"
    )

    calls = _calls(source)

    assert len(calls) == 1
    assert calls[0].method == "GET"
    assert calls[0].path == "/api/v1/views"


def test_anchored_regex_route_is_normalised_and_checked() -> None:
    # FAR-1847 round 2: ``$`` was rejected by normalise_path, so an anchored
    # regex registration was silently skipped - a stale PATCH would pass.
    source = (
        "await page.route(/\\/api\\/v1\\/pipelines\\/abc$/, (route) => {\n"
        "  if (route.request().method() === 'PATCH') {\n"
        "    return route.fulfill({ status: 200 })\n"
        "  }\n"
        "})\n"
    )

    calls = _calls(source)

    assert len(calls) == 1
    assert calls[0].path == "/api/v1/pipelines/abc"

    violations = _violations(source)

    assert len(violations) == 1
    assert violations[0].call.method == "PATCH"


def test_character_class_regex_route_is_normalised() -> None:
    # The anchored + character-class form collapses to the same ``{param}``
    # shape a string registration produces, and is checked like one.
    source = (
        "await page.route(/^\\/api\\/v1\\/pipelines\\/([^/]+)\\/graph$/, (route) => {\n"
        "  if (route.request().method() === 'PATCH') {\n"
        "    return route.fulfill({ status: 200 })\n"
        "  }\n"
        "})\n"
    )

    calls = _calls(source)

    assert len(calls) == 1
    assert calls[0].path == "/api/v1/pipelines/{param}/graph"

    violations = _violations(source)

    assert len(violations) == 1
    assert "GET, PUT" in violations[0].detail


def test_anchored_regex_plan_path_normalises() -> None:
    # The REAL registration in feedback-inbox.spec.ts:23. It carries no method
    # filter, so it yields no descriptor - but its path must resolve, proving it
    # is no longer dropped before the filter is even considered.
    path_info = _checker.extract_path("/\\/api\\/v1\\/plan$/")

    assert path_info is not None
    assert path_info[0] == "/api/v1/plan"


def test_real_feedback_inbox_regex_registration_still_contributes() -> None:
    # feedback-inbox.spec.ts registers ``/\/api\/v1\/feedback\/inbox/`` and has
    # a bare ``if (method === 'GET')`` branch; normalising the anchored plan
    # regex must not disturb that descriptor.
    calls = [
        call
        for call in _checker.collect_calls()
        if call.filename.endswith("feedback-inbox.spec.ts")
        and call.method == "GET"
        and call.path == "/api/v1/feedback/inbox"
    ]

    assert len(calls) == 1


def test_api_shaped_route_that_cannot_be_normalised_is_noted() -> None:
    # FAR-1847 round 2: a route registration the checker cannot normalise must
    # leave an explicit note, never disappear silently.
    notes: list[str] = []

    calls = _checker.extract_http_calls(
        "await page.route('/api/v1/bad path', (route) => {\n"
        "  if (route.request().method() === 'GET') { return route.fulfill({ status: 200 }) }\n"
        "})\n",
        FILENAME,
        notes,
    )

    assert not calls
    assert len(notes) == 1
    assert "could not be normalised statically" in notes[0]


def test_concatenated_query_suffix_does_not_add_a_path_segment() -> None:
    # FAR-1847 round 2: ``'/api/v1/runners/status' + query`` became
    # ``.../status/{param}`` - a hard false positive.
    live = _checker.build_live_routes({"/api/v1/runners/status": {"get": {}}})
    source = "await apiFetch(apiBase, token, 'GET', '/api/v1/runners/status' + query)\n"

    calls = _calls(source)

    assert len(calls) == 1
    assert calls[0].path == "/api/v1/runners/status"
    assert not _checker.find_violations(calls, live)


def test_concatenated_ts_assertion_does_not_add_a_path_segment() -> None:
    # ``'/api/v1/auth/me' as const`` is the same literal - no trailing variable.
    live = _checker.build_live_routes({"/api/v1/auth/me": {"get": {}}})
    source = "await apiFetch(apiBase, token, 'GET', '/api/v1/auth/me' as const)\n"

    calls = _calls(source)

    assert len(calls) == 1
    assert calls[0].path == "/api/v1/auth/me"
    assert not _checker.find_violations(calls, live)


def test_conventional_query_interpolation_names_are_not_path_segments() -> None:
    # FAR-1847 round 2: ``${queryParams}`` (and the conventional siblings) were
    # counted as a path segment because the allowlist only knew ``query``.
    calls = []
    for expression in ("queryParams", "params", "searchQuery", "queryString", "searchParams"):
        source = f"await apiFetch(apiBase, token, 'GET', `/api/v1/pipelines${{{expression}}}`)\n"
        extracted = _calls(source)
        assert len(extracted) == 1
        calls.append(extracted[0].path)

    assert calls == ["/api/v1/pipelines"] * 5


def test_query_interpolation_predicate_is_shared_with_concatenation() -> None:
    # One predicate must govern both paths, so the concatenation form of the
    # same conventional name resolves identically.
    source = "await apiFetch(apiBase, token, 'GET', '/api/v1/pipelines' + queryParams)\n"

    calls = _calls(source)

    assert len(calls) == 1
    assert calls[0].path == "/api/v1/pipelines"


def test_interceptor_combined_with_url_condition_on_a_specific_route_is_not_guessed() -> None:
    # FAR-1847 round 2: the previous test used the catch-all ``**/api/v1/**``,
    # which is skipped BEFORE method filters are read - it stayed green with
    # ``_URL_CONDITION_RE`` deleted. This non-catch-all registration would be
    # reported as a stale PATCH without the URL-condition guard.
    source = (
        "await page.route('**/api/v1/pipelines', (route) => {\n"
        "  const url = route.request().url()\n"
        "  if (route.request().method() === 'PATCH' && url.includes('/review')) {\n"
        "    return route.fulfill({ status: 200 })\n"
        "  }\n"
        "})\n"
    )

    assert not _calls(source)


def test_real_tree_scan_yields_plausible_descriptor_floor() -> None:
    # FAR-1847: the check must be pinned against the REAL tree, not only
    # synthetic sources. The floor is a MINIMUM, not an exact figure - it
    # guards against the extractor silently collapsing, so raise it when shapes
    # are added; 119 were observed at green on this branch (113 before the
    # round-2 shapes: the second callback parameter, the hoisted request
    # object, and the anchored/character-class regex registrations).
    calls = _checker.collect_calls()

    assert len(calls) >= 119


def test_ticket_named_files_contribute_descriptors() -> None:
    # FAR-1847: both files the guard was written for must keep contributing.
    calls = _checker.collect_calls()
    contributors = {call.filename for call in calls}

    assert "frontend/tests/e2e/setup/realstack-api.ts" in contributors
    assert "frontend/tests/e2e/pipeline-editor.spec.ts" in contributors


def test_real_tenancy_rbac_leading_interpolation_is_resolved() -> None:
    # FAR-1847: frontend/tests/e2e/journeys/tenancy-rbac.spec.ts:168 posts to
    # ``${apiBase}/api/v1/pipelines``. That line is what pins the
    # leading-interpolation shape against the real tree (the other
    # POST /api/v1/pipelines in the file is a plain literal apiFetch).
    calls = [
        call
        for call in _checker.collect_calls()
        if call.filename.endswith("journeys/tenancy-rbac.spec.ts")
        and call.line == 168
        and call.method == "POST"
        and call.path == "/api/v1/pipelines"
    ]

    assert len(calls) == 1
