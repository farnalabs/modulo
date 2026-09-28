"""Project-level conftest — shared test utilities only.

Do NOT put connector-specific fixtures here; they belong in
``tests/connectors/conftest.py``.
"""

import os

import pytest

import modulo.core.ssrf as _ssrf

# FAR-1050 R5 (the dispatch flip): the PRODUCT default for
# ``MODULO_E2B_VIA_PROVIDER`` is now ON, so every gated call site routes
# through the RuntimeProvider ABC unless told otherwise. Most of this suite
# predates the flip and exercises the LEGACY direct path (mocking
# ``AsyncSandbox.create``) without pinning the flag, so the baseline is pinned
# to the REVERT value here — one definition at the root, applying to every
# sub-suite — exactly as it was before the default changed. Forced (not
# ``setdefault``) so a stray exported value cannot silently flip the whole
# suite onto the provider path; any test that wants a different value
# overrides it per-test with ``monkeypatch.setenv`` / ``_enable_flag``.
#
# The flag-ON (default) path is covered by the ``tests/unit/pipeline_engine/
# test_e2b_*`` files, and the shipped default itself is asserted against a
# ``Settings`` built with this override cleared.
#
# Deliberately an import-time pin with a semgrep waiver, NOT an autouse
# ``monkeypatch.setenv`` fixture: ``get_settings()`` is ``lru_cache``d and every
# gated site reads the flag through it, so the value must be in the environment
# before the FIRST ``get_settings()`` call. Conftest import precedes test-module
# collection, which guarantees that; a per-test fixture runs only after
# collection-time imports could already have populated the cache, which would
# leave the baseline stuck on the new ON product default. The set is
# intentionally not auto-restored — per-test overrides still go through
# ``monkeypatch`` and revert themselves.
os.environ["MODULO_E2B_VIA_PROVIDER"] = "false"  # nosemgrep: environ-mutation-without-monkeypatch


@pytest.fixture(autouse=True)
def _allow_test_hostnames(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutralise real DNS in the SSRF guard for the whole backend suite.

    This is the SINGLE definition of the shim — it lives at the root of
    ``tests/`` so it applies to every sub-suite (unit, connectors, model
    backends, BDD). Do not copy it into a per-directory conftest.

    The SSRF guard (``modulo.core.ssrf``) performs real DNS resolution, which is
    unavailable in CI sandboxes. Every backend test mocks the HTTP layer (respx
    or a patched ``AsyncClient``) against ``example.com`` / ``localhost`` hosts,
    so the guard fails closed on absent DNS and breaks the suite.

    Stubbing ``ssrf._resolve_all_sync`` / ``_resolve_all_async`` makes any
    hostname validate as a public address. This only affects the *validation*
    pre-check: production call sites use ``validate_outbound_url`` (no connection
    pinning), so no test will actually connect to the stubbed address. Literal
    private/loopback IPs remain blocked via a separate DNS-independent code
    path, so the control is not weakened.

    Two opt-outs exist so the shim can never hide a fail-closed regression:

    * ``tests/unit/core/test_ssrf.py`` exercises the REAL resolver (including
      DNS timeouts and resolution failures) and is skipped by filename.
    * Any test marked ``@pytest.mark.real_ssrf_dns`` gets the real resolver, so
      the connector / model-backend gate tests can prove the guard rejects a
      private or loopback ``base_url``.
    """
    # The SSRF unit suite validates the real resolver; never stub it there, or
    # the fail-closed timeout/failure assertions would be silently defeated.
    if "test_ssrf" in getattr(request.node.path, "name", ""):
        return
    # Explicit per-test opt-out for suites that assert the guard fails closed.
    if request.node.get_closest_marker("real_ssrf_dns") is not None:
        return

    monkeypatch.setattr(_ssrf, "_resolve_all_sync", lambda host: ["8.8.8.8"])

    async def _fake_resolve(_host: str) -> list[str]:  # pragma: no cover - test shim
        return ["8.8.8.8"]

    monkeypatch.setattr(_ssrf, "_resolve_all_async", _fake_resolve)


pytest_plugins = ["tests.quarantine_plugin"]
