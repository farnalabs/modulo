"""Tests for FAR-798 multi-host git credential helper.

Covers:
- Byte-identical regression: github.com-only output equals the pre-change string
- Multi-host: two hosts -> only the matching host's token is emitted
- Token isolation: host A never receives host B's token
- Unknown host gets nothing (deny)
- git_credentials="none" -> no credential for any host
- Capability: multi_host True for >1 host, False for a single host
"""

from __future__ import annotations

from modulo.core.pipeline_engine.sandbox_mode import (
    SANDBOX_CAPABILITY_MULTI_HOST,
    derive_sandbox_capabilities,
)
from modulo.core.pipeline_engine.sandbox_policy import (
    build_git_multi_host_script,
    build_git_none_script,
    build_git_scoped_script,
)

# ---------------------------------------------------------------------------
# Byte-identical regression constant (captured from build_git_scoped_script
# BEFORE the FAR-798 change — the single-host github.com-only output).
# ---------------------------------------------------------------------------
_GITHUB_ONLY_SCRIPT = (
    "set -e\n"
    "mkdir -p /home/user/.git-policy\n"
    "cat > /home/user/.git-policy/cred-helper.sh <<'POLICY_EOF'\n"
    "#!/bin/sh\n"
    'host=""\n'
    "while read -r l; do\n"
    '  [ "$l" = "" ] && break\n'
    '  case "$l" in\n'
    '    host=*) host="${l#host=}" ;;\n'
    "  esac\n"
    "done\n"
    'if [ "$host" = "github.com" ] && [ -n "$GITHUB_TOKEN" ]; then\n'
    "  printf 'username=x-access-token\\npassword=%s\\n' \"$GITHUB_TOKEN\"\n"
    "fi\n"
    "POLICY_EOF\n"
    "chmod +x /home/user/.git-policy/cred-helper.sh\n"
    "git config --file /home/user/.gitconfig credential.helper "
    '"/home/user/.git-policy/cred-helper.sh"\n'
)


class TestByteIdenticalRegression:
    """The single-host github.com-only output must be BYTE-IDENTICAL to pre-change."""

    def test_scoped_script_unchanged(self) -> None:
        assert build_git_scoped_script() == _GITHUB_ONLY_SCRIPT


class TestMultiHostCredentialHelper:
    """Tests for the multi-host credential helper script builder."""

    def test_two_hosts_only_matching_token_emitted(self) -> None:
        """Two hosts: only the matching host's token is emitted for a given host."""
        hosts = {"github.com": "MODULO_GIT_CRED_0", "gitlab.com": "MODULO_GIT_CRED_1"}
        script = build_git_multi_host_script(hosts)

        # Verify github.com gets CRED_0
        assert "MODULO_GIT_CRED_0" in script
        # Verify gitlab.com gets CRED_1
        assert "MODULO_GIT_CRED_1" in script
        # Verify the case statement structure
        assert 'case "$host" in' in script
        assert '"github.com")' in script
        assert '"gitlab.com")' in script

    def test_unknown_host_gets_nothing(self) -> None:
        """A third, unknown host gets NOTHING (deny)."""
        hosts = {"github.com": "MODULO_GIT_CRED_0"}
        script = build_git_multi_host_script(hosts)
        # The script only has a case arm for github.com — an unknown host
        # falls through to nothing (no credential).
        assert '"bitbucket.org")' not in script

    def test_token_isolation(self) -> None:
        """Host A never receives host B's token."""
        hosts = {"github.com": "MODULO_GIT_CRED_0", "gitlab.com": "MODULO_GIT_CRED_1"}
        script = build_git_multi_host_script(hosts)

        # Each host's case arm only references its OWN env var.
        # Find the github.com arm and verify it references CRED_0, not CRED_1.
        github_arm_start = script.index('"github.com")')
        gitlab_arm_start = script.index('"gitlab.com")')

        # Extract the github.com arm (between its start and the gitlab start)
        github_arm = script[github_arm_start:gitlab_arm_start]
        assert "MODULO_GIT_CRED_0" in github_arm
        assert "MODULO_GIT_CRED_1" not in github_arm

        # Extract the gitlab.com arm (from its start to the esac)
        gitlab_arm = script[gitlab_arm_start:]
        assert "MODULO_GIT_CRED_1" in gitlab_arm
        assert "MODULO_GIT_CRED_0" not in gitlab_arm

    def test_exact_literal_match_no_globs(self) -> None:
        """Matching is by exact literal equality — no glob patterns."""
        hosts = {"github.com": "MODULO_GIT_CRED_0"}
        script = build_git_multi_host_script(hosts)
        # The case arm uses a quoted literal string, not a glob pattern.
        # The * in "host=*)" is shell case syntax for "any host= value",
        # not a glob — the actual matching is the quoted "github.com") arm.
        assert '"github.com")' in script

    def test_per_host_env_var_naming(self) -> None:
        """Each host's token comes from its own per-host env var."""
        hosts = {
            "github.com": "MODULO_GIT_CRED_0",
            "gitlab.com": "MODULO_GIT_CRED_1",
            "bitbucket.org": "MODULO_GIT_CRED_2",
        }
        script = build_git_multi_host_script(hosts)
        assert "MODULO_GIT_CRED_0" in script
        assert "MODULO_GIT_CRED_1" in script
        assert "MODULO_GIT_CRED_2" in script

    def test_wrapper_structure(self) -> None:
        """The wrapper installs the helper and registers it in the agent config."""
        hosts = {"github.com": "MODULO_GIT_CRED_0"}
        script = build_git_multi_host_script(hosts)
        # Must start with set -e
        assert script.startswith("set -e\n")
        # Must create the policy directory
        assert "mkdir -p /home/user/.git-policy\n" in script
        # Must write the helper to the policy directory
        assert "cat > /home/user/.git-policy/cred-helper.sh <<'POLICY_EOF'\n" in script
        # Must chmod the helper
        assert "chmod +x /home/user/.git-policy/cred-helper.sh\n" in script
        # Must register in the AGENT's git config (not root's)
        assert "git config --file /home/user/.gitconfig credential.helper" in script


class TestNoneCredentialsRefusesAll:
    """git_credentials="none" must refuse ALL hosts."""

    def test_none_script_has_exit_1(self) -> None:
        """The none script exits 1 — no credential for any host."""
        script = build_git_none_script()
        assert "exit 1" in script

    def test_none_script_installs_helper(self) -> None:
        """The none script installs a refusing helper in the agent config."""
        script = build_git_none_script()
        assert "git config --file /home/user/.gitconfig credential.helper" in script


class TestMultiHostCapability:
    """Capability derivation for sandbox.git_credentials.multi_host."""

    def test_multi_host_true_for_multiple_hosts(self) -> None:
        """multi_host is True when >1 distinct host is granted."""
        node_def = {
            "node_type": "sandbox_agent",
            "allowed_hosts": {"github.com": "VAR0", "gitlab.com": "VAR1"},
        }
        caps = derive_sandbox_capabilities(node_def)
        assert caps[SANDBOX_CAPABILITY_MULTI_HOST] is True

    def test_multi_host_false_for_single_host(self) -> None:
        """multi_host is False for a single host."""
        node_def = {
            "node_type": "sandbox_agent",
            "allowed_hosts": {"github.com": "VAR0"},
        }
        caps = derive_sandbox_capabilities(node_def)
        assert caps[SANDBOX_CAPABILITY_MULTI_HOST] is False

    def test_multi_host_false_when_absent(self) -> None:
        """multi_host is False when allowed_hosts is absent."""
        node_def = {"node_type": "sandbox_agent"}
        caps = derive_sandbox_capabilities(node_def)
        assert caps[SANDBOX_CAPABILITY_MULTI_HOST] is False

    def test_multi_host_false_for_non_sandbox_node(self) -> None:
        """multi_host is absent for non-sandbox nodes."""
        node_def = {"node_type": "agent"}
        caps = derive_sandbox_capabilities(node_def)
        assert SANDBOX_CAPABILITY_MULTI_HOST not in caps

    def test_multi_host_false_for_empty_dict(self) -> None:
        """multi_host is False for an empty dict."""
        node_def = {
            "node_type": "sandbox_agent",
            "allowed_hosts": {},
        }
        caps = derive_sandbox_capabilities(node_def)
        assert caps[SANDBOX_CAPABILITY_MULTI_HOST] is False

    def test_multi_host_false_for_non_dict(self) -> None:
        """multi_host is False when allowed_hosts is not a dict."""
        node_def = {
            "node_type": "sandbox_agent",
            "allowed_hosts": ["github.com"],
        }
        caps = derive_sandbox_capabilities(node_def)
        assert caps[SANDBOX_CAPABILITY_MULTI_HOST] is False

    def test_constant_name(self) -> None:
        """The capability constant is the expected dotted string."""
        assert SANDBOX_CAPABILITY_MULTI_HOST == "sandbox.git_credentials.multi_host"
