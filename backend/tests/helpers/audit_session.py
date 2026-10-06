"""In-memory double for the ``audited(...)`` isolated audit write (FAR-1472).

``modulo.core.audit_coverage.audited`` opens a FRESH session from the
process-shared engine (``_shared_session_factory``) to append one audit event
after a mutating route handler runs. Suites that mock the request session
(unit, BDD) never mocked that second session, so every annotated mutating
request dialled the real engine — and, because the ``fail_closed=True``
destruction routes re-raise an append failure, a green route test went red for
an environmental reason (missing table / dead port) rather than a behavioural
one.

This double implements exactly the session surface the isolated append path
touches: an active-transaction guard, a non-Postgres bind (so ``set_rls_*``
store into ``session.info`` rather than issuing ``set_config``), a chain-head
read that returns no head, and ``add``/``flush``. Pointing
``_shared_session_factory`` at it covers every annotated route in a suite —
including tests that clear ``app.dependency_overrides`` mid-test, which would
defeat a dependency override on ``audit_session`` itself.

Integration tests run against real Postgres and must NOT use this double: they
exercise the genuine append.
"""

from types import SimpleNamespace
from typing import Self

import pytest

#: Bind object whose dialect is NOT postgresql, so ``set_rls_org`` /
#: ``set_rls_user_context`` take their generic ``session.info`` branch instead
#: of issuing ``set_config`` statements.
AUDIT_TEST_BIND = SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))


class AuditTestResult:
    """Result stand-in for the chain-head ``SELECT ... FOR UPDATE`` read."""

    def scalar_one_or_none(self) -> None:
        """No persisted chain head - the append creates the org's first event."""
        return


class AuditTestTransaction:
    """Async context manager backing ``session.begin()`` / ``begin_nested()``."""

    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_exc: object) -> bool:
        return False


class AuditTestSession:
    """AsyncSession stand-in for the ``audited(...)`` isolated audit write.

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
        return AUDIT_TEST_BIND

    def begin(self) -> AuditTestTransaction:
        return AuditTestTransaction()

    def begin_nested(self) -> AuditTestTransaction:
        return AuditTestTransaction()

    async def execute(self, *_args: object, **_kwargs: object) -> AuditTestResult:
        return AuditTestResult()

    async def flush(self) -> None:
        return None

    def add(self, obj: object) -> None:
        self.added.append(obj)


def patch_audit_session_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point ``audit_coverage``'s fresh audit session at :class:`AuditTestSession`."""
    monkeypatch.setattr(
        "modulo.core.audit_coverage._shared_session_factory",
        lambda: AuditTestSession,
    )
