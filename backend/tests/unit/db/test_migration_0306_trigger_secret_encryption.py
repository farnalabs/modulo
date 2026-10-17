"""Unit tests for migration 0306_trigger_secret_encryption (FAR-1732).

Structural + behavioural contract (no Postgres needed — the bind is mocked):

* **Revision chain** — 0306 revises ``0305_guardrail_escalate_action``
  (the migrations directory has exactly one head, pinned by every sibling
  chain test), the revision id matches the filename, and neither
  ``branch_labels`` nor ``depends_on`` is set.
* **Skip semantics (mocked bind)** — the upgrade rewrites ONLY rows whose
  ``hmac_secret``/``signing_secret`` are PLAINTEXT strings: values that are
  already Fernet tokens, the sensitive-value mask sentinel, empty strings,
  or a non-string type pass through untouched, so a second run writes
  nothing (idempotency).
* **Fail closed** — a value that cannot be encrypted aborts the migration
  instead of silently persisting (half-encrypted secrets are never ok).
* **Vocabulary parity** — the migration's secret-key set is pinned equal to
  the canonical write-path helper's set
  (``modulo.api.routes.triggers._SECRET_CONFIG_KEYS``) so the two cannot
  drift.
* **No downgrade** — ``downgrade()`` does nothing: un-encrypting secrets
  into plaintext on rollback would be a security regression.
"""

from __future__ import annotations

import importlib.util
import json
import uuid
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import MagicMock, patch

from alembic.script import ScriptDirectory
from cryptography.fernet import Fernet

from modulo.core.secret_patterns import SENSITIVE_VALUE_MASK

_VERSIONS = Path(__file__).resolve().parents[3] / "src" / "modulo" / "db" / "migrations" / "versions"
_MIGRATION_NAME = "0306_trigger_secret_encryption"
_MIGRATION_PATH = _VERSIONS / f"{_MIGRATION_NAME}.py"
_DOWN_REVISION = "0305_guardrail_escalate_action"
_CHAIN_HEAD_MIGRATION = "0319_evidence_redaction_rules"

_KEY = "c3Nzc3Nzc3Nzc3Nzc3Nzc3Nzc3Nzc3Nzc3Nzc3Nzc3M="  # url-safe base64 of 32 b's — a valid deterministic Fernet key
_FERNET = Fernet(_KEY.encode())
_TOKEN = _FERNET.encrypt(b"plaintext").decode()


def _load_migration() -> ModuleType:
    assert _MIGRATION_PATH.exists(), f"Migration file missing: {_MIGRATION_PATH}"
    spec = importlib.util.spec_from_file_location(f"migration_{_MIGRATION_NAME}", _MIGRATION_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fake_rows(configs: list[dict[str, Any]]) -> list[MagicMock]:
    rows: list[MagicMock] = []
    for config in configs:
        obj = MagicMock()
        obj.id = uuid.uuid4()
        obj.config_json = config
        rows.append(obj)
    return rows


def _update_calls(bind: MagicMock) -> list[tuple[str, dict[str, Any]]]:
    """The bind's UPDATE calls as (statement-text, params) pairs."""
    calls: list[tuple[str, dict[str, Any]]] = []
    for c in bind.execute.call_args_list:
        if len(c.args) < 2:
            continue
        calls.append((str(getattr(c.args[0], "text", c.args[0])), dict(c.args[1])))
    return calls


def _bind(configs: list[dict[str, Any]]) -> MagicMock:
    """A mocked ``bind`` as ``op.get_bind()`` returns.

    The SELECT (the type-agnostic ``->>`` candidate-narrowing statement)
    returns fake rows built from ``configs``; the mock records all later
    ``execute`` calls for assertion. The real-Postgres execution of that
    statement — and of the UPDATE — is covered by
    ``tests/integration/db/test_migration_0306_trigger_secret_encryption.py``.
    """
    bind = MagicMock()
    select_result = MagicMock()
    select_result.all.return_value = _fake_rows(configs)
    bind.execute.return_value = select_result
    return bind


def _run_upgrade(configs: list[dict[str, Any]]) -> tuple[MagicMock, ModuleType]:
    module = _load_migration()
    bind = _bind(configs)
    with (
        patch.object(module, "op") as op,
        patch.object(module, "_fernet_key", return_value=_KEY),
        patch("modulo.auth.secret_storage.encrypt_stored_secret") as encrypt,
    ):
        encrypt.side_effect = lambda value, key: Fernet(key.encode()).encrypt(value.encode())
        op.get_bind.return_value = bind
        module.upgrade()
    return bind, module


class TestChain:
    def test_single_head_is_0319_evidence_redaction_rules(self) -> None:
        heads = ScriptDirectory(str(_VERSIONS.parent)).get_heads()
        assert heads == [_CHAIN_HEAD_MIGRATION], f"expected a single head, got {heads}"

    def test_down_revision_is_0305_guardrail_escalate_action(self) -> None:
        assert _load_migration().down_revision == _DOWN_REVISION

    def test_revision_id_matches_filename(self) -> None:
        assert _load_migration().revision == _MIGRATION_NAME

    def test_no_branch_labels_or_depends_on(self) -> None:
        module = _load_migration()
        assert module.branch_labels is None
        assert module.depends_on is None


class TestUpgrade:
    def test_no_pending_rows_never_touches_the_fernet_key(self) -> None:
        """No candidates → no UPDATE at all and the key is not needed."""
        bind = _bind([])
        module = _load_migration()
        with (
            patch.object(module, "op") as op,
            patch.object(module, "_fernet_key", side_effect=AssertionError("key must not be needed")),
        ):
            op.get_bind.return_value = bind
            module.upgrade()
        assert not _update_calls(bind)

    def test_plaintext_rows_are_encrypted_with_other_keys_preserved(self) -> None:
        from cryptography.fernet import Fernet as _Fernet

        key = _KEY
        fern = _Fernet(key.encode())
        bind, _module = _run_upgrade(
            [{"url": "https://example.com", "hmac_secret": "hmac-value", "signing_secret": "signing-value"}]
        )
        updates = _update_calls(bind)
        assert len(updates) == 1, f"expected exactly one row update, got {updates}"
        stmt, params = updates[0]
        assert stmt.startswith("UPDATE triggers SET config_json")
        assert params["id"] is not None
        payload = json.loads(params["config"])
        assert payload["url"] == "https://example.com"
        assert payload["hmac_secret"].startswith("gAAAA")
        assert payload["signing_secret"].startswith("gAAAA")
        assert fern.decrypt(payload["hmac_secret"].encode()) == b"hmac-value"
        assert fern.decrypt(payload["signing_secret"].encode()) == b"signing-value"

    def test_second_run_over_encrypted_rows_writes_nothing(self) -> None:
        """Idempotency: after encryption a re-run performs zero UPDATEs."""
        bind, _module = _run_upgrade([{"hmac_secret": _TOKEN, "signing_secret": _TOKEN}])
        updates = _update_calls(bind)
        assert not updates, "already-encrypted rows must never be rewritten"

    def test_masked_sentinel_empty_and_nonstring_values_skip(self) -> None:
        bind, _module = _run_upgrade(
            [{"hmac_secret": SENSITIVE_VALUE_MASK}, {"hmac_secret": ""}, {"hmac_secret": 12345}]
        )
        updates = _update_calls(bind)
        assert not updates, "non-plaintext values must never be rewritten"

    def test_config_without_secret_keys_skips(self) -> None:
        bind, _module = _run_upgrade([{"url": "https://example.com"}])
        updates = _update_calls(bind)
        assert not updates

    def test_plaintext_mixed_rows_only_rewrites_secret_rows(self) -> None:
        bind, _module = _run_upgrade(
            [
                {"url": "https://example.com"},
                {"hmac_secret": "value", "events": ["a"]},
            ]
        )
        updates = _update_calls(bind)
        assert len(updates) == 1
        payload = json.loads(updates[0][1]["config"])
        assert payload["events"] == ["a"]

    def test_unencryptable_value_aborts_fail_closed(self) -> None:
        """A value the encryptor rejects aborts the migration BEFORE any write."""
        module = _load_migration()
        bind = _bind([{"hmac_secret": "value"}])
        with (
            patch.object(module, "op") as op,
            patch.object(module, "_fernet_key", return_value=_KEY),
            patch("modulo.auth.secret_storage.encrypt_stored_secret", side_effect=ValueError("bad")),
        ):
            op.get_bind.return_value = bind
            try:
                module.upgrade()
            except ValueError:
                expected_abort = True
            else:
                expected_abort = False
        assert expected_abort
        assert not _update_calls(bind)


class TestVocabularyParity:
    def test_secret_key_set_matches_the_canonical_helper(self) -> None:
        from modulo.api.routes.triggers import _SECRET_CONFIG_KEYS

        module = _load_migration()
        assert module._SECRET_CONFIG_KEYS == _SECRET_CONFIG_KEYS
        assert {"hmac_secret", "signing_secret"} == _SECRET_CONFIG_KEYS


class TestDowngrade:
    def test_downgrade_is_a_deliberate_noop(self) -> None:
        module = _load_migration()
        with patch.object(module, "op") as op:
            module.downgrade()
        assert op.get_bind.called is False
