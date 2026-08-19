"""Unit tests for the sandbox capability derivation (FAR-212 PR A).

Covers :func:`derive_sandbox_capabilities` — the MECHANICAL derivation of the
sandbox egress capability profile from a ``sandbox_agent`` node's ACTUAL
enforced config, so a conformance hard-block can certify egress is impossible
rather than merely un-declared.

The polarity here is RAW (True = present / risked): ``sandbox.egress`` is False
for ``deny_all``/``selected``. ``sandbox.write_files`` and
``sandbox.git_credentials`` are ALWAYS None (unknown): the read-only-workspace
and git-credential-scope surfaces do not exist as validated, enforced product
config (``PipelineGraphNode`` has no such fields and node_runner/e2b never read
or enforce them), so any derived value would certify an unenforced
deny-guarantee (fail-open through the unvalidated workflow-import path). They
stay unknown — fail-closed for a block guardrail — until the enforcement
surface lands (FAR-212 PR B). The conformance polarity inversion (False = the
certified guarantee) is covered by the conformance wiring tests in
``test_guardrail_conformance_midrun.py``.
"""

from __future__ import annotations

from typing import Any

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


def test_write_files_always_unknown_read_only_smuggled():
    """write_files is ALWAYS None (unknown) — even when a ``read_only`` key is
    smuggled into the node dict. The key is not a real, validated, enforced
    surface inside and a derivation value would certify an unenforced
    deny-guarantee (fail-open via the raw workflow-import path)."""
    caps = derive_sandbox_capabilities(_sandbox_node(read_only=True))
    assert caps[SANDBOX_CAPABILITY_WRITE_FILES] is None


def test_write_files_always_unknown_writable_declared():
    caps = derive_sandbox_capabilities(_sandbox_node(read_only=False))
    assert caps[SANDBOX_CAPABILITY_WRITE_FILES] is None


def test_write_files_always_unknown_undeclared():
    # No read-only surface exists anywhere in the product (PipelineGraphNode
    # has no such field) — the capability stays unknown so a block guardrail
    # fails CLOSED rather than assuming the sandbox default is writable.
    caps = derive_sandbox_capabilities(_sandbox_node())
    assert caps[SANDBOX_CAPABILITY_WRITE_FILES] is None


def test_write_files_always_unknown_non_bool():
    caps = derive_sandbox_capabilities(_sandbox_node(read_only="yes"))
    assert caps[SANDBOX_CAPABILITY_WRITE_FILES] is None


# ---------------------------------------------------------------------------
# sandbox.git_credentials
# ---------------------------------------------------------------------------


def test_git_credentials_always_unknown_declared_scoped():
    """git_credentials is ALWAYS None (unknown) — even when a ``git_credentials``
    key is smuggled into the node dict. No git-credential scoping exists as a
    validated, enforced product surface; deriving ``True`` would certify scoped
    credentials nothing limits (fail-open)."""
    caps = derive_sandbox_capabilities(_sandbox_node(git_credentials="scoped"))
    assert caps[SANDBOX_CAPABILITY_GIT_CREDENTIALS] is None


def test_git_credentials_always_unknown_declared_unscoped():
    caps = derive_sandbox_capabilities(_sandbox_node(git_credentials="unscoped"))
    assert caps[SANDBOX_CAPABILITY_GIT_CREDENTIALS] is None


def test_git_credentials_always_unknown_bool():
    caps = derive_sandbox_capabilities(_sandbox_node(git_credentials=True))
    assert caps[SANDBOX_CAPABILITY_GIT_CREDENTIALS] is None


def test_git_credentials_always_unknown_undeclared():
    caps = derive_sandbox_capabilities(_sandbox_node())
    assert caps[SANDBOX_CAPABILITY_GIT_CREDENTIALS] is None


def test_git_credentials_always_unknown_unrecognised():
    caps = derive_sandbox_capabilities(_sandbox_node(git_credentials="weird"))
    assert caps[SANDBOX_CAPABILITY_GIT_CREDENTIALS] is None


# ---------------------------------------------------------------------------
# Combined / non-sandbox
# ---------------------------------------------------------------------------


def test_deny_all_profile_egress_false_write_unknown():
    caps = derive_sandbox_capabilities(_sandbox_node(egress_policy="deny_all", read_only=True))
    assert caps[SANDBOX_CAPABILITY_EGRESS] is False
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


# ---------------------------------------------------------------------------
# Round-trip through a real API-validated node (FAR-212 PR A review)
# ---------------------------------------------------------------------------


def test_api_validated_node_round_trip_write_and_git_unknown():
    """A REAL API-validated node (via PipelineGraphNode) can never carry the
    phantom ``read_only`` / ``git_credentials`` keys — Pydantic's default
    ``extra="ignore"`` silently drops them, exactly as the REST/MCP paths
    behave — so the derivation resolves ``sandbox.write_files`` and
    ``sandbox.git_credentials`` to unknown (fail-closed block) until the
    enforcement surface lands. This proves the dead-end the old synthetic
    node_defs masked: no real pipeline can produce a certified value today.
    """
    from modulo.api.routes.pipelines import PipelineGraphNode

    node = PipelineGraphNode.model_validate(
        {
            "id": "11111111-1111-1111-1111-111111111111",
            "node_type": "sandbox_agent",
            "position": {"x": 10, "y": 20},
            "agent_prompt": "Do the thing",
            "agent_command": "opencode run --auto < /home/user/prompt.md",
            "template_id": "opencode",
            "egress_policy": "deny_all",
            "read_only": True,
            "git_credentials": "scoped",
        }
    )
    round_tripped = node.model_dump()

    assert round_tripped.get("read_only") is None
    assert round_tripped.get("git_credentials") is None
    assert round_tripped["egress_policy"] == "deny_all"

    caps = derive_sandbox_capabilities(round_tripped)
    assert caps[SANDBOX_CAPABILITY_EGRESS] is False
    assert caps[SANDBOX_CAPABILITY_WRITE_FILES] is None
    assert caps[SANDBOX_CAPABILITY_GIT_CREDENTIALS] is None
