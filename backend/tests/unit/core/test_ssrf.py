"""Unit tests for the SSRF safe-URL validation helpers.

Covers the literal-IP blocking branch (the core security primitive), the
hostname-resolution path via patched resolvers, the async variant used by
event-loop-hostile callers, and the false-positive regression where a
hostname literally containing "private/internal" must not be spuriously
blocked.
"""

from unittest.mock import patch

import pytest

from modulo.core import ssrf


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8080/admin",
        "http://10.0.0.5/",
        "http://192.168.1.1/",
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata
        "http://100.100.100.200/latest/meta-data/",  # Aliyun metadata
        "http://100.64.0.1/",  # CGNAT
        "http://0.0.0.0/",  # current network
    ],
)
def test_validate_outbound_url_blocks_private_literal_ips(url: str) -> None:
    with pytest.raises(ValueError, match="private/internal"):
        ssrf.validate_outbound_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://8.8.8.8/",
        "http://1.1.1.1/",
    ],
)
def test_validate_outbound_url_accepts_public_literal_ips_without_dns(url: str) -> None:
    # Public literal IPs must be accepted without any DNS lookup: the
    # validator returns None (no exception) and must not attempt DNS, which
    # would raise for a bare literal IP if the resolver were consulted.
    assert ssrf.validate_outbound_url(url) is None


def test_validate_outbound_url_blocks_hostname_resolving_to_internal() -> None:
    def fake_resolve(host: str) -> list[str]:
        assert host == "example.internal.com"
        return ["10.0.0.5"]

    try:
        original = ssrf._resolve_all_sync
        ssrf._resolve_all_sync = fake_resolve
        with pytest.raises(ValueError, match="resolves to a private/internal address"):
            ssrf.validate_outbound_url("http://example.internal.com/")
    finally:
        ssrf._resolve_all_sync = original


def test_validate_outbound_url_accepts_hostname_resolving_to_public() -> None:
    def fake_resolve(host: str) -> list[str]:
        assert host == "example.com"
        return ["93.184.216.34"]

    try:
        original = ssrf._resolve_all_sync
        ssrf._resolve_all_sync = fake_resolve
        ssrf.validate_outbound_url("http://example.com/")
    finally:
        ssrf._resolve_all_sync = original


def test_hostname_containing_private_internal_is_not_spuriously_blocked() -> None:
    """Regression: a hostname with 'private/internal' in its name must be
    resolved, not rejected by the literal-IP false-positive path."""

    def fake_resolve(host: str) -> list[str]:
        assert host == "private.internal.example.com"
        return ["93.184.216.34"]

    try:
        original = ssrf._resolve_all_sync
        ssrf._resolve_all_sync = fake_resolve
        ssrf.validate_outbound_url("https://private.internal.example.com/")
    finally:
        ssrf._resolve_all_sync = original


async def test_validate_outbound_url_async_blocks_literal_ip() -> None:
    with pytest.raises(ValueError, match="private/internal"):
        await ssrf.validate_outbound_url_async("http://127.0.0.1/")


async def test_validate_outbound_url_async_resolves_and_blocks_internal() -> None:
    async def fake_resolve(host: str) -> list[str]:
        assert host == "collector.internal"
        return ["192.168.0.10"]

    try:
        original = ssrf._resolve_all_async
        ssrf._resolve_all_async = fake_resolve
        with pytest.raises(ValueError, match="resolves to a private/internal address"):
            await ssrf.validate_outbound_url_async("http://collector.internal:4318/")
    finally:
        ssrf._resolve_all_async = original


async def test_validate_outbound_url_async_accepts_public_hostname() -> None:
    async def fake_resolve(host: str) -> list[str]:
        assert host == "api.example.com"
        return ["93.184.216.34"]

    try:
        original = ssrf._resolve_all_async
        ssrf._resolve_all_async = fake_resolve
        await ssrf.validate_outbound_url_async("https://api.example.com/")
    finally:
        ssrf._resolve_all_async = original


def test_allowlist_allows_matching_private_range(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SSRF_ALLOW_PRIVATE_RANGES", "10.0.0.0/8")
    # 10.1.2.3 is private but explicitly allowlisted.
    assert ssrf.validate_outbound_url("http://10.1.2.3/") is None


def test_allowlist_multiple_cidrs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SSRF_ALLOW_PRIVATE_RANGES", "192.168.0.0/16,10.0.0.0/8")
    assert ssrf.validate_outbound_url("http://192.168.1.1/") is None
    assert ssrf.validate_outbound_url("http://10.0.0.9/") is None


def test_allowlist_metadata_range_still_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    # Only the allowlisted 10.0.0.0/8 is permitted; cloud metadata stays blocked.
    monkeypatch.setenv("SSRF_ALLOW_PRIVATE_RANGES", "10.0.0.0/8")
    with pytest.raises(ValueError, match="private/internal"):
        ssrf.validate_outbound_url("http://169.254.169.254/latest/meta-data/")
    with pytest.raises(ValueError, match="private/internal"):
        ssrf.validate_outbound_url("http://127.0.0.1/")


def test_allowlist_invalid_entry_logged_and_other_cidrs_apply(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("SSRF_ALLOW_PRIVATE_RANGES", "not-a-cidr,10.0.0.0/8")
    assert ssrf.validate_outbound_url("http://10.1.2.3/") is None
    assert any("ssrf.invalid_allowlist_entry" in r.message for r in caplog.records)


def test_allowlist_honours_runtime_env_change(monkeypatch: pytest.MonkeyPatch) -> None:
    """The allowlist is parsed lazily and cached on the env value, so a
    mid-process env change takes effect without a restart."""
    monkeypatch.setenv("SSRF_ALLOW_PRIVATE_RANGES", "")
    with pytest.raises(ValueError, match="private/internal"):
        ssrf.validate_outbound_url("http://10.1.2.3/")
    monkeypatch.setenv("SSRF_ALLOW_PRIVATE_RANGES", "10.0.0.0/8")
    assert ssrf.validate_outbound_url("http://10.1.2.3/") is None
    # And switching back off re-blocks.
    monkeypatch.setenv("SSRF_ALLOW_PRIVATE_RANGES", "")
    with pytest.raises(ValueError, match="private/internal"):
        ssrf.validate_outbound_url("http://10.1.2.3/")


# ---------------------------------------------------------------------------
# URL syntax validation (all rejected before any DNS resolution)
# ---------------------------------------------------------------------------


class TestURLSyntaxValidation:
    def test_empty_url_raises(self) -> None:
        with pytest.raises(ValueError, match="URL is required"):
            ssrf.validate_outbound_url("")

    def test_none_url_raises(self) -> None:
        with pytest.raises(ValueError, match="URL is required"):
            ssrf.validate_outbound_url(None)  # type: ignore[arg-type]

    def test_non_string_url_raises(self) -> None:
        with pytest.raises(ValueError, match="URL is required"):
            ssrf.validate_outbound_url(123)  # type: ignore[arg-type]

    def test_non_http_scheme_raises(self) -> None:
        with pytest.raises(ValueError, match="http:// or https://"):
            ssrf.validate_outbound_url("ftp://example.com/file")

    def test_missing_hostname_raises(self) -> None:
        with pytest.raises(ValueError, match="valid hostname"):
            ssrf.validate_outbound_url("http:///path")

    def test_public_ip_with_userinfo_accepted_without_dns(self) -> None:
        assert ssrf.validate_outbound_url("http://user:pass@8.8.8.8/path") is None


# ---------------------------------------------------------------------------
# IPv6 literal addresses
# ---------------------------------------------------------------------------


class TestIPV6LiteralAddresses:
    def test_ipv6_loopback_blocked(self) -> None:
        with pytest.raises(ValueError, match="private/internal"):
            ssrf.validate_outbound_url("http://[::1]/")

    def test_ipv6_link_local_blocked(self) -> None:
        with pytest.raises(ValueError, match="private/internal"):
            ssrf.validate_outbound_url("http://[fe80::1]/")

    def test_ipv6_unique_local_blocked(self) -> None:
        with pytest.raises(ValueError, match="private/internal"):
            ssrf.validate_outbound_url("http://[fd12:3456::1]/")

    def test_ipv6_public_accepted_without_dns(self) -> None:
        assert ssrf.validate_outbound_url("http://[2606:4700:4700::1111]/") is None

    def test_ipv6_allowlist_overrides_private_check(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SSRF_ALLOW_PRIVATE_RANGES", "fc00::/7")
        assert ssrf.validate_outbound_url("http://[fd12:3456::1]/") is None

    def test_ipv6_link_local_allowlist_still_blocked(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SSRF_ALLOW_PRIVATE_RANGES", "10.0.0.0/8")
        with pytest.raises(ValueError, match="private/internal"):
            ssrf.validate_outbound_url("http://[fe80::1]/")


# ---------------------------------------------------------------------------
# Hostname resolution path
# ---------------------------------------------------------------------------


class TestHostnameResolution:
    def test_dns_resolution_failure_is_fail_closed(self) -> None:
        with (
            patch.object(ssrf.socket, "getaddrinfo", side_effect=ssrf.socket.gaierror("no such host")),
            pytest.raises(ValueError, match="DNS resolution failed"),
        ):
            ssrf.validate_outbound_url("http://inexistent.example/")

    async def test_async_dns_resolution_failure_is_fail_closed(self) -> None:
        async def fail_resolve(host: str) -> list[str]:
            raise ValueError(f"DNS resolution failed for {host}. Cannot verify the target is not internal.")

        with (
            patch.object(ssrf, "_resolve_all_async", fail_resolve),
            pytest.raises(ValueError, match="DNS resolution failed"),
        ):
            await ssrf.validate_outbound_url_async("http://dead.example/")

    def test_hostname_resolving_to_any_internal_address_blocked(self) -> None:
        def fake_resolve(host: str) -> list[str]:
            return ["93.184.216.34", "10.0.0.5"]

        with (
            patch.object(ssrf, "_resolve_all_sync", fake_resolve),
            pytest.raises(ValueError, match=r"10\.0\.0\.5"),
        ):
            ssrf.validate_outbound_url("http://mixed.example/")

    def test_hostname_resolving_to_internal_ipv6_blocked(self) -> None:
        with (
            patch.object(ssrf, "_resolve_all_sync", lambda host: ["fd12:3456::1"]),
            pytest.raises(ValueError, match="resolves to a private/internal address"),
        ):
            ssrf.validate_outbound_url("http://ipv6.internal.example/")

    def test_hostname_resolving_to_nothing_accepted(self) -> None:
        # A hostname with no resolved records is treated as external.
        with patch.object(ssrf, "_resolve_all_sync", lambda host: []):
            assert ssrf.validate_outbound_url("http://empty.example/") is None

    def test_hostname_normalized_before_resolution(self) -> None:
        """Trailing dots and ports must be stripped before the resolver is consulted."""
        resolved: list[str] = []

        def fake_resolve(host: str) -> list[str]:
            resolved.append(host)
            return ["93.184.216.34"]

        with patch.object(ssrf, "_resolve_all_sync", fake_resolve):
            ssrf.validate_outbound_url("http://example.com.:8080/path")

        assert resolved == ["example.com"]


# ---------------------------------------------------------------------------
# Non-canonical IP encodings (FAR-413 SSRF adversarial)
# ---------------------------------------------------------------------------
# A resolver/httpx client is far more liberal than ``ipaddress.ip_address``
# which only accepts dotted-quad / bracketed-IPv6 *strings*. Loopback shown as
# a single decimal or hex integer (``2130706433`` / ``0x7f000001``) must be
# blocked by the literal-IP guard rather than escaping to the DNS path (where
# an integer-decoding resolver would turn it back into 127.0.0.1).


class TestNonCanonicalIPEncodings:
    def test_decimal_integer_ipv4_loopback_blocked(self) -> None:
        with pytest.raises(ValueError, match="private/internal"):
            ssrf.validate_outbound_url("http://2130706433/")

    def test_decimal_integer_ipv4_private_blocked(self) -> None:
        with pytest.raises(ValueError, match="private/internal"):
            ssrf.validate_outbound_url("http://167772161/")  # 10.0.0.1

    def test_hex_integer_ipv4_loopback_blocked(self) -> None:
        with pytest.raises(ValueError, match="private/internal"):
            ssrf.validate_outbound_url("http://0x7f000001/")

    def test_hex_integer_ipv4_metadata_blocked(self) -> None:
        with pytest.raises(ValueError, match="private/internal"):
            ssrf.validate_outbound_url("http://0xa9fea9fe/")  # 169.254.169.254

    def test_dotted_hex_ipv4_loopback_blocked(self) -> None:
        # 0x7f.0.0.1 -> 127.0.0.1 (per-segment hex base detection).
        with pytest.raises(ValueError, match="private/internal"):
            ssrf.validate_outbound_url("http://0x7f.0.0.1/")

    def test_dotted_octal_ipv4_loopback_blocked(self) -> None:
        # 0177.0.0.1 -> 127.0.0.1 (per-segment octal base detection).
        with pytest.raises(ValueError, match="private/internal"):
            ssrf.validate_outbound_url("http://0177.0.0.1/")

    def test_dotted_hex_hostname_not_spuriously_blocked_when_resolves_public(self) -> None:
        """A hostname that merely CONTAINS a hex-looking segment must NOT be
        spuriously treated as a non-canonical IPv4: the segment parser bails on
        the non-numeric segment, so it falls through to the DNS path and is
        accepted when it resolves to a public address."""

        def fake_resolve(host: str) -> list[str]:
            assert host == "0x7f.helpers.example.com"
            return ["93.184.216.34"]

        with patch.object(ssrf, "_resolve_all_sync", fake_resolve):
            assert ssrf.validate_outbound_url("http://0x7f.helpers.example.com/") is None

    async def test_async_decimal_integer_ipv4_loopback_blocked(self) -> None:
        with pytest.raises(ValueError, match="private/internal"):
            await ssrf.validate_outbound_url_async("http://2130706433/")

    def test_encode_percent_host_is_fail_closed(self) -> None:
        # A %-encoded host is NOT decoded by urlparse, so the guard treats it as
        # a hostname and fails closed on the (invalid) DNS resolution.
        with pytest.raises(ValueError, match="DNS resolution failed"):
            ssrf.validate_outbound_url("http://127.0.0.1%00/")

    def test_userinfo_loopback_still_blocked(self) -> None:
        with pytest.raises(ValueError, match="private/internal"):
            ssrf.validate_outbound_url("http://user:pass@127.0.0.1/")

    def test_ipv4_mapped_ipv6_loopback_blocked(self) -> None:
        with pytest.raises(ValueError, match="private/internal"):
            ssrf.validate_outbound_url("http://[::ffff:127.0.0.1]/")

    def test_ipv4_mapped_ipv6_metadata_blocked(self) -> None:
        with pytest.raises(ValueError, match="private/internal"):
            ssrf.validate_outbound_url("http://[::ffff:169.254.169.254]/")


# ---------------------------------------------------------------------------
# DNS-rebinding — the guard re-resolves on every validation and never caches a
# resolution result. A hostname that flips between public and internal across
# two validations is therefore caught the moment it reads private; validation
# and connection are separate lookups (the residual risk is a rebind between a
# validation and the subsequent HTTP connection, documented in the module).
# ---------------------------------------------------------------------------


class TestDNSRebinding:
    def test_guard_re_resolves_each_validate_no_cached_public_answer(self) -> None:
        """The guard is not caching a first public answer between calls."""
        calls: list[str] = []
        answers = iter([["93.184.216.34"], ["10.0.0.5"]])  # public then internal

        def fake_resolve(host: str) -> list[str]:
            calls.append(host)
            return next(answers)

        with patch.object(ssrf, "_resolve_all_sync", fake_resolve):
            # First validation: public answer -> accepted.
            ssrf.validate_outbound_url("http://flip.example/")
            # Second validation: the same hostname now answers internal -> blocked.
            with pytest.raises(ValueError, match="resolves to a private/internal address"):
                ssrf.validate_outbound_url("http://flip.example/")

        # Each validation performed its own fresh resolution — no memoisation.
        assert len(calls) == 2

    async def test_async_guard_resolves_per_validation(self) -> None:
        calls = 0

        async def fake_resolve(host: str) -> list[str]:
            nonlocal calls
            calls += 1
            return ["93.184.216.34"]

        with patch.object(ssrf, "_resolve_all_async", fake_resolve):
            await ssrf.validate_outbound_url_async("http://fresh.example/")
            await ssrf.validate_outbound_url_async("http://fresh.example/")

        assert calls == 2
