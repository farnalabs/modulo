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


def test_interceptor_url_condition_leaves_an_explicit_note() -> None:
    # The effective path lives in ``url.includes(...)``, not in the broad route
    # registration, so the checker must not invent a descriptor from the
    # registration - and must SAY so. This discriminates on the note: without
    # ``_URL_CONDITION_RE`` a stale PATCH descriptor is emitted here. (The
    # catch-all form is covered separately; a catch-all is skipped before any
    # method filter is read, so it cannot pin this guard.)
    notes: list[str] = []

    calls = _checker.extract_http_calls(
        "await page.route('**/api/v1/pipelines', (route) => {\n"
        "  const url = route.request().url()\n"
        "  if (route.request().method() === 'PATCH' && url.includes('/review')) {\n"
        "    return route.fulfill({ status: 200 })\n"
        "  }\n"
        "})\n",
        FILENAME,
        notes,
    )

    assert not calls
    assert any("combined with an in-callback URL test" in note for note in notes)


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


def test_main_reports_setup_error_on_empty_descriptor_set(monkeypatch, capsys) -> None:
    # FAR-1847: an extractor that collapses to zero descriptors must not exit 0
    # - a "0 descriptor(s) OK" line is a guard that has stopped guarding.
    #
    # The stub mirrors ``main()``'s real call shape: ``collect_calls(notes,
    # unanalysed)``. An earlier one-argument stub raised ``TypeError``, which
    # ``main()`` converted to exit 2 for the WRONG reason - deleting the empty-set
    # guard left this test green.
    monkeypatch.setattr(_checker, "load_live_routes", lambda: LIVE_ROUTES)
    monkeypatch.setattr(_checker, "collect_calls", lambda notes=None, unanalysed=None: [])

    assert _checker.main() == 2
    assert "found NO HTTP descriptors" in capsys.readouterr().err


def test_main_reports_setup_error_when_a_required_file_yields_nothing(monkeypatch, capsys) -> None:
    # FAR-1847: the two ticket-named files are the guard's reason to exist; if
    # either yields no descriptor, the scan regressed and must not report clean.
    working = _checker.extract_http_calls(
        "await apiFetch(apiBase, token, 'GET', '/api/v1/pipelines')\n",
        "frontend/tests/e2e/setup/realstack-api.ts",
    )
    monkeypatch.setattr(_checker, "load_live_routes", lambda: LIVE_ROUTES)
    monkeypatch.setattr(_checker, "collect_calls", lambda notes=None, unanalysed=None: working)

    assert _checker.main() == 2
    assert "found NO descriptor in frontend/tests/e2e/pipeline-editor.spec.ts" in capsys.readouterr().err


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

    # Presence and shape, not an exact count: a legitimate second identical
    # call in the spec must not turn a behavioural pin into a brittle one.
    assert calls
    assert {call.kind for call in calls} == {"interceptor"}


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
    # Two distinct facts: the registration path is unreadable (line 1) and the
    # method reference inside its callback is unattributable (line 2). The
    # ledger deliberately does not let the first note hide the second.
    assert len(notes) == 2
    assert any("could not be normalised statically" in note for note in notes)
    assert any(note.startswith(f"{FILENAME}:2:") for note in notes)


def test_concatenated_query_suffix_does_not_add_a_path_segment() -> None:
    # FAR-1847 round 2: ``'/api/v1/runners/status' + query`` became
    # ``.../status/{param}`` - a hard false positive.
    live = _checker.build_live_routes({"/api/v1/runners/status": {"get": {}}})
    source = "await apiFetch(apiBase, token, 'GET', '/api/v1/runners/status' + query)\n"

    calls = _calls(source)

    assert len(calls) == 1
    assert calls[0].path == "/api/v1/runners/status"
    assert not _checker.find_violations(calls, live)


def test_wrapper_call_argument_does_not_fabricate_a_path_segment() -> None:
    # ``buildUrl('/api/v1/runners/status')`` passes the literal as a WHOLE
    # argument; the fragment fallback read the closing ``)`` as a path variable
    # and invented ``.../status/{param}`` - a hard false violation.
    live = _checker.build_live_routes({"/api/v1/runners/status": {"get": {}}})
    source = "await apiFetch(apiBase, token, 'GET', buildUrl('/api/v1/runners/status'))\n"

    calls = _calls(source)

    assert len(calls) == 1
    assert calls[0].path == "/api/v1/runners/status"
    assert not _checker.find_violations(calls, live)


def test_whole_path_ternary_is_noted_not_spliced() -> None:
    # Both ternary branches are complete routes. Splicing them produced
    # ``/api/v1/plan{param}/api/v1/runs``; alternatives must be a note.
    notes: list[str] = []

    calls = _checker.extract_http_calls(
        "await apiFetch(b, t, 'GET', cond ? '/api/v1/plan' : '/api/v1/runs')\n", FILENAME, notes
    )

    assert not calls
    assert len(notes) == 1
    assert "non-literal path" in notes[0]
    assert all("/api/v1/plan{param}/api/v1/runs" not in note for note in notes)


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
    # synthetic sources. This is a COLLAPSE DETECTOR, not an exact count: 122
    # were observed at green on this branch (119 before the implicit-GET fetch
    # shape). The floor leaves headroom so a legitimate added/removed call does
    # not turn a behavioural pin into a brittle one - a regression that loses a
    # whole class of shapes still trips it.
    calls = _checker.collect_calls()

    assert len(calls) >= 100


def test_ticket_named_files_contribute_descriptors() -> None:
    # FAR-1847: both files the guard was written for must keep contributing.
    calls = _checker.collect_calls()
    contributors = {call.filename for call in calls}

    assert "frontend/tests/e2e/setup/realstack-api.ts" in contributors
    assert "frontend/tests/e2e/pipeline-editor.spec.ts" in contributors


def test_real_tenancy_rbac_leading_interpolation_is_resolved() -> None:
    # FAR-1847: frontend/tests/e2e/journeys/tenancy-rbac.spec.ts has a direct
    # ``fetch(`${apiBase}/api/v1/pipelines`, ...)``. That call is what pins the
    # leading-interpolation shape against the real tree. Assert on the shape
    # (kind/method/path), never on a line number - the spec moves and a line pin
    # turns a behavioural test into a brittle one.
    calls = [
        call
        for call in _checker.collect_calls()
        if call.filename.endswith("journeys/tenancy-rbac.spec.ts")
        and call.kind == "fetch"
        and call.method == "POST"
        and call.path == "/api/v1/pipelines"
    ]

    # Presence and shape, not an exact count: a legitimate second identical
    # call in the spec must not turn a behavioural pin into a brittle one.
    assert calls
    assert {(call.kind, call.method, call.path) for call in calls} == {("fetch", "POST", "/api/v1/pipelines")}


# ---------------------------------------------------------------------------
# FAR-1847 final pass: silent drops and unresolvable shapes
# ---------------------------------------------------------------------------


def test_implicit_get_fetch_is_extracted_and_checked() -> None:
    # ``fetch(url)`` / ``fetch(url, { headers })`` with no method option is a
    # real GET - skipping it hid the request from the check entirely.
    for source in (
        "await fetch('/api/v1/pipelines')\n",
        "await fetch('/api/v1/pipelines', { headers })\n",
    ):
        calls = _calls(source)
        assert len(calls) == 1
        assert calls[0].kind == "fetch"
        assert calls[0].method == "GET"
        assert calls[0].path == "/api/v1/pipelines"


def test_implicit_get_fetch_stale_method_is_reported() -> None:
    # A route that only serves PUT makes an implicit GET stale - proving the
    # implicit method is checked, not merely extracted.
    live = _checker.build_live_routes({"/api/v1/pipelines/abc/graph": {"put": {}}})
    source = "await fetch('/api/v1/pipelines/abc/graph')\n"

    violations = _checker.find_violations(_calls(source), live)

    assert len(violations) == 1
    assert violations[0].call.method == "GET"


def test_ts_asserted_method_argument_is_resolved() -> None:
    # ``'PATCH' as const`` / ``as HttpMethod`` still names one literal method.
    for expression in ("'PATCH' as const", "'PATCH' as HttpMethod"):
        source = f"await apiFetch(apiBase, token, {expression}, '/api/v1/pipelines/abc/graph')\n"

        calls = _calls(source)

        assert len(calls) == 1
        assert calls[0].method == "PATCH"
        assert calls[0].path == "/api/v1/pipelines/abc/graph"


def test_boolean_holding_method_comparison_is_resolved() -> None:
    # ``const isPatch = route.request().method() === 'PATCH'; if (isPatch)`` -
    # the comparison is not itself inside the ``if``, so it was dropped.
    source = (
        "await page.route('**/api/v1/pipelines/abc/graph', (route) => {\n"
        "  const isPatch = route.request().method() === 'PATCH'\n"
        "  if (isPatch) {\n"
        "    return route.fulfill({ status: 200 })\n"
        "  }\n"
        "})\n"
    )

    calls = _calls(source)

    assert len(calls) == 1
    assert calls[0].kind == "interceptor"
    assert calls[0].method == "PATCH"
    assert calls[0].path == "/api/v1/pipelines/abc/graph"


def test_negated_boolean_method_comparison_guards_the_fallback_branch() -> None:
    # ``if (!isPatch)`` selects the fallback branch, like ``!==`` - it must not
    # invent a method descriptor.
    source = (
        "await page.route('**/api/v1/pipelines/abc/graph', (route) => {\n"
        "  const isPut = route.request().method() === 'PUT'\n"
        "  if (!isPut) {\n"
        "    return route.fallback()\n"
        "  }\n"
        "})\n"
    )

    assert not _calls(source)


def test_unresolvable_api_fetch_method_is_noted() -> None:
    # A method expression the extractor cannot read must leave an explicit note
    # instead of disappearing.
    notes: list[str] = []

    calls = _checker.extract_http_calls(
        "await apiFetch(apiBase, token, pickMethod(), '/api/v1/pipelines')\n",
        FILENAME,
        notes,
    )

    assert not calls
    assert len(notes) == 1
    assert "method argument" in notes[0]
    assert FILENAME in notes[0]


def test_unreadable_fetch_init_is_noted() -> None:
    # An init object (or spread) that may carry a method is not an implicit GET.
    notes: list[str] = []

    calls = _checker.extract_http_calls("await fetch('/api/v1/plan', init)\n", FILENAME, notes)

    assert not calls
    assert len(notes) == 1
    assert "method" in notes[0]


def test_catch_all_with_a_method_filter_is_noted() -> None:
    # A catch-all that mocks specific routes by URL and filters on the method
    # can hide a stale one, so it must not be silent.
    notes: list[str] = []

    calls = _checker.extract_http_calls(
        "await page.route('**/api/v1/**', (route) => {\n"
        "  if (route.request().method() === 'PATCH') { return route.fulfill({ status: 200 }) }\n"
        "})\n",
        FILENAME,
        notes,
    )

    assert not calls
    # The catch-all registration note and the per-line method reference note
    # are different facts; neither may mask the other.
    assert len(notes) == 2
    assert any("catch-all" in note for note in notes)
    assert any(note.startswith(f"{FILENAME}:2:") for note in notes)


def test_catch_all_without_a_method_filter_stays_silent() -> None:
    # A catch-all mock with no method filter asserts nothing about a method, so
    # it carries no route to validate and no note.
    notes: list[str] = []

    calls = _checker.extract_http_calls(
        "await page.route('**/api/v1/**', (route) => route.fulfill({ status: 200 }))\n",
        FILENAME,
        notes,
    )

    assert not calls
    assert not notes


def test_delegated_interceptor_filter_is_noted() -> None:
    # ``if (isMethod(route, 'PATCH'))`` reads no ``.method()`` in the callback:
    # the filter lives in the helper. That registration used to be completely
    # silent - no descriptor, no note, no unanalysed.
    notes: list[str] = []
    source = (
        "await page.route('**/api/v1/x', (route) => {\n"
        "  if (isMethod(route, 'PATCH')) return route.fulfill({ status: 200 })\n"
        "})\n"
    )

    calls = _checker.extract_http_calls(source, FILENAME, notes)

    assert not calls
    assert len(notes) == 1
    assert notes[0].startswith(f"{FILENAME}:1:")
    assert "delegates its method filter to a helper function" in notes[0]


def test_locally_defined_delegated_helper_is_noted() -> None:
    # The helper's own method filter is a separate unmodelled reference (noted
    # on its line); the registration must ALSO be named, not left silent.
    notes: list[str] = []
    source = (
        "function isMethod(route: Route) { return route.request().method() === 'PATCH' }\n"
        "await page.route('**/api/v1/x', (route) => {\n"
        "  if (isMethod(route, 'PATCH')) return route.fulfill({ status: 200 })\n"
        "})\n"
    )

    calls = _checker.extract_http_calls(source, FILENAME, notes)

    assert not calls
    assert any(note.startswith(f"{FILENAME}:2:") and "delegates its method filter" in note for note in notes)


def test_constructor_call_is_not_a_delegated_method_filter() -> None:
    # ``new URL(route.request().url())`` receives the route but is not a helper
    # method filter - noting it would flood the real tree with noise.
    notes: list[str] = []

    calls = _checker.extract_http_calls(
        "await page.route('**/api/v1/notifications/in-app*', (route) => {\n"
        "  const url = new URL(route.request().url())\n"
        "  return route.fulfill({ status: 200, json: { items: [] } })\n"
        "})\n",
        FILENAME,
        notes,
    )

    assert not calls
    assert not notes


# ---------------------------------------------------------------------------
# FAR-1847 closing round: non-literal paths and un-modelled filter shapes
# ---------------------------------------------------------------------------


def test_non_literal_api_fetch_path_is_noted() -> None:
    # A literal method with a variable path names a real request. Dropping it
    # with no note is the silent failure this guard exists to prevent.
    notes: list[str] = []

    calls = _checker.extract_http_calls("await apiFetch(b, t, 'DELETE', path)\n", FILENAME, notes)

    assert not calls
    assert len(notes) == 1
    assert "non-literal path" in notes[0]
    assert FILENAME in notes[0]


def test_non_literal_request_verb_path_is_noted() -> None:
    # ``request.delete(path)`` has a resolvable method (the verb) and an
    # unreadable path - it must leave a note, not disappear.
    notes: list[str] = []

    calls = _checker.extract_http_calls("await request.delete(path)\n", FILENAME, notes)

    assert not calls
    assert len(notes) == 1
    assert "non-literal URL" in notes[0]


def test_non_literal_fetch_url_with_explicit_method_is_noted() -> None:
    notes: list[str] = []

    calls = _checker.extract_http_calls("await fetch(endpoint, { method: 'POST' })\n", FILENAME, notes)

    assert not calls
    assert len(notes) == 1
    assert "non-literal URL" in notes[0]


def test_destructured_method_filter_is_noted() -> None:
    # ``const { method } = route.request()`` selects a branch the comparison
    # passes cannot resolve; the registration must not be silent.
    notes: list[str] = []
    source = (
        "await page.route('**/api/v1/pipelines/abc', (route) => {\n"
        "  const { method } = route.request()\n"
        "  if (method === 'PATCH') {\n"
        "    return route.fulfill({ status: 200 })\n"
        "  }\n"
        "})\n"
    )

    calls = _checker.extract_http_calls(source, FILENAME, notes)

    assert not calls
    # The destructured reference (registration, line 1) and its comparison
    # (line 3) are both unattributable, so both are reported.
    assert len(notes) == 2
    assert all("cannot attribute to a single literal method" in note for note in notes)
    assert any(note.startswith(f"{FILENAME}:3:") for note in notes)


def test_includes_method_filter_is_noted() -> None:
    # An array-membership test is a valid filter the extractor does not model.
    notes: list[str] = []
    source = (
        "await page.route('**/api/v1/pipelines/abc', (route) => {\n"
        "  if (['PATCH', 'PUT'].includes(route.request().method())) {\n"
        "    return route.fulfill({ status: 200 })\n"
        "  }\n"
        "})\n"
    )

    calls = _checker.extract_http_calls(source, FILENAME, notes)

    assert not calls
    assert len(notes) == 1
    assert "cannot attribute to a single literal method" in notes[0]


def test_fetch_method_shorthand_is_not_an_implicit_get() -> None:
    # ``{ method }`` carries the method but no literal here, so the request is
    # unresolvable - assuming GET can fail a valid PR.
    notes: list[str] = []
    source = "const method = 'POST'\nawait fetch('/api/v1/pipelines', { method })\n"

    calls = _checker.extract_http_calls(source, FILENAME, notes)

    assert not calls
    assert len(notes) == 1
    assert "method" in notes[0]


def test_fetch_quoted_method_key_is_not_an_implicit_get() -> None:
    notes: list[str] = []

    calls = _checker.extract_http_calls(
        "await fetch('/api/v1/pipelines', { \"method\": selected })\n",
        FILENAME,
        notes,
    )

    assert not calls
    assert len(notes) == 1
    assert "method" in notes[0]


def test_live_route_table_is_built_from_route_objects_not_openapi() -> None:
    # ``app.openapi()['paths']`` omits ``include_in_schema=False`` routes and
    # flattens ``{param:path}`` to one segment. The real table must come from
    # the route objects so both shapes resolve.
    class Leaf:
        def __init__(self, path: str, methods: set[str], include_in_schema: bool) -> None:
            self.path = path
            self.methods = methods
            self.include_in_schema = include_in_schema

    class Included:
        def __init__(self, routes: list[Leaf]) -> None:
            self.original_router = type("Router", (), {"routes": routes})()

    routes = _checker.build_live_routes_from_routes(
        [
            Included(
                [
                    Leaf("/api/v1/registry/primitives/{slug:path}", {"GET"}, True),
                    Leaf("/api/v1/runs/{run_id}/workspace-lease", {"GET"}, False),
                ]
            )
        ]
    )

    calls = [
        _checker.HttpCall(FILENAME, 1, "GET", "/api/v1/registry/primitives/acme/widgets", "apiFetch"),
        _checker.HttpCall(FILENAME, 2, "GET", "/api/v1/runs/abc/workspace-lease", "apiFetch"),
    ]

    assert sorted(route.template for route in routes) == [
        "/api/v1/registry/primitives/{param:path}",
        "/api/v1/runs/{run_id}/workspace-lease",
    ]
    assert not _checker.find_violations(calls, routes)


def test_path_converter_matches_a_multi_segment_request_with_a_segment_prefix() -> None:
    # A ``{slug:path}`` converter mid-path, reached by a request whose final
    # segment is a Playwright GLOB PREFIX (``down*`` matching ``download``).
    #
    # This replaces a test that called itself "broad glob prefix" but never
    # exercised the converter: a trailing-glob call short-circuits at the
    # ``trailing_glob`` early return, before the ``{param:path}`` branch runs, so
    # it passed with converter support removed. Here the converter must consume
    # ``acme/widgets`` (TWO segments) for ``down`` to align with ``download``; a
    # single-segment ``{param}`` cannot, so the test fails if converter support
    # is dropped.
    routes = _checker.build_live_routes({"/api/v1/registry/{slug:path}/download": {"get": {}}})
    matching = _checker.HttpCall(
        FILENAME,
        1,
        "GET",
        "/api/v1/registry/acme/widgets/down",
        "interceptor",
        last_segment_prefix=True,
    )
    wrong_trailing_literal = _checker.HttpCall(
        FILENAME,
        2,
        "GET",
        "/api/v1/registry/acme/widgets/upload",
        "interceptor",
        last_segment_prefix=True,
    )

    assert _checker.path_matches(matching, routes[0])
    assert not _checker.path_matches(wrong_trailing_literal, routes[0])


def test_real_route_table_resolves_path_converter_and_non_schema_routes(monkeypatch) -> None:
    # The two shapes the OpenAPI table rejected: a multi-segment registry slug
    # and an ``include_in_schema=False`` read model. Both are live requests.
    for key, value in _checker._TEMPLATE_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite://")

    routes = _checker.load_live_routes()
    calls = [
        _checker.HttpCall(FILENAME, 1, "GET", "/api/v1/registry/primitives/acme/widgets", "apiFetch"),
        _checker.HttpCall(FILENAME, 2, "GET", "/api/v1/runs/abc/workspace-lease", "apiFetch"),
    ]

    assert not _checker.find_violations(calls, routes)


def test_generic_type_with_a_function_type_does_not_drop_the_call() -> None:
    # ``=>`` inside a generic argument list: its ``>`` is not the generic
    # close, so counting it ended the generic scan early and dropped the call.
    source = "await apiFetch<{ onSave: () => void }>(b, t, 'PATCH', '/api/v1/pipelines/abc/graph')\n"

    calls = _calls(source)

    assert len(calls) == 1
    assert calls[0].kind == "apiFetch"
    assert calls[0].method == "PATCH"
    assert calls[0].path == "/api/v1/pipelines/abc/graph"


def test_api_fetch_with_variable_method_and_path_is_noted() -> None:
    # ``apiFetch`` is inherently an API call: a hoisted method AND path must
    # leave one explicit note, never disappear because no argument spells
    # ``/api/``.
    notes: list[str] = []
    source = (
        "const endpoint = '/api/v1/pipelines/x/graph'\nconst verb = 'PATCH'\nawait apiFetch(b, t, verb, endpoint)\n"
    )

    calls = _checker.extract_http_calls(source, FILENAME, notes)

    assert not calls
    assert len(notes) == 1
    assert "could not be fully resolved" in notes[0]
    assert "method argument" in notes[0]


def test_api_fetch_declaration_is_not_treated_as_a_call() -> None:
    # The helper's own signature has typed parameters, not a method/path pair.
    # Noting it as an unresolved request would report the definition itself on
    # every real run (realstack-api.ts:166).
    notes: list[str] = []
    source = (
        "export async function apiFetch<T>(\n"
        "  apiBase: string,\n"
        "  token: string,\n"
        "  method: 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE',\n"
        "  path: string,\n"
        "  body?: unknown,\n"
        "): Promise<T> {\n"
        "  return request(apiBase, token, method, path, body)\n"
        "}\n"
    )

    calls = _checker.extract_http_calls(source, FILENAME, notes)

    assert not calls
    assert not notes


def test_hoisted_route_pattern_with_method_filter_is_noted() -> None:
    # A registration whose URL is a hoisted variable cannot be matched to one
    # route, but its callback filters on the method - so it must be noted.
    notes: list[str] = []
    source = (
        "const pattern = '**/api/v1/pipelines/abc/graph'\n"
        "await page.route(pattern, (route) => {\n"
        "  if (route.request().method() === 'PATCH') {\n"
        "    return route.fulfill({ status: 200 })\n"
        "  }\n"
        "})\n"
    )

    calls = _checker.extract_http_calls(source, FILENAME, notes)

    assert not calls
    # The hoisted URL (line 2) and the method reference in its callback
    # (line 3) are independently uncheckable, so both must be reported.
    assert len(notes) == 2
    assert any("non-literal expression" in note for note in notes)
    assert any(note.startswith(f"{FILENAME}:3:") for note in notes)


def test_conditional_query_interpolation_is_not_a_path_segment() -> None:
    # ``${includeArchived ? '?archived=true' : ''}`` toggles a query suffix.
    # Treating it as a path segment made a live route look stale.
    live = _checker.build_live_routes({"/api/v1/pipelines": {"get": {}}})
    source = "await apiFetch(apiBase, token, 'GET', `/api/v1/pipelines${includeArchived ? '?archived=true' : ''}`)\n"

    calls = _calls(source)

    assert len(calls) == 1
    assert calls[0].path == "/api/v1/pipelines"
    assert not _checker.find_violations(calls, live)


# ---------------------------------------------------------------------------
# FAR-1847 final structural pass: coverage ledger and path-shape correctness
# ---------------------------------------------------------------------------


def test_context_request_verb_is_detected() -> None:
    # ``context.request.<verb>`` is an APIRequestContext call distinct from
    # ``page.request`` and ``request``; all three carry the verb as the method.
    source = "await context.request.patch(`${base}/api/v1/pipelines/${id}/graph`, { data })\n"

    calls = _calls(source)

    assert len(calls) == 1
    assert calls[0].kind == "request"
    assert calls[0].method == "PATCH"
    assert calls[0].path == "/api/v1/pipelines/{param}/graph"


def test_page_context_request_verb_is_detected() -> None:
    source = "await page.context().request.get(`${base}/api/v1/pipelines`)\n"

    calls = _calls(source)

    assert len(calls) == 1
    assert calls[0].kind == "request"
    assert calls[0].method == "GET"
    assert calls[0].path == "/api/v1/pipelines"


def test_ternary_path_segment_is_a_parameter_not_a_query() -> None:
    # ``${role ? 'admins' : 'members'}`` builds a PATH SEGMENT. Treating every
    # ``?`` as a query dropped it, so the caller looked like the collection
    # route and a rotated path could pass unseen.
    live = _checker.build_live_routes({"/api/v1/admin/users/admins": {"get": {}}})
    source = "await apiFetch(apiBase, token, 'GET', `/api/v1/admin/users/${role ? 'admins' : 'members'}`)\n"

    calls = _calls(source)

    assert len(calls) == 1
    assert calls[0].path == "/api/v1/admin/users/{param}"
    assert not _checker.find_violations(calls, live)


def test_concatenated_ternary_path_segment_is_a_parameter_not_garbage() -> None:
    # The concatenation form used to splice the ternary's branches into the
    # path as separate literals (``/{param}admins{param}members``).
    live = _checker.build_live_routes({"/api/v1/admin/users/members": {"get": {}}})
    source = "await apiFetch(apiBase, token, 'GET', '/api/v1/admin/users/' + (role ? 'admins' : 'members'))\n"

    calls = _calls(source)

    assert len(calls) == 1
    assert calls[0].path == "/api/v1/admin/users/{param}"
    assert not _checker.find_violations(calls, live)


def test_conditional_query_in_the_else_branch_is_still_not_a_path_segment() -> None:
    # The query may be toggled on in either branch; a ``?``-prefixed branch
    # anywhere keeps the interpolation a query suffix.
    live = _checker.build_live_routes({"/api/v1/pipelines": {"get": {}}})
    source = "await apiFetch(apiBase, token, 'GET', `/api/v1/pipelines${archived ? '' : '?archived=true'}`)\n"

    calls = _calls(source)

    assert len(calls) == 1
    assert calls[0].path == "/api/v1/pipelines"
    assert not _checker.find_violations(calls, live)


def test_mid_path_converter_matches_multi_segment_and_its_suffix() -> None:
    # ``{slug:path}`` consumes one or more segments even when it is NOT the
    # final pattern segment (registry.py:260).
    routes = _checker.build_live_routes({"/api/v1/registry/primitives/{slug:path}/download": {"post": {}}})
    matching = _checker.HttpCall(
        FILENAME,
        1,
        "POST",
        "/api/v1/registry/primitives/acme/widgets/download",
        "apiFetch",
    )
    missing_suffix = _checker.HttpCall(
        FILENAME,
        2,
        "POST",
        "/api/v1/registry/primitives/acme/widgets",
        "apiFetch",
    )

    assert _checker.path_matches(matching, routes[0])
    assert not _checker.path_matches(missing_suffix, routes[0])


def test_final_path_converter_matches_a_multi_segment_request() -> None:
    routes = _checker.build_live_routes({"/api/v1/registry/primitives/{slug:path}": {"get": {}}})
    call = _checker.HttpCall(FILENAME, 1, "GET", "/api/v1/registry/primitives/acme/widgets", "apiFetch")

    assert _checker.path_matches(call, routes[0])


def test_coverage_ledger_flags_an_unmodelled_call_shape() -> None:
    # A call shape neither modelled nor annotated must fail the run, naming the
    # exact line - otherwise a new request silently escapes the deploy gate.
    unanalysed: list[str] = []

    calls = _checker.extract_http_calls("const res = await fetch(buildUrl(), init)\n", FILENAME, [], unanalysed)

    assert not calls
    assert len(unanalysed) == 1
    assert unanalysed[0].startswith(f"{FILENAME}:1: unanalysed E2E HTTP call shape")
    assert "e2e-route-check: ignore" in unanalysed[0]


def test_coverage_ledger_accepts_a_same_line_suppression() -> None:
    unanalysed: list[str] = []

    calls = _checker.extract_http_calls(
        "await fetch(buildUrl(), init) // e2e-route-check: ignore test fixture\n",
        FILENAME,
        [],
        unanalysed,
    )

    assert not calls
    assert not unanalysed


def test_coverage_ledger_accepts_a_preceding_line_suppression() -> None:
    unanalysed: list[str] = []
    source = "// e2e-route-check: ignore test fixture\nawait fetch(buildUrl(), init)\n"

    calls = _checker.extract_http_calls(source, FILENAME, [], unanalysed)

    assert not calls
    assert not unanalysed


def test_coverage_ledger_notes_a_method_filter_even_when_the_callback_yields_a_descriptor() -> None:
    # The old ``len(calls) == calls_before`` guard silenced an unmodelled filter
    # when another comparison in the SAME callback resolved. The ledger reports
    # the unmodelled reference per line.
    notes: list[str] = []
    source = (
        "await page.route('**/api/v1/pipelines/abc/graph', (route) => {\n"
        "  if (route.request().method() === 'PUT') {\n"
        "    return route.fulfill({ status: 200 })\n"
        "  }\n"
        "  if (route.request().method() !== 'GET') {\n"
        "    return route.fulfill({ status: 405 })\n"
        "  }\n"
        "})\n"
    )

    calls = _checker.extract_http_calls(source, FILENAME, notes)

    assert [(call.method, call.path) for call in calls] == [("PUT", "/api/v1/pipelines/abc/graph")]
    assert any(note.startswith(f"{FILENAME}:5:") for note in notes)
    assert all("cannot attribute to a single literal method" in note for note in notes)


def test_coverage_ledger_does_not_flag_the_api_fetch_declaration() -> None:
    unanalysed: list[str] = []
    source = (
        "export async function apiFetch<T>(\n"
        "  apiBase: string,\n"
        "  token: string,\n"
        "  method: 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE',\n"
        "  path: string,\n"
        "): Promise<T> {\n"
        "  return request(apiBase, token, method, path)\n"
        "}\n"
    )

    calls = _checker.extract_http_calls(source, FILENAME, [], unanalysed)

    assert not calls
    assert not unanalysed


def test_main_fails_when_the_ledger_reports_an_unanalysed_shape(monkeypatch) -> None:
    working = _checker.extract_http_calls(
        "await apiFetch(apiBase, token, 'GET', '/api/v1/pipelines/abc/graph')\n",
        "frontend/tests/e2e/setup/realstack-api.ts",
    ) + _checker.extract_http_calls(
        "await apiFetch(apiBase, token, 'PUT', '/api/v1/pipelines/abc/graph')\n",
        "frontend/tests/e2e/pipeline-editor.spec.ts",
    )

    def _collect(notes=None, unanalysed=None):
        if unanalysed is not None:
            unanalysed.append(f"{FILENAME}:11: unanalysed E2E HTTP call shape - model it")
        return working

    monkeypatch.setattr(_checker, "load_live_routes", lambda: LIVE_ROUTES)
    monkeypatch.setattr(_checker, "collect_calls", _collect)

    assert _checker.main() == 1


def test_destructured_filter_is_noted_even_when_the_same_callback_yields_a_descriptor() -> None:
    # The removed ``len(calls) == calls_before`` guard used to suppress the
    # destructured-filter note whenever another comparison in the callback
    # resolved. A descriptor for one comparison does not validate the other.
    notes: list[str] = []
    source = (
        "await page.route('**/api/v1/pipelines/abc/graph', (route) => {\n"
        "  const { method } = route.request()\n"
        "  if (route.request().method() === 'PUT') {\n"
        "    return route.fulfill({ status: 200 })\n"
        "  }\n"
        "})\n"
    )

    calls = _checker.extract_http_calls(source, FILENAME, notes)

    assert [(call.method, call.path) for call in calls] == [("PUT", "/api/v1/pipelines/abc/graph")]
    assert any(note.startswith(f"{FILENAME}:1:") for note in notes)


# ---------------------------------------------------------------------------
# FAR-1847 ledger gaps: property-qualified callees, transport helper, per-call
# accounting, and ``new URL`` / ``new Request`` fetch targets.
# ---------------------------------------------------------------------------


def test_global_this_fetch_is_extracted() -> None:
    # ``globalThis.fetch`` (and ``window.fetch``) is a property-qualified call.
    # The old marker excluded any ``.``-prefixed ``fetch``, so this live login
    # call was neither extracted nor noted - a silent ledger gap.
    source = "await globalThis.fetch('/api/v1/auth/login', { method: 'POST' })\n"

    calls = _calls(source)

    assert len(calls) == 1
    assert calls[0].kind == "fetch"
    assert calls[0].method == "POST"
    assert calls[0].path == "/api/v1/auth/login"


def test_page_request_fetch_is_extracted() -> None:
    source = "await page.request.fetch('/api/v1/pipelines', { method: 'DELETE' })\n"

    calls = _calls(source)

    assert len(calls) == 1
    assert calls[0].kind == "fetch"
    assert calls[0].method == "DELETE"
    assert calls[0].path == "/api/v1/pipelines"


def test_fetch_with_transient_retry_is_extracted_and_checked() -> None:
    # ``fetchWithTransientRetry`` is the shared transport helper: a real HTTP
    # call whose method lives in its init. It must be extracted, not trusted.
    live = _checker.build_live_routes({"/api/v1/triggers/{trigger_id}/webhook": {"post": {}}})
    source = "await fetchWithTransientRetry('/api/v1/triggers/abc/webhook', { method: 'POST' })\n"

    calls = _calls(source)

    assert len(calls) == 1
    assert calls[0].method == "POST"
    assert calls[0].path == "/api/v1/triggers/abc/webhook"
    assert not _checker.find_violations(calls, live)


def test_fetch_with_transient_retry_stale_method_is_reported() -> None:
    live = _checker.build_live_routes({"/api/v1/triggers/{trigger_id}/webhook": {"post": {}}})
    source = "await fetchWithTransientRetry('/api/v1/triggers/abc/webhook', { method: 'GET' })\n"

    calls = _calls(source)
    violations = _checker.find_violations(calls, live)

    assert len(calls) == 1
    assert len(violations) == 1
    assert "not GET" in violations[0].detail


def test_transport_helper_declaration_is_not_a_candidate() -> None:
    unanalysed: list[str] = []
    source = (
        "async function fetchWithTransientRetry(\n"
        "  url: string,\n"
        "  init: RequestInit,\n"
        "): Promise<{ status: number }> {\n"
        "  // e2e-route-check: ignore generic transport helper\n"
        "  const res = await fetch(url, init)\n"
        "  return { status: res.status }\n"
        "}\n"
    )

    calls = _checker.extract_http_calls(source, FILENAME, [], unanalysed)

    assert not calls
    assert not unanalysed


def test_two_calls_on_one_line_do_not_absorb_each_other() -> None:
    # Per-LINE accounting let the modelled ``fetch('/api/v1/pipelines')`` hide
    # the unmodelled ``fetch(buildUrl(), init)`` beside it. Accounting is now
    # per call occurrence, so the second one is still a violation.
    unanalysed: list[str] = []
    source = "await Promise.all([fetch('/api/v1/pipelines'), fetch(buildUrl(), init)])\n"

    calls = _checker.extract_http_calls(source, FILENAME, [], unanalysed)

    assert len(calls) == 1
    assert calls[0].method == "GET"
    assert len(unanalysed) == 1
    assert unanalysed[0].startswith(f"{FILENAME}:1: unanalysed E2E HTTP call shape")


def test_two_call_occurrences_can_be_individually_accounted() -> None:
    unanalysed: list[str] = []
    source = "await Promise.all([fetch('/api/v1/pipelines'), fetch('/api/v1/runs')])\n"

    calls = _checker.extract_http_calls(source, FILENAME, [], unanalysed)

    assert [(call.method, call.path) for call in calls] == [
        ("GET", "/api/v1/pipelines"),
        ("GET", "/api/v1/runs"),
    ]
    assert not unanalysed


def test_fetch_new_url_target_extracts_the_path_literal() -> None:
    # ``fetch(new URL('/api/v1/auth/login', base), { method: 'POST' })``: the
    # fragment scanner used to splice the base argument into the path as
    # ``/api/v1/auth/login/{param}`` - a false stale violation.
    live = _checker.build_live_routes({"/api/v1/auth/login": {"post": {}}})
    source = "await fetch(new URL('/api/v1/auth/login', base), { method: 'POST' })\n"

    calls = _calls(source)

    assert len(calls) == 1
    assert calls[0].method == "POST"
    assert calls[0].path == "/api/v1/auth/login"
    assert not _checker.find_violations(calls, live)


def test_fetch_new_request_target_extracts_path_and_method() -> None:
    # ``fetch(new Request(path, { method }))``: the method lives in the Request
    # init, not in an outer fetch init. It used to be read as an implicit GET
    # and the method literal leaked into the path.
    live = _checker.build_live_routes({"/api/v1/auth/login": {"post": {}}})
    source = "await fetch(new Request('/api/v1/auth/login', { method: 'POST' }))\n"

    calls = _calls(source)

    assert len(calls) == 1
    assert calls[0].method == "POST"
    assert calls[0].path == "/api/v1/auth/login"
    assert not _checker.find_violations(calls, live)


def test_realstack_webhook_transport_call_is_extracted() -> None:
    # The helper's caller is literal even though the helper itself is generic:
    # ``fireWebhook`` names POST /api/v1/triggers/{id}/webhook. It used to be
    # extracted by nothing and annotated by nothing.
    source = (REPO_ROOT / "frontend/tests/e2e/setup/realstack-api.ts").read_text(encoding="utf-8")

    calls = _checker.extract_http_calls(source, "frontend/tests/e2e/setup/realstack-api.ts")

    webhook_calls = [
        call for call in calls if call.path == "/api/v1/triggers/{param}/webhook" and call.method == "POST"
    ]

    # Shape, not a line pin: the spec moves and a hard-coded line turns a
    # behavioural test into a brittle one (the suite's own rule - see
    # ``test_real_tenancy_rbac_leading_interpolation_is_resolved``).
    assert len(webhook_calls) == 1
    assert {(call.kind, call.method, call.path) for call in webhook_calls} == {
        ("fetch", "POST", "/api/v1/triggers/{param}/webhook")
    }


# ---------------------------------------------------------------------------
# FAR-1847 final review: aliased callables, init-parsing depth, recursive
# constructor unwrapping, and query-vs-path suffix classification.
# ---------------------------------------------------------------------------


def test_fetch_alias_forms_are_unanalysed() -> None:
    # A renamed callable defeats the extractor silently: ``const f =
    # window.fetch``, a destructured binding, ``fetch.bind(...)`` and
    # ``fetch.call(...)`` all issue real requests. The alias is not resolved -
    # the occurrence must surface as unanalysed so it is annotated explicitly.
    for source in (
        "const f = window.fetch\nawait f('/api/v1/pipelines', { method: 'POST' })\n",
        "const { fetch: f } = globalThis\nawait f('/api/v1/pipelines', { method: 'POST' })\n",
        "const f = fetch.bind(globalThis)\nawait f('/api/v1/pipelines', { method: 'POST' })\n",
        "await fetch.call(null, '/api/v1/pipelines', { method: 'POST' })\n",
    ):
        unanalysed: list[str] = []

        calls = _checker.extract_http_calls(source, FILENAME, [], unanalysed)

        assert not calls
        assert len(unanalysed) == 1
        assert unanalysed[0].startswith(f"{FILENAME}:1: unanalysed E2E HTTP call shape")


def test_bracket_request_verb_is_unanalysed() -> None:
    # ``page.request['post'](...)`` is an APIRequestContext call whose verb is
    # dynamic; it was neither extracted nor flagged.
    unanalysed: list[str] = []
    source = "await page.request['post']('/api/v1/pipelines', { data })\n"

    calls = _checker.extract_http_calls(source, FILENAME, [], unanalysed)

    assert not calls
    assert len(unanalysed) == 1
    assert unanalysed[0].startswith(f"{FILENAME}:1: unanalysed E2E HTTP call shape")


def test_renamed_request_fixture_binding_is_unanalysed() -> None:
    # Playwright fixtures are destructured by name: ``async ({ request: api })
    # => api.get(...)`` - the binding, not the call, is the only visible marker.
    unanalysed: list[str] = []

    calls = _checker.extract_http_calls(
        "test('x', async ({ request: api }) => { await api.get('/api/v1/pipelines') })\n", FILENAME, [], unanalysed
    )

    assert not calls
    assert len(unanalysed) == 1
    assert unanalysed[0].startswith(f"{FILENAME}:1: unanalysed E2E HTTP call shape")


def test_api_fetch_alias_forms_are_unanalysed() -> None:
    # The fetch alias handling was not mirrored onto ``apiFetch``, so these real
    # requests vanished with no descriptor, note or unanalysed entry.
    for source in (
        "const call = apiFetch\nawait call(b, t, 'PATCH', '/api/v1/pipelines/1/graph')\n",
        "const { apiFetch: af } = helpers\nawait af(b, t, 'PATCH', '/api/v1/pipelines/1/graph')\n",
        "const af = helpers.apiFetch\nawait af(b, t, 'PATCH', '/api/v1/pipelines/1/graph')\n",
        "await helpers.apiFetch(b, t, 'PATCH', '/api/v1/pipelines/1/graph')\n",
    ):
        unanalysed: list[str] = []

        calls = _checker.extract_http_calls(source, FILENAME, [], unanalysed)

        assert not calls
        assert len(unanalysed) == 1
        assert unanalysed[0].startswith(f"{FILENAME}:1: unanalysed E2E HTTP call shape")


def test_api_fetch_import_and_export_are_not_candidates() -> None:
    # An imported/re-exported binding is not a call; flagging it would make
    # every spec file that imports the helper unanalysed.
    unanalysed: list[str] = []
    source = (
        "import { apiBaseFor, apiFetch } from './realstack-api'\n"
        "import {\n"
        "  apiFetch as fetchApi,\n"
        "} from './other'\n"
        "export { apiFetch }\n"
    )

    calls = _checker.extract_http_calls(source, FILENAME, [], unanalysed)

    assert not calls
    assert not unanalysed


def test_request_context_alias_forms_are_unanalysed() -> None:
    # A captured APIRequestContext loses the ``page.request`` marker, so its
    # later ``api.patch(...)`` calls are invisible. The capture is flagged.
    for source in (
        "const api = page.request\nawait api.patch('/api/v1/pipelines/1/graph')\n",
        "const api = await request.newContext()\nawait api.patch('/api/v1/pipelines/1/graph')\n",
        "const api = page.context().request\nawait api.patch('/api/v1/pipelines/1/graph')\n",
    ):
        unanalysed: list[str] = []

        calls = _checker.extract_http_calls(source, FILENAME, [], unanalysed)

        assert not calls
        assert len(unanalysed) == 1
        assert unanalysed[0].startswith(f"{FILENAME}:1: unanalysed E2E HTTP call shape")


def test_direct_request_verb_is_not_an_alias_candidate() -> None:
    # ``page.request.post(...)`` is extracted, not an alias capture - the
    # lookahead must not turn every direct verb call into a false candidate.
    unanalysed: list[str] = []
    source = "const createResp = await page.request.post('/api/v1/sso/providers', { data })\n"

    calls = _checker.extract_http_calls(source, FILENAME, [], unanalysed)

    assert [(call.method, call.path) for call in calls] == [("POST", "/api/v1/sso/providers")]
    assert not unanalysed


def test_bracket_fetch_is_unanalysed() -> None:
    # ``globalThis['fetch']`` places the callee behind a blanked string key, so
    # the extractor sees no ``fetch`` identifier at all.
    unanalysed: list[str] = []

    calls = _checker.extract_http_calls(
        "await globalThis['fetch']('/api/v1/pipelines/1', { method: 'DELETE' })\n", FILENAME, [], unanalysed
    )

    assert not calls
    assert len(unanalysed) == 1
    assert unanalysed[0].startswith(f"{FILENAME}:1: unanalysed E2E HTTP call shape")


def test_fetch_alias_can_be_suppressed_with_an_annotation() -> None:
    # The escape hatch is the point of the ledger: a genuinely non-HTTP alias
    # (capturing fetch to restore it) is annotated, not silently ignored.
    unanalysed: list[str] = []
    source = "// e2e-route-check: ignore captures the original fetch to restore it\nconst f = window.fetch\n"

    calls = _checker.extract_http_calls(source, FILENAME, [], unanalysed)

    assert not calls
    assert not unanalysed


def test_fetch_assignment_and_typeof_reference_are_not_candidates() -> None:
    # A write (replacing global fetch) and a ``typeof`` type reference are not
    # calls; flagging them would make the ledger noisy for no coverage.
    unanalysed: list[str] = []
    source = "globalThis.fetch = replacement\nconst ref = value as typeof globalThis.fetch\n"

    calls = _checker.extract_http_calls(source, FILENAME, [], unanalysed)

    assert not calls
    assert not unanalysed


def test_nested_body_method_is_not_the_request_method() -> None:
    # ``{ body: JSON.stringify({ method: 'DELETE' }) }`` is an implicit GET -
    # a flat text scan read the nested ``method`` as the HTTP method.
    calls = _calls("await fetch('/api/v1/pipelines', { body: JSON.stringify({ method: 'DELETE' }) })\n")

    assert len(calls) == 1
    assert calls[0].method == "GET"
    assert calls[0].path == "/api/v1/pipelines"


def test_method_inside_a_comment_is_not_a_property() -> None:
    calls = _calls("await fetch('/api/v1/pipelines', { headers /* method: 'DELETE' */ })\n")

    assert len(calls) == 1
    assert calls[0].method == "GET"


def test_init_property_order_does_not_change_the_method() -> None:
    for source, expected in (
        ("await fetch('/api/v1/pipelines', { headers, method: 'DELETE' })\n", "DELETE"),
        ("await fetch('/api/v1/pipelines', { method: 'DELETE', headers })\n", "DELETE"),
    ):
        calls = _calls(source)

        assert len(calls) == 1
        assert calls[0].method == expected


def test_first_top_level_method_property_wins() -> None:
    calls = _calls("await fetch('/api/v1/pipelines', { method: 'POST', method: 'DELETE' })\n")

    assert len(calls) == 1
    assert calls[0].method == "POST"


def test_nested_url_inside_request_is_unwrapped() -> None:
    # ``new Request(new URL(path, base), init)``: one level of unwrapping left
    # the URL constructor in the path and reported a false stale violation.
    live = _checker.build_live_routes({"/api/v1/auth/login": {"post": {}}})
    source = "await fetch(new Request(new URL('/api/v1/auth/login', base), { method: 'POST' }))\n"

    calls = _calls(source)

    assert len(calls) == 1
    assert calls[0].method == "POST"
    assert calls[0].path == "/api/v1/auth/login"
    assert not _checker.find_violations(calls, live)


def test_property_chain_off_a_query_name_is_a_path_segment() -> None:
    # ``params.id`` / ``search.id`` / ``filters.x`` are property reads, not
    # query strings: treating them as queries dropped the segment and made
    # ``/api/v1/pipelines/{id}/graph`` match the collection route (false stale).
    for expression in ("params.id", "search.id", "filters.x"):
        source = f"await apiFetch(apiBase, token, 'GET', `/api/v1/pipelines/${{{expression}}}/graph`)\n"

        calls = _calls(source)

        assert len(calls) == 1
        assert calls[0].path == "/api/v1/pipelines/{param}/graph"


def test_method_call_on_a_query_name_is_still_a_query_suffix() -> None:
    calls = _calls("await apiFetch(apiBase, token, 'GET', `/api/v1/triggers${queryParams.toString()}`)\n")

    assert len(calls) == 1
    assert calls[0].path == "/api/v1/triggers"


def test_property_chain_off_a_query_name_in_a_concatenation_is_a_path_segment() -> None:
    calls = _calls("await apiFetch(apiBase, token, 'GET', '/api/v1/pipelines/' + params.id + '/graph')\n")

    assert len(calls) == 1
    assert calls[0].path == "/api/v1/pipelines/{param}/graph"


# ---------------------------------------------------------------------------
# FAR-1847 bounded marker completeness: bracket / optional-chain access to the
# helper callables and to the Playwright APIRequestContext. Each shape below is
# a real request; the ledger must enumerate it (extracted, or unanalysed -> exit
# 1), never let it vanish.
# ---------------------------------------------------------------------------


def test_bracket_accessed_helper_callables_are_unanalysed() -> None:
    # ``helpers['apiFetch'](...)`` / ``helpers['fetchWithTransientRetry'](...)``
    # reach a real request through a quoted key, which the extractor's
    # ``.``-only member chains never spell.
    for source in (
        "await helpers['apiFetch'](b, t, 'PATCH', '/api/v1/pipelines/1/graph')\n",
        "await helpers['fetchWithTransientRetry']('/api/v1/pipelines/1/graph', { method: 'PATCH' })\n",
    ):
        unanalysed: list[str] = []

        calls = _checker.extract_http_calls(source, FILENAME, [], unanalysed)

        assert not calls
        assert len(unanalysed) == 1
        assert unanalysed[0].startswith(f"{FILENAME}:1: unanalysed E2E HTTP call shape")


def test_optional_chained_api_fetch_is_unanalysed() -> None:
    # ``helpers?.apiFetch(...)``: the optional chain breaks the extractor's
    # ``.``-only member match, so only the ledger can see the occurrence.
    unanalysed: list[str] = []
    source = "await helpers?.apiFetch(b, t, 'PATCH', '/api/v1/pipelines/1/graph')\n"

    calls = _checker.extract_http_calls(source, FILENAME, [], unanalysed)

    assert not calls
    assert len(unanalysed) == 1
    assert unanalysed[0].startswith(f"{FILENAME}:1: unanalysed E2E HTTP call shape")


def test_optional_chained_retry_fetch_is_extracted_not_flagged() -> None:
    # ``helpers?.fetchWithTransientRetry(...)`` is a candidate that extraction
    # DOES resolve (its lookbehind permits a preceding ``.``), so the marker
    # must not manufacture a duplicate unanalysed entry for the same offset.
    unanalysed: list[str] = []
    source = "await helpers?.fetchWithTransientRetry('/api/v1/pipelines/1/graph', { method: 'PATCH' })\n"

    calls = _checker.extract_http_calls(source, FILENAME, [], unanalysed)

    assert len(calls) == 1
    assert calls[0].method == "PATCH"
    assert calls[0].path == "/api/v1/pipelines/1/graph"
    assert not unanalysed


def test_optional_chained_request_verb_is_unanalysed() -> None:
    # ``page.request?.post(...)`` / ``request?.delete(...)``: the extractor
    # requires a plain ``.`` before the verb.
    for source in (
        "await page.request?.post('/api/v1/pipelines', { data })\n",
        "await request?.delete('/api/v1/pipelines/1')\n",
    ):
        unanalysed: list[str] = []

        calls = _checker.extract_http_calls(source, FILENAME, [], unanalysed)

        assert not calls
        assert len(unanalysed) == 1
        assert unanalysed[0].startswith(f"{FILENAME}:1: unanalysed E2E HTTP call shape")


def test_bracket_accessed_request_verb_is_unanalysed() -> None:
    # ``page['request'].post(...)`` / ``context['request'].delete(...)``: the
    # ``request`` property is behind a quoted key, so no ``.request`` spelling
    # matches and the verb call would otherwise vanish.
    for source in (
        "await page['request'].post('/api/v1/pipelines', { data })\n",
        "await context['request'].delete('/api/v1/pipelines/1')\n",
    ):
        unanalysed: list[str] = []

        calls = _checker.extract_http_calls(source, FILENAME, [], unanalysed)

        assert not calls
        assert len(unanalysed) == 1
        assert unanalysed[0].startswith(f"{FILENAME}:1: unanalysed E2E HTTP call shape")


def test_destructured_request_verbs_are_unanalysed() -> None:
    # ``const { get, post: create } = page.request``: the verbs become bare
    # identifiers, so the later calls carry no ``request`` marker. The binding
    # is the only visible occurrence.
    unanalysed: list[str] = []
    source = (
        "const { get, post: create } = page.request\n"
        "await get('/api/v1/pipelines')\n"
        "await create('/api/v1/pipelines', { data })\n"
    )

    calls = _checker.extract_http_calls(source, FILENAME, [], unanalysed)

    assert not calls
    assert len(unanalysed) == 1
    assert unanalysed[0].startswith(f"{FILENAME}:1: unanalysed E2E HTTP call shape")


def test_unrelated_bracket_keys_and_destructuring_are_not_candidates() -> None:
    # Precision: the new markers must not turn ordinary property access into
    # unanalysed noise - only a quoted helper/context key counts.
    source = "const { get } = config\nconst value = form['name']\n"

    unanalysed: list[str] = []
    calls = _checker.extract_http_calls(source, FILENAME, [], unanalysed)

    assert not calls
    assert not unanalysed


def test_documented_limitation_dynamic_property_access_is_not_enumerated() -> None:
    # KNOWN LIMITATION, see the module docstring's "Known limitations" section;
    # the AST rewrite is tracked separately.
    #
    # This is intentional: a callee assembled at runtime is outside the
    # text-based marker set's contract, so it produces no descriptor, no note
    # and no unanalysed entry. The assertion PINS that boundary so it cannot
    # drift silently - if a future change starts enumerating these shapes, this
    # test fails and the docstring contract must be updated with it.
    for source in (
        # Dynamic property access: the key itself is a runtime value.
        "const key = 'apiFetch'\nconst send = helpers[key]\nawait send(b, t, 'PATCH', '/api/v1/pipelines/1/graph')\n",
        # A callee passed as data into a user-defined higher-order function.
        "const send = (fn) => fn('/api/v1/pipelines/1/graph')\nawait send(page.request.post)\n",
    ):
        notes: list[str] = []
        unanalysed: list[str] = []

        calls = _checker.extract_http_calls(source, FILENAME, notes, unanalysed)

        assert not calls
        assert not notes
        assert not unanalysed
