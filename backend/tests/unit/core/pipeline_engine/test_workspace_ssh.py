"""Tests for SSH transport hardening (FAR-799).

Covers:
* Pinned known_hosts generation (single host, multiple hosts, unknown host).
* IP validation + pinning via the SSRF guard (public IP accepted, private IP
  refused, blocked range refused).
* SSH options builder (StrictHostKeyChecking=yes, HostKeyAlias, HostName).
* GIT_SSH_COMMAND builder (options present, HostName matches validated IP).
* Script snippet generation (mkdir, printf, chmod, export).
* Orchestrator: end-to-end build_ssh_transport (validated IP == resolved IP).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from modulo.core.pipeline_engine.workspace_ssh import (
    GITHUB_HOST_KEYS,
    SshHostRefusedError,
    SshTransportConfig,
    build_git_ssh_command,
    build_ssh_command,
    build_ssh_options,
    build_ssh_transport,
    build_ssh_transport_script,
    generate_known_hosts_entry,
    generate_pinned_known_hosts,
    validate_and_pin_ip,
)

# ---------------------------------------------------------------------------
# generate_known_hosts_entry
# ---------------------------------------------------------------------------


class TestGenerateKnownHostsEntry:
    def test_github_entry_contains_all_key_types(self) -> None:
        entry = generate_known_hosts_entry("github.com")
        assert "ssh-rsa" in entry
        assert "ecdsa-sha2-nistp256" in entry
        assert "ssh-ed25519" in entry
        assert entry.startswith("github.com ")

    def test_gitlab_entry_contains_all_key_types(self) -> None:
        entry = generate_known_hosts_entry("gitlab.com")
        assert "ssh-rsa" in entry
        assert "ecdsa-sha2-nistp256" in entry
        assert "ssh-ed25519" in entry
        assert entry.startswith("gitlab.com ")

    def test_unknown_host_raises(self) -> None:
        with pytest.raises(ValueError, match="No published SSH host keys"):
            generate_known_hosts_entry("evil.example.com")

    def test_entry_ends_with_newline(self) -> None:
        entry = generate_known_hosts_entry("github.com")
        assert entry.endswith("\n")

    def test_entry_line_count_matches_keys(self) -> None:
        entry = generate_known_hosts_entry("github.com")
        lines = [line for line in entry.strip().splitlines() if line]
        assert len(lines) == len(GITHUB_HOST_KEYS)


# ---------------------------------------------------------------------------
# generate_pinned_known_hosts
# ---------------------------------------------------------------------------


class TestGeneratePinnedKnownHosts:
    def test_single_host(self) -> None:
        content = generate_pinned_known_hosts("github.com")
        assert "github.com ssh-rsa" in content

    def test_multiple_hosts(self) -> None:
        content = generate_pinned_known_hosts("github.com", "gitlab.com")
        assert "github.com ssh-rsa" in content
        assert "gitlab.com ssh-rsa" in content

    def test_no_hosts_raises(self) -> None:
        with pytest.raises(ValueError, match="At least one hostname"):
            generate_pinned_known_hosts()

    def test_unknown_host_raises(self) -> None:
        with pytest.raises(ValueError, match="No published SSH host keys"):
            generate_pinned_known_hosts("github.com", "evil.example.com")


# ---------------------------------------------------------------------------
# validate_and_pin_ip
# ---------------------------------------------------------------------------


class TestValidateAndPinIp:
    def test_public_ip_accepted(self) -> None:
        """A public IP like 140.82.121.3 (github.com) passes validation."""
        with patch(
            "modulo.core.pipeline_engine.workspace_ssh._resolve_all_sync",
            return_value=["140.82.121.3"],
        ):
            ips = validate_and_pin_ip("github.com")
            assert ips == ("140.82.121.3",)

    def test_private_ip_refused(self) -> None:
        """A private IP (192.168.1.1) is refused — fail closed."""
        with (
            patch(
                "modulo.core.pipeline_engine.workspace_ssh._resolve_all_sync",
                return_value=["192.168.1.1"],
            ),
            pytest.raises(SshHostRefusedError, match="blocked address"),
        ):
            validate_and_pin_ip("internal.example.com")

    def test_loopback_ip_refused(self) -> None:
        """Loopback (127.0.0.1) is refused without an explicit allowlist."""
        with (
            patch(
                "modulo.core.pipeline_engine.workspace_ssh._resolve_all_sync",
                return_value=["127.0.0.1"],
            ),
            pytest.raises(SshHostRefusedError, match="blocked address"),
        ):
            validate_and_pin_ip("localhost")

    def test_link_local_refused(self) -> None:
        """Link-local (169.254.x.x) is ALWAYS refused — non-negotiable floor."""
        with (
            patch(
                "modulo.core.pipeline_engine.workspace_ssh._resolve_all_sync",
                return_value=["169.254.1.1"],
            ),
            pytest.raises(SshHostRefusedError, match="blocked address"),
        ):
            validate_and_pin_ip("metadata.internal")

    def test_empty_resolution_refused(self) -> None:
        """A hostname resolving to no addresses is refused — fail closed."""
        with (
            patch(
                "modulo.core.pipeline_engine.workspace_ssh._resolve_all_sync",
                return_value=[],
            ),
            pytest.raises(SshHostRefusedError, match="resolved to no addresses"),
        ):
            validate_and_pin_ip("nonexistent.example.com")

    def test_dns_failure_refused(self) -> None:
        """A DNS resolution failure is refused — fail closed."""
        with (
            patch(
                "modulo.core.pipeline_engine.workspace_ssh._resolve_all_sync",
                side_effect=ValueError("DNS resolution failed for nonexistent"),
            ),
            pytest.raises(SshHostRefusedError, match="DNS resolution failed"),
        ):
            validate_and_pin_ip("nonexistent.example.com")

    def test_mixed_public_private_all_refused(self) -> None:
        """If ANY resolved address is blocked, the entire resolution is refused."""
        with (
            patch(
                "modulo.core.pipeline_engine.workspace_ssh._resolve_all_sync",
                return_value=["140.82.121.3", "192.168.1.1"],
            ),
            pytest.raises(SshHostRefusedError, match="blocked address"),
        ):
            validate_and_pin_ip("dual.example.com")

    def test_multiple_public_ips_all_accepted(self) -> None:
        """Multiple public IPs all pass validation."""
        with patch(
            "modulo.core.pipeline_engine.workspace_ssh._resolve_all_sync",
            return_value=["140.82.121.3", "140.82.121.4"],
        ):
            ips = validate_and_pin_ip("cdn.example.com")
            assert ips == ("140.82.121.3", "140.82.121.4")


# ---------------------------------------------------------------------------
# build_ssh_options
# ---------------------------------------------------------------------------


class TestBuildSshOptions:
    def test_strict_host_key_checking_yes(self) -> None:
        options = build_ssh_options(
            "140.82.121.3",
            hostname="github.com",
            known_hosts_path="/pinned/known_hosts",
        )
        assert "-o" in options
        assert "StrictHostKeyChecking=yes" in options

    def test_user_known_hosts_file(self) -> None:
        options = build_ssh_options(
            "140.82.121.3",
            hostname="github.com",
            known_hosts_path="/pinned/known_hosts",
        )
        assert "UserKnownHostsFile=/pinned/known_hosts" in options

    def test_host_key_alias_matches_hostname(self) -> None:
        """HostKeyAlias is the ORIGINAL hostname, not the validated IP."""
        options = build_ssh_options(
            "140.82.121.3",
            hostname="github.com",
            known_hosts_path="/pinned/known_hosts",
        )
        assert "HostKeyAlias=github.com" in options

    def test_hostname_binds_to_validated_ip(self) -> None:
        """HostName is the EXACT validated IP — the connection targets this address."""
        validated_ip = "140.82.121.3"
        options = build_ssh_options(
            validated_ip,
            hostname="github.com",
            known_hosts_path="/pinned/known_hosts",
        )
        assert f"HostName={validated_ip}" in options

    def test_hostname_not_hostname(self) -> None:
        """HostName must NOT be the hostname — it must be the validated IP."""
        options = build_ssh_options(
            "140.82.121.3",
            hostname="github.com",
            known_hosts_path="/pinned/known_hosts",
        )
        # HostName should be the IP, not the hostname.
        hostname_entries = [opt for opt in options if opt.startswith("HostName=")]
        assert len(hostname_entries) == 1
        assert hostname_entries[0] == "HostName=140.82.121.3"
        assert "HostName=github.com" not in options

    def test_options_are_paired_minus_o(self) -> None:
        """Every option value is preceded by a -o flag."""
        options = build_ssh_options(
            "140.82.121.3",
            hostname="github.com",
            known_hosts_path="/pinned/known_hosts",
        )
        # Options come in pairs: ["-o", "key=value", "-o", "key=value", ...]
        # Verify the -o flags are at even indices.
        for i, opt in enumerate(options):
            if i % 2 == 0:
                assert opt == "-o", f"Expected '-o' at index {i}, got {opt!r}"


# ---------------------------------------------------------------------------
# build_git_ssh_command
# ---------------------------------------------------------------------------


class TestBuildGitSshCommand:
    def test_starts_with_ssh(self) -> None:
        cmd = build_git_ssh_command(
            "140.82.121.3",
            hostname="github.com",
            known_hosts_path="/pinned/known_hosts",
        )
        assert cmd.startswith("ssh ")

    def test_contains_all_hardened_options(self) -> None:
        cmd = build_git_ssh_command(
            "140.82.121.3",
            hostname="github.com",
            known_hosts_path="/pinned/known_hosts",
        )
        assert "StrictHostKeyChecking=yes" in cmd
        assert "UserKnownHostsFile=/pinned/known_hosts" in cmd
        assert "HostKeyAlias=github.com" in cmd
        assert "HostName=140.82.121.3" in cmd

    def test_hostname_binds_to_validated_ip(self) -> None:
        """The GIT_SSH_COMMAND's HostName must be the exact validated IP."""
        validated_ip = "140.82.121.3"
        cmd = build_git_ssh_command(
            validated_ip,
            hostname="github.com",
            known_hosts_path="/pinned/known_hosts",
        )
        # The command must bind HostName to the validated IP.
        assert f"HostName={validated_ip}" in cmd
        # And NOT to the hostname.
        assert "HostName=github.com" not in cmd


# ---------------------------------------------------------------------------
# build_ssh_command
# ---------------------------------------------------------------------------


class TestBuildSshCommand:
    def test_includes_target_user_at_ip(self) -> None:
        cmd = build_ssh_command(
            "140.82.121.3",
            hostname="github.com",
            known_hosts_path="/pinned/known_hosts",
        )
        assert "git@140.82.121.3" in cmd

    def test_target_is_validated_ip_not_hostname(self) -> None:
        """The SSH target must be user@validated_ip, NOT user@hostname."""
        cmd = build_ssh_command(
            "140.82.121.3",
            hostname="github.com",
            known_hosts_path="/pinned/known_hosts",
        )
        assert "git@github.com" not in cmd
        assert "git@140.82.121.3" in cmd

    def test_extra_args_appended(self) -> None:
        cmd = build_ssh_command(
            "140.82.121.3",
            hostname="github.com",
            known_hosts_path="/pinned/known_hosts",
            extra_args=["-p", "2222"],
        )
        assert "-p" in cmd
        assert "2222" in cmd
        # Extra args come before the target.
        target_idx = cmd.index("git@140.82.121.3")
        port_idx = cmd.index("2222")
        assert port_idx < target_idx


# ---------------------------------------------------------------------------
# build_ssh_transport_script
# ---------------------------------------------------------------------------


class TestBuildSshTransportScript:
    def test_script_contains_mkdir(self) -> None:
        script = build_ssh_transport_script(
            hostname="github.com",
            validated_ip="140.82.121.3",
        )
        assert "mkdir -p" in script

    def test_script_contains_known_hosts_path(self) -> None:
        script = build_ssh_transport_script(
            hostname="github.com",
            validated_ip="140.82.121.3",
            known_hosts_path="/home/user/.ssh/known_hosts",
        )
        assert "/home/user/.ssh/known_hosts" in script

    def test_script_contains_printf(self) -> None:
        """known_hosts content is written via printf (POSIX portable)."""
        script = build_ssh_transport_script(
            hostname="github.com",
            validated_ip="140.82.121.3",
        )
        assert "printf" in script

    def test_script_contains_github_keys(self) -> None:
        script = build_ssh_transport_script(
            hostname="github.com",
            validated_ip="140.82.121.3",
        )
        assert "github.com ssh-rsa" in script
        assert "github.com ssh-ed25519" in script

    def test_script_exports_git_ssh_command(self) -> None:
        script = build_ssh_transport_script(
            hostname="github.com",
            validated_ip="140.82.121.3",
        )
        assert "export GIT_SSH_COMMAND=" in script

    def test_script_git_ssh_command_binds_validated_ip(self) -> None:
        """The exported GIT_SSH_COMMAND must use the validated IP in HostName."""
        script = build_ssh_transport_script(
            hostname="github.com",
            validated_ip="140.82.121.3",
        )
        assert "HostName=140.82.121.3" in script

    def test_script_contains_chmod(self) -> None:
        script = build_ssh_transport_script(
            hostname="github.com",
            validated_ip="140.82.121.3",
        )
        assert "chmod 0600" in script

    def test_script_shlex_quotes_paths(self) -> None:
        """Adversarial known_hosts_path is safely quoted."""
        script = build_ssh_transport_script(
            hostname="github.com",
            validated_ip="140.82.121.3",
            known_hosts_path="/home/user/.ssh/kn;own_hosts",
        )
        # shlex.quote wraps the path so the semicolon is not shell-interpreted.
        assert "'/home/user/.ssh/kn;own_hosts'" in script


# ---------------------------------------------------------------------------
# build_ssh_transport (orchestrator)
# ---------------------------------------------------------------------------


class TestBuildSshTransport:
    def test_returns_ssh_transport_config(self) -> None:
        with patch(
            "modulo.core.pipeline_engine.workspace_ssh._resolve_all_sync",
            return_value=["140.82.121.3"],
        ):
            config = build_ssh_transport("github.com")
            assert isinstance(config, SshTransportConfig)

    def test_validated_ip_equals_resolved_ip(self) -> None:
        """THE CRITICAL ASSERTION: the validated IP in the config must equal
        the IP returned by DNS resolution.  This proves the SSH connection
        targets the exact address the SSRF guard validated."""
        resolved_ip = "140.82.121.3"
        with patch(
            "modulo.core.pipeline_engine.workspace_ssh._resolve_all_sync",
            return_value=[resolved_ip],
        ):
            config = build_ssh_transport("github.com")
            assert config.validated_ip == resolved_ip

    def test_hostname_preserved(self) -> None:
        with patch(
            "modulo.core.pipeline_engine.workspace_ssh._resolve_all_sync",
            return_value=["140.82.121.3"],
        ):
            config = build_ssh_transport("github.com")
            assert config.hostname == "github.com"

    def test_git_ssh_command_binds_validated_ip(self) -> None:
        """GIT_SSH_COMMAND's HostName must be the validated IP, not the hostname."""
        resolved_ip = "140.82.121.3"
        with patch(
            "modulo.core.pipeline_engine.workspace_ssh._resolve_all_sync",
            return_value=[resolved_ip],
        ):
            config = build_ssh_transport("github.com")
            assert f"HostName={resolved_ip}" in config.git_ssh_command
            assert "HostName=github.com" not in config.git_ssh_command

    def test_ssh_command_binds_validated_ip(self) -> None:
        resolved_ip = "140.82.121.3"
        with patch(
            "modulo.core.pipeline_engine.workspace_ssh._resolve_all_sync",
            return_value=[resolved_ip],
        ):
            config = build_ssh_transport("github.com")
            assert f"HostName={resolved_ip}" in config.ssh_command
            assert "HostName=github.com" not in config.ssh_command

    def test_ssh_command_targets_ip_not_hostname(self) -> None:
        """The SSH target must be user@validated_ip, NOT user@hostname."""
        resolved_ip = "140.82.121.3"
        with patch(
            "modulo.core.pipeline_engine.workspace_ssh._resolve_all_sync",
            return_value=[resolved_ip],
        ):
            config = build_ssh_transport("github.com")
            assert "git@github.com" not in config.ssh_command
            assert f"git@{resolved_ip}" in config.ssh_command

    def test_known_hosts_content_present(self) -> None:
        with patch(
            "modulo.core.pipeline_engine.workspace_ssh._resolve_all_sync",
            return_value=["140.82.121.3"],
        ):
            config = build_ssh_transport("github.com")
            assert "github.com ssh-rsa" in config.known_hosts_content
            assert "github.com ssh-ed25519" in config.known_hosts_content

    def test_script_snippet_exports_git_ssh_command(self) -> None:
        with patch(
            "modulo.core.pipeline_engine.workspace_ssh._resolve_all_sync",
            return_value=["140.82.121.3"],
        ):
            config = build_ssh_transport("github.com")
            assert "export GIT_SSH_COMMAND=" in config.script_snippet

    def test_custom_known_hosts_path(self) -> None:
        with patch(
            "modulo.core.pipeline_engine.workspace_ssh._resolve_all_sync",
            return_value=["140.82.121.3"],
        ):
            config = build_ssh_transport(
                "github.com",
                known_hosts_path="/custom/path/known_hosts",
            )
            assert config.known_hosts_path == "/custom/path/known_hosts"
            assert "/custom/path/known_hosts" in config.git_ssh_command

    def test_private_ip_refused(self) -> None:
        """A hostname resolving to a private IP is refused — the REFUSE-TO-SHIP gate."""
        with (
            patch(
                "modulo.core.pipeline_engine.workspace_ssh._resolve_all_sync",
                return_value=["192.168.1.1"],
            ),
            pytest.raises(SshHostRefusedError, match="blocked address"),
        ):
            build_ssh_transport("internal.example.com")

    def test_loopback_refused(self) -> None:
        """Loopback is refused without an explicit allowlist."""
        with (
            patch(
                "modulo.core.pipeline_engine.workspace_ssh._resolve_all_sync",
                return_value=["127.0.0.1"],
            ),
            pytest.raises(SshHostRefusedError, match="blocked address"),
        ):
            build_ssh_transport("localhost")

    def test_unknown_host_key_refused(self) -> None:
        """A host without pinned keys is refused — the REFUSE-TO-SHIP gate."""
        with (
            patch(
                "modulo.core.pipeline_engine.workspace_ssh._resolve_all_sync",
                return_value=["93.184.216.34"],
            ),
            pytest.raises(ValueError, match="No published SSH host keys"),
        ):
            build_ssh_transport("unknown-host.example.com")

    def test_multiple_resolved_ips_first_pinned(self) -> None:
        """When multiple IPs resolve, the first is pinned as the primary."""
        with patch(
            "modulo.core.pipeline_engine.workspace_ssh._resolve_all_sync",
            return_value=["140.82.121.3", "140.82.121.4"],
        ):
            config = build_ssh_transport("github.com")
            assert config.validated_ip == "140.82.121.3"

    def test_resolved_equals_validated_in_command(self) -> None:
        """End-to-end: the SSH command's HostName must be exactly the resolved IP.

        This is the test that proves resolved == validated — the SSH transport
        binds to the exact address the SSRF guard validated, not a
        re-resolved or attacker-controlled address.
        """
        resolved_ip = "140.82.121.3"
        with patch(
            "modulo.core.pipeline_engine.workspace_ssh._resolve_all_sync",
            return_value=[resolved_ip],
        ):
            config = build_ssh_transport("github.com")
            # HostName in GIT_SSH_COMMAND must be the exact resolved IP.
            assert f"HostName={resolved_ip}" in config.git_ssh_command
            # HostName in ssh command must be the exact resolved IP.
            assert f"HostName={resolved_ip}" in config.ssh_command
            # validated_ip field must be the exact resolved IP.
            assert config.validated_ip == resolved_ip
            # Target user@ must be the exact resolved IP.
            assert f"git@{resolved_ip}" in config.ssh_command
