"""Unit tests for modulo.db.settings_resolver — the org/system setting resolver.

Covers the org-wide trigger-pause gate (fail-closed) and the
org.settings_json → SystemConfig → default resolution chain:

  * ``get_effective_setting`` — precedence, SQLAlchemyError failover (an org
    read error falls through to system config, a config read error falls to the
    default), non-dict ``settings_json``, and falsy-but-present org values
    (presence beats the default even when the value is ``False``/``0``/``""``).
  * ``org_row_is_paused`` — the pure pause predicate truth table.
  * ``org_is_paused`` — column-level SELECT, fail-closed on a missing row,
    SQLAlchemyError propagation (never fabricate "paused").
  * ``ensure_triggers_resumable`` — raises ``TriggersPausedError`` when paused,
    no-op when active, and lets read failures propagate untouched.

Mock/fake based — no Postgres.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.exc import SQLAlchemyError

from modulo.core.exceptions import TriggersPausedError
from modulo.db.settings_resolver import (
    ensure_triggers_resumable,
    get_effective_setting,
    org_is_paused,
    org_row_is_paused,
)

_ORG_ID = uuid.uuid4()
_TRIGGER_ID = uuid.uuid4()


# ---------------------------------------------------------------------------
# get_effective_setting — org → system config → default precedence + failover
# ---------------------------------------------------------------------------


class _Org:
    def __init__(self, settings_json: object) -> None:
        self.settings_json = settings_json


class TestGetEffectiveSetting:
    async def test_returns_org_setting_when_key_present_among_others(self) -> None:
        """A populated settings_json missing the key must NOT short-circuit —
        presence of the specific key decides, not a non-empty dict."""
        session = AsyncMock()
        with pytest.MonkeyPatch().context() as m:
            m.setattr(
                "modulo.db.settings_resolver.get_organisation",
                AsyncMock(return_value=_Org({"other": 1})),
            )
            m.setattr(
                "modulo.db.settings_resolver.get_config",
                AsyncMock(return_value=MagicMock(value="system_wide")),
            )
            assert await get_effective_setting(session, _ORG_ID, "theme") == "system_wide"

    async def test_falsy_false_org_value_beats_default(self) -> None:
        """Presence must beat truthiness: an explicit ``False`` resolves to
        ``False``, never to the default."""
        session = AsyncMock()
        with pytest.MonkeyPatch().context() as m:
            m.setattr(
                "modulo.db.settings_resolver.get_organisation",
                AsyncMock(return_value=_Org({"feature_enabled": False})),
            )
            m.setattr("modulo.db.settings_resolver.get_config", AsyncMock(return_value=None))
            assert await get_effective_setting(session, _ORG_ID, "feature_enabled", default=True) is False

    async def test_zero_org_value_beats_default(self) -> None:
        session = AsyncMock()
        with pytest.MonkeyPatch().context() as m:
            m.setattr("modulo.db.settings_resolver.get_organisation", AsyncMock(return_value=_Org({"max_items": 0})))
            m.setattr("modulo.db.settings_resolver.get_config", AsyncMock(return_value=None))
            assert await get_effective_setting(session, _ORG_ID, "max_items", default=10) == 0

    async def test_empty_string_org_value_beats_default(self) -> None:
        session = AsyncMock()
        with pytest.MonkeyPatch().context() as m:
            m.setattr("modulo.db.settings_resolver.get_organisation", AsyncMock(return_value=_Org({"name": ""})))
            m.setattr("modulo.db.settings_resolver.get_config", AsyncMock(return_value=None))
            assert await get_effective_setting(session, _ORG_ID, "name", default="fallback") == ""

    async def test_org_read_error_falls_through_to_system_config(self) -> None:
        """A SQLAlchemyError resolving the org must NOT abort resolution — the
        chain continues to the system config (failover, not fail-closed)."""
        session = AsyncMock()
        with pytest.MonkeyPatch().context() as m:
            m.setattr(
                "modulo.db.settings_resolver.get_organisation",
                AsyncMock(side_effect=SQLAlchemyError("org read failed")),
            )
            m.setattr(
                "modulo.db.settings_resolver.get_config",
                AsyncMock(return_value=MagicMock(value="system_wide")),
            )
            assert await get_effective_setting(session, _ORG_ID, "theme") == "system_wide"

    async def test_org_read_error_falls_to_default_when_no_config(self) -> None:
        session = AsyncMock()
        with pytest.MonkeyPatch().context() as m:
            m.setattr(
                "modulo.db.settings_resolver.get_organisation",
                AsyncMock(side_effect=SQLAlchemyError("org read failed")),
            )
            m.setattr("modulo.db.settings_resolver.get_config", AsyncMock(return_value=None))
            assert await get_effective_setting(session, _ORG_ID, "theme", default="light") == "light"

    async def test_config_read_error_returns_default(self) -> None:
        """A SQLAlchemyError reading the system config returns the default — the
        resolver is best-effort, never surfaces a config read failure."""
        session = AsyncMock()
        with pytest.MonkeyPatch().context() as m:
            m.setattr("modulo.db.settings_resolver.get_organisation", AsyncMock(return_value=_Org({})))
            m.setattr(
                "modulo.db.settings_resolver.get_config",
                AsyncMock(side_effect=SQLAlchemyError("config read failed")),
            )
            assert await get_effective_setting(session, _ORG_ID, "theme", default="light") == "light"

    async def test_non_dict_settings_json_falls_through(self) -> None:
        """A corrupt/non-dict settings_json must not raise — fall through to the
        system config and the default."""
        session = AsyncMock()
        with pytest.MonkeyPatch().context() as m:
            m.setattr(
                "modulo.db.settings_resolver.get_organisation",
                AsyncMock(return_value=_Org(["not", "a", "dict"])),
            )
            m.setattr(
                "modulo.db.settings_resolver.get_config",
                AsyncMock(return_value=MagicMock(value="system_wide")),
            )
            assert await get_effective_setting(session, _ORG_ID, "theme") == "system_wide"


# ---------------------------------------------------------------------------
# org_row_is_paused — pure pause predicate truth table
# ---------------------------------------------------------------------------


class TestOrgRowIsPaused:
    @pytest.mark.parametrize(
        ("status", "triggers_paused", "expected"),
        [
            pytest.param("active", True, True, id="active_paused_flag"),
            pytest.param("suspended", True, True, id="suspended_paused_flag"),
            pytest.param("deleted", True, True, id="deleted_paused_flag"),
            pytest.param(None, True, True, id="missing_status_paused_flag"),
            pytest.param("active", False, False, id="active_no_pause"),
            pytest.param("suspended", False, True, id="suspended_fails_closed"),
            pytest.param("deleted", False, True, id="deleted_fails_closed"),
            pytest.param(None, False, False, id="missing_status_no_pause"),
            pytest.param("active", None, False, id="active_null_flag"),
            pytest.param("suspended", None, True, id="suspended_null_flag"),
            pytest.param(None, None, False, id="all_missing_not_paused"),
        ],
    )
    def test_truth_table(self, status: str | None, triggers_paused: bool | None, expected: bool) -> None:
        assert org_row_is_paused(status, triggers_paused) is expected


# ---------------------------------------------------------------------------
# org_is_paused — column-level SELECT, fail-closed on missing row
# ---------------------------------------------------------------------------


def _session_with_org_row(row: object) -> AsyncMock:
    session = AsyncMock()
    result = MagicMock()
    result.one_or_none.return_value = row
    session.execute = AsyncMock(return_value=result)
    return session


def _session_raising(exc: Exception) -> AsyncMock:
    session = AsyncMock()
    session.execute = AsyncMock(side_effect=exc)
    return session


class TestOrgIsPaused:
    async def test_active_org_not_paused(self) -> None:
        session = _session_with_org_row((False, "active"))
        assert await org_is_paused(session, _ORG_ID) is False

    async def test_triggers_paused_org_is_paused(self) -> None:
        session = _session_with_org_row((True, "active"))
        assert await org_is_paused(session, _ORG_ID) is True

    async def test_suspended_org_fails_closed(self) -> None:
        session = _session_with_org_row((False, "suspended"))
        assert await org_is_paused(session, _ORG_ID) is True

    async def test_deleted_org_fails_closed(self) -> None:
        session = _session_with_org_row((False, "deleted"))
        assert await org_is_paused(session, _ORG_ID) is True

    async def test_missing_row_fails_closed(self) -> None:
        """A deleted org (no row) must never let its triggers fire."""
        session = _session_with_org_row(None)
        assert await org_is_paused(session, _ORG_ID) is True

    async def test_read_error_propagates(self) -> None:
        """A SQLAlchemyError must NOT be converted into "paused" — it propagates
        so the caller decides how to surface the read failure."""
        session = _session_raising(SQLAlchemyError("db down"))
        with pytest.raises(SQLAlchemyError):
            await org_is_paused(session, _ORG_ID)

    async def test_selects_dedicated_columns_scoped_to_org(self) -> None:
        """The read is a dedicated column-level SELECT keyed by org id — it must
        reference triggers_paused + status and never the ORM identity map."""
        session = _session_with_org_row((False, "active"))
        await org_is_paused(session, _ORG_ID)

        stmt = session.execute.await_args.args[0]
        rendered = str(stmt)
        assert "triggers_paused" in rendered
        assert "status" in rendered
        assert "organisations" in rendered
        where_sql = str(stmt.whereclause.compile())
        assert ":id_1" in where_sql or ":id" in where_sql


# ---------------------------------------------------------------------------
# ensure_triggers_resumable — shared gate for every trigger-initiated path
# ---------------------------------------------------------------------------


class TestEnsureTriggersResumable:
    async def test_paused_raises_triggers_paused_error(self) -> None:
        session = AsyncMock()
        with pytest.MonkeyPatch().context() as m:
            m.setattr("modulo.db.settings_resolver.org_is_paused", AsyncMock(return_value=True))
            with pytest.raises(TriggersPausedError):
                await ensure_triggers_resumable(session, _ORG_ID)

    async def test_paused_error_carries_trigger_context(self) -> None:
        session = AsyncMock()
        with pytest.MonkeyPatch().context() as m:
            m.setattr("modulo.db.settings_resolver.org_is_paused", AsyncMock(return_value=True))
            with pytest.raises(TriggersPausedError) as excinfo:
                await ensure_triggers_resumable(
                    session,
                    _ORG_ID,
                    trigger_id=_TRIGGER_ID,
                    trigger_type="webhook",
                )
        assert excinfo.value.org_id == _ORG_ID
        assert excinfo.value.trigger_id == _TRIGGER_ID
        assert excinfo.value.trigger_type == "webhook"

    async def test_active_org_allows_resume(self) -> None:
        session = AsyncMock()
        with pytest.MonkeyPatch().context() as m:
            m.setattr("modulo.db.settings_resolver.org_is_paused", AsyncMock(return_value=False))
            assert await ensure_triggers_resumable(session, _ORG_ID) is None

    async def test_read_error_propagates_not_converted_to_paused(self) -> None:
        """A read failure must propagate untouched — never a fabricated
        ``TriggersPausedError``."""
        session = AsyncMock()
        with pytest.MonkeyPatch().context() as m:
            m.setattr(
                "modulo.db.settings_resolver.org_is_paused",
                AsyncMock(side_effect=SQLAlchemyError("db down")),
            )
            with pytest.raises(SQLAlchemyError):
                await ensure_triggers_resumable(session, _ORG_ID)
