"""Package-local test isolation for the ``cron_helpers`` unit suite (FAR-1612).

The unit-level ``_far681_any_credential_default`` autouse fixture
(``tests/unit/conftest.py``) imports the entire FastAPI application
(``modulo.api.main``) on first use. No test under ``tests/unit/cron_helpers/``
touches the app or its dependency overrides -- they exercise cron scheduling /
dispatcher-reconcile logic entirely against mocks -- so for this package that
import is pure overhead: ~13-25 s of CPU-bound pydantic model construction,
paid again by every fresh test process.

That import is also this suite's dominant, load-sensitive cost. On a busy host
(all agents share the machine) it balloons far past the per-test budget, so a
whole-file run spends most of its wall-clock importing the app before the first
dot is printed -- the shape that reads as a hang. Overriding the fixture with a
no-op for this package removes the app import; behaviour is unchanged because
no module here resolves a FastAPI dependency (verified: the whole
``tests/unit/cron_helpers/`` tree has zero references to ``get_current_user``,
``dependency_overrides``, ``TestClient`` or ``modulo.api``).

Caveat (FAR-1229): pytest keys a conftest's autouse-fixture names to the exact
``Package`` node current when that conftest was parsed, so an argv that detours
out of ``tests/unit/cron_helpers/`` and back in -- e.g.
``pytest tests/unit/cron_helpers/a.py tests/unit/test_x.py
tests/unit/cron_helpers/b.py`` -- collects the later file under a fresh node
whose autouse names were never registered, and this shadow silently drops for
it (the app-importing parent fixture returns). Perf-only here -- no
``cron_helpers`` test needs the overrides -- unlike the 401 breakage FAR-1229
describes.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _far681_any_credential_default() -> None:
    """Package-local no-op override of the unit-level app-importing fixture.

    Shadows ``tests/unit/conftest.py``'s same-named autouse fixture for the
    ``cron_helpers`` package only. See the module docstring for why this package
    never needs the FastAPI app or its dependency overrides.
    """
