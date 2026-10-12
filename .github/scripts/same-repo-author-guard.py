#!/usr/bin/env python3
"""Same-repo author gate for the autonomous PR pipeline (FAR-264).

A pull request may only be auto-merged (merge-queue.yml) or auto-reviewed
(pr-review.yml) when it comes from a SAME-REPO author - i.e. its head branch
lives on the base repository itself and its author carries a trusted
``author_association``. A fork PR, or any PR whose author association is
missing/untrusted, is EXTERNAL and must go through a human/approved path; the
autonomous pipeline must not touch it.

Reads the GitHub REST ``GET /repos/{owner}/{repo}/pulls/{number}`` response
(written to a file by the calling workflow via ``gh api``) plus the base
repository full name, and prints exactly one verdict line to stdout::

    ok|<reason>        -> same-repo author; auto-merge/auto-review allowed
    external|<reason>  -> fork / external / unverifiable author; fail CLOSED

Fail-closed contract (mirrors ``merge-queue-approve-guard.py``): any missing
or unparseable input - head repository absent (e.g. a deleted fork),
``author_association`` absent, payload mismatch against the caller's base repo
- yields ``external``, never ``ok``. An external verdict means "do not
auto-merge / do not auto-review"; a human path is required.

Unit tests: backend/tests/unit/scripts/test_same_repo_author_guard.py.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Associations GitHub computes for a PR author. Only these four are trusted for
# autonomous processing: the repo owner, org members, explicit collaborators,
# and authors who have previously committed to the repository. Everything else
# (NONE, FIRST_TIMER, FIRST_TIME_CONTRIBUTOR, MANNEQUIN, ...) is external.
TRUSTED_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR", "CONTRIBUTOR"})


def _resolve_head(pr: dict, base_repo: str) -> tuple[str, str]:
    """Return (head_full_name, error_reason); error_reason is '' on success.

    Also cross-checks the payload's own base against the caller's repository so
    a mis-targeted payload can never grant a pass on a foreign PR.
    """
    head_repo = (pr.get("head") or {}).get("repo") or {}
    head_full = head_repo.get("full_name")
    if not head_full:
        # head.repo is null when the fork has been deleted, and absent on a
        # malformed payload - either way the PR head cannot be verified.
        return "", "could not resolve PR head repository - fail closed"
    payload_base = ((pr.get("base") or {}).get("repo") or {}).get("full_name")
    if payload_base and payload_base.lower() != base_repo.lower():
        return (
            "",
            f"payload base repo ({payload_base}) does not match caller base repo ({base_repo}) - fail closed",
        )
    return head_full, ""


def _resolve_association(pr: dict) -> tuple[str, str]:
    """Return (association, error_reason); error_reason is '' on success."""
    association = pr.get("author_association")
    if not association or not isinstance(association, str):
        return "", "author_association missing - fail closed"
    if association not in TRUSTED_ASSOCIATIONS:
        return (
            "",
            f"author_association '{association}' is not a trusted same-repo association - fail closed",
        )
    return association, ""


def decide(pr: dict, base_repo: str) -> tuple[str, str]:
    """Return (verdict, reason). verdict is ``'ok'`` or ``'external'``."""
    if not base_repo or not isinstance(base_repo, str):
        return "external", "base repository argument missing - fail closed"
    if not isinstance(pr, dict):
        return "external", "PR payload is not a JSON object - fail closed"

    head_full, head_err = _resolve_head(pr, base_repo)
    if head_err:
        return "external", head_err
    if head_full.lower() != base_repo.lower():
        return (
            "external",
            f"PR head is from {head_full}, not {base_repo} (fork / external contributor) - fail closed",
        )

    association, association_err = _resolve_association(pr)
    if association_err:
        return "external", association_err

    author = (pr.get("user") or {}).get("login") or "?"
    return "ok", f"same-repo author '{author}' (association={association}, head={head_full})"


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(
            "external|usage: same-repo-author-guard.py <pr.json> <base_repo_full_name>",
            flush=True,
        )
        return 2
    # ``argv[1]`` is a CLI argument supplied by the calling workflow
    # (.github/workflows/merge-queue.yml and pr-review.yml pass a ``gh api``
    # output path); it is never user- or model-supplied, so this S8707 taint
    # flow is a false positive.
    try:
        with Path(argv[1]).open(encoding="utf-8") as fh:  # NOSONAR pythonsecurity:S8707
            pr = json.load(fh)
    except Exception as exc:  # fail closed on any I/O or JSON error
        print(f"external|error loading PR payload: {exc} - fail closed", flush=True)
        return 1
    verdict, reason = decide(pr, argv[2])
    print(f"{verdict}|{reason}", flush=True)
    return 0 if verdict == "ok" else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
