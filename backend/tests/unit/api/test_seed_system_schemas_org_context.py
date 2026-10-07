"""FAR-1546 — per-org isolation + ERROR attribution for ``_seed_system_schemas``.

``modulo.api.main._seed_system_schemas`` looped every organisation with NO
per-org ``try/except`` inside ONE shared transaction: the first org's failure
poisoned that transaction for every org after it, and the exception bubbled to
``_boot_seed``'s ORG-LESS ``startup.seed_failed`` ERROR — which
``ErrorTrackingLogHandler`` announces and drops as ``no_org_context``.

These tests drive the REAL entry point — real ``_seed_system_schemas``, real
``seed_system_schemas``, the real ``_bound_org`` bind, a real
``ErrorTrackingLogHandler`` — and never set ``org_id_var`` themselves: the org
on each forwarded record comes solely from the production bind.

Session I/O runs against a real in-memory SQLite engine carrying only the five
tables this seeder touches. A SQLite trigger aborts the ``schemas`` INSERT for
ORG1 ONLY (scoped by an org id bound as a parameter), so that org's seed fails
for real while ORG2's succeeds — exactly the isolation + attribution pair under
test. The two org-less phases (enumeration, and the failure it reports through
``_boot_seed``) are pinned to keep the announced ``no_org_context`` drop.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncGenerator, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

import modulo.api.main as main
from modulo.core.logging_config import ErrorTrackingLogHandler, org_id_var
from modulo.db.models.account import Account
from modulo.db.models.base import Base
from modulo.db.models.organisation import Organisation
from modulo.db.models.schema import Schema, SchemaVersion
from modulo.db.seed import SYSTEM_SCHEMAS
from tests.unit.api.test_main_coverage_gaps import _make_settings

MAIN_LOGGER = "modulo.api.main"

ORG1 = uuid.UUID("00000000-0000-0000-0000-0000000000e1")
ORG2 = uuid.UUID("00000000-0000-0000-0000-0000000000e2")
ADMIN_ACCOUNT = uuid.UUID("00000000-0000-0000-0000-0000000000e3")

# ORG1 is deliberately the OLDEST org so it is enumerated FIRST: the isolation
# claim under test is "the first org's real failure does not stop the rest".
_ORG1_CREATED = datetime(2026, 1, 1, tzinfo=UTC)
_ORG2_CREATED = datetime(2026, 1, 2, tzinfo=UTC)

# Only the tables this seeder touches — other models use Postgres-only column
# types (e.g. ARRAY) that SQLite cannot render.
_TABLES = {"accounts", "organisations", "schema_folders", "schemas", "schema_versions"}


class _RecordingSink:
    """Stand-in for ``ErrorTrackingLogHandler._async_emit``.

    Records each forwarded record AND the ``org_id_var`` value visible at
    forward time — the value the real ingest would have used to attribute the
    row. Plain instance (not a function), so ``self._async_emit(record)``
    resolves to ``sink(record)`` without binding the handler as ``self``.
    """

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
    """Hermetic SQLite engine whose ``schemas`` INSERT fails for ORG1 only.

    The failing org id is bound as a QUERY PARAMETER (never interpolated into
    the SQL), and both sides of the trigger's comparison are normalised with
    ``replace()``: SQLAlchemy renders ``Uuid()`` as a 32-char hex string on
    SQLite while the bound parameter carries the dashed form.
    """
    eng = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with eng.begin() as conn:
        tables = [t for t in Base.metadata.sorted_tables if t.name in _TABLES]
        await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=tables))
        await conn.exec_driver_sql("CREATE TABLE far1546_failing_orgs (organisation_id TEXT NOT NULL)")
        await conn.exec_driver_sql(
            "INSERT INTO far1546_failing_orgs (organisation_id) VALUES (?)",
            (str(ORG1),),
        )
        await conn.exec_driver_sql(
            "CREATE TRIGGER far1546_seed_failure BEFORE INSERT ON schemas "
            "BEGIN "
            "SELECT RAISE(ABORT, 'FAR-1546 injected per-org seed failure') "
            "WHERE replace(NEW.organisation_id, '-', '') IN "
            "(SELECT replace(organisation_id, '-', '') FROM far1546_failing_orgs); "
            "END"
        )
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
def _attach(sink: _RecordingSink) -> Iterator[None]:
    """Attach a REAL ``ErrorTrackingLogHandler`` to ``modulo.api.main``.

    Scoped to the seeder's own logger so records from other loggers (the
    SQLAlchemy engine errors on the deliberately missing tables) can neither be
    mistaken for the seeded ERROR nor consume the handler's per-org rate-limit
    budget.
    """
    handler = ErrorTrackingLogHandler()
    logger = logging.getLogger(MAIN_LOGGER)
    logger.addHandler(handler)
    try:
        with patch.object(ErrorTrackingLogHandler, "_async_emit", new=sink):
            yield
    finally:
        logger.removeHandler(handler)


@pytest.fixture
def capture_main(sink: _RecordingSink) -> Iterator[None]:
    with _attach(sink):
        yield


def _patch_seams(
    monkeypatch: pytest.MonkeyPatch,
    engine: AsyncEngine,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Point the seeder's engine/factory seams at this test's SQLite engine.

    ``_seed_system_schemas`` resolves both through a function-local import from
    ``modulo.api.dependencies``, so patching the module attributes is what the
    real call site sees.
    """
    monkeypatch.setattr("modulo.api.dependencies.get_or_create_engine", lambda *_a, **_kw: engine)
    monkeypatch.setattr("modulo.api.dependencies.get_or_create_session_factory", lambda *_a, **_kw: factory)


async def _seed_orgs_and_admin(factory: async_sessionmaker[AsyncSession]) -> None:
    async with factory() as session, session.begin():
        session.add(Organisation(id=ORG1, name="Org One", slug="org-one", created_at=_ORG1_CREATED))
        session.add(Organisation(id=ORG2, name="Org Two", slug="org-two", created_at=_ORG2_CREATED))
        session.add(Account(id=ADMIN_ACCOUNT, email="admin", display_name="Admin", auth_provider="local"))


async def _seeded_rows(
    factory: async_sessionmaker[AsyncSession],
    org_id: uuid.UUID,
) -> tuple[list[uuid.UUID], list[uuid.UUID]]:
    """(schema ids, schema-version ids) committed for *org_id*."""
    async with factory() as session, session.begin():
        schemas = (await session.execute(select(Schema.id).where(Schema.organisation_id == org_id))).scalars().all()
        versions = (
            (await session.execute(select(SchemaVersion.id).where(SchemaVersion.organisation_id == org_id)))
            .scalars()
            .all()
        )
    return list(schemas), list(versions)


async def _drain() -> None:
    """Let the handler's forwarding task run before the sink is read."""
    for _ in range(5):
        await asyncio.sleep(0)


async def test_per_org_failure_is_isolated_and_attributed(
    monkeypatch: pytest.MonkeyPatch,
    engine: AsyncEngine,
    factory: async_sessionmaker[AsyncSession],
    capture_main: None,
    sink: _RecordingSink,
) -> None:
    """ORG1's real seed failure does not stop ORG2, and its ERROR is attributed.

    Before FAR-1546 the whole loop shared ONE transaction with no per-org
    ``try/except``: ORG1 is the first org, so its trigger-raised failure
    escapes ``_seed_system_schemas`` (this test errors), ORG2 never seeds, and
    no ``system_schemas.seed_org_failed`` record exists to forward. After the
    change both halves hold: ORG2 carries every system schema, ORG1 carries
    none (its own transaction rolled back), and the single forwarded ERROR
    carries ORG1's id — from the production bind alone, never a hand-set
    ``org_id_var``.
    """
    assert org_id_var.get() is None
    await _seed_orgs_and_admin(factory)
    _patch_seams(monkeypatch, engine, factory)

    await main._seed_system_schemas(_make_settings())
    await _drain()

    org1_schemas, org1_versions = await _seeded_rows(factory, ORG1)
    org2_schemas, org2_versions = await _seeded_rows(factory, ORG2)
    assert not org1_schemas
    assert not org1_versions
    assert len(org2_schemas) == len(SYSTEM_SCHEMAS)
    assert len(org2_versions) == len(SYSTEM_SCHEMAS)

    attributed = sink.orgs_for("system_schemas.seed_org_failed")
    assert attributed == [str(ORG1)]
    assert org_id_var.get() is None


async def test_org_enumeration_failure_stays_unbound(
    monkeypatch: pytest.MonkeyPatch,
    engine: AsyncEngine,
    factory: async_sessionmaker[AsyncSession],
    capture_main: None,
    sink: _RecordingSink,
) -> None:
    """The org enumeration runs BEFORE any per-org tick — there is no org to bind.

    With ``organisations`` dropped, the org-less phase itself fails and the
    exception escapes ``_seed_system_schemas`` (``_boot_seed`` reports it), so
    no per-org record exists to forward and the caller context stays clean.
    Pinning that the FAR-1546/FAR-1539 bind covers only the per-org loop body.
    """
    assert org_id_var.get() is None
    _patch_seams(monkeypatch, engine, factory)
    async with engine.begin() as conn:
        await conn.execute(text("DROP TABLE organisations"))

    with pytest.raises(OperationalError):
        await main._seed_system_schemas(_make_settings())
    await _drain()

    assert not sink.orgs_for("system_schemas.seed_org_failed")
    assert org_id_var.get() is None


async def test_boot_seed_failure_keeps_the_announced_drop(
    monkeypatch: pytest.MonkeyPatch,
    engine: AsyncEngine,
    factory: async_sessionmaker[AsyncSession],
    capture_main: None,
    sink: _RecordingSink,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An org-less failure reported through ``_boot_seed`` keeps the drop.

    ``startup.seed_failed`` is logged with no organisation to attribute, so the
    handler must announce ``no_org_context`` rather than forward the record —
    the org-less phases of this seeder are deliberately unbound (FAR-1546).
    """
    assert org_id_var.get() is None
    _patch_seams(monkeypatch, engine, factory)
    async with engine.begin() as conn:
        await conn.execute(text("DROP TABLE organisations"))

    await main._boot_seed("system_schemas", main._seed_system_schemas(_make_settings()))
    await _drain()

    assert not sink.orgs_for("system_schemas.seed_org_failed")
    assert any("no_org_context" in record.getMessage() for record in caplog.records)
    assert org_id_var.get() is None
