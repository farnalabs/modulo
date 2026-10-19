#!/usr/bin/env python3
"""Docs validity sweep for the Modulo product docs.

Scans ``README.md`` and ``docs/**/*.md`` for three classes of defect:

1. Retired tokens - organisation names, paths and environment flags the repo
   no longer recognises. This includes the retired ``Development/Product``
   AGENTS.md path and a ``ghcr.io`` image path whose first segment is not
   ``farnalabs`` - a third-party organisation, or a placeholder such as
   ``<org>``, ``{org}`` or ``$ORG``. A bare ``ghcr.io/farnalabs`` (no image
   path) is not flagged.
2. Repo-relative paths named in backticks - each backticked value that begins
   with a known top-level prefix (see ``PATH_PREFIXES``) and carries no space
   or placeholder character is checked against the checkout (also trying
   ``backend/`` and ``frontend/`` prefixes, because the test tree is documented
   without its ``backend/`` prefix). A reference to a sibling workspace repo
   (``Repos/...``) is reported as skipped, never failed.
3. Advertised external URLs - a bounded HTTP status probe per unique URL.

What this check does NOT cover: prose accuracy, markdown anchors, backticked
paths that do not begin with a known prefix or that contain a space or a
placeholder, or documentation held in another repository. Those are reviewed by
hand.

Exit code 0 only when nothing was found AND every candidate URL was probed. It
is 1 when a stale token, missing path or unreachable URL is found, or when the
``--max-urls`` cap left URLs unprobed - a capped run is NOT a clean result. Use
``--no-urls`` to skip the network pass, and ``--self-test`` to exercise the
checker's own detection rules.

Usage:
    uv run --project backend python scripts/check_doc_validity.py
    uv run --project backend python scripts/check_doc_validity.py --no-urls
    uv run --project backend python scripts/check_doc_validity.py --self-test
"""

from __future__ import annotations

import argparse
import contextlib
import io
import re
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Characters a ``ghcr.io`` first path segment can use: an organisation name, or
# a placeholder such as ``<org>``, ``{org}``, ``$ORG`` or ``*``. A sentence
# period is deliberately excluded, so a bare ``ghcr.io/farnalabs.`` does not
# look like a path.
_GHCR_SEGMENT_CHARS = r"[A-Za-z0-9_$<>{}*@-]"

# A ``ghcr.io`` reference whose first path segment is not ``farnalabs``. The
# lookahead fails only when the first segment is exactly ``farnalabs`` (bare, or
# followed by a real path separator), so ``ghcr.io/farnalabs`` and
# ``ghcr.io/farnalabs/modulo`` pass while ``ghcr.io/<org>/modulo``,
# ``ghcr.io/acme/modulo`` and ``ghcr.io/farnalabsx/modulo`` are flagged.
NON_FARNALABS_IMAGE_RE = re.compile(rf"ghcr\.io/(?!farnalabs(?!{_GHCR_SEGMENT_CHARS})){_GHCR_SEGMENT_CHARS}+")
NON_FARNALABS_IMAGE_MESSAGE = (
    "container image path is not under ghcr.io/farnalabs/ (the first path segment is not 'farnalabs')"
)

# Patterns that must not appear in the docs. Each entry is (pattern, message).
RETIRED_TOKENS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"anomalyco", re.IGNORECASE),
        "retired organisation reference 'anomalyco'",
    ),
    (
        re.compile(r"Development/Product"),
        "retired AGENTS.md path 'Development/Product' (use Repos/devtools/AGENTS.md)",
    ),
    (
        re.compile(r"\bMODULO_DEMO_MODE\b"),
        "retired flag 'MODULO_DEMO_MODE' (the demo is configured with MODULO_DEMO_* flags)",
    ),
    (
        re.compile(r"(?<![\w.-])staging\.modulo\.app\b"),
        "retired host 'staging.modulo.app' (use staging.modulo.run)",
    ),
    (NON_FARNALABS_IMAGE_RE, NON_FARNALABS_IMAGE_MESSAGE),
)

PATH_PREFIXES: tuple[str, ...] = (
    "docs/",
    "backend/",
    "frontend/",
    "scripts/",
    "deploy/",
    "configs/",
    ".github/",
    ".semgrep/",
    "tests/",
    "tools/",
    # Sibling workspace repos: extracted so ``CROSS_REPO_PREFIXES`` can report
    # them as skipped rather than silently dropping them.
    "Repos/",
)

# Paths that intentionally do not resolve inside this checkout.
INTENTIONAL_MISSING: dict[str, str] = {
    ".github/workflows/uptime-monitor.yml": ("removed deliberately (FAR-400 / FAR-1156); the doc records the removal"),
    ".github/workflows/modulo-apply.yml": ("a workflow file the reader saves in their own repository"),
    "tools/list": "an MCP protocol method, not a filesystem path",
}

# Programmatic endpoints used as base URLs rather than clickable pages: a bare
# GET does not serve a document, so the status probe is meaningless for them.
API_BASE_URLS: frozenset[str] = frozenset(
    {
        "https://api.e2b.app",
        "https://gitlab.com/api/v4",
        "https://staging.modulo.run/api/v1",
    }
)

# References that live in the workspace, outside this repository.
CROSS_REPO_PREFIXES: tuple[str, ...] = ("Repos/",)

PLACEHOLDER_RE = re.compile(r"[*{}<>]|::|\.\.\.|\$\(")
ANCHOR_RE = re.compile(r"[#§].*$")
LINE_SUFFIX_RE = re.compile(r":\d+(?:-\d+)?$")
URL_RE = re.compile(r"""https?://[^\s<>"')\]]+""")

SKIPPED_URL_HOSTS = frozenset(
    {
        # A host to skip in docs, never a socket binding.
        "0.0.0.0",  # noqa: S104
        "127.0.0.1",
        "backend",
        "docker-socket-proxy",
        "host.docker.internal",
        "localhost",
        "modulo",
        "otel-collector",
        "prometheus",
    }
)
TEMPLATE_URL_MARKERS = ("{", "}", "*", "<", ">", "$", "...", "xxx@", "your-")


@dataclass(frozen=True)
class UrlResult:
    url: str
    status: str
    ok: bool


def _doc_files(repo_root: Path) -> list[Path]:
    files = [repo_root / "README.md"]
    docs_dir = repo_root / "docs"
    if docs_dir.exists():
        files.extend(sorted(docs_dir.rglob("*.md")))
    return [path for path in files if path.is_file()]


def _line_number(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def scan_retired_tokens(files: list[Path], repo_root: Path) -> list[str]:
    findings: list[str] = []
    for path in files:
        text = path.read_text(encoding="utf-8", errors="replace")
        rel = path.relative_to(repo_root).as_posix()
        findings.extend(
            f"{rel}:{_line_number(text, match.start())}: {message}"
            for pattern, message in RETIRED_TOKENS
            for match in pattern.finditer(text)
        )
    return findings


def _candidate_paths(value: str) -> list[str]:
    candidates = [value]
    if value.startswith(("tests/", "tools/")):
        candidates.append(f"backend/{value}")
        candidates.append(f"frontend/{value}")
    return candidates


def _extract_backticked_paths(text: str) -> list[str]:
    values: list[str] = []
    for match in re.finditer(r"`([^`\n]+)`", text):
        value = match.group(1).strip()
        if " " in value or value.startswith(("http://", "https://", "ghcr.io/", "www.")):
            continue
        if not value.startswith(PATH_PREFIXES):
            continue
        if PLACEHOLDER_RE.search(value):
            continue
        value = value.split("`")[0].rstrip(".,;:")
        if ANCHOR_RE.search(value):
            value = ANCHOR_RE.sub("", value)
        value = LINE_SUFFIX_RE.sub("", value)
        if value:
            values.append(value)
    return values


def scan_paths(files: list[Path], repo_root: Path) -> tuple[list[str], list[str]]:
    findings: list[str] = []
    skipped: list[str] = []
    for path in files:
        text = path.read_text(encoding="utf-8", errors="replace")
        rel = path.relative_to(repo_root).as_posix()
        for value in _extract_backticked_paths(text):
            if value.startswith(CROSS_REPO_PREFIXES):
                skipped.append(f"{rel}: {value} (cross-repo reference, not checked)")
                continue
            if value in INTENTIONAL_MISSING:
                continue
            if any((repo_root / candidate).exists() for candidate in _candidate_paths(value)):
                continue
            findings.append(f"{rel}: path does not exist: {value}")
    return findings, skipped


def _extract_urls(files: list[Path]) -> list[str]:
    seen: set[str] = set()
    for path in files:
        text = path.read_text(encoding="utf-8", errors="replace")
        for match in URL_RE.finditer(text):
            # A comma-separated list (for example a CORS origin list) is several
            # URLs, not one.
            for part in match.group(0).split(","):
                url = part.rstrip(".,;:`")
                if url:
                    seen.add(url.replace("&amp;", "&"))
    return sorted(seen)


def _is_checkable_url(url: str) -> bool:
    if "`" in url:
        return False
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    if parsed.hostname in SKIPPED_URL_HOSTS:
        return False
    if parsed.hostname.endswith(".example.com") or parsed.hostname == "example.com":
        return False
    return not any(marker in url for marker in TEMPLATE_URL_MARKERS)


def _probe_url(url: str, timeout: float) -> UrlResult:
    # S310: the URLs are read from the repository's own markdown, and only
    # http/https ever reach here (_is_checkable_url filters the scheme).
    request = urllib.request.Request(  # noqa: S310
        url,
        headers={"User-Agent": "modulo-docs-validity/1.0"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            code = response.getcode()
            return UrlResult(url, str(code), 200 <= code < 400)
    except urllib.error.HTTPError as exc:
        # 401/403/405 mean the endpoint is reachable and gated, not broken.
        gated = exc.code in {401, 403, 405}
        status = f"{exc.code} (gated)" if gated else str(exc.code)
        return UrlResult(url, status, gated)
    except (urllib.error.URLError, TimeoutError) as exc:
        reason = getattr(exc, "reason", exc)
        return UrlResult(url, f"unreachable ({reason})", False)


def scan_urls(files: list[Path], timeout: float, max_urls: int) -> tuple[list[UrlResult], list[str], int]:
    candidates = [url for url in _extract_urls(files) if _is_checkable_url(url)]
    base_urls = [url for url in candidates if url in API_BASE_URLS]
    urls = [url for url in candidates if url not in API_BASE_URLS]
    unchecked = max(0, len(urls) - max_urls)
    urls = urls[:max_urls]
    if not urls:
        return [], base_urls, unchecked
    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(lambda url: _probe_url(url, timeout), urls))
    return results, base_urls, unchecked


def _self_test() -> list[str]:
    """Exercise the checker's own rules; return the failures (empty is good).

    This guards the classes of defect a change here can reintroduce silently:
    a registry pattern that no longer catches a placeholder first segment (its
    own target class), a path extractor that stops reporting a cross-repo
    reference, and a URL-cap path that reports a capped run as clean.
    """
    failures: list[str] = []

    def check(description: str, condition: bool) -> None:
        if not condition:
            failures.append(description)

    def flags(image: str) -> bool:
        return NON_FARNALABS_IMAGE_RE.search(image) is not None

    for image in (
        "ghcr.io/acme/modulo",
        "ghcr.io/<org>/modulo:${TAG}",
        "ghcr.io/{org}/modulo",
        "ghcr.io/$ORG/modulo",
        "ghcr.io/ORG/modulo",
        "ghcr.io/<org>/*",
        "ghcr.io/farnalabsx/modulo",
    ):
        check(f"expected a finding for {image!r}", flags(image))
    for reference in (
        "ghcr.io/farnalabs",
        "ghcr.io/farnalabs.",
        "ghcr.io/farnalabs/modulo:${TAG}",
        "ghcr.io/farnalabs/modulo@sha256:<digest>",
        "ghcr.io",
        "ghcr.io/",
    ):
        check(f"expected no finding for {reference!r}", not flags(reference))

    extracted = _extract_backticked_paths(
        "`docs/missing.md` `Repos/devtools/AGENTS.md` `docs/two words.md` `docs/<placeholder>.md`"
    )
    check("a missing in-repo path is extracted", "docs/missing.md" in extracted)
    check(
        "a sibling-workspace path is extracted (so the skip reports)",
        "Repos/devtools/AGENTS.md" in extracted,
    )
    check("a path containing a space is ignored", "docs/two words.md" not in extracted)
    check("a placeholder path is ignored", "docs/<placeholder>.md" not in extracted)

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "docs").mkdir()
        (root / "README.md").write_text("# t\n", encoding="utf-8")
        (root / "docs" / "page.md").write_text("See https://modulo.run for details.\n", encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            capped_exit = main(["--repo-root", str(root), "--max-urls", "0"])
        check("a capped URL pass exits non-zero", capped_exit != 0)
    return failures


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Check the product docs for retired tokens, repo-relative paths that "
            "do not exist, and advertised URLs that do not resolve."
        )
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=REPO_ROOT,
        help="repository root to scan (default: the checkout containing this script)",
    )
    parser.add_argument(
        "--no-urls",
        action="store_true",
        help="skip the network pass that probes advertised URLs",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        help="per-URL timeout in seconds (default: 10)",
    )
    parser.add_argument(
        "--max-urls",
        type=int,
        default=100,
        help="maximum number of unique URLs to probe (default: 100)",
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="run the checker's built-in self-test and exit (no repository scan)",
    )
    return parser.parse_args(argv)


def _report_self_test(failures: list[str]) -> int:
    for failure in failures:
        print(f"SELF-TEST FAIL  {failure}", file=sys.stderr)
    if failures:
        print(f"\nSelf-test: {len(failures)} failure(s).", file=sys.stderr)
        return 1
    print("Self-test: OK (ghcr.io placeholder detection, path extraction, capped-run exit code).")
    return 0


def _scan_and_report_urls(files: list[Path], timeout: float, max_urls: int) -> tuple[list[UrlResult], int]:
    results, base_urls, unchecked_urls = scan_urls(files, timeout, max_urls)
    failed = [result for result in results if not result.ok]
    print(f"\nURL status ({len(results)} checked, {unchecked_urls} over the cap):")
    for result in results:
        marker = "ok" if result.ok else "FAIL"
        print(f"  {marker:4} {result.status:>16}  {result.url}")
    for url in base_urls:
        print(f"  SKIP {'(API base URL)':>16}  {url}")
    return failed, unchecked_urls


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.self_test:
        return _report_self_test(_self_test())
    repo_root = args.repo_root.resolve()
    files = _doc_files(repo_root)
    if not files:
        print(f"no docs found under {repo_root}", file=sys.stderr)
        return 1

    token_findings = scan_retired_tokens(files, repo_root)
    path_findings, skipped_paths = scan_paths(files, repo_root)

    for finding in token_findings:
        print(f"STALE TOKEN  {finding}", file=sys.stderr)
    for finding in path_findings:
        print(f"MISSING PATH {finding}", file=sys.stderr)
    for note in skipped_paths:
        print(f"SKIP         {note}")

    url_findings: list[UrlResult] = []
    unchecked_urls = 0
    if not args.no_urls:
        url_findings, unchecked_urls = _scan_and_report_urls(files, args.timeout, args.max_urls)

    stale_count = len(token_findings) + len(path_findings)
    failed_count = len(url_findings)
    print(
        f"\nSummary: {stale_count} stale token/path finding(s), "
        f"{failed_count} failed URL(s), {unchecked_urls} URL(s) not checked."
    )
    if unchecked_urls:
        print(
            "Raise --max-urls to cover the remaining URLs; they were NOT verified, so this is not a clean result.",
            file=sys.stderr,
        )
        print(
            f"{unchecked_urls} URL(s) went unprobed; the run exits 1.",
            file=sys.stderr,
        )
    return 1 if stale_count or failed_count or unchecked_urls else 0


if __name__ == "__main__":
    raise SystemExit(main())
