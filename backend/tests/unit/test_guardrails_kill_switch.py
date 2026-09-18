"""FAR-223 item 9 — guardrails kill-switch admin endpoint + enable alert.

Direct-handler tests (no TestClient/DI): the route toggles the org flag,
stamps ``guardrails_kill_switch_at``, writes a ``guardrails_kill_switch``
audit event, and fires the paging Notification on enable. The enable alert is
asserted via a monkeypatched ``notify_guardrail_event`` (the real one lazy-
imports the shared engine, unavailable in unit tests).
"""

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from sqlalchemy import Table, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from modulo.api.routes.admin_orgs import (
    SetOrgGuardrailsKillSwitchRequest,
    admin_get_org_guardrails_kill_switch,
    admin_set_org_guardrails_kill_switch,
)
from modulo.core import guardrails as guardrails_module
from modulo.db.models.audit_event import AuditChainHead, AuditEvent
from modulo.db.models.base import Base
from modulo.db.models.organisation import Organisation

_ORG = uuid.UUID("00000000-0000-0000-0000-000000000001")

_TABLES: list[Table] = cast(
    list[Table],
    [Organisation.__table__, AuditEvent.__table__, AuditChainHead.__table__],
)


@pytest.fixture
async def engine() -> AsyncGenerator[AsyncEngine, None]:
    eng = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=_TABLES))
        await conn.exec_driver_sql("PRAGMA foreign_keys = OFF")
    yield eng
    await eng.dispose()


@pytest.fixture
async def session(engine: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    # autobegin=False matches the app DI default: the route's
    # ``async with session.begin():`` owns transaction start.
    maker = async_sessionmaker(engine, expire_on_commit=False, autobegin=False)
    async with maker() as s:
        yield s


class _Principal:
    user_id = uuid.UUID("00000000-0000-0000-0000-0000000000d1")


async def _seed_org(session: AsyncSession, *, kill_switch: bool = False) -> None:
    async with session.begin():
        session.add(
            Organisation(
                id=_ORG,
                name="test org",
                slug="test-org",
                guardrails_kill_switch=kill_switch,
                guardrails_kill_switch_at=datetime.now(UTC) if kill_switch else None,
            )
        )
        await session.flush()


async def test_set_kill_switch_enable_audits_and_alerts(session: AsyncSession, monkeypatch: pytest.MonkeyPatch):
    await _seed_org(session)
    notified: list[dict[str, Any]] = []

    async def _fake_notify(org_id: uuid.UUID, event_type: str, payload: dict[str, Any], **kwargs: Any) -> None:
        notified.append({"org_id": org_id, "event_type": event_type, "payload": payload})

    monkeypatch.setattr(guardrails_module, "notify_guardrail_event", _fake_notify)

    resp = await admin_set_org_guardrails_kill_switch(
        _ORG,
        SetOrgGuardrailsKillSwitchRequest(enabled=True),
        _Principal(),
        session,
    )
    assert resp.enabled is True
    assert resp.enabled_at is not None

    # The flag + timestamp persisted.
    async with session.begin():
        org = (await session.execute(select(Organisation).where(Organisation.id == _ORG))).scalar_one()
        assert org.guardrails_kill_switch is True
        assert org.guardrails_kill_switch_at is not None

        # Audit event written.
        audit = (
            (await session.execute(select(AuditEvent).where(AuditEvent.event_type == "guardrails_kill_switch")))
            .scalars()
            .all()
        )
        assert len(audit) == 1
        assert audit[0].payload_json["enabled"] is True

    # Enable alert fired.
    assert len(notified) == 1
    assert notified[0]["event_type"] == "guardrail_kill_switch"
    assert notified[0]["payload"]["enabled"] is True


async def test_set_kill_switch_disable_no_alert(session: AsyncSession, monkeypatch: pytest.MonkeyPatch):
    await _seed_org(session, kill_switch=True)
    notified: list[dict[str, Any]] = []

    async def _fake_notify(org_id: uuid.UUID, event_type: str, payload: dict[str, Any], **kwargs: Any) -> None:
        notified.append({"org_id": org_id, "event_type": event_type, "payload": payload})

    monkeypatch.setattr(guardrails_module, "notify_guardrail_event", _fake_notify)

    resp = await admin_set_org_guardrails_kill_switch(
        _ORG,
        SetOrgGuardrailsKillSwitchRequest(enabled=False),
        _Principal(),
        session,
    )
    assert resp.enabled is False
    async with session.begin():
        org = (await session.execute(select(Organisation).where(Organisation.id == _ORG))).scalar_one()
        assert org.guardrails_kill_switch is False
        assert org.guardrails_kill_switch_at is None
    # Disabling restores enforcement — no downgrade alert.
    assert notified == []


async def test_set_kill_switch_idempotent_noop(session: AsyncSession, monkeypatch: pytest.MonkeyPatch):
    await _seed_org(session, kill_switch=True)
    notified: list[dict[str, Any]] = []

    async def _fake_notify(org_id: uuid.UUID, event_type: str, payload: dict[str, Any], **kwargs: Any) -> None:
        notified.append({"org_id": org_id, "event_type": event_type, "payload": payload})

    monkeypatch.setattr(guardrails_module, "notify_guardrail_event", _fake_notify)

    resp = await admin_set_org_guardrails_kill_switch(
        _ORG,
        SetOrgGuardrailsKillSwitchRequest(enabled=True),
        _Principal(),
        session,
    )
    assert resp.enabled is True
    # No-op: no new audit write, no re-alert.
    async with session.begin():
        audit = (await session.execute(select(AuditEvent))).scalars().all()
        assert audit == []
    assert notified == []


async def test_get_kill_switch_returns_state(session: AsyncSession):
    await _seed_org(session, kill_switch=True)
    resp = await admin_get_org_guardrails_kill_switch(_ORG, _Principal(), session)
    assert resp.enabled is True
    assert resp.enabled_at is not None
