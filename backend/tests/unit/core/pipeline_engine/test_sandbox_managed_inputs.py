"""Tests for FAR-802 managed workspace input save-time validation.

Covers:
  - Jinja-stripped literal ``git clone`` detection (including ``{{ var }}``
    and ``{% if %}`` cases that must NOT false-positive)
  - ``dest`` adversarial paths: traversal, empty, ``"."``, ``"/home/user"``,
    ``".git"``, denylisted components, excessive depth
  - Unsupported URL schemes
  - Invalid ``ref.kind`` values
  - Capability constant existence and value
"""

from __future__ import annotations

import pytest

from modulo.core.pipeline_engine.sandbox_mode import (
    SANDBOX_CAPABILITY_WORKSPACE_INPUTS,
    _validate_sandbox_managed_inputs_config,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _base_node(**overrides: object) -> dict[str, object]:
    """Minimal sandbox_agent node definition with one managed input."""
    node: dict[str, object] = {
        "id": "test-node",
        "node_type": "sandbox_agent",
        "workspace_inputs": [
            {
                "dest": "/home/user/repo",
                "url": "https://github.com/org/repo.git",
                "ref": {"kind": "branch"},
            }
        ],
    }
    node.update(overrides)
    return node


def _input_with(**overrides: object) -> dict[str, object]:
    """Single managed input dict with overrides."""
    inp: dict[str, object] = {
        "dest": "/home/user/repo",
        "url": "https://github.com/org/repo.git",
        "ref": {"kind": "branch"},
    }
    inp.update(overrides)
    return inp


# ---------------------------------------------------------------------------
# Capability constant
# ---------------------------------------------------------------------------


class TestCapabilityConstant:
    def test_value(self) -> None:
        assert SANDBOX_CAPABILITY_WORKSPACE_INPUTS == "sandbox.workspace_inputs"

    def test_is_string(self) -> None:
        assert isinstance(SANDBOX_CAPABILITY_WORKSPACE_INPUTS, str)


# ---------------------------------------------------------------------------
# No-op cases (valid config passes silently)
# ---------------------------------------------------------------------------


class TestValidConfigs:
    def test_no_workspace_inputs(self) -> None:
        """Absent workspace_inputs is valid."""
        assert _validate_sandbox_managed_inputs_config({"id": "n"}) is None

    def test_empty_workspace_inputs(self) -> None:
        """Empty list is valid."""
        assert _validate_sandbox_managed_inputs_config({"id": "n", "workspace_inputs": []}) is None

    def test_valid_https_input(self) -> None:
        assert _validate_sandbox_managed_inputs_config(_base_node()) is None

    def test_valid_ssh_input(self) -> None:
        node = _base_node(workspace_inputs=[_input_with(url="ssh://git@github.com/org/repo.git")])
        assert _validate_sandbox_managed_inputs_config(node) is None

    def test_valid_git_at_input(self) -> None:
        node = _base_node(workspace_inputs=[_input_with(url="git@github.com:org/repo.git")])
        assert _validate_sandbox_managed_inputs_config(node) is None

    def test_relative_dest_resolves_under_home(self) -> None:
        """Relative dest is resolved under /home/user/."""
        node = _base_node(workspace_inputs=[_input_with(dest="projects/my-repo")])
        assert _validate_sandbox_managed_inputs_config(node) is None

    def test_no_ref_defaults_ok(self) -> None:
        """Absent ref is fine (defaults to HEAD/main at runtime)."""
        node = _base_node(workspace_inputs=[_input_with(ref=None)])
        assert _validate_sandbox_managed_inputs_config(node) is None

    def test_no_url_ok(self) -> None:
        """Absent url is fine (may reference a connector instead, deferred)."""
        node = _base_node(workspace_inputs=[_input_with(url=None)])
        assert _validate_sandbox_managed_inputs_config(node) is None


# ---------------------------------------------------------------------------
# (a) Literal git clone detection (Jinja-stripped)
# ---------------------------------------------------------------------------


class TestGitCloneDetection:
    def test_literal_git_clone_rejected(self) -> None:
        node = _base_node(agent_commands=["git clone https://example.com/repo.git && do_stuff"])
        with pytest.raises(ValueError, match="literal 'git clone'"):
            _validate_sandbox_managed_inputs_config(node)

    def test_git_clone_in_jinja_block_not_rejected(self) -> None:
        """{{ git_clone }} is interpolation — must NOT trigger."""
        node = _base_node(agent_commands=["{{ git_clone }} && do_stuff"])
        assert _validate_sandbox_managed_inputs_config(node) is None

    def test_git_clone_after_jinja_block_rejected(self) -> None:
        """Static git clone AFTER a jinja block is still a literal."""
        node = _base_node(agent_commands=["echo {{ greeting }} && git clone https://x.com/r.git"])
        with pytest.raises(ValueError, match="literal 'git clone'"):
            _validate_sandbox_managed_inputs_config(node)

    def test_git_clone_in_if_block_rejected(self) -> None:
        """{% if clone %}git clone foo{% endif %} — after stripping Jinja
        control blocks, ``git clone foo`` remains as a literal. Reject it."""
        node = _base_node(agent_commands=["{% if clone %}git clone foo{% endif %}"])
        with pytest.raises(ValueError, match="literal 'git clone'"):
            _validate_sandbox_managed_inputs_config(node)

    def test_clone_without_git_prefix_not_rejected(self) -> None:
        """Just 'clone' without 'git' prefix is fine."""
        node = _base_node(agent_commands=["clone https://example.com/r.git"])
        assert _validate_sandbox_managed_inputs_config(node) is None

    def test_git_clone_in_multiline_command(self) -> None:
        node = _base_node(agent_commands=["#!/bin/bash\necho setup\ngit clone https://x.com/r.git"])
        with pytest.raises(ValueError, match="literal 'git clone'"):
            _validate_sandbox_managed_inputs_config(node)

    def test_no_agent_commands_passes(self) -> None:
        """No agent_commands means no git clone check needed."""
        node = _base_node()
        node.pop("agent_commands", None)
        assert _validate_sandbox_managed_inputs_config(node) is None


# ---------------------------------------------------------------------------
# (b) Dest adversarial cases
# ---------------------------------------------------------------------------


class TestDestValidation:
    def test_empty_dest_rejected(self) -> None:
        node = _base_node(workspace_inputs=[_input_with(dest="")])
        with pytest.raises(ValueError, match=r"'dest'.*non-empty"):
            _validate_sandbox_managed_inputs_config(node)

    def test_whitespace_dest_rejected(self) -> None:
        node = _base_node(workspace_inputs=[_input_with(dest="   ")])
        with pytest.raises(ValueError, match=r"'dest'.*non-empty"):
            _validate_sandbox_managed_inputs_config(node)

    def test_dot_dest_rejected(self) -> None:
        node = _base_node(workspace_inputs=[_input_with(dest=".")])
        with pytest.raises(ValueError, match=r"'dest'.*not a valid target"):
            _validate_sandbox_managed_inputs_config(node)

    def test_home_user_dest_rejected(self) -> None:
        node = _base_node(workspace_inputs=[_input_with(dest="/home/user")])
        with pytest.raises(ValueError, match=r"'dest'.*not a valid target"):
            _validate_sandbox_managed_inputs_config(node)

    def test_traversal_to_tmp_rejected(self) -> None:
        node = _base_node(workspace_inputs=[_input_with(dest="/tmp/evil")])
        with pytest.raises(ValueError, match="resolves outside /home/user/"):
            _validate_sandbox_managed_inputs_config(node)

    def test_traversal_dotdot_rejected(self) -> None:
        node = _base_node(workspace_inputs=[_input_with(dest="/home/user/../../etc/passwd")])
        with pytest.raises(ValueError, match="resolves outside /home/user/"):
            _validate_sandbox_managed_inputs_config(node)

    def test_dot_git_denied(self) -> None:
        node = _base_node(workspace_inputs=[_input_with(dest="/home/user/.git")])
        with pytest.raises(ValueError, match="denied path component"):
            _validate_sandbox_managed_inputs_config(node)

    def test_dot_git_as_subdir_denied(self) -> None:
        node = _base_node(workspace_inputs=[_input_with(dest="/home/user/repo/.git")])
        with pytest.raises(ValueError, match="denied path component"):
            _validate_sandbox_managed_inputs_config(node)

    def test_agent_log_denied(self) -> None:
        node = _base_node(workspace_inputs=[_input_with(dest="/home/user/agent.log")])
        with pytest.raises(ValueError, match="denied path component"):
            _validate_sandbox_managed_inputs_config(node)

    def test_output_json_denied(self) -> None:
        node = _base_node(workspace_inputs=[_input_with(dest="/home/user/output.json")])
        with pytest.raises(ValueError, match="denied path component"):
            _validate_sandbox_managed_inputs_config(node)

    def test_gitconfig_denied(self) -> None:
        node = _base_node(workspace_inputs=[_input_with(dest="/home/user/.gitconfig")])
        with pytest.raises(ValueError, match="denied path component"):
            _validate_sandbox_managed_inputs_config(node)

    def test_git_policy_denied(self) -> None:
        node = _base_node(workspace_inputs=[_input_with(dest="/home/user/.git-policy/")])
        with pytest.raises(ValueError, match="denied path component"):
            _validate_sandbox_managed_inputs_config(node)

    def test_ssh_denied(self) -> None:
        node = _base_node(workspace_inputs=[_input_with(dest="/home/user/.ssh/")])
        with pytest.raises(ValueError, match="denied path component"):
            _validate_sandbox_managed_inputs_config(node)

    def test_excessive_depth_rejected(self) -> None:
        deep = "/home/user/" + "/".join(f"d{i}" for i in range(10))
        node = _base_node(workspace_inputs=[_input_with(dest=deep)])
        with pytest.raises(ValueError, match="levels deep"):
            _validate_sandbox_managed_inputs_config(node)

    def test_relative_dest_resolves(self) -> None:
        """Relative dest should resolve under /home/user/ and pass."""
        node = _base_node(workspace_inputs=[_input_with(dest="workspace/src")])
        assert _validate_sandbox_managed_inputs_config(node) is None

    def test_non_string_dest_rejected(self) -> None:
        node = _base_node(workspace_inputs=[_input_with(dest=123)])
        with pytest.raises(ValueError, match=r"'dest'.*non-empty"):
            _validate_sandbox_managed_inputs_config(node)


# ---------------------------------------------------------------------------
# Ref validation
# ---------------------------------------------------------------------------


class TestRefValidation:
    def test_invalid_ref_kind_rejected(self) -> None:
        node = _base_node(workspace_inputs=[_input_with(ref={"kind": "commit"})])
        with pytest.raises(ValueError, match=r"'ref\.kind'.*not valid"):
            _validate_sandbox_managed_inputs_config(node)

    def test_empty_ref_kind_rejected(self) -> None:
        node = _base_node(workspace_inputs=[_input_with(ref={"kind": ""})])
        with pytest.raises(ValueError, match=r"'ref\.kind'.*not valid"):
            _validate_sandbox_managed_inputs_config(node)

    def test_ref_not_dict_rejected(self) -> None:
        node = _base_node(workspace_inputs=[_input_with(ref="main")])
        with pytest.raises(ValueError, match=r"'ref'.*must be an object"):
            _validate_sandbox_managed_inputs_config(node)

    def test_valid_branch_ref(self) -> None:
        node = _base_node(workspace_inputs=[_input_with(ref={"kind": "branch"})])
        assert _validate_sandbox_managed_inputs_config(node) is None

    def test_valid_tag_ref(self) -> None:
        node = _base_node(workspace_inputs=[_input_with(ref={"kind": "tag"})])
        assert _validate_sandbox_managed_inputs_config(node) is None

    def test_valid_sha_ref(self) -> None:
        node = _base_node(workspace_inputs=[_input_with(ref={"kind": "sha"})])
        assert _validate_sandbox_managed_inputs_config(node) is None


# ---------------------------------------------------------------------------
# URL validation
# ---------------------------------------------------------------------------


class TestUrlValidation:
    def test_unsupported_http_scheme_rejected(self) -> None:
        node = _base_node(workspace_inputs=[_input_with(url="http://github.com/org/repo.git")])
        with pytest.raises(ValueError, match="unsupported scheme"):
            _validate_sandbox_managed_inputs_config(node)

    def test_ftp_scheme_rejected(self) -> None:
        node = _base_node(workspace_inputs=[_input_with(url="ftp://example.com/repo.git")])
        with pytest.raises(ValueError, match="unsupported scheme"):
            _validate_sandbox_managed_inputs_config(node)

    def test_file_scheme_rejected(self) -> None:
        node = _base_node(workspace_inputs=[_input_with(url="file:///tmp/repo.git")])
        with pytest.raises(ValueError, match="unsupported scheme"):
            _validate_sandbox_managed_inputs_config(node)

    def test_empty_url_rejected(self) -> None:
        node = _base_node(workspace_inputs=[_input_with(url="")])
        with pytest.raises(ValueError, match=r"'url'.*non-empty"):
            _validate_sandbox_managed_inputs_config(node)

    def test_whitespace_url_rejected(self) -> None:
        node = _base_node(workspace_inputs=[_input_with(url="   ")])
        with pytest.raises(ValueError, match=r"'url'.*non-empty"):
            _validate_sandbox_managed_inputs_config(node)

    def test_non_string_url_rejected(self) -> None:
        node = _base_node(workspace_inputs=[_input_with(url=42)])
        with pytest.raises(ValueError, match=r"'url'.*non-empty"):
            _validate_sandbox_managed_inputs_config(node)


# ---------------------------------------------------------------------------
# Input item validation
# ---------------------------------------------------------------------------


class TestInputItemValidation:
    def test_non_dict_input_rejected(self) -> None:
        node = _base_node(workspace_inputs=["not-a-dict"])  # type: ignore[list-item]
        with pytest.raises(ValueError, match="must be an object"):
            _validate_sandbox_managed_inputs_config(node)


# ---------------------------------------------------------------------------
# Error code constants (regression: codes exist in the registry)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Security regression: Jinja-split git clone bypass (PR #425 review MINOR)
# ---------------------------------------------------------------------------


class TestJinjaSplitCloneBypass:
    def test_literal_git_clone_in_jinja_block_rejected(self) -> None:
        """``{{ 'git clone' }}`` renders to a clone at runtime — must be rejected."""
        node = _base_node(agent_commands=["{{ 'git clone' }} https://x.com/r.git"])
        with pytest.raises(ValueError, match="literal 'git clone'"):
            _validate_sandbox_managed_inputs_config(node)

    def test_git_clone_var_in_jinja_block_rejected(self) -> None:
        """``git {{ clone_cmd }}`` (variable resolves to clone) must be rejected."""
        node = _base_node(agent_commands=["git {{ clone_cmd }}"])
        with pytest.raises(ValueError, match="literal 'git clone'"):
            _validate_sandbox_managed_inputs_config(node)

    def test_git_jinja_var_clone_rejected(self) -> None:
        """``git {{ x }} clone`` (empties to a clone) must be rejected."""
        node = _base_node(agent_commands=["git {{ x }} clone https://x.com/r.git"])
        with pytest.raises(ValueError, match="literal 'git clone'"):
            _validate_sandbox_managed_inputs_config(node)


class TestErrorCodeConstants:
    """Verify the new error codes are registered in the error-code registry."""

    def test_all_five_codes_registered(self) -> None:
        from modulo.core.pipeline_engine.error_codes import ERROR_CODE_REGISTRY

        expected = {
            "sandbox.input_credential_failed",
            "sandbox.input_checkout_failed",
            "sandbox.input_host_mismatch",
            "sandbox.input_resolution_failed",
            "sandbox.workspace_inputs_disabled",
        }
        missing = expected - set(ERROR_CODE_REGISTRY)
        assert not missing, f"Missing error codes: {missing}"

    def test_codes_are_retryable_appropriately(self) -> None:
        from modulo.core.pipeline_engine.error_codes import ERROR_CODE_REGISTRY

        # Credential and checkout failures are retryable (transient).
        for code in (
            "sandbox.input_credential_failed",
            "sandbox.input_checkout_failed",
            "sandbox.input_resolution_failed",
        ):
            assert ERROR_CODE_REGISTRY[code].retryable is True, code

        # Host mismatch and disabled are not retryable (config/permanent).
        for code in ("sandbox.input_host_mismatch", "sandbox.workspace_inputs_disabled"):
            assert ERROR_CODE_REGISTRY[code].retryable is False, code
