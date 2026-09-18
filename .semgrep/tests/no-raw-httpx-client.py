# Fixture proving the `no-raw-httpx-client` gate fires on a forbidden
# construction and stays silent on a pinned-client construction.
#
# This file is NOT imported by the app or the test suite; it exists only for the
# semgrep rule test runner, which reads the expected-finding / no-finding
# annotation markers (ruleid / ok) on each case. A raw `httpx.AsyncClient(...)`
# or `httpx.Client(...)` outside the pinned factory (core.ssrf) is exactly the
# regression the gate is designed to catch.

import httpx


def unsafe_raw_async_constructor(base_url: str) -> httpx.AsyncClient:
    # ruleid: no-raw-httpx-client
    return httpx.AsyncClient(base_url=base_url, timeout=30)


def unsafe_raw_sync_constructor(base_url: str) -> httpx.Client:
    # ruleid: no-raw-httpx-client
    return httpx.Client(base_url=base_url, timeout=30)


def unsafe_imported_constructor(base_url: str) -> httpx.AsyncClient:
    from httpx import AsyncClient

    # ruleid: no-raw-httpx-client
    return AsyncClient(base_url=base_url, timeout=30)


def unsafe_imported_sync_constructor(base_url: str) -> httpx.Client:
    Client = httpx.Client

    # ruleid: no-raw-httpx-client
    return Client(base_url=base_url, timeout=30)


def safe_pinned_constructor(base_url: str) -> httpx.AsyncClient:
    # ok: no-raw-httpx-client
    return pinned_async_client_sync(base_url, base_url=base_url, timeout=30)
