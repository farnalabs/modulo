#!/usr/bin/env python3
"""Reject a native-release-smoke manual dispatch that names no target (FAR-2161).

``native-release-smoke.yml`` has three triggers, and only ``workflow_dispatch``
can be fired with no argument at all. An argument-free dispatch runs NO
verification: ``release-smoke`` is push-only, ``canary`` requires a non-empty
``canary_version`` and ``rollback-verify`` requires a non-empty
``rollback_version``, so the run shrinks to ``canary-housekeeping`` (whose
steps are gated on the canary job's result) and concludes SUCCESS having
verified nothing - the FAR-2152 class of a green run that did no work.

The workflow runs this guard as its first job on a manual dispatch. It exits 1
with a message naming both accepted inputs when neither is supplied, and exits
0 otherwise (each single-input dispatch keeps its own job gate).

Inputs are read from the environment (``ROLLBACK_VERSION``, ``CANARY_VERSION``)
so the workflow step maps the dispatch inputs without interpolating them into a
shell command.

Unit test: backend/tests/architecture/test_workflow_noop_signal.py.
"""

from __future__ import annotations

import os
import sys


def dispatch_error(rollback_version: str | None, canary_version: str | None) -> str | None:
    """Return the rejection message for an argument-free dispatch, else ``None``."""
    if (rollback_version or "").strip() or (canary_version or "").strip():
        return None
    return (
        "a manual dispatch must name a target: set canary_version (smoke a published release and its N-1 "
        "upgrade) or rollback_version (verify a rollback target). With neither, this workflow would run only "
        "its tracking housekeeping and verify nothing - refusing to report success for a run that checked "
        "nothing."
    )


def main() -> int:
    canary_version = os.environ.get("CANARY_VERSION")
    rollback_version = os.environ.get("ROLLBACK_VERSION")
    error = dispatch_error(rollback_version, canary_version)
    if error is not None:
        print(f"::error::{error}")
        return 1
    print(
        "dispatch target present: "
        f"canary_version={canary_version or '<unset>'}, rollback_version={rollback_version or '<unset>'}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
