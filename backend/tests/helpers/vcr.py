"""VCR recording helpers for connector integration tests.

Usage::

    @pytest.mark.vcr
    async def test_github_query_repos(connector):
        result = await connector.query(...)

CI runs with VCR_RECORD_MODE=none (replay only).
Run with VCR_RECORD_MODE=once against a real API to record cassettes.
"""

import os
import warnings
from pathlib import Path
from typing import Any

_VALID_RECORD_MODES = {"once", "new_episodes", "none", "all"}

VCR_CASSETTE_DIR = Path(__file__).parent.parent / "cassettes"


def vcr_config(**overrides: Any) -> dict[str, Any]:
    """Return a VCR config dict for connector tests.

    Overrides for list-valued keys (``filter_headers``,
    ``filter_query_parameters``, ``match_on``) are **extended**
    onto the defaults, not replaced.
    """
    record_mode = os.environ.get("VCR_RECORD_MODE", "none")
    if record_mode not in _VALID_RECORD_MODES:
        raise ValueError(
            f"Invalid VCR_RECORD_MODE={record_mode!r}. Valid modes: {', '.join(sorted(_VALID_RECORD_MODES))}",
        )

    config: dict[str, Any] = {
        "cassette_library_dir": str(VCR_CASSETTE_DIR),
        "record_mode": record_mode,
        "filter_headers": [("Authorization", "Bearer <TOKEN>")],
        "filter_query_parameters": [("api_key", "<API_KEY>")],
        "match_on": ["method", "path", "query"],
        "decode_compressed_response": True,
    }

    if not VCR_CASSETTE_DIR.exists():
        warnings.warn(
            f"VCR cassette directory does not exist: {VCR_CASSETTE_DIR}. "
            "Create it or run with VCR_RECORD_MODE=once to record cassettes.",
            stacklevel=2,
        )

    for key, value in overrides.items():
        if key == "record_mode" and value not in _VALID_RECORD_MODES:
            raise ValueError(
                f"Invalid record_mode={value!r}. Valid modes: {', '.join(sorted(_VALID_RECORD_MODES))}",
            )
        if isinstance(value, list) and isinstance(config.get(key), list):
            config[key] = config[key] + value
        else:
            config[key] = value

    return config
