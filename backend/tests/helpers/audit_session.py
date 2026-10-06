"""In-memory double for the ``audited(...)`` isolated audit session (FAR-1472).

The ``audited()`` route dependency opens a FRESH session from the process-shared
engine to append one chained audit event after the handler runs. Suites that
drive the API with a mocked request session (unit and BDD) must route that
fresh-session write to a double too, or it dials the real engine: on the unit
suite a dead port, and on the BDD SQLite suite a ``test.db`` that never ran the
audit migrations - where the ``fail_closed=True`` destruction routes then turn a
green route test red with ``no such table: audit_chain_heads``.

This is the SINGLE definition of that double; both ``tests/unit/conftest.py``
and ``tests/bdd/conftest.py`` install it via an autouse fixture. Integration
tests are deliberately NOT covered - they run against real Postgres and
exercise the genuine append.
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


class _AuditTestSession:
    """AsyncSession stand-in for the ``audited(...)`` isolated audit write.

    Mocked-session suites run without a database, so the route dependency's
    fresh-session append would otherwise dial a dead port on EVERY annotated
    mutating request - and, worse, RAISE on the ``fail_closed=True`` destruction
    routes, turning a green route test red for an environmental reason rather
    than a behavioural one. This double implements exactly the surface the
    append path touches: an active-transaction guard, a non-Postgres bind (so
    ``set_rls_*`` store into ``session.info`` rather than issuing ``set_config``),
    a chain-head read that returns no head, and ``add``/``flush``.

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


def install_audit_session_double(monkeypatch: pytest.MonkeyPatch) -> None:
    """Route the ``audited(...)`` fresh-session write to the in-memory double.

    ``audit_coverage.audit_session`` builds its session from
    ``_shared_session_factory`` (lazy-imported process-shared engine), so
    patching that factory covers every annotated route in the suite - including
    tests that ``dependency_overrides.clear()`` mid-test, which would defeat a
    dependency override on ``audit_session`` itself.
    """
    monkeypatch.setattr(
        "modulo.core.audit_coverage._shared_session_factory",
        lambda: _AuditTestSession,
    )
