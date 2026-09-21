#!/usr/bin/env python3
"""Changed-lines coverage gate — enforces per-PR coverage thresholds on the
lines each PR actually changes, plus a project-wide floor ratchet.

Why this exists: farnalabs/modulo is on free SonarCloud, which does NOT allow
customising or assigning quality gates.  SonarCloud still computes and
displays coverage, but we cannot make it block on a threshold.  We enforce it
ourselves in CI via this script.

The script runs ``diff-cover`` against the backend Cobertura XML report and
the frontend LCOV report *separately*, using ``--compare-branch`` to diff
against the PR's base branch.  The comparison uses diff-cover's merge-base
semantics (the branch's actual changes, not main's newer content), and the
frontend LCOV paths are normalised before the run because vitest emits them
relative to ``frontend/`` rather than the repo root.  It prints a clear
summary per language and exits non-zero when a threshold is breached or a
required report is missing.

In addition to the diff-cover line-coverage check, the script computes branch
coverage on changed lines by parsing the raw reports directly:

- **Cobertura XML** (``backend/coverage.xml``): ``<line number="N" hits="H"
  branch="true" condition-coverage="50% (1/2)">`` with child
  ``<conditions><condition number=".." type="jump"
  coverage="50%"/></conditions>``.  Total = number of ``<condition>``
  entries (or the ``(a/b)`` denominator); covered = those with
  ``coverage="100%"``.

- **LCOV** (``frontend/coverage/lcov.info``):
  ``BRDA:<line>,<block>,<branch>,<taken>``.  ``taken`` is ``"-"`` when the
  branch never executed.  Covered = ``taken`` is present and ``> 0``.

Branch records are attributed to their source line and intersected with the
lines the PR added (via ``git diff --unified=0``).  Branch coverage is
computed over that intersection.

Project-wide floor (the ratchet): a whole-project check on BOTH metrics from
the full reports (not diff-limited).  ``MIN_PROJECT_LINE_COVERAGE`` and
``MIN_PROJECT_BRANCH_COVERAGE`` are deliberately conservative initial floors
that cannot fail on today's repo — the point is to install the mechanism, not
to pick the final number.  These floors are a ratchet: raise as coverage
improves, never lower, and lowering requires an explicit justified commit
message.

.. note::

   The project-wide percentage is computed over the CI coverage run's
   instrumented set (``pytest --cov=src/modulo``, ``vitest run src``) and will
   therefore NOT equal the SonarCloud badge figure, which analyses a wider
   scope.

Gate semantics (fail-closed):

- **Report file missing** → ERROR, exit 1.  A missing report means the
  upstream job that produces it failed or its artifact download broke.
  Fail-closed so a broken pipeline never silently disables the gate.
  Use ``--allow-missing-reports`` for local runs where you may not have
  every report.
- **No changed production lines** → SKIP, exit 0.  A test-only or docs-only
  diff has no production files once the exclusions are applied.  A diff that
  only adds comments, docstrings, or blank lines also skips — only
  executable changed lines enter the coverage denominator (FAR-962).
- **Changed production lines, but no coverage data for them** → FAIL,
  exit 1.  If production lines changed but diff-cover cannot match them to
  the coverage report (the report does not contain those files at all), every
  such changed line counts as unmeasured coverage, i.e. 0%.
- **Threshold breach** → FAIL, exit 1.
- **Tiny diff (≤10 non-blank lines)** → PASS with a note.  Trivial
  changes (typo fixes, label tweaks) should not fail the gate.
- **Unmeasured changed file** → counts as 0% coverage.  A brand-new
  production file with no coverage in the report is a gate failure.
  Detected by comparing the changed production files against the report's
  ``src_stats``: a file absent from the report contributes every non-blank
  changed line to the denominator at 0%.  A file that IS in the report is
  measured only against its *coverable* changed lines (``covered_lines`` +
  ``violation_lines`` from diff-cover's ``src_stats``), because coverage.py
  and v8 never instrument non-executable lines (annotations, decorators,
  continuations, static ``.vue`` template markup, comments, import-only
  lines).  Counting those against a tested module would make a fully-covered
  module mathematically unable to clear the threshold.

Branch coverage edge cases:

- **Zero branches on changed lines** → vacuous pass.  When no branch records
  exist for any changed line, branch coverage is undefined and the branch
  gate passes with a note ("vacuous — no branch records on changed lines").
  The parsers retain only branch records, so a changed line that has no
  branch (straight-line code) is indistinguishable from a missing branch
  record: neither contributes a branch, and neither is scored as 0%.  A
  straight-line diff therefore passes the branch gate vacuously no matter
  how many lines it changes.
- **Branch records present but none taken** → 0%, FAIL.  When branch records
  exist on changed lines but every ``taken`` value is ``"-"`` (never
  executed), branch coverage is 0% and the gate fails.  This is NOT a
  vacuous pass — the branches exist and were not exercised.

Usage (local)::

    uv run --project backend python scripts/run_coverage_gate.py \\
        --compare-branch origin/main --allow-missing-reports

Usage (CI)::

    python scripts/run_coverage_gate.py \\
        --compare-branch origin/$GITHUB_BASE_REF
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import json
import os
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# Threshold constants — the single source of truth for the coverage floors.
# Change these values to adjust the gate across all PRs.
# ---------------------------------------------------------------------------
COVERAGE_THRESHOLD = 98  # Changed-lines line coverage minimum (%)
BRANCH_COVERAGE_THRESHOLD = 98  # Changed-lines branch coverage minimum (%)

# Project-wide floor (the ratchet).  These are deliberately conservative
# initial floors that cannot fail on today's repo — the point is to install
# the mechanism, not to pick the final number.  Raise as coverage improves;
# never lower.  Lowering requires an explicit justified commit message.
#
# Computed over the CI coverage run's instrumented set (pytest --cov=src/modulo,
# vitest run src), NOT the full SonarCloud scope.
MIN_PROJECT_LINE_COVERAGE = 89.0
MIN_PROJECT_BRANCH_COVERAGE = 81.0

# Files/patterns excluded from the gate's denominator.  These mirror the
# ``sonar.coverage.exclusions`` in sonar-project.properties — test paths,
# migrations, scripts, docs examples, and generated code are all out of scope.
#
# The generic ``tests/**`` patterns below do NOT match this repo's frontend
# specs, which live under ``frontend/src/__tests__/**`` (vitest) and
# ``frontend/tests/e2e/**`` (Playwright).  Those directories are excluded from
# SonarCloud's analysis scope (see ``sonar.exclusions``), so they are never
# instrumented and would otherwise register every changed spec line as 0%
# production coverage.  The same applies to the generated API schema and the
# i18n catalogs, which ``sonar.exclusions`` also removes from analysis scope.
_EXCLUDE_PATTERNS: list[str] = [
    "migrations/**",
    "backend/scripts/**",
    "backend/tools/**",
    "scripts/**",
    "frontend/scripts/**",
    "docs/api/examples/**",
    # Auto-generated code: ``frontend/src/lib/api/schema.ts`` is emitted by
    # openapi-typescript (scripts/run_generate_api_types.py) and carries no
    # executable logic, so it never appears in the frontend LCOV report. It is
    # already excluded from the SonarCloud analysis scope in
    # sonar-project.properties / .sonarcloud.properties (``**/schema.ts``);
    # counting its thousands of generated lines here as "0% unmeasured" would
    # fail the gate on any PR that regenerates the API types.
    "**/schema.ts",
    "**/locales/**",
    "tests/**",
    "test_*/**",
    "**/test_*",
    "**/test_*.py",
    "**/conftest.py",
    "**/tests/**",
    "**/migrations/**",
    "**/__tests__/**",
    "**/*.spec.*",
    "**/*.test.*",
]

# Tiny-diff exemption: if the aggregate valid line count is below this
# threshold, pass with a note.  The ticket specified 10.
TINY_DIFF_THRESHOLD = 10

REPO_ROOT = Path(__file__).resolve().parent.parent

# Sentinel strings from diff-cover output.  These are matched against diff-cover
# 10.5.1's *real* human-readable output (verified empirically):
#   - stdout always ends with a ``Coverage: <pct>%`` line.
#   - on a threshold breach stderr carries ``Failure. Coverage is below <n>%.``
#   - a docs/test-only diff prints ``No lines with coverage information in this diff.``
_NO_CHANGED_LINES_RE = re.compile(r"No lines with coverage information in this diff", re.IGNORECASE)
_COVERAGE_LINE_RE = re.compile(r"Coverage:\s*(\d+(?:\.\d+)?)\s*%")
_THRESHOLD_NOT_MET_RE = re.compile(r"Failure\. Coverage is below", re.IGNORECASE)
_DIFF_COVER_TOTAL_RE = re.compile(r"Total:\s*(\d+)\s+line", re.IGNORECASE)
# ``@@ -old,count +new,count @@``: captures the new-file start line so the
# per-hunk bracket/string state can be seeded from the file content.
_HUNK_HEADER_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")

# Matches a standalone Python string literal (triple-quoted or single/double
# quoted) with no other code around it.  Used to exclude docstrings and bare
# strings from the executable-line count (coverage.py does not instrument them).
_PYTHON_STRING_LITERAL_RE = re.compile(r"""^\s*(?:('{3}|"{3})[\s\S]*\1|('{1}|"{1})[^\n]*\2)\s*$""")

# ``git diff`` filter and language pathspecs for changed-file discovery.
_DIFF_FILTER = "--diff-filter=ACM"
_EXTENSIONS: dict[str, tuple[str, ...]] = {
    "Python": ("*.py",),
    "JavaScript": ("*.ts", "*.tsx", "*.js", "*.jsx", "*.vue"),
}

# Directory (relative to the repo root) that the frontend LCOV report's
# relative ``SF:`` paths are resolved against.
DEFAULT_JS_SRC_ROOT = "frontend"

# Allow-list for values that flow into a subprocess command line.  Deriving the
# value from a regex ``fullmatch().group(0)`` gives the taint analyser a string
# provably bounded to safe characters before it reaches ``subprocess``.
# Includes ``\\`` and ``:`` for Windows absolute paths (e.g. ``C:\\Users\\...``).
_PATH_CHARS_RE = re.compile(r"[A-Za-z0-9._/\\:-]+")


def _validate_ref(value: str, name: str) -> str:
    """Reject values that would be interpreted as CLI flags when passed as a
    subprocess argument (defense against argument injection), and return a
    regex-bounded copy so the taint analyser sees a value that cannot carry an
    injection payload.
    """
    if not isinstance(value, str):
        raise ValueError(f"invalid {name}: expected a string, got {type(value).__name__}")
    if not value or value.startswith("-"):
        raise ValueError(f"invalid {name}: must be a non-empty value that does not start with '-'")
    matched = _PATH_CHARS_RE.fullmatch(value)
    if not matched:
        raise ValueError(f"invalid {name}: {value!r} contains disallowed characters")
    return matched.group(0)


def _sanitize_path(value: str, name: str) -> str:
    """Constrain *value* to a safe path/ref character set and return the matched
    substring.  Any character outside the set (spaces, quotes, ``$``, ``;``, etc.)
    is rejected, so a crafted report path can never be interpreted as extra shell
    or diff-cover arguments.
    """
    if not isinstance(value, str):
        raise ValueError(f"invalid {name}: expected a string, got {type(value).__name__}")
    if not value or value.startswith("-"):
        raise ValueError(f"invalid {name}: must be a non-empty value that does not start with '-'")
    matched = _PATH_CHARS_RE.fullmatch(value)
    if not matched:
        raise ValueError(f"invalid {name}: {value!r} contains disallowed characters")
    return matched.group(0)


def _is_excluded(path: str) -> bool:
    """Check if a file path matches any exclusion pattern."""
    from fnmatch import fnmatch

    return any(fnmatch(path, pattern) for pattern in _EXCLUDE_PATTERNS)


# ---------------------------------------------------------------------------
# Executable-line detection — used to exclude non-executable changed lines
# (comments, docstrings, blank lines) from the coverage denominator.
# ---------------------------------------------------------------------------


def _is_executable_python_line(line: str) -> bool:
    """Return True if a Python line is an executable statement.

    Uses ``ast.parse`` to distinguish real statements from comments,
    docstrings, and blank lines.  Lines that are syntactically non-executable
    (comment-only, string-only, blank) are excluded so they do not inflate
    the coverage denominator.

    Standalone string literals (e.g. ``'docstring'``) are treated as
    non-executable because coverage.py does not instrument them — they are
    data, not control flow.  Strings inside assignments, calls, or
    expressions ARE executable and are counted.

    This is a *single-line* predicate: it cannot see across lines, so body
    lines inside a multi-line docstring (which are not independently
    parseable and hit the lenient fallback below) would be counted as
    executable.  Filter a whole file's added lines with
    :func:`_iter_executable_python_lines` to track triple-quote state.

    The function is lenient on parse failure — lines that cannot be parsed
    are counted as executable to avoid under-counting.
    """
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return False
    # Standalone string literals (docstrings, bare strings) are not
    # instrumented by coverage.py.  Detect by checking if the entire
    # stripped line is a string literal (starts and ends with matching
    # quotes, no other code around it).
    if _PYTHON_STRING_LITERAL_RE.fullmatch(stripped):
        return False
    try:
        tree = ast.parse(stripped, mode="eval")
        return True
    except SyntaxError:
        try:
            tree = ast.parse(stripped, mode="exec")
            return bool(tree.body)
        except SyntaxError:
            return True


def _js_strip_line_comment(line: str) -> str:
    """Strip a trailing ``//`` comment and surrounding whitespace.

    Heuristic: a ``//`` inside a string literal is not distinguished, which
    is acceptable for the coverage gate's purpose of excluding clearly
    non-executable lines.
    """
    return re.sub(r"//[^\n]*$", "", line).strip()


def _is_executable_js_line(line: str) -> bool:
    """Return True if a single JS/TS line carries executable code.

    Removes blank lines, a trailing ``//`` comment, and a complete inline
    ``/* ... */`` block comment.  A block comment that *opens* on this line
    and closes on a later line is handled by
    :func:`_iter_executable_js_lines`, which tracks block-comment state
    across the added lines; this single-line predicate treats such a line as
    non-executable unless it carries code before the ``/*``.

    This is a heuristic — it does not handle every edge case (e.g. ``//``
    inside a string literal), but it is correct for the vast majority of
    real-world diffs and sufficient for the coverage gate's purpose of
    excluding clearly non-executable lines.
    """
    stripped = line.strip()
    if not stripped:
        return False
    start = stripped.find("/*")
    if start != -1:
        end = stripped.find("*/", start + 2)
        stripped = stripped[:start] + stripped[end + 2 :] if end != -1 else stripped[:start]
    return bool(_js_strip_line_comment(stripped))


def _py_multiline_opener(line: str) -> tuple[int, str] | None:
    """Return ``(index, delimiter)`` for an unclosed triple-quote in *line*.

    Scans left to right, skipping single-line strings and ``#`` comments, so
    a ``\"\"\"`` inside a comment or a normal string is not mistaken for a
    docstring opener.  Returns ``None`` when the line opens no multi-line
    string (or closes it on the same line).  Escape sequences are honoured
    inside short strings; triple-quoted strings are assumed not to contain
    an escaped delimiter.
    """
    i = 0
    n = len(line)
    while i < n:
        if line[i] == "#":
            return None
        if line.startswith("'''", i) or line.startswith('"""', i):
            quote = line[i : i + 3]
            close = line.find(quote, i + 3)
            if close == -1:
                return i, quote
            i = close + 3
            continue
        if line[i] in ("'", '"'):
            quote = line[i]
            i += 1
            while i < n:
                if line[i] == "\\":
                    i += 2
                    continue
                if line[i] == quote:
                    i += 1
                    break
                i += 1
            continue
        i += 1
    return None


def _scan_python_brackets(line: str, depth: int) -> int:
    """Update bracket nesting *depth* by scanning *line*.

    Skips brackets inside single-line strings, triple-quoted strings, and
    ``#`` comments, so a literal ``(``/``[``/``{`` in data never shifts the
    depth.  An unclosed triple-quote ends the scan: the rest of the line (and
    every following line until the delimiter closes) is string data.
    """
    i = 0
    n = len(line)
    while i < n:
        if line[i] == "#":
            return depth
        if line.startswith("'''", i) or line.startswith('"""', i):
            quote = line[i : i + 3]
            close = line.find(quote, i + 3)
            if close == -1:
                return depth
            i = close + 3
            continue
        if line[i] in ("'", '"'):
            quote = line[i]
            i += 1
            while i < n:
                if line[i] == "\\":
                    i += 2
                    continue
                if line[i] == quote:
                    i += 1
                    break
                i += 1
            continue
        if line[i] in "([{":
            depth += 1
        elif line[i] in ")]}" and depth > 0:
            depth -= 1
        i += 1
    return depth


def _python_line_state(
    line: str,
    depth: int,
    delimiter: str | None,
) -> tuple[int, str | None, bool, bool]:
    """Advance the Python bracket/string state by one *line*.

    Returns ``(depth, delimiter, continuation, bare_string)`` where
    *continuation* is True when the line begins inside an unclosed bracket or
    triple-quoted string (so it is not a statement start) and *bare_string* is
    True when the line opens a docstring with no code prefix.
    """
    if delimiter is not None:
        return depth, (None if delimiter in line else delimiter), True, False
    continuation = depth > 0
    opener = _py_multiline_opener(line)
    bare_string = False
    if opener is not None:
        index, quote = opener
        prefix = line[:index].strip().strip("rbfuRBFU").strip()
        if not prefix:
            # A bare string literal (docstring): the opening line itself is
            # data, not a statement.
            bare_string = True
            delimiter = quote
    depth = _scan_python_brackets(line, depth)
    if opener is not None and not bare_string:
        delimiter = opener[1]
    return depth, delimiter, continuation, bare_string


def _python_state_before(file_lines: Sequence[str], line_number: int) -> tuple[int, str | None]:
    """Return the bracket/string state just before *line_number* (1-based)."""
    depth: int = 0
    delimiter: str | None = None
    for context_line in file_lines[: max(0, line_number - 1)]:
        depth, delimiter, _, _ = _python_line_state(context_line, depth, delimiter)
    return depth, delimiter


def _iter_executable_python_lines(
    lines: Iterable[str],
    initial_depth: int = 0,
    initial_delimiter: str | None = None,
) -> Iterator[str]:
    """Yield the added Python *lines* that are executable statements.

    Tracks triple-quoted string state across lines so the body and closing
    delimiter of a multi-line docstring are not counted as executable even
    though, taken in isolation, they are not parseable statements.

    Also tracks bracket nesting: a line that continues a statement inside an
    unclosed ``(``/``[``/``{`` is not itself a statement, and coverage.py
    never instruments it.  Counting such continuation lines (e.g. entries
    inside a module-level data literal) inflated the gate's denominator and
    failed PRs that only added data.  Only lines whose statement *starts* at
    bracket depth zero are counted.

    *initial_depth* / *initial_delimiter* carry the state in from the file
    context preceding a diff hunk, so an added line inside a bracket that
    opened before the hunk is still recognised as a continuation.
    """
    delimiter = initial_delimiter
    depth = initial_depth
    for line in lines:
        depth, delimiter, continuation, bare_string = _python_line_state(line, depth, delimiter)
        if continuation or bare_string:
            continue
        if not _is_executable_python_line(line):
            continue
        yield line


def _iter_executable_js_lines(lines: Iterable[str]) -> Iterator[str]:
    """Yield the added JS/TS *lines* that carry executable code.

    Tracks ``/* ... */`` block-comment state across lines so JSDoc
    continuation lines and the closing ``*/`` are not counted as
    executable.
    """
    in_block = False
    for line in lines:
        if in_block:
            end = line.find("*/")
            if end == -1:
                continue
            in_block = False
            rest = line[end + 2 :]
            if _js_strip_line_comment(rest):
                yield rest
            continue
        start = line.find("/*")
        if start != -1 and line.find("*/", start + 2) == -1:
            in_block = True
            if _js_strip_line_comment(line[:start]):
                yield line
            continue
        if _is_executable_js_line(line):
            yield line


def _safe_repo_path(value: str) -> str | None:
    """Constrain a repo-relative path to a safe character set.

    Returns the matched value (so the taint analyser sees a string bounded by
    the regex) or ``None`` when the path is empty or contains disallowed
    characters.  Values that start with ``-`` are rejected so a path can never
    be mistaken for a CLI flag.
    """
    if not value or value.startswith("-"):
        return None
    matched = _PATH_CHARS_RE.fullmatch(value)
    return matched.group(0) if matched else None


def _diff_range(compare_branch: str) -> str:
    """Return the validated ``<compare_branch>...HEAD`` merge-base range.

    The three-dot range MUST match the one diff-cover uses internally
    (``GitDiffTool.diff_committed`` defaults to ``<compare_branch>...HEAD``).  A
    two-dot ``<compare_branch> HEAD`` range compares the tips instead, so every
    commit that landed on the base branch after this branch diverged shows up
    as a changed line that diff-cover never measures - inflating
    ``changed_lines`` and failing the gate with a bogus "N unmeasured lines"
    result even though the PR touched no production files at all.

    ``compare_branch`` is validated with ``_validate_ref`` (regex fullmatch,
    rejects a leading ``-``) before it is embedded, so the range cannot carry an
    injection payload or be read as an extra argument.
    """
    safe_ref = _validate_ref(compare_branch, "compare-branch")
    return f"{safe_ref}...HEAD"


def _count_python_added_lines(diff_text: str, filepath: str) -> int:
    """Count executable added Python lines, tracking state through each hunk.

    Each ``@@`` hunk is seeded with the bracket/triple-quote state recovered
    from the file content preceding the hunk, then every hunk line is replayed
    in order: context lines advance the new-file state (a hunk often starts
    mid-statement, so the state at the hunk header is not the state at the
    first added line), removed lines are ignored, and an added line is counted
    only when it is a statement start rather than a continuation.
    """
    try:
        file_lines = (REPO_ROOT / filepath).read_text(encoding="utf-8").splitlines()
    except OSError:
        file_lines = []

    depth: int = 0
    delimiter: str | None = None
    total = 0
    for raw in diff_text.splitlines():
        header = _HUNK_HEADER_RE.match(raw)
        if header:
            depth, delimiter = _python_state_before(file_lines, int(header.group(1)))
            continue
        if raw.startswith(("+++", "---", "\\")):
            continue
        if raw.startswith("+"):
            content = raw[1:]
            total += sum(1 for _ in _iter_executable_python_lines((content,), depth, delimiter))
            depth, delimiter, _, _ = _python_line_state(content, depth, delimiter)
        elif raw.startswith("-"):
            continue
        else:
            depth, delimiter, _, _ = _python_line_state(raw[1:], depth, delimiter)
    return total


def _count_added_lines(diff_range: str, filepath: str) -> int:
    """Count executable non-blank lines added to *filepath* within *diff_range*.

    Both arguments are regex fullmatch-bounded before they reach
    ``subprocess`` (resolves pythonsecurity:S8705).

    Only executable lines (statements, assignments, control flow, etc.) are
    counted.  Comments, docstrings, blank lines, and bracket/string
    continuation lines are excluded so they do not inflate the coverage
    denominator (FAR-962).
    """
    safe_range = _safe_repo_path(diff_range)
    safe_path = _safe_repo_path(filepath)
    if safe_range is None or safe_path is None:
        return 0
    try:
        result = subprocess.run(
            ["git", "diff", _DIFF_FILTER, safe_range, "--", safe_path],
            capture_output=True,
            text=True,
            check=False,
            cwd=str(REPO_ROOT),
        )
    except Exception:
        return 0
    if result.returncode != 0:
        return 0

    # Determine language from extension for executable-line filtering.
    is_python = filepath.endswith(".py")
    is_js = any(filepath.endswith(ext) for ext in (".ts", ".tsx", ".js", ".jsx", ".vue"))

    if is_python:
        return _count_python_added_lines(result.stdout, filepath)

    added = [
        diff_line[1:]
        for diff_line in result.stdout.splitlines()
        if diff_line.startswith("+") and not diff_line.startswith("+++")
    ]
    if is_js:
        return sum(1 for _ in _iter_executable_js_lines(added))
    return sum(1 for content in added if content.strip())


def _get_changed_production_files(compare_branch: str, language: str) -> dict[str, int]:
    """Return {filepath: executable_line_count} for changed production files.

    Diffs ``<compare_branch>...HEAD`` (three-dot / merge-base, matching
    diff-cover) to find changed files, then counts executable added lines per
    file.  Non-executable lines (comments, docstrings, blank lines) are
    excluded so they do not inflate the coverage denominator (FAR-962).
    Deleted lines are excluded by ``--diff-filter=ACM``.
    Files matching the exclusion patterns are skipped.  Returns an empty
    dict when the range is invalid or the diff fails.
    """
    pathspecs = _EXTENSIONS.get(language)
    if not pathspecs:
        return {}
    try:
        diff_range = _diff_range(compare_branch)
    except ValueError:
        return {}
    safe_range = _safe_repo_path(diff_range)
    if safe_range is None:
        return {}
    try:
        result = subprocess.run(
            ["git", "diff", _DIFF_FILTER, "--name-only", safe_range, "--", *pathspecs],
            capture_output=True,
            text=True,
            check=False,
            cwd=str(REPO_ROOT),
        )
    except Exception:
        return {}
    if result.returncode != 0:
        return {}

    files: dict[str, int] = {}
    for line in result.stdout.strip().splitlines():
        filepath = line.strip()
        if not filepath or _is_excluded(filepath):
            continue
        count = _count_added_lines(diff_range, filepath)
        if count > 0:
            files[filepath] = count
    return files


def _normalize_js_report(report_path: Path, src_root: str) -> Path | None:
    """Rewrite relative ``SF:`` entries in an LCOV report to absolute paths.

    vitest's v8 reporter emits source paths relative to the frontend project
    root (e.g. ``src/App.vue``), while diff-cover matches report paths against
    repo-relative git diff paths (``frontend/src/App.vue``).  Left as-is, no
    frontend file ever matches and every changed JS line is reported as
    unmeasured.  Resolving the relative entries against *src_root* (under the
    repo root) makes diff-cover relativise them back to the same repo-relative
    paths as the diff.

    ``report_path`` (a CLI-supplied path) and *src_root* are regex
    fullmatch-bounded before any file operation, and each rewritten ``SF:``
    entry must resolve inside *src_root* so report content can never escape it.

    Returns the path to a normalised copy when anything changed, otherwise
    ``None``.  The caller owns (and deletes) any returned temp file.
    """
    safe_report = Path(_sanitize_path(str(report_path), "js report"))
    safe_src_root = _sanitize_path(src_root, "js-src-root")
    root = (REPO_ROOT / safe_src_root).resolve()
    try:
        lines = safe_report.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None

    rewritten: list[str] = []
    changed = False
    for line in lines:
        raw = line[len("SF:") :] if line.startswith("SF:") else ""
        if raw and not Path(raw).is_absolute():
            candidate = (root / raw).resolve()
            if candidate.is_relative_to(root):
                rewritten.append(f"SF:{candidate}")
                changed = True
            else:
                rewritten.append(line)
        else:
            rewritten.append(line)
    if not changed:
        return None

    fd, tmp_name = tempfile.mkstemp(prefix="lcov-normalized-", suffix=".info")
    os.close(fd)
    tmp_path = Path(tmp_name)
    # tmp_path is a fresh file created by tempfile.mkstemp (not derived from any
    # input); only the (containment-checked) LCOV text content is written here.
    tmp_path.write_text("\n".join(rewritten) + "\n", encoding="utf-8")  # NOSONAR
    return tmp_path


# ---------------------------------------------------------------------------
# Branch coverage — raw report parsing for per-line branch taken/not-taken.
# ---------------------------------------------------------------------------


_CONDITION_COVERAGE_RE = re.compile(r"(\d+)%\s*\((\d+)/(\d+)\)")


def _parse_condition_coverage_attr(attr_value: str) -> tuple[int, int] | None:
    """Parse a ``condition-coverage`` attribute value like ``"100% (2/2)"``.

    Returns ``(covered, total)`` or ``None`` when the value is unparseable.
    """
    m = _CONDITION_COVERAGE_RE.fullmatch(attr_value.strip())
    if m is None:
        return None
    return int(m.group(2)), int(m.group(3))


def _parse_cobertura_branches(report_path: Path) -> dict[str, dict[int, tuple[int, int]]]:
    """Parse Cobertura XML for per-line branch coverage.

    Returns ``{filepath: {line_number: (covered_conditions, total_conditions)}}``.
    Only lines with ``branch="true"`` are included.

    Supports two coverage.py output formats:

    - **With ``<condition>`` children** (older or explicit ``--cov-branch``):
      each ``<condition>`` child is counted; covered when
      ``coverage="100%"``.
    - **Without ``<condition>`` children** (coverage.py ≥7.x default):
      the ``condition-coverage`` attribute carries the aggregated
      ``"XX% (N/M)"`` string — parse N/M directly.
    """
    result: dict[str, dict[int, tuple[int, int]]] = {}
    try:
        tree = ET.parse(str(report_path))  # noqa: S314 — internal trusted report
    except (ET.ParseError, OSError):
        return result
    root = tree.getroot()
    for cls in root.iter("class"):
        filename = cls.get("filename", "")
        lines_data: dict[int, tuple[int, int]] = {}
        for line_el in cls.iter("line"):
            if line_el.get("branch") != "true":
                continue
            line_no = int(line_el.get("number", "0"))
            # Prefer explicit <condition> children when present.
            conditions = line_el.findall(".//condition")
            if conditions:
                total = len(conditions)
                covered = sum(1 for c in conditions if c.get("coverage") == "100%")
            else:
                # Fallback: parse the condition-coverage attribute
                # (e.g. "100% (2/2)" or "50% (1/2)").
                attr = line_el.get("condition-coverage", "")
                parsed = _parse_condition_coverage_attr(attr)
                if parsed is None:
                    continue
                covered, total = parsed
            lines_data[line_no] = (covered, total)
        if lines_data:
            result[filename] = lines_data
    return result


def _parse_lcov_count(value: str) -> int:
    """Parse a numeric LCOV field, treating a malformed value as 0.

    LCOV uses ``-`` for "never executed" and a decimal count otherwise.  A
    malformed (non-numeric) value must not crash the gate — it cannot be shown
    to be covered, so it fail-closes to 0, exactly like ``-``.
    """
    try:
        return int(value)
    except ValueError:
        return 0


def _parse_lcov_branches(report_path: Path) -> dict[str, dict[int, tuple[int, int]]]:
    """Parse LCOV ``BRDA`` records for per-line branch coverage.

    Returns ``{filepath: {line_number: (taken_branches, total_branches)}}``.
    Each ``BRDA:<line>,<block>,<branch>,<taken>`` record is attributed to
    its line.  ``taken`` is ``"-"`` when the branch was never executed
    (uncovered).  ``taken`` > 0 means covered.
    """
    result: dict[str, dict[int, tuple[int, int]]] = {}
    try:
        lines = report_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return result
    current_file = ""
    for raw in lines:
        if raw.startswith("SF:"):
            current_file = raw[3:]
        elif raw.startswith("BRDA:"):
            parts = raw[5:].split(",")
            if len(parts) < 4:
                continue
            line_no = _parse_lcov_count(parts[0])
            if line_no <= 0:
                continue
            taken_str = parts[3].strip()
            if current_file not in result:
                result[current_file] = {}
            prev = result[current_file].get(line_no, (0, 0))
            total = prev[1] + 1
            covered = prev[0] + (1 if _parse_lcov_count(taken_str) > 0 else 0)
            result[current_file][line_no] = (covered, total)
    return result


def _parse_added_line_numbers(diff_text: str) -> set[int]:
    """Extract new-file line numbers of added lines from ``git diff --unified=0``.

    Parses ``@@ -old,count +new,count @@`` hunk headers and counts ``+``
    lines within each hunk.
    """
    added: set[int] = set()
    current_line = 0
    for raw in diff_text.splitlines():
        m = _HUNK_HEADER_RE.match(raw)
        if m:
            current_line = int(m.group(1))
            continue
        if raw.startswith(("+++", "---", "\\")):
            continue
        if raw.startswith("+"):
            added.add(current_line)
            current_line += 1
        elif raw.startswith("-"):
            continue
        else:
            current_line += 1
    return added


def _match_report_key_to_changed(
    raw_key: str,
    changed_files: Iterable[str],
    language: str,
    js_src_root: str = DEFAULT_JS_SRC_ROOT,
) -> str | None:
    """Return the repo-relative changed-file key that *raw_key* names.

    Coverage reports do not carry repo-relative paths.  coverage.py emits
    Cobertura ``filename`` values relative to its ``source`` root (this repo:
    ``src/modulo``), and vitest emits LCOV ``SF:`` values relative to
    ``frontend/`` (the gate normalises the report to absolute paths before
    evaluation).  The changed-file keys from ``git diff`` are always
    repo-relative (``backend/src/modulo/...``, ``frontend/src/...``), so a
    naive ``branch_data.get(filepath)`` never matches and every changed JS /
    Python production file is scored as ``branch_total == 0`` — a false 0%
    branch failure regardless of real coverage.  Resolve *raw_key* into the
    changed-file namespace by exact match, then by a unique path-segment
    suffix match; return ``None`` when nothing matches.
    """
    changed = {path.replace("\\", "/") for path in changed_files}
    normalized = raw_key.replace("\\", "/")
    candidates: list[str] = []
    if Path(normalized).is_absolute():
        with contextlib.suppress(ValueError, OSError):
            candidates.append(Path(normalized).resolve().relative_to(REPO_ROOT).as_posix())
    else:
        candidates.append(normalized)
        prefix = "backend" if language == "Python" else js_src_root
        candidates.append(f"{prefix}/{normalized}")

    for candidate in candidates:
        if candidate in changed:
            return candidate

    # The report path may be relative to a deeper source root (coverage.py
    # relativises to ``src/modulo``), so ``core/foo.py`` names
    # ``backend/src/modulo/core/foo.py``.  Match on whole path segments and
    # require a unique hit so a shared basename can never cross-attribute.
    for candidate in sorted(candidates, key=len, reverse=True):
        suffix = "/" + candidate.lstrip("/")
        matches = [path for path in changed if path == candidate or path.endswith(suffix)]
        if len(matches) == 1:
            return matches[0]
    return None


def _normalize_branch_data_keys(
    branch_data: dict[str, dict[int, tuple[int, int]]],
    changed_files: Iterable[str],
    language: str,
    js_src_root: str = DEFAULT_JS_SRC_ROOT,
) -> dict[str, dict[int, tuple[int, int]]]:
    """Re-key raw report branch data onto the repo-relative changed-file keys.

    Report entries that do not correspond to a changed production file can
    never contribute to the gate and are dropped.  Entries that map to the
    same changed file are merged, so a report listing a file under more than
    one spelling counts each line only once.
    """
    normalized: dict[str, dict[int, tuple[int, int]]] = {}
    for raw_key, line_branches in branch_data.items():
        target = _match_report_key_to_changed(raw_key, changed_files, language, js_src_root)
        if target is None:
            continue
        normalized.setdefault(target, {}).update(line_branches)
    return normalized


def _compute_branch_coverage_from_raw(
    changed_files: dict[str, int],
    report_path: Path | None,
    compare_branch: str,
    language: str,
    js_src_root: str = DEFAULT_JS_SRC_ROOT,
) -> tuple[int, int, int]:
    """Parse raw coverage report and compute branch coverage on changed lines.

    Uses ``git diff --unified=0`` to find the exact added line numbers, then
    intersects with branch records from the raw report.  Report file keys are
    re-aligned to the repo-relative changed-file keys (see
    :func:`_normalize_branch_data_keys`) because the report's paths come from
    a different working directory than the diff.

    Returns ``(branch_covered, branch_total, branch_unmeasured)``.
    """
    if report_path is None or not report_path.exists():
        return 0, 0, sum(changed_files.values())

    diff_range = f"{compare_branch}...HEAD"
    safe_range = _safe_repo_path(diff_range)
    if safe_range is None:
        return 0, 0, sum(changed_files.values())

    added_lines: dict[str, set[int]] = {}
    for filepath in changed_files:
        safe_path = _safe_repo_path(filepath)
        if safe_path is None:
            continue
        try:
            result = subprocess.run(
                ["git", "diff", "--unified=0", safe_range, "--", safe_path],
                capture_output=True,
                text=True,
                check=False,
                cwd=str(REPO_ROOT),
            )
            if result.returncode == 0 and result.stdout:
                added_lines[filepath] = _parse_added_line_numbers(result.stdout)
        except Exception:  # noqa: S112 — best-effort per-file parse, skip on any error
            continue

    raw_branch_data = (
        _parse_cobertura_branches(report_path) if language == "Python" else _parse_lcov_branches(report_path)
    )
    branch_data = _normalize_branch_data_keys(raw_branch_data, changed_files, language, js_src_root)

    branch_covered = 0
    branch_total = 0
    branch_unmeasured = 0

    for filepath, changed_count in changed_files.items():
        file_added = added_lines.get(filepath, set())
        file_branch = branch_data.get(filepath, {})

        if file_branch:
            for line_no, (covered, total) in file_branch.items():
                if line_no in file_added:
                    branch_covered += covered
                    branch_total += total
            measured_lines = {ln for ln in file_branch if ln in file_added}
            branch_unmeasured += len(file_added - measured_lines)
        else:
            branch_unmeasured += len(file_added) if file_added else changed_count

    return branch_covered, branch_total, branch_unmeasured


def _compute_project_wide_metrics_cobertura(report_path: Path) -> tuple[float, float | None] | None:
    """Compute project-wide line and branch coverage from Cobertura XML.

    Returns ``(line_pct, branch_pct)`` or ``None`` on parse error.
    ``branch_pct`` is ``None`` when the report carries no branch records at
    all (no branch data to measure), which is distinct from ``0.0`` (branch
    records exist but none are covered).  The caller must not treat the two
    as equivalent: the project floor is only meaningful when branch data
    actually exists.

    Branch data is read from two sources, in order of preference:

    1. **Root ``<coverage>`` attributes** (``branches-covered`` /
       ``branches-valid``): coverage.py ≥7.x always emits these when branch
       measurement is enabled — the authoritative, pre-aggregated values.
    2. **Per-line ``condition-coverage`` attributes**: parsed from
       ``<line branch="true" condition-coverage="XX% (N/M)"/>`` when the
       root attributes are absent (backward compatibility).
    3. **``<condition>`` child elements**: legacy format; tried last.
    """
    try:
        tree = ET.parse(str(report_path))  # noqa: S314 — internal trusted report
    except (ET.ParseError, OSError):
        return None
    root = tree.getroot()
    total_lines = 0
    covered_lines = 0
    for line_el in root.iter("line"):
        hits = int(line_el.get("hits", "0"))
        total_lines += 1
        if hits > 0:
            covered_lines += 1
    line_pct = (covered_lines / total_lines * 100.0) if total_lines else 0.0

    # --- Branch coverage: prefer root-level attributes (pre-aggregated) ---
    branch_pct: float | None = None
    branches_covered_attr = root.get("branches-covered")
    branches_valid_attr = root.get("branches-valid")
    if branches_covered_attr is not None and branches_valid_attr is not None:
        bv = int(branches_valid_attr)
        bc = int(branches_covered_attr)
        if bv > 0:
            branch_pct = (bc / bv) * 100.0
        elif bc == 0 and bv == 0:
            branch_pct = None  # no branches at all
        else:
            branch_pct = 0.0
    else:
        # Fallback: count from per-line data (condition-coverage attr or
        # <condition> children — _parse_cobertura_branches handles both).
        total_conditions = 0
        covered_conditions = 0
        for line_el in root.iter("line"):
            if line_el.get("branch") != "true":
                continue
            # Try explicit <condition> children first.
            conditions = line_el.findall(".//condition")
            if conditions:
                total_conditions += len(conditions)
                covered_conditions += sum(1 for c in conditions if c.get("coverage") == "100%")
            else:
                # Parse condition-coverage attribute (e.g. "50% (1/2)").
                attr = line_el.get("condition-coverage", "")
                parsed = _parse_condition_coverage_attr(attr)
                if parsed is not None:
                    covered, total = parsed
                    total_conditions += total
                    covered_conditions += covered
        branch_pct = (covered_conditions / total_conditions * 100.0) if total_conditions else None
    return line_pct, branch_pct


def _compute_project_wide_metrics_lcov(report_path: Path) -> tuple[float, float | None] | None:
    """Compute project-wide line and branch coverage from LCOV.

    Returns ``(line_pct, branch_pct)`` or ``None`` on parse error.
    ``branch_pct`` is ``None`` when the report carries no ``BRDA`` records at
    all — see :func:`_compute_project_wide_metrics_cobertura`.
    """
    try:
        lines = report_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    total_lines = 0
    hit_lines = 0
    total_branches = 0
    hit_branches = 0
    for raw in lines:
        if raw.startswith("DA:"):
            parts = raw[3:].split(",")
            if len(parts) >= 2:
                total_lines += 1
                if _parse_lcov_count(parts[1]) > 0:
                    hit_lines += 1
        elif raw.startswith("BRDA:"):
            parts = raw[5:].split(",")
            if len(parts) >= 4:
                total_branches += 1
                if _parse_lcov_count(parts[3].strip()) > 0:
                    hit_branches += 1
    line_pct = (hit_lines / total_lines * 100.0) if total_lines else 0.0
    branch_pct = (hit_branches / total_branches * 100.0) if total_branches else None
    return line_pct, branch_pct


@dataclass(frozen=True)
class GateResult:
    """Result of evaluating one language's coverage gate."""

    language: str
    skipped: bool
    skip_reason: str
    passed: bool
    actual_pct: float | None
    threshold: int
    changed_lines: int = 0
    tiny_diff: bool = False
    measured_lines: int = 0
    unmeasured_lines: int = 0
    # Branch coverage fields
    branch_covered: int = 0
    branch_total: int = 0
    # Changed lines with no branch record.  Diagnostic only: the parsers keep
    # only branch records, so this cannot distinguish "no branch on the line"
    # from "branch record missing", and it never drives the pass/fail decision
    # (a zero ``branch_total`` is a vacuous pass — see ``evaluate``).
    branch_unmeasured: int = 0
    branch_actual_pct: float | None = None
    branch_passed: bool | None = None  # None = no branch data (vacuous)
    branch_threshold: int = 0

    def summary(self) -> str:
        if self.skipped:
            return f"[{self.language}] SKIPPED — {self.skip_reason}"
        if self.tiny_diff:
            # Tiny diffs are line-only by design: ``evaluate`` returns before
            # measuring branches, so ``branch_passed`` is always None and there
            # is no branch status to surface here.
            return f"[{self.language}] PASS — tiny diff ({self.changed_lines} lines, ≤{TINY_DIFF_THRESHOLD} threshold)"
        if self.unmeasured_lines > 0 and not self.passed:
            line_part = (
                f"[{self.language}] FAIL — {self.actual_pct:.1f}% effective coverage, "
                f"{self.unmeasured_lines} unmeasured line(s) at 0% < {self.threshold}%"
            )
        elif self.passed:
            pct = f"{self.actual_pct:.1f}%" if self.actual_pct is not None else "?"
            line_part = f"[{self.language}] PASS — {pct} >= {self.threshold}%"
        elif self.actual_pct is not None:
            line_part = f"[{self.language}] FAIL — {self.actual_pct:.1f}% < {self.threshold}%"
        else:
            line_part = f"[{self.language}] FAIL — {self.skip_reason}"
        if self.branch_passed is None:
            branch_part = " (vacuous — no branch records on changed lines)"
        elif self.branch_actual_pct is not None:
            bpct = f"{self.branch_actual_pct:.1f}%"
            if self.branch_passed:
                branch_part = f" | branch {bpct} >= {self.branch_threshold}%"
            else:
                branch_part = f" | branch {bpct} < {self.branch_threshold}%"
        else:
            branch_part = ""
        return line_part + branch_part


def _run_diff_cover(
    report_path: Path,
    compare_branch: str,
    fail_under: int,
) -> tuple[int, str]:
    """Run ``diff-cover`` and return (exit_code, combined_output).

    ``compare_branch`` is validated before it reaches ``subprocess`` so a
    caller-supplied value can never be interpreted as an extra flag.
    """
    exe_dir = Path(sys.executable).parent
    diff_cover_bin = exe_dir / "diff-cover"
    if sys.platform == "win32":
        diff_cover_bin = exe_dir / "diff-cover.exe"

    safe_compare_branch = _validate_ref(compare_branch, "compare-branch")
    safe_report = _sanitize_path(str(report_path), "report path")
    cmd = [  # NOSONAR - compare-branch is regex fullmatch-bounded by _validate_ref and the report path by _sanitize_path (both reject values starting with '-'); subprocess has no shell
        str(diff_cover_bin),
        safe_report,
        "--compare-branch",
        safe_compare_branch,
        "--fail-under",
        str(fail_under),
    ]
    result = subprocess.run(  # NOSONAR - reachable argv = [diff_cover_bin (resolved from the synced venv, not caller input), safe_report (regex fullmatch-bounded by _sanitize_path, rejects leading '-'), "--compare-branch" (literal), safe_compare_branch (regex fullmatch-bounded by _validate_ref, rejects leading '-'), "--fail-under" (literal), str(fail_under) (int->str literal)]; subprocess runs without a shell, so no flag injection is reachable from the only caller-influenced elements (safe_report/safe_compare_branch), which are validated before reaching this line.
        cmd,
        capture_output=True,
        text=True,
        check=False,
        cwd=str(REPO_ROOT),
    )
    combined = result.stdout
    if result.stderr:
        combined += "\n" + result.stderr
    return result.returncode, combined


def _get_diff_cover_json(report_path: Path, compare_branch: str) -> dict | None:
    """Run diff-cover with ``--format json:<path>`` and return parsed JSON.

    Returns the JSON report dict containing ``src_stats`` (per-file measured
    coverage), ``total_num_lines`` (lines diff-cover measured), and
    ``num_changed_lines`` (total lines in the diff).  Returns None on error.
    """
    exe_dir = Path(sys.executable).parent
    diff_cover_bin = exe_dir / "diff-cover"
    if sys.platform == "win32":
        diff_cover_bin = exe_dir / "diff-cover.exe"

    safe_compare_branch = _validate_ref(compare_branch, "compare-branch")
    safe_report = _sanitize_path(str(report_path), "report path")
    json_report_path = REPO_ROOT / ".diff-cover-report.json"
    safe_json_path = _sanitize_path(str(json_report_path), "json report path")

    cmd = [  # NOSONAR
        str(diff_cover_bin),
        safe_report,
        "--compare-branch",
        safe_compare_branch,
        "--format",
        f"json:{safe_json_path}",
    ]
    subprocess.run(  # NOSONAR
        cmd,
        capture_output=True,
        text=True,
        check=False,
        cwd=str(REPO_ROOT),
    )

    if not json_report_path.exists():
        return None
    try:
        return json.loads(json_report_path.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    finally:
        with contextlib.suppress(OSError):
            json_report_path.unlink()


def _production_coverage_from_json(changed_files: dict[str, int], json_data: dict | None) -> tuple[int, int] | None:
    """Return ``(covered_lines, measured_lines)`` over production files only.

    diff-cover's top-level totals cover its *whole* diff - including test files
    the gate excludes - so pairing them with a production-only denominator can
    push coverage above 100%.  Instead, sum the per-file ``covered_lines`` and
    ``violation_lines`` from ``src_stats``, restricted to the production files
    the gate counts.  Each file's measured lines are capped at its changed-line
    count so a report measuring more lines than the gate counted cannot inflate
    the result.

    Only files *present* in ``src_stats`` contribute: ``covered_lines`` +
    ``violation_lines`` is exactly the set of changed lines the coverage report
    can account for, so non-instrumentable lines (static ``.vue`` template
    markup, comments) are not counted against the file.  Files absent from the
    report contribute nothing here — ``evaluate`` counts every one of their
    changed lines as unmeasured so a brand-new untested file still fails.

    Returns ``None`` when *json_data* carries no ``src_stats`` mapping.
    """
    if not json_data or not isinstance(json_data.get("src_stats"), dict):
        return None
    src_stats = json_data["src_stats"]
    covered = 0
    measured = 0
    for path, changed in changed_files.items():
        stats = src_stats.get(path)
        if not isinstance(stats, dict):
            continue
        file_covered = len(stats.get("covered_lines") or [])
        file_missing = len(stats.get("violation_lines") or [])
        file_measured = min(file_covered + file_missing, changed)
        covered += min(file_covered, file_measured)
        measured += file_measured
    return covered, measured


def _unmeasured_file_lines(changed_files: dict[str, int], json_data: dict | None) -> int:
    """Count changed lines in production files ABSENT from the coverage report.

    The "unmeasured lines at 0%" penalty exists to fail a brand-new production
    file that never reaches the report (see the module docstring).  It must NOT
    also punish the non-executable lines *inside* a file that IS measured:
    ``git diff`` counts every non-blank added line (Pydantic field annotations,
    ``@router`` decorators, multi-line call continuations, ...) while
    coverage.py only records executable statements, so subtracting the two
    charged every annotation to the file as uncovered and made a fully-tested
    route module mathematically unable to clear the threshold.

    Returns the summed changed-line count of files with no ``src_stats`` entry.
    """
    if not json_data or not isinstance(json_data.get("src_stats"), dict):
        return 0
    src_stats = json_data["src_stats"]
    return sum(changed for path, changed in changed_files.items() if path not in src_stats)


def _production_coverage_from_text(output: str, changed_lines: int) -> tuple[float, int, int] | None:
    """Fallback coverage parse from diff-cover's text output.

    Returns ``(effective_pct, measured_lines, unmeasured_lines)`` or ``None``
    when the output carries no coverage line.  The ``Total:`` line gives the
    number of lines diff-cover measured; anything the gate counted beyond that
    is unmeasured and scored at 0%.
    """
    coverage_match = _COVERAGE_LINE_RE.search(output)
    if coverage_match is None:
        return None
    measured_pct_val = float(coverage_match.group(1))
    total_match = _DIFF_COVER_TOTAL_RE.search(output)
    measured_lines = min(int(total_match.group(1)), changed_lines) if total_match else changed_lines
    unmeasured_lines = max(0, changed_lines - measured_lines)
    effective_pct = measured_pct_val * (measured_lines / changed_lines) if changed_lines else 0.0
    return effective_pct, measured_lines, unmeasured_lines


def evaluate(
    language: str,
    report_path: Path | None,
    compare_branch: str,
    fail_under: int,
    *,
    allow_missing: bool = False,
    branch_fail_under: int = BRANCH_COVERAGE_THRESHOLD,
    js_src_root: str = DEFAULT_JS_SRC_ROOT,
) -> GateResult:
    """Evaluate one language's changed-lines coverage (line + branch).

    When *allow_missing* is False (the CI default), a missing or unreadable
    report is a gate failure — the upstream job or artifact download broke.
    When True (local convenience), a missing report is a skip.

    The gate detects unmeasured files by comparing the executable changed
    production lines (from ``git diff``, excluding comments/docstrings/blanks)
    against the production-only lines diff-cover measured.  Lines it did not
    measure count as 0% coverage, and the numerator and denominator always
    come from the same production-only file set.
    """
    # --- Get changed production files and count executable added lines ---
    changed_files = _get_changed_production_files(compare_branch, language)
    changed_lines = sum(changed_files.values())

    # --- Tiny-diff exemption ---
    if 0 < changed_lines <= TINY_DIFF_THRESHOLD:
        return GateResult(
            language=language,
            skipped=False,
            skip_reason="",
            passed=True,
            actual_pct=None,
            threshold=fail_under,
            changed_lines=changed_lines,
            tiny_diff=True,
        )

    # --- No changed lines → skip ---
    if changed_lines == 0:
        return GateResult(
            language=language,
            skipped=True,
            skip_reason="no changed production lines in this diff",
            passed=True,
            actual_pct=None,
            threshold=fail_under,
            changed_lines=0,
        )

    # --- Missing report ---
    if report_path is None or not report_path.exists():
        if allow_missing:
            return GateResult(
                language=language,
                skipped=True,
                skip_reason=f"no coverage report found at {report_path} (allow-missing)",
                passed=True,
                actual_pct=None,
                threshold=fail_under,
                changed_lines=changed_lines,
            )
        return GateResult(
            language=language,
            skipped=False,
            skip_reason=f"missing coverage report: {report_path}",
            passed=False,
            actual_pct=None,
            threshold=fail_under,
            changed_lines=changed_lines,
        )

    # --- Run diff-cover with JSON report to detect unmeasured lines ---
    json_data = _get_diff_cover_json(report_path.resolve(), compare_branch)
    rc, output = _run_diff_cover(report_path.resolve(), compare_branch, fail_under)

    # --- diff-cover found no coverage data for the changed files -> FAIL ---
    # Reaching here means changed_lines > 0 (zero changed production lines
    # already skipped above), so every changed line is unmeasured: 0%.
    if _NO_CHANGED_LINES_RE.search(output):
        return GateResult(
            language=language,
            skipped=False,
            skip_reason="no coverage data for changed lines (0% effective)",
            passed=False,
            actual_pct=0.0,
            threshold=fail_under,
            changed_lines=changed_lines,
            measured_lines=0,
            unmeasured_lines=changed_lines,
            branch_actual_pct=0.0,
            branch_passed=False,
            branch_threshold=branch_fail_under,
        )

    # --- Extract measured coverage (production files only) ---
    # changed_lines > 0 here: the zero case returned above, so the divisions
    # below are safe.
    measured_pct: float | None = None
    measured_lines = 0
    unmeasured_lines = 0
    # Denominator actually scored by the gate.  For files present in the report
    # this is their coverable changed lines; for files absent from the report it
    # is every changed line (scored at 0%).  Reported as ``changed_lines`` in the
    # summary so ``Measured + Unmeasured == Changed Lines`` stays coherent.
    scored_lines = changed_lines

    src_stats = json_data.get("src_stats") if isinstance(json_data, dict) else None
    production_stats = _production_coverage_from_json(changed_files, json_data)
    if production_stats is not None and isinstance(src_stats, dict):
        covered_lines, measured_lines = production_stats
        # Only production files ABSENT from the report are "unmeasured" and
        # scored at 0% (brand-new untested file detection).  Non-executable
        # lines inside a measured file are simply not part of the coverage
        # denominator (see _unmeasured_file_lines).
        unmeasured_lines = _unmeasured_file_lines(changed_files, json_data)
        scored_lines = measured_lines + unmeasured_lines
        measured_pct = (covered_lines / scored_lines) * 100.0 if scored_lines else None
    else:
        # Fallback: parse the text output (with the measured-line count so
        # unmeasured lines are still detected when the JSON report is absent).
        text_stats = _production_coverage_from_text(output, changed_lines)
        if text_stats is not None:
            measured_pct, measured_lines, unmeasured_lines = text_stats

    # --- Determine pass/fail ---
    if measured_pct is not None:
        passed = measured_pct >= fail_under
        if not passed and unmeasured_lines > 0:
            reason = f"coverage {measured_pct:.1f}% (with {unmeasured_lines} unmeasured lines at 0%) is below threshold {fail_under}%"
        elif not passed:
            reason = f"coverage {measured_pct:.1f}% is below threshold {fail_under}%"
        elif unmeasured_lines > 0:
            reason = (
                f"coverage {measured_pct:.1f}% >= {fail_under}% (but {unmeasured_lines} unmeasured lines counted at 0%)"
            )
        else:
            reason = ""
    elif rc == 0:
        passed = False
        reason = f"diff-cover exited 0 but produced no parseable coverage result for {language}"
    elif _THRESHOLD_NOT_MET_RE.search(output):
        passed = False
        reason = f"coverage below threshold {fail_under}%"
    else:
        passed = False
        error_lines = [ln.strip() for ln in output.strip().splitlines() if ln.strip()]
        reason = error_lines[-1] if error_lines else "diff-cover returned non-zero"

    # --- Branch coverage on changed lines ---
    branch_covered, branch_total, branch_unmeasured = _compute_branch_coverage_from_raw(
        changed_files,
        report_path,
        compare_branch,
        language,
        js_src_root,
    )
    branch_actual_pct: float | None = None
    branch_passed_val: bool | None = None
    if branch_total == 0:
        # No branch records on any changed line: there is no branch to
        # measure, so the branch gate passes vacuously.  The report parsers
        # retain only branch records, so a changed line that simply has no
        # branch (straight-line code) is indistinguishable from one whose
        # branch record is absent — neither carries a branch into the
        # denominator.  Treating the former as 0% spuriously failed every
        # branch-free diff of more than TINY_DIFF_THRESHOLD lines.
        # ``branch_unmeasured`` is retained as a diagnostic only; it never
        # drives the pass/fail decision.  Genuinely unexercised branches
        # still surface as ``branch_total > 0`` with 0% coverage, and a
        # brand-new untested file still fails the line gate.
        branch_passed_val = None  # vacuous
    else:
        branch_actual_pct = (branch_covered / branch_total) * 100.0
        branch_passed_val = branch_actual_pct >= branch_fail_under

    return GateResult(
        language=language,
        skipped=False,
        skip_reason=reason,
        passed=passed,
        actual_pct=measured_pct,
        threshold=fail_under,
        changed_lines=scored_lines,
        measured_lines=measured_lines,
        unmeasured_lines=unmeasured_lines,
        branch_covered=branch_covered,
        branch_total=branch_total,
        branch_unmeasured=branch_unmeasured,
        branch_actual_pct=branch_actual_pct,
        branch_passed=branch_passed_val,
        branch_threshold=branch_fail_under,
    )


def _write_summary(results: list[GateResult]) -> None:
    """Write a GitHub step summary table and emit ::error:: annotations on failure."""
    summary_lines = [
        "## Coverage Gate (Changed Lines)\n",
        "| Language | Status | Line % | Line Thr | Branch % | Branch Thr | Changed Lines | Measured | Unmeasured |",
        "|----------|--------|--------|----------|----------|------------|---------------|----------|------------|",
    ]
    for r in results:
        if r.skipped:
            status = "SKIPPED"
            pct = "—"
            bpct = "—"
        elif r.tiny_diff:
            # Tiny diffs are line-only (branch is never measured); there is no
            # branch status to display.  See ``GateResult.summary``.
            status = "PASS (tiny)"
            pct = "—"
            bpct = "—"
        elif r.passed:
            status = "PASS" if (r.branch_passed is not False) else "FAIL (branch)"
            pct = f"{r.actual_pct:.1f}%" if r.actual_pct is not None else "?"
            bpct = f"{r.branch_actual_pct:.1f}%" if r.branch_actual_pct is not None else "—"
        else:
            status = "FAIL"
            pct = f"{r.actual_pct:.1f}%" if r.actual_pct is not None else "N/A"
            bpct = f"{r.branch_actual_pct:.1f}%" if r.branch_actual_pct is not None else "—"
        bthr = f"{r.branch_threshold}%" if r.branch_threshold else "—"
        summary_lines.append(
            f"| {r.language} | {status} | {pct} | {r.threshold}% "
            f"| {bpct} | {bthr} "
            f"| {r.changed_lines} | {r.measured_lines} | {r.unmeasured_lines} |"
        )

    summary_lines.append("")

    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        with Path(step_summary).open("a") as f:
            f.write("\n".join(summary_lines) + "\n")

    print("\n".join(summary_lines))

    for r in results:
        if not r.skipped and not r.passed and not r.tiny_diff:
            msg = f"[{r.language}] Coverage gate failed: {r.summary()}"
            print(f"::error::{msg}")
        elif r.branch_passed is False:
            msg = f"[{r.language}] Branch coverage gate failed: {r.summary()}"
            print(f"::error::{msg}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Changed-lines coverage gate using diff-cover.",
    )
    parser.add_argument(
        "--compare-branch",
        default="origin/main",
        help="Branch to diff against (default: origin/main).",
    )
    parser.add_argument(
        "--fail-under",
        type=int,
        default=COVERAGE_THRESHOLD,
        help=f"Minimum coverage percentage for changed lines (default: {COVERAGE_THRESHOLD}).",
    )
    parser.add_argument(
        "--branch-fail-under",
        type=int,
        default=BRANCH_COVERAGE_THRESHOLD,
        help=f"Minimum branch coverage percentage for changed lines (default: {BRANCH_COVERAGE_THRESHOLD}).",
    )
    parser.add_argument(
        "--python-report",
        type=Path,
        default=None,
        help="Path to the backend Cobertura XML coverage report.",
    )
    parser.add_argument(
        "--js-report",
        type=Path,
        default=None,
        help="Path to the frontend LCOV coverage report.",
    )
    parser.add_argument(
        "--js-src-root",
        default=DEFAULT_JS_SRC_ROOT,
        help=(
            "Directory (relative to the repo root) that the LCOV report's "
            f"relative SF paths resolve against (default: {DEFAULT_JS_SRC_ROOT})."
        ),
    )
    parser.add_argument(
        "--allow-missing-reports",
        action="store_true",
        default=False,
        help="Exit 0 when a report file is missing (for local use only; CI must NOT pass this).",
    )
    args = parser.parse_args()

    # Resolve default report paths relative to the repo root
    default_python = REPO_ROOT / "backend" / "coverage.xml"
    default_js = REPO_ROOT / "frontend" / "coverage" / "lcov.info"

    if args.python_report is not None:
        python_report = Path(_sanitize_path(str(args.python_report), "python-report"))
    elif default_python.exists():
        python_report = default_python
    else:
        python_report = None

    if args.js_report is not None:
        js_report = Path(_sanitize_path(str(args.js_report), "js-report"))
    elif default_js.exists():
        js_report = default_js
    else:
        js_report = None

    # Normalise the LCOV report so diff-cover can match its paths (see
    # _normalize_js_report).  The temp file, if any, is cleaned up below.
    #
    # Keep the ORIGINAL report path: the project-wide floor is computed after
    # the finally block has unlinked the normalised temp copy, so it must read
    # the raw report (which still exists) rather than ``js_report``.  In CI the
    # vitest LCOV always uses relative ``SF:`` paths, so ``_normalize_js_report``
    # always returns a temp file and ``js_report`` always points at the deleted
    # copy — reading it would silently skip the JavaScript project-wide floor on
    # every run.
    raw_js_report = js_report
    normalised_js_report: Path | None = None
    js_src_root = _sanitize_path(args.js_src_root, "js-src-root")
    if js_report is not None and js_report.exists():
        normalised = _normalize_js_report(js_report, js_src_root)
        if normalised is not None:
            normalised_js_report = normalised
            js_report = normalised

    results: list[GateResult] = []
    try:
        for language, report in (("Python", python_report), ("JavaScript", js_report)):
            results.append(
                evaluate(
                    language,
                    report,
                    args.compare_branch,
                    args.fail_under,
                    allow_missing=args.allow_missing_reports,
                    branch_fail_under=args.branch_fail_under,
                    js_src_root=js_src_root,
                )
            )
    finally:
        if normalised_js_report is not None:
            with contextlib.suppress(OSError):
                normalised_js_report.unlink()

    # --- Branch coverage summary ---
    any_branch_failed = any(r.branch_passed is False for r in results)

    # --- Project-wide floor check (the ratchet) ---
    # Floors are a ratchet: raise as coverage improves, never lower.
    # Lowering requires an explicit, justified commit message.
    project_floor_failed = False
    project_metrics: list[str] = []
    if python_report is not None and python_report.exists():
        py_metrics = _compute_project_wide_metrics_cobertura(python_report)
        if py_metrics is not None:
            pl, pb = py_metrics
            branch_display = f"{pb:.1f}%" if pb is not None else "n/a (no branch data)"
            project_metrics.append(f"  Python project-wide: line {pl:.1f}%, branch {branch_display}")
            if pl < MIN_PROJECT_LINE_COVERAGE:
                project_floor_failed = True
                project_metrics.append(
                    f"  BREACH: Python line {pl:.1f}% < floor {MIN_PROJECT_LINE_COVERAGE}% — "
                    "fix coverage (floors are a ratchet and must not be lowered)"
                )
            # Branch data must be present.  A report with no branch data at all
            # is indistinguishable from one whose branch data was dropped — fail
            # closed with its own distinct reason, not a floor breach.
            if pb is None:
                project_floor_failed = True
                project_metrics.append(
                    "  BREACH: Python branch data missing from Cobertura report "
                    "(no branch-rate attributes or <condition> elements) — branch coverage cannot be enforced"
                )
            elif pb < MIN_PROJECT_BRANCH_COVERAGE:
                project_floor_failed = True
                project_metrics.append(
                    f"  BREACH: Python branch {pb:.1f}% < floor {MIN_PROJECT_BRANCH_COVERAGE}% — "
                    "fix coverage (floors are a ratchet and must not be lowered)"
                )
    if raw_js_report is not None and raw_js_report.exists():
        js_metrics = _compute_project_wide_metrics_lcov(raw_js_report)
        if js_metrics is not None:
            jl, jb = js_metrics
            branch_display = f"{jb:.1f}%" if jb is not None else "n/a (no branch data)"
            project_metrics.append(f"  JavaScript project-wide: line {jl:.1f}%, branch {branch_display}")
            if jl < MIN_PROJECT_LINE_COVERAGE:
                project_floor_failed = True
                project_metrics.append(
                    f"  BREACH: JavaScript line {jl:.1f}% < floor {MIN_PROJECT_LINE_COVERAGE}% — "
                    "fix coverage (floors are a ratchet and must not be lowered)"
                )
            if jb is None:
                project_floor_failed = True
                project_metrics.append(
                    "  BREACH: JavaScript branch data missing from LCOV report "
                    "(no BRDA records) — branch coverage cannot be enforced"
                )
            elif jb < MIN_PROJECT_BRANCH_COVERAGE:
                project_floor_failed = True
                project_metrics.append(
                    f"  BREACH: JavaScript branch {jb:.1f}% < floor {MIN_PROJECT_BRANCH_COVERAGE}% — "
                    "fix coverage (floors are a ratchet and must not be lowered)"
                )

    # --- Summary ---
    print("\n=== Coverage Gate Summary ===")
    for r in results:
        print(f"  {r.summary()}")
    print()

    if project_metrics:
        print("=== Project-wide Coverage Floor (ratchet) ===")
        for line in project_metrics:
            print(line)
        print(
            f"  Floors: MIN_PROJECT_LINE_COVERAGE={MIN_PROJECT_LINE_COVERAGE}%, "
            f"MIN_PROJECT_BRANCH_COVERAGE={MIN_PROJECT_BRANCH_COVERAGE}%"
        )
        print("  Raise floors as coverage improves; never lower without justification.")
        print()

    all_skipped = all(r.skipped for r in results)
    any_line_failed = any(not r.skipped and not r.passed for r in results)

    if all_skipped and not project_floor_failed:
        print("No coverage data to check — gate passed (all languages skipped).")
        _write_summary(results)
        return 0

    if any_line_failed or any_branch_failed or project_floor_failed:
        reasons = []
        if any_line_failed:
            reasons.append("line coverage threshold")
        if any_branch_failed:
            reasons.append("branch coverage threshold")
        if project_floor_failed:
            reasons.append("project-wide floor")
        print(f"FAILED: one or more languages did not meet the {' and '.join(reasons)}.")
        _write_summary(results)
        return 1

    print("PASSED: all languages met the coverage thresholds.")
    _write_summary(results)
    return 0


if __name__ == "__main__":
    sys.exit(main())
