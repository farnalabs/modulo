"""In-memory double for the ``audited(...)`` isolated audit write (FAR-1472).

Both the unit and BDD suites drive the real FastAPI routes with mocked DB
sessions, so the ``audited()`` route dependency's fresh-session append would
otherwise reach a real engine on every annotated mutating request: unit tests
would dial a dead port, and BDD tests would open the sqlite shared engine,
which has no ``audit_chain_heads`` table (``sqlite3.OperationalError``), turning
every annotated mutating request into an HTTP 500.

This double implements exactly the surface the append path touches, so the
``fail_closed=True`` destruction routes still succeed and the test observes the
route's own business behaviour. It is shared by the two suites so their
definitions cannot drift.

The integration suite is deliberately excluded: it runs against real Postgres
with real migrations and exercises the genuine append.
"""

from types import SimpleNamespace
from typing import Self

import pytest

#: Bind object whose dialect is NOT postgresql, so ``set_rls_org`` /
#: ``set_rls_user_context`` take their generic ``session.info`` branch instead
#: of issuing ``set_config`` statements.
_AUDIT_TEST_BIND = SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))


class _AuditTestResult:
    """Result stand-in for the chain-head ``SELECT ... FOR UPDATE`` read."""

    def scalar_one_or_none(self) -> None:
        """No persisted chain head - the append creates the org's first event."""
        return


class _AuditTestTransaction:
    """Async context manager backing ``session.begin()`` / ``begin_nested()``."""

    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_exc: object) -> bool:
        return False


class AuditSessionDouble:
    """AsyncSession stand-in for the ``audited(...)`` isolated audit write.

    Test suites run without a real database, so the route dependency's
    fresh-session append would otherwise dial a dead port (unit) or a sqlite
    engine missing ``audit_chain_heads`` (BDD) on EVERY annotated mutating
    request - and, worse, RAISE on the ``fail_closed=True`` destruction routes,
    turning a green route test red for an environmental reason rather than a
    behavioural one. This double implements exactly the surface the append path
    touches: an active-transaction guard, a non-Postgres bind (so ``set_rls_*``
    store into ``session.info`` rather than issuing ``set_config``), a chain-head
    read that returns no head, and ``add``/``flush``.

    It deliberately records what it was handed (``added``) so a test can assert
    an audit event WAS built when that matters.
    """

    def __init__(self) -> None:
        self.info: dict[str, object] = {}
        self.added: list[object] = []

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False

    def in_transaction(self) -> bool:
        """The append path only runs inside an explicit ``begin()`` block."""
        return True

    def get_bind(self) -> SimpleNamespace:
        return _AUDIT_TEST_BIND

    def begin(self) -> _AuditTestTransaction:
        return _AuditTestTransaction()

    def begin_nested(self) -> _AuditTestTransaction:
        return _AuditTestTransaction()

    async def execute(self, *_args: object, **_kwargs: object) -> _AuditTestResult:
        return _AuditTestResult()

    async def flush(self) -> None:
        return None

    def add(self, obj: object) -> None:
        self.added.append(obj)


def patch_isolated_audit_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """Route ``audited(...)``'s fresh-session write to the in-memory double.

    Patches ``audit_coverage._shared_session_factory`` (the lazy-imported
    process-shared engine) rather than the ``audit_session`` dependency, so it
    also covers tests that ``dependency_overrides.clear()`` mid-test - which
    would defeat a dependency override on ``audit_session`` itself.
    """
    monkeypatch.setattr(
        "modulo.core.audit_coverage._shared_session_factory",
        lambda: AuditSessionDouble,
    )
