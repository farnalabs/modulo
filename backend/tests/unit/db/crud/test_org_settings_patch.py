"""Unit tests for the atomic org ``settings_json`` patch (FAR-1830).

The integration counterpart (``tests/integration/crud/test_org_settings_patch.py``)
proves the lost-update guarantee against a real Postgres engine. These unit
tests exercise the helper's branch surface without a database — the tenant
guard, the dialect gate, both apply paths, and the identity-map refresh — so
the changed-lines coverage gate (which reads the UNIT coverage report only)
sees the same code the integration tier proves correct.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from modulo.db.crud import organisation as organisation_mod
from modulo.db.crud.organisation import (
    OrgSettingsPatchContextAbsentError,
    OrgSettingsPatchContextUnreadableError,
    OrgSettingsPatchOrgMismatch,
    _apply_settings_patch_generic,
    _apply_settings_patch_postgres,
    _coerce_org_uuid,
    _strict_read_rls_org,
    _sync_identity_map,
    update_org_settings_patch,
)
from modulo.db.models.organisation import Organisation

_UNSET = object()


class _FakeResult:
    def __init__(self, rows: list[object]) -> None:
        self._rows = rows

    def scalar(self) -> object:
        return self._rows[0] if self._rows else None

    def first(self) -> object:
        return self._rows[0] if self._rows else None


class _FakeSession:
    """Minimal async session: configurable ``info``, one canned execute result."""

    def __init__(
        self,
        *,
        info: object = _UNSET,
        rows: list[object] | None = None,
        exc: Exception | None = None,
        identity_map: object = _UNSET,
    ) -> None:
        self.info = {} if info is _UNSET else info
        self._rows = rows or []
        self._exc = exc
        self.flushed = 0
        if identity_map is not _UNSET:
            self.identity_map = identity_map

    async def execute(self, *args: object, **kwargs: object) -> _FakeResult:
        if self._exc is not None:
            raise self._exc
        return _FakeResult(self._rows)

    async def flush(self) -> None:
        self.flushed += 1


class _IdentityMap:
    def __init__(self, org: object) -> None:
        self._org = org

    def get(self, key: object) -> object:
        return self._org


# ---------------------------------------------------------------------------
# _coerce_org_uuid
# ---------------------------------------------------------------------------


def test_coerce_org_uuid_accepts_uuid_and_string() -> None:
    value = uuid.uuid4()
    assert _coerce_org_uuid(value) == value
    assert _coerce_org_uuid(f"  {value}  ") == value


def test_coerce_org_uuid_returns_none_for_invalid_value() -> None:
    assert _coerce_org_uuid("not-a-uuid") is None
    assert _coerce_org_uuid(None) is None


# ---------------------------------------------------------------------------
# update_org_settings_patch — tenant guard and dialect gate
# ---------------------------------------------------------------------------


def _stub_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    *,
    dialect: str,
    bound_org: uuid.UUID | None,
) -> dict[str, MagicMock]:
    lock = AsyncMock()
    monkeypatch.setattr(organisation_mod, "_take_settings_patch_lock_wait", lock)
    monkeypatch.setattr(organisation_mod, "_settings_patch_dialect", AsyncMock(return_value=dialect))
    monkeypatch.setattr(organisation_mod, "_strict_read_rls_org", AsyncMock(return_value=bound_org))
    postgres = AsyncMock(return_value={"path": "postgres"})
    generic = AsyncMock(return_value={"path": "generic"})
    monkeypatch.setattr(organisation_mod, "_apply_settings_patch_postgres", postgres)
    monkeypatch.setattr(organisation_mod, "_apply_settings_patch_generic", generic)
    invalidate = MagicMock()
    monkeypatch.setattr(organisation_mod, "_invalidate_settings_derived_caches", invalidate)
    return {"postgres": postgres, "generic": generic, "invalidate": invalidate}


async def test_non_uuid_org_id_is_rejected() -> None:
    with pytest.raises(ValueError, match="requires a UUID organisation id"):
        await update_org_settings_patch(_FakeSession(), "not-a-uuid")  # type: ignore[arg-type]


async def test_unbound_context_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_dependencies(monkeypatch, dialect="sqlite", bound_org=None)
    with pytest.raises(OrgSettingsPatchContextAbsentError):
        await update_org_settings_patch(_FakeSession(), uuid.uuid4(), {"k": 1})


async def test_allow_unbound_context_uses_generic_path(monkeypatch: pytest.MonkeyPatch) -> None:
    stubs = _stub_dependencies(monkeypatch, dialect="sqlite", bound_org=None)
    result = await update_org_settings_patch(_FakeSession(), uuid.uuid4(), {"k": 1}, allow_unbound_context=True)
    assert result == {"path": "generic"}
    stubs["generic"].assert_awaited_once()
    stubs["postgres"].assert_not_called()
    stubs["invalidate"].assert_called_once()


async def test_bound_to_other_org_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_dependencies(monkeypatch, dialect="sqlite", bound_org=uuid.uuid4())
    with pytest.raises(OrgSettingsPatchOrgMismatch):
        await update_org_settings_patch(_FakeSession(), uuid.uuid4(), {"k": 1})


async def test_matching_bound_org_uses_postgres_path(monkeypatch: pytest.MonkeyPatch) -> None:
    org_id = uuid.uuid4()
    stubs = _stub_dependencies(monkeypatch, dialect="postgresql", bound_org=org_id)
    result = await update_org_settings_patch(_FakeSession(), org_id, {"k": 1})
    assert result == {"path": "postgres"}
    stubs["postgres"].assert_awaited_once()
    stubs["generic"].assert_not_called()


# ---------------------------------------------------------------------------
# _strict_read_rls_org
# ---------------------------------------------------------------------------


async def test_strict_read_returns_info_org() -> None:
    value = uuid.uuid4()
    assert await _strict_read_rls_org(_FakeSession(info={"org_id": str(value)}), "sqlite") == value


async def test_strict_read_ignores_invalid_info_value() -> None:
    session = _FakeSession(info={"org_id": "not-a-uuid"})
    assert await _strict_read_rls_org(session, "sqlite") is None


async def test_strict_read_absent_info_key() -> None:
    assert await _strict_read_rls_org(_FakeSession(info={}), "sqlite") is None


async def test_strict_read_non_dict_info() -> None:
    assert await _strict_read_rls_org(_FakeSession(info=None), "sqlite") is None


async def test_strict_read_postgres_returns_scalar_org() -> None:
    value = uuid.uuid4()
    session = _FakeSession(info={}, rows=[str(value)])
    assert await _strict_read_rls_org(session, "postgresql") == value


async def test_strict_read_postgres_null_setting() -> None:
    session = _FakeSession(info={}, rows=[None])
    assert await _strict_read_rls_org(session, "postgresql") is None


async def test_strict_read_postgres_blank_setting() -> None:
    session = _FakeSession(info={}, rows=[""])
    assert await _strict_read_rls_org(session, "postgresql") is None


async def test_strict_read_postgres_read_failure_raises() -> None:
    session = _FakeSession(info={}, exc=RuntimeError("guc read failed"))
    with pytest.raises(OrgSettingsPatchContextUnreadableError):
        await _strict_read_rls_org(session, "postgresql")


# ---------------------------------------------------------------------------
# _apply_settings_patch_postgres
# ---------------------------------------------------------------------------


async def test_apply_postgres_returns_dict_row() -> None:
    session = _FakeSession(rows=[({"a": 1},)])
    assert await _apply_settings_patch_postgres(session, uuid.uuid4(), {"a": 1}, []) == {"a": 1}


async def test_apply_postgres_parses_json_string_row() -> None:
    session = _FakeSession(rows=[('{"a": 2}',)])
    assert await _apply_settings_patch_postgres(session, uuid.uuid4(), {"a": 2}, []) == {"a": 2}


async def test_apply_postgres_missing_row_raises() -> None:
    session = _FakeSession(rows=[])
    with pytest.raises(LookupError):
        await _apply_settings_patch_postgres(session, uuid.uuid4(), {}, [])


async def test_apply_postgres_refreshes_loaded_identity() -> None:
    org = Organisation(id=uuid.uuid4(), name="Org", slug="org", settings_json={})
    session = _FakeSession(rows=[({"logo_url": "x"},)], identity_map=_IdentityMap(org))
    assert await _apply_settings_patch_postgres(session, org.id, {"logo_url": "x"}, []) == {"logo_url": "x"}
    assert org.settings_json == {"logo_url": "x"}


# ---------------------------------------------------------------------------
# _apply_settings_patch_generic
# ---------------------------------------------------------------------------


async def test_apply_generic_merges_and_deletes(monkeypatch: pytest.MonkeyPatch) -> None:
    org = MagicMock()
    org.settings_json = {"keep": 1, "drop": 2}
    monkeypatch.setattr(organisation_mod, "get_organisation", AsyncMock(return_value=org))
    session = _FakeSession()
    result = await _apply_settings_patch_generic(session, uuid.uuid4(), {"add": 3}, ["drop"])
    assert result == {"keep": 1, "add": 3}
    assert org.settings_json == {"keep": 1, "add": 3}
    assert session.flushed == 1


async def test_apply_generic_non_dict_settings_starts_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    org = MagicMock()
    org.settings_json = None
    monkeypatch.setattr(organisation_mod, "get_organisation", AsyncMock(return_value=org))
    assert await _apply_settings_patch_generic(_FakeSession(), uuid.uuid4(), {"a": 1}, []) == {"a": 1}


async def test_apply_generic_missing_org_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(organisation_mod, "get_organisation", AsyncMock(return_value=None))
    with pytest.raises(LookupError):
        await _apply_settings_patch_generic(_FakeSession(), uuid.uuid4(), {"a": 1}, [])


# ---------------------------------------------------------------------------
# _sync_identity_map
# ---------------------------------------------------------------------------


def test_sync_identity_map_without_identity_map() -> None:
    assert _sync_identity_map(_FakeSession(), uuid.uuid4(), {"a": 1}) is None


def test_sync_identity_map_absent_org() -> None:
    assert _sync_identity_map(_FakeSession(identity_map=_IdentityMap(None)), uuid.uuid4(), {"a": 1}) is None


def test_sync_identity_map_updates_loaded_org() -> None:
    org = Organisation(id=uuid.uuid4(), name="Org", slug="org", settings_json={})
    _sync_identity_map(_FakeSession(identity_map=_IdentityMap(org)), org.id, {"logo_url": "x"})
    assert org.settings_json == {"logo_url": "x"}


# ---------------------------------------------------------------------------
# FAR-1832: update_organisation forbids the whole-column settings_json dumper
# ---------------------------------------------------------------------------


async def test_update_organisation_refuses_settings_json_writes() -> None:
    """The whole-column dumper shape cannot return: any ``settings_json`` key
    is refused BEFORE any database work.

    The session's ``execute`` raises — if the guard were missing, the call
    would reach ``get_organisation`` and surface that RuntimeError instead of
    the ValueError (the failure shape this test pins).
    """
    session = _FakeSession(exc=RuntimeError("db work must not start"))
    with pytest.raises(ValueError, match="settings_json"):
        await organisation_mod.update_organisation(
            session, uuid.uuid4(), {"settings_json": {"logo_url": "https://x/l.png"}}
        )


async def test_update_organisation_refuses_settings_json_alongside_other_keys() -> None:
    """A mixed updates dict (legit column + settings_json) is refused whole —
    the guard runs before anything is applied, so no partial write happens."""
    session = _FakeSession(exc=RuntimeError("db work must not start"))
    with pytest.raises(ValueError, match="settings_json"):
        await organisation_mod.update_organisation(
            session, uuid.uuid4(), {"name": "Renamed", "settings_json": {"k": 1}}
        )


async def test_update_organisation_still_writes_non_settings_columns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Legitimate non-settings callers (``name``, ``plan_id``, ...) are
    unaffected by the guard."""
    org = Organisation(id=uuid.uuid4(), name="Org", slug="org", settings_json={"license_key": "keep"})
    monkeypatch.setattr(organisation_mod, "get_organisation", AsyncMock(return_value=org))
    session = _FakeSession()

    result = await organisation_mod.update_organisation(session, uuid.uuid4(), {"name": "Renamed", "plan_id": "pro"})

    assert result is org
    assert org.name == "Renamed"
    assert org.plan_id == "pro"
    assert org.settings_json == {"license_key": "keep"}
    assert session.flushed == 1
