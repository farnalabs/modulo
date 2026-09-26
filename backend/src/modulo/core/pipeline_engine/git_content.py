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

Fetch bounds (host-side runs)
----------------------------
The run-time fetch (:func:`fetch_git_content`) does NOT clone the whole
repository: it creates an empty working tree, adds the remote, and fetches the
pinned commit with progressively larger bounded strategies —
``fetch --depth 1 --filter=blob:none``, then ``--depth 1``, then a full fetch
(bounded by :data:`FETCH_TIMEOUT_SECONDS`) — so a pinned-SHA-only run never
transfers the repository's history. The shallow probes require the remote to
support uploadpack reachability-by-SHA for ``fetch <sha>`` (GitHub/GitLab do);
when they are refused, the full fetch is the fallback.

Retention is additionally capped even after a *successful* transfer: if the
fetched working tree exceeds :data:`REPO_MAX_CLONE_BYTES`, the content is
refused (a fetched-but-unbounded artifact is still an exfiltration surface).
``git`` runs with prompts disabled (``GIT_TERMINAL_PROMPT=0`` +
``BatchMode=yes``) so a credential-hungry private URL fails closed instead of
hanging the run.

Repeated (repo, sha, path) renders are served from a small on-disk cache
(default under ``tempfile.gettempdir()/modulo-git-content-cache``, per worker
host): TTL :data:`CONTENT_CACHE_TTL_SECONDS`, entry cap
:data:`CONTENT_CACHE_MAX_ENTRIES`, values larger than
:data:`CONTENT_CACHE_MAX_VALUE_BYTES` are never cached, writes are atomic
(TempFile + ``os.replace``), the directory is ``chmod 0o700`` where supported,
and every key-carrying file binds its own ``(repo_url, sha, path)`` so a
traversal into the cache cannot serve a wrong key's content. The SSRF host
gate (below) runs BEFORE the cache is consulted — the boundary is
unconditional.

SSRF host pinning (best-effort for git transports)
--------------------------------------------------
Every host-side git invocation (:func:`run_git_ls_remote`,
:func:`fetch_git_content`) validates the repository host through
:func:`require_public_git_host` — the same ``modulo.core.ssrf`` resolver the
HTTP connectors use (loopback/private/link-local/metadata addresses refused,
DNS fail-closed). Residual: git spawns its own libcurl/ssh stack, which has no
``CURLOPT_RESOLVE``-style IP pinning hook, so a TOCTOU DNS-rebinding window
between this pre-spawn validation and git's own DNS lookup cannot be closed
without breaking TLS SNI; compensating controls are the pre-spawn gate, the
credential-free public-only scheme set, prompts-off transport env, and the
fetch/transfer bounds. Closing it fully is tied to the private-repository
credential increment (which must also re-consider credential handling —
currently public-only and fail-closed).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
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

#: Post-fetch retention cap for a fetched working tree (Minor 2 / FAR-220).
#: Timeout bounds the transfer; this bounds what is RETAINED on disk.
REPO_MAX_CLONE_BYTES = 512 * 1024 * 1024

# Content-cache bounds (see the module docstring):
CONTENT_CACHE_TTL_SECONDS = 24 * 3600.0
CONTENT_CACHE_MAX_ENTRIES = 256
CONTENT_CACHE_MAX_VALUE_BYTES = 1024 * 1024
CONTENT_CACHE_DIR_NAME = "modulo-git-content-cache"


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


def git_repository_host_url(repo_url: str) -> str:
    """Host-bound pseudo-URL (``https://host[:port]``) for SSRF gate probing.

    Mapping: ``https://host[:port]/...`` -> as-is; ``ssh://[user@]host[:port]/...``
    -> ``https://host[:port]`` (userinfo stripped — :mod:`modulo.core.ssrf`
    refuses userinfo; the HOST is what the gate validates); SCP-style
    ``git@host:path`` -> ``https://host``.

    Raises :class:`GitContentRefError` when no host can be extracted.
    """
    if repo_url.startswith("git@"):
        host = repo_url[len("git@") :].partition(":")[0]
        if not host:
            msg = f"cannot extract the repository host from {repo_url!r}"
            raise GitContentRefError(msg)
        return f"https://{host}"
    netloc = urlsplit(repo_url).netloc
    if not netloc:
        msg = f"cannot extract the repository host from {repo_url!r}"
        raise GitContentRefError(msg)
    if "@" in netloc:  # ssh://user@host[:port] — ssrf refuses userinfo; keep host[:port] only
        netloc = netloc.rpartition("@")[2]
    return f"https://{netloc}"


async def require_public_git_host(repo_url: str) -> None:
    """SSRF gate for the repository host — the SAME policy HTTP connectors use.

    Maps *repo_url* to its host via :func:`git_repository_host_url`, then runs
    the shared ``modulo.core.ssrf`` resolver (refuses loopback / private /
    link-local / metadata addresses; DNS is fail-closed). The function-level
    import avoids the (currently absent but easy to create) import cycle.

    Every failure raises :class:`GitContentFetchError` — security boundaries
    fail closed. This is a pre-spawn mitigation only; see the module docstring
    for the residual DNS-TOCTOU window and its compensating controls.
    """
    from modulo.core.ssrf import resolve_pinned_ip

    target = git_repository_host_url(repo_url)
    try:
        await resolve_pinned_ip(target)
    except ValueError as exc:
        msg = f"repository host refused (SSRF validation): {exc}"
        raise GitContentFetchError(msg) from None


def _git_process_env() -> dict[str, str]:
    """Host-side git environment: refuse credential prompts / ssh hangs.

    ``GIT_TERMINAL_PROMPT=0`` makes http(s) auth failures exit non-zero instead
    of prompting; ``BatchMode=yes`` does the same for the ssh transport unless
    an operator-provided ``GIT_SSH_COMMAND`` is already set. Public-only
    repositories need no credentials, so both paths fail closed.
    """
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    if not env.get("GIT_SSH_COMMAND"):
        env["GIT_SSH_COMMAND"] = "ssh -o BatchMode=yes"
    return env


async def run_git_ls_remote(repo_url: str, *, timeout_seconds: float = LS_REMOTE_TIMEOUT_SECONDS) -> str:
    """Run ``git ls-remote <repo_url>`` and return the raw output (bounded).

    Refuses internal/loopback hosts via :func:`require_public_git_host` first
    (same gate the run-time fetch applies). Raises
    :class:`GitContentRefError` on non-zero exit or timeout so a flaky remote
    surfaces as a typed, observed failure instead of a hang.
    """
    await require_public_git_host(repo_url)
    proc = await asyncio.create_subprocess_exec(
        "git",
        "ls-remote",
        repo_url,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=_git_process_env(),
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
    max_clone_bytes: int = REPO_MAX_CLONE_BYTES,
    cache_dir: str | Path | None = None,
    use_cache: bool = True,
) -> str:
    """Fetch the UTF-8 content of *path* at the pinned commit *sha*.

    Host-side fetch at the pinned SHA (see the module docstring for the
    shallow-with-fallback strategy and its server-support caveat) + ``git show
    <sha>:<path>`` — the same host-side git pattern managed workspace inputs
    use for ``ls-remote``. PUBLIC repositories only: no credential material is
    ever placed in the remote URL or the process environment, prompts are
    disabled, and every failure (host refused, fetch strategy exhausted,
    missing object/path, oversized clone, non-UTF-8 content, timeout) raises
    :class:`GitContentFetchError` — the caller fails the node rather than
    dispatching the raw ref string.

    Results are served from an on-disk content cache keyed by
    ``(repo_url, sha, path)`` (pinned content is immutable) so repeated runs
    do not re-fetch; see the module docstring. The SSRF host gate runs before
    the cache is consulted — the boundary is unconditional.
    """
    if not _SHA_RE.match(sha):
        msg = f"fetch_git_content requires a 40-hex pinned SHA, got {sha!r}"
        raise GitContentFetchError(msg)
    await require_public_git_host(repo_url)
    if use_cache:
        cached = _cache_get(repo_url, sha, path, cache_dir=cache_dir)
        if cached is not None:
            return cached
    workdir = tempfile.mkdtemp(prefix="modulo-git-content-")
    try:
        await _git(
            ("init", "--quiet", "."), cwd=workdir, timeout_seconds=timeout_seconds, what="init", repo_url=repo_url
        )
        await _git(
            ("remote", "add", "origin", repo_url),
            cwd=workdir,
            timeout_seconds=timeout_seconds,
            what="remote add",
            repo_url=repo_url,
        )
        await _bounded_fetch_attempts(sha, workdir=workdir, repo_url=repo_url, timeout_seconds=timeout_seconds)
        _assert_clone_size_bounded(workdir, max_clone_bytes, repo_url)
        raw = await _git(
            ("show", f"{sha.lower()}:{path}"),
            cwd=workdir,
            timeout_seconds=timeout_seconds,
            what=f"show {path}",
            repo_url=repo_url,
        )
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            msg = f"content at {path!r} (commit {sha}) of {repo_url!r} is not UTF-8 text: {exc}"
            raise GitContentFetchError(msg) from None
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    if use_cache and len(content.encode("utf-8")) <= CONTENT_CACHE_MAX_VALUE_BYTES:
        _cache_put(repo_url, sha, path, content, cache_dir=cache_dir)
    return content


async def _bounded_fetch_attempts(
    sha: str,
    *,
    workdir: str,
    repo_url: str,
    timeout_seconds: float,
) -> None:
    """Fetch commit *sha* trying progressively larger bounded strategies.

    Order (module docstring): shallow+blobless probe, plain shallow probe,
    full fetch of the pinned SHA. First success wins; when all fail the typed
    error lists every attempt — never a silent partial.
    """
    strategies: tuple[tuple[str, tuple[str, ...]], ...] = (
        ("shallow+blobless", ("fetch", "--quiet", "--depth", "1", "--filter=blob:none", "--no-tags", "origin", sha)),
        ("shallow", ("fetch", "--quiet", "--depth", "1", "--no-tags", "origin", sha)),
        ("full", ("fetch", "--quiet", "--no-tags", "origin", sha)),
    )
    failures: list[str] = []
    for label, args in strategies:
        try:
            await _git(args, cwd=workdir, timeout_seconds=timeout_seconds, what=f"fetch ({label})", repo_url=repo_url)
            return
        except GitContentFetchError as exc:
            # Best-effort progress log for each strategy the server refused;
            # the eventual failure below is never silent.
            _log.warning(
                "git_content.fetch_attempt_failed",
                extra={"attempt": label, "repo_url": repo_url, "error": str(exc)[:200]},
            )
            failures.append(f"{label}: {exc}")
    msg = (
        f"could not fetch commit {sha} of {repo_url!r} — every bounded fetch strategy failed "
        "(shallow+blobless, shallow, full): " + " | ".join(failures)
    )
    raise GitContentFetchError(msg)


def _assert_clone_size_bounded(workdir: str, max_clone_bytes: int, repo_url: str) -> None:
    """Fail closed when the retained clone exceeds *max_clone_bytes*."""
    total = 0
    for root, _dirs, files in os.walk(workdir):
        for name in files:
            with contextlib.suppress(OSError):
                total += (Path(root) / name).stat().st_size
    if total > max_clone_bytes:
        msg = (
            f"fetched clone of {repo_url!r} is {total} bytes, exceeding the {max_clone_bytes}-byte "
            "clone size cap — refusing to render git content from it"
        )
        raise GitContentFetchError(msg)


def default_content_cache_dir() -> Path:
    """Root of the on-disk content cache (namespaced under the user's tmp)."""
    return Path(tempfile.gettempdir()) / CONTENT_CACHE_DIR_NAME


def _cache_dir(root: str | Path | None) -> Path:
    directory = Path(root) if root is not None else default_content_cache_dir()
    directory.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        directory.chmod(0o700)  # best-effort on platforms without POSIX modes
    return directory


def _cache_key(repo_url: str, sha: str, path: str) -> str:
    payload = json.dumps([repo_url, sha.lower(), path]).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _cache_get(
    repo_url: str,
    sha: str,
    path: str,
    *,
    cache_dir: str | Path | None = None,
    ttl_seconds: float = CONTENT_CACHE_TTL_SECONDS,
) -> str | None:
    """Cached content for ``(repo, sha, path)``, or ``None`` on miss/TTL/staleness."""
    entry = _cache_dir(cache_dir) / f"{_cache_key(repo_url, sha, path)}.json"
    try:
        data = json.loads(entry.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not (
        isinstance(data, dict)
        and data.get("repo_url") == repo_url
        and data.get("sha") == sha.lower()
        and data.get("path") == path
        and isinstance(data.get("content"), str)
        and isinstance(data.get("fetched_at"), (int, float))
    ):
        # Key-binding mismatch or malformed entry: treat as a miss AND drop it.
        entry.unlink(missing_ok=True)
        return None
    if (time.time() - float(data["fetched_at"])) > ttl_seconds:
        entry.unlink(missing_ok=True)
        return None
    return str(data["content"])


def _cache_put(
    repo_url: str,
    sha: str,
    path: str,
    content: str,
    *,
    cache_dir: str | Path | None = None,
    max_entries: int = CONTENT_CACHE_MAX_ENTRIES,
) -> None:
    """Persist the entry atomically; evict expired, then oldest-over-cap entries."""
    directory = _cache_dir(cache_dir)
    target = directory / f"{_cache_key(repo_url, sha, path)}.json"
    tmp = directory / f".tmp-{uuid.uuid4().hex}"
    tmp.write_text(
        json.dumps(
            {"repo_url": repo_url, "sha": sha.lower(), "path": path, "fetched_at": time.time(), "content": content}
        ),
        encoding="utf-8",
    )
    tmp.replace(target)
    now = time.time()
    # Drop TTL-expired entries first, then the oldest beyond the entry cap.
    for candidate in directory.glob("*.json"):
        if now - _mtime_or_zero(candidate) > CONTENT_CACHE_TTL_SECONDS:
            candidate.unlink(missing_ok=True)
    entries = sorted(directory.glob("*.json"), key=_mtime_or_zero)
    for stale in entries[: max(0, len(entries) - max_entries)]:
        stale.unlink(missing_ok=True)


def _mtime_or_zero(entry: Path) -> float:
    try:
        return entry.stat().st_mtime
    except OSError:
        return 0.0


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
            f"({GIT_CONTENT_PREFIX}<repo>@<40-hex-sha>#<path>); 'modulo apply' resolves and pins "
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

    Only ``sandbox_agent`` nodes are pinned (Minor 2): the run-time resolver
    fires exclusively for sandbox configs, so pinning a ref on any other node
    type would rewrite a raw ref into a literal prompt nothing ever fetches —
    no content substitution and no drift signal. Non-sandbox nodes are
    returned unchanged (same discriminator as the executor / graph cache:
    ``str(node_type).strip() == "sandbox_agent"``).
    """
    if str(node_data.get("node_type", "")).strip() != "sandbox_agent":
        return node_data
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
    "CONTENT_CACHE_DIR_NAME",
    "CONTENT_CACHE_MAX_ENTRIES",
    "CONTENT_CACHE_MAX_VALUE_BYTES",
    "CONTENT_CACHE_TTL_SECONDS",
    "FETCH_TIMEOUT_SECONDS",
    "GIT_CONTENT_PREFIX",
    "LS_REMOTE_TIMEOUT_SECONDS",
    "REPO_MAX_CLONE_BYTES",
    "GitContentFetchError",
    "GitContentRef",
    "GitContentRefError",
    "default_content_cache_dir",
    "default_git_content_resolver",
    "fetch_git_content",
    "git_content_values",
    "git_repository_host_url",
    "is_git_content_ref",
    "parse_git_content_ref",
    "pin_git_content_node_fields",
    "pin_git_content_spec",
    "require_public_git_host",
    "resolve_against_ls_remote",
    "resolve_git_content_field",
    "run_git_ls_remote",
]
