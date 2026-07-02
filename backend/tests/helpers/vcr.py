"""VCR recording helpers for connector integration tests.

Usage::

    @pytest.mark.vcr
    async def test_github_query_repos(connector):
        result = await connector.query(...)

Run with ``--record-mode=once`` against a real API to record cassettes.
CI runs with ``--record-mode=none`` (replay only).
"""

import os
from pathlib import Path

VCR_CASSETTE_DIR = Path(__file__).parent.parent / "cassettes"


def vcr_config(**overrides):
    """Return a VCR config dict for connector tests."""
    config = {
        "cassette_library_dir": str(VCR_CASSETTE_DIR),
        "record_mode": os.environ.get("VCR_RECORD_MODE", "none"),
        "filter_headers": [("Authorization", "Bearer <TOKEN>")],
        "filter_query_parameters": [("api_key", "<API_KEY>")],
        "decode_compressed_response": True,
    }
    config.update(overrides)
    return config
