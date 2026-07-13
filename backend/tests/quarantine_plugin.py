"""
pytest plugin: flaky-test quarantine mechanism.

Reads .quarantine.yml from the repo root and marks each quarantined test as
xfail with a reason. If any quarantined test has passed its expiry date,
the plugin prints a warning (the gate/publish scripts check this separately).

Usage: enabled automatically via conftest.py that registers this plugin, or
via pyproject.toml:
    [tool.pytest.ini_options]
    plugins = ["tests.quarantine_plugin"]
"""

import os
from datetime import date

import pytest
import yaml

QUARANTINE_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".quarantine.yml")


def pytest_addoption(parser):
    parser.addini(
        "quarantine_file",
        "Path to quarantine YAML file",
        default=QUARANTINE_PATH,
    )


def pytest_collection_modifyitems(config, items):
    qfile = config.getini("quarantine_file")
    if not os.path.exists(qfile):
        return

    with open(qfile) as f:
        data = yaml.safe_load(f)

    if not data or "quarantine" not in data:
        return

    today = date.today()
    quarantined = set()
    expired = []

    for entry in data["quarantine"]:
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
            quarantine_entry = next((e for e in data["quarantine"] if e.get("test_id") == item.nodeid), {})
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
