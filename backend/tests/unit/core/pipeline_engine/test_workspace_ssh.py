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
    PUBLISHED_HOST_KEY_FINGERPRINTS,
    SshHostRefusedError,
    SshTransportConfig,
    build_git_ssh_command,
    build_ssh_command,
    build_ssh_options,
    build_ssh_transport,
    build_ssh_transport_script,
    generate_known_hosts_entry,
    generate_pinned_known_hosts,
    ssh_public_key_fingerprint,
    validate_and_pin_ip,
    verify_pinned_host_keys,
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


# ---------------------------------------------------------------------------
# Host key fingerprint verification (REFUSE-TO-SHIP gate)
# ---------------------------------------------------------------------------


class TestHostKeyFingerprints:
    """Prove the embedded host keys are GENUINE, not fabricated placeholders.

    Each test asserts the embedded key parses as a valid public key and that its
    SHA256 fingerprint matches the fingerprint the provider publishes — so a
    hand-crafted or stale blob can never be shipped as a "pinned" key.
    """

    def test_every_embedded_key_matches_published_fingerprint(self) -> None:
        """Every pinned key's SHA256 fingerprint equals the published value."""
        for hostname, published in PUBLISHED_HOST_KEY_FINGERPRINTS.items():
            from modulo.core.pipeline_engine.workspace_ssh import (
                KNOWN_HOSTS_BY_HOSTNAME,
            )

            for entry in KNOWN_HOSTS_BY_HOSTNAME[hostname]:
                key_type = entry.split(" ", 2)[1]
                expected = published[key_type]
                actual = ssh_public_key_fingerprint(entry)
                assert actual == expected, f"{hostname} {key_type} fingerprint {actual} != published {expected}"

    def test_verify_pinned_host_keys_passes_on_genuine_keys(self) -> None:
        """The gate accepts the real, published keys."""
        # The gate must return cleanly (None) when given the genuine keys,
        # proving it neither raises nor silently shorthands the verification.
        assert verify_pinned_host_keys() is None

    def test_tampered_key_is_refused(self) -> None:
        """A key whose blob does not match the published fingerprint is rejected."""
        from modulo.core.pipeline_engine.workspace_ssh import (
            KNOWN_HOSTS_BY_HOSTNAME,
        )

        # Swap a github key for a gitlab one: the fingerprint will no longer match.
        bad = list(KNOWN_HOSTS_BY_HOSTNAME["github.com"])
        bad[0] = KNOWN_HOSTS_BY_HOSTNAME["gitlab.com"][0]
        original = KNOWN_HOSTS_BY_HOSTNAME["github.com"]
        try:
            KNOWN_HOSTS_BY_HOSTNAME["github.com"] = tuple(bad)
            with pytest.raises(ValueError, match="does not match the published"):
                verify_pinned_host_keys()
        finally:
            KNOWN_HOSTS_BY_HOSTNAME["github.com"] = original

    def test_embedded_key_parses_with_ssh_keygen(self) -> None:
        """Each embedded key is a parseable public key (ssh-keygen -lf)."""
        import shutil
        import subprocess
        import tempfile
        from pathlib import Path

        ssh_keygen = shutil.which("ssh-keygen")
        if ssh_keygen is None:
            pytest.skip("ssh-keygen not available")
        from modulo.core.pipeline_engine.workspace_ssh import (
            KNOWN_HOSTS_BY_HOSTNAME,
        )

        for hostname, keys in KNOWN_HOSTS_BY_HOSTNAME.items():
            for entry in keys:
                # Close the temp file BEFORE invoking ssh-keygen: on Windows an
                # open NamedTemporaryFile handle is exclusively locked, so the
                # child process would get "Permission denied".
                with tempfile.NamedTemporaryFile("w", suffix=".pub", delete=False) as fh:
                    fh.write(entry + "\n")
                    pub_path = fh.name
                try:
                    result = subprocess.run(  # noqa: S603
                        [ssh_keygen, "-lf", pub_path],
                        capture_output=True,
                        text=True,
                        check=False,
                        timeout=30,
                    )
                    assert result.returncode == 0, f"{hostname} key did not parse: {result.stderr.strip()}"
                finally:
                    Path(pub_path).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# build_ssh_command: user parameter
# ---------------------------------------------------------------------------


class TestBuildSshCommandUser:
    def test_default_user_is_git(self) -> None:
        cmd = build_ssh_command(
            "140.82.121.3",
            hostname="github.com",
            known_hosts_path="/pinned/known_hosts",
        )
        assert "git@140.82.121.3" in cmd

    def test_custom_user_is_honoured(self) -> None:
        """Non-GitHub remotes (e.g. gitlab.com) can pin a different login."""
        cmd = build_ssh_command(
            "140.82.121.3",
            hostname="gitlab.com",
            known_hosts_path="/pinned/known_hosts",
            user="git",
        )
        assert "git@140.82.121.3" in cmd

    def test_user_parameter_propagates_to_transport(self) -> None:
        resolved_ip = "140.82.121.3"
        with patch(
            "modulo.core.pipeline_engine.workspace_ssh._resolve_all_sync",
            return_value=[resolved_ip],
        ):
            config = build_ssh_transport("github.com", user="custom")
            assert f"custom@{resolved_ip}" in config.ssh_command


# ---------------------------------------------------------------------------
# build_ssh_transport_script: end-to-end round-trip through /bin/sh
# ---------------------------------------------------------------------------


class TestSshTransportScriptRoundTrip:
    """The only true round-trip for the printf-escaping logic: run the script
    with ``sh`` and confirm the known_hosts file is written and parseable."""

    def test_script_writes_parseable_known_hosts(self) -> None:
        import shutil
        import subprocess
        import tempfile
        from pathlib import Path

        sh_bin = shutil.which("sh")
        ssh_keygen = shutil.which("ssh-keygen")
        if sh_bin is None or ssh_keygen is None:
            pytest.skip("sh or ssh-keygen not available")
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            known_hosts = tmp_path / "known_hosts"
            script = build_ssh_transport_script(
                hostname="github.com",
                validated_ip="140.82.121.3",
                known_hosts_path=str(known_hosts),
            )
            script_path = tmp_path / "setup.sh"
            script_path.write_text(script)
            script_path.chmod(0o700)
            result = subprocess.run(  # noqa: S603
                [sh_bin, str(script_path)],
                capture_output=True,
                text=True,
                check=False,
                cwd=tmp,
                timeout=60,
            )
            assert result.returncode == 0, result.stderr
            # The known_hosts file must exist and be non-empty.
            assert known_hosts.exists()
            written = known_hosts.read_text()
            expected = generate_known_hosts_entry("github.com")
            assert written == expected
            # ssh-keygen must be able to read back the entry (valid format).
            lookup = subprocess.run(  # noqa: S603
                [ssh_keygen, "-F", "github.com", "-f", str(known_hosts)],
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
            assert lookup.returncode == 0, lookup.stderr
            assert "github.com" in lookup.stdout

    def test_script_exports_git_ssh_command_with_validated_ip(self) -> None:
        import shutil
        import subprocess
        import tempfile
        from pathlib import Path

        sh_bin = shutil.which("sh")
        if sh_bin is None:
            pytest.skip("sh not available")
        with tempfile.TemporaryDirectory() as tmp:
            known_hosts = Path(tmp) / "known_hosts"
            script = build_ssh_transport_script(
                hostname="github.com",
                validated_ip="140.82.121.3",
                known_hosts_path=str(known_hosts),
            )
            # Sourcing the script must put a GIT_SSH_COMMAND using the validated
            # IP onto the environment.
            check = script + '\necho "GIT_SSH_COMMAND=$GIT_SSH_COMMAND"\n'
            result = subprocess.run(  # noqa: S603
                [sh_bin, "-c", check],
                capture_output=True,
                text=True,
                check=False,
                cwd=tmp,
                timeout=60,
            )
            assert result.returncode == 0, result.stderr
            assert "HostName=140.82.121.3" in result.stdout
