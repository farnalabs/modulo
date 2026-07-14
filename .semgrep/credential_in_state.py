from typing import Any


def unsafe_state_writes(state: dict[str, Any], checkpoint_state: dict[str, Any]) -> None:
    # ruleid: credential-not-in-state
    state["api_key"] = "decrypted"
    # ruleid: credential-not-in-state
    checkpoint_state["private_key"] = "decrypted"


def safe_non_state_metadata(node: dict[str, Any], file_checksums: dict[str, Any]) -> None:
    # ok: credential-not-in-state
    node["token_budget"] = 1000
    # ok: credential-not-in-state
    file_checksums["credentials_references.json"] = "sha256"
