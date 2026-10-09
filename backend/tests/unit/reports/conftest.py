"""Package-wide fixtures for the reports unit test package."""

from __future__ import annotations

import pytest

from modulo.core.reports import scheduler as sched_mod


@pytest.fixture(autouse=True)
def _isolated_report_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give every test a private report registry.

    ``register_report_type`` mutates the module-level ``_generators`` /
    ``_formatters`` / ``_deliverers`` dicts, and the built-in ``quality`` /
    ``cost`` registrations are installed at import time. Without isolation a
    test that registers a report type permanently pollutes the process.
    ``monkeypatch.setattr`` swaps in fresh dicts and restores the originals at
    teardown, so the import-time registrations survive beyond each test. During
    a test the registries are deliberately empty, so a test that needs a
    built-in registration (``quality`` / ``cost``) must register it explicitly.
    """
    monkeypatch.setattr(sched_mod, "_generators", {})
    monkeypatch.setattr(sched_mod, "_formatters", {})
    monkeypatch.setattr(sched_mod, "_deliverers", {})
