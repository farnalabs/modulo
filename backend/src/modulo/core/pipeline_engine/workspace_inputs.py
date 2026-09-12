"""Pure functions for managed workspace input resolution (FAR-796, ADR 033).

This module owns three responsibilities in the managed-inputs lifecycle:

1. **Parsing** ``git ls-remote`` output into a ref→SHA map
   (:func:`parse_ls_remote`).
2. **Resolving** a requested ``branch|tag|sha`` to a commit SHA
   (:func:`resolve_movable_ref`).
3. **Building** a POSIX ``sh`` clone script that checks out the resolved SHA
   with an assertion guard (:func:`build_input_clone_script`).

The module is intentionally dependency-free (stdlib only) so it can be imported
by the provisioning layer, the validator, and unit tests without dragging
database or LangGraph into them.  All functions are pure: no I/O, no side
effects, no settings imports.
"""

from __future__ import annotations

import shlex

# ---------------------------------------------------------------------------
# parse_ls_remote
# ---------------------------------------------------------------------------


def parse_ls_remote(output: str) -> dict[str, str]:  # vulture: ignore
    """Parse ``git ls-remote`` output into ``{ref_name: object_sha}``.

    Behaviour:

    * Each line is expected to have the form ``<sha>\\t<ref_name>``; lines
      that do not match this shape are silently skipped (truncated output,
      header lines, blank lines).
    * CRLF line endings (``\\r\\n``) are tolerated.
    * Extra whitespace around the SHA or ref name is stripped.
    * **Peeled entries win**: when both ``refs/tags/v1.0`` (the tag object)
      and ``refs/tags/v1.0^{}`` (the underlying commit) appear, the peeled
      entry *replaces* the unpeeled one so the returned SHA is always the
      **commit** SHA for annotated tags.
    * Dangling symrefs (``ref: refs/heads/x\\tHEAD``) are silently ignored —
      the ref resolves to nothing useful and ``HEAD`` is typically not a
      branch or tag the caller wants.
    * The function never raises — malformed input yields a partial or empty
      dict rather than a crash.
    """
    refs: dict[str, str] = {}
    for raw_line in output.splitlines():
        line = line.strip() if (line := raw_line.strip("\r\n")) else raw_line
        if not line or "\t" not in line:
            continue
        parts = line.split("\t", 1)
        if len(parts) != 2:
            continue
        sha_candidate, ref_name = parts[0].strip(), parts[1].strip()
        # Skip dangling symrefs — ``ref: refs/heads/x`` is not a real SHA.
        if sha_candidate.startswith("ref:"):
            continue
        # Validate hex SHA (40 chars) or at least non-empty.
        if not sha_candidate or not ref_name:
            continue
        # Prefer peeled entries (^{}) so annotated tags resolve to the
        # underlying commit SHA.
        if ref_name.endswith("^{}"):
            base_ref = ref_name[:-3]
            refs[base_ref] = sha_candidate
        # Only store if not already replaced by a peel entry.
        elif ref_name not in refs:
            refs[ref_name] = sha_candidate
    return refs


# ---------------------------------------------------------------------------
# resolve_movable_ref
# ---------------------------------------------------------------------------

_VALID_KINDS = frozenset({"branch", "tag", "sha"})


class RefResolutionError(Exception):
    """Raised when a movable ref cannot be resolved to a commit SHA."""


def resolve_movable_ref(refs: dict[str, str], kind: str, value: str) -> str:  # vulture: ignore
    """Resolve a requested ``branch|tag|sha`` to a commit SHA.

    * ``kind="sha"``: return *value* as-is (caller validates the hex shape).
    * ``kind="branch"``: look up ``refs/heads/<value>`` in *refs*.
    * ``kind="tag"``: look up ``refs/tags/<value>`` in *refs*.

    Raises :class:`RefResolutionError` with a clear message on not-found or
    unknown *kind*.

    The persisted SHA is always a **commit** — the ``parse_ls_remote`` peel
    preference guarantees that annotated-tag entries already point at the
    underlying commit, not the tag object.
    """
    if kind not in _VALID_KINDS:
        raise RefResolutionError(f"unknown ref kind {kind!r} — expected one of {sorted(_VALID_KINDS)}")
    if kind == "sha":
        return value
    prefix_map: dict[str, str] = {
        "branch": "refs/heads/",
        "tag": "refs/tags/",
    }
    full_ref = prefix_map[kind] + value
    sha = refs.get(full_ref)
    if sha is None:
        raise RefResolutionError(f"{kind!r} ref {value!r} (resolved to {full_ref!r}) not found in ls-remote output")
    return sha


# ---------------------------------------------------------------------------
# build_input_clone_script
# ---------------------------------------------------------------------------


def build_input_clone_script(*, url: str, dest: str, resolved_sha: str) -> str:  # vulture: ignore
    """Build a POSIX ``sh`` script string that clones and checks out a SHA.

    Requirements (FAR-796 ADR 033 §5):

    * Every interpolated value (url, dest, sha) is passed through
      ``shlex.quote()`` to prevent injection.
    * ``set -e`` for fail-fast.
    * Clone, then ``git checkout <resolved_sha>`` (not a ref name) so a
      force-push between resolve and clone cannot drift the checkout.
    * A trailing ``git rev-parse HEAD`` assertion that the checkout SHA
      matches the resolved SHA; exit non-zero on mismatch.
    * **No credential material** in the script or the URL — credentials are
      supplied by a separate provisioning mechanism.
    """
    quoted_url = shlex.quote(url)
    quoted_dest = shlex.quote(dest)
    quoted_sha = shlex.quote(resolved_sha)
    return (
        "#!/bin/sh\n"
        "set -e\n"
        f"git clone {quoted_url} {quoted_dest}\n"
        f"git -C {quoted_dest} checkout {quoted_sha}\n"
        f"ACTUAL=$(git -C {quoted_dest} rev-parse HEAD)\n"
        f'if [ "$ACTUAL" != {quoted_sha} ]; then\n'
        f'  echo "SHA mismatch: expected {quoted_sha}, got $ACTUAL" >&2\n'
        "  exit 1\n"
        "fi\n"
    )
