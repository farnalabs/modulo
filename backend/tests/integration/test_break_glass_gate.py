"""Deploy-gate CI regression tests for break-glass deliverable (B) acceptance (b).

The CI regression gate = the boot-time allow-list assertion + the connectivity
smoke that fails boot when ENABLED AND boot-failure-mode=fail. Tagged
``breakglass_gate`` and run by the ``break-glass-deploy-gate`` CI job against
testcontainers Postgres with the PRODUCTION ``bootstrap_role.py`` (the
session-scoped conftest provisions modulo_app / modulo_breakglass / modulo_migrate
and runs bootstrap BEFORE and AFTER alembic upgrade heads).

The boot-gate contract under test (break-glass plan §3, "Deploy gate = acceptance
(b)"):
* connectivity probe (SELECT 1) is TRANSIENT — retried N=3 with backoff
  1s->2s->4s, then boot fails (or proceeds) per boot-failure-mode;
* the allow-list + role assertions are FATAL — the DB is reachable but an
  assertion fails => FAIL BOOT immediately (never retried).

The assertions are the PRODUCTION bootstrap_role functions
(``_find_allow_list_violations`` / ``_assert_role_posture``), exercised in BOTH
directions: a clean config passes, a rogue grant FAILS.
"""

import asyncio
from collections.abc import Awaitable, Callable

import asyncpg  # type: ignore[import-untyped]
import pytest

from modulo.db.bootstrap_role import (
    ACCOUNTS_WRITABLE_COLUMNS,
    _apply_accounts_allow_list,
    _assert_role_posture,
    _find_allow_list_violations,
)

pytestmark = pytest.mark.breakglass_gate

# N=3 retries with backoff 1s->2s->4s (plan §3 — the connectivity probe is
# transient; the tuple is pinned so a later edit cannot silently change the
# retry profile the CI gate asserts).
_BOOT_BACKOFF_SECONDS: tuple[float, ...] = (1.0, 2.0, 4.0)

Probe = Callable[[], Awaitable[None]]
Violations = Callable[[], Awaitable[list[str]]]


class _BootGateError(RuntimeError):
    """The boot gate failed and the process must not start."""


async def _boot_gate(
    probe: Probe,
    allow_list_violations: Violations,
    *,
    mode: str = "fail",
    backoffs: tuple[float, ...] = _BOOT_BACKOFF_SECONDS,
) -> int:
    """The boot gate contract: transient connectivity probe, then fatal assertions.

    Phase 1 — connectivity probe (SELECT 1): TRANSIENT. Retried N=3 with backoff
    ``backoffs`` (pinned 1s->2s->4s); on exhaustion boot fails when
    ``mode='fail'`` or proceeds when ``mode='warn'``.
    Phase 2 — allow-list + role assertions: FATAL. The DB is reachable but the
    assertion fails => boot fails immediately, never retried.
    """
    attempts = 0
    last_error: BaseException | None = None
    for attempt in range(len(backoffs) + 1):
        attempts = attempt + 1
        try:
            await probe()
            break
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            last_error = exc
            if attempt < len(backoffs):
                await asyncio.sleep(backoffs[attempt])
    else:
        if mode == "fail":
            raise _BootGateError(
                f"connectivity probe failed after {attempts} attempts (backoff {backoffs}): {last_error}"
            ) from last_error

    violations = await allow_list_violations()
    if violations:
        raise _BootGateError("allow-list assertion failed (fatal): " + "; ".join(map(str, violations)))
    return attempts


async def _pg_connect(migrated_db_url: str) -> asyncpg.Connection:
    url = migrated_db_url.replace("postgresql+asyncpg://", "postgres://").split("?")[0]
    return await asyncpg.connect(url, ssl=False)


# ── allow-list assertion: BOTH directions ──────────────────────────────


async def test_clean_boot_passes_allow_list_assertion(migrated_db_url: str) -> None:
    """A clean post-bootstrap config passes every boot-time assertion."""
    conn = await _pg_connect(migrated_db_url)
    try:
        assert await _find_allow_list_violations(conn, "modulo_app") == []
        await _assert_role_posture(conn, "modulo_app")  # must not raise
    finally:
        await conn.close()


async def test_rogue_table_level_grant_fails_boot(migrated_db_url: str) -> None:
    """Positive control: a rogue table-level UPDATE grant on accounts fails boot.

    A table-level ``REVOKE`` also wipes the column-level allow-list grants in
    Postgres, so the restore re-applies the allow-list exactly as bootstrap does
    on every boot (``_apply_accounts_allow_list``) — the same self-heal path the
    gate is protecting.
    """
    conn = await _pg_connect(migrated_db_url)
    try:
        assert await _find_allow_list_violations(conn, "modulo_app") == []
        await conn.execute("GRANT UPDATE ON public.accounts TO modulo_app")
        try:
            violations = await _find_allow_list_violations(conn, "modulo_app")
            assert any("table-level" in v and "modulo_app" in v for v in violations), violations
            with pytest.raises(RuntimeError, match="Break-glass role posture assertion FAILED"):
                await _assert_role_posture(conn, "modulo_app")
        finally:
            await conn.execute("REVOKE UPDATE ON public.accounts FROM modulo_app")
            await _apply_accounts_allow_list(conn, "modulo_app")
        assert await _find_allow_list_violations(conn, "modulo_app") == []
    finally:
        await conn.close()


async def test_rogue_rolsuper_and_privileged_membership_fail_boot(migrated_db_url: str) -> None:
    """Positive control: a superuser app role and privileged-role membership fail boot."""
    conn = await _pg_connect(migrated_db_url)
    try:
        await conn.execute("ALTER ROLE modulo_app SUPERUSER")
        try:
            violations = await _find_allow_list_violations(conn, "modulo_app")
            assert any("superuser" in v for v in violations), violations
        finally:
            await conn.execute("ALTER ROLE modulo_app NOSUPERUSER")

        await conn.execute("GRANT modulo_migrate TO modulo_app")
        try:
            violations = await _find_allow_list_violations(conn, "modulo_app")
            assert any("member of" in v for v in violations), violations
        finally:
            await conn.execute("REVOKE modulo_migrate FROM modulo_app")

        assert await _find_allow_list_violations(conn, "modulo_app") == []
    finally:
        await conn.close()


async def test_allow_list_constant_is_the_ten_writable_columns() -> None:
    """Schema-evolution tripwire: the allow-list constant is exactly the 10 columns."""
    assert tuple(sorted(ACCOUNTS_WRITABLE_COLUMNS)) == (
        "active",
        "auth_provider",
        "display_name",
        "email",
        "is_system_admin",
        "last_login",
        "password_hash",
        "preferences",
        "sso_subject",
        "updated_at",
    )


# ── connectivity smoke: transient retry + exhaustion per mode ──────────


async def test_connectivity_smoke_retries_then_succeeds() -> None:
    """A transient probe failure is retried (not fatal) and boot proceeds."""
    calls: list[int] = []

    async def probe() -> None:
        calls.append(1)
        if len(calls) < 3:
            raise ConnectionError("transient connectivity failure")

    attempts = await _boot_gate(probe, _no_violations, backoffs=(0.0, 0.0, 0.0))
    assert len(calls) == 3
    assert attempts == 3


async def test_connectivity_exhaustion_respects_boot_failure_mode() -> None:
    """Persistent probe failure fails boot after N=3 retries in fail mode, not warn."""
    calls: list[int] = []

    async def probe() -> None:
        calls.append(1)
        raise ConnectionError("db unreachable")

    with pytest.raises(_BootGateError, match="connectivity probe failed after 4 attempts"):
        await _boot_gate(probe, _no_violations, backoffs=(0.0, 0.0, 0.0), mode="fail")
    assert len(calls) == 4  # initial probe + N=3 retries

    warn_calls: list[int] = []

    async def warn_probe() -> None:
        warn_calls.append(1)
        raise ConnectionError("db unreachable")

    attempts = await _boot_gate(warn_probe, _no_violations, backoffs=(0.0, 0.0, 0.0), mode="warn")
    assert len(warn_calls) == 4
    assert attempts == 4


async def test_pinned_backoff_is_1_2_4() -> None:
    """The boot-gate backoff profile is pinned to 1s->2s->4s."""
    assert _BOOT_BACKOFF_SECONDS == (1.0, 2.0, 4.0)


# ── transient-vs-fatal discriminator ───────────────────────────────────


async def test_transient_vs_fatal_discriminator() -> None:
    """Probe failure is transient (retried); assertion failure is fatal (not retried)."""
    probe_calls: list[int] = []

    async def probe() -> None:
        probe_calls.append(1)
        if len(probe_calls) == 1:
            raise ConnectionError("transient")

    await _boot_gate(probe, _no_violations, backoffs=(0.0, 0.0, 0.0))
    assert len(probe_calls) == 2  # retried once

    assertion_calls: list[int] = []

    async def violations() -> list[str]:
        assertion_calls.append(1)
        return ["accounts has a table-level UPDATE grant for: modulo_app"]

    async def ok_probe() -> None:
        return None

    with pytest.raises(_BootGateError, match="allow-list assertion failed"):
        await _boot_gate(ok_probe, violations, backoffs=(0.0, 0.0, 0.0))
    assert len(assertion_calls) == 1  # fatal — evaluated exactly once, never retried


# ── the gate against the real migrated DB ──────────────────────────────


async def test_real_select_one_probe_passes_boot(migrated_db_url: str) -> None:
    """The SELECT-1 connectivity probe against the real migrated DB succeeds."""
    conn = await _pg_connect(migrated_db_url)
    try:

        async def probe() -> None:
            value = await conn.fetchval("SELECT 1")
            if value != 1:
                raise ConnectionError(f"SELECT 1 returned {value!r}")

        attempts = await _boot_gate(probe, _no_violations, backoffs=(0.0, 0.0, 0.0))
        assert attempts == 1
    finally:
        await conn.close()


async def test_db_reachable_but_allow_list_violation_fails_boot(migrated_db_url: str) -> None:
    """DB reachable but the allow-list assertion fails => FAIL BOOT immediately."""
    conn = await _pg_connect(migrated_db_url)
    try:

        async def probe() -> None:
            value = await conn.fetchval("SELECT 1")
            if value != 1:
                raise ConnectionError(f"SELECT 1 returned {value!r}")

        async def violations() -> list[str]:
            return await _find_allow_list_violations(conn, "modulo_app")

        await conn.execute("GRANT UPDATE ON public.accounts TO PUBLIC")
        try:
            with pytest.raises(_BootGateError, match="allow-list assertion failed"):
                await _boot_gate(probe, violations, backoffs=(0.0, 0.0, 0.0))
        finally:
            await conn.execute("REVOKE UPDATE ON public.accounts FROM PUBLIC")
    finally:
        await conn.close()


async def _no_violations() -> list[str]:
    return []
