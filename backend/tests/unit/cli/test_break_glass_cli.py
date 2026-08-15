"""Unit tests for the modulo-break-glass CLI wrapper.

Covers the operator-secret matrix, TTY/--yes credential-delivery branches,
the exhaustive exit-code table (0-9), never-on-rollback credential printing,
and the SECURITY DEFINER pgcode mapping (M2010/M2020/M2040).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Self
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner
from sqlalchemy.exc import SQLAlchemyError

from modulo.cli.break_glass import (
    EXIT_ACTIVATION_TXN_FAILURE,
    EXIT_CREDENTIAL_PRINT_FAILURE,
    EXIT_DEACTIVATE_ATOMICITY_FAILURE,
    EXIT_DEACTIVATE_REFUSED,
    EXIT_ORG_NOT_FOUND,
    EXIT_PRECONDITIONS,
    EXIT_SMOKE_FAILURE,
    EXIT_UNEXPECTED,
    EXIT_USAGE,
    CredentialPrintError,
    DeactivateAtomicityError,
    DeactivateRefusedError,
    OrgNotFoundError,
    PreconditionError,
    _confirm_interactive,
    _deliver_credential,
    _sqlstate_from_exc,
    authenticate_operator,
    cli,
    deactivate,
)
from modulo.settings import Settings

_NOW = datetime.now(UTC)
_ORG = SimpleNamespace(
    id=uuid.UUID("00000000-0000-0000-0000-0000000000aa"),
    name="Acme Corp",
    slug="acme",
)


def _make_settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "database_url": "postgresql+asyncpg://modulo:modulo@localhost:5432/modulo",
        "secret_key": "a" * 32,
        "fernet_key": "a" * 32,
        "modulo_admin_password": "test",
        "redis_url": "",
        "modulo_break_glass_secret": "p" * 32,
        "modulo_break_glass_standby_secret": "s" * 32,
        "modulo_break_glass_database_url": "postgresql+asyncpg://modulo_breakglass:bgpass@localhost:5432/modulo",
    }
    base.update(overrides)
    return Settings(**base)


class _FakeTxn:
    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None


class _FakeSession:
    def __init__(self) -> None:
        self._txn = _FakeTxn()

    def begin(self) -> _FakeTxn:
        return self._txn

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None


class _FakeFactory:
    def __init__(self) -> None:
        self.session = _FakeSession()

    def __call__(self) -> _FakeSession:
        return self.session


def _invoke(args: list[str], settings: Settings, *, actor: str = "operator") -> object:
    runner = CliRunner()
    factory = _FakeFactory()
    return runner.invoke(cli, args, obj={"settings": settings, "actor": actor, "session_factory": factory})


class _FakeBgRow:
    def __init__(
        self,
        *,
        expires_at: datetime | None = None,
        deactivated_at: datetime | None = None,
        password_hash: str | None = "$2b$12$somebcrypthash",
    ) -> None:
        self.id = uuid.uuid4()
        self.is_break_glass = True
        self.break_glass_expires_at = expires_at
        self.break_glass_deactivated_at = deactivated_at
        self.password_hash = password_hash


def _dbapi_error(sqlstate: str) -> SQLAlchemyError:
    orig = SimpleNamespace(sqlstate=sqlstate)
    exc = SQLAlchemyError("boom")
    exc.orig = orig
    return exc


# ── Operator secret matrix ───────────────────────────────────────────────────


class TestAuthenticateOperator:
    def test_primary_secret_matches(self) -> None:
        settings = _make_settings()
        assert authenticate_operator("p" * 32, settings) == "operator"

    def test_standby_secret_matches(self) -> None:
        settings = _make_settings()
        assert authenticate_operator("s" * 32, settings) == "operator-standby"

    def test_mismatch_raises_precondition(self) -> None:
        settings = _make_settings()
        with pytest.raises(PreconditionError):
            authenticate_operator("wrong-secret-0123456789", settings)

    def test_empty_raises_precondition(self) -> None:
        settings = _make_settings()
        with pytest.raises(PreconditionError):
            authenticate_operator(None, settings)
        with pytest.raises(PreconditionError):
            authenticate_operator("   ", settings)

    def test_bad_secret_via_cli_exits_5(self) -> None:
        settings = _make_settings()
        runner = CliRunner()
        result = runner.invoke(cli, ["smoke", "--secret", "wrong-secret-0123456789"], obj={"settings": settings})
        assert result.exit_code == EXIT_PRECONDITIONS

    def test_missing_secret_via_cli_exits_5(self) -> None:
        settings = _make_settings()
        runner = CliRunner()
        result = runner.invoke(cli, ["smoke"], obj={"settings": settings})
        assert result.exit_code == EXIT_PRECONDITIONS


# ── activate ─────────────────────────────────────────────────────────────────


class TestActivate:
    def test_success_yes_prints_credential_once(self, caplog: pytest.LogCaptureFixture) -> None:
        settings = _make_settings()
        with (
            patch("modulo.cli.break_glass._resolve_org", AsyncMock(return_value=_ORG)),
            patch("modulo.cli.break_glass._db_now", AsyncMock(return_value=_NOW)),
            patch("modulo.cli.break_glass._bg_accounts_for_org", AsyncMock(return_value=[])),
            patch("modulo.cli.break_glass.activate", AsyncMock(return_value="cred-single-use")) as core,
        ):
            result = _invoke(
                ["activate", "acme", "--reason", "TKT-1", "--ttl-minutes", "60", "--yes"],
                settings,
            )
        assert result.exit_code == 0
        assert result.output.count("cred-single-use") == 1
        core.assert_awaited_once()
        assert "cred-single-use" not in caplog.text

    def test_missing_reason_exits_2(self) -> None:
        settings = _make_settings()
        result = _invoke(["activate", "acme", "--yes"], settings)
        assert result.exit_code == EXIT_USAGE

    def test_empty_reason_exits_2(self) -> None:
        settings = _make_settings()
        result = _invoke(["activate", "acme", "--reason", "  ", "--yes"], settings)
        assert result.exit_code == EXIT_USAGE

    def test_org_not_found_exits_3(self) -> None:
        settings = _make_settings()
        with patch("modulo.cli.break_glass._resolve_org", AsyncMock(return_value=None)):
            result = _invoke(["activate", "nope", "--reason", "TKT-1", "--yes"], settings)
        assert result.exit_code == EXIT_ORG_NOT_FOUND

    def test_activation_txn_failure_exits_4(self) -> None:
        settings = _make_settings()
        with (
            patch("modulo.cli.break_glass._resolve_org", AsyncMock(return_value=_ORG)),
            patch("modulo.cli.break_glass._db_now", AsyncMock(return_value=_NOW)),
            patch("modulo.cli.break_glass._bg_accounts_for_org", AsyncMock(return_value=[])),
            patch("modulo.cli.break_glass.activate", AsyncMock(side_effect=RuntimeError("db exploded"))),
        ):
            result = _invoke(["activate", "acme", "--reason", "TKT-1", "--yes"], settings)
        assert result.exit_code == EXIT_ACTIVATION_TXN_FAILURE

    def test_never_prints_credential_on_rollback(self) -> None:
        settings = _make_settings()
        with (
            patch("modulo.cli.break_glass._resolve_org", AsyncMock(return_value=_ORG)),
            patch("modulo.cli.break_glass._db_now", AsyncMock(return_value=_NOW)),
            patch("modulo.cli.break_glass._bg_accounts_for_org", AsyncMock(return_value=[])),
            patch("modulo.cli.break_glass.activate", AsyncMock(side_effect=RuntimeError("commit failed"))),
        ):
            result = _invoke(["activate", "acme", "--reason", "TKT-1", "--yes"], settings)
        assert result.exit_code == EXIT_ACTIVATION_TXN_FAILURE
        assert "cred" not in result.output

    def test_ttl_out_of_range_exits_5(self) -> None:
        settings = _make_settings(
            modulo_break_glass_ttl_minutes=60,
            modulo_break_glass_max_ttl_minutes=120,
        )
        result = _invoke(["activate", "acme", "--reason", "TKT-1", "--ttl-minutes", "500", "--yes"], settings)
        assert result.exit_code == EXIT_PRECONDITIONS

    def test_ttl_below_one_exits_5(self) -> None:
        settings = _make_settings()
        result = _invoke(["activate", "acme", "--reason", "TKT-1", "--ttl-minutes", "0", "--yes"], settings)
        assert result.exit_code == EXIT_PRECONDITIONS

    def test_non_tty_without_yes_exits_5(self) -> None:
        settings = _make_settings()
        with (
            patch("modulo.cli.break_glass._resolve_org", AsyncMock(return_value=_ORG)),
            patch("modulo.cli.break_glass._db_now", AsyncMock(return_value=_NOW)),
            patch("modulo.cli.break_glass._bg_accounts_for_org", AsyncMock(return_value=[])),
        ):
            result = _invoke(["activate", "acme", "--reason", "TKT-1"], settings)
        assert result.exit_code == EXIT_PRECONDITIONS
        assert "Target organisation" in result.output

    def test_tty_confirm_true_proceeds(self) -> None:
        settings = _make_settings()
        with (
            patch("modulo.cli.break_glass._resolve_org", AsyncMock(return_value=_ORG)),
            patch("modulo.cli.break_glass._db_now", AsyncMock(return_value=_NOW)),
            patch("modulo.cli.break_glass._bg_accounts_for_org", AsyncMock(return_value=[])),
            patch("modulo.cli.break_glass.activate", AsyncMock(return_value="cred-tty")),
            patch("modulo.cli.break_glass._confirm_interactive", return_value=True),
        ):
            result = _invoke(["activate", "acme", "--reason", "TKT-1"], settings)
        assert result.exit_code == 0
        assert "cred-tty" in result.output

    def test_dry_run_prints_plan_without_activating(self) -> None:
        settings = _make_settings()
        with (
            patch("modulo.cli.break_glass._resolve_org", AsyncMock(return_value=_ORG)),
            patch("modulo.cli.break_glass._db_now", AsyncMock(return_value=_NOW)),
            patch("modulo.cli.break_glass._bg_accounts_for_org", AsyncMock(return_value=[])),
            patch("modulo.cli.break_glass.activate", AsyncMock()) as core,
        ):
            result = _invoke(
                ["activate", "acme", "--reason", "TKT-1", "--ttl-minutes", "60", "--dry-run", "--yes"],
                settings,
            )
        assert result.exit_code == 0
        assert "[dry-run]" in result.output
        core.assert_not_awaited()

    def test_warns_when_live_row_exists(self) -> None:
        settings = _make_settings()
        live_row = _FakeBgRow(expires_at=_NOW + timedelta(minutes=30))
        with (
            patch("modulo.cli.break_glass._resolve_org", AsyncMock(return_value=_ORG)),
            patch("modulo.cli.break_glass._db_now", AsyncMock(return_value=_NOW)),
            patch("modulo.cli.break_glass._bg_accounts_for_org", AsyncMock(return_value=[live_row])),
            patch("modulo.cli.break_glass.activate", AsyncMock(return_value="cred-live-warn")),
        ):
            result = _invoke(["activate", "acme", "--reason", "TKT-1", "--yes"], settings)
        assert result.exit_code == 0
        assert "already exists" in result.output

    def test_credential_print_failure_exits_9(self) -> None:
        settings = _make_settings()
        with (
            patch("modulo.cli.break_glass._resolve_org", AsyncMock(return_value=_ORG)),
            patch("modulo.cli.break_glass._db_now", AsyncMock(return_value=_NOW)),
            patch("modulo.cli.break_glass._bg_accounts_for_org", AsyncMock(return_value=[])),
            patch("modulo.cli.break_glass.activate", AsyncMock(return_value="cred-print-fail")),
            patch(
                "modulo.cli.break_glass._deliver_credential",
                side_effect=CredentialPrintError("stdout closed"),
            ),
        ):
            result = _invoke(["activate", "acme", "--reason", "TKT-1", "--yes"], settings)
        assert result.exit_code == EXIT_CREDENTIAL_PRINT_FAILURE

    def test_deliver_credential_raises_on_write_failure(self) -> None:
        class _FailingStdout:
            def write(self, _data: str) -> int:
                raise OSError("broken pipe")

            def flush(self) -> None:
                return None

        with patch("modulo.cli.break_glass.sys.stdout", _FailingStdout()), pytest.raises(CredentialPrintError):
            _deliver_credential("cred", yes=True)


# ── deactivate ───────────────────────────────────────────────────────────────


class TestDeactivate:
    def test_success_exits_0(self) -> None:
        settings = _make_settings()
        with (
            patch("modulo.cli.break_glass._resolve_org", AsyncMock(return_value=_ORG)),
            patch("modulo.cli.break_glass._db_now", AsyncMock(return_value=_NOW)),
            patch(
                "modulo.cli.break_glass.deactivate",
                AsyncMock(return_value={"deactivated": 1, "account_ids": ["acc-1"]}),
            ) as core,
        ):
            result = _invoke(["deactivate", "acme", "--reason", "TKT-1", "--force"], settings)
        assert result.exit_code == 0
        assert "deactivated 1" in result.output
        core.assert_awaited_once()

    def test_refused_exits_6(self) -> None:
        settings = _make_settings()
        with (
            patch("modulo.cli.break_glass._resolve_org", AsyncMock(return_value=_ORG)),
            patch("modulo.cli.break_glass._db_now", AsyncMock(return_value=_NOW)),
            patch(
                "modulo.cli.break_glass.deactivate",
                AsyncMock(side_effect=DeactivateRefusedError("a live break-glass activation exists")),
            ),
        ):
            result = _invoke(["deactivate", "acme", "--reason", "TKT-1"], settings)
        assert result.exit_code == EXIT_DEACTIVATE_REFUSED

    def test_org_not_found_exits_3(self) -> None:
        settings = _make_settings()
        with patch("modulo.cli.break_glass._resolve_org", AsyncMock(return_value=None)):
            result = _invoke(["deactivate", "nope", "--reason", "TKT-1", "--force"], settings)
        assert result.exit_code == EXIT_ORG_NOT_FOUND

    def test_atomicity_failure_exits_8(self) -> None:
        settings = _make_settings()
        with (
            patch("modulo.cli.break_glass._resolve_org", AsyncMock(return_value=_ORG)),
            patch("modulo.cli.break_glass._db_now", AsyncMock(return_value=_NOW)),
            patch(
                "modulo.cli.break_glass.deactivate",
                AsyncMock(side_effect=DeactivateAtomicityError("deactivation transaction failed")),
            ),
        ):
            result = _invoke(["deactivate", "acme", "--reason", "TKT-1", "--force"], settings)
        assert result.exit_code == EXIT_DEACTIVATE_ATOMICITY_FAILURE

    def test_invalid_account_id_exits_2(self) -> None:
        settings = _make_settings()
        result = _invoke(["deactivate", "acme", "--reason", "TKT-1", "--account-id", "not-a-uuid"], settings)
        assert result.exit_code == EXIT_USAGE


# ── force-last-admin ─────────────────────────────────────────────────────────


class TestForceLastAdmin:
    def test_success_exits_0(self) -> None:
        settings = _make_settings()
        with (
            patch("modulo.cli.break_glass._resolve_org", AsyncMock(return_value=_ORG)),
            patch("modulo.cli.break_glass._db_now", AsyncMock(return_value=_NOW)),
            patch(
                "modulo.cli.break_glass.force_last_admin",
                AsyncMock(return_value={"removed_account_id": "acc-final"}),
            ) as core,
        ):
            result = _invoke(["force-last-admin", "acme", "--reason", "TKT-9"], settings)
        assert result.exit_code == 0
        assert "acc-final" in result.output
        core.assert_awaited_once()

    def test_refuses_bg_only_org_exits_5(self) -> None:
        settings = _make_settings()
        with (
            patch("modulo.cli.break_glass._resolve_org", AsyncMock(return_value=_ORG)),
            patch("modulo.cli.break_glass._db_now", AsyncMock(return_value=_NOW)),
            patch(
                "modulo.cli.break_glass.force_last_admin",
                AsyncMock(side_effect=PreconditionError("refusing to remove a live break-glass account")),
            ),
        ):
            result = _invoke(["force-last-admin", "acme", "--reason", "TKT-9"], settings)
        assert result.exit_code == EXIT_PRECONDITIONS

    def test_missing_reason_exits_2(self) -> None:
        settings = _make_settings()
        result = _invoke(["force-last-admin", "acme"], settings)
        assert result.exit_code == EXIT_USAGE


# ── status ───────────────────────────────────────────────────────────────────


class TestStatus:
    def test_empty_exits_0(self) -> None:
        settings = _make_settings()
        with (
            patch("modulo.cli.break_glass._resolve_org", AsyncMock(return_value=_ORG)),
            patch("modulo.cli.break_glass._db_now", AsyncMock(return_value=_NOW)),
            patch("modulo.cli.break_glass.status_rows", AsyncMock(return_value=[])),
        ):
            result = _invoke(["status", "acme"], settings)
        assert result.exit_code == 0
        assert "no break-glass rows" in result.output

    def test_lists_rows(self) -> None:
        settings = _make_settings()
        row = {
            "org_slug": "acme",
            "state": "live",
            "account_id": "acc-1",
            "expires_at": _NOW + timedelta(minutes=60),
            "reason": "TKT-1",
            "actor": "operator",
        }
        with (
            patch("modulo.cli.break_glass._resolve_org", AsyncMock(return_value=_ORG)),
            patch("modulo.cli.break_glass._db_now", AsyncMock(return_value=_NOW)),
            patch("modulo.cli.break_glass.status_rows", AsyncMock(return_value=[row])),
        ):
            result = _invoke(["status", "acme"], settings)
        assert result.exit_code == 0
        assert "acme" in result.output

    def test_json_output(self) -> None:
        settings = _make_settings()
        row = {
            "org_slug": "acme",
            "state": "expired",
            "account_id": "acc-1",
            "expires_at": None,
            "reason": "TKT-1",
            "actor": "operator",
        }
        with (
            patch("modulo.cli.break_glass._resolve_org", AsyncMock(return_value=_ORG)),
            patch("modulo.cli.break_glass._db_now", AsyncMock(return_value=_NOW)),
            patch("modulo.cli.break_glass.status_rows", AsyncMock(return_value=[row])),
        ):
            result = _invoke(["status", "acme", "--json"], settings)
        assert result.exit_code == 0
        assert '"state": "expired"' in result.output

    def test_org_not_found_exits_3(self) -> None:
        settings = _make_settings()
        with patch("modulo.cli.break_glass._resolve_org", AsyncMock(return_value=None)):
            result = _invoke(["status", "nope"], settings)
        assert result.exit_code == EXIT_ORG_NOT_FOUND

    def test_no_org_no_all_exits_2(self) -> None:
        settings = _make_settings()
        result = _invoke(["status"], settings)
        assert result.exit_code == EXIT_USAGE

    def test_sweep_live_row_exits_5(self) -> None:
        settings = _make_settings()
        row = {
            "org_slug": "acme",
            "state": "live",
            "account_id": "acc-1",
            "expires_at": _NOW + timedelta(minutes=60),
            "reason": "TKT-1",
            "actor": "operator",
        }
        with (
            patch("modulo.cli.break_glass._db_now", AsyncMock(return_value=_NOW)),
            patch("modulo.cli.break_glass.status_rows", AsyncMock(return_value=[row])),
        ):
            result = _invoke(["status", "--all", "--json"], settings)
        assert result.exit_code == EXIT_PRECONDITIONS
        assert '"state": "live"' in result.output

    def test_sweep_clean_exits_0(self) -> None:
        settings = _make_settings()
        with (
            patch("modulo.cli.break_glass._db_now", AsyncMock(return_value=_NOW)),
            patch("modulo.cli.break_glass.status_rows", AsyncMock(return_value=[])),
        ):
            result = _invoke(["status", "--all", "--json"], settings)
        assert result.exit_code == 0

    def test_unexpected_error_exits_1(self) -> None:
        settings = _make_settings()
        with (
            patch("modulo.cli.break_glass._resolve_org", AsyncMock(return_value=_ORG)),
            patch("modulo.cli.break_glass._db_now", AsyncMock(return_value=_NOW)),
            patch("modulo.cli.break_glass.status_rows", AsyncMock(side_effect=RuntimeError("boom"))),
        ):
            result = _invoke(["status", "acme"], settings)
        assert result.exit_code == EXIT_UNEXPECTED


# ── smoke ────────────────────────────────────────────────────────────────────


class TestSmoke:
    def test_success_exits_0(self) -> None:
        settings = _make_settings()
        with patch(
            "modulo.cli.break_glass.smoke",
            AsyncMock(return_value={"connectivity": "ok", "session_user": "modulo_breakglass"}),
        ) as core:
            result = _invoke(["smoke"], settings)
        assert result.exit_code == 0
        core.assert_awaited_once()

    def test_failure_exits_7(self) -> None:
        settings = _make_settings()
        with patch(
            "modulo.cli.break_glass.smoke",
            AsyncMock(side_effect=Exception("connection refused")),
        ):
            result = _invoke(["smoke"], settings)
        assert result.exit_code == EXIT_SMOKE_FAILURE


# ── SECURITY DEFINER pgcode mapping (core deactivate) ───────────────────────


class TestCoreDeactivatePgcode:
    @pytest.fixture(autouse=True)
    def _patch_target_rows(self) -> object:
        live_row = _FakeBgRow(expires_at=_NOW + timedelta(minutes=30))
        with patch("modulo.cli.break_glass._bg_accounts_for_org", AsyncMock(return_value=[live_row])):
            yield None

    def _failing_session(self, sqlstate: str) -> object:
        session = MagicMock()
        session.begin.return_value = _FakeTxn()
        session.execute = AsyncMock(side_effect=_dbapi_error(sqlstate))
        return session

    async def test_m2040_maps_to_org_not_found(self) -> None:
        with pytest.raises(OrgNotFoundError):
            await deactivate(
                self._failing_session("M2040"),
                org_id=_ORG.id,
                account_id=None,
                actor="operator",
                reason="TKT-1",
                force=True,
                now=_NOW,
            )

    async def test_m2020_maps_to_deactivate_refused(self) -> None:
        with pytest.raises(DeactivateRefusedError):
            await deactivate(
                self._failing_session("M2020"),
                org_id=_ORG.id,
                account_id=None,
                actor="operator",
                reason="TKT-1",
                force=True,
                now=_NOW,
            )

    async def test_m2010_maps_to_deactivate_refused(self) -> None:
        with pytest.raises(DeactivateRefusedError):
            await deactivate(
                self._failing_session("M2010"),
                org_id=_ORG.id,
                account_id=None,
                actor="operator",
                reason="TKT-1",
                force=True,
                now=_NOW,
            )

    async def test_generic_sqlalchemy_error_maps_to_atomicity(self) -> None:
        with pytest.raises(DeactivateAtomicityError):
            await deactivate(
                self._failing_session("23505"),
                org_id=_ORG.id,
                account_id=None,
                actor="operator",
                reason="TKT-1",
                force=True,
                now=_NOW,
            )

    async def test_live_activation_refused_without_force(self) -> None:
        session = MagicMock()
        with pytest.raises(DeactivateRefusedError):
            await deactivate(
                session,
                org_id=_ORG.id,
                account_id=None,
                actor="operator",
                reason="TKT-1",
                force=False,
                now=_NOW,
            )

    async def test_no_targets_raises_org_not_found(self) -> None:
        session = MagicMock()
        with (
            patch("modulo.cli.break_glass._bg_accounts_for_org", AsyncMock(return_value=[])),
            pytest.raises(OrgNotFoundError),
        ):
            await deactivate(
                session,
                org_id=_ORG.id,
                account_id=None,
                actor="operator",
                reason="TKT-1",
                force=True,
                now=_NOW,
            )


# ── helpers ──────────────────────────────────────────────────────────────────


class TestHelpers:
    def test_sqlstate_extraction_unwraps_orig(self) -> None:
        assert _sqlstate_from_exc(_dbapi_error("M2040")) == "M2040"

    def test_sqlstate_none_when_absent(self) -> None:
        assert _sqlstate_from_exc(RuntimeError("no state")) is None

    def test_confirm_interactive_false_when_not_tty(self) -> None:
        with patch("modulo.cli.break_glass.sys.stdin", SimpleNamespace(isatty=lambda: False)):
            assert _confirm_interactive(_ORG) is False

    def test_confirm_interactive_true_when_confirmed(self) -> None:
        with (
            patch("modulo.cli.break_glass.sys.stdin", SimpleNamespace(isatty=lambda: True)),
            patch("modulo.cli.break_glass.click.confirm", return_value=True),
        ):
            assert _confirm_interactive(_ORG) is True

    def test_confirm_interactive_false_when_declined(self) -> None:
        with (
            patch("modulo.cli.break_glass.sys.stdin", SimpleNamespace(isatty=lambda: True)),
            patch("modulo.cli.break_glass.click.confirm", return_value=False),
        ):
            assert _confirm_interactive(_ORG) is False
