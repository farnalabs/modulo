"""FAR-1402: pin EVERY copy of the GitHub-PR-URL regex — pattern AND flags.

The GitHub-PR-URL grammar is deliberately duplicated across layers, because
a single shared definition cannot serve every consumer:

* ``modulo.db`` may not import ``modulo.core`` (import-linter contract
  ``db-does-not-import-core``), so ``db.crud.run`` cannot reuse the
  extractor's constant;
* ``sandbox_policy`` must stay dependency-free (no LangGraph, no DB), so it
  cannot import ``node_runner`` either.

Mirroring is therefore a layering trade-off — but an UNPINNED mirror is a
drift trap: an isolated edit to any copy silently changes what counts as a
delivered or duplicate PR (FAR-1274 detection, FAR-1315 claim settle) with
no other failing test. This module pins every copy to ONE canonical pattern
string and an exact per-copy flag mask, so ANY drift — pattern or flags, on
any copy — fails here.

Copies pinned:

* ``core.pipeline_engine.node_runner._PR_URL_PATTERN`` — FAR-188 raw-output
  ``pr_url`` extractor (the canonical pattern, case-sensitive);
* ``db.crud.run._PR_URL_PATTERN`` — FAR-1274 one-PR-per-run detector
  harvest; deliberately ``re.IGNORECASE`` (the ONE pinned flag divergence,
  justified at its definition in ``run.py`` and exercised by
  ``test_run_one_pr_per_run.py``);
* ``core.pipeline_engine.sandbox_policy._GH_PR_URL_RE`` — FAR-1315
  corroboration mirror, present only once that work lands; looked up
  defensively so it is pinned the moment it exists, without this test
  failing on a tree that does not carry it yet.
"""

from __future__ import annotations

import re

import modulo.core.pipeline_engine.node_runner as node_runner
import modulo.core.pipeline_engine.sandbox_policy as sandbox_policy
import modulo.db.crud.run as run_crud

#: The ONE canonical GitHub-PR-URL grammar every copy must compile verbatim.
_PR_URL_PATTERN = r"https?://github\.com/[A-Za-z\d_.-]+/[A-Za-z\d_.-]+/pull/\d+"


def _copies() -> dict[str, tuple[re.Pattern[str], int]]:
    """Every PR-URL regex copy in this tree with its exact pinned flags.

    The flag masks are absolute (not pairwise comparisons): the extractor
    and corroboration copies compile with the str-pattern default only
    (``re.UNICODE``), the detector copy adds exactly ``re.IGNORECASE``. An
    absolute mask fails when a flag is added to, or dropped from, ANY copy —
    including a change applied consistently to all of them.
    """
    copies: dict[str, tuple[re.Pattern[str], int]] = {
        "node_runner._PR_URL_PATTERN": (node_runner._PR_URL_PATTERN, re.UNICODE),
        "db.crud.run._PR_URL_PATTERN": (run_crud._PR_URL_PATTERN, re.UNICODE | re.IGNORECASE),
    }
    corroboration = getattr(sandbox_policy, "_GH_PR_URL_RE", None)
    if corroboration is not None:
        copies["sandbox_policy._GH_PR_URL_RE"] = (corroboration, re.UNICODE)
    return copies


def test_pr_url_regex_copies_all_share_one_pattern() -> None:
    """No copy may drift from the canonical pattern text (FAR-1402)."""
    for label, (pattern, _) in _copies().items():
        assert pattern.pattern == _PR_URL_PATTERN, (
            f"{label} pattern drifted from the canonical GitHub-PR-URL grammar; "
            "update every copy together — they are pinned precisely so this cannot happen silently"
        )


def test_pr_url_regex_copy_flags_are_pinned() -> None:
    """Each copy's flags are exact: the detector's ``re.IGNORECASE`` is the one pinned divergence (FAR-1402).

    Absolute masks, not pairwise equality: dropping ``re.IGNORECASE`` from
    the detector (losing the case-variant second-PR sighting its sweep
    exists to catch) and adding it to the extractor (letting case-variant
    agent prose win the first-match persisted as the canonical marker
    ``pr_url``) BOTH fail here even though they would keep the copies
    pairwise-equal or leave them untouched respectively.
    """
    for label, (pattern, expected_flags) in _copies().items():
        assert pattern.flags == expected_flags, (
            f"{label} flags drifted: expected {expected_flags} (0x{expected_flags:x}), "
            f"got {pattern.flags} (0x{pattern.flags:x})"
        )
