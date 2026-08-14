"""Unit tests for the Fernet key-rotation service (modulo.core.fernet_rotation).

Applies QA lenses (correctness, bugs, maintainability) to the rotation module,
which re-encrypts every Fernet-encrypted data store with a new key while
falling back to the previous key for decryption.

Covers the pure helpers (decrypt_with_fallback, re_encrypt_bytes,
re_encrypt_str), every table-specific rotator, and the end-to-end
rotate_all_encrypted_data orchestration.
"""

import json
from collections.abc import Awaitable, Callable, Mapping
from contextlib import ExitStack
from types import SimpleNamespace
from typing import Any, ClassVar
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cryptography.fernet import Fernet, InvalidToken

from modulo.core import fernet_rotation
from modulo.core.fernet_rotation import (
    RotationResult,
    _rotate_checkpoint_blobs,
    _rotate_checkpoint_writes,
    _rotate_checkpoints,
    _rotate_connector_instances,
    _rotate_model_backends,
    _rotate_notification_endpoints,
    _rotate_otel_config,
    _rotate_secrets_table,
    decrypt_with_fallback,
    re_encrypt_bytes,
    re_encrypt_str,
    rotate_all_encrypted_data,
)


def _new_key() -> str:
    """A fresh, valid Fernet key as a string."""
    return Fernet.generate_key().decode()


def _make_session(rows: list[object] | None = None) -> AsyncMock:
    """Build an AsyncMock session that records every executed statement.

    ``execute`` returns a result whose ``.all()`` yields *rows*; all executed
    statements are recorded on ``session.calls`` as ``(sql, params)`` pairs.
    """
    session = AsyncMock()
    calls: list[tuple[str, dict[str, object]]] = []

    async def execute(statement: object, params: dict[str, object] | None = None) -> MagicMock:
        calls.append((str(statement), dict(params or {})))
        result = MagicMock()
        result.all.return_value = list(rows or [])
        return result

    session.execute = execute
    session.calls = calls
    return session


class TestRotationResult:
    def test_defaults(self) -> None:
        result = RotationResult()
        assert not result.tables_processed
        assert result.total_rows_reencrypted == 0
        assert not result.details

    def test_fields_mutable(self) -> None:
        result = RotationResult()
        result.tables_processed.append("secrets")
        result.details["secrets"] = 3
        result.total_rows_reencrypted = 3
        assert result.tables_processed == ["secrets"]
        assert result.details == {"secrets": 3}
        assert result.total_rows_reencrypted == 3


class TestDecryptWithFallback:
    def test_decrypts_with_new_fernet(self) -> None:
        new_fernet = Fernet(_new_key())
        ct = new_fernet.encrypt(b"plain")
        assert decrypt_with_fallback(ct, new_fernet, None) == b"plain"

    def test_falls_back_to_old_fernet_on_invalid_token(self) -> None:
        new_fernet = Fernet(_new_key())
        old_fernet = Fernet(_new_key())
        ct = old_fernet.encrypt(b"legacy")
        assert decrypt_with_fallback(ct, new_fernet, old_fernet) == b"legacy"

    def test_reraises_without_old_fernet(self) -> None:
        new_fernet = Fernet(_new_key())
        other_fernet = Fernet(_new_key())
        ct = other_fernet.encrypt(b"alien")
        with pytest.raises(InvalidToken):
            decrypt_with_fallback(ct, new_fernet, None)

    def test_propagates_old_fernet_failure(self) -> None:
        new_fernet = Fernet(_new_key())
        old_fernet = Fernet(_new_key())
        other_fernet = Fernet(_new_key())
        ct = other_fernet.encrypt(b"alien")
        with pytest.raises(InvalidToken):
            decrypt_with_fallback(ct, new_fernet, old_fernet)


class TestReEncryptBytes:
    def test_round_trips_through_new_key(self) -> None:
        new_fernet = Fernet(_new_key())
        old_fernet = Fernet(_new_key())
        ct = new_fernet.encrypt(b"secret-data")
        out = re_encrypt_bytes(ct, new_fernet, old_fernet)
        assert new_fernet.decrypt(out) == b"secret-data"

    def test_re_encrypts_old_encrypted_data_with_new_key(self) -> None:
        new_fernet = Fernet(_new_key())
        old_fernet = Fernet(_new_key())
        ct = old_fernet.encrypt(b"legacy-data")
        out = re_encrypt_bytes(ct, new_fernet, old_fernet)
        assert new_fernet.decrypt(out) == b"legacy-data"
        with pytest.raises(InvalidToken):
            old_fernet.decrypt(out)

    def test_no_old_key_uses_new_key_only(self) -> None:
        new_fernet = Fernet(_new_key())
        ct = new_fernet.encrypt(b"data")
        out = re_encrypt_bytes(ct, new_fernet, None)
        assert new_fernet.decrypt(out) == b"data"


class TestReEncryptStr:
    def test_round_trips_through_new_key(self) -> None:
        new_fernet = Fernet(_new_key())
        ct = new_fernet.encrypt(b"secret").decode()
        out = re_encrypt_str(ct, new_fernet, None)
        assert new_fernet.decrypt(out.encode()) == b"secret"

    def test_re_encrypts_old_encrypted_str_with_new_key(self) -> None:
        new_fernet = Fernet(_new_key())
        old_fernet = Fernet(_new_key())
        ct = old_fernet.encrypt(b"legacy").decode()
        out = re_encrypt_str(ct, new_fernet, old_fernet)
        assert new_fernet.decrypt(out.encode()) == b"legacy"
        assert isinstance(out, str)


class TestRotateSecretsTable:
    @pytest.mark.asyncio
    async def test_re_encrypts_each_row(self) -> None:
        new_fernet = Fernet(_new_key())
        old_fernet = Fernet(_new_key())
        old_ct = old_fernet.encrypt(b"api-key")
        session = _make_session([SimpleNamespace(id="s-1", encrypted_value=old_ct)])

        count = await _rotate_secrets_table(session, new_fernet, old_fernet)

        assert count == 1
        assert len(session.calls) == 2
        select_sql, _ = session.calls[0]
        assert "FROM secrets" in select_sql
        update_sql, params = session.calls[1]
        assert "UPDATE secrets" in update_sql
        assert "encrypted_value = :ct" in update_sql
        assert params["id"] == "s-1"
        assert new_fernet.decrypt(params["ct"]) == b"api-key"

    @pytest.mark.asyncio
    async def test_empty_table_returns_zero(self) -> None:
        session = _make_session([])
        count = await _rotate_secrets_table(session, Fernet(_new_key()), None)
        assert count == 0
        assert len(session.calls) == 1


class TestRotateConnectorInstances:
    @pytest.mark.asyncio
    async def test_re_encrypts_each_row(self) -> None:
        new_fernet = Fernet(_new_key())
        old_fernet = Fernet(_new_key())
        old_ct = old_fernet.encrypt(b"github-token")
        session = _make_session([SimpleNamespace(id="c-1", credentials_ciphertext=old_ct)])

        count = await _rotate_connector_instances(session, new_fernet, old_fernet)

        assert count == 1
        select_sql, _ = session.calls[0]
        assert "FROM connector_instances" in select_sql
        update_sql, params = session.calls[1]
        assert "UPDATE connector_instances" in update_sql
        assert params["id"] == "c-1"
        assert new_fernet.decrypt(params["ct"]) == b"github-token"

    @pytest.mark.asyncio
    async def test_empty_table_returns_zero(self) -> None:
        session = _make_session([])
        count = await _rotate_connector_instances(session, Fernet(_new_key()), None)
        assert count == 0


class TestRotateModelBackends:
    @pytest.mark.asyncio
    async def test_re_encrypts_each_row(self) -> None:
        new_fernet = Fernet(_new_key())
        old_fernet = Fernet(_new_key())
        old_ct = old_fernet.encrypt(b"openai-key")
        session = _make_session([SimpleNamespace(id="m-1", credentials_ciphertext=old_ct)])

        count = await _rotate_model_backends(session, new_fernet, old_fernet)

        assert count == 1
        select_sql, _ = session.calls[0]
        assert "FROM model_backends" in select_sql
        update_sql, params = session.calls[1]
        assert "UPDATE model_backends" in update_sql
        assert params["id"] == "m-1"
        assert new_fernet.decrypt(params["ct"]) == b"openai-key"


class TestRotateNotificationEndpoints:
    @pytest.mark.asyncio
    async def test_re_encrypts_non_null_secrets(self) -> None:
        new_fernet = Fernet(_new_key())
        old_fernet = Fernet(_new_key())
        old_ct = old_fernet.encrypt(b"webhook-secret")
        session = _make_session([SimpleNamespace(id="n-1", secret_ciphertext=old_ct)])

        count = await _rotate_notification_endpoints(session, new_fernet, old_fernet)

        assert count == 1
        select_sql, _ = session.calls[0]
        assert "FROM notification_endpoints" in select_sql
        assert "secret_ciphertext IS NOT NULL" in select_sql
        update_sql, params = session.calls[1]
        assert "UPDATE notification_endpoints" in update_sql
        assert params["id"] == "n-1"
        assert new_fernet.decrypt(params["ct"]) == b"webhook-secret"

    @pytest.mark.asyncio
    async def test_no_rows_returns_zero(self) -> None:
        session = _make_session([])
        count = await _rotate_notification_endpoints(session, Fernet(_new_key()), None)
        assert count == 0


class TestRotateOtelConfig:
    @pytest.mark.asyncio
    async def test_re_encrypts_langsmith_key(self) -> None:
        new_fernet = Fernet(_new_key())
        old_fernet = Fernet(_new_key())
        old_ct = old_fernet.encrypt(b"langsmith-key").decode()
        config = {"langsmith_api_key_ciphertext": old_ct, "other": "kept"}
        session = _make_session([SimpleNamespace(id="o-1", otel_config_json=config)])

        count = await _rotate_otel_config(session, new_fernet, old_fernet)

        assert count == 1
        select_sql, _ = session.calls[0]
        assert "FROM organisations" in select_sql
        assert "langsmith_api_key_ciphertext" in select_sql
        update_sql, params = session.calls[1]
        assert "UPDATE organisations" in update_sql
        assert params["id"] == "o-1"
        updated = json.loads(params["config"])
        assert updated["other"] == "kept"
        assert new_fernet.decrypt(updated["langsmith_api_key_ciphertext"].encode()) == b"langsmith-key"

    @pytest.mark.asyncio
    async def test_skips_row_with_empty_stored_value(self) -> None:
        new_fernet = Fernet(_new_key())
        config = {"langsmith_api_key_ciphertext": "", "other": "kept"}
        session = _make_session([SimpleNamespace(id="o-1", otel_config_json=config)])

        count = await _rotate_otel_config(session, new_fernet, None)

        assert count == 0
        assert len(session.calls) == 1
        assert "UPDATE" not in session.calls[0][0]

    @pytest.mark.asyncio
    async def test_skips_row_with_null_stored_value(self) -> None:
        new_fernet = Fernet(_new_key())
        config = {"langsmith_api_key_ciphertext": None, "other": "kept"}
        session = _make_session([SimpleNamespace(id="o-1", otel_config_json=config)])

        count = await _rotate_otel_config(session, new_fernet, None)

        assert count == 0
        assert len(session.calls) == 1


class TestRotateCheckpoints:
    @pytest.mark.asyncio
    async def test_re_encrypts_encrypted_wrapper(self) -> None:
        new_fernet = Fernet(_new_key())
        old_fernet = Fernet(_new_key())
        old_ct = old_fernet.encrypt(b"checkpoint-state").decode()
        wrapper = {"__encrypted__": "true", "data": old_ct, "metadata": {}}
        session = _make_session(
            [
                SimpleNamespace(
                    organisation_id="org-1",
                    thread_id="thr-1",
                    checkpoint_ns="",
                    checkpoint_id="ck-1",
                    checkpoint=wrapper,
                )
            ]
        )

        count = await _rotate_checkpoints(session, new_fernet, old_fernet)

        assert count == 1
        select_sql, _ = session.calls[0]
        assert "FROM checkpoints" in select_sql
        assert "__encrypted__" in select_sql
        update_sql, params = session.calls[1]
        assert "UPDATE checkpoints" in update_sql
        assert params["org_id"] == "org-1"
        assert params["thread_id"] == "thr-1"
        assert params["ns"] == ""
        assert params["ckpt_id"] == "ck-1"
        updated = json.loads(params["checkpoint"])
        assert not updated["metadata"]
        assert new_fernet.decrypt(updated["data"].encode()) == b"checkpoint-state"

    @pytest.mark.asyncio
    async def test_skips_row_with_empty_data(self) -> None:
        new_fernet = Fernet(_new_key())
        wrapper = {"__encrypted__": "true", "data": "", "metadata": {}}
        session = _make_session(
            [
                SimpleNamespace(
                    organisation_id="org-1",
                    thread_id="thr-1",
                    checkpoint_ns="",
                    checkpoint_id="ck-1",
                    checkpoint=wrapper,
                )
            ]
        )

        count = await _rotate_checkpoints(session, new_fernet, None)

        assert count == 0
        assert len(session.calls) == 1
        assert "UPDATE" not in session.calls[0][0]


class TestRotateCheckpointBlobs:
    @pytest.mark.asyncio
    async def test_re_encrypts_blob_bytes(self) -> None:
        new_fernet = Fernet(_new_key())
        old_fernet = Fernet(_new_key())
        old_blob = old_fernet.encrypt(b"blob-bytes")
        session = _make_session(
            [
                SimpleNamespace(
                    organisation_id="org-1",
                    thread_id="thr-1",
                    checkpoint_ns="",
                    channel="channel-1",
                    version="1",
                    blob=old_blob,
                )
            ]
        )

        count = await _rotate_checkpoint_blobs(session, new_fernet, old_fernet)

        assert count == 1
        select_sql, _ = session.calls[0]
        assert "FROM checkpoint_blobs" in select_sql
        assert "blob IS NOT NULL" in select_sql
        update_sql, params = session.calls[1]
        assert "UPDATE checkpoint_blobs" in update_sql
        assert params["org_id"] == "org-1"
        assert params["thread_id"] == "thr-1"
        assert params["ns"] == ""
        assert params["channel"] == "channel-1"
        assert params["version"] == "1"
        assert new_fernet.decrypt(params["blob"]) == b"blob-bytes"

    @pytest.mark.asyncio
    async def test_no_rows_returns_zero(self) -> None:
        session = _make_session([])
        count = await _rotate_checkpoint_blobs(session, Fernet(_new_key()), None)
        assert count == 0


class TestRotateCheckpointWrites:
    @pytest.mark.asyncio
    async def test_re_encrypts_writes_with_composite_key(self) -> None:
        new_fernet = Fernet(_new_key())
        old_fernet = Fernet(_new_key())
        old_blob = old_fernet.encrypt(b"write-bytes")
        session = _make_session(
            [
                SimpleNamespace(
                    organisation_id="org-1",
                    thread_id="thr-1",
                    checkpoint_ns="",
                    checkpoint_id="ck-1",
                    task_id="task-1",
                    idx=0,
                    blob=old_blob,
                )
            ]
        )

        count = await _rotate_checkpoint_writes(session, new_fernet, old_fernet)

        assert count == 1
        select_sql, _ = session.calls[0]
        assert "FROM checkpoint_writes" in select_sql
        update_sql, params = session.calls[1]
        assert "UPDATE checkpoint_writes" in update_sql
        assert params["org_id"] == "org-1"
        assert params["thread_id"] == "thr-1"
        assert params["ns"] == ""
        assert params["ckpt_id"] == "ck-1"
        assert params["task_id"] == "task-1"
        assert params["idx"] == 0
        assert new_fernet.decrypt(params["blob"]) == b"write-bytes"

    @pytest.mark.asyncio
    async def test_no_rows_returns_zero(self) -> None:
        session = _make_session([])
        count = await _rotate_checkpoint_writes(session, Fernet(_new_key()), None)
        assert count == 0


class TestRotateAllEncryptedData:
    # "observability_config" is the logical label for _rotate_otel_config,
    # which re-encrypts the langsmith key stored in the "organisations"
    # table (column otel_config_json).
    _TABLES: ClassVar[list[str]] = [
        "secrets",
        "connector_instances",
        "model_backends",
        "notification_endpoints",
        "observability_config",
        "checkpoints",
        "checkpoint_blobs",
        "checkpoint_writes",
    ]
    _ROTATOR_NAMES: ClassVar[dict[str, str]] = {
        "secrets": "_rotate_secrets_table",
        "connector_instances": "_rotate_connector_instances",
        "model_backends": "_rotate_model_backends",
        "notification_endpoints": "_rotate_notification_endpoints",
        "observability_config": "_rotate_otel_config",
        "checkpoints": "_rotate_checkpoints",
        "checkpoint_blobs": "_rotate_checkpoint_blobs",
        "checkpoint_writes": "_rotate_checkpoint_writes",
    }

    def _enter_rotator_patches(self, stack: ExitStack, rotators: Mapping[str, object]) -> None:
        for table in self._TABLES:
            stack.enter_context(patch.object(fernet_rotation, self._ROTATOR_NAMES[table], rotators[table]))

    @pytest.mark.asyncio
    async def test_rotates_all_stores_and_aggregates(self) -> None:
        new_key = _new_key()
        old_key = _new_key()
        session = _make_session()
        counts = dict(zip(self._TABLES, range(1, 9), strict=True))
        mocks = {table: AsyncMock(return_value=count) for table, count in counts.items()}

        with ExitStack() as stack:
            self._enter_rotator_patches(stack, mocks)
            result = await rotate_all_encrypted_data(session, new_key, old_key)

        assert result.tables_processed == self._TABLES
        assert result.details == counts
        assert result.total_rows_reencrypted == sum(counts.values())
        for mock in mocks.values():
            mock.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_old_key_empty_uses_no_fallback(self) -> None:
        new_key = _new_key()
        session = _make_session()
        captured: dict[str, list[Any]] = {}

        def _make_rotator() -> Callable[[Any, Fernet, Fernet | None], Awaitable[int]]:
            async def rotator(_session: Any, new_fernet: Fernet, old_fernet: Fernet | None) -> int:
                captured.setdefault("new_fernet", []).append(new_fernet)
                captured.setdefault("old_fernet", []).append(old_fernet)
                return 0

            return rotator

        rotators = {table: _make_rotator() for table in self._TABLES}
        with ExitStack() as stack:
            self._enter_rotator_patches(stack, rotators)
            result = await rotate_all_encrypted_data(session, new_key, old_key="")

        assert result.total_rows_reencrypted == 0
        assert len(captured["new_fernet"]) == 8
        assert len(captured["old_fernet"]) == 8
        assert all(fn is None for fn in captured["old_fernet"])

    @pytest.mark.asyncio
    async def test_old_key_builds_fallback_fernet(self) -> None:
        new_key = _new_key()
        old_key = _new_key()
        session = _make_session()
        captured: dict[str, list[Any]] = {}

        def _make_rotator() -> Callable[[Any, Fernet, Fernet | None], Awaitable[int]]:
            async def rotator(_session: Any, _new_fernet: Fernet, old_fernet: Fernet | None) -> int:
                captured.setdefault("old_fernet", []).append(old_fernet)
                return 0

            return rotator

        rotators = {table: _make_rotator() for table in self._TABLES}
        with ExitStack() as stack:
            self._enter_rotator_patches(stack, rotators)
            await rotate_all_encrypted_data(session, new_key, old_key=old_key)

        assert len(captured["old_fernet"]) == 8
        assert all(isinstance(fn, Fernet) for fn in captured["old_fernet"])
        plaintext = b"roundtrip"
        ct = captured["old_fernet"][0].encrypt(plaintext)
        assert Fernet(old_key.encode()).decrypt(ct) == plaintext

    @pytest.mark.asyncio
    async def test_invalid_new_key_raises_before_any_rotator(self) -> None:
        session = _make_session()
        rotators = {table: AsyncMock(side_effect=AssertionError("rotator should not run")) for table in self._TABLES}
        with ExitStack() as stack:
            self._enter_rotator_patches(stack, rotators)
            with pytest.raises(ValueError, match="Fernet key"):
                await rotate_all_encrypted_data(session, "not-a-fernet-key", old_key="")

    @pytest.mark.asyncio
    async def test_invalid_old_key_raises(self) -> None:
        session = _make_session()
        with pytest.raises(ValueError, match="Fernet key"):
            await rotate_all_encrypted_data(session, _new_key(), old_key="bad-key")

    @pytest.mark.asyncio
    async def test_failing_rotator_propagates_exception(self) -> None:
        session = _make_session()
        failures = {
            "secrets": AsyncMock(return_value=5),
            "connector_instances": AsyncMock(side_effect=RuntimeError("boom")),
            **{table: AsyncMock(return_value=0) for table in self._TABLES[2:]},
        }
        with ExitStack() as stack:
            self._enter_rotator_patches(stack, failures)
            with pytest.raises(RuntimeError, match="boom"):
                await rotate_all_encrypted_data(session, _new_key(), old_key=_new_key())

        failures["secrets"].assert_awaited_once()
