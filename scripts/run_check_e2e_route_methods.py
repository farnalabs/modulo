#!/usr/bin/env python3
"""Fail when an E2E HTTP call no longer matches a live Modulo API route.

Deploy-critical: ``.github/workflows/deploy.yml`` runs
``playwright test --grep "@regression"`` against staging before promoting to
production. A ``@regression`` caller that names a rotated method is therefore a
deploy blocker, and a stale Playwright interceptor is worse than a blocker -
the spec keeps passing while asserting nothing, and the gate reads as coverage.

This guard exists because FAR-1829 rotated ``PATCH /api/v1/pipelines/{id}/graph``
to ``PUT`` and two callers were invisible to a path-based grep:

1. ``frontend/tests/e2e/setup/realstack-api.ts`` passes the method as a bare
   string argument to ``apiFetch(...)``; it throws on a non-200, so a rotated
   route fails the deploy at staging.
2. ``frontend/tests/e2e/pipeline-editor.spec.ts`` registered
   ``page.route('**/api/v1/pipelines/<id>/graph', ...)`` and filtered on
   ``route.request().method() === 'PATCH'``. After the rotation that branch
   never fired - the test still passed, asserting a request that was never
   intercepted.

The check deliberately does not grep for ``'PATCH'``: legitimate PATCH routes
exist (``/api/v1/pipelines/{id}``, ``.../snapshots/{id}``,
``.../folder``). It builds the live route table from the app's ACTUAL route
objects (in-process, template env, no network or database - the same import the
schema-freshness job uses) and matches every extracted (method, path) pair
against it. ``app.openapi()['paths']`` is not enough: it omits
``include_in_schema=False`` routes and renders a ``{param:path}`` converter as a
single segment, so valid requests were reported as stale. A path converter is
matched as a multi-segment (``.*``) tail instead.

What is scanned (``frontend/tests/e2e/**/*.ts``):

* ``apiFetch(<base>, <token>, <method>, <path>)`` with the method as a
  string literal (a trailing ``as const`` / ``as HttpMethod`` assertion is
  stripped) and the path as a string/template literal, across multi-line
  argument lists and ``${...}`` interpolation.
* direct ``fetch(<url>, { method: '<METHOD>' })`` in the setup helpers, plus
  the implicit-GET ``fetch(<url>)`` / ``fetch(<url>, { headers })`` form (a
  genuinely absent ``method`` key means GET, which is checked like any other
  method). Property-qualified callees are the same call shape
  (``globalThis.fetch``, ``window.fetch``, ``page.request.fetch``).
  ``fetchWithTransientRetry(<url>, <init>)`` - the shared transport helper - is
  modelled identically, so its ``method``/URL arguments are checked rather than
  trusted. ``new URL(<path>, <base>)`` and ``new Request(<path>, <init>)``
  targets unwrap to the path literal, and a ``Request``'s own ``method`` is read
  from its init. A ``method`` property present without a literal value (the
  ``{ method }`` shorthand, a quoted key, or any other non-literal value) is
  unresolvable, not an implicit GET, and is reported as a note.
* ``page.request.<verb>(<url>)`` / ``context.request.<verb>(<url>)`` /
  ``page.context().request.<verb>(<url>)`` / ``request.<verb>(<url>)`` calls on
  a Playwright ``APIRequestContext`` (the verb is the method).
* ``route.request().method() === '<METHOD>'`` interceptor filters (and the
  reversed ``'<METHOD>' === route.request().method()`` form), resolved against
  the ``page.route('<url>', ...)`` registration that encloses them in the same
  file/scope. The callback's parameters may be renamed and both are read: the
  first is the route, the second is the request (``async (route, request) =>
  ... request.method() === 'GET'``). A local variable holding the request
  object (``const request = route.request()``), the method result
  (``const m = route.request().method()``) or a boolean holding a positive
  method comparison (``const isPatch = route.request().method() === 'PATCH';
  if (isPatch)``) is resolved too. A comparison combined with an in-callback
  URL test (``url.includes`` / ``url.endsWith``) is not guessed - its effective
  path lives in that condition, not in the registration, so it is reported as
  a note. ``!== '<METHOD>'`` and negated-boolean filters are skipped too - they
  guard a fallback branch rather than selecting a method - and any other method
  reference the extractor cannot attribute to one literal method (a
  destructured ``const { method } = route.request()``, an array-membership
  ``['PATCH', 'PUT'].includes(...)`` test) leaves a note for its registration,
  so no filter shape is silently dropped. A catch-all mock
  (``**/api/v1/**``) is skipped silently only when it carries no method filter;
  one that mocks specific routes by URL and filters on the method is noted.
  Anything that cannot be resolved safely is reported as one explicit note on
  stderr (never silently dropped, and never a failure).

Template placeholders (``${expr}``, ``:param``, ``{param}``) and glob
wildcards normalise to one path-parameter segment, and an anchored
regex-literal registration (``/\\/api\\/v1\\/plan$/``) is normalised the same
way (anchors stripped, parameter-ish groups/classes collapsed to ``{param}``).
A leading interpolation before the ``/api/`` segment
(``${apiBase}/api/v1/...``) is discarded, and a concatenation that ends in a
genuine path variable (``'/api/v1/pipelines/' + id``) keeps a trailing
wildcard. A trailing query-string variable (``'/api/v1/pipelines' + query``),
a conditional query interpolation
(``${includeArchived ? '?archived=true' : ''}``), a TypeScript assertion
(``'/api/v1/auth/me' as const``) and a literal that already carries a ``?`` add
no segment. Query strings and fragments are stripped.

Coverage ledger (the guard's invariant - no silent skips)
--------------------------------------------------------

Extraction alone can never prove it saw every call shape: the space of
JavaScript expressions that build an HTTP request is unbounded, and every miss
is silent. The check therefore also enumerates CANDIDATE call OCCURRENCES with a
deliberately broad marker set - ``apiFetch(``, ``fetch(`` and every
property-qualified form (``globalThis.fetch(``, ``page.request.fetch(``),
``fetchWithTransientRetry(``, ``page.request.<verb>(`` /
``context.request.<verb>(`` / ``request.<verb>(`` - plus method-reference lines
(``.request().method(``, ``request.method(``, and HTTP-method literals compared
against a method expression: ``=== 'PATCH'``, ``!== 'GET'``, ``includes(...)``).
Aliasing forms are candidates even though they cannot be modelled, because a
miss there is silent: a ``fetch`` identifier READ as a value
(``const f = window.fetch``, ``= fetch.bind(...)``, ``const { fetch: f } =
globalThis``), bracket access to a request context
(``page.request['post'](...)``), and a renamed ``request`` binding
(``async ({ request: api }) => api.get(...)``). Assignment targets and
``typeof`` type references are not calls and are excluded.
Every candidate CALL occurrence MUST be accounted for individually on the same
``file:line`` by exactly one of:

* a descriptor extracted for that occurrence, or
* an explicit note emitted for that occurrence, or
* an inline suppression annotation on that line or the immediately preceding
  line: ``// e2e-route-check: ignore <short reason>``.

Accounting is per occurrence, not per line: a second, unmodelled call on a line
that also carries a modelled call (``Promise.all([fetch(a), fetch(b, init)])``)
is still a violation. An unaccounted API-call occurrence is a violation (exit 1)
naming the line: a new call shape fails loudly instead of passing. A
method-reference line is reported as a note when unaccounted - it names no route
this checker can validate, but it is never silently dropped. A candidate is only
excluded structurally when it is provably not a call (an ``apiFetch`` /
``fetchWithTransientRetry`` function declaration, or an identifier with no
argument list), never by a pattern that happens to miss.

Exit codes: 0 clean, 1 stale descriptors or unanalysed call shapes (one line
each, with file:line, the method, the path and the closest live route), 2
setup/tool error - including an extractor that found no descriptors at all or
no descriptor in either ticket-named file - never a silent pass.
"""

from __future__ import annotations

import difflib
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
E2E_DIR = REPO_ROOT / "frontend" / "tests" / "e2e"

# Same template environment the schema-freshness job and
# scripts/run_generate_api_types.py use to import the app without a DB.
_TEMPLATE_ENV = {
    "DATABASE_URL": "sqlite+aiosqlite://",
    "SECRET_KEY": "a" * 32,
    "FERNET_KEY": "b" * 32,
    "MODULO_CSRF_ENABLED": "false",
}

_HTTP_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})
_WILDCARD = "{param}"
_TEMPLATE_EXPR_RE = re.compile(r"\$\{[^{}]*\}")
#: Names conventionally holding a query string, not a path segment. Shared by
#: the template-literal (``${queryParams}``) and concatenation-tail
#: (``'/api/v1/pipelines' + query``) paths so the two cannot disagree.
_QUERY_SUFFIX_NAMES = frozenset(
    {
        "query",
        "queryobject",
        "queryobj",
        "queryparams",
        "querystring",
        "qs",
        "params",
        "search",
        "searchparams",
        "searchquery",
        "searchterm",
        "filters",
    }
)
#: A ``?`` that is a ternary operator or a literal query fragment - never
#: optional chaining (``?.``) or nullish coalescing (``??``). An interpolation
#: carrying one is a conditional query suffix, not a path segment
#: (``${includeArchived ? '?archived=true' : ''}``).
_QUERY_OPERATOR_RE = re.compile(r"(?<!\?)\?(?![.?])")
#: The tail of a query-suffix expression AFTER its conventional name: only a
#: chain of METHOD CALLS on that name (``queryParams.toString()``,
#: ``search.get('page')``) or a literal default. A bare property read
#: (``params.id``, ``filters.x``) is a path segment, not a query string.
_QUERY_SUFFIX_CALL_TAIL_RE = re.compile(r"(?:\s*(?:\?\.|\.)\s*[A-Za-z_$][A-Za-z0-9_$]*\s*\([^()]*\)\s*)+")
_QUERY_SUFFIX_DEFAULT_RE = re.compile(r"\s*(?:\?\?|\|\|)\s*(?:''|\"\"|``)")
#: A trailing TypeScript assertion (``as const``, ``as string``, ``satisfies X``).
_TS_ASSERTION_RE = re.compile(r"(?:^|\s)(?:as|satisfies)\s+[^+\n]+$")
#: Regex-literal route registrations: parameter-ish constructs.
_REGEX_NAMED_GROUP_RE = re.compile(r"\(\?(?:[:=!]|<[A-Za-z_][A-Za-z0-9_]*>)[^()]*\)(?:[+*?]|\{\d+(?:,\d*)?\})?")
_REGEX_GROUP_RE = re.compile(r"\((?:[^()\\]|\\.)*\)(?:[+*?]|\{\d+(?:,\d*)?\})?")
_REGEX_CHAR_CLASS_RE = re.compile(r"\[[^\]]*\](?:[+*?]|\{\d+(?:,\d*)?\})?")
_REGEX_SHORTHAND_RE = re.compile(r"\\[dDwWsS](?:[+*?]|\{\d+(?:,\d*)?\})?")
_REGEX_ESCAPE_RE = re.compile(r"\\([^A-Za-z0-9])")
_COLON_PARAM_RE = re.compile(r"(?<=/):[A-Za-z_][A-Za-z0-9_]*")
#: A Starlette/FastAPI path-converter parameter (``{slug:path}``). Unlike a
#: plain ``{param}`` it consumes one OR MORE segments, so it needs its own
#: marker: normalising it to ``{param}`` would make a multi-segment request
#: look stale. Rendered by the LiveRoute as a multi-segment tail.
_PATH_CONVERTER_RE = re.compile(r"\{[A-Za-z_][A-Za-z0-9_]*\s*:\s*path\}")
_MULTI_WILDCARD = "{param:path}"
_SQUASH_SLASH_RE = re.compile(r"/{2,}")
_SEGMENT_RE = re.compile(r"[A-Za-z0-9._~%\-{}]+")
_API_PREFIX = "/api/"
#: A top-level (depth-1) ``method`` property key in an init object. Properties
#: are enumerated structurally by :func:`_init_method`, never by a flat regex:
#: a nested ``method`` inside a body/headers object, or inside a comment, is not
#: the request method, and a regex scan read it as one.
_TS_OBJECT_ASSERTION_RE = re.compile(r"(?:as|satisfies)\b")
_METHOD_CALL_RE = re.compile(r"\.\s*method\s*\(")
#: A ``method`` pulled out of the request object by destructuring
#: (``const { method } = route.request()``) - a filter form the comparison
#: passes below cannot attribute to a literal method.
_DESTRUCTURED_METHOD_RE = re.compile(r"\{[^}]*\bmethod\b[^}]*\}\s*=\s*[^;\n]*\brequest\s*\(\s*\)")
_ROUTE_CALL_RE = re.compile(r"(?<![\w.])(?:page|context)\s*\.\s*route\s*\(")
_API_FETCH_RE = re.compile(r"(?<![\w.$])apiFetch\b")
#: ``fetch(...)`` with an optional dotted qualifier: bare ``fetch``, plus the
#: property-qualified forms (``globalThis.fetch``, ``window.fetch``,
#: ``page.request.fetch``). A leading ``.`` used to exclude every qualified form,
#: so those calls were neither extracted nor flagged - a silent ledger gap.
_FETCH_RE = re.compile(r"(?<![\w$])(?:[A-Za-z_$][\w$]*\s*\.\s*)*fetch\b")
#: ``fetchWithTransientRetry(url, init)`` - the shared transport helper in
#: ``frontend/tests/e2e/setup/realstack-api.ts``. It is a real HTTP call whose
#: method/path live in its arguments, so it is modelled exactly like ``fetch``.
_RETRY_FETCH_RE = re.compile(r"(?<![\w$])fetchWithTransientRetry\b")
#: ``new URL(<path>, <base>)`` / ``new Request(<path>, <init>)`` used as a
#: ``fetch`` target. The path literal is the first constructor argument; a
#: ``Request`` also carries the method in its init.
_NEW_FETCH_TARGET_RE = re.compile(r"^\s*new\s+(URL|Request)\s*\(")
# ``page.request.get(...)`` / ``context.request.delete(...)`` /
# ``page.context().request.put(...)`` / ``request.post(...)`` on a Playwright
# APIRequestContext.
_API_REQUEST_RE = re.compile(
    r"(?<![\w.$])"
    r"(?:(?:page|context)\s*(?:\.\s*context\s*\(\s*\))?\s*\.\s*request|request)"
    r"\s*\.\s*(get|post|put|patch|delete|head|options)\b"
)
# ---------------------------------------------------------------------------
# Coverage-ledger markers. Deliberately broader than the extractor: anything
# that could name an HTTP call or select on its method is a candidate, and an
# unaccounted candidate is reported rather than silently skipped.
# ---------------------------------------------------------------------------
_HTTP_METHOD_LITERAL_RE = re.compile(r"""['"](?:GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)['"]""", re.IGNORECASE)
_LEDGER_IGNORE_RE = re.compile(r"//\s*e2e-route-check:\s*ignore\b")
#: Bracket access to an APIRequestContext (``page.request['post'](...)``,
#: ``request[verb](...)``). The verb is dynamic, so the call cannot be modelled
#: but must still be a candidate occurrence rather than vanish.
_LEDGER_BRACKET_REQUEST_RE = re.compile(
    r"(?:(?:page|context)\s*(?:\.\s*context\s*\(\s*\))?\s*\.\s*request|(?<![\w.$])request)\s*\["
)
#: A ``request`` binding introduced by destructuring or rename
#: (``async ({ request: api }) => api.get(...)``). The alias itself is not
#: resolvable, so the binding occurrence is the candidate the ledger reports.
_LEDGER_REQUEST_ALIAS_RE = re.compile(r"(?<![\w$.])request\s*:\s*(?P<alias>[A-Za-z_$][A-Za-z0-9_$]*)")
_LEDGER_COMPARISON_RE = re.compile(r"===?|!==?|includes\s*\(")
_LEDGER_METHOD_WORD_RE = re.compile(r"\bmethod\b")
_LEDGER_METHOD_CALL_RE = re.compile(r"\.\s*method\s*\(|(?<![\w.$])request\s*\.\s*method\s*\(")
_LEDGER_UNMODELLED_METHOD_NOTE = (
    "page.route callback filters on the request method in a form this check cannot "
    "attribute to a single literal method, so its method filter was not checked"
)
_UNANALYSED_CALL_MESSAGE = (
    "unanalysed E2E HTTP call shape - model it, or annotate with // e2e-route-check: ignore <reason>"
)
#: Files the guard exists to protect. A run that finds no descriptor in either
#: means the extractor regressed - report a setup error, never "clean".
_REQUIRED_FILES = (
    "frontend/tests/e2e/setup/realstack-api.ts",
    "frontend/tests/e2e/pipeline-editor.spec.ts",
)
_IF_RE = re.compile(r"(?<![\w.])if\s*\(")
_URL_CONDITION_RE = re.compile(r"\.includes\s*\(|\.endsWith\s*\(|\.match\s*\(|request\s*\(\s*\)\s*\.\s*url\s*\(")


@dataclass(frozen=True)
class HttpCall:
    """One statically-extracted HTTP method/path pair from an E2E file."""

    filename: str
    line: int
    method: str
    path: str
    kind: str  # "apiFetch" | "fetch" | "interceptor"
    trailing_glob: bool = False
    last_segment_prefix: bool = False


@dataclass(frozen=True)
class LiveRoute:
    """A live route from the OpenAPI document, with its normalised segments."""

    template: str
    segments: tuple[str, ...]
    methods: frozenset[str]


@dataclass(frozen=True)
class Violation:
    """A stale descriptor: a method/path pair with no matching live route."""

    call: HttpCall
    detail: str

    def describe(self) -> str:
        return f"{self.call.filename}:{self.call.line}: {self.call.method} {self.call.path} ({self.call.kind}) - {self.detail}"


# ---------------------------------------------------------------------------
# Source scanning
# ---------------------------------------------------------------------------


def _blank_literal(text: str, out: list[str], start: int) -> int:
    """Blank a quoted string / template literal in *out*, returning the index after it."""
    quote = text[start]
    index = start + 1
    length = len(text)
    out[start] = " "
    while index < length:
        char = text[index]
        if char == "\\":
            out[index] = " "
            if index + 1 < length and text[index + 1] != "\n":
                out[index + 1] = " "
            index += 2
            continue
        if quote == "`" and char == "$" and index + 1 < length and text[index + 1] == "{":
            depth = 1
            out[index] = " "
            out[index + 1] = " "
            index += 2
            while index < length and depth:
                inner = text[index]
                if inner in ("'", '"', "`"):
                    index = _blank_literal(text, out, index)
                    continue
                if inner == "{":
                    depth += 1
                elif inner == "}":
                    depth -= 1
                if inner != "\n":
                    out[index] = " "
                index += 1
            continue
        if char == quote:
            out[index] = " "
            return index + 1
        if char != "\n":
            out[index] = " "
        index += 1
    return length


def mask_non_code(text: str) -> str:
    """Return *text* with comments and string/template contents blanked to spaces.

    Offsets and newlines are preserved, so structural scanning (parens, commas,
    identifiers) can never be confused by a bracket or comma inside a literal
    or a comment, while line numbers still line up with the original file.
    """
    out = list(text)
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char == "/" and index + 1 < length and text[index + 1] == "/":
            while index < length and text[index] != "\n":
                out[index] = " "
                index += 1
            continue
        if char == "/" and index + 1 < length and text[index + 1] == "*":
            end = text.find("*/", index + 2)
            end = length if end < 0 else end + 2
            for cursor in range(index, end):
                if out[cursor] != "\n":
                    out[cursor] = " "
            index = end
            continue
        if char in ("'", '"', "`"):
            index = _blank_literal(text, out, index)
            continue
        index += 1
    return "".join(out)


def _find_open_paren(masked: str, start: int) -> int | None:
    """Find the ``(`` that opens a call following an identifier at *start*.

    Skips an optional TypeScript generic argument list (tracking ``<``/``>``
    depth) so ``apiFetch<PendingReviewsResponse>(`` is handled. A ``>`` that
    closes a function type arrow (``<{ onSave: () => void }>``) is not a
    generic bracket - counting it ended the generic early and dropped the
    whole call, so the arrow is skipped instead.
    """
    length = len(masked)
    index = start
    while index < length and masked[index] in " \t\r\n":
        index += 1
    if index < length and masked[index] == "<":
        depth = 0
        while index < length:
            char = masked[index]
            if char == "<":
                depth += 1
            elif char == ">":
                previous = index - 1
                while previous >= 0 and masked[previous] in " \t\r\n":
                    previous -= 1
                if previous >= 0 and masked[previous] == "=":
                    index += 1
                    continue
                depth -= 1
                if depth == 0:
                    index += 1
                    break
            index += 1
        while index < length and masked[index] in " \t\r\n":
            index += 1
    if index < length and masked[index] == "(":
        return index
    return None


def _split_args(masked: str, open_paren: int) -> list[tuple[int, int]]:
    """Return ``(start, end)`` spans for each top-level argument of a call."""
    spans: list[tuple[int, int]] = []
    length = len(masked)
    depth = 0
    arg_start = open_paren + 1
    index = open_paren + 1
    while index < length:
        char = masked[index]
        if char in "([{":
            depth += 1
        elif char in ")]}":
            if char == ")" and depth == 0:
                spans.append((arg_start, index))
                return spans
            depth -= 1
        elif char == "," and depth == 0:
            spans.append((arg_start, index))
            arg_start = index + 1
        index += 1
    return spans


def _read_quoted(text: str, start: int) -> tuple[str, int]:
    """Read a quoted string starting at *start*; return ``(value, index_after)``."""
    quote = text[start]
    index = start + 1
    length = len(text)
    buffer: list[str] = []
    while index < length:
        char = text[index]
        if char == "\\" and index + 1 < length:
            buffer.append(text[index + 1])
            index += 2
            continue
        if char == quote:
            return "".join(buffer), index + 1
        buffer.append(char)
        index += 1
    return "".join(buffer), length


def _read_template(text: str, start: int) -> tuple[str, int]:
    """Read a template literal, mapping each ``${...}`` interpolation to ``{param}``."""
    index = start + 1
    length = len(text)
    buffer: list[str] = []
    while index < length:
        char = text[index]
        if char == "\\" and index + 1 < length:
            buffer.append(text[index + 1])
            index += 2
            continue
        if char == "`":
            return "".join(buffer), index + 1
        if char == "$" and index + 1 < length and text[index + 1] == "{":
            depth = 1
            expression_start = index + 2
            index += 2
            while index < length and depth:
                inner = text[index]
                if inner in ("'", '"', "`"):
                    _, index = _read_quoted(text, index)
                    continue
                if inner == "{":
                    depth += 1
                elif inner == "}":
                    depth -= 1
                index += 1
            expression = text[expression_start : index - 1].strip()
            # ``${query}`` / ``${searchParams}`` is conventionally a query-string
            # suffix, not a path segment - counting it as a segment would make a
            # live path look stale (``/api/v1/triggers${query}``).
            if not _is_query_suffix(expression):
                buffer.append(_WILDCARD)
            continue
        buffer.append(char)
        index += 1
    return "".join(buffer), length


def _strip_ts_assertion(expression: str) -> str:
    """Drop a trailing TypeScript assertion (``as const``, ``satisfies X``)."""
    return _TS_ASSERTION_RE.sub("", expression.strip()).strip()


def _is_query_suffix(expression: str) -> bool:
    """True when *expression* is a conventional query-string suffix, not a path segment.

    One predicate for BOTH the template-literal path (``${queryParams}``) and
    the string-concatenation tail (``'/api/v1/pipelines' + query``), so the two
    code paths cannot disagree about what counts as a query string.
    """
    text = _strip_ts_assertion(expression)
    while text.startswith("(") and text.endswith(")"):
        text = text[1:-1].strip()
    if not text:
        return False
    if text.startswith(("?", "&")):
        # A bare ``?``/``&``-prefixed fragment is a query suffix.
        return True
    if text.startswith(("`", "'", '"')):
        # A string/template literal that begins with ``?``/``&`` is a query.
        value, _ = _read_template(text, 0) if text[0] == "`" else _read_quoted(text, 0)
        return value.lstrip().startswith(("?", "&"))
    if _QUERY_OPERATOR_RE.search(text):
        # A ternary is a query suffix ONLY when one of its string branches
        # starts with ``?``/``&`` (``${cond ? '?archived=true' : ''}``). A
        # ternary that builds a PATH SEGMENT
        # (``${role ? 'admins' : 'members'}``) is a ``{param}`` - treating every
        # ``?`` as a query dropped the segment and hid the real route.
        return _ternary_branches_are_query(text)
    match = re.fullmatch(r"(?P<name>[A-Za-z_$][A-Za-z0-9_$]*)(?P<rest>.*)", text, flags=re.DOTALL)
    if match is None:
        return False
    if match.group("name").lower() not in _QUERY_SUFFIX_NAMES:
        return False
    rest = match.group("rest").strip()
    if not rest:
        # Exactly the conventional identifier (``${query}``).
        return True
    # Only a METHOD CALL on that name is a query string. A property read
    # (``params.id``, ``search.id``, ``filters.x``) is a PATH segment: treating
    # it as a query dropped the segment, so
    # ``/api/v1/pipelines/${params.id}/graph`` matched the collection route and
    # reported the real route as stale.
    return bool(_QUERY_SUFFIX_CALL_TAIL_RE.fullmatch(rest) or _QUERY_SUFFIX_DEFAULT_RE.fullmatch(rest))


def _ternary_branches_are_query(expression: str) -> bool:
    """True when a ternary expression's string branches form a query suffix.

    ``cond ? '?page=1' : ''`` switches a query string on/off. A branch that is a
    path fragment (``'admins'``, ``'/users'``) or a non-literal makes the
    expression a path-building ternary instead, so it becomes a ``{param}``.
    """
    for match in re.finditer(r"""(['"])(.*?)\1|`(.*?)`""", expression, flags=re.DOTALL):
        value = match.group(2) if match.group(2) is not None else match.group(3)
        if value.lstrip().startswith(("?", "&")):
            return True
    return False


def _tail_adds_path_segment(tail: str) -> bool:
    """True when a non-literal concatenation tail appends a genuine path segment.

    The tail is the source after the last string/template literal. A tail that
    is only a TypeScript assertion adds no variable, and a tail whose appended
    operands are all conventional query suffixes adds no path segment.
    """
    text = re.sub(r"//[^\n]*|/\*.*?\*/", "", tail, flags=re.DOTALL).strip()
    operands: list[str] = []
    for part in text.split("+"):
        operand = _strip_ts_assertion(part.strip())
        if operand:
            operands.append(operand)
    if not operands:
        return False
    return not all(_is_query_suffix(operand) for operand in operands)


def _unwrap_parens(expression: str) -> str:
    """Strip redundant balanced parentheses around *expression*."""
    text = expression.strip()
    while text.startswith("(") and text.endswith(")"):
        text = text[1:-1].strip()
    return text


def _literal_method(expr: str) -> str | None:
    """Return the HTTP method when *expr* is exactly one quoted method literal.

    A TypeScript assertion on the argument (``'PATCH' as const`` /
    ``'PATCH' as HttpMethod``) still names one literal method, so it is
    stripped before the "nothing but the literal" test rather than dropping the
    call.
    """
    stripped = _unwrap_parens(_strip_ts_assertion(_unwrap_parens(expr)))
    if len(stripped) < 2 or stripped[0] not in ("'", '"'):
        return None
    value, end = _read_quoted(stripped, 0)
    if stripped[end:].strip() or value.upper() not in _HTTP_METHODS:
        return None
    return value.upper()


def _regex_path(expr: str) -> str | None:
    """Return the source of a regex-literal URL argument (``/\\/api\\/v1\\/x/``)."""
    stripped = expr.strip()
    if not stripped.startswith("/") or stripped.startswith("//"):
        return None
    match = re.fullmatch(r"/(.*)/([dgimsuvy]*)", stripped, flags=re.DOTALL)
    if match is None:
        return None
    return match.group(1).replace("\\/", "/")


def _normalise_regex_path(source: str) -> str:
    """Turn a regex-literal path into a matchable path.

    Playwright accepts a RegExp for ``page.route``; the anchored and
    character-class forms (``/\\/api\\/v1\\/plan$/``,
    ``/\\/api\\/v1\\/pipelines\\/([^/]+)$/``) are common. Stripping the anchors
    and collapsing parameter-ish constructs (groups, character classes,
    shorthand classes) to one ``{param}`` segment lets the registration be
    checked exactly like a string-registered route instead of being dropped.
    """
    text = source.strip()
    text = text.removeprefix("^")
    text = text.removeprefix("\\A")
    text = text.removesuffix("$")
    text = text.removesuffix("\\z")
    text = text.removesuffix("\\Z")
    text = text.replace("\\/", "/")
    text = _REGEX_NAMED_GROUP_RE.sub(_WILDCARD, text)
    text = _REGEX_GROUP_RE.sub(_WILDCARD, text)
    text = _REGEX_CHAR_CLASS_RE.sub(_WILDCARD, text)
    text = _REGEX_SHORTHAND_RE.sub(_WILDCARD, text)
    # A trailing ``.*`` / ``.+`` is a prefix glob, not a path segment.
    trailing = re.search(r"\.\s*[*+]\s*$", text)
    if trailing is not None:
        text = text[: trailing.start()] + "*"
    # A bare wildcard that is a whole segment is a path parameter.
    text = re.sub(r"(?<=/)\.(?=/|$)", _WILDCARD, text)
    return _REGEX_ESCAPE_RE.sub(r"\1", text)


def normalise_path(raw: str) -> tuple[str, bool, bool] | None:
    """Normalise a raw path/glob literal.

    Returns ``(path, trailing_glob, last_segment_is_prefix)`` or ``None`` when
    the literal cannot be normalised statically. Template placeholders become a
    single ``{param}`` segment; query strings, fragments and a leading ``**/``
    glob prefix are stripped; a trailing ``*``/``**`` marks the path as a prefix
    match whose last literal segment is itself a prefix (Playwright glob
    semantics, so ``**/api/v1/library*`` covers ``/api/v1/libraries``).
    """
    text = raw.strip()
    if text.startswith(("**/", "**")):
        text = text[2:]
    text = text.split("#", 1)[0].split("?", 1)[0]
    trailing_glob = False
    last_prefix = False
    if text.endswith("**"):
        text = text[:-2]
        trailing_glob = True
        last_prefix = True
    elif text.endswith("*"):
        text = text[:-1]
        trailing_glob = True
        last_prefix = True
    text = _TEMPLATE_EXPR_RE.sub(_WILDCARD, text)
    if "${" in text:
        return None
    text = _COLON_PARAM_RE.sub(_WILDCARD, text)
    text = _SQUASH_SLASH_RE.sub("/", text)
    if len(text) > 1:
        text = text.rstrip("/")
    if not text.startswith("/"):
        # A template whose leading interpolation is the base URL
        # (``${apiBase}/api/v1/pipelines``) yields ``{param}/api/v1/pipelines``.
        # The interpolation precedes the path, so discard everything before the
        # first ``/api/`` segment (falling back to the first ``/``).
        api_index = text.find(_API_PREFIX)
        if api_index > 0:
            text = text[api_index:]
        else:
            slash_index = text.find("/")
            if slash_index <= 0:
                return None
            text = text[slash_index:]
    for segment in text.split("/")[1:]:
        if not _SEGMENT_RE.fullmatch(segment):
            return None
    return text, trailing_glob, last_prefix


def extract_path(expr: str) -> tuple[str, bool, bool] | None:
    """Extract a normalised API path from an arbitrary URL expression.

    Handles quoted literals, template literals (``${...}`` -> ``{param}``),
    string concatenation with identifiers (``apiBase + '/api/v1/org/' + orgId``
    -> a wildcard between the literal fragments) and regex-literal URL args.
    Returns ``None`` for anything without a statically readable ``/api/`` path.
    """
    regex = _regex_path(expr)
    if regex is not None:
        normalised_regex = _normalise_regex_path(regex)
        if _API_PREFIX in normalised_regex:
            return normalise_path(normalised_regex)

    structured = _extract_path_from_concat(expr)
    if structured is not None:
        return structured
    # Fallback for expressions that are not a top-level ``+`` chain (e.g. a
    # path literal inside a factory call): the original fragment scan still
    # finds the ``/api/`` literal.
    return _extract_path_from_fragments(expr)


def _split_top_level_concat(expr: str) -> list[str]:
    """Split *expr* on its top-level ``+`` operators, skipping literals/groups."""
    parts: list[str] = []
    depth = 0
    start = 0
    index = 0
    length = len(expr)
    while index < length:
        char = expr[index]
        if char in ("'", '"'):
            _, index = _read_quoted(expr, index)
            continue
        if char == "`":
            _, index = _read_template(expr, index)
            continue
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        elif char == "+" and depth == 0:
            parts.append(expr[start:index])
            start = index + 1
        index += 1
    parts.append(expr[start:])
    return parts


def _render_concat_operand(operand: str) -> tuple[str, str | None] | None:
    """Render one ``+`` operand as ``("literal", value)`` or ``("segment", None)``.

    A parenthesised ternary that builds a path segment (``(role ? 'admins' :
    'members')``) is ONE segment, not two literals with wildcards between them;
    a ternary that toggles a query string adds nothing. ``None`` means the
    operand contributes no path text (a query suffix or an empty assertion).
    """
    text = _strip_ts_assertion(operand.strip())
    if not text:
        return None
    if text.startswith(("'", '"')):
        value, end = _read_quoted(text, 0)
        if text[end:].strip():
            return None
        return ("literal", value)
    if text.startswith("`"):
        value, end = _read_template(text, 0)
        if text[end:].strip():
            return None
        return ("literal", value)
    inner = _unwrap_parens(text)
    if inner != text:
        return None if _is_query_suffix(inner) else ("segment", None)
    return None if _is_query_suffix(text) else ("segment", None)


def _extract_path_from_concat(expr: str) -> tuple[str, bool, bool] | None:
    """Build the path from a top-level ``+`` chain, one operand at a time."""
    rendered = [item for part in _split_top_level_concat(expr) if (item := _render_concat_operand(part))]
    first_api = next(
        (
            position
            for position, (kind, value) in enumerate(rendered)
            if kind == "literal" and value is not None and _API_PREFIX in value
        ),
        None,
    )
    if first_api is None:
        return None
    joined = rendered[first_api][1] or ""
    for kind, value in rendered[first_api + 1 :]:
        if kind == "literal":
            joined += value or ""
        elif joined.endswith("/"):
            joined += _WILDCARD
        else:
            joined = joined.rstrip("/") + "/" + _WILDCARD
    return normalise_path(joined)


def _extract_path_from_fragments(expr: str) -> tuple[str, bool, bool] | None:
    """Original fragment-scan fallback: join every literal, wildcard the gaps."""
    fragments: list[tuple[str, int, int]] = []
    index = 0
    length = len(expr)
    while index < length:
        char = expr[index]
        if char in ("'", '"'):
            value, end = _read_quoted(expr, index)
            fragments.append((value, index, end))
            index = end
            continue
        if char == "`":
            value, end = _read_template(expr, index)
            fragments.append((value, index, end))
            index = end
            continue
        index += 1
    if not fragments:
        return None

    first_api = next((position for position, (value, _, _) in enumerate(fragments) if _API_PREFIX in value), None)
    if first_api is None:
        return None
    joined = fragments[first_api][0]
    previous_end = fragments[first_api][2]
    for value, start, end in fragments[first_api + 1 :]:
        if expr[previous_end:start].strip():
            joined += _WILDCARD
        joined += value
        previous_end = end
    # A concatenation that ENDS in a genuine path variable
    # (``'/api/v1/pipelines/' + id``) has a non-literal tail; truncating would
    # collide the item route with the collection route, so keep the variable as
    # a trailing path parameter. A query-suffix variable
    # (``'/api/v1/pipelines' + query``), a TypeScript assertion
    # (``'/api/v1/auth/me' as const``) and a literal that already carries a
    # ``?`` add NO segment.
    tail = re.sub(r"//[^\n]*|/\*.*?\*/", "", expr[previous_end:], flags=re.DOTALL)
    if "?" not in joined and _tail_adds_path_segment(tail):
        joined = joined.rstrip("/") + "/" + _WILDCARD
    return normalise_path(joined)


def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _add_note(notes: list[str], filename: str, line: int, message: str) -> None:
    """Record one explicit "not checked" note, deduplicated by its full text."""
    note = f"{filename}:{line}: {message}"
    if note not in notes:
        notes.append(note)


def _has_string_literal(expression: str) -> bool:
    """True when *expression* contains a string or template literal.

    Used to tell a hoisted variable (``page.route(pattern, ...)``) from a
    literal that merely failed to normalise (``page.route('**/*.png', ...)``):
    only the former is an unreadable API registration that must be noted when
    its callback filters on the method.
    """
    return any(quote in expression for quote in ("'", '"', "`"))


def _skip_literal_or_comment(text: str, index: int) -> int | None:
    """Skip a string/template literal or comment at *index*; ``None`` if neither."""
    char = text[index]
    if char in ("'", '"'):
        _, end = _read_quoted(text, index)
        return end
    if char == "`":
        _, end = _read_template(text, index)
        return end
    if char == "/" and index + 1 < len(text):
        if text[index + 1] == "/":
            newline = text.find("\n", index)
            return len(text) if newline < 0 else newline
        if text[index + 1] == "*":
            end = text.find("*/", index + 2)
            return len(text) if end < 0 else end + 2
    return None


def _object_property_spans(text: str) -> tuple[list[tuple[int, int]], int] | None:
    """Split an object literal's OWN (depth-1) properties.

    Returns ``(spans, index_after_closing_brace)`` or ``None`` when *text* is not
    a single object literal. Strings, template literals and comments are skipped,
    so a ``method`` inside any of them is never seen as a property, and a nested
    object's property is inside a deeper brace depth so it is never one of the
    results.
    """
    index = 0
    length = len(text)
    while index < length and text[index].isspace():
        index += 1
    if index >= length or text[index] != "{":
        return None
    body_start = index + 1
    depth = 1
    spans: list[tuple[int, int]] = []
    chunk_start = body_start
    index = body_start
    while index < length:
        skipped = _skip_literal_or_comment(text, index)
        if skipped is not None:
            index = skipped
            continue
        char = text[index]
        if char in "{[(":
            depth += 1
        elif char in "}])":
            depth -= 1
            if depth == 0:
                if text[chunk_start:index].strip():
                    spans.append((chunk_start, index))
                return spans, index + 1
        elif char == "," and depth == 1:
            spans.append((chunk_start, index))
            chunk_start = index + 1
        index += 1
    return None


def _property_parts(chunk: str) -> tuple[str, str, bool]:
    """Split one object property into ``(raw_key, raw_value, has_colon)``."""
    masked = mask_non_code(chunk)
    depth = 0
    for index, char in enumerate(masked):
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        elif char == ":" and depth == 0:
            return chunk[:index].strip(), chunk[index + 1 :].strip(), True
    return chunk.strip(), "", False


def _property_key(raw_key: str) -> str | None:
    """Return an object property's key name, or ``None`` when it is not static."""
    key = raw_key.strip()
    if not key:
        return None
    if key[0] in ("'", '"'):
        value, end = _read_quoted(key, 0)
        return value if not key[end:].strip() else None
    return key if re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_$]*", key) else None


def _init_method(text: str) -> tuple[str | None, bool, bool]:
    """Read the method from ONE init argument.

    Returns ``(method, unresolved, present)``. *present* is True when a
    top-level ``method`` property exists (literal or not), so an init object that
    merely omits it does not shadow a ``Request``'s own init.

    Only a depth-1 property can be the request method: ``{ body: JSON.stringify({
    method: 'DELETE' }) }`` is an implicit GET, not a DELETE, and a ``method``
    inside a comment is not a property at all. The FIRST top-level ``method``
    property wins, so property order does not change the result.
    """
    stripped = text.strip()
    parsed = _object_property_spans(stripped)
    if parsed is None:
        return None, True, False
    spans, after = parsed
    trailing = stripped[after:].strip()
    if trailing and not _TS_OBJECT_ASSERTION_RE.match(trailing):
        return None, True, False
    for start, end in spans:
        chunk = stripped[start:end].strip()
        if not chunk:
            continue
        if chunk.startswith("..."):
            # A spread may supply ``method``; the init is not readable.
            return None, True, False
        raw_key, raw_value, has_colon = _property_parts(chunk)
        if _property_key(raw_key) != "method":
            continue
        if not has_colon:
            # ``{ method }`` shorthand: a value exists but is not literal.
            return None, True, True
        value = _literal_method(raw_value)
        if value is None:
            return None, True, True
        return value, False, True
    return None, False, False


def _fetch_method(init_arguments: list[str]) -> tuple[str | None, bool]:
    """Resolve a ``fetch`` init's method, or report that it is unreadable.

    Returns ``(method, unresolved)``. No init, or an object literal without a
    ``method`` key, is an implicit GET - that is a real request the guard must
    check. A ``method`` key whose value is not a single string literal - the
    ``{ method }`` shorthand, a quoted key, an expression - a spread that may
    supply one, or a non-literal init object cannot be resolved statically, so
    the caller notes it rather than assuming GET.
    """
    for argument in init_arguments:
        text = argument.strip()
        if not text:
            continue
        method, unresolved, present = _init_method(text)
        if unresolved:
            return None, True
        if present and method is not None:
            return method, False
    return None, False


def _has_unmodelled_method_reference(masked_body: str) -> bool:
    """True when a route callback reads the request method in a form we cannot attribute.

    The comparison passes resolve ``x.method() === '<METHOD>'`` and its
    siblings. Anything else that still reads the method - a destructured
    ``const { method } = route.request()``, an ``['PATCH', 'PUT'].includes(...)``
    array test - selects a branch this checker cannot map to one literal
    method, so the caller emits a note rather than dropping it silently.
    """
    return bool(_METHOD_CALL_RE.search(masked_body) or _DESTRUCTURED_METHOD_RE.search(masked_body))


def _args_of(text: str, masked: str, open_paren: int) -> list[str]:
    """Return the original source text of each top-level argument of a call."""
    return [text[start:end] for start, end in _split_args(masked, open_paren)]


def _enclosing_if_condition(text: str, masked: str, offset: int) -> str | None:
    """Return the innermost ``if (...)`` condition text containing *offset*."""
    best: tuple[int, str] | None = None
    for match in _IF_RE.finditer(masked, 0, offset + 1):
        open_paren = match.end() - 1
        spans = _split_args(masked, open_paren)
        if not spans:
            continue
        close = spans[-1][1]
        if match.start() <= offset <= close and (best is None or close - match.start() < best[0]):
            best = (close - match.start(), text[open_paren + 1 : close])
    return best[1] if best else None


def _callback_params(handler: str) -> list[str]:
    """Return an inline callback's parameter names (``[]`` if not inline).

    Handles ``(route) =>``, ``async (route, request) =>``, typed params
    (``(route: Route, request: Request) =>``), ``route =>``,
    ``async function (route)`` and renamed parameters such as ``(r) =>``.

    Both Playwright ``page.route`` parameters matter: the first is the route,
    the second is the request, and either may carry the method filter.
    """
    parenthesised = re.search(
        r"(?:async\s+)?(?:function\s*\w*\s*)?\(([^()]*)\)\s*(?::[^=]*)?(?:=>|\{)",
        handler,
    )
    if parenthesised is None:
        parenthesised = re.search(r"function\s*(?:\w+\s*)?\(([^()]*)\)", handler)
    if parenthesised is not None:
        names: list[str] = []
        for piece in parenthesised.group(1).split(","):
            name = piece.split(":", 1)[0].split("=", 1)[0].strip()
            if re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_$]*", name):
                names.append(name)
        if names:
            return names
    bare = re.search(r"(?:async\s+)?([A-Za-z_$][A-Za-z0-9_$]*)\s*=>", handler)
    if bare is not None:
        return [bare.group(1)]
    return []


def _method_comparison_re(operand: str) -> re.Pattern[str]:
    """Return the regex for ``<operand> === '<METHOD>'`` and its reverse."""
    escaped = re.escape(operand)
    return re.compile(
        rf"(?<![\w.$]){escaped}\s*\.\s*request\s*\(\s*\)\s*\.\s*method\s*\(\s*\)\s*===?\s*(['\"])([A-Za-z]+)\1"
        rf"|(['\"])([A-Za-z]+)\3\s*===\s*{escaped}\s*\.\s*request\s*\(\s*\)\s*\.\s*method\s*\(\s*\)"
    )


def _variable_comparison_re(variable: str) -> re.Pattern[str]:
    """Return the regex for ``<variable> === '<METHOD>'`` and its reverse."""
    escaped = re.escape(variable)
    return re.compile(
        rf"(?<![\w.$]){escaped}\s*(?:===?)\s*(['\"])([A-Za-z]+)\1"
        rf"|(['\"])([A-Za-z]+)\3\s*(?:===?)\s*{escaped}(?![\w$])"
    )


def _request_method_comparison_re(operand: str) -> re.Pattern[str]:
    """Return the regex for ``<requestObject>.method() === '<METHOD>'`` and its reverse.

    The second ``page.route`` callback parameter is the Request itself, so its
    method is read directly (``request.method()``) rather than through
    ``request().method()`` on the route.
    """
    escaped = re.escape(operand)
    return re.compile(
        rf"(?<![\w.$]){escaped}\s*\.\s*method\s*\(\s*\)\s*===?\s*(['\"])([A-Za-z]+)\1"
        rf"|(['\"])([A-Za-z]+)\3\s*===\s*{escaped}\s*\.\s*method\s*\(\s*\)"
    )


def _boolean_assignment_res(receiver: str) -> list[re.Pattern[str]]:
    """Return regexes for ``const <var> = <receiver>.method() === '<METHOD>'``.

    Both operand orders are covered. The variable is group 1 and the method
    literal is group 3. The receiver is either the route (``route.request()``)
    or the request object (``request``), so the ``.request()`` hop is optional.
    """
    escaped = re.escape(receiver)
    call = rf"(?<![\w.$]){escaped}(?:\s*\.\s*request\s*\(\s*\))?\s*\.\s*method\s*\(\s*\)"
    prefix = r"(?:const|let|var)\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*"
    return [
        re.compile(prefix + call + r"\s*===?\s*(['\"])([A-Za-z]+)\2"),
        re.compile(prefix + r"(['\"])([A-Za-z]+)\2\s*===\s*" + call + r"(?![\w$])"),
    ]


def _match_is_code(text: str, masked: str, match: re.Match[str], token: str) -> bool:
    """True when *token* occurs as code inside *match* (not in a comment/string).

    The method literal is a string, so it is blanked in ``masked``. Comparing
    the start offset is wrong for the reversed form (it starts at the opening
    quote); probe the receiver identifier instead.
    """
    for probe in re.finditer(rf"(?<![\w.$]){re.escape(token)}", text[match.start() : match.end()]):
        if masked[match.start() + probe.start()] != " ":
            return True
    return False


def _resolved_method_comparisons(
    text: str, masked: str, start: int, end: int, operand: str, comparison_re: re.Pattern[str]
) -> list[re.Match[str]]:
    """Return the in-scope comparison matches for *operand* inside ``[start, end]``."""
    matches: list[re.Match[str]] = []
    for match in comparison_re.finditer(text, start, end):
        if not _match_is_code(text, masked, match, operand):
            continue
        matches.append(match)
    return matches


def _extract_route_interceptors(
    text: str, masked: str, filename: str, notes: list[str] | None = None
) -> list[HttpCall]:
    """Extract positive method filters from ``page.route`` callbacks.

    Only positive filters (``===``/``==``) are checked; ``!==`` compares a
    fallback branch and asserts nothing about which methods the route serves,
    so it is deliberately skipped - but it still leaves a note, because its
    method reference is otherwise unattributable. A comparison whose enclosing ``if`` also
    tests the request URL (``url.includes(...)``, ``url.endsWith(...)``) is
    skipped too: the effective path lives in that condition, not in the
    registration, so it cannot be read statically here.

    Resolvable shapes: the inline receiver (``route.request().method()``,
    including a renamed callback parameter and the reversed operand order), the
    second callback parameter (the Request: ``async (route, request) =>
    request.method()``), a local variable holding the request object
    (``const request = route.request()``), and a local variable holding the
    method result (``const m = r.request().method(); if (m === 'PATCH')``). A
    registration whose handler is a bare identifier, or whose API path cannot
    be normalised statically, is reported as an explicit note rather than
    silently skipped.

    A callback that reads the method in a form the comparison passes cannot
    attribute to one literal method - a destructured
    ``const { method } = route.request()``, an
    ``['PATCH', 'PUT'].includes(route.request().method())`` array test, or a
    ``!==`` fallback guard - produces one note for that registration instead of
    vanishing silently.
    """
    notes = notes if notes is not None else []
    calls: list[HttpCall] = []
    registrations: list[tuple[str, int, int, int, int, tuple[str, ...]]] = []
    for match in _ROUTE_CALL_RE.finditer(masked):
        open_paren = match.end() - 1
        args = [span for span in _split_args(masked, open_paren) if text[span[0] : span[1]].strip()]
        if not args:
            continue
        first = text[args[0][0] : args[0][1]].strip()
        path_info = extract_path(first)
        if path_info is None:
            # Not a statically-readable path. An API-shaped registration that
            # failed normalisation is noted (never silently dropped); a
            # non-API mock (``**/*.png``) has nothing specific to validate.
            regex_source = _regex_path(first)
            api_shaped = _API_PREFIX in first or (
                regex_source is not None and _API_PREFIX in _normalise_regex_path(regex_source)
            )
            handler_masked = masked[args[-1][0] : args[-1][1]]
            if not _has_string_literal(first) and _has_unmodelled_method_reference(handler_masked):
                # A hoisted URL variable (``const pattern = '**/api/...'``;
                # ``page.route(pattern, ...)``) carries the real path, and the
                # callback still filters on the method. Skipping it means the
                # filter is never checked - so it is never silent.
                _add_note(
                    notes,
                    filename,
                    _line_of(text, match.start()),
                    "page.route registration URL is a non-literal expression, so its method filter "
                    "cannot be checked against a single route",
                )
            elif api_shaped:
                _add_note(
                    notes,
                    filename,
                    _line_of(text, match.start()),
                    "page.route registration uses an API path that could not be normalised statically; "
                    "its method filter was not checked",
                )
            continue
        path, trailing_glob, last_prefix = path_info
        if trailing_glob and path in ("/api", "/api/v1"):
            # A catch-all API mock (``**/api/v1/**``) carries no specific route
            # to validate - resolving its fallback branch would invent one. It
            # is silent only when it has no method filter; a catch-all that
            # mocks specific routes by URL and filters on the method CAN hide a
            # stale one, so that shape is noted.
            handler_masked = masked[args[-1][0] : args[-1][1]]
            if _has_unmodelled_method_reference(handler_masked):
                _add_note(
                    notes,
                    filename,
                    _line_of(text, match.start()),
                    "page.route catch-all registration mocks specific routes by URL and filters on the HTTP "
                    "method; its method filters cannot be checked against a single route",
                )
            continue
        handler = text[args[-1][0] : args[-1][1]].strip()
        params = _callback_params(handler)
        if not params:
            notes.append(
                f"{filename}:{_line_of(text, match.start())}: page.route handler is not an inline "
                f"function; its method filter was not checked"
            )
            continue
        registrations.append((path, int(trailing_glob), int(last_prefix), match.start(), args[-1][1], tuple(params)))

    seen: set[tuple[int, str, str]] = set()
    for path, trailing_glob, last_prefix, span_start, span_end, params in registrations:
        body = text[span_start:span_end]
        route_parameter = params[0]
        # The Request object: Playwright passes it as the callback's second
        # parameter, and it is also commonly hoisted from ``route.request()``.
        request_objects: list[str] = list(params[1:])
        param_alternation = "|".join(re.escape(name) for name in dict.fromkeys(params))
        request_objects.extend(
            assignment.group(1)
            for assignment in re.finditer(
                rf"(?:const|let|var)\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*"
                rf"(?<![\w.$])(?:{param_alternation})\s*\.\s*request\s*\(\s*\)",
                body,
            )
        )
        request_objects = list(dict.fromkeys(request_objects))

        # ``(match, line_override)``: a method captured into a variable is
        # attributed to the REFERENCE line (where ``.method()`` is read), not
        # the later comparison line, so the coverage ledger sees that exact
        # candidate line accounted for.
        resolved: list[tuple[re.Match[str], int | None]] = [
            (match, None)
            for match in _resolved_method_comparisons(
                text, masked, span_start, span_end, route_parameter, _method_comparison_re(route_parameter)
            )
        ]
        for request_object in request_objects:
            resolved.extend(
                (
                    match,
                    None,
                )
                for match in _resolved_method_comparisons(
                    text,
                    masked,
                    span_start,
                    span_end,
                    request_object,
                    _request_method_comparison_re(request_object),
                )
            )

        # A variable holding the request method, resolved within this handler.
        receivers = list(dict.fromkeys([route_parameter, *request_objects]))
        receiver_alternation = "|".join(re.escape(name) for name in receivers)
        for assignment in re.finditer(
            rf"(?:const|let|var)\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*"
            rf"(?<![\w.$])(?:{receiver_alternation})(?:\s*\.\s*request\s*\(\s*\))?\s*\.\s*method\s*\(\s*\)",
            body,
        ):
            variable = assignment.group(1)
            reference_line = _line_of(text, span_start + assignment.start())
            resolved.extend(
                (match, reference_line)
                for match in _resolved_method_comparisons(
                    text, masked, span_start, span_end, variable, _variable_comparison_re(variable)
                )
            )

        for match, line_override in resolved:
            method = match.group(2) or match.group(4)
            if method is None:
                continue
            normalised_method = method.upper()
            if normalised_method not in _HTTP_METHODS:
                _add_note(
                    notes,
                    filename,
                    _line_of(text, match.start()),
                    f"interceptor method filter names {method!r}, which is not an HTTP method this check models",
                )
                continue
            condition = _enclosing_if_condition(text, masked, match.start())
            if condition is None:
                # A comparison assigned to a boolean variable is resolved by the
                # dedicated pass below; anything else outside an if/ternary is
                # not a branch selector this checker can attribute.
                if re.search(
                    r"(?:const|let|var)\s+[A-Za-z_$][A-Za-z0-9_$]*\s*=\s*$",
                    text[span_start : match.start()],
                ):
                    continue
                _add_note(
                    notes,
                    filename,
                    _line_of(text, match.start()),
                    "interceptor method comparison is not inside an if/ternary branch, so it selects no "
                    "route branch this check can attribute",
                )
                continue
            if _URL_CONDITION_RE.search(condition):
                # The effective path lives in that condition, not in the
                # registration - report the skip rather than guess a route.
                _add_note(
                    notes,
                    filename,
                    _line_of(text, match.start()),
                    "interceptor method filter is combined with an in-callback URL test, so its effective "
                    "route cannot be attributed to the registration",
                )
                continue
            descriptor_line = line_override if line_override is not None else _line_of(text, match.start())
            key = (descriptor_line, normalised_method, path)
            if key in seen:
                continue
            seen.add(key)
            calls.append(
                HttpCall(
                    filename=filename,
                    line=key[0],
                    method=normalised_method,
                    path=path,
                    kind="interceptor",
                    trailing_glob=bool(trailing_glob),
                    last_segment_prefix=bool(last_prefix),
                )
            )

        # A boolean variable holding a positive method comparison:
        # ``const isPatch = route.request().method() === 'PATCH'; if (isPatch)``.
        # The comparison itself is not inside an ``if``, so the pass above
        # leaves it alone; resolve the branch that tests the variable.
        for receiver in receivers:
            for assignment_re in _boolean_assignment_res(receiver):
                for assignment in assignment_re.finditer(body):
                    variable = assignment.group(1)
                    method = assignment.group(3)
                    if method is None:
                        continue
                    normalised_method = method.upper()
                    assignment_line = _line_of(text, span_start + assignment.start())
                    if normalised_method not in _HTTP_METHODS:
                        _add_note(
                            notes,
                            filename,
                            assignment_line,
                            f"interceptor method filter names {method!r}, which is not an HTTP method "
                            f"this check models",
                        )
                        continue
                    usage_re = re.compile(rf"(?<![\w$.]){re.escape(variable)}(?![\w$])")
                    for usage in usage_re.finditer(masked, span_start + assignment.end(), span_end):
                        if re.search(r"!\s*$", masked[max(span_start, usage.start() - 2) : usage.start()]):
                            continue  # negated: guards the fallback branch, like ``!==``
                        condition = _enclosing_if_condition(text, masked, usage.start())
                        if condition is not None:
                            if _URL_CONDITION_RE.search(condition):
                                _add_note(
                                    notes,
                                    filename,
                                    _line_of(text, usage.start()),
                                    "interceptor method filter is combined with an in-callback URL test, so "
                                    "its effective route cannot be attributed to the registration",
                                )
                                break
                        elif not re.match(r"\s*\?", masked[usage.end() : span_end]):
                            continue
                        key = (assignment_line, normalised_method, path)
                        if key in seen:
                            break
                        seen.add(key)
                        calls.append(
                            HttpCall(
                                filename=filename,
                                line=assignment_line,
                                method=normalised_method,
                                path=path,
                                kind="interceptor",
                                trailing_glob=bool(trailing_glob),
                                last_segment_prefix=bool(last_prefix),
                            )
                        )
                        break

        # ``_has_unmodelled_method_reference`` cannot see a destructured method
        # (``const { method } = route.request()``) because there is no
        # ``.method()`` call to match. It is a real filter the comparison passes
        # cannot attribute, so it is noted even when the SAME callback also
        # yields a descriptor for another comparison - a descriptor elsewhere in
        # the callback does not make this shape checked.
        if _DESTRUCTURED_METHOD_RE.search(masked[span_start:span_end]):
            _add_note(notes, filename, _line_of(text, span_start), _LEDGER_UNMODELLED_METHOD_NOTE)
    return calls


# ---------------------------------------------------------------------------
# Coverage ledger - prove every candidate line was analysed
# ---------------------------------------------------------------------------


def _is_function_declaration(masked: str, offset: int) -> bool:
    """True when the identifier at *offset* is being declared (``function foo``)."""
    return bool(re.search(r"(?:\basync\s+)?\bfunction\s*$", masked[max(0, offset - 40) : offset]))


def _is_api_fetch_declaration(masked: str, offset: int) -> bool:
    """True when the ``apiFetch`` identifier at *offset* declares the helper."""
    return _is_function_declaration(masked, offset)


def _unwrap_fetch_target(expression: str) -> tuple[str, list[str]]:
    """Resolve a ``fetch`` target to ``(path_expression, request_init_arguments)``.

    ``fetch`` accepts a string, a ``URL`` or a ``Request``. A ``new URL(<path>,
    <base>)`` target contributes only its first argument as the path; a ``new
    Request(<path>, <init>)`` target contributes BOTH the path and the init, so
    its ``method`` is read from the Request rather than assumed to be an
    implicit GET. A constructor may WRAP another constructor
    (``fetch(new Request(new URL(path, base), { method }))``), so unwrapping is
    recursive until neither ``URL`` nor ``Request`` remains.

    Inits accumulate outermost-first: an outer ``fetch`` init overrides the
    outermost ``Request`` init, which overrides an inner one. The fragment
    scanner used to splice the constructors' remaining arguments into the path
    (``/api/v1/auth/login/{param}``) - a false stale violation.
    """
    text = expression.strip()
    request_init_arguments: list[str] = []
    while True:
        masked = mask_non_code(text)
        match = _NEW_FETCH_TARGET_RE.match(masked)
        if match is None:
            break
        spans = _split_args(masked, match.end() - 1)
        arguments = [text[start:end].strip() for start, end in spans if text[start:end].strip()]
        # The guard protects against a pathological self-referential expression
        # (`new Request(` whose first argument is the same text) looping forever.
        if not arguments or arguments[0] == text:
            break
        if match.group(1) == "Request":
            request_init_arguments.extend(arguments[1:])
        text = arguments[0]
    return text, request_init_arguments


def _is_fetch_value_reference(masked: str, start: int, end: int) -> bool:
    """True when a ``fetch`` identifier here is READ as a value (an alias).

    ``const f = window.fetch``, ``const f = fetch.bind(globalThis)`` and
    ``fetch.call(...)`` cannot be followed statically, so they are candidate
    occurrences that force an explicit annotation. Two shapes are NOT value
    reads and are excluded: an assignment TARGET (``globalThis.fetch = ...``,
    which writes a replacement) and a ``typeof`` type reference
    (``as typeof globalThis.fetch``).
    """
    previous = masked[:start].rstrip()
    if re.search(r"(?<![\w$])typeof$", previous):
        return False
    index = end
    while index < len(masked) and masked[index] in " \t\r\n":
        index += 1
    if index < len(masked) and masked[index] == "=":
        following = masked[index + 1] if index + 1 < len(masked) else ""
        if following not in {"=", ">"}:
            return False
    return True


def _call_candidates(source: str, masked: str) -> list[tuple[int, int]]:
    """Return ``(offset, line)`` for every HTTP-call CANDIDATE occurrence.

    Enumerated with the SAME regexes the extractor uses, so the ledger counts
    each occurrence independently - an unmodelled call can no longer be
    absorbed by a modelled call on the same line (the ``Promise.all([fetch(a),
    fetch(b, init)])`` shape).

    Deliberately broader than the extractor on three shapes that used to be
    silent ledger gaps:

    * a ``fetch`` identifier READ as a value - the alias capture
      (``const f = window.fetch``, ``= fetch.bind(...)``) and the destructured
      binding (``const { fetch: f } = globalThis``). Resolving the alias is not
      this checker's job; the unanalysed occurrence forces an annotation.
    * bracket access to an APIRequestContext (``page.request['post'](...)``).
    * a ``request`` binding introduced by destructuring/rename
      (``async ({ request: api }) => api.get(...)``).

    A genuine call occurrence is accounted for by extraction; the alias/binding
    forms are never modelled, so they always surface unless annotated. A
    ``function apiFetch(...)`` / ``function fetchWithTransientRetry(...)``
    declaration is excluded structurally, exactly as extraction excludes it.
    """
    occurrences: list[tuple[int, int]] = []
    seen: set[int] = set()

    def add(offset: int) -> None:
        if offset in seen:
            return
        seen.add(offset)
        occurrences.append((offset, _line_of(source, offset)))

    for match in _API_FETCH_RE.finditer(masked):
        if _is_api_fetch_declaration(masked, match.start()):
            continue
        if _find_open_paren(masked, match.end()) is None:
            continue
        add(match.start())
    for match in _RETRY_FETCH_RE.finditer(masked):
        if _is_function_declaration(masked, match.start()):
            continue
        if _find_open_paren(masked, match.end()) is None:
            continue
        add(match.start())
    for match in _FETCH_RE.finditer(masked):
        if _is_function_declaration(masked, match.start()):
            continue
        is_call = _find_open_paren(masked, match.end()) is not None
        if is_call or _is_fetch_value_reference(masked, match.start(), match.end()):
            add(match.start())
    for match in _API_REQUEST_RE.finditer(masked):
        if _find_open_paren(masked, match.end()) is None:
            continue
        add(match.start())
    for match in _LEDGER_BRACKET_REQUEST_RE.finditer(masked):
        add(match.start())
    for match in _LEDGER_REQUEST_ALIAS_RE.finditer(masked):
        # A PascalCase name after ``request:`` is a TYPE annotation
        # (``(route, request: Request)``), not a renamed binding.
        if match.group("alias")[:1].isupper():
            continue
        add(match.start())
    return occurrences


def _method_ref_lines(source: str, masked: str) -> set[int]:
    """Return every line that references an HTTP method without naming a route.

    Method references are accounted per line: a descriptor or note emitted for
    the line covers it, and an unaccounted one is noted (never dropped).
    """
    lines: set[int] = set()
    raw_lines = source.splitlines()
    for index, masked_line in enumerate(masked.splitlines()):
        line = index + 1
        if _LEDGER_METHOD_CALL_RE.search(masked_line):
            lines.add(line)
            continue
        raw_line = raw_lines[index] if index < len(raw_lines) else ""
        if (
            _HTTP_METHOD_LITERAL_RE.search(raw_line)
            and _LEDGER_METHOD_WORD_RE.search(masked_line)
            and _LEDGER_COMPARISON_RE.search(masked_line)
        ):
            lines.add(line)
    return lines


def _note_line_numbers(filename: str, notes: list[str]) -> set[int]:
    """Return the line numbers of the notes emitted for *filename*."""
    lines: set[int] = set()
    prefix = f"{filename}:"
    for note in notes:
        if not note.startswith(prefix):
            continue
        number, separator, _ = note[len(prefix) :].partition(":")
        if separator and number.isdigit():
            lines.add(int(number))
    return lines


def _apply_coverage_ledger(
    source: str,
    masked: str,
    filename: str,
    calls: list[HttpCall],
    notes: list[str],
    unanalysed: list[str] | None,
    accounted_offsets: set[int],
) -> None:
    """Account for every candidate occurrence, or report it.

    Call candidates are accounted PER OCCURRENCE: *accounted_offsets* is the
    set of call-marker offsets extraction resolved into a descriptor or an
    explicit note, so a second, unmodelled call on the same line cannot be
    absorbed by the first. An unaccounted call occurrence is appended to
    *unanalysed* (the CLI turns those into exit 1). An unaccounted method
    reference is only ever a note - it names no route this checker can validate,
    but it is never dropped. When no *unanalysed* sink is supplied (a direct
    library call), the violation is downgraded to a note so it still cannot
    vanish silently.
    """
    call_occurrences = _call_candidates(source, masked)
    method_lines = _method_ref_lines(source, masked)
    if not call_occurrences and not method_lines:
        return
    accounted_lines = {call.line for call in calls}
    accounted_lines |= _note_line_numbers(filename, notes)
    raw_lines = source.splitlines()
    suppressed_lines: set[int] = set()
    for line in {line for _, line in call_occurrences} | method_lines:
        current = raw_lines[line - 1] if line - 1 < len(raw_lines) else ""
        previous = raw_lines[line - 2] if line >= 2 else ""
        if _LEDGER_IGNORE_RE.search(current) or _LEDGER_IGNORE_RE.search(previous):
            suppressed_lines.add(line)
    for offset, line in call_occurrences:
        if offset in accounted_offsets or line in suppressed_lines:
            continue
        message = f"{filename}:{line}: {_UNANALYSED_CALL_MESSAGE}"
        if unanalysed is not None:
            unanalysed.append(message)
        else:
            _add_note(notes, filename, line, _UNANALYSED_CALL_MESSAGE)
    for line in sorted(method_lines):
        if line in accounted_lines or line in suppressed_lines:
            continue
        _add_note(notes, filename, line, _LEDGER_UNMODELLED_METHOD_NOTE)


def _extract_fetch_like(
    source: str,
    masked: str,
    filename: str,
    notes: list[str],
    accounted_offsets: set[int],
    pattern: re.Pattern[str],
    label: str,
    *,
    skip_declaration: bool,
    note_unresolved_always: bool,
) -> list[HttpCall]:
    """Extract ``fetch``-shaped calls (``fetch`` and the transport helper).

    *label* names the callee in notes (``fetch`` / ``fetchWithTransientRetry``).
    The target is unwrapped through ``new URL`` / ``new Request`` first, so the
    path and a Request's own ``method`` are read from the constructor arguments.
    Every occurrence that becomes a descriptor or a note registers its offset in
    *accounted_offsets*, which is what makes the ledger per-occurrence.
    """
    calls: list[HttpCall] = []
    for match in pattern.finditer(masked):
        if skip_declaration and _is_function_declaration(masked, match.start()):
            continue
        open_paren = _find_open_paren(masked, match.end())
        if open_paren is None:
            continue
        offset = match.start()
        line = _line_of(source, offset)
        args = _args_of(source, masked, open_paren)
        if not args:
            continue
        target, request_init_arguments = _unwrap_fetch_target(args[0])
        # An outer init overrides a Request's own init, so it is read first.
        method, unresolved = _fetch_method(args[1:] + request_init_arguments)
        path_info = extract_path(target)
        if path_info is None:
            if method is not None:
                _add_note(
                    notes,
                    filename,
                    line,
                    f"{label} call has a resolvable method but a non-literal URL argument, "
                    f"so its route was not checked",
                )
                accounted_offsets.add(offset)
            elif _API_PREFIX in target:
                _add_note(
                    notes,
                    filename,
                    line,
                    f"{label} URL argument names an API path but could not be normalised statically",
                )
                accounted_offsets.add(offset)
            elif note_unresolved_always:
                _add_note(
                    notes,
                    filename,
                    line,
                    f"{label} call could not be fully resolved: its URL is not a statically readable "
                    f"API path and its method is unknown; its call sites are checked separately",
                )
                accounted_offsets.add(offset)
            continue
        if unresolved:
            if _API_PREFIX in target or note_unresolved_always:
                _add_note(
                    notes,
                    filename,
                    line,
                    f"{label} init does not expose a single readable method option, so the request method is unknown",
                )
                accounted_offsets.add(offset)
            continue
        path, trailing_glob, last_prefix = path_info
        calls.append(
            HttpCall(
                filename=filename,
                line=line,
                method=method or "GET",  # no method option means an implicit GET
                path=path,
                kind="fetch",
                trailing_glob=trailing_glob,
                last_segment_prefix=last_prefix,
            )
        )
        accounted_offsets.add(offset)
    return calls


def extract_http_calls(
    source: str,
    filename: str,
    notes: list[str] | None = None,
    unanalysed: list[str] | None = None,
) -> list[HttpCall]:
    """Extract every statically-readable E2E HTTP call descriptor from *source*.

    *notes* (when given) collects explicit explanations for shapes the checker
    cannot resolve safely, so a skip is visible rather than silent. *unanalysed*
    (when given) collects coverage-ledger violations - candidate call
    occurrences with no descriptor, note or suppression annotation.
    """
    notes = notes if notes is not None else []
    masked = mask_non_code(source)
    calls: list[HttpCall] = []
    accounted_offsets: set[int] = set()

    for match in _API_FETCH_RE.finditer(masked):
        # A function DECLARATION (``export async function apiFetch<T>(...)``)
        # is the helper itself, not a call: its typed parameters are not a
        # method/path pair and must not be noted as an unresolved request.
        if _is_api_fetch_declaration(masked, match.start()):
            continue
        open_paren = _find_open_paren(masked, match.end())
        if open_paren is None:
            continue
        offset = match.start()
        line = _line_of(source, offset)
        args = _args_of(source, masked, open_paren)
        if len(args) < 4:
            # ``apiFetch`` is inherently an API call, so a call shape this
            # checker cannot read is noted regardless of whether any argument
            # happens to spell ``/api/`` (a hoisted variable does not).
            _add_note(
                notes,
                filename,
                line,
                "apiFetch call has fewer than four arguments, so its method and path were not checked",
            )
            accounted_offsets.add(offset)
            continue
        method = _literal_method(args[2])
        path_info = extract_path(args[3])
        if method is not None and path_info is None:
            # ``apiFetch(b, t, 'DELETE', path)`` names a real request, so its
            # route must not disappear silently just because the path is a
            # variable rather than a literal.
            _add_note(
                notes,
                filename,
                line,
                "apiFetch call has a resolvable method but a non-literal path argument, so its route was not checked",
            )
            accounted_offsets.add(offset)
            continue
        if method is None or path_info is None:
            # ``apiFetch`` IS the API call - both arguments may be hoisted
            # variables (``apiFetch(b, t, verb, endpoint)``), so a ``/api/``
            # literal is not a precondition for reporting the skip.
            reasons: list[str] = []
            if method is None:
                reasons.append("the method argument is not a single string-literal HTTP verb")
            if path_info is None:
                reasons.append("the path argument could not be normalised to an API path")
            _add_note(
                notes,
                filename,
                line,
                "apiFetch call could not be fully resolved: " + "; ".join(reasons),
            )
            accounted_offsets.add(offset)
            continue
        path, trailing_glob, last_prefix = path_info
        calls.append(
            HttpCall(
                filename=filename,
                line=line,
                method=method,
                path=path,
                kind="apiFetch",
                trailing_glob=trailing_glob,
                last_segment_prefix=last_prefix,
            )
        )
        accounted_offsets.add(offset)

    calls.extend(
        _extract_fetch_like(
            source,
            masked,
            filename,
            notes,
            accounted_offsets,
            _FETCH_RE,
            "fetch",
            skip_declaration=False,
            note_unresolved_always=False,
        )
    )
    calls.extend(
        _extract_fetch_like(
            source,
            masked,
            filename,
            notes,
            accounted_offsets,
            _RETRY_FETCH_RE,
            "fetchWithTransientRetry",
            skip_declaration=True,
            note_unresolved_always=True,
        )
    )

    for match in _API_REQUEST_RE.finditer(masked):
        open_paren = _find_open_paren(masked, match.end())
        if open_paren is None:
            continue
        offset = match.start()
        args = _args_of(source, masked, open_paren)
        if not args:
            continue
        method = match.group(1).upper()
        if method not in _HTTP_METHODS:
            continue
        path_info = extract_path(args[0])
        if path_info is None:
            # The verb IS the method, so the method is always resolvable here -
            # a non-literal URL is an unchecked request, never a silent skip.
            _add_note(
                notes,
                filename,
                _line_of(source, offset),
                "page.request/request verb call has a resolvable method but a non-literal URL "
                "argument, so its route was not checked",
            )
            accounted_offsets.add(offset)
            continue
        path, trailing_glob, last_prefix = path_info
        calls.append(
            HttpCall(
                filename=filename,
                line=_line_of(source, offset),
                method=method,
                path=path,
                kind="request",
                trailing_glob=trailing_glob,
                last_segment_prefix=last_prefix,
            )
        )
        accounted_offsets.add(offset)

    calls.extend(_extract_route_interceptors(source, masked, filename, notes))
    calls.sort(key=lambda call: (call.line, call.method, call.path))
    _apply_coverage_ledger(source, masked, filename, calls, notes, unanalysed, accounted_offsets)
    return calls


# ---------------------------------------------------------------------------
# Live route table and matching
# ---------------------------------------------------------------------------


def _split_segments(path: str) -> tuple[str, ...]:
    return tuple(segment for segment in path.split("/") if segment)


def _normalise_template(template: str) -> str:
    """Normalise a route template's parameters into the matcher's vocabulary.

    ``{param:path}`` is preserved (as ``{param:path}``) because it consumes one
    or more path segments; every other parameter form collapses to the single
    segment ``{param}``.
    """
    with_path_converters = _PATH_CONVERTER_RE.sub(_MULTI_WILDCARD, template)
    return _TEMPLATE_EXPR_RE.sub(_WILDCARD, _COLON_PARAM_RE.sub(_WILDCARD, with_path_converters))


def build_live_routes(openapi_paths: dict[str, Any]) -> list[LiveRoute]:
    """Build the normalised live route table from a ``path -> operations`` map.

    Kept as the OpenAPI-shaped entry point for callers (and tests) that supply
    their own table; the real tree is built from the app's route objects by
    :func:`build_live_routes_from_routes`.
    """
    routes: list[LiveRoute] = []
    for template, operations in openapi_paths.items():
        normalised = _normalise_template(template)
        methods = frozenset(method.upper() for method in operations if method.upper() in _HTTP_METHODS)
        routes.append(LiveRoute(template=normalised, segments=_split_segments(normalised), methods=methods))
    return routes


def _iter_route_objects(routes: Any) -> Any:
    """Yield the leaf route objects from a FastAPI/Starlette route list.

    FastAPI 0.146 wraps each ``include_router`` in a lazy ``_IncludedRouter``
    whose inner routes live on ``original_router.routes`` (already prefixed).
    Older versions flatten the list, in which case every entry is a leaf. A
    ``Mount`` is skipped: its siblings are not API route objects.
    """
    for route in routes:
        original_router = getattr(route, "original_router", None)
        if original_router is not None:
            yield from _iter_route_objects(getattr(original_router, "routes", ()) or ())
            continue
        yield route


def build_live_routes_from_routes(routes: Any) -> list[LiveRoute]:
    """Build the live route table from the app's actual route objects.

    Unlike ``app.openapi()['paths']`` this includes ``include_in_schema=False``
    routes (e.g. the workspace-lease read model) and retains the
    ``{param:path}`` converter, so a multi-segment path is not reported stale.
    """
    table: dict[str, set[str]] = {}
    for route in _iter_route_objects(routes):
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None)
        if not isinstance(path, str) or not methods:
            continue
        normalised = _normalise_template(path)
        table.setdefault(normalised, set()).update(
            method.upper() for method in methods if method.upper() in _HTTP_METHODS
        )
    return [
        LiveRoute(template=template, segments=_split_segments(template), methods=frozenset(methods))
        for template, methods in table.items()
        if methods
    ]


def load_live_routes() -> list[LiveRoute]:
    """Import the app under the template env and return its live routes."""
    temp_db = Path(tempfile.gettempdir()) / "modulo_e2e_route_method_check.db"
    for key, value in _TEMPLATE_ENV.items():
        os.environ[key] = value
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{temp_db.as_posix()}"
    from modulo.api.main import app

    return build_live_routes_from_routes(app.routes)


def _segments_match(extracted: str, live: str, *, last_prefix: bool) -> bool:
    if extracted == _WILDCARD:
        return True
    if live.startswith("{") and live.endswith("}"):
        return True
    if last_prefix:
        return live.startswith(extracted)
    return extracted == live


def path_matches(call: HttpCall, route: LiveRoute) -> bool:
    """Does *route* match *call*'s path under Playwright-glob semantics?

    A live ``{param:path}`` converter consumes one or more segments and may sit
    ANYWHERE in the pattern (``/primitives/{slug:path}/download``), so matching
    is recursive rather than a fixed-length comparison.
    """
    return _match_route_segments(_split_segments(call.path), route.segments, call, 0, 0)


def _match_route_segments(
    extracted: tuple[str, ...], live: tuple[str, ...], call: HttpCall, extracted_index: int, live_index: int
) -> bool:
    if live_index == len(live):
        return extracted_index == len(extracted)
    live_segment = live[live_index]
    if live_segment == _MULTI_WILDCARD:
        # A prefix glob's ``**`` already covers the converter's segments.
        if call.trailing_glob and extracted_index == len(extracted):
            return True
        # The converter needs at least one segment unless the call is a prefix
        # glob; try every possible length so a converter mid-path (followed by
        # literal segments) can still align with its suffix.
        for consumed in range(1, len(extracted) - extracted_index + 1):
            if _match_route_segments(extracted, live, call, extracted_index + consumed, live_index + 1):
                return True
        return False
    if extracted_index == len(extracted):
        # A prefix glob may stop before the live route's remaining segments.
        return call.trailing_glob
    is_last_extracted = extracted_index == len(extracted) - 1
    if not _segments_match(
        extracted[extracted_index], live_segment, last_prefix=is_last_extracted and call.last_segment_prefix
    ):
        return False
    return _match_route_segments(extracted, live, call, extracted_index + 1, live_index + 1)


def _closest_route(call: HttpCall, routes: list[LiveRoute]) -> LiveRoute | None:
    candidates = [route.template for route in routes]
    matches = difflib.get_close_matches(call.path, candidates, n=1, cutoff=0.5)
    if not matches:
        return None
    return next(route for route in routes if route.template == matches[0])


def find_violations(calls: list[HttpCall], routes: list[LiveRoute]) -> list[Violation]:
    """Return one violation per call whose (method, path) has no live route."""
    violations: list[Violation] = []
    for call in calls:
        matching_paths = [route for route in routes if path_matches(call, route)]
        if not matching_paths:
            closest = _closest_route(call, routes)
            detail = (
                f"no live route matches (closest: {call.method} {closest.template} -> "
                f"{_format_methods(closest.methods)})"
                if closest is not None
                else "no live route matches"
            )
            violations.append(Violation(call=call, detail=detail))
            continue
        if any(call.method in route.methods for route in matching_paths):
            continue
        best = min(matching_paths, key=lambda route: len(route.template))
        violations.append(
            Violation(
                call=call,
                detail=f"live route {best.template} supports {_format_methods(best.methods)}, not {call.method}",
            )
        )
    return violations


def _format_methods(methods: frozenset[str]) -> str:
    return ", ".join(sorted(methods)) if methods else "(no methods)"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _e2e_files() -> list[Path]:
    if not E2E_DIR.is_dir():
        raise FileNotFoundError(f"e2e directory not found: {E2E_DIR}")
    files = sorted(E2E_DIR.rglob("*.ts"))
    if not files:
        raise FileNotFoundError(f"no *.ts files found under: {E2E_DIR}")
    return files


def collect_calls(notes: list[str] | None = None, unanalysed: list[str] | None = None) -> list[HttpCall]:
    calls: list[HttpCall] = []
    for path in _e2e_files():
        relative = path.relative_to(REPO_ROOT).as_posix()
        calls.extend(extract_http_calls(path.read_text(encoding="utf-8"), relative, notes, unanalysed))
    return calls


def main() -> int:
    notes: list[str] = []
    unanalysed: list[str] = []
    try:
        routes = load_live_routes()
        if not routes:
            print("e2e route-method check: backend exposed no routes - refusing to pass", file=sys.stderr)
            return 2
        calls = collect_calls(notes, unanalysed)
    except FileNotFoundError as exc:
        print(f"e2e route-method check: input error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"e2e route-method check: setup failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    for note in notes:
        print(f"e2e route-method check: note: {note}", file=sys.stderr)

    if not calls:
        print(
            "e2e route-method check: the extractor found NO HTTP descriptors in "
            f"{E2E_DIR} - refusing to report a clean scan",
            file=sys.stderr,
        )
        return 2

    contributing = {call.filename for call in calls}
    missing = [required for required in _REQUIRED_FILES if required not in contributing]
    if missing:
        for required in missing:
            print(
                f"e2e route-method check: the extractor found NO descriptor in {required} "
                f"- its scan shapes regressed, refusing to report a clean scan",
                file=sys.stderr,
            )
        return 2

    if unanalysed:
        print(
            f"e2e route-method check: {len(unanalysed)} unanalysed E2E HTTP call shape(s):",
            file=sys.stderr,
        )
        for message in unanalysed:
            print(f"  {message}", file=sys.stderr)
        print(
            "\nA call shape this guard does not model is a silent gap in the deploy gate. "
            "Model the shape, or annotate the line with "
            "`// e2e-route-check: ignore <reason>` when it is genuinely not an API call.",
            file=sys.stderr,
        )
        return 1

    violations = find_violations(calls, routes)
    if violations:
        print(
            f"E2E HTTP calls reference a method the live API does not serve "
            f"({len(violations)} stale descriptor(s) across {len(calls)} checked):",
            file=sys.stderr,
        )
        for violation in violations:
            print(f"  {violation.describe()}", file=sys.stderr)
        print(
            "\nA deployed @regression caller naming a rotated method fails the staging deploy; "
            "a stale page.route interceptor passes while asserting nothing. "
            "Update the descriptor to the live method (see the OpenAPI route above).",
            file=sys.stderr,
        )
        return 1

    print(f"E2E route-method check OK: {len(calls)} descriptor(s) match {len(routes)} live routes.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
