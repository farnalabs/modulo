"""Git-sourced content refs for ``sandbox_agent`` nodes (FAR-220 increment 1).

A ``sandbox_agent`` node's content fields — ``agent_prompt``, each
``agent_commands`` item, and ``script_command`` — may reference a file tracked
in a git repository instead of inlining the content (the "checkout-and-execute"
/ "fetch-and-render" pattern, productised):

Format
------
``git+<repo-url>[@<ref>]#<path>``

* ``git+`` — the ref marker (sits in a string field exactly like
  ``secretref://`` does: an alternative value, not a new field).
* ``<repo-url>`` — ``https://``, ``ssh://``, or SCP-style ``git@host:path``
  (the SAME scheme set managed workspace inputs accept). Credentials must NOT
  be embedded in the URL (no ``user@`` userinfo): ``@`` after the repository is
  reserved for the ref separator.
* ``@<ref>`` — optional branch / tag / commit SHA; omitted means ``HEAD``.
  A 40-hex value is a **pin**. Stored graphs may only carry pinned refs
  (enforced at graph-save validation), so every run snapshot contains the
  resolved commit SHA for audit; ``modulo apply`` resolves movable refs
  (branch/tag/HEAD) to their current SHA at plan time and writes the pinned
  form (pin-on-apply).
* ``#<path>`` — repository-relative path of the file; must be non-empty,
  relative (no leading ``/``), free of ``..`` segments, ``#``, and newlines.

Resolution / fetch
------------------
* :func:`default_git_content_resolver` — plan-time pinning (``modulo apply``):
  identity for an already-pinned ref, otherwise ``git ls-remote`` to resolve a
  movable ref to its current commit SHA (bounded, fail-closed).
* :func:`fetch_git_content` — run-time rendering (the agent_command rendering
  point): clones the repository at the pinned SHA and returns the UTF-8 file
  content that replaces the ref before dispatch. Unpinned refs and fetch
  failures raise typed errors — there is no silent fallback to the raw ref
  string.

The module is deliberately lightweight (stdlib + the sibling
``workspace_inputs`` ls-remote parser) so the graph validator, the run-time
renderer, and the ``modulo apply`` CLI can all import it.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from modulo.core.pipeline_engine.workspace_inputs import parse_ls_remote

_log = logging.getLogger(__name__)

#: Marker prefix that makes a content field a git-sourced reference.
GIT_CONTENT_PREFIX = "git+"

#: Node content fields that may carry a git-sourced ref (FAR-220 scope).
GIT_CONTENT_SCALAR_FIELDS = ("agent_prompt", "script_command")
GIT_CONTENT_LIST_FIELDS = ("agent_commands",)

# A ref suffix / pin: alphanumeric start, then alphanumerics plus . _ / -.
# Branch names like ``feature/x`` and tags like ``v1.0`` are accepted; ``:``
# and ``@`` are not (``:`` marks an SCP host, ``@`` is the separator).
_REF_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._/-]*\Z", re.ASCII)
_SHA_RE = re.compile(r"\A[0-9a-fA-F]{40}\Z", re.ASCII)
# SCP-style clone URL: git@host:path (the ONLY '@' allowed inside the repo
# part — it is git's default user, and credentials are forbidden).
_SCP_URL_RE = re.compile(r"\Agit@[^/:]+:[^@\s]+\Z", re.ASCII)

# Same scheme set as managed workspace inputs (sandbox_mode._SUPPORTED_URL_SCHEMES).
_SUPPORTED_URL_SCHEMES = ("https://", "ssh://", "git@")

#: Bounds for host-side git invocations (fail closed, never hang a plan/run).
LS_REMOTE_TIMEOUT_SECONDS = 30.0
FETCH_TIMEOUT_SECONDS = 60.0


class GitContentRefError(ValueError):
    """A git-sourced content ref is malformed, unpinned-when-must-pin, or unresolvable."""


class GitContentFetchError(GitContentRefError):
    """The content at a pinned git ref could not be fetched (fail closed at render)."""


@dataclass(frozen=True)
class GitContentRef:
    """A parsed ``git+<repo-url>[@<ref>]#<path>`` content reference."""

    repo_url: str
    ref: str | None  # None = HEAD (unpinned)
    path: str

    @property
    def is_pinned(self) -> bool:
        """True when ``ref`` is a full 40-hex commit SHA."""
        return self.ref is not None and bool(_SHA_RE.match(self.ref))

    def spec(self, sha: str | None = None) -> str:
        """Canonical ``git+<repo>@<sha>#<path>`` string (``sha`` defaults to the parsed ref)."""
        resolved = (sha if sha is not None else self.ref) or ""
        ref_part = f"@{resolved}" if resolved else ""
        return f"{GIT_CONTENT_PREFIX}{self.repo_url}{ref_part}#{self.path}"


def is_git_content_ref(value: object) -> bool:
    """True when *value* is a string whose trimmed form starts with ``git+``."""
    return isinstance(value, str) and value.strip().startswith(GIT_CONTENT_PREFIX)


def parse_git_content_ref(value: str) -> GitContentRef:
    """Parse a git content ref, raising :class:`GitContentRefError` on any violation.

    Fail-closed by construction: a value that STARTS with ``git+`` must parse
    completely — there is no partial-accept path (a silently-misparsed
    declarative ref would create permanent plan drift or dispatch the raw ref
    string to the agent).
    """
    if not isinstance(value, str):
        msg = f"git content ref must be a string, got {type(value).__name__}"
        raise GitContentRefError(msg)
    raw = value.strip()
    if not raw.startswith(GIT_CONTENT_PREFIX):
        msg = f"git content ref must start with {GIT_CONTENT_PREFIX!r}, got {value!r}"
        raise GitContentRefError(msg)
    if any(ch in raw for ch in "\r\n"):
        msg = f"git content ref must not contain newlines, got {value!r}"
        raise GitContentRefError(msg)

    body = raw[len(GIT_CONTENT_PREFIX) :]
    if "#" not in body:
        msg = f"git content ref must contain '#<path>' (got {value!r})"
        raise GitContentRefError(msg)
    repo_part, _, path = body.partition("#")
    path = path.strip()
    _validate_ref_path(path, value)
    repo_part = repo_part.strip()
    if not repo_part:
        msg = f"git content ref is missing the repository URL (got {value!r})"
        raise GitContentRefError(msg)

    ref, repo_url = _split_ref_suffix(repo_part, value)
    _validate_repo_url(repo_url, value)
    return GitContentRef(repo_url=repo_url, ref=ref, path=path)


def _validate_ref_path(path: str, original: str) -> None:
    """Repo-relative, non-empty, no traversal / '#' / newline."""
    if not path:
        msg = f"git content ref path must be non-empty (got {original!r})"
        raise GitContentRefError(msg)
    if path.startswith("/"):
        msg = f"git content ref path must be repository-relative, not absolute (got {original!r})"
        raise GitContentRefError(msg)
    if "#" in path:
        msg = f"git content ref allows a single '#' separator (got {original!r})"
        raise GitContentRefError(msg)
    if any(segment == ".." for segment in path.split("/")):
        msg = f"git content ref path must not traverse with '..' (got {original!r})"
        raise GitContentRefError(msg)
    if any(ch in path for ch in "\r\n"):
        msg = f"git content ref path must not contain newlines (got {original!r})"
        raise GitContentRefError(msg)


def _split_ref_suffix(repo_part: str, original: str) -> tuple[str | None, str]:
    """Split an optional ``@<ref>`` suffix off the repository part.

    The LAST ``@`` is the separator when the tail after it looks like a ref
    (``_REF_RE`` — no ``:`` so an SCP ``git@host:path`` user is never eaten).
    ``https://user@host/repo`` (embedded credentials) therefore does NOT split
    as a ref (``user`` has no path … the tail ``host/repo`` WOULD match, so the
    repo-side credential check in :func:`_validate_repo_url` rejects the
    remainder instead).
    """
    if "@" not in repo_part:
        return None, repo_part
    head, _, tail = repo_part.rpartition("@")
    if head and _REF_RE.match(tail):
        return tail, head
    return None, repo_part


def _validate_repo_url(repo_url: str, original: str) -> None:
    """Whitelisted scheme, no embedded credentials, no whitespace."""
    if any(ch.isspace() for ch in repo_url):
        msg = f"git content ref repository URL must not contain whitespace (got {original!r})"
        raise GitContentRefError(msg)
    if not repo_url.startswith(_SUPPORTED_URL_SCHEMES):
        msg = (
            f"git content ref repository URL uses an unsupported scheme (got {original!r}) — "
            f"supported schemes are {sorted(_SUPPORTED_URL_SCHEMES)}"
        )
        raise GitContentRefError(msg)
    if repo_url.startswith("git@"):
        if not _SCP_URL_RE.match(repo_url):
            msg = (
                f"git content ref SCP-style repository URL must be git@host:path with no further "
                f"'@' (got {original!r}) — credentials are forbidden in git content refs"
            )
            raise GitContentRefError(msg)
        return
    parsed = urlsplit(repo_url)
    if "@" in repo_url:
        msg = (
            f"git content ref repository URL must not embed credentials (got {original!r}) — "
            "'@' after the repository is reserved for the @<ref> separator"
        )
        raise GitContentRefError(msg)
    if not parsed.netloc or not parsed.path.strip("/"):
        msg = f"git content ref repository URL is malformed (got {original!r})"
        raise GitContentRefError(msg)


def resolve_against_ls_remote(ref: GitContentRef, refs: dict[str, str]) -> str:
    """Resolve *ref* to a commit SHA against ``git ls-remote`` output.

    ``refs`` is the ``{ref_name: sha}`` map produced by
    :func:`modulo.core.pipeline_engine.workspace_inputs.parse_ls_remote`
    (peeled annotated-tag entries already point at the underlying commit).

    * pinned (40-hex) — returned as-is (lowercased), no lookup needed;
    * ``None`` (HEAD) — the ``HEAD`` entry;
    * otherwise branch first (``refs/heads/``), then tag (``refs/tags/``).

    Raises :class:`GitContentRefError` when the ref cannot be resolved —
    resolution failure is never a silent passthrough.
    """
    if ref.is_pinned and ref.ref is not None:
        return ref.ref.lower()
    if ref.ref is None:
        sha = refs.get("HEAD")
        if sha is None:
            msg = f"HEAD not found in git ls-remote output for {ref.repo_url!r}"
            raise GitContentRefError(msg)
        return _require_sha(sha, f"HEAD of {ref.repo_url!r}")
    for prefix in ("refs/heads/", "refs/tags/"):
        sha = refs.get(prefix + ref.ref)
        if sha is not None:
            return _require_sha(sha, f"{prefix + ref.ref} of {ref.repo_url!r}")
    msg = (
        f"ref {ref.ref!r} of {ref.repo_url!r} not found "
        "(looked up refs/heads/<ref> and refs/tags/<ref> in git ls-remote output)"
    )
    raise GitContentRefError(msg)


def _require_sha(candidate: str, label: str) -> str:
    if not _SHA_RE.match(candidate):
        msg = f"{label} resolved to {candidate!r}, which is not a 40-hex commit SHA"
        raise GitContentRefError(msg)
    return candidate.lower()


def pin_git_content_spec(ref: GitContentRef, sha: str) -> str:
    """Canonical pinned spec string for *ref* at commit *sha*."""
    return ref.spec(_require_sha(sha, "resolver result"))


async def run_git_ls_remote(repo_url: str, *, timeout_seconds: float = LS_REMOTE_TIMEOUT_SECONDS) -> str:
    """Run ``git ls-remote <repo_url>`` and return the raw output (bounded).

    Raises :class:`GitContentRefError` on non-zero exit or timeout so a flaky
    remote surfaces as a typed, observed failure instead of a hang.
    """
    proc = await asyncio.create_subprocess_exec(
        "git",
        "ls-remote",
        repo_url,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout_seconds)
    except TimeoutError:
        proc.kill()
        await proc.communicate()
        msg = f"git ls-remote for {repo_url!r} timed out after {timeout_seconds}s"
        raise GitContentRefError(msg) from None
    if proc.returncode != 0:
        err_tail = (stderr or b"").decode("utf-8", errors="replace").strip()[-400:]
        msg = f"git ls-remote failed for {repo_url!r} (exit {proc.returncode}): {err_tail}"
        raise GitContentRefError(msg)
    return stdout.decode("utf-8", errors="replace")


def default_git_content_resolver(ref: GitContentRef) -> str:
    """Plan-time resolver used by ``modulo apply`` (identity, else ls-remote).

    Pinned refs never touch the network. Movable refs resolve via
    :func:`run_git_ls_remote`; every failure mode raises
    :class:`GitContentRefError` (the CLI maps that to a blocked/failed entity —
    fail closed). Module-level so tests can monkeypatch it without network.
    """
    if ref.is_pinned and ref.ref is not None:
        return ref.ref.lower()
    raw = asyncio.run(run_git_ls_remote(ref.repo_url))
    return resolve_against_ls_remote(ref, parse_ls_remote(raw))


async def fetch_git_content(
    repo_url: str,
    sha: str,
    path: str,
    *,
    timeout_seconds: float = FETCH_TIMEOUT_SECONDS,
) -> str:
    """Fetch the UTF-8 content of *path* at the pinned commit *sha*.

    Host-side clone (``--no-checkout`` for objects only) + ``git show
    <sha>:<path>`` — the same host-side git pattern managed workspace inputs
    use for ``ls-remote``. PUBLIC repositories only in this increment: no
    credential material is ever placed in the clone URL or the process
    environment, and every failure (clone, missing object/path, non-UTF-8
    content, timeout) raises :class:`GitContentFetchError` — the caller fails
    the node rather than dispatching the raw ref string.

    NOTE: like the existing host-side ``ls-remote`` used by managed workspace
    inputs, the URL is scheme-validated but not IP-pinned; private-repository
    credential support is a later increment.
    """
    if not _SHA_RE.match(sha):
        msg = f"fetch_git_content requires a 40-hex pinned SHA, got {sha!r}"
        raise GitContentFetchError(msg)
    workdir = tempfile.mkdtemp(prefix="modulo-git-content-")
    try:
        await _git(
            ("clone", "--quiet", "--no-checkout", repo_url, "."),
            cwd=workdir,
            timeout_seconds=timeout_seconds,
            what="clone",
            repo_url=repo_url,
        )
        content = await _git(
            ("show", f"{sha.lower()}:{path}"),
            cwd=workdir,
            timeout_seconds=timeout_seconds,
            what=f"show {path}",
            repo_url=repo_url,
        )
        try:
            return content.decode("utf-8")
        except UnicodeDecodeError as exc:
            msg = f"content at {path!r} (commit {sha}) of {repo_url!r} is not UTF-8 text: {exc}"
            raise GitContentFetchError(msg) from None
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


async def _git(
    args: tuple[str, ...],
    *,
    cwd: str,
    timeout_seconds: float,
    what: str,
    repo_url: str,
) -> bytes:
    """Run one bounded ``git`` invocation; non-zero exit -> :class:`GitContentFetchError`."""
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout_seconds)
    except TimeoutError:
        proc.kill()
        await proc.communicate()
        msg = f"git {what} for {repo_url!r} timed out after {timeout_seconds}s"
        raise GitContentFetchError(msg) from None
    if proc.returncode != 0:
        err_tail = (stderr or b"").decode("utf-8", errors="replace").strip()[-400:]
        msg = f"git {what} failed for {repo_url!r} (exit {proc.returncode}): {err_tail}"
        raise GitContentFetchError(msg)
    return stdout


async def resolve_git_content_field(
    value: str,
    *,
    node_id: str | None = None,
    field: str = "",
    fetch: Callable[[str, str, str], Awaitable[str]] | None = None,
) -> str:
    """Replace a whole-field git content ref with the file content at its pinned SHA.

    Non-ref values pass through untouched (zero cost for inline content).
    An UNPINNED ref raises :class:`GitContentRefError` — the render point never
    resolves movable refs (the saved graph is required to be pinned, so an
    unpinned value here means the save-time gate was bypassed, e.g. a raw
    workflow import). The resolved pin is logged for audit.
    """
    if not is_git_content_ref(value):
        return value
    ref = parse_git_content_ref(value)
    if not ref.is_pinned or ref.ref is None:
        msg = (
            f"{field or 'content field'} of sandbox_agent node {node_id!r} carries an unpinned "
            f"git content ref {value.strip()!r} — stored graphs must pin the commit "
            f"({GIT_CONTENT_PREFIX}<repo>@<40-hex-sha>#<path>; 'modulo apply' resolves and pins "
            "movable refs automatically, otherwise resolve the ref and pin it manually"
        )
        raise GitContentRefError(msg)
    sha = ref.ref.lower()
    do_fetch = fetch if fetch is not None else fetch_git_content
    content = await do_fetch(ref.repo_url, sha, ref.path)
    # Non-reserved LogRecord keys only (node_id/sha/path/field/repo_url).
    _log.info(
        "git_content.resolved",
        extra={
            "node_id": node_id,
            "field": field,
            "repo_url": ref.repo_url,
            "sha": sha,
            "path": ref.path,
        },
    )
    return content


def git_content_values(node: dict[str, Any]) -> list[tuple[str, str]]:
    """``(label, value)`` pairs for every content field of *node* that could hold a ref."""
    values: list[tuple[str, str]] = []
    for key in GIT_CONTENT_SCALAR_FIELDS:
        value = node.get(key)
        if isinstance(value, str):
            values.append((key, value))
    for key in GIT_CONTENT_LIST_FIELDS:
        items = node.get(key)
        if isinstance(items, list):
            for index, item in enumerate(items):
                if isinstance(item, str):
                    values.append((f"{key}[{index}]", item))
    return values


def pin_git_content_node_fields(
    node_data: dict[str, Any],
    *,
    resolver: Callable[[GitContentRef], str] | None = None,
) -> dict[str, Any]:
    """Resolve + pin every git content ref on a node dict IN PLACE; returns it.

    Used by ``modulo apply`` (plan + write): a movable ref resolves through
    *resolver* (default :func:`default_git_content_resolver`) to a commit SHA
    and the field is rewritten to the canonical pinned spec, so the desired
    managed view, the PATCH payload, and therefore the stored graph / run
    snapshot all carry the same pinned SHA. Resolver failures propagate as
    :class:`GitContentRefError` (CLI: blocked/failed entity — fail closed).
    """
    resolve = resolver if resolver is not None else default_git_content_resolver

    def _pinned(value: str) -> str:
        parsed = parse_git_content_ref(value)
        sha = parsed.ref.lower() if parsed.is_pinned and parsed.ref is not None else resolve(parsed)
        return pin_git_content_spec(parsed, sha)

    for key in GIT_CONTENT_SCALAR_FIELDS:
        value = node_data.get(key)
        if isinstance(value, str) and is_git_content_ref(value):
            node_data[key] = _pinned(value)
    for key in GIT_CONTENT_LIST_FIELDS:
        items = node_data.get(key)
        if isinstance(items, list):
            for index, item in enumerate(items):
                if isinstance(item, str) and is_git_content_ref(item):
                    items[index] = _pinned(item)
    return node_data


__all__ = [
    "FETCH_TIMEOUT_SECONDS",
    "GIT_CONTENT_PREFIX",
    "LS_REMOTE_TIMEOUT_SECONDS",
    "GitContentFetchError",
    "GitContentRef",
    "GitContentRefError",
    "default_git_content_resolver",
    "fetch_git_content",
    "git_content_values",
    "is_git_content_ref",
    "parse_git_content_ref",
    "pin_git_content_node_fields",
    "pin_git_content_spec",
    "resolve_against_ls_remote",
    "resolve_git_content_field",
    "run_git_ls_remote",
]
