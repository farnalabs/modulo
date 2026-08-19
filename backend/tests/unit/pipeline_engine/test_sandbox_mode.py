"""Unit tests for the sandbox capability derivation (FAR-212 PR A).

Covers :func:`derive_sandbox_capabilities` — the MECHANICAL derivation of the
sandbox write/egress/git-credential capability profile from a ``sandbox_agent``
node's ACTUAL config, so a conformance hard-block can certify writes/egress are
impossible rather than merely un-declared.

The polarity here is RAW (True = present / risked): ``sandbox.egress`` is False
for ``deny_all``/``selected``, ``sandbox.write_files`` is False when the node
declares a read-only workspace, ``sandbox.git_credentials`` is True when scoped
and False when unscoped/absent. Unknown surfaces derive to None. The conformance
polarity inversion (False = the certified guarantee) is covered by the
conformance wiring tests in ``test_guardrail_conformance_midrun.py``.
"""

from __future__ import annotations

from typing import Any

import pytest

from modulo.core.pipeline_engine.sandbox_mode import (
    SANDBOX_CAPABILITY_EGRESS,
    SANDBOX_CAPABILITY_GIT_CREDENTIALS,
    SANDBOX_CAPABILITY_WRITE_FILES,
    derive_sandbox_capabilities,
)


def _sandbox_node(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "n1",
        "node_type": "sandbox_agent",
        "agent_command": "opencode run",
        "agent_prompt": "do the thing",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# sandbox.egress
# ---------------------------------------------------------------------------


def test_egress_deny_all_is_false():
    caps = derive_sandbox_capabilities(_sandbox_node(egress_policy="deny_all"))
    assert caps[SANDBOX_CAPABILITY_EGRESS] is False


def test_egress_selected_is_false():
    caps = derive_sandbox_capabilities(
        _sandbox_node(egress_policy="selected", egress_allowlist=[{"host": "api.github.com", "port": 443}])
    )
    assert caps[SANDBOX_CAPABILITY_EGRESS] is False


def test_egress_default_is_true():
    caps = derive_sandbox_capabilities(_sandbox_node(egress_policy="default"))
    assert caps[SANDBOX_CAPABILITY_EGRESS] is True


def test_egress_absent_is_true():
    # The sandbox default allows internet (node_runner maps None -> allow_internet_access=True).
    caps = derive_sandbox_capabilities(_sandbox_node())
    assert caps[SANDBOX_CAPABILITY_EGRESS] is True


def test_egress_unrecognised_is_unknown():
    caps = derive_sandbox_capabilities(_sandbox_node(egress_policy="allow_all"))
    assert caps[SANDBOX_CAPABILITY_EGRESS] is None


# ---------------------------------------------------------------------------
# sandbox.write_files
# ---------------------------------------------------------------------------


def test_write_files_read_only_is_false():
    caps = derive_sandbox_capabilities(_sandbox_node(read_only=True))
    assert caps[SANDBOX_CAPABILITY_WRITE_FILES] is False


def test_write_files_writable_is_true():
    caps = derive_sandbox_capabilities(_sandbox_node(read_only=False))
    assert caps[SANDBOX_CAPABILITY_WRITE_FILES] is True


def test_write_files_undeclared_is_true():
    # An undeclared workspace surface means the workspace is writable (the
    # sandbox default) — the mechanical fact, not a guess.
    caps = derive_sandbox_capabilities(_sandbox_node())
    assert caps[SANDBOX_CAPABILITY_WRITE_FILES] is True


def test_write_files_non_bool_is_unknown():
    caps = derive_sandbox_capabilities(_sandbox_node(read_only="yes"))
    assert caps[SANDBOX_CAPABILITY_WRITE_FILES] is None


# ---------------------------------------------------------------------------
# sandbox.git_credentials
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "declared",
    ["scoped", "scoped_org", "limited", "true", "on", " SCOPEd "],
)
def test_git_credentials_scoped_is_true(declared: str):
    caps = derive_sandbox_capabilities(_sandbox_node(git_credentials=declared))
    assert caps[SANDBOX_CAPABILITY_GIT_CREDENTIALS] is True


@pytest.mark.parametrize(
    "declared",
    ["unscoped", "none", "absent", "false", "off"],
)
def test_git_credentials_unscoped_is_false(declared: str):
    caps = derive_sandbox_capabilities(_sandbox_node(git_credentials=declared))
    assert caps[SANDBOX_CAPABILITY_GIT_CREDENTIALS] is False


def test_git_credentials_bool_scoped_is_true():
    caps = derive_sandbox_capabilities(_sandbox_node(git_credentials=True))
    assert caps[SANDBOX_CAPABILITY_GIT_CREDENTIALS] is True


def test_git_credentials_bool_unscoped_is_false():
    caps = derive_sandbox_capabilities(_sandbox_node(git_credentials=False))
    assert caps[SANDBOX_CAPABILITY_GIT_CREDENTIALS] is False


def test_git_credentials_undeclared_is_unknown():
    # An undeclared git-credential surface cannot be mechanically confirmed
    # either way -> None (fail-closed for a block guardrail).
    caps = derive_sandbox_capabilities(_sandbox_node())
    assert caps[SANDBOX_CAPABILITY_GIT_CREDENTIALS] is None


def test_git_credentials_unrecognised_is_unknown():
    caps = derive_sandbox_capabilities(_sandbox_node(git_credentials="weird"))
    assert caps[SANDBOX_CAPABILITY_GIT_CREDENTIALS] is None


# ---------------------------------------------------------------------------
# Combined / non-sandbox
# ---------------------------------------------------------------------------


def test_deny_all_read_only_combined_profile():
    caps = derive_sandbox_capabilities(_sandbox_node(egress_policy="deny_all", read_only=True))
    assert caps[SANDBOX_CAPABILITY_EGRESS] is False
    assert caps[SANDBOX_CAPABILITY_WRITE_FILES] is False


def test_non_sandbox_node_type_contributes_empty_profile():
    caps = derive_sandbox_capabilities({"id": "n1", "node_type": "agent"})
    assert caps == {}


def test_missing_node_type_derives_sandbox_surface():
    # Legacy snapshots may omit node_type; the derivation reads the sandbox
    # config surface directly (the conformance wiring only invokes it for
    # sandbox_agent nodes, but a missing type must not crash).
    caps = derive_sandbox_capabilities({"id": "n1", "egress_policy": "deny_all"})
    assert caps[SANDBOX_CAPABILITY_EGRESS] is False
