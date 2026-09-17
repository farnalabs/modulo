"""Standalone cassette recorder for npm and pypi connector tests.

This module is NOT collected by pytest as part of the normal offline suite.
It must be run as a script with network access to re-record the committed
cassettes in tests/cassettes/.

Usage (from backend/):

    uv run python tests/connectors/_record_cassettes.py

The cassettes are saved in VCR YAML format (version 1) in tests/cassettes/.
The committed cassettes are the replay source for CI — they must never be
hand-crafted.  Re-record when the npm or PyPI API response shape changes or
when the cassettes become stale.

NOTE — VCR's ``cassette_library_dir`` path transformer does NOT append the
``.yaml`` extension; this script does it explicitly.  The test-suite VCR
config (conftest.py ``vcr_config``) resolves cassettes via
``@pytest.mark.vcr`` which *does* add ``.yaml``, so the two paths stay in
sync only when the files on disk carry the extension.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import httpx
import yaml

_log = logging.getLogger(__name__)

CASSETTE_DIR = Path(__file__).resolve().parent.parent / "cassettes"

_RECORDS: list[tuple[str, str, str]] = [
    (
        "test_npm_search_express",
        "https://registry.npmjs.org/-/v1/search?text=express&size=5",
        "npm search",
    ),
    (
        "test_npm_get_package",
        "https://registry.npmjs.org/express",
        "npm package",
    ),
    (
        "test_pypi_get_package",
        "https://pypi.org/pypi/requests/json",
        "pypi package",
    ),
]


def _record_one(cassette_name: str, uri: str, label: str) -> None:
    """Fetch *uri* and write a VCR-format YAML cassette."""
    r = httpx.get(uri, timeout=30)
    r.raise_for_status()
    body = r.json()

    cassette = {
        "interactions": [
            {
                "request": {
                    "body": None,
                    "headers": {"Content-Type": ["application/json"]},
                    "method": "GET",
                    "uri": uri,
                },
                "response": {
                    "body": {"string": json.dumps(body, separators=(",", ":"))},
                    "headers": {"Content-Type": [r.headers.get("content-type", "application/json")]},
                    "status": {"code": r.status_code, "message": "OK"},
                },
            }
        ],
        "version": 1,
    }

    path = CASSETTE_DIR / (cassette_name + ".yaml")
    with path.open("w", encoding="utf-8") as fh:
        yaml.dump(cassette, fh, default_flow_style=False, allow_unicode=True, sort_keys=False)

    # Verify round-trip
    with path.open(encoding="utf-8") as fh:
        verified = yaml.safe_load(fh)
    verified_body = json.loads(verified["interactions"][0]["response"]["body"]["string"])
    assert verified_body == body, "Round-trip verification failed"

    _log.info("  %s: recorded %s (%d top-level keys)", label, path.name, len(body))


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    CASSETTE_DIR.mkdir(parents=True, exist_ok=True)

    for name, uri, label in _RECORDS:
        _record_one(name, uri, label)

    _log.info("\nAll %d cassettes recorded from live registries.", len(_RECORDS))


if __name__ == "__main__":
    main()
