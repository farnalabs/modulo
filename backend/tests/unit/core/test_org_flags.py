"""Unit tests for the FAR-795 org-flag substrate (core.runtime_config.org_flags)."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy.exc import SQLAlchemyError

from modulo.core.runtime_config import org_flags
from modulo.core.runtime_config.org_flags import (
    FLAG_WORK_ITEM_AGENT_MINTING_ENABLED,
    clear_org_flag_cache,
    is_org_flag_enabled,
    read_org_flag,
    set_org_flag,
)

ORG_ID = uuid4()


@pytest.fixture(autouse=True)
def _clean_cache():
    clear_org_flag_cache()
    yield
    clear_org_flag_cache()


def _session_for_settings(settings_json):
    """Mock session whose org row exposes *settings_json*.

    ``session.info`` carries the bound org RLS context: real sessions get it
    from ``set_rls_org`` on generic backends, and the FAR-1830/FAR-1832 atomic
    settings patch (behind ``set_org_flag``) reads it back through its strict
    fail-closed tenant guard.
    """
    org = MagicMock()
    org.settings_json = settings_json
    result = MagicMock()
    result.scalar_one_or_none.return_value = org
    session = AsyncMock()
    session.info = {"org_id": str(ORG_ID)}
    session.execute.return_value = result
    return session


class TestReadOrgFlag:
    @pytest.mark.anyio
    async def test_absent_key_defaults_off(self):
        session = _session_for_settings({})
        assert await read_org_flag(session, ORG_ID, FLAG_WORK_ITEM_AGENT_MINTING_ENABLED) is False

    @pytest.mark.anyio
    async def test_true_enabled(self):
        session = _session_for_settings({FLAG_WORK_ITEM_AGENT_MINTING_ENABLED: True})
        assert await read_org_flag(session, ORG_ID, FLAG_WORK_ITEM_AGENT_MINTING_ENABLED) is True

    @pytest.mark.anyio
    async def test_null_false_and_non_bool_values_disable(self):
        """Only a JSON ``true`` enables; null/string/int never do."""
        for raw in (None, False, "true", 1, []):
            clear_org_flag_cache()
            session = _session_for_settings({FLAG_WORK_ITEM_AGENT_MINTING_ENABLED: raw})
            assert await read_org_flag(session, ORG_ID, FLAG_WORK_ITEM_AGENT_MINTING_ENABLED) is False, raw

    @pytest.mark.anyio
    async def test_non_dict_settings_disable(self):
        session = _session_for_settings("not a dict")
        assert await read_org_flag(session, ORG_ID, FLAG_WORK_ITEM_AGENT_MINTING_ENABLED) is False

    @pytest.mark.anyio
    async def test_unknown_flag_warns_and_returns_default(self):
        session = _session_for_settings({})
        assert await read_org_flag(session, ORG_ID, "not_a_flag") is False

    @pytest.mark.anyio
    async def test_read_org_not_found_returns_default(self):
        """A missing org row resolves to the fail-closed default OFF."""
        session = _session_for_settings({})
        session.execute.return_value.scalar_one_or_none.return_value = None
        assert await read_org_flag(session, ORG_ID, FLAG_WORK_ITEM_AGENT_MINTING_ENABLED) is False

    @pytest.mark.anyio
    async def test_cache_hit_skips_db_read(self):
        session = _session_for_settings({FLAG_WORK_ITEM_AGENT_MINTING_ENABLED: True})
        assert await read_org_flag(session, ORG_ID, FLAG_WORK_ITEM_AGENT_MINTING_ENABLED) is True
        first_calls = session.execute.await_count
        # Same org+flag within TTL: second read must not hit the DB again.
        assert await read_org_flag(session, ORG_ID, FLAG_WORK_ITEM_AGENT_MINTING_ENABLED) is True
        assert session.execute.await_count == first_calls

    @pytest.mark.anyio
    async def test_cache_expires_within_ttl(self, monkeypatch):
        clock = {"t": 0.0}
        monkeypatch.setattr(
            org_flags,
            "time",
            SimpleNamespace(monotonic=lambda: clock["t"]),
        )
        assert org_flags._CACHE_TTL_SECONDS <= 30  # contract: ≤30s TTL

        session = _session_for_settings({FLAG_WORK_ITEM_AGENT_MINTING_ENABLED: True})
        assert await read_org_flag(session, ORG_ID, FLAG_WORK_ITEM_AGENT_MINTING_ENABLED) is True

        # Value flips in the DB, but still within TTL → stale value served.
        org = session.execute.return_value.scalar_one_or_none.return_value
        org.settings_json = {FLAG_WORK_ITEM_AGENT_MINTING_ENABLED: False}
        clock["t"] = org_flags._CACHE_TTL_SECONDS - 0.001
        assert await read_org_flag(session, ORG_ID, FLAG_WORK_ITEM_AGENT_MINTING_ENABLED) is True

        # Beyond TTL → fresh read sees the new value.
        clock["t"] = org_flags._CACHE_TTL_SECONDS + 0.001
        assert await read_org_flag(session, ORG_ID, FLAG_WORK_ITEM_AGENT_MINTING_ENABLED) is False


class TestIsOrgFlagEnabled:
    @pytest.mark.anyio
    async def test_fail_closed_on_db_error(self):
        """ANY read error resolves OFF — the safety control's core guarantee."""
        session = AsyncMock()
        session.execute.side_effect = SQLAlchemyError("db outage")
        assert await is_org_flag_enabled(session, ORG_ID, FLAG_WORK_ITEM_AGENT_MINTING_ENABLED) is False

    @pytest.mark.anyio
    async def test_fail_closed_evicts_cache_so_next_read_retries_db(self):
        session = _session_for_settings({FLAG_WORK_ITEM_AGENT_MINTING_ENABLED: True})
        assert await is_org_flag_enabled(session, ORG_ID, FLAG_WORK_ITEM_AGENT_MINTING_ENABLED) is True
        (org_id, flag) = (ORG_ID, FLAG_WORK_ITEM_AGENT_MINTING_ENABLED)
        assert (org_id, flag) in org_flags._flag_cache

        # The error must hit a FRESH read (within-TTL cached data is legitimately
        # servable during an outage — it is real, not unknown, state).
        clear_org_flag_cache()
        session.execute.side_effect = SQLAlchemyError("db outage")
        assert await is_org_flag_enabled(session, ORG_ID, FLAG_WORK_ITEM_AGENT_MINTING_ENABLED) is False
        assert (org_id, flag) not in org_flags._flag_cache

        # Recovery: the next read hits the DB again and restores True.
        session.execute.side_effect = None
        assert await is_org_flag_enabled(session, ORG_ID, FLAG_WORK_ITEM_AGENT_MINTING_ENABLED) is True


class TestSetOrgFlag:
    @pytest.mark.anyio
    async def test_set_persists_via_the_atomic_settings_patch(self):
        """The write lands through the FAR-1830 patch helper (generic-dialect
        fallback on this mock: read-merge-write under the helper's own lock)."""
        session = _session_for_settings({})
        await set_org_flag(session, ORG_ID, FLAG_WORK_ITEM_AGENT_MINTING_ENABLED, True)
        org = session.execute.return_value.scalar_one_or_none.return_value
        assert org.settings_json[FLAG_WORK_ITEM_AGENT_MINTING_ENABLED] is True

    @pytest.mark.anyio
    async def test_set_delegates_to_the_patch_with_only_its_own_key(self):
        """FAR-1832: ``set_org_flag`` emits ONLY its flag key through the atomic
        patch — no ``SELECT ... FOR UPDATE`` + whole-column read-modify-write.

        Fails against the old shape: the old code never called the patch
        helper (``captured`` stays empty) and issued a locked org select.
        """
        session = _session_for_settings({})
        captured: dict[str, object] = {}

        async def _fake_patch(_session, org_id, patch=None, delete_keys=None, **kwargs):
            captured["org_id"] = org_id
            captured["patch"] = patch
            captured["delete_keys"] = delete_keys
            return dict(patch or {})

        with patch("modulo.db.crud.organisation.update_org_settings_patch", new=_fake_patch):
            await set_org_flag(session, ORG_ID, FLAG_WORK_ITEM_AGENT_MINTING_ENABLED, True)
        assert captured["org_id"] == ORG_ID
        assert captured["patch"] == {FLAG_WORK_ITEM_AGENT_MINTING_ENABLED: True}
        for call in session.execute.await_args_list:
            assert "FOR UPDATE" not in str(call.args[0]), f"writer must not pre-lock the org row: {call.args[0]}"

    @pytest.mark.anyio
    async def test_set_preserves_other_settings(self):
        session = _session_for_settings({"license_key": "license-abc", "retention_days": 30})
        await set_org_flag(session, ORG_ID, FLAG_WORK_ITEM_AGENT_MINTING_ENABLED, True)
        org = session.execute.return_value.scalar_one_or_none.return_value
        assert org.settings_json["license_key"] == "license-abc"
        assert org.settings_json["retention_days"] == 30

    @pytest.mark.anyio
    async def test_set_updates_cache_immediately(self):
        session = _session_for_settings({})
        await set_org_flag(session, ORG_ID, FLAG_WORK_ITEM_AGENT_MINTING_ENABLED, True)
        # Cache is warm: read must NOT hit the DB a second time.
        calls_after_write = session.execute.await_count
        assert await read_org_flag(session, ORG_ID, FLAG_WORK_ITEM_AGENT_MINTING_ENABLED) is True
        assert session.execute.await_count == calls_after_write

    @pytest.mark.anyio
    async def test_set_missing_org_raises_lookup_error(self):
        session = _session_for_settings({})
        session.execute.return_value.scalar_one_or_none.return_value = None
        with pytest.raises(LookupError):
            await set_org_flag(session, ORG_ID, FLAG_WORK_ITEM_AGENT_MINTING_ENABLED, True)

    @pytest.mark.anyio
    async def test_set_rejects_non_bool(self):
        session = _session_for_settings({})
        with pytest.raises(TypeError):
            await set_org_flag(session, ORG_ID, FLAG_WORK_ITEM_AGENT_MINTING_ENABLED, "true")

    @pytest.mark.anyio
    async def test_set_rejects_unknown_flag(self):
        session = _session_for_settings({})
        with pytest.raises(ValueError, match="unknown org flag"):
            await set_org_flag(session, ORG_ID, "not_a_flag", True)
