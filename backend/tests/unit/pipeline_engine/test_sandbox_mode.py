"""Unit tests for the sandbox capability derivation (FAR-212 PR A).

Covers :func:`derive_sandbox_capabilities` — the MECHANICAL derivation of the
sandbox write/egress/git-credential capability profile from a ``sandbox_agent``
node's ACTUAL config, so a conformance hard-block can certify writes/egress are
impossible rather than merely un-declared.

The polarity here is RAW (True = present / risked): ``sandbox.egress`` is False
for ``deny_all``/``selected``, True for ``default``/absent, None when
unrecognised. ``sandbox.write_files`` and ``sandbox.git_credentials`` ALWAYS
derive None (unknown): the read-only / git-credential scope surfaces are NOT
enforced until PR B (read-only mounts, git-credential scope), so a block
guardrail on them fails CLOSED and the derivation never certifies from
unvalidated/unenforced node keys (``read_only`` / ``git_credentials`` are not
``PipelineGraphNode`` fields). The conformance polarity inversion (False = the
certified guarantee) is covered by the conformance wiring tests in
``test_guardrail_conformance_midrun.py``.
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

# NOTE (FAR-212 PR A review): ``sandbox.write_files`` always derives None
# (unknown). The ``read_only`` key is NOT a PipelineGraphNode field (Pydantic
# ``extra="ignore"`` silently drops it on the REST/MCP paths) and node_runner /
# e2b never enforce a read-only filesystem, so the derivation must never
# certify "writes are impossible" from a node config nothing enforces. It
# resolves unknown (fail-closed block) until the PR B read-only mount surface
# lands together with the PipelineGraphNode / GraphValidator / node_runner
# enforcement.


def test_write_files_undeclared_is_unknown():
    caps = derive_sandbox_capabilities(_sandbox_node())
    assert caps[SANDBOX_CAPABILITY_WRITE_FILES] is None


def test_write_files_ignores_read_only_key():
    # A smuggled ``read_only`` (true/false/non-bool) must NOT certify writes —
    # the e2b sandbox is still writable, so the capability stays unknown.
    for declared in (True, False, "yes"):
        caps = derive_sandbox_capabilities(_sandbox_node(read_only=declared))
        assert caps[SANDBOX_CAPABILITY_WRITE_FILES] is None


# ---------------------------------------------------------------------------
# sandbox.git_credentials
# ---------------------------------------------------------------------------

# NOTE (FAR-212 PR A review): ``sandbox.git_credentials`` always derives None
# (unknown). The ``git_credentials`` key is NOT a PipelineGraphNode field and
# node_runner / e2b never scope git credentials, so the derivation must never
# certify "git credentials are scoped" from a declared value nothing enforces.
# It resolves unknown (fail-closed block) until the PR B git-credential scope
# surface lands together with the PipelineGraphNode / GraphValidator /
# node_runner enforcement.


def test_git_credentials_undeclared_is_unknown():
    caps = derive_sandbox_capabilities(_sandbox_node())
    assert caps[SANDBOX_CAPABILITY_GIT_CREDENTIALS] is None


@pytest.mark.parametrize(
    "declared",
    ["scoped", "scoped_org", "limited", "true", "on", " SCOPEd "],
)
def test_git_credentials_scoped_declaration_is_unknown(declared: str):
    # A scoped-git declaration must NOT certify the scoped guarantee — the
    # sandbox still has full git access, so the capability stays unknown.
    caps = derive_sandbox_capabilities(_sandbox_node(git_credentials=declared))
    assert caps[SANDBOX_CAPABILITY_GIT_CREDENTIALS] is None


@pytest.mark.parametrize(
    "declared",
    ["unscoped", "none", "absent", "false", "off"],
)
def test_git_credentials_unscoped_declaration_is_unknown(declared: str):
    caps = derive_sandbox_capabilities(_sandbox_node(git_credentials=declared))
    assert caps[SANDBOX_CAPABILITY_GIT_CREDENTIALS] is None


def test_git_credentials_ignores_bool_declaration():
    for declared in (True, False):
        caps = derive_sandbox_capabilities(_sandbox_node(git_credentials=declared))
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
    # read_only is unenforced -> write_files stays unknown, never certified.
    assert caps[SANDBOX_CAPABILITY_WRITE_FILES] is None


def test_non_sandbox_node_type_contributes_empty_profile():
    caps = derive_sandbox_capabilities({"id": "n1", "node_type": "agent"})
    assert caps == {}


def test_missing_node_type_derives_sandbox_surface():
    # Legacy snapshots may omit node_type; the derivation reads the sandbox
    # config surface directly (the conformance wiring only invokes it for
    # sandbox_agent nodes, but a missing type must not crash).
    caps = derive_sandbox_capabilities({"id": "n1", "egress_policy": "deny_all"})
    assert caps[SANDBOX_CAPABILITY_EGRESS] is False
