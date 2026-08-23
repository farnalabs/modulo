"""Unit tests for the SSRF safe-URL validation helpers.

Covers the literal-IP blocking branch (the core security primitive), the
hostname-resolution path via patched resolvers, the async variant used by
event-loop-hostile callers, and the false-positive regression where a
hostname literally containing "private/internal" must not be spuriously
blocked.
"""

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
