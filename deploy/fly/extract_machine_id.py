#!/usr/bin/env python3
"""Extract the launched Fly machine id from ``fly machine run`` output.

flyctl (v0.4.87, the version the deploy step installs via fly.io/install.sh)
prints the newly created machine id on a stable human-readable line::

    Machine ID: 3dXg9aB2cF1eH7k

``fly machine run`` has no ``--json`` flag in that version (cobra rejects the
unknown flag and the command exits non-zero), so the deploy step must parse
the human line rather than a JSON document. This module is the single source
of truth for that parse and is unit-tested so the deploy behaviour cannot rot.
"""

from __future__ import annotations

import re
import sys

# Matches the flyctl v0.4.87 launch line " Machine ID: <id>" (note the leading
# space in flyctl's fmt.Fprintf). Fly machine ids are alphanumeric.
MACHINE_ID_RE = re.compile(r"Machine\s+ID:\s*([A-Za-z0-9]+)")


def extract_machine_id(text: str) -> str | None:
    """Return the first Fly machine id found in *text*, or ``None``."""
    match = MACHINE_ID_RE.search(text)
    return match.group(1) if match else None


def main() -> int:
    mid = extract_machine_id(sys.stdin.read())
    if mid is None:
        return 1
    sys.stdout.write(mid)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
