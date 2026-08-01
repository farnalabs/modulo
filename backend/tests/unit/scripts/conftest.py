"""Fixtures and collection setup for pure-script unit tests.

Insert the repo root on ``sys.path`` so scripts can be imported as the
``scripts.*`` package, and shadow the parent ``tests/unit/conftest.py``
autouse ``_patch_verify_identity`` fixture (which patches
``modulo.auth.dependencies._verify_identity``) so these tests can be
collected and run without importing the full ``modulo`` tree.
"""

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[4]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


@pytest.fixture(autouse=True)
def _patch_verify_identity() -> None:
    """No-op stand-in: the scripts under test never touch the database."""
    yield
