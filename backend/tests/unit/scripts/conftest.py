"""Make the ``scripts/`` directories importable for unit tests in this package.

Two distinct ``scripts/`` trees are involved: ``backend/scripts`` holds the
backend helper scripts (for example ``check_partner_eligibility``), while the
repository-root ``scripts`` holds the CI/automation helpers (for example
``fast_lane_audit``). Import both so neither set of tests regresses.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS_DIRS = (
    Path(__file__).resolve().parents[3] / "scripts",
    Path(__file__).resolve().parents[4] / "scripts",
)
for _scripts_dir in _SCRIPTS_DIRS:
    if str(_scripts_dir) not in sys.path:
        sys.path.insert(0, str(_scripts_dir))
