"""Project-level conftest — shared test utilities only.

Do NOT put connector-specific fixtures here; they belong in
``tests/connectors/conftest.py``.
"""

import pytest

import modulo.auth.dependencies as _auth_dependencies
import modulo.core.ssrf as _ssrf

# Captured at conftest import time — before any test module is collected, and
# therefore before any fixture can replace the symbol. The guard below compares
# by IDENTITY (``is``) rather than ``isinstance(..., Mock)`` so that ANY
# replacement — a plain callable, a lambda, a partial — is caught, not just
# ``unittest.mock`` instances.
_REAL_VERIFY_IDENTITY = _auth_dependencies._verify_identity


def _assert_real_verify_identity() -> None:
    """Fail fast when a leaked patch has replaced ``_verify_identity``.

    Regression guard for the FAR-1631 leak class: two fixtures patching the
    SAME symbol (``modulo.auth.dependencies._verify_identity``) with DIFFERENT
    mechanisms (``unittest.mock.patch`` vs ``monkeypatch.setattr``) unwind their
    restores out of order, so ``monkeypatch``'s undo (it is a shared fixture
    torn down last) overwrites the real function with a *stale* mock. That
    silently changes every later test from that point on.

    Removing the original duplicate fixture did not retire the class — this
    guard's first run caught a live instance in the unit suite:
    ``tests/unit/conftest.py``'s ``_patch_verify_identity`` used
    ``unittest.mock.patch`` while per-module fixtures (e.g.
    ``tests/unit/test_schema_folders.py``'s ``_prevent_db_auth_check``) used
    ``monkeypatch.setattr``. ``_patch_verify_identity`` now also uses
    ``monkeypatch.setattr`` so both restores share one LIFO-correct undo stack.

    The comparison is by identity against the real function captured at import
    time, so it catches a replacement of any shape, not only ``Mock`` objects.
    Exposed as a module-level function (not inlined in the fixture) so the
    guard's own behaviour is covered by ``tests/architecture/
    test_verify_identity_guard.py`` — a guard that cannot fail is not a guard.
    """
    current = _auth_dependencies._verify_identity
    if current is not _REAL_VERIFY_IDENTITY:
        raise AssertionError(
            "modulo.auth.dependencies._verify_identity was replaced and not "
            "restored before test setup: a fixture leaked a patch (teardown "
            "restored a stale value). Expected the real function "
            f"{_REAL_VERIFY_IDENTITY!r} but found {current!r}. Fix the leaking "
            "fixture's teardown so it restores the original callable."
        )


@pytest.fixture(autouse=True)
def _guard_real_verify_identity() -> None:
    """Run :func:`_assert_real_verify_identity` at every test SETUP.

    This root-level autouse guard runs before the legitimate patchers, which
    set up strictly after it, so it observes the state left by the *previous*
    test's teardown. A leaked replacement is caught here on the next test
    instead of silently changing its behaviour.
    """
    _assert_real_verify_identity()


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
