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
``.../folder``). It builds the live route table from the backend's OpenAPI
document (in-process, template env, no network or database - the same import
the schema-freshness job uses) and matches every extracted (method, path) pair
against it.

What is scanned (``frontend/tests/e2e/**/*.ts``):

* ``apiFetch(<base>, <token>, '<METHOD>', <path>)`` with the method as a
  string literal and the path as a string/template literal, across multi-line
  argument lists and ``${...}`` interpolation.
* direct ``fetch(<url>, { method: '<METHOD>' })`` in the setup helpers.
* ``page.request.<verb>(<url>)`` / ``request.<verb>(<url>)`` calls on a
  Playwright ``APIRequestContext`` (the verb is the method).
* ``route.request().method() === '<METHOD>'`` interceptor filters (and the
  reversed ``'<METHOD>' === route.request().method()`` form), resolved against
  the ``page.route('<url>', ...)`` registration that encloses them in the same
  file/scope. The callback's parameters may be renamed and both are read: the
  first is the route, the second is the request (``async (route, request) =>
  ... request.method() === 'GET'``). A local variable holding the request
  object (``const request = route.request()``) or the method result
  (``const m = route.request().method()``) is resolved too. A comparison
  combined with an in-callback URL test (``url.includes`` / ``url.endsWith``)
  is skipped: its effective path lives in that condition, not in the
  registration, so the guard reports only what it can prove statically.
  ``!== '<METHOD>'`` filters are skipped too - they guard a fallback branch
  rather than selecting a method. Anything that cannot be resolved safely is
  reported as an explicit note on stderr (never silently dropped, and never a
  failure).

Template placeholders (``${expr}``, ``:param``, ``{param}``) and glob
wildcards normalise to one path-parameter segment, and an anchored
regex-literal registration (``/\\/api\\/v1\\/plan$/``) is normalised the same
way (anchors stripped, parameter-ish groups/classes collapsed to ``{param}``).
A leading interpolation before the ``/api/`` segment
(``${apiBase}/api/v1/...``) is discarded, and a concatenation that ends in a
genuine path variable (``'/api/v1/pipelines/' + id``) keeps a trailing
wildcard. A trailing query-string variable (``'/api/v1/pipelines' + query``),
a TypeScript assertion (``'/api/v1/auth/me' as const``) and a literal that
already carries a ``?`` add no segment. Query strings and fragments are
stripped.

Exit codes: 0 clean, 1 stale descriptors (one line each, with file:line, the
method, the path and the closest live route), 2 setup/tool error - including an
extractor that found no descriptors at all or no descriptor in either
ticket-named file - never a silent pass.
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
#: A query-suffix expression: a conventional identifier, optionally chained
#: through a property/call (``queryParams.toString()``) or defaulted.
_QUERY_SUFFIX_EXPR_RE = re.compile(
    r"(?P<name>[A-Za-z_$][A-Za-z0-9_$]*)"
    r"(?:\s*(?:\?\.|\.)\s*[A-Za-z_$][A-Za-z0-9_$]*\s*(?:\([^()]*\))?)*"
    r"(?:\s*(?:\?\?|\|\|)\s*(?:''|\"\"|``))?"
)
#: A trailing TypeScript assertion (``as const``, ``as string``, ``satisfies X``).
_TS_ASSERTION_RE = re.compile(r"(?:^|\s)(?:as|satisfies)\s+[^+\n]+$")
#: Regex-literal route registrations: parameter-ish constructs.
_REGEX_NAMED_GROUP_RE = re.compile(r"\(\?(?:[:=!]|<[A-Za-z_][A-Za-z0-9_]*>)[^()]*\)(?:[+*?]|\{\d+(?:,\d*)?\})?")
_REGEX_GROUP_RE = re.compile(r"\((?:[^()\\]|\\.)*\)(?:[+*?]|\{\d+(?:,\d*)?\})?")
_REGEX_CHAR_CLASS_RE = re.compile(r"\[[^\]]*\](?:[+*?]|\{\d+(?:,\d*)?\})?")
_REGEX_SHORTHAND_RE = re.compile(r"\\[dDwWsS](?:[+*?]|\{\d+(?:,\d*)?\})?")
_REGEX_ESCAPE_RE = re.compile(r"\\([^A-Za-z0-9])")
_COLON_PARAM_RE = re.compile(r"(?<=/):[A-Za-z_][A-Za-z0-9_]*")
_SQUASH_SLASH_RE = re.compile(r"/{2,}")
_SEGMENT_RE = re.compile(r"[A-Za-z0-9._~%\-{}]+")
_API_PREFIX = "/api/"
_FETCH_INIT_METHOD_RE = re.compile(r"""\bmethod\s*:\s*(['"])([A-Za-z]+)\1""")
_ROUTE_CALL_RE = re.compile(r"(?<![\w.])(?:page|context)\s*\.\s*route\s*\(")
_API_FETCH_RE = re.compile(r"(?<![\w.$])apiFetch\b")
_FETCH_RE = re.compile(r"(?<![\w.$])fetch\b")
# ``page.request.get(...)`` / ``request.delete(...)`` on an APIRequestContext.
_API_REQUEST_RE = re.compile(
    r"(?<![\w.$])(?:page\s*\.\s*request|request)\s*\.\s*"
    r"(get|post|put|patch|delete|head|options)\b"
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
    depth) so ``apiFetch<PendingReviewsResponse>(`` is handled.
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
    text = text.lstrip("?&").strip()
    if not text:
        return False
    if text.startswith(("`", "'", '"')):
        # A string/template literal that begins with ``?``/``&`` is a query.
        value, _ = _read_template(text, 0) if text[0] == "`" else _read_quoted(text, 0)
        return value.lstrip().startswith(("?", "&"))
    match = _QUERY_SUFFIX_EXPR_RE.fullmatch(text)
    if match is None:
        return False
    return match.group("name").lower() in _QUERY_SUFFIX_NAMES


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


def _literal_method(expr: str) -> str | None:
    """Return the HTTP method when *expr* is exactly one quoted method literal."""
    stripped = expr.strip()
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
    so it is deliberately skipped. A comparison whose enclosing ``if`` also
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
            if api_shaped:
                notes.append(
                    f"{filename}:{_line_of(text, match.start())}: page.route registration uses an API path "
                    f"that could not be normalised statically; its method filter was not checked"
                )
            continue
        path, trailing_glob, last_prefix = path_info
        if trailing_glob and path in ("/api", "/api/v1"):
            # A catch-all API mock (``**/api/v1/**``) carries no specific route
            # to validate - resolving its fallback branch would invent one.
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

        resolved: list[re.Match[str]] = list(
            _resolved_method_comparisons(
                text, masked, span_start, span_end, route_parameter, _method_comparison_re(route_parameter)
            )
        )
        for request_object in request_objects:
            resolved.extend(
                _resolved_method_comparisons(
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
            resolved.extend(
                _resolved_method_comparisons(
                    text, masked, span_start, span_end, variable, _variable_comparison_re(variable)
                )
            )

        for match in resolved:
            method = match.group(2) or match.group(4)
            if method is None or method.upper() not in _HTTP_METHODS:
                continue
            condition = _enclosing_if_condition(text, masked, match.start())
            if condition is None or _URL_CONDITION_RE.search(condition):
                # The effective path (or the branch) lives in that condition.
                continue
            key = (_line_of(text, match.start()), method.upper(), path)
            if key in seen:
                continue
            seen.add(key)
            calls.append(
                HttpCall(
                    filename=filename,
                    line=key[0],
                    method=method.upper(),
                    path=path,
                    kind="interceptor",
                    trailing_glob=bool(trailing_glob),
                    last_segment_prefix=bool(last_prefix),
                )
            )
    return calls


def extract_http_calls(source: str, filename: str, notes: list[str] | None = None) -> list[HttpCall]:
    """Extract every statically-readable E2E HTTP call descriptor from *source*.

    *notes* (when given) collects explicit explanations for shapes the checker
    cannot resolve safely, so a skip is visible rather than silent.
    """
    masked = mask_non_code(source)
    calls: list[HttpCall] = []

    for match in _API_FETCH_RE.finditer(masked):
        open_paren = _find_open_paren(masked, match.end())
        if open_paren is None:
            continue
        args = _args_of(source, masked, open_paren)
        if len(args) < 4:
            continue
        method = _literal_method(args[2])
        path_info = extract_path(args[3])
        if method is None or path_info is None:
            continue
        path, trailing_glob, last_prefix = path_info
        calls.append(
            HttpCall(
                filename=filename,
                line=_line_of(source, match.start()),
                method=method,
                path=path,
                kind="apiFetch",
                trailing_glob=trailing_glob,
                last_segment_prefix=last_prefix,
            )
        )

    for match in _FETCH_RE.finditer(masked):
        open_paren = _find_open_paren(masked, match.end())
        if open_paren is None:
            continue
        args = _args_of(source, masked, open_paren)
        if not args:
            continue
        path_info = extract_path(args[0])
        if path_info is None:
            continue
        method: str | None = None
        for argument in args[1:]:
            init_match = _FETCH_INIT_METHOD_RE.search(argument)
            if init_match is not None and init_match.group(2).upper() in _HTTP_METHODS:
                method = init_match.group(2).upper()
                break
        if method is None:
            continue  # implicit GET (or an init we cannot read) - not asserted
        path, trailing_glob, last_prefix = path_info
        calls.append(
            HttpCall(
                filename=filename,
                line=_line_of(source, match.start()),
                method=method,
                path=path,
                kind="fetch",
                trailing_glob=trailing_glob,
                last_segment_prefix=last_prefix,
            )
        )

    for match in _API_REQUEST_RE.finditer(masked):
        open_paren = _find_open_paren(masked, match.end())
        if open_paren is None:
            continue
        args = _args_of(source, masked, open_paren)
        if not args:
            continue
        method = match.group(1).upper()
        if method not in _HTTP_METHODS:
            continue
        path_info = extract_path(args[0])
        if path_info is None:
            continue
        path, trailing_glob, last_prefix = path_info
        calls.append(
            HttpCall(
                filename=filename,
                line=_line_of(source, match.start()),
                method=method,
                path=path,
                kind="request",
                trailing_glob=trailing_glob,
                last_segment_prefix=last_prefix,
            )
        )

    calls.extend(_extract_route_interceptors(source, masked, filename, notes))
    calls.sort(key=lambda call: (call.line, call.method, call.path))
    return calls


# ---------------------------------------------------------------------------
# Live route table and matching
# ---------------------------------------------------------------------------


def _split_segments(path: str) -> tuple[str, ...]:
    return tuple(segment for segment in path.split("/") if segment)


def build_live_routes(openapi_paths: dict[str, Any]) -> list[LiveRoute]:
    """Build the normalised live route table from ``app.openapi()['paths']``."""
    routes: list[LiveRoute] = []
    for template, operations in openapi_paths.items():
        normalised = _TEMPLATE_EXPR_RE.sub(_WILDCARD, _COLON_PARAM_RE.sub(_WILDCARD, template))
        methods = frozenset(method.upper() for method in operations if method.upper() in _HTTP_METHODS)
        routes.append(LiveRoute(template=normalised, segments=_split_segments(normalised), methods=methods))
    return routes


def load_live_routes() -> list[LiveRoute]:
    """Import the app under the template env and return its live routes."""
    temp_db = Path(tempfile.gettempdir()) / "modulo_e2e_route_method_check.db"
    for key, value in _TEMPLATE_ENV.items():
        os.environ[key] = value
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{temp_db.as_posix()}"
    from modulo.api.main import app

    return build_live_routes(app.openapi()["paths"])


def _segments_match(extracted: str, live: str, *, last_prefix: bool) -> bool:
    if extracted == _WILDCARD:
        return True
    if live.startswith("{") and live.endswith("}"):
        return True
    if last_prefix:
        return live.startswith(extracted)
    return extracted == live


def path_matches(call: HttpCall, route: LiveRoute) -> bool:
    """Does *route* match *call*'s path under Playwright-glob semantics?"""
    extracted = _split_segments(call.path)
    live = route.segments
    if call.trailing_glob:
        if len(extracted) > len(live):
            return False
    elif len(extracted) != len(live):
        return False
    for index, (extracted_segment, live_segment) in enumerate(zip(extracted, live, strict=False)):
        is_last = index == len(extracted) - 1
        if not _segments_match(extracted_segment, live_segment, last_prefix=is_last and call.last_segment_prefix):
            return False
    return True


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


def collect_calls(notes: list[str] | None = None) -> list[HttpCall]:
    calls: list[HttpCall] = []
    for path in _e2e_files():
        relative = path.relative_to(REPO_ROOT).as_posix()
        calls.extend(extract_http_calls(path.read_text(encoding="utf-8"), relative, notes))
    return calls


def main() -> int:
    notes: list[str] = []
    try:
        routes = load_live_routes()
        if not routes:
            print("e2e route-method check: backend exposed no routes - refusing to pass", file=sys.stderr)
            return 2
        calls = collect_calls(notes)
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
