"""Unit tests for _delete_run_artifacts (FAR-582 artifact side-car cleanup).

Tests the module-level callback extracted from the purge handler for S3776.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from modulo.api.routes.admin_run_retention import _delete_run_artifacts


def _make_run(org_id: str | None = None, run_id: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        organisation_id=uuid.UUID(org_id or "00000000-0000-0000-0000-000000000001"),
        id=uuid.UUID(run_id or "00000000-0000-0000-0000-000000000002"),
    )


# ── happy path ────────────────────────────────────────────────────────


def test_delete_run_artifacts_happy_path():
    """Each run's delete_run is called on the store."""
    mock_store = MagicMock()
    run1 = _make_run()
    run2 = _make_run(org_id="00000000-0000-0000-0000-000000000003")

    with patch("modulo.core.artifacts.store.get_store", return_value=mock_store):
        _delete_run_artifacts([run1, run2], uuid.UUID("00000000-0000-0000-0000-000000000001"))

    assert mock_store.delete_run.call_count == 2
    mock_store.delete_run.assert_any_call(str(run1.organisation_id), str(run1.id))
    mock_store.delete_run.assert_any_call(str(run2.organisation_id), str(run2.id))


# ── store unavailable ────────────────────────────────────────────────


def test_delete_run_artifacts_store_unavailable():
    """When get_store() raises, the callback swallows the error — no deletion attempted."""
    mock_get_store = MagicMock(side_effect=RuntimeError("boom"))
    with patch("modulo.core.artifacts.store.get_store", mock_get_store):
        # Should not raise (best-effort store access)
        _delete_run_artifacts([_make_run()], None)
    # get_store was exercised (proving the code path ran)
    mock_get_store.assert_called_once()


# ── per-run delete failure ───────────────────────────────────────────


def test_delete_run_artifacts_per_run_failure():
    """When one run's delete_run fails, others still proceed."""
    mock_store = MagicMock()
    mock_store.delete_run.side_effect = [OSError("fail"), None]

    run1 = _make_run()
    run2 = _make_run(org_id="00000000-0000-0000-0000-000000000003")

    with patch("modulo.core.artifacts.store.get_store", return_value=mock_store):
        # Should not raise
        _delete_run_artifacts([run1, run2], uuid.UUID("00000000-0000-0000-0000-000000000001"))

    # Both calls were attempted
    assert mock_store.delete_run.call_count == 2
