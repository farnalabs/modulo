"""FAR-2153 — the system-scope licence is durable and reaches every process.

The defect these tests pin: a licence installed through ``/settings/license``
lived in process memory only. Every routine deploy (``strategy = 'rolling'``)
discarded it, and the SAQ worker — a separate process — could not see it at all,
so a paying customer silently dropped to Community.

The fix persists the two in-memory licence slots (active + parked successor)
in the RLS-free instance ``system_config`` store and restores/refreshes them.
The tests prove, against a real (SQLite) database:

1. an install survives a simulated restart;
2. a fresh worker process resolves the SAME tier as the API;
3. an install made while the worker is running becomes visible without a
   restart, within the documented refresh TTL;
4. removal deletes the durable row and falls back to the env floor / Community;
5. the documented precedence (persisted install > env floor > Community);
6. a parked successor is persisted alongside the active licence, not instead;
7. an unverifiable persisted licence with only the development anchor fails
   the boot closed (FAR-2091's rule, applied to a runtime install);
8. the refresh cannot destroy a directly-stored (non-persisted) licence.
"""

from __future__ import annotations

import time
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, Self

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

import modulo.core.feature_flags as feature_flags
import modulo.core.license as license_mod
from modulo.core.feature_flags import resolve_effective_license
from modulo.core.license import (
    SYSTEM_LICENSE_REFRESH_TTL_SECONDS,
    PersistedLicenseAnchorError,
    PersistedLicenseState,
    clear_license,
    clear_persisted_system_license,
    get_license,
    get_parked_successor,
    persist_installed_system_license,
    read_persisted_license_state,
    refresh_persisted_system_license,
    restore_persisted_system_license,
    set_public_key,
    store_license,
)
from modulo.core.license_signing import encode_license_key
from modulo.core.registry.crypto import generate_keypair
from modulo.db.models.base import Base
from modulo.db.models.system_config import SystemConfig

_TEST_KP = generate_keypair()
_PRIVATE_KEY = _TEST_KP["private_key"]
_PUBLIC_KEY = _TEST_KP["public_key"]


def _payload(
    *,
    expires_in_days: int = 365,
    starts_in_days: int | None = None,
    features: list[str] | None = None,
) -> dict[str, Any]:
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "tier": "team",
        "features": features if features is not None else ["sso", "audit_viewer"],
        "expires_at": (now + timedelta(days=expires_in_days)).isoformat(),
        "org_id": "instance-wide",
    }
    if starts_in_days is not None:
        payload["starts_at"] = (now + timedelta(days=starts_in_days)).isoformat()
    return payload


def _key(**kwargs: Any) -> str:
    return encode_license_key(_payload(**kwargs), _PRIVATE_KEY)


def _settings(*, env_key: str = "", debug: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        debug=debug,
        modulo_license_key=env_key,
        modulo_license_public_key=_PUBLIC_KEY,
    )


def _forget_process_state() -> None:
    """Simulate a FRESH process: empty memory and no observed persisted state."""
    license_mod._current_license = None
    license_mod._parked_successor = None
    license_mod._persisted_state = None
    license_mod._persisted_state_checked_at = 0.0


async def _install(session: AsyncSession, key: str, *, parked: bool = False) -> None:
    """Install through the same two steps the route uses: persist, then memory."""
    data = license_mod.verify_license_key(key)
    assert data is not None
    await persist_installed_system_license(session, key, parked=parked)
    store_license(key, data)


async def _write_state_directly(session: AsyncSession, state: PersistedLicenseState) -> None:
    """Write the store as ANOTHER process would (this process observes nothing)."""
    async with session.begin():
        await license_mod._write_persisted_license_state(session, state)


async def _read_state(session: AsyncSession) -> PersistedLicenseState:
    async with session.begin():
        return await read_persisted_license_state(session)


@pytest.fixture(autouse=True)
def _isolate_license_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the anchor and start every test with empty process state."""
    monkeypatch.setattr(license_mod, "_configured_public_key_hex", _PUBLIC_KEY)
    monkeypatch.setattr(license_mod, "_dev_anchor_trusted", False)
    monkeypatch.setattr(license_mod, "_current_license", None)
    monkeypatch.setattr(license_mod, "_parked_successor", None)
    monkeypatch.setattr(license_mod, "_persisted_state", None)
    monkeypatch.setattr(license_mod, "_persisted_state_checked_at", 0.0)
    set_public_key(_PUBLIC_KEY)


@pytest.fixture
async def engine() -> AsyncGenerator[AsyncEngine, None]:
    eng = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=[SystemConfig.__table__]))
    yield eng
    await eng.dispose()


@pytest.fixture
async def session(engine: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    maker = async_sessionmaker(engine, expire_on_commit=False, autobegin=False)
    async with maker() as db_session:
        yield db_session


# ── 1. An install survives a restart ────────────────────────────────────────


async def test_install_survives_a_restart(session: AsyncSession) -> None:
    key = _key()
    await _install(session, key)
    assert get_license() is not None

    _forget_process_state()
    assert get_license() is None  # the bug: memory-only

    async with session.begin():
        state = await restore_persisted_system_license(session, _settings())

    assert state.active_key == key
    restored = get_license()
    assert restored is not None
    assert restored.tier == "team"
    assert resolve_effective_license(_settings(), None) is not None
    assert resolve_effective_license(_settings(), None).tier == "team"


# ── 2. Worker context resolves the same tier as the API ─────────────────────


async def test_worker_context_resolves_the_same_tier_as_the_api(session: AsyncSession) -> None:
    key = _key()
    await _install(session, key)

    api_licence = resolve_effective_license(_settings(), None)
    assert api_licence is not None
    assert api_licence.tier == "team"

    # A worker process has its OWN memory: before the fix it saw nothing.
    _forget_process_state()
    assert resolve_effective_license(_settings(), None) is None

    async with session.begin():
        await restore_persisted_system_license(session, _settings())

    worker_licence = resolve_effective_license(_settings(), None)
    assert worker_licence is not None
    assert worker_licence.tier == api_licence.tier
    assert worker_licence.features == api_licence.features


# ── 3. Install while the worker is running, no restart ──────────────────────


async def test_install_while_running_becomes_visible_without_a_restart(session: AsyncSession) -> None:
    # The worker is running and has already observed an EMPTY store.
    license_mod._persisted_state = PersistedLicenseState()
    license_mod._persisted_state_checked_at = time.monotonic()

    # ANOTHER process installs.
    key = _key()
    await _write_state_directly(session, PersistedLicenseState(active_key=key))

    # Inside the TTL window the worker has not re-read yet: bounded latency,
    # not a missed change.
    assert await refresh_persisted_system_license(session) is False
    assert get_license() is None

    # Once the TTL has elapsed, the very next resolution sees it — no restart.
    license_mod._persisted_state_checked_at = time.monotonic() - SYSTEM_LICENSE_REFRESH_TTL_SECONDS - 1
    changed = await refresh_persisted_system_license(session)

    assert changed is True
    visible = get_license()
    assert visible is not None
    assert visible.tier == "team"
    assert visible.raw_key == key


# ── 4. Removal deletes the row and falls back to the env floor ──────────────


async def test_removal_deletes_the_persisted_licence_and_falls_back_to_env(session: AsyncSession) -> None:
    persisted_key = _key(features=["sso"])
    env_key = _key(features=["audit_viewer"])
    await _install(session, persisted_key)
    settings = _settings(env_key=env_key)

    assert resolve_effective_license(settings, None).raw_key == persisted_key  # type: ignore[union-attr]

    removed = await clear_persisted_system_license(session)
    clear_license()

    assert removed is True
    assert (await _read_state(session)).is_empty()
    fallback = resolve_effective_license(settings, None)
    assert fallback is not None
    assert fallback.raw_key == env_key


async def test_removal_without_an_env_key_falls_back_to_community(session: AsyncSession) -> None:
    await _install(session, _key())

    await clear_persisted_system_license(session)
    clear_license()

    assert (await _read_state(session)).is_empty()
    assert resolve_effective_license(_settings(), None) is None
    assert get_license() is None


# ── 5. Precedence: persisted install > env floor ────────────────────────────


async def test_persisted_install_overrides_the_env_floor(session: AsyncSession) -> None:
    env_key = _key(features=["sso"])
    persisted_key = _key(features=["audit_viewer"])
    await _install(session, persisted_key)

    resolved = resolve_effective_license(_settings(env_key=env_key), None)

    assert resolved is not None
    assert resolved.raw_key == persisted_key
    assert resolved.features == ["audit_viewer"]


# ── 6. A parked successor is persisted ALONGSIDE the active licence ─────────


async def test_parked_successor_survives_a_restart_without_displacing_the_active(session: AsyncSession) -> None:
    active_key = _key(expires_in_days=30)
    await _install(session, active_key)

    successor_key = _key(expires_in_days=400, starts_in_days=30)
    await _install(session, successor_key, parked=True)

    _forget_process_state()
    async with session.begin():
        state = await restore_persisted_system_license(session, _settings())

    assert state.active_key == active_key
    assert state.parked_key == successor_key

    active = get_license()
    parked = get_parked_successor()
    assert active is not None
    assert active.raw_key == active_key
    assert parked is not None
    assert parked.raw_key == successor_key


async def test_replacing_the_active_licence_preserves_a_parked_successor(session: AsyncSession) -> None:
    """The persisted state must equal what memory holds.

    ``store_license`` leaves the parked slot alone when an ACTIVE licence is
    installed, so the persisted state must keep that successor too; otherwise a
    restart would silently drop a renewal the customer has already bought.
    """
    await _install(session, _key(expires_in_days=30))
    successor_key = _key(expires_in_days=400, starts_in_days=30)
    await _install(session, successor_key, parked=True)

    replacement_key = _key(expires_in_days=90)
    await _install(session, replacement_key)

    state = await _read_state(session)
    assert state.active_key == replacement_key
    assert state.parked_key == successor_key


# ── 7. An unverifiable persisted licence fails the boot closed ──────────────


async def test_persisted_licence_without_an_anchor_fails_closed(session: AsyncSession) -> None:
    stranger = generate_keypair()
    key = encode_license_key(_payload(), stranger["private_key"])
    await _write_state_directly(session, PersistedLicenseState(active_key=key))

    set_public_key(license_mod._BUILTIN_DEV_PUBLIC_KEY_HEX)
    license_mod._configured_public_key_hex = None

    with pytest.raises(PersistedLicenseAnchorError):
        async with session.begin():
            await restore_persisted_system_license(session, _settings(debug=False))


async def test_debug_process_with_a_persisted_licence_recovers_the_dev_anchor_path(session: AsyncSession) -> None:
    # In debug the development anchor is a legitimate trust anchor, so a key
    # signed by the development keypair restores normally.
    dev_kp = generate_keypair()
    key = encode_license_key(_payload(), dev_kp["private_key"])
    await _write_state_directly(session, PersistedLicenseState(active_key=key))

    set_public_key(dev_kp["public_key"])
    license_mod._dev_anchor_trusted = True

    async with session.begin():
        state = await restore_persisted_system_license(session, _settings(debug=True))

    assert state.active_key == key
    assert get_license() is not None


# ── 8. A directly-stored licence is not destroyed by an empty store ─────────


async def test_refresh_does_not_clear_a_directly_stored_licence(session: AsyncSession) -> None:
    key = _key()
    data = license_mod.verify_license_key(key)
    assert data is not None
    store_license(key, data)  # deliberately NOT persisted

    changed = await refresh_persisted_system_license(session, force=True)

    assert changed is False
    assert get_license() is not None
    assert get_license().raw_key == key  # type: ignore[union-attr]


# ── 9. Plan resolution refreshes before walking the ladder ──────────────────


async def test_plan_resolution_refreshes_the_persisted_licence_first(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[bool] = []

    async def _record_refresh(_session: Any, *, force: bool = False) -> bool:
        calls.append(force)
        return False

    key = _key()
    data = license_mod.verify_license_key(key)
    assert data is not None
    sentinel = object()

    async def _from_db(*_args: Any, **_kwargs: Any) -> object:
        return sentinel

    monkeypatch.setattr(license_mod, "refresh_persisted_system_license", _record_refresh)
    monkeypatch.setattr(feature_flags, "resolve_effective_license", lambda _settings, _org: data)
    monkeypatch.setattr(feature_flags.DbPlanContext, "from_db", staticmethod(_from_db))

    plan = await feature_flags._resolve_paid_plan(_settings(), session, None)

    assert plan is sentinel
    assert calls == [False]


# ── 10. The worker's per-job hook refreshes the licence ─────────────────────


async def test_worker_job_start_refreshes_the_persisted_licence(monkeypatch: pytest.MonkeyPatch) -> None:
    """The worker resolves a tier INSIDE a job, so the per-job hook is the
    cross-process visibility point (no long-lived background task needed)."""
    import modulo.core.saq_worker as saq_worker

    refresh_calls: list[bool] = []

    async def _record_refresh(_session: Any, *, force: bool = False) -> bool:
        refresh_calls.append(force)
        return False

    class _SessionDouble:
        def begin(self) -> Self:
            return self

        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *_exc: object) -> bool:
            return False

    monkeypatch.setattr(license_mod, "persisted_license_refresh_due", lambda: True)
    monkeypatch.setattr(license_mod, "refresh_persisted_system_license", _record_refresh)
    monkeypatch.setattr("modulo.db.session.AsyncSessionLocal", lambda: _SessionDouble())

    await saq_worker._before_process_hook({"job": SimpleNamespace(kwargs={"org_id": "org-1"})})

    assert refresh_calls == [False]
