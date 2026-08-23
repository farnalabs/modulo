"""OSS Partner composite eligibility checker (launch tool).

Given a GitHub repository URL or `owner/name`, evaluates the OSS Partner
composite and prints a verdict plus evidence for founder review.

Criteria
--------
1. public, NOT a fork, NOT archived
2. OSI-approved licence detected (license.spdx_id, with a LICENSE-file
   fallback when the SPDX id is null/NOASSERTION)
3. >= 100 stars AND repo age >= 6 months AND >= 1 tagged release AND
   >= 2 distinct committers in the last 90 days (bots excluded heuristically)

The verdict is keyed on the GitHub repo ID (survives renames). Any API
failure (rate-limit / outage / token expiry) is reported as INCONCLUSIVE
with an escalation note -- it never produces a false reject.

Usage
-----
    python backend/scripts/check_partner_eligibility.py <repo-url-or-owner/name>
    python backend/scripts/check_partner_eligibility.py owner/repo --json

Design note: the network layer lives entirely in :func:`gather_repo_evidence`,
which is small and injectable so tests can patch it without touching the
network. All evaluation logic is pure and operates on the returned dict.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import subprocess  # nosec B404
import sys
import time
from collections.abc import Callable
from typing import Any

import requests

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# A curated set of OSI-approved SPDX licence identifiers. GitHub's per-repo
# licence object does not expose an `osi_approved` boolean directly, so we
# approximate with this allow-list of the licences the OSS Partner program
# commonly accepts. Extend as needed.
OSI_APPROVED_SPDX_IDS: frozenset[str] = frozenset(
    {
        "MIT",
        "Apache-2.0",
        "BSD-2-Clause",
        "BSD-3-Clause",
        "GPL-2.0",
        "GPL-3.0",
        "LGPL-2.1",
        "LGPL-3.0",
        "MPL-2.0",
        "EPL-2.0",
        "AGPL-3.0",
        "ISC",
        "Unlicense",
        "CC0-1.0",
        "Zlib",
        "BSL-1.0",
        "Artistic-2.0",
        "Python-2.0",
        "LLVM-exception",
    }
)

MIN_STARS = 100
MIN_AGE_DAYS = 183  # ~6 months
MIN_TAGGED_RELEASES = 1
MIN_DISTINCT_COMMITTERS = 2
COMMITTER_WINDOW_DAYS = 90

# Bot usernames are matched case-insensitively. The `[bot]` suffix covers the
# bulk of GitHubApps; the explicit set catches notable exceptions.
KNOWN_BOT_LOGINS: frozenset[str] = frozenset(
    {
        "dependabot[bot]",
        "github-actions[bot]",
        "renovate[bot]",
        "mergify[bot]",
        "greenkeeper[bot]",
        "snyk-bot",
        "imgbot[bot]",
        "codecov[bot]",
        "dependabot-preview[bot]",
    }
)

_API_TIMEOUT_SECONDS = 30
_MAX_RETRIES = 3
_BACKOFF_BASE_SECONDS = 1.0
_MAX_PAGINATION_PAGES = 10
_GITHUB_API_BASE = "https://api.github.com/"


class GithubApiError(Exception):
    """Raised when the GitHub API cannot be reached or returns an error.

    Callers translate this into an INCONCLUSIVE verdict -- never a false
    reject.
    """


# ---------------------------------------------------------------------------
# Reason codes
# ---------------------------------------------------------------------------

RC_NOT_PUBLIC = "NOT_PUBLIC"
RC_IS_FORK = "IS_FORK"
RC_IS_ARCHIVED = "IS_ARCHIVED"
RC_NO_OSI_LICENSE = "NO_OSI_LICENSE"
RC_LICENSE_FILE_FALLBACK = "LICENSE_FILE_FALLBACK"
RC_BELOW_STARS = "BELOW_STARS"
RC_TOO_YOUNG = "TOO_YOUNG"
RC_NO_TAGGED_RELEASE = "NO_TAGGED_RELEASE"
RC_INSUFFICIENT_COMMITTERS = "INSUFFICIENT_COMMITTERS"
RC_API_FAILURE = "API_FAILURE"

VERDICT_ELIGIBLE = "ELIGIBLE"
VERDICT_INELIGIBLE = "INELIGIBLE"
VERDICT_INCONCLUSIVE = "INCONCLUSIVE"

STATUS_PASS = "PASS"  # nosec B105
STATUS_FAIL = "FAIL"
STATUS_INCONCLUSIVE = "INCONCLUSIVE"


def _exit_code(verdict: str) -> int:
    """Map a verdict to a CLI process exit code.

    ELIGIBLE -> 0, INELIGIBLE -> 1, INCONCLUSIVE -> 2. Distinguishing
    INCONCLUSIVE (2) from a definitive INELIGIBLE (1) lets callers/script
    authors tell "we could not decide" apart from "reject".
    """
    if verdict == VERDICT_ELIGIBLE:
        return 0
    if verdict == VERDICT_INELIGIBLE:
        return 1
    return 2


# ---------------------------------------------------------------------------
# Target parsing
# ---------------------------------------------------------------------------


def parse_target(target: str) -> str:
    """Normalise a repo URL or `owner/name` string to `owner/name`.

    Raises ValueError if the input cannot be parsed.
    """
    target = target.strip()
    # Strip a trailing ".git" and any leading "git@" scp-like forms defensively.
    target = target.removesuffix(".git")

    # https://github.com/owner/repo or http://github.com/owner/repo
    url_match = re.match(r"^https?://github\.com/([^/]+)/([^/?#]+)/?$", target, re.IGNORECASE)
    if url_match:
        return f"{url_match.group(1)}/{url_match.group(2)}"

    # github.com/owner/repo (no scheme)
    bare_match = re.match(r"^github\.com/([^/]+)/([^/?#]+)/?$", target, re.IGNORECASE)
    if bare_match:
        return f"{bare_match.group(1)}/{bare_match.group(2)}"

    # owner/name
    if re.match(r"^[^/\s]+/[^/\s]+$", target):
        return target

    raise ValueError(f"Could not parse {target!r} as a GitHub repo URL or owner/name.")


# ---------------------------------------------------------------------------
# Network layer (injectable)
# ---------------------------------------------------------------------------


def _url_for_path(path: str) -> str:
    """Build the absolute GitHub API URL for *path* (or pass through full URLs)."""
    if "://" in path:
        return path
    return f"{_GITHUB_API_BASE}{path.lstrip('/')}"


def _gh_is_transient(result: subprocess.CompletedProcess[str]) -> bool:
    """Best-effort detection of a transient `gh` failure worth retrying."""
    text = f"{result.stdout} {result.stderr}".lower()
    return any(token in text for token in ("rate limit", "429", "500", "502", "503", "timeout", "temporarily"))


def _gh_retry_after(result: subprocess.CompletedProcess[str]) -> str | None:
    """Extract a Retry-After hint from `gh` output, if present."""
    import re as _re  # local import keeps module-level clean

    match = _re.search(r"retry-after:\s*(\d+)", f"{result.stdout} {result.stderr}", _re.IGNORECASE)
    return match.group(1) if match else None


def _backoff(attempt: int, retry_after: str | None) -> None:
    """Sleep with exponential backoff, honouring a Retry-After hint if given."""
    if retry_after is not None:
        try:
            delay = float(retry_after)
        except (TypeError, ValueError):
            delay = _BACKOFF_BASE_SECONDS * (2**attempt)
    else:
        delay = _BACKOFF_BASE_SECONDS * (2**attempt)
    # Cap the wait so a pathological Retry-After cannot hang the tool forever.
    time.sleep(min(delay, 30.0))


def _extract_next_link(link_header: str | None) -> str | None:
    """Return the `rel="next"` URL from a GitHub `Link` header, if any.

    A `rel="next"` segment whose angle-bracketed URL is missing or
    unparseable is evidence that pagination is unreliable -- it raises
    :class:`GithubApiError` (→ INCONCLUSIVE) rather than being treated as
    "no more pages".
    """
    if not link_header:
        return None
    for part in link_header.split(","):
        segment = part.strip()
        if 'rel="next"' not in segment and "rel='next'" not in segment:
            continue
        start = segment.find("<")
        end = segment.find(">", start)
        if start != -1 and end != -1 and start + 1 < end:
            return segment[start + 1 : end]
        raise GithubApiError(
            'GitHub API Link header contains rel="next" with an unparseable URL; pagination cannot be trusted.'
        )
    return None


def _parse_gh_include_output(stdout: str) -> tuple[dict[str, Any], dict[str, str]]:
    """Parse the combined stdout of ``gh api -i <endpoint>``.

    ``gh api -i`` emits the response headers, a blank line, then the JSON
    body. Returns ``(parsed_json, headers_dict)`` with header keys
    lower-cased. Raises :class:`GithubApiError` if the body is not valid JSON.
    """
    parts = stdout.split("\n\n", 1)
    header_block = parts[0]
    body = parts[1] if len(parts) > 1 else ""
    headers: dict[str, str] = {}
    for line in header_block.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        # Normalise to lower case so callers look up a stable key regardless of
        # how the source capitalised it (``requests`` returns a case-insensitive
        # dict, this one is plain -- lower-casing both makes the lookup uniform).
        headers[key.strip().lower()] = value.strip()
    try:
        return json.loads(body), headers
    except json.JSONDecodeError as exc:
        raise GithubApiError(f"gh returned non-JSON body after headers: {exc}") from None


def _coerce_int(value: Any, *, field: str) -> int:
    """Coerce *value* to ``int`` or raise :class:`GithubApiError`.

    Any non-numeric / missing value is treated as unreliable evidence (→
    INCONCLUSIVE), never as a silent false reject.
    """
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise GithubApiError(f"GitHub API returned a non-numeric {field}: {value!r}") from exc


# Sentinel returned by :func:`_run_gh_once` when the `gh` CLI could not be
# launched (OSError) or timed out -- both mean "fall through to requests".
_TRANSIENT_GH = object()


def _run_gh_once(path: str):
    """Launch one `gh api -i` call.

    Returns the :class:`subprocess.CompletedProcess` on success, or
    :data:`_TRANSIENT_GH` when `gh` could not be launched or timed out.
    """
    try:
        return subprocess.run(  # nosec  # noqa: S603
            ["gh", "api", "-i", path],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=_API_TIMEOUT_SECONDS,
        )
    except OSError:
        # gh cannot even be launched (e.g. PermissionError) -> requests.
        return _TRANSIENT_GH
    except subprocess.TimeoutExpired:
        return _TRANSIENT_GH


def _gh_fetch_with_retries(path: str):
    """Try the `gh` CLI with retries.

    Returns ``(parsed_json, headers)`` on a successful call, or ``None`` when
    `gh` cannot serve the request so the caller falls back to `requests`.
    """
    for attempt in range(_MAX_RETRIES):
        result = _run_gh_once(path)
        if result is _TRANSIENT_GH:
            if attempt < _MAX_RETRIES - 1:
                _backoff(attempt, None)
                continue
            return None
        if result.returncode == 0 and result.stdout.strip():
            # _parse_gh_include_output raises GithubApiError on a non-JSON body
            # (unreliable evidence -> INCONCLUSIVE), which we let propagate.
            return _parse_gh_include_output(result.stdout)
        # gh errored. Retry only clearly transient failures, else fall through.
        if _gh_is_transient(result) and attempt < _MAX_RETRIES - 1:
            _backoff(attempt, _gh_retry_after(result))
            continue
        return None
    return None


def _requests_fetch_with_retries(url: str, allow_codes: tuple[int, ...]) -> tuple[dict[str, Any], Any]:
    """Fetch *url* with `requests`, retrying on 429/5xx.

    Raises :class:`GithubApiError` on any unrecoverable failure. *allow_codes*
    lists HTTP status codes that should be returned as an empty dict ``{}``
    instead of raising (so a 404 "no LICENSE file" is distinguishable from a
    genuine API failure -- which must stay INCONCLUSIVE, never a silent
    false reject).
    """
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    last_exc: Exception | None = None
    for attempt in range(_MAX_RETRIES):
        try:
            resp = requests.get(url, headers=headers, timeout=_API_TIMEOUT_SECONDS)
        except requests.RequestException as exc:
            last_exc = exc
            break

        if resp.status_code == 403 and resp.headers.get("X-RateLimit-Remaining") == "0":
            raise GithubApiError("GitHub API rate limit exceeded.")
        if resp.status_code in (401, 403):
            raise GithubApiError(f"GitHub API auth/permission error: HTTP {resp.status_code}.")
        if resp.status_code < 400:
            try:
                return resp.json(), resp.headers
            except json.JSONDecodeError as exc:
                raise GithubApiError(f"GitHub API returned non-JSON body: {exc}") from None
        if resp.status_code in allow_codes:
            return {}, resp.headers
        # Retry only on rate-limit / server errors; never on other 4xx.
        if (resp.status_code == 429 or resp.status_code >= 500) and attempt < _MAX_RETRIES - 1:
            _backoff(attempt, resp.headers.get("Retry-After"))
            continue
        raise GithubApiError(f"GitHub API error: HTTP {resp.status_code}.")

    if last_exc is not None:
        raise GithubApiError(f"GitHub API request failed: {last_exc}") from None
    raise GithubApiError("GitHub API request failed after retries.")


def _api_get_with_headers(path: str, *, allow_codes: tuple[int, ...] = ()) -> tuple[dict[str, Any], Any]:
    """GET a GitHub REST API path, returning ``(parsed_json, response_headers)``.

    Prefers the `gh` CLI (handles auth + rate-limit), falling back to
    `requests` with a token from the environment. Raises
    :class:`GithubApiError` on any unrecoverable failure. On HTTP 429 or 5xx,
    retries up to :data:`_MAX_RETRIES` times with backoff (honouring a
    ``Retry-After`` header when present) before giving up. A ``Retry-After``
    hint is also honoured for the `gh` CLI path when it surfaces a transient
    error.

    *allow_codes* lists HTTP status codes that should be returned as an empty
    dict ``{}`` instead of raising -- used so a 404 (e.g. "no LICENSE file")
    can be distinguished from a genuine API failure (which must stay
    INCONCLUSIVE, never a silent false reject).
    """
    url = _url_for_path(path)
    gh_result = _gh_fetch_with_retries(path)
    if gh_result is not None:
        return gh_result
    return _requests_fetch_with_retries(url, allow_codes)


def _api_get(path: str, *, allow_codes: tuple[int, ...] = ()) -> dict[str, Any]:
    """GET a GitHub REST API path, returning parsed JSON only.

    Thin wrapper over :func:`_api_get_with_headers` (which is what pagination
    needs for the ``Link`` header). Kept for call sites that only want the body.
    """
    data, _ = _api_get_with_headers(path, allow_codes=allow_codes)
    return data


def _paginate(endpoint: str, *, max_pages: int = _MAX_PAGINATION_PAGES) -> list[dict[str, Any]]:
    """Follow GitHub `Link` pagination for *endpoint*, aggregating list results.

    Caps at *max_pages* to avoid runaway loops on misbehaving servers. If the
    API ever returns a non-list body (truncation / unexpected shape) or the
    page cap is hit while more pages remain, raises :class:`GithubApiError`
    so the caller reports INCONCLUSIVE rather than undercounting.
    """
    results: list[dict[str, Any]] = []
    next_path: str = endpoint
    for _ in range(max_pages):
        data, headers = _api_get_with_headers(next_path)
        if not isinstance(data, list):
            raise GithubApiError(f"GitHub API returned non-list payload for {next_path}.")
        results.extend(data)
        link_header = None
        if headers:
            for header_name in headers:
                if header_name.lower() == "link":
                    link_header = headers[header_name]
                    break
        nxt = _extract_next_link(link_header)
        if not nxt:
            return results
        next_path = nxt
    raise GithubApiError("GitHub API pagination exceeded the maximum page count; results truncated.")


def gather_repo_evidence(target: str) -> dict[str, Any]:
    """Fetch all raw GitHub evidence for *target*.

    The returned dict is consumed by :func:`evaluate_evidence` and contains no
    network calls of its own. Raises :class:`GithubApiError` on any API
    failure (so callers can report INCONCLUSIVE).
    """
    owner_repo = parse_target(target)
    now = _dt.datetime.now(_dt.UTC)

    repo = _api_get(f"repos/{owner_repo}")
    repo_id = repo.get("id")
    if repo_id is None:
        raise GithubApiError("GitHub API response missing repo id.")

    # Essential evaluation fields must be present. A partial /repos response
    # (e.g. field dropped by an outage or a redirect to a stub) must NOT be
    # silently defaulted to 0/None and produce a definitive reject -- it is
    # reported as INCONCLUSIVE instead.
    if repo.get("stargazers_count") is None or repo.get("created_at") is None:
        raise GithubApiError("GitHub API /repos response missing essential evaluation fields.")

    # The star count must be a valid integer. A non-numeric value (or any other
    # coercion failure) is unreliable evidence and must surface as INCONCLUSIVE,
    # never as a crash or a silent false reject.
    stargazers_count = _coerce_int(repo.get("stargazers_count"), field="stargazers_count")

    # Licence: the /license endpoint returns 404 when no licence file exists
    # (legitimately "not present"); any other API error must propagate as
    # INCONCLUSIVE rather than be swallowed into a silent false reject.
    spdx_id = None
    if isinstance(repo.get("license"), dict):
        spdx_id = repo["license"].get("spdx_id")
    license_file_present = bool(_api_get(f"repos/{owner_repo}/license", allow_codes=(404,)))

    # Tags (>= 1 tagged release).
    tags = _paginate(f"repos/{owner_repo}/tags?per_page=100")

    # Commits in the last COMMITTER_WINDOW_DAYS.
    since = (now - _dt.timedelta(days=COMMITTER_WINDOW_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    commits = _paginate(f"repos/{owner_repo}/commits?since={since}&per_page=100")

    return {
        "repo_id": repo_id,
        "full_name": repo.get("full_name"),
        "private": bool(repo.get("private", False)),
        "fork": bool(repo.get("fork", False)),
        "archived": bool(repo.get("archived", False)),
        "stargazers_count": stargazers_count,
        "created_at": repo.get("created_at"),
        "spdx_id": spdx_id,
        "license_file_present": license_file_present,
        "tags": tags if isinstance(tags, list) else [],
        "commits": commits if isinstance(commits, list) else [],
        "now": now,
    }


# ---------------------------------------------------------------------------
# Evaluation (pure)
# ---------------------------------------------------------------------------


def _is_bot_login(login: str | None) -> bool:
    if not login:
        return False
    if login.lower().endswith("[bot]"):
        return True
    return login.lower() in KNOWN_BOT_LOGINS


def _distinct_human_committers(commits: list[dict[str, Any]]) -> set[str]:
    authors: set[str] = set()
    for commit in commits:
        if not isinstance(commit, dict):
            continue
        author = commit.get("author") or commit.get("committer")
        login = None
        if isinstance(author, dict):
            login = author.get("login")
        if login and not _is_bot_login(login):
            authors.add(login)
    return authors


def _set_criterion(
    criteria: dict[str, dict[str, str | None]],
    name: str,
    passed: bool,
    fail_reason: str | None,
    pass_reason: str | None = None,
) -> None:
    """Record a single criterion.

    A passing criterion normally has ``reason=None``; *pass_reason* overrides
    that for the licence-file fallback, which passes but records *why* it did.
    """
    criteria[name] = {
        "status": STATUS_PASS if passed else STATUS_FAIL,
        "reason": pass_reason if passed else fail_reason,
    }


def _evaluate_criteria(
    evidence: dict[str, Any],
    committers: set[str] | None = None,
) -> dict[str, dict[str, str | None]]:
    """Return a mapping of criterion -> {status, reason}."""
    now = evidence["now"]
    criteria: dict[str, dict[str, str | None]] = {}

    # 1. Visibility / fork / archived.
    if evidence["private"]:
        _set_criterion(criteria, "public", False, RC_NOT_PUBLIC)
    elif evidence["fork"]:
        _set_criterion(criteria, "public", False, RC_IS_FORK)
    elif evidence["archived"]:
        _set_criterion(criteria, "public", False, RC_IS_ARCHIVED)
    else:
        _set_criterion(criteria, "public", True, None)

    # 2. Licence.
    #    (a) Accept a known OSI spdx_id regardless of case.
    #    (b) ONLY fall back to a LICENSE file when spdx_id is null/NOASSERTION.
    #    (c) Reject a KNOWN non-OSI spdx_id even when a LICENSE file is present.
    spdx = evidence.get("spdx_id")
    spdx_upper = (spdx or "").upper()
    if spdx and spdx_upper not in ("NULL", "NOASSERTION") and spdx_upper in OSI_APPROVED_SPDX_IDS:
        _set_criterion(criteria, "license", True, None)
    elif (spdx is None or spdx_upper in ("NULL", "NOASSERTION")) and evidence.get("license_file_present"):
        _set_criterion(criteria, "license", True, RC_NO_OSI_LICENSE, pass_reason=RC_LICENSE_FILE_FALLBACK)
    else:
        _set_criterion(criteria, "license", False, RC_NO_OSI_LICENSE)

    # 3. Stars.
    _set_criterion(
        criteria,
        "stars",
        evidence["stargazers_count"] >= MIN_STARS,
        RC_BELOW_STARS,
    )

    # 4. Age.
    created_raw = evidence.get("created_at")
    if not created_raw:
        raise GithubApiError("Repository created_at is missing or empty.")
    try:
        created = _dt.datetime.fromisoformat(created_raw)
    except ValueError as exc:
        # An unparseable date is an evidence problem, not a policy fail --
        # report INCONCLUSIVE rather than a hard TOO_YOUNG reject.
        raise GithubApiError(f"Repository created_at is unparseable: {created_raw!r}") from exc
    age_ok = (now - created) >= _dt.timedelta(days=MIN_AGE_DAYS)
    _set_criterion(criteria, "age", age_ok, RC_TOO_YOUNG)

    # 5. Tagged release.
    _set_criterion(
        criteria,
        "release",
        len(evidence.get("tags", [])) >= MIN_TAGGED_RELEASES,
        RC_NO_TAGGED_RELEASE,
    )

    # 6. Distinct human committers.
    if committers is None:
        committers = _distinct_human_committers(evidence.get("commits", []))
    _set_criterion(
        criteria,
        "committers",
        len(committers) >= MIN_DISTINCT_COMMITTERS,
        RC_INSUFFICIENT_COMMITTERS,
    )

    return criteria


def evaluate_evidence(evidence: dict[str, Any]) -> dict[str, Any]:
    """Evaluate evidence into a full result dict (no network)."""
    # Compute distinct committers once and reuse in both criteria + evidence.
    committers = _distinct_human_committers(evidence.get("commits", []))
    criteria = _evaluate_criteria(evidence, committers)

    has_fail = any(c["status"] == STATUS_FAIL for c in criteria.values())
    verdict = VERDICT_INELIGIBLE if has_fail else VERDICT_ELIGIBLE

    return {
        "repo_id": evidence.get("repo_id"),
        "full_name": evidence.get("full_name"),
        "verdict": verdict,
        "criteria": criteria,
        "evidence": {
            "stars": evidence.get("stargazers_count"),
            "created_at": evidence.get("created_at"),
            "tag_count": len(evidence.get("tags", [])),
            "distinct_human_committers": len(committers),
            "spdx_id": evidence.get("spdx_id"),
            "license_file_present": evidence.get("license_file_present"),
        },
    }


def check_eligibility(
    target: str,
    fetcher: Callable[[str], dict[str, Any]] = gather_repo_evidence,
) -> dict[str, Any]:
    """Check eligibility for *target*.

    *fetcher* is injectable for testing. Any :class:`GithubApiError` raised by
    the fetcher OR during evaluation yields an INCONCLUSIVE verdict (never a
    false reject).
    """
    try:
        evidence = fetcher(target)
        return evaluate_evidence(evidence)
    except GithubApiError as exc:
        return {
            "repo_id": None,
            "full_name": None,
            "verdict": VERDICT_INCONCLUSIVE,
            "criteria": {"api": {"status": STATUS_INCONCLUSIVE, "reason": RC_API_FAILURE}},
            "evidence": {"error": str(exc)},
            "escalation": (
                "API unreachable or rate-limited. Do NOT reject on this result. "
                "Escalate to a human to verify manually (e.g. check the repo in "
                "a browser, or retry with a valid GITHUB_TOKEN / gh auth)."
            ),
        }


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def _format_human(result: dict[str, Any]) -> str:
    lines: list[str] = []
    verdict = result["verdict"]
    lines.append("=" * 60)
    lines.append(f" OSS PARTNER ELIGIBILITY — {verdict}")
    lines.append("=" * 60)
    if result.get("full_name"):
        lines.append(f" Repository : {result['full_name']}")
    if result.get("repo_id") is not None:
        lines.append(f" Repo ID    : {result['repo_id']} (rename-safe key)")
    lines.append("")
    lines.append(" Criteria:")
    for name, c in result.get("criteria", {}).items():
        reason = c.get("reason")
        reason_txt = f" ({reason})" if reason else ""
        lines.append(f"   - {name:<10} {c['status']}{reason_txt}")
    if result.get("evidence"):
        lines.append("")
        lines.append(" Evidence:")
        for k, v in result["evidence"].items():
            lines.append(f"   - {k}: {v}")
    if verdict == VERDICT_INCONCLUSIVE and result.get("escalation"):
        lines.append("")
        lines.append(" ESCALATION: " + result["escalation"])
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check OSS Partner composite eligibility for a GitHub repo.")
    parser.add_argument(
        "target",
        help="GitHub repo URL (https://github.com/owner/repo) or owner/name.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of a human report.",
    )
    args = parser.parse_args(argv)

    try:
        result = check_eligibility(args.target, fetcher=gather_repo_evidence)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        print(_format_human(result))

    return _exit_code(result["verdict"])


if __name__ == "__main__":
    raise SystemExit(main())
