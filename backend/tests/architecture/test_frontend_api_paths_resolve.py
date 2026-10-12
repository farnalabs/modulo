"""Architecture guard: frontend ``/api/`` path literals must resolve against the OpenAPI schema.

``frontend/src/lib/api/schema.ts`` is the openapi-typescript output — the
generated, authoritative list of the API surface the SPA is written against.
A frontend literal such as ``api.GET('/api/v1/pipelines')`` that names a path
the schema does not contain is a *dangling call*: it 404s (or is never routed)
at runtime while every unit test stays green, because tests mock the HTTP
layer and never notice the route is absent.

This guard walks ``frontend/src/**`` (``.vue`` / ``.ts``), extracts every
string and template literal that begins with ``/api/``, normalises it, and
asserts that each one resolves against the schema's ``paths`` keys **or** is
listed in ``ALLOWLIST`` (every entry carrying a reason).

Scope decisions
---------------
* **Test files are excluded** (``__tests__/``, ``*.spec.ts``, ``*.test.ts``).
  They deliberately fabricate URLs — ``/api/v1/widgets``, ``/api/v1/x`` — as
  mock-routing keys; flagging those would force a wholesale allowlist of
  invented endpoints with no signal. The guard covers product code, where a
  dangling path is a real defect. This is an extractor tightening, not an
  allowlist entry.
* **The generated ``schema.ts`` itself is excluded** — it is the authority
  being checked, and it mentions ``/api/...`` prefixes inside doc comments.

Normalisation and matching
--------------------------
``${expr}`` and ``:param`` both become ``{param}``; ``?query`` and
``#fragment`` are stripped; ``//`` collapses and a trailing ``/`` drops.
Resolution follows RFC 6570 segment semantics: a concrete segment matches a
schema segment when the schema segment is a ``{param}`` template (it accepts
any value) or when the two literals are equal — so ``/api/v1/admin/users/u-1``
resolves against ``/api/v1/admin/users/{user_id}``, while a misspelled literal
segment does not.

A literal that cannot be *statically* normalised (nested interpolation leaving
``${`` behind, or a stray ``$`` / ``}`` / whitespace in the path) is skipped
rather than allowlisted: the guard never guesses at a value it cannot read.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
FRONTEND_SRC = REPO_ROOT / "frontend" / "src"
SCHEMA_FILE = FRONTEND_SRC / "lib" / "api" / "schema.ts"
SCHEMA_REL = "frontend/src/lib/api/schema.ts"

# Anchored on a quote immediately before ``/api/`` so an apostrophe in prose
# or a comment cannot swallow the real literal that follows it.
_API_LITERAL_RE = re.compile(
    r"""(?P<quote>['"`])(?P<path>/api/(?:\\.|(?!(?P=quote)).)*)(?P=quote)""",
    re.DOTALL,
)
_TEMPLATE_EXPR_RE = re.compile(r"\$\{[^}]*\}")
_COLON_PARAM_RE = re.compile(r"(?<=/):[A-Za-z_][A-Za-z0-9_]*")
_SQUASH_SLASH_RE = re.compile(r"/{2,}")
# A path is only trusted if, after normalisation, every character is one a
# URL path segment may legitimately contain (``%2F`` style escapes included).
_WELL_FORMED_PATH_RE = re.compile(r"/[A-Za-z0-9._~%\-{}]+(?:/[A-Za-z0-9._~%\-{}]+)*\Z")

_EXCLUDED_DIR_NAMES = frozenset({"__tests__", "node_modules", "dist", "coverage"})
_EXCLUDED_SUFFIXES = (".spec.ts", ".test.ts", ".spec.js", ".test.js")

# ---------------------------------------------------------------------------
# Allowlist. Each entry: normalised path -> reason. A reason is mandatory and
# is asserted to be present and non-blank by the test below.
# ---------------------------------------------------------------------------
ALLOWLIST: dict[str, str] = {
    "/api/v1/composites": (
        "frontend/src/stores/compositeStore.ts loadComposites() GETs /api/v1/composites, but the "
        "backend only mounts /api/v1/composite-templates (backend/src/modulo/api/routes/"
        "composite_templates.py) — no /composites route exists anywhere in the backend. Genuine "
        "dangling call in product code; fixing it means repointing the store AND its spec mock to "
        "the composite-templates endpoint (response shape differs), which is not a one-line "
        "correction. Reported for follow-up under FAR-1713."
    ),
    "/api/v1/admin/assistant/primer/regenerate": (
        "frontend/src/views/AdminAssistantView.vue regeneratePrimer() POSTs this path, but "
        "backend/src/modulo/api/routes/admin_assistant.py exposes no /primer/* route (only "
        "/config, /available-providers, /skills, /context-sources). Genuine dangling call in "
        "product code — the Regenerate Primer button can never succeed; fixing it needs a new "
        "backend route. Reported for follow-up under FAR-1713."
    ),
    "/api/v1/plugins/{param}/toggle": (
        "frontend/src/views/AdminPluginsView.vue togglePlugin() PUTs /api/v1/plugins/{id}/toggle, "
        "but backend/src/modulo/api/routes/plugins.py (prefix /api/v1/plugins) exposes only GET "
        "'', GET /{plugin_id} and GET /{plugin_id}/health — there is no PUT route at all. Genuine "
        "dangling call in product code; fixing it needs a new backend route. Reported for "
        "follow-up under FAR-1713."
    ),
}


def _iter_frontend_files() -> list[Path]:
    """Product-code ``.vue``/``.ts`` files under ``frontend/src`` (tests excluded)."""
    files: list[Path] = []
    for path in sorted(FRONTEND_SRC.rglob("*")):
        if path.suffix not in {".vue", ".ts"}:
            continue
        if path.name.endswith(_EXCLUDED_SUFFIXES):
            continue
        relative = path.relative_to(FRONTEND_SRC)
        if relative == Path("lib/api/schema.ts"):
            continue  # generated authority, not a consumer
        if any(part in _EXCLUDED_DIR_NAMES for part in relative.parts[:-1]):
            continue
        files.append(path)
    return files


def _extract_api_literals(content: str) -> list[tuple[int, str]]:
    """Return ``(line_number, raw_literal)`` for every ``/api/`` literal in ``content``."""
    return [
        (content.count("\n", 0, match.start()) + 1, match.group("path")) for match in _API_LITERAL_RE.finditer(content)
    ]


def _normalise(raw: str) -> str | None:
    """Normalise a raw path literal, or return ``None`` if it cannot be normalised statically."""
    path = raw.split("#", 1)[0].split("?", 1)[0]
    if not path:
        return None
    path = _TEMPLATE_EXPR_RE.sub("{param}", path)
    if "${" in path:
        return None  # interpolation the regex could not bracket — do not guess
    path = _COLON_PARAM_RE.sub("{param}", path)
    path = _SQUASH_SLASH_RE.sub("/", path)
    if len(path) > 1:
        path = path.rstrip("/")
    if not _WELL_FORMED_PATH_RE.fullmatch(path):
        return None
    return path


def _read_string(text: str, start: int) -> tuple[str, int]:
    """Read a quoted string starting at ``start``; return ``(value, index_after_closing_quote)``."""
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
    return "".join(buffer), length  # unterminated — the parser stops at EOF


def _scan_paths_interface_keys(text: str, open_brace: int) -> set[str]:
    """Collect the top-level keys of the ``export interface paths`` block.

    A small comment/string-aware brace scanner: doc comments inside the block
    routinely contain ``{...}`` path-parameter examples, so naive brace
    counting would mis-locate the block end.
    """
    keys: set[str] = set()
    depth = 0
    index = open_brace
    length = len(text)
    while index < length:
        char = text[index]
        if char == "/" and index + 1 < length and text[index + 1] == "/":
            newline = text.find("\n", index)
            index = length if newline < 0 else newline + 1
            continue
        if char == "/" and index + 1 < length and text[index + 1] == "*":
            end = text.find("*/", index + 2)
            index = length if end < 0 else end + 2
            continue
        if char in ('"', "'", "`"):
            value, index = _read_string(text, index)
            if depth == 1 and value.startswith("/"):
                cursor = index
                while cursor < length and text[cursor] in " \t\r\n":
                    cursor += 1
                if cursor < length and text[cursor] == ":":
                    keys.add(value)
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                break
        index += 1
    return keys


@lru_cache(maxsize=1)
def _load_schema_paths() -> frozenset[str]:
    text = SCHEMA_FILE.read_text(encoding="utf-8")
    marker = text.find("export interface paths")
    if marker < 0:
        raise AssertionError(f"{SCHEMA_REL} no longer declares 'export interface paths'")
    open_brace = text.find("{", marker)
    if open_brace < 0:
        raise AssertionError(f"{SCHEMA_REL} 'export interface paths' has no opening brace")
    return frozenset(_scan_paths_interface_keys(text, open_brace))


def _resolves(concrete: str, template: str) -> bool:
    """RFC 6570-style match: a schema ``{param}`` segment accepts any concrete segment."""
    concrete_segments = concrete.split("/")
    template_segments = template.split("/")
    if len(concrete_segments) != len(template_segments):
        return False
    for concrete_segment, template_segment in zip(concrete_segments, template_segments, strict=True):
        if template_segment.startswith("{") and template_segment.endswith("}"):
            continue
        if concrete_segment != template_segment:
            return False
    return True


@lru_cache(maxsize=1)
def _scan() -> tuple[frozenset[str], tuple[str, ...], tuple[str, ...]]:
    """Scan every product source file once.

    Returns ``(observed_unresolved_paths, offender_report_lines,
    skipped_non_normalisable_literals)``.
    """
    schema_paths = _load_schema_paths()
    observed: set[str] = set()
    offenders: list[str] = []
    skipped: list[str] = []
    for path in _iter_frontend_files():
        content = path.read_text(encoding="utf-8")
        relative = path.relative_to(REPO_ROOT).as_posix()
        for line, raw in _extract_api_literals(content):
            normalised = _normalise(raw)
            if normalised is None:
                skipped.append(f"  {relative}:{line}  {raw!r}")
                continue
            if any(_resolves(normalised, candidate) for candidate in schema_paths):
                continue
            observed.add(normalised)
            if normalised not in ALLOWLIST:
                offenders.append(f"  {relative}:{line}  {normalised}   (raw: {raw})")
    return frozenset(observed), tuple(sorted(offenders)), tuple(sorted(skipped))


def test_openapi_schema_paths_parse():
    """The generated schema parses and yields a non-trivial path inventory."""
    schema_paths = _load_schema_paths()
    assert schema_paths, f"parsed no paths out of {SCHEMA_REL}"
    assert "/api/v1/pipelines" in schema_paths, (
        f"{SCHEMA_REL} no longer contains /api/v1/pipelines — the paths-interface scanner is broken"
    )


def test_frontend_api_paths_resolve_against_openapi_schema():
    """Every ``/api/`` literal in frontend product code resolves against the schema."""
    _, offenders, _ = _scan()
    assert not offenders, (
        f"{len(offenders)} frontend /api/ path literal(s) do not resolve against the OpenAPI "
        f"schema in {SCHEMA_REL} and are not in ALLOWLIST:\n" + "\n".join(offenders) + "\n"
        "Fix the dangling call, or add the *normalised* path to ALLOWLIST with a reason."
    )


def test_allowlist_entries_are_live_and_reasoned():
    """No dead allowlist entries, and every entry carries a non-blank reason."""
    unreasoned = [path for path, reason in ALLOWLIST.items() if not reason.strip()]
    assert not unreasoned, f"ALLOWLIST entries missing a reason string: {unreasoned}"
    observed, _, _ = _scan()
    dead = sorted(set(ALLOWLIST) - observed)
    assert not dead, (
        "Stale ALLOWLIST entries — these paths now resolve (or no longer exist) and the entry "
        f"should be deleted: {dead}"
    )


def test_extractor_skips_are_reported():
    """Literals the extractor could not statically normalise are surfaced, not swallowed.

    A non-normalisable ``/api/`` literal is a blind spot in this guard. It is
    allowed (the guard never guesses), but it must never be *invisible*: the
    current tree is expected to contain none, so any appearing later fails here
    and the author must either fix the literal or consciously widen the guard.
    """
    _, _, skipped = _scan()
    assert not skipped, (
        f"{len(skipped)} /api/ literal(s) could not be statically normalised and were skipped:\n" + "\n".join(skipped)
    )
