"""Tests for FAR-799 SSH transport hardening (managed workspace inputs P1).

Covers:
  - ``build_ssh_transport_options``: StrictHostKeyChecking=yes, HostKeyAlias,
    HostName, UserKnownHostsFile present; accept-new / StrictHostKeyChecking=no
    absent; shlex.quote on adversarial values
  - ``resolve_and_validate_ssh_host``: blocks a resolved private IP; returns a
    public IP on clean resolution
  - ``assert_known_hosts_is_pinned``: raises on missing / empty file; passes on
    a non-empty fixture
"""

from __future__ import annotations

import pathlib
import shlex
from unittest.mock import AsyncMock, patch

import pytest

from modulo.core.pipeline_engine.sandbox_policy import (
    SANDBOX_KNOWN_HOSTS_PINNED_PATH,
    assert_known_hosts_is_pinned,
    build_ssh_transport_options,
    resolve_and_validate_ssh_host,
)

# ---------------------------------------------------------------------------
# build_ssh_transport_options
# ---------------------------------------------------------------------------


class TestBuildSshTransportOptions:
    """Pure builder — no I/O, no sandbox."""

    def test_strict_host_key_checking_yes(self) -> None:
        opts = build_ssh_transport_options(host="github.com", resolved_ip="140.82.121.3")
        assert "StrictHostKeyChecking=yes" in opts

    def test_host_key_alias_matches_host(self) -> None:
        opts = build_ssh_transport_options(host="github.com", resolved_ip="140.82.121.3")
        assert "HostKeyAlias=github.com" in opts

    def test_hostname_matches_resolved_ip(self) -> None:
        opts = build_ssh_transport_options(host="github.com", resolved_ip="140.82.121.3")
        assert "HostName=140.82.121.3" in opts

    def test_user_known_hosts_file_present(self) -> None:
        opts = build_ssh_transport_options(host="github.com", resolved_ip="140.82.121.3")
        assert "UserKnownHostsFile=" in opts

    def test_accept_new_absent(self) -> None:
        opts = build_ssh_transport_options(host="github.com", resolved_ip="140.82.121.3")
        assert "accept-new" not in opts

    def test_strict_host_key_checking_no_absent(self) -> None:
        opts = build_ssh_transport_options(host="github.com", resolved_ip="140.82.121.3")
        assert "StrictHostKeyChecking=no" not in opts

    def test_custom_known_hosts_path(self) -> None:
        opts = build_ssh_transport_options(
            host="github.com", resolved_ip="140.82.121.3", known_hosts_path="/tmp/pinned_kh"
        )
        assert "UserKnownHostsFile=/tmp/pinned_kh" in opts

    def test_adversarial_host_is_shlex_quoted(self) -> None:
        """Adversarial host values are shlex.quote-d for shell safety."""
        adversarial = "github.com'; echo pwned; #"
        expected_quoted = shlex.quote(adversarial)
        opts = build_ssh_transport_options(host=adversarial, resolved_ip="140.82.121.3")
        # The raw adversarial string must NOT appear unquoted in the output.
        assert adversarial not in opts
        # The shell-safe quoted form must appear.
        assert f"HostKeyAlias={expected_quoted}" in opts

    def test_adversarial_ip_is_shlex_quoted(self) -> None:
        """Adversarial IP values are shlex.quote-d for shell safety."""
        adversarial = "140.82.121.3'; mkdir /tmp/evil; #"
        expected_quoted = shlex.quote(adversarial)
        opts = build_ssh_transport_options(host="github.com", resolved_ip=adversarial)
        assert adversarial not in opts
        assert f"HostName={expected_quoted}" in opts

    def test_output_contains_dash_o_prefixes(self) -> None:
        opts = build_ssh_transport_options(host="github.com", resolved_ip="140.82.121.3")
        assert opts.startswith("-o ")
        assert opts.count("-o ") == 4

    def test_default_known_hosts_path(self) -> None:
        opts = build_ssh_transport_options(host="github.com", resolved_ip="140.82.121.3")
        assert SANDBOX_KNOWN_HOSTS_PINNED_PATH in opts


# ---------------------------------------------------------------------------
# resolve_and_validate_ssh_host
# ---------------------------------------------------------------------------


class TestResolveAndValidateSshHost:
    """Async — uses mock resolver to avoid real DNS."""

    async def test_returns_public_ip(self) -> None:
        with patch(
            "modulo.core.ssrf._resolve_all_async",
            new_callable=AsyncMock,
            return_value=["140.82.121.3"],
        ):
            result = await resolve_and_validate_ssh_host("github.com")
        assert result == "140.82.121.3"

    async def test_blocks_private_ip(self) -> None:
        with (
            patch(
                "modulo.core.ssrf._resolve_all_async",
                new_callable=AsyncMock,
                return_value=["10.0.0.1"],
            ),
            pytest.raises(ValueError, match="private/internal"),
        ):
            await resolve_and_validate_ssh_host("internal.corp")

    async def test_blocks_link_local(self) -> None:
        with (
            patch(
                "modulo.core.ssrf._resolve_all_async",
                new_callable=AsyncMock,
                return_value=["169.254.1.1"],
            ),
            pytest.raises(ValueError, match="private/internal"),
        ):
            await resolve_and_validate_ssh_host("metadata.local")

    async def test_empty_resolution_raises(self) -> None:
        with (
            patch(
                "modulo.core.ssrf._resolve_all_async",
                new_callable=AsyncMock,
                return_value=[],
            ),
            pytest.raises(ValueError, match="no addresses"),
        ):
            await resolve_and_validate_ssh_host("nonexistent.invalid")

    async def test_mixed_ips_all_blocked(self) -> None:
        """All resolved addresses are private — must raise."""
        with (
            patch(
                "modulo.core.ssrf._resolve_all_async",
                new_callable=AsyncMock,
                return_value=["10.0.0.1", "192.168.1.1"],
            ),
            pytest.raises(ValueError, match="private/internal"),
        ):
            await resolve_and_validate_ssh_host("private.host")


# ---------------------------------------------------------------------------
# assert_known_hosts_is_pinned
# ---------------------------------------------------------------------------


class TestAssertKnownHostsIsPinned:
    def test_raises_on_missing_file(self, tmp_path: pathlib.Path) -> None:
        missing = str(tmp_path / "known_hosts")
        with pytest.raises(FileNotFoundError, match="not found"):
            assert_known_hosts_is_pinned(missing)

    def test_raises_on_empty_file(self, tmp_path: pathlib.Path) -> None:
        kh = tmp_path / "known_hosts"
        kh.write_text("")
        with pytest.raises(ValueError, match="empty"):
            assert_known_hosts_is_pinned(str(kh))

    def test_passes_on_non_empty_file(self, tmp_path: pathlib.Path) -> None:
        kh = tmp_path / "known_hosts"
        # Minimal fixture line (not a real key — production seeding is deferred).
        kh.write_text("github.com ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABgQCtest\n")
        # Must not raise.
        assert_known_hosts_is_pinned(str(kh))


# ---------------------------------------------------------------------------
# Constant
# ---------------------------------------------------------------------------


class TestKnownHostsConstant:
    def test_value(self) -> None:
        assert SANDBOX_KNOWN_HOSTS_PINNED_PATH == "/home/user/.ssh/known_hosts"

    def test_is_string(self) -> None:
        assert isinstance(SANDBOX_KNOWN_HOSTS_PINNED_PATH, str)
