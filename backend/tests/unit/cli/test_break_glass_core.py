"""Unit tests for break-glass CLI core paths the wrapper tests mock out."""

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.exc import SQLAlchemyError

from modulo.cli.break_glass import (
    ActivationTxnError,
    DeactivateAtomicityError,
    DeactivateRefusedError,
    OrgNotFoundError,
    PreconditionError,
    SmokeFailureError,
    _bg_accounts_for_org,
    _db_now,
    _factory_from_ctx,
    _is_consumed,
    _is_live,
    _latest_activation_actor_reason,
    _live_non_bg_admins,
    _render_status,
    _resolve_org,
    _row_state,
    _settings_from_ctx,
    activate,
    deactivate,
    force_last_admin,
    smoke,
    status_rows,
)

_NOW = datetime.now(UTC)
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


def _make_settings() -> SimpleNamespace:
    return SimpleNamespace(
        modulo_break_glass_database_url="postgresql+asyncpg://bg:bg@localhost:5432/modulo",
        modulo_break_glass_secret="p" * 32,
        modulo_break_glass_standby_secret="s" * 32,
        modulo_break_glass_ttl_minutes=60,
        modulo_break_glass_max_ttl_minutes=120,
    )


def _txn() -> object:
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=None)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _mock_session() -> AsyncMock:
    session = AsyncMock()
    session.begin = MagicMock(return_value=_txn())
    session.flush = AsyncMock()
    return session


def _bg_row(**overrides: object) -> SimpleNamespace:
    base = {
        "id": uuid.UUID("00000000-0000-0000-0000-00000000000a"),
        "is_break_glass": True,
        "break_glass_deactivated_at": None,
        "break_glass_expires_at": _NOW + timedelta(minutes=30),
        "password_hash": "$2b$12$hash",
        "created_at": _NOW - timedelta(minutes=5),
        "last_login": None,
        "email": "bg@modulo.run",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class TestPredicateHelpers:
    def test_is_live_when_expiring_later(self) -> None:
        row = _bg_row()
        assert _is_live(row, _NOW) is True

    def test_is_live_false_when_expired_or_deactivated(self) -> None:
        expired = _bg_row(break_glass_expires_at=_NOW - timedelta(minutes=1))
        deactivated = _bg_row(break_glass_deactivated_at=_NOW)
        no_expiry = _bg_row(break_glass_expires_at=None)
        assert _is_live(expired, _NOW) is False
        assert _is_live(deactivated, _NOW) is False
        assert _is_live(no_expiry, _NOW) is False

    def test_row_state_matrix(self) -> None:
        assert _row_state(_bg_row(break_glass_deactivated_at=_NOW), _NOW) == "deactivated"
        assert _row_state(_bg_row(break_glass_expires_at=None), _NOW) == "expired"
        assert _row_state(_bg_row(break_glass_expires_at=_NOW - timedelta(hours=1)), _NOW) == "expired"
        assert _row_state(_bg_row(), _NOW) == "live"

    def test_is_consumed_when_hash_not_bcrypt(self) -> None:
        assert _is_consumed(_bg_row(password_hash="random-hash-88")) is True
        assert _is_consumed(_bg_row(password_hash=None)) is False
        assert _is_consumed(_bg_row()) is False


class TestResolveOrg:
    async def test_by_uuid_and_by_slug(self) -> None:
        session = _mock_session()
        org = SimpleNamespace(id=_ORG_ID, name="Acme", slug="acme")
        by_id = MagicMock(scalar_one_or_none=MagicMock(return_value=org))
        by_slug = MagicMock(scalar_one_or_none=MagicMock(return_value=None))
        session.execute = AsyncMock(side_effect=[by_id, by_slug])
        assert await _resolve_org(session, str(_ORG_ID)) is org
        assert await _resolve_org(session, "acme-org") is None
        assert session.execute.await_count == 2

    async def test_malformed_ref_uses_slug_lookup(self) -> None:
        session = _mock_session()
        org = SimpleNamespace(id=_ORG_ID, name="Acme", slug="acme")
        by_slug = MagicMock(scalar_one_or_none=MagicMock(return_value=org))
        session.execute = AsyncMock(return_value=by_slug)
        assert await _resolve_org(session, "not-a-uuid!!") is org


class TestDbNowAndScopes:
    async def test_db_now_returns_db_clock(self) -> None:
        session = _mock_session()
        session.execute = AsyncMock(return_value=MagicMock(scalar_one=MagicMock(return_value=_NOW)))
        assert await _db_now(session) == _NOW
        stmt = session.execute.call_args.args[0]
        assert str(stmt) == "SELECT current_timestamp"

    async def test_bg_accounts_only_undeactivated(self) -> None:
        session = _mock_session()
        rows = [_bg_row()]
        result = MagicMock()
        result.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=rows)))
        session.execute = AsyncMock(return_value=result)
        assert await _bg_accounts_for_org(session, _ORG_ID, only_undeactivated=True) == rows

    async def test_live_non_bg_admins(self) -> None:
        session = _mock_session()
        rows = [SimpleNamespace(id=uuid.uuid4())]
        result = MagicMock()
        result.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=rows)))
        session.execute = AsyncMock(return_value=result)
        assert await _live_non_bg_admins(session, _ORG_ID) == rows


class TestCoreActivate:
    async def test_returns_credential_once_committed(self) -> None:
        session = _mock_session()
        account_row = MagicMock()
        account_row.id = uuid.UUID("00000000-0000-0000-0000-00000000000b")
        result = MagicMock(scalar_one_or_none=MagicMock(return_value=None))
        session.execute = AsyncMock(return_value=result)
        with (
            patch("modulo.cli.break_glass.set_rls_org", AsyncMock()) as rls,
            patch("modulo.cli.break_glass.hash_password", MagicMock(return_value="$2b$h")) as hasher,
            patch("modulo.cli.break_glass.append_audit_event", AsyncMock()) as audit,
        ):
            credential = await activate(
                session,
                now=_NOW,
                org_id=_ORG_ID,
                ttl_minutes=60,
                actor="operator",
                reason="TKT-1",
            )
        assert credential
        rls.assert_awaited_once_with(session, _ORG_ID)
        hasher.assert_called_once_with(credential)
        audit.assert_awaited_once()
        assert audit.call_args.kwargs["event_type"] == "break_glass_activated"
        assert session.flush.await_count >= 2

    async def test_email_collision_retries_then_succeeds(self) -> None:
        session = _mock_session()
        taken = MagicMock(scalar_one_or_none=MagicMock(return_value=uuid.uuid4()))
        free = MagicMock(scalar_one_or_none=MagicMock(return_value=None))
        session.execute = AsyncMock(side_effect=[taken, taken, free])
        with (
            patch("modulo.cli.break_glass.set_rls_org", AsyncMock()),
            patch("modulo.cli.break_glass.hash_password", MagicMock(return_value="$2b$12$")),
            patch("modulo.cli.break_glass.append_audit_event", AsyncMock()),
        ):
            credential = await activate(session, now=_NOW, org_id=_ORG_ID, ttl_minutes=60, actor="op", reason="r")
        assert credential
        assert session.execute.await_count == 3

    async def test_all_email_attempts_taken_raises(self) -> None:
        session = _mock_session()
        taken = MagicMock(scalar_one_or_none=MagicMock(return_value=uuid.uuid4()))
        session.execute = AsyncMock(return_value=taken)
        with (
            patch("modulo.cli.break_glass.set_rls_org", AsyncMock()),
            pytest.raises(ActivationTxnError, match="after 5 attempts"),
        ):
            await activate(session, now=_NOW, org_id=_ORG_ID, ttl_minutes=60, actor="op", reason="r")


class TestCoreDeactivate:
    async def test_tombstones_each_target_and_audits(self) -> None:
        session = _mock_session()
        rows = [_bg_row(), _bg_row(id=uuid.UUID("00000000-0000-0000-0000-00000000000c"))]
        with (
            patch("modulo.cli.break_glass._bg_accounts_for_org", AsyncMock(return_value=rows)),
            patch("modulo.cli.break_glass.append_audit_event", AsyncMock(return_value=None)) as audit,
        ):
            result = await deactivate(
                session,
                org_id=_ORG_ID,
                account_id=None,
                actor="operator",
                reason="TKT-1",
                force=True,
                now=_NOW,
            )
        assert result["deactivated"] == 2
        assert len(result["account_ids"]) == 2
        assert session.execute.await_count == 2
        audit.assert_awaited_once()

    async def test_specific_account_filters_out_others(self) -> None:
        session = _mock_session()
        rows = [_bg_row(), _bg_row(id=uuid.UUID("00000000-0000-0000-0000-00000000000c"))]
        with (
            patch("modulo.cli.break_glass._bg_accounts_for_org", AsyncMock(return_value=rows)),
            patch("modulo.cli.break_glass.append_audit_event", AsyncMock(return_value=None)),
        ):
            result = await deactivate(
                session,
                org_id=_ORG_ID,
                account_id=uuid.UUID("00000000-0000-0000-0000-00000000000c"),
                actor="operator",
                reason="x",
                force=True,
                now=_NOW,
            )
        assert result["deactivated"] == 1

    async def test_no_targets_raises_org_not_found(self) -> None:
        session = _mock_session()

        with (
            patch("modulo.cli.break_glass._bg_accounts_for_org", AsyncMock(return_value=[])),
            pytest.raises(OrgNotFoundError),
        ):
            await deactivate(
                session,
                org_id=_ORG_ID,
                account_id=None,
                actor="op",
                reason="x",
                force=False,
                now=_NOW,
            )

    async def test_live_refusal_without_force(self) -> None:
        session = _mock_session()
        rows = [_bg_row()]
        with (
            patch("modulo.cli.break_glass._bg_accounts_for_org", AsyncMock(return_value=rows)),
            pytest.raises(DeactivateRefusedError),
        ):
            await deactivate(
                session,
                org_id=_ORG_ID,
                account_id=None,
                actor="op",
                reason="x",
                force=False,
                now=_NOW,
            )

    def _failing_session(self, sqlstate: str | None) -> AsyncMock:
        session = _mock_session()
        orig = SimpleNamespace(sqlstate=sqlstate)
        failure = SQLAlchemyError("pg says no")
        failure.orig = orig
        session.execute = AsyncMock(return_value=MagicMock())
        session.flush = AsyncMock(side_effect=failure)
        return session

    async def test_pgcode_mapping(self) -> None:

        for sqlstate, expected in (
            ("M2040", OrgNotFoundError),
            ("M2010", DeactivateRefusedError),
            ("M2020", DeactivateRefusedError),
            ("23505", DeactivateAtomicityError),
        ):
            session = self._failing_session(sqlstate)
            rows = [_bg_row()]
            with (
                patch("modulo.cli.break_glass._bg_accounts_for_org", AsyncMock(return_value=rows)),
                patch("modulo.cli.break_glass.append_audit_event", AsyncMock(return_value=None)),
                pytest.raises(expected),
            ):
                await deactivate(session, org_id=_ORG_ID, account_id=None, actor="op", reason="x", force=True, now=_NOW)


class TestCoreForceLastAdmin:
    @staticmethod
    def _admin() -> SimpleNamespace:
        return SimpleNamespace(id=uuid.UUID("00000000-0000-0000-0000-00000000000d"))

    async def test_no_admins_refuses(self) -> None:
        session = _mock_session()
        with (
            patch("modulo.cli.break_glass._live_non_bg_admins", AsyncMock(return_value=[])),
            patch("modulo.cli.break_glass._bg_accounts_for_org", AsyncMock(return_value=[])),
            patch("modulo.cli.break_glass.append_audit_event", AsyncMock(return_value=None)),
            pytest.raises(PreconditionError, match="no live non-break-glass admins to remove"),
        ):
            await force_last_admin(session, org_id=_ORG_ID, actor="op", reason="r", now=_NOW)

    async def test_break_glass_only_org_refuses(self) -> None:
        session = _mock_session()
        with (
            patch("modulo.cli.break_glass._live_non_bg_admins", AsyncMock(return_value=[])),
            patch(
                "modulo.cli.break_glass._bg_accounts_for_org",
                AsyncMock(return_value=[_bg_row()]),
            ),
            pytest.raises(PreconditionError, match="refusing to remove a live break-glass account"),
        ):
            await force_last_admin(session, org_id=_ORG_ID, actor="op", reason="r", now=_NOW)

    async def test_multiple_admins_refuse(self) -> None:
        session = _mock_session()
        admins = [self._admin(), self._admin()]
        with (
            patch("modulo.cli.break_glass._live_non_bg_admins", AsyncMock(return_value=admins)),
            pytest.raises(PreconditionError, match="exactly one"),
        ):
            await force_last_admin(session, org_id=_ORG_ID, actor="op", reason="r", now=_NOW)

    async def test_removes_single_admin_and_audits(self) -> None:
        session = _mock_session()
        admins = [self._admin()]
        with (
            patch("modulo.cli.break_glass._live_non_bg_admins", AsyncMock(return_value=admins)),
            patch("modulo.cli.break_glass.append_audit_event", AsyncMock(return_value=None)) as audit,
        ):
            result = await force_last_admin(session, org_id=_ORG_ID, actor="operator", reason="r", now=_NOW)
        assert result["removed_account_id"] == str(admins[0].id)
        audit.assert_awaited_once()

    async def test_custom_error_maps_to_atomicity(self) -> None:
        from modulo.cli.break_glass import DeactivateAtomicityError

        session = _mock_session()
        admins = [SimpleNamespace(id=uuid.UUID(int=0))]
        with (
            patch("modulo.cli.break_glass._live_non_bg_admins", AsyncMock(return_value=admins)),
            patch("modulo.cli.break_glass.append_audit_event", AsyncMock(return_value=None)),
        ):
            session.flush = AsyncMock(side_effect=SQLAlchemyError("weird state"))
            with pytest.raises(DeactivateAtomicityError, match="transaction failed"):
                await force_last_admin(session, org_id=_ORG_ID, actor="op", reason="r", now=_NOW)


class TestLatestActorReason:
    async def test_empty_org_ids_returns_empty(self) -> None:
        session = _mock_session()
        session.execute = AsyncMock(return_value=MagicMock())
        assert not await _latest_activation_actor_reason(session, set())

    async def test_first_event_per_org_wins(self) -> None:
        session = _mock_session()
        events = [
            SimpleNamespace(
                organisation_id=_ORG_ID,
                payload_json={"operator": "operator", "reason": "latest"},
            ),
            SimpleNamespace(
                organisation_id=_ORG_ID,
                payload_json={"operator": "older", "reason": "old"},
            ),
        ]
        result = MagicMock()
        result.scalars = MagicMock(return_value=iter(events))
        events = iter(events)
        del events  # replaced below
        result.scalars = MagicMock(return_value=())
        session.execute = AsyncMock(return_value=result)
        latest = await _latest_activation_actor_reason(session, {str(_ORG_ID)})
        assert latest == {}

    async def test_rows_scoped_to_requested_orgs(self) -> None:
        session = _mock_session()
        result = MagicMock()
        scalars = MagicMock()
        events = [
            SimpleNamespace(
                organisation_id=_ORG_ID,
                payload_json={"operator": "operator", "reason": "TKT-1"},
            )
        ]
        scalars.__iter__ = MagicMock(return_value=iter(events))
        result.scalars = MagicMock(return_value=scalars)
        session.execute = AsyncMock(return_value=result)
        latest = await _latest_activation_actor_reason(session, {str(_ORG_ID)})
        assert latest == {str(_ORG_ID): ("operator", "TKT-1")}


class TestStatusRows:
    @staticmethod
    def _pair(account: SimpleNamespace, org: SimpleNamespace) -> tuple[SimpleNamespace, SimpleNamespace]:
        return (account, org)

    @staticmethod
    def _org(**overrides: object) -> SimpleNamespace:
        base = {
            "id": _ORG_ID,
            "name": "Acme",
            "slug": "acme",
        }
        base.update(overrides)
        return SimpleNamespace(**base)

    async def test_empty_when_no_rows(self) -> None:
        session = _mock_session()
        result = MagicMock()
        result.all = MagicMock(return_value=[])
        session.execute = AsyncMock(return_value=result)
        assert not await status_rows(session, org_id=_ORG_ID, all_rows=False, now=_NOW)

    async def test_rows_rendered_with_actor_reason(self) -> None:
        session = _mock_session()
        account = SimpleNamespace(
            id=uuid.UUID("00000000-0000-0000-0000-00000000000e"),
            email="bg@modulo.run",
            break_glass_deactivated_at=None,
            break_glass_expires_at=_NOW + timedelta(minutes=10),
            created_at=_NOW - timedelta(hours=1),
            last_login=None,
            password_hash="$2b$12$",
        )
        org = self._org()
        pairs_result = MagicMock()
        pairs_result.all = MagicMock(return_value=[self._pair(account, org)])
        session.execute = AsyncMock(return_value=pairs_result)
        with patch(
            "modulo.cli.break_glass._latest_activation_actor_reason",
            AsyncMock(return_value={str(_ORG_ID): ("operator", "TKT-7")}),
        ):
            rows = await status_rows(session, org_id=_ORG_ID, all_rows=True, now=_NOW)
        assert rows[0]["state"] == "live"
        assert rows[0]["org_slug"] == "acme"
        assert rows[0]["actor"] == "operator"


class TestSmoke:
    async def test_success(self) -> None:
        session = _mock_session()
        session.execute = AsyncMock(
            side_effect=[
                MagicMock(scalar_one=MagicMock(return_value=1)),
                MagicMock(scalar_one=MagicMock(return_value="modulo_breakglass")),
                MagicMock(scalar_one=MagicMock(return_value="function-ref")),
            ]
        )
        result = await smoke(session)
        assert result["connectivity"] == "ok"
        assert result["session_user"] == "modulo_breakglass"

    async def test_probe_returns_wrong_value(self) -> None:
        session = _mock_session()
        session.execute = AsyncMock(return_value=MagicMock(scalar_one=MagicMock(return_value=2)))
        with pytest.raises(SmokeFailureError):
            await smoke(session)

    async def test_wrong_session_user(self) -> None:
        session = _mock_session()
        session.execute = AsyncMock(
            side_effect=[
                MagicMock(scalar_one=MagicMock(return_value=1)),
                MagicMock(scalar_one=MagicMock(return_value="postgres")),
            ]
        )
        with pytest.raises(SmokeFailureError, match="modulo_breakglass"):
            await smoke(session)

    async def test_function_missing(self) -> None:
        session = _mock_session()
        session.execute = AsyncMock(
            side_effect=[
                MagicMock(scalar_one=MagicMock(return_value=1)),
                MagicMock(scalar_one=MagicMock(return_value="modulo_breakglass")),
                MagicMock(scalar_one=MagicMock(return_value=None)),
            ]
        )
        with pytest.raises(SmokeFailureError, match="not found"):
            await smoke(session)

    async def test_generic_failure_wrapped(self) -> None:
        session = _mock_session()
        session.execute = AsyncMock(side_effect=RuntimeError("no connection"))
        with pytest.raises(SmokeFailureError, match="probe failed"):
            await smoke(session)


class TestEngineFactory:
    def test_engine_cached_per_url(self) -> None:
        import modulo.cli.break_glass as bg

        engine_one = bg.get_break_glass_engine(_make_settings())
        engine_two = bg.get_break_glass_engine(_make_settings())
        assert engine_one is engine_two
        other = SimpleNamespace(modulo_break_glass_database_url="postgresql+asyncpg://bg:bg@other:5432/db")
        engine_other = bg.get_break_glass_engine(other)
        assert engine_other is not engine_one

    def test_factory_cached_per_url(self) -> None:
        import modulo.cli.break_glass as bg

        settings = _make_settings()
        factory_one = bg.get_break_glass_session_factory(settings)
        factory_two = bg.get_break_glass_session_factory(settings)
        assert factory_one is factory_two


class TestRenderAndContextHelpers:
    def test_render_status_text_and_json(self, capsys: pytest.CaptureFixture[str]) -> None:
        _render_status([], as_json=False)
        assert "no break-glass rows" in capsys.readouterr().out
        _render_status(
            [
                {
                    "org_slug": "acme",
                    "state": "live",
                    "account_id": "a",
                    "expires_at": _NOW,
                    "reason": "r",
                    "actor": "op",
                }
            ],
            as_json=False,
        )
        assert "acme" in capsys.readouterr().out
        _render_status([{"state": "live"}], as_json=True)
        assert '"state"' in capsys.readouterr().out

    def test_settings_from_ctx_caches(self) -> None:

        ctx = MagicMock()
        ctx.obj = {}
        settings = SimpleNamespace()
        ctx.obj["settings"] = settings
        assert _settings_from_ctx(ctx) is settings

    def test_factory_from_ctx_caches(self) -> None:
        ctx = MagicMock()
        ctx.obj = {"session_factory": object()}
        assert _factory_from_ctx(ctx, SimpleNamespace()) is ctx.obj["session_factory"]

        ctx.obj = {}
        fake = object()
        with patch("modulo.cli.break_glass.get_break_glass_session_factory", return_value=fake) as getter:
            assert _factory_from_ctx(ctx, SimpleNamespace()) is fake
        getter.assert_called_once()
        assert ctx.obj["session_factory"] is fake
