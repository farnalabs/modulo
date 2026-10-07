"""FAR-1539 — per-org ERROR attribution for the boot-time seeders.

``ErrorTrackingLogHandler`` persists an ERROR record only when ``org_id_var``
is bound at log time; FAR-1501 fixed that across the cron/sweep family, but the
BOOT seeders reached from ``api.main`` sit outside it, so their per-org ERRORs
were announced-dropped as ``no_org_context``. These tests drive the REAL entry
points (real ``_bound_org`` bind, real ``ErrorTrackingLogHandler``) and never
set ``org_id_var`` themselves — the org on each forwarded record comes solely
from the production bind.

Session I/O runs against a real in-memory SQLite engine carrying ONLY the
``organisations`` table: the org enumeration succeeds and each per-org
cost-component seed then fails for real (``no such table: cost_components``),
which is exactly the ERROR the bind must attribute. The demo-org seeder's
failure is likewise real — no signing key configured raises
``LicenseSigningError`` before any DB write; only the settings lookup is
stubbed. Each test also asserts the caller context is clean afterwards.
"""

from __future__ import annotations

import asyncio
import logging
import types
import uuid
from collections.abc import AsyncGenerator, Iterator
from contextlib import contextmanager
from unittest.mock import patch

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from modulo.core.logging_config import ErrorTrackingLogHandler, org_id_var
from modulo.core.seed_data import demo_data as demo_mod
from modulo.core.seed_data.cost_components import seed_cost_components
from modulo.core.seed_data.demo_data import DemoOrgSpec, seed_demo_orgs
from modulo.db.models.base import Base
from modulo.db.models.organisation import Organisation

COST_LOGGER = "modulo.core.seed_data.cost_components"
DEMO_LOGGER = "modulo.core.seed_data.demo_data"

ORG1 = uuid.UUID("00000000-0000-0000-0000-0000000000f1")
ORG2 = uuid.UUID("00000000-0000-0000-0000-0000000000f2")
ORG_DEMO = uuid.UUID("00000000-0000-0000-0000-0000000000f3")

_DEMO_SLUG = "far-1539-demo"
_DEMO_SPEC: DemoOrgSpec = {
    "slug": _DEMO_SLUG,
    "tier": "community",
    "full": False,
    "email": "demo@example.com",
    "password": "pw",
}

# Deliberately NO ``cost_components`` table: the per-org seed must fail for
# real so the ERROR the bind has to attribute actually fires.
_TABLES = {"organisations"}


class _RecordingSink:
    """Stand-in for ``ErrorTrackingLogHandler._async_emit``."""

    def __init__(self) -> None:
        self.records: list[logging.LogRecord] = []
        self.orgs: list[str | None] = []

    async def __call__(self, record: logging.LogRecord) -> None:
        self.records.append(record)
        self.orgs.append(org_id_var.get())

    @property
    def messages(self) -> list[str]:
        return [record.getMessage() for record in self.records]

    def orgs_for(self, substring: str) -> list[str | None]:
        """Orgs on the forwarded records whose message contains *substring*."""
        pairs = zip(self.messages, self.orgs, strict=True)
        return [org for message, org in pairs if substring in message]


@pytest.fixture(autouse=True)
def _clean_rate_limit_state() -> Iterator[None]:
    """The handler forwards at most one record per org per 5s window."""
    ErrorTrackingLogHandler._last_write_time.clear()
    yield
    ErrorTrackingLogHandler._last_write_time.clear()


@pytest.fixture
async def engine() -> AsyncGenerator[AsyncEngine, None]:
    eng = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with eng.begin() as conn:
        tables = [t for t in Base.metadata.sorted_tables if t.name in _TABLES]
        await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=tables))
    yield eng
    await eng.dispose()


@pytest.fixture
def factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    # Production shape: expire_on_commit=False + autobegin=False (see
    # modulo.api.dependencies.get_or_create_session_factory).
    return async_sessionmaker(engine, expire_on_commit=False, autobegin=False)


@pytest.fixture
def sink() -> _RecordingSink:
    return _RecordingSink()


@contextmanager
def _attach(logger_name: str, sink: _RecordingSink) -> Iterator[None]:
    """Attach a REAL ``ErrorTrackingLogHandler`` to *logger_name*.

    Scoped to the seeder's own logger so records from other loggers (SQLAlchemy
    engine errors on the deliberately missing table) can neither be mistaken
    for the seeded ERROR nor consume the handler's per-org rate-limit budget.
    """
    handler = ErrorTrackingLogHandler()
    logger = logging.getLogger(logger_name)
    logger.addHandler(handler)
    try:
        with patch.object(ErrorTrackingLogHandler, "_async_emit", new=sink):
            yield
    finally:
        logger.removeHandler(handler)


@pytest.fixture
def capture_cost(sink: _RecordingSink) -> Iterator[None]:
    with _attach(COST_LOGGER, sink):
        yield


@pytest.fixture
def capture_demo(sink: _RecordingSink) -> Iterator[None]:
    with _attach(DEMO_LOGGER, sink):
        yield


async def _drain() -> None:
    """Let the handler's forwarding task run before the sink is read."""
    for _ in range(5):
        await asyncio.sleep(0)


async def test_cost_components_seed_failures_are_attributed_to_their_own_orgs(
    factory: async_sessionmaker[AsyncSession],
    capture_cost: None,
    sink: _RecordingSink,
) -> None:
    """``seed_cost_components`` attributes each per-org seed failure to its org.

    Two orgs; the ``cost_components`` table is absent, so the real per-org seed
    raises for every org and ``cost_components.seed_org_failed`` fires once per
    org inside the real bound tick. This test never touches ``org_id_var``: the
    two distinct orgs on the forwarded records prove both attribution AND that
    the second tick did not inherit the first org's context. Before FAR-1539
    both records are dropped as ``no_org_context``, so the assertions on
    ``sink.orgs_for`` fail.
    """
    assert org_id_var.get() is None
    async with factory() as session, session.begin():
        session.add(Organisation(id=ORG1, name="Org One", slug="org-one"))
        session.add(Organisation(id=ORG2, name="Org Two", slug="org-two"))

    seeded = await seed_cost_components(factory)
    await _drain()

    assert seeded == 0
    attributed = sink.orgs_for("cost_components.seed_org_failed")
    assert len(attributed) == 2
    assert set(attributed) == {str(ORG1), str(ORG2)}
    assert org_id_var.get() is None


async def test_cost_components_enumeration_failure_stays_unbound(
    engine: AsyncEngine,
    factory: async_sessionmaker[AsyncSession],
    capture_cost: None,
    sink: _RecordingSink,
) -> None:
    """The org enumeration runs BEFORE any org tick — no org exists to bind.

    With ``organisations`` dropped, the enumeration itself fails and the
    exception escapes the seeder (``_boot_seed`` reports it); no per-org record
    exists to forward. Pinning that the FAR-1539 bind covers only the per-org
    loop body, and that the caller context stays clean either way.
    """
    assert org_id_var.get() is None
    async with engine.begin() as conn:
        await conn.execute(text("DROP TABLE organisations"))

    with pytest.raises(OperationalError):
        await seed_cost_components(factory)
    await _drain()

    assert not sink.orgs_for("cost_components.seed_org_failed")
    assert org_id_var.get() is None


async def test_demo_org_seed_failure_is_attributed_when_the_org_row_survives(
    factory: async_sessionmaker[AsyncSession],
    capture_demo: None,
    sink: _RecordingSink,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``seed_demo_orgs`` attributes ``demo_org.seed_failed`` to the demo org.

    The org row pre-exists (idempotent re-run), so after the per-spec
    transaction rolls back the resolver still finds it and the ERROR is
    forwarded with that org. No hand-set ``org_id_var``: the org on the record
    comes solely from the production bind.
    """
    assert org_id_var.get() is None
    async with factory() as session, session.begin():
        session.add(Organisation(id=ORG_DEMO, name="Demo Org", slug=_DEMO_SLUG))

    monkeypatch.setattr(demo_mod, "DEMO_ORGS", [_DEMO_SPEC])
    # Real failure path: no signing key -> LicenseSigningError BEFORE any DB write.
    monkeypatch.setattr(demo_mod, "get_settings", lambda: types.SimpleNamespace(modulo_license_private_key=""))

    await seed_demo_orgs(factory)
    await _drain()

    attributed = sink.orgs_for("demo_org.seed_failed")
    assert attributed == [str(ORG_DEMO)]
    assert org_id_var.get() is None


async def test_demo_org_seed_failure_keeps_the_announced_drop_when_no_org_row_survives(
    factory: async_sessionmaker[AsyncSession],
    capture_demo: None,
    sink: _RecordingSink,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No surviving organisation means no binding — never fabricate one.

    First-run failure: the spec's transaction rolled back its own insert, so
    there is no row for ``error_events.organisation_id`` to reference. The
    record must keep the announced ``no_org_context`` drop instead of being
    attributed to an id the FK would reject.
    """
    assert org_id_var.get() is None
    monkeypatch.setattr(demo_mod, "DEMO_ORGS", [_DEMO_SPEC])
    monkeypatch.setattr(demo_mod, "get_settings", lambda: types.SimpleNamespace(modulo_license_private_key=""))

    await seed_demo_orgs(factory)
    await _drain()

    assert not sink.orgs_for("demo_org.seed_failed")
    assert any("no_org_context" in record.getMessage() for record in caplog.records)
    assert org_id_var.get() is None


async def test_demo_org_seed_failure_with_no_slug_skips_attribution(
    factory: async_sessionmaker[AsyncSession],
    capture_demo: None,
    sink: _RecordingSink,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A spec with a falsy slug has no org to resolve — attribute nothing.

    ``seed_demo_orgs`` reads ``spec.get("slug")`` and, once the per-spec seed
    fails, asks the resolver to find a surviving row. With a falsy slug there
    is nothing to look up, so the resolver short-circuits to ``None`` and the
    caller keeps the announced drop. Pins that the empty-slug guard is a real
    path, not dead defensive code.
    """
    assert org_id_var.get() is None
    no_slug_spec: DemoOrgSpec = {
        "slug": "",
        "tier": "community",
        "full": False,
        "email": "demo@example.com",
        "password": "pw",
    }
    monkeypatch.setattr(demo_mod, "DEMO_ORGS", [no_slug_spec])
    monkeypatch.setattr(demo_mod, "get_settings", lambda: types.SimpleNamespace(modulo_license_private_key=""))

    await seed_demo_orgs(factory)
    await _drain()

    assert not sink.orgs_for("demo_org.seed_failed")
    assert org_id_var.get() is None


async def test_demo_org_resolution_failure_degrades_to_no_attribution(
    engine: AsyncEngine,
    factory: async_sessionmaker[AsyncSession],
    capture_demo: None,
    sink: _RecordingSink,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A resolver failure logs and degrades to ``None`` — never masks the seed error.

    The org lookup itself fails (``organisations`` dropped), so the resolver's
    ``except`` must swallow the DB error with a warning and return ``None``;
    the caller then keeps the announced drop. A resolver that propagated here
    would replace the per-spec seed ERROR with an unrelated lookup ERROR.
    """
    assert org_id_var.get() is None
    async with engine.begin() as conn:
        await conn.execute(text("DROP TABLE organisations"))

    monkeypatch.setattr(demo_mod, "DEMO_ORGS", [_DEMO_SPEC])
    monkeypatch.setattr(demo_mod, "get_settings", lambda: types.SimpleNamespace(modulo_license_private_key=""))

    await seed_demo_orgs(factory)
    await _drain()

    assert any("demo_org.failure_org_resolution_failed" in record.getMessage() for record in caplog.records)
    assert not sink.orgs_for("demo_org.seed_failed")
    assert org_id_var.get() is None
