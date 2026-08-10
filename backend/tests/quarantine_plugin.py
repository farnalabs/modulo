"""
pytest plugin: flaky-test quarantine mechanism.

Reads .quarantine.yml from the repo root and marks each quarantined test as
xfail with a reason. If any quarantined test has passed its expiry date,
the plugin prints a warning (the gate/publish scripts check this separately).

Usage: registered via ``pytest_plugins = ["tests.quarantine_plugin"]`` in
``backend/tests/conftest.py`` (this package is on sys.path because
``backend/tests/__init__.py`` exists). The ``quarantine_file`` ini option may
override the default path; pytest does NOT support a ``plugins`` ini option,
so do not configure it there.
"""

from datetime import date
from pathlib import Path

import pytest
import yaml

QUARANTINE_PATH = Path(__file__).resolve().parents[2] / ".quarantine.yml"


def pytest_addoption(parser):
    parser.addini(
        "quarantine_file",
        "Path to quarantine YAML file",
        default=QUARANTINE_PATH,
    )


def pytest_collection_modifyitems(config, items):
    qfile = Path(config.getini("quarantine_file"))
    if not qfile.exists():
        return

    with qfile.open() as f:
        data = yaml.safe_load(f)

    if not data or "quarantine" not in data:
        return

    entries = data["quarantine"] or []
    today = date.today()
    quarantined = set()
    expired = []

    for entry in entries:
        test_id = entry.get("test_id", "")
        reason = entry.get("reason", "Quarantined (flaky test)")
        expiry_str = entry.get("expiry", "")
        quarantined.add(test_id)

        if expiry_str:
            try:
                expiry = date.fromisoformat(expiry_str)
                if expiry < today:
                    expired.append((test_id, expiry_str, reason))
            except ValueError:
                pass

    for item in items:
        if item.nodeid in quarantined:
            quarantine_entry = next((e for e in entries if e.get("test_id") == item.nodeid), {})
            reason = quarantine_entry.get("reason", "Quarantined (flaky test)")
            item.add_marker(pytest.mark.xfail(reason=reason, strict=False))

    if expired:
        import warnings

        for test_id, expiry_str, reason in expired:
            warnings.warn(
                f"QUARANTINE EXPIRED: {test_id} expired {expiry_str} — {reason}. "
                f"Fix the test or extend the quarantine expiry.",
                stacklevel=2,
            )
