"""Unit tests for migrate CLI paths not covered elsewhere (mocked session)."""

import hashlib
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import click
import pytest
from click.testing import CliRunner

import modulo.cli.migrate as migrate_mod
from modulo.cli.migrate import (
    _filter_scope,
    _find_existing_row,
    _hash_record,
    _import_org_data,
    _import_row,
    _parse_uuid,
    _read_jsonl,
    _read_jsonl_sync,
    _remap_fk_row,
    _resolve_admin_auth,
    _serialise_row,
    _sort_key_id,
    _verify_admin_access,
    _verify_export,
)

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_ADMIN_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")


class TestAdminAuth:
    def test_missing_credentials_returns_none(self) -> None:
        env = {"MODULO_ADMIN_SECRET": ""}
        with patch.dict("os.environ", env, clear=False):
            assert _resolve_admin_auth(None) is None

    def test_env_secret_fallback(self) -> None:
        env = {"MODULO_ADMIN_SECRET": "op-secret"}
        with patch.dict("os.environ", env, clear=False):
            assert _resolve_admin_auth(None) == "__admin_secret__"

    def test_explicit_token_takes_priority(self) -> None:
        env = {"MODULO_ADMIN_SECRET": "op-secret-ignored"}
        principal = MagicMock(org_role="admin", user_id=_ADMIN_ID)
        with (
            patch.dict("os.environ", env, clear=False),
            patch(
                "modulo.cli.migrate.get_settings",
                MagicMock(return_value=MagicMock(secret_key="secret-material")),
            ),
            patch("modulo.cli.migrate.decode_principal", return_value=principal) as decode,
        ):
            assert _resolve_admin_auth("valid-jwt") == str(_ADMIN_ID)
        decode.assert_called_once_with("valid-jwt", "secret-material")

    def test_non_admin_jwt_rejected(self) -> None:
        principal = MagicMock(org_role="runner")
        with (
            patch("modulo.cli.migrate.decode_principal", return_value=principal),
            patch(
                "modulo.cli.migrate.get_settings",
                MagicMock(return_value=MagicMock(secret_key="secret-material")),
            ),
            pytest.raises(click.ClickException, match="not an admin-level JWT"),
        ):
            _resolve_admin_auth("runner-jwt")

    def test_unparseable_jwt_rejected(self) -> None:
        with (
            patch("modulo.cli.migrate.decode_principal", MagicMock(side_effect=ValueError("bad"))),
            patch(
                "modulo.cli.migrate.get_settings",
                MagicMock(return_value=MagicMock(secret_key="secret-material")),
            ),
            pytest.raises(click.ClickException, match="Invalid admin JWT"),
        ):
            _resolve_admin_auth("not-a-jwt")


class TestVerifyAdminAccess:
    async def test_admin_secret_bypasses_db(self) -> None:
        with patch("modulo.cli.migrate.get_account_by_id", AsyncMock()) as get_account:
            await _verify_admin_access(MagicMock(), _ORG_ID, "__admin_secret__")
        get_account.assert_not_awaited()

    async def test_account_missing_raises(self) -> None:
        with (
            patch("modulo.cli.migrate.get_account_by_id", AsyncMock(return_value=None)),
            pytest.raises(click.ClickException, match="Admin account not found"),
        ):
            await _verify_admin_access(MagicMock(), _ORG_ID, str(_ADMIN_ID))

    async def test_membership_missing_raises(self) -> None:
        account = MagicMock(id=_ADMIN_ID)
        with (
            patch("modulo.cli.migrate.get_account_by_id", AsyncMock(return_value=account)),
            patch(
                "modulo.cli.migrate.get_membership_by_account_and_org",
                AsyncMock(return_value=None),
            ),
            pytest.raises(click.ClickException, match="belong to the target organisation"),
        ):
            await _verify_admin_access(MagicMock(), _ORG_ID, str(_ADMIN_ID))

    async def test_non_admin_role_raises(self) -> None:
        account = MagicMock(id=_ADMIN_ID)
        membership = MagicMock(role="runner")
        with (
            patch("modulo.cli.migrate.get_account_by_id", AsyncMock(return_value=account)),
            patch(
                "modulo.cli.migrate.get_membership_by_account_and_org",
                AsyncMock(return_value=membership),
            ),
            pytest.raises(click.ClickException, match="admin-level access"),
        ):
            await _verify_admin_access(MagicMock(), _ORG_ID, str(_ADMIN_ID))

    async def test_admin_membership_passes(self) -> None:
        account = MagicMock(id=_ADMIN_ID)
        membership = MagicMock(role="admin")
        get_account = AsyncMock(return_value=account)
        get_membership = AsyncMock(return_value=membership)
        with (
            patch("modulo.cli.migrate.get_account_by_id", get_account),
            patch("modulo.cli.migrate.get_membership_by_account_and_org", get_membership),
        ):
            await _verify_admin_access(MagicMock(), _ORG_ID, str(_ADMIN_ID))
        get_account.assert_awaited_once()
        get_membership.assert_awaited_once()


class TestSmallHelpers:
    def test_filter_scope_flags(self) -> None:
        tables = [("accounts", 1), ("pipelines", 2), ("runs", 3)]
        assert _filter_scope(tables, pipelines_only=True, users_only=False) == [("pipelines", 2)]
        assert len(_filter_scope(tables, pipelines_only=False, users_only=True)) == 1
        assert len(_filter_scope(tables, pipelines_only=False, users_only=False)) == 3

    def test_filter_scope_rejects_conflicting_flags(self) -> None:
        tables = [("accounts", 1)]
        with pytest.raises(click.ClickException, match="mutually exclusive"):
            _filter_scope(tables, pipelines_only=True, users_only=True)

    def test_sort_key_and_hash(self) -> None:
        assert not _sort_key_id({"id": None})
        assert _sort_key_id({"id": "b"}) == "b"
        assert _hash_record({"a": 1}) == hashlib.sha256(b'{"a": 1}').hexdigest()

    def test_parse_uuid(self) -> None:
        assert _parse_uuid(str(_ORG_ID), "org id") == _ORG_ID
        with pytest.raises(click.ClickException, match="Invalid org id"):
            _parse_uuid("bogus", "org id")

    def test_serialise_row_types(self) -> None:
        col_id = SimpleNamespace(name="id")
        col_ts = SimpleNamespace(name="created_at")
        values = {"id": uuid.UUID(int=7), "created_at": datetime(2026, 1, 1, tzinfo=UTC)}
        row = SimpleNamespace(**values)
        row.__table__ = SimpleNamespace(columns=[col_id, col_ts])
        result = _serialise_row(row)
        assert result["id"] == str(uuid.UUID(int=7))
        assert result["created_at"] == "2026-01-01T00:00:00+00:00"

    def test_remap_fk_row_maps_known_ids(self) -> None:
        row = {"organisation_id": "keep", "created_at": "c", "pipeline_id": str(_ORG_ID), "other": "zzz"}
        id_map = {str(_ORG_ID): str(_ADMIN_ID)}
        _remap_fk_row(row, id_map)
        assert row["pipeline_id"] == _ADMIN_ID
        assert row["other"] == "zzz"
        assert row["organisation_id"] == "keep"

    def test_remap_fk_row_skips_missing_values(self) -> None:
        row = {"pipeline_id": None}
        _remap_fk_row(row, {})
        assert row["pipeline_id"] is None


class TestImportRow:
    @staticmethod
    def _cfg(strategy: str = "skip") -> MagicMock:
        cfg = MagicMock()
        cfg.strategy = strategy
        cfg.pk_col = "id"
        cfg.skip_cols = {"id", "created_at", "organisation_id"}
        cfg.org_id = _ORG_ID
        cfg.table_name = "accounts"
        cfg.session = MagicMock()
        return cfg

    @staticmethod
    def _record(data: dict[str, object] | None) -> dict[str, object]:
        return {"__table__": "accounts", "id": "r1", "data": data}

    async def test_missing_data_counts_error(self) -> None:
        counts = {"errors": 0, "created": 0, "overwritten": 0, "skipped": 0}
        await _import_row(self._cfg(), self._record(None), {}, counts)
        assert counts["errors"] == 1

    async def test_data_not_dict_counts_error(self) -> None:
        counts = {"errors": 0, "created": 0, "overwritten": 0, "skipped": 0}
        await _import_row(self._cfg(), {"data": "junk"}, {}, counts)
        assert counts["errors"] == 1

    async def test_skip_strategy_creates_row(self) -> None:
        cfg = self._cfg()
        counts = {"errors": 0, "created": 0, "overwritten": 0, "skipped": 0}
        with (
            patch.object(migrate_mod, "_find_existing_row", AsyncMock(return_value=None)),
            patch.object(migrate_mod, "_create_row", AsyncMock()) as create,
        ):
            await _import_row(cfg, self._record({"id": str(_ORG_ID), "x": 1}), {}, counts)
        assert counts["created"] == 1
        create.assert_awaited_once()

    async def test_skip_strategy_counts_existing(self) -> None:
        cfg = self._cfg()
        counts = {"errors": 0, "created": 0, "overwritten": 0, "skipped": 0}
        existing = MagicMock()
        with patch.object(migrate_mod, "_find_existing_row", AsyncMock(return_value=existing)):
            await _import_row(cfg, self._record({"id": str(_ORG_ID)}), {}, counts)
        assert counts["skipped"] == 1

    async def test_overwrite_strategy_applies_to_existing(self) -> None:
        cfg = self._cfg(strategy="overwrite")
        counts = {"errors": 0, "created": 0, "overwritten": 0, "skipped": 0}
        existing = MagicMock()
        nested = MagicMock()
        nested.__aenter__ = AsyncMock(return_value=None)
        nested.__aexit__ = AsyncMock(return_value=False)
        cfg.session.begin_nested = MagicMock(return_value=nested)
        with (
            patch.object(migrate_mod, "_find_existing_row", AsyncMock(return_value=existing)),
            patch.object(migrate_mod, "_apply_conflict_strategy", MagicMock()) as apply_conflict,
        ):
            await _import_row(cfg, self._record({"id": str(_ORG_ID)}), {}, counts)
        apply_conflict.assert_called_once()
        assert counts["overwritten"] == 1

    async def test_generic_exception_counts_error(self) -> None:
        cfg = self._cfg()
        counts = {"errors": 0, "created": 0, "overwritten": 0, "skipped": 0}
        with patch.object(migrate_mod, "_find_existing_row", AsyncMock(side_effect=RuntimeError("db down"))):
            await _import_row(cfg, self._record({"id": str(_ORG_ID)}), {}, counts)
        assert counts["errors"] == 1


class TestImportOrgLoop:
    async def test_unknown_table_counts_error(self) -> None:
        session = MagicMock()
        session.flush = AsyncMock()
        records = [{"__table__": "no_such", "id": "1", "data": {}}]
        counts = await _import_org_data(session, _ORG_ID, records, "skip")
        assert counts["errors"] == 1

    async def test_missing_table_key_is_grouped_out(self) -> None:
        session = MagicMock()
        session.flush = AsyncMock()
        records = [{"id": "1", "data": {}}]
        counts = await _import_org_data(session, _ORG_ID, records, "skip")
        assert counts["errors"] == 0
        assert counts["created"] == 0

    async def test_flush_failure_propagates(self) -> None:
        session = MagicMock()
        session.flush = AsyncMock(side_effect=RuntimeError("flush boom"))
        fake_model = MagicMock()
        fake_model.__table__ = MagicMock()
        fake_model.__table__.primary_key.columns.keys.return_value = ["id"]
        records = [{"__table__": "accounts", "id": "9", "data": {"name": "x"}}]
        nested = (
            patch.object(migrate_mod, "_MODEL_MAP", {"accounts": fake_model}),
            patch.object(migrate_mod, "_find_existing_row", AsyncMock(return_value=None)),
            patch.object(migrate_mod, "_create_row", AsyncMock()),
            pytest.raises(RuntimeError, match="flush boom"),
        )
        with nested[0], nested[1], nested[2], nested[3]:
            await _import_org_data(session, _ORG_ID, records, "skip")
        session.flush.assert_awaited()

    async def test_scope_flags_limit_tables(self) -> None:
        session = MagicMock()
        session.flush = AsyncMock()
        fake_model = MagicMock()
        fake_model.__table__ = MagicMock()
        fake_model.__table__.primary_key.columns.keys.return_value = ["id"]
        records = [
            {"__table__": "accounts", "id": "1", "data": {}},
            {"__table__": "pipelines", "id": "2", "data": {}},
        ]
        with (
            patch.object(migrate_mod, "_MODEL_MAP", {"accounts": fake_model, "pipelines": fake_model}),
            patch.object(migrate_mod, "_import_row", AsyncMock()) as import_row,
        ):
            counts = await _import_org_data(session, _ORG_ID, records, "skip", pipelines_only=True)
        assert import_row.await_count == 1
        assert counts == {"created": 0, "skipped": 0, "overwritten": 0, "errors": 0}


class TestFindExistingRow:
    async def test_none_id_returns_none(self) -> None:
        session = MagicMock()
        assert await _find_existing_row(session, MagicMock(), "id", None, _ORG_ID) is None

    async def test_found_row_returned(self) -> None:
        from modulo.db.models.account import Account

        session = MagicMock()
        found = MagicMock(scalar_one_or_none=MagicMock(return_value="row"))
        session.execute = AsyncMock(return_value=found)
        assert await _find_existing_row(session, Account, "id", str(_ADMIN_ID), _ORG_ID) == "row"


class TestVerifyExport:
    def test_matching_hash_passes(self, capsys: pytest.CaptureFixture[str]) -> None:
        import hashlib

        row_hash = _hash_record({"id": "row-1"})
        records = [
            {
                "__table__": "accounts",
                "id": "row-1",
                "data": {"id": "row-1"},
                "__hash__": row_hash,
            }
        ]
        expected = hashlib.sha256(row_hash.encode()).hexdigest()
        result = _verify_export({"export_hash": expected}, records)
        assert result is True
        assert "OK" in capsys.readouterr().out

    def test_hash_mismatch_fails(self, capsys: pytest.CaptureFixture[str]) -> None:
        records = [{"__table__": "accounts", "data": {}, "__hash__": "h"}]
        result = _verify_export({"export_hash": "expected"}, records)
        assert result is False
        assert "FAILED" in capsys.readouterr().out

    def test_missing_hash_skips_verification(self) -> None:
        records = [{"__table__": "accounts", "data": {}, "__hash__": "h"}]
        result = _verify_export({}, records)
        assert result is False


class TestJsonlIO:
    def test_read_sync_skips_blank_lines(self, tmp_path: Path) -> None:
        path = tmp_path / "in.jsonl"
        lines = [
            json.dumps({"__meta__": {"export_hash": "abc"}}),
            "",
            json.dumps({"__table__": "accounts", "id": "1"}),
        ]
        path.write_text("\n".join(lines))
        meta, records = _read_jsonl_sync(path)
        assert meta == {"export_hash": "abc"}
        assert len(records) == 1

    def test_read_sync_invalid_json_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "in.jsonl"
        path.write_text("{not json}\n")
        with pytest.raises(click.ClickException, match="Invalid JSONL line"):
            _read_jsonl_sync(path)

    async def test_read_async_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(click.ClickException, match="not found"):
            await _read_jsonl(tmp_path / "missing.jsonl")


class TestCliCommands:
    def test_cli_requires_auth(self) -> None:
        runner = CliRunner()
        env = {"MODULO_ADMIN_TOKEN": "", "MODULO_ADMIN_SECRET": ""}
        with patch.dict("os.environ", env, clear=False):
            result = runner.invoke(
                migrate_mod.cli,
                ["export-org", str(_ORG_ID), "--output", "nope.jsonl"],
            )
            assert "Admin authentication required" in result.output
