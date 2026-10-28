"""Fernet key rotation service — re-encrypts all encrypted data stores with a new key.

No-downtime: during rotation, reads fall back to the old key if the new key fails.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from functools import partial
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.auth.secret_storage import _is_complete_fernet_token, is_encrypted_secret
from modulo.core.secret_patterns import TRIGGER_CONFIG_SECRET_KEYS

_log = logging.getLogger(__name__)


@dataclass
class RotationResult:
    """Result of a full rotation pass across all encrypted stores."""

    tables_processed: list[str] = field(default_factory=list)
    total_rows_reencrypted: int = 0
    details: dict[str, int] = field(default_factory=dict)


def decrypt_with_fallback(ciphertext: bytes, new_fernet: Fernet, old_fernet: Fernet | None) -> bytes:
    """Decrypt *ciphertext* with *new_fernet*, falling back to *old_fernet*."""
    try:
        return new_fernet.decrypt(ciphertext)
    except InvalidToken:
        if old_fernet is not None:
            return old_fernet.decrypt(ciphertext)
        raise


def re_encrypt_bytes(ciphertext: bytes, new_fernet: Fernet, old_fernet: Fernet | None) -> bytes:
    """Decrypt with fallback, re-encrypt with new key — returns bytes."""
    plaintext = decrypt_with_fallback(ciphertext, new_fernet, old_fernet)
    return new_fernet.encrypt(plaintext)


def re_encrypt_str(ciphertext_str: str, new_fernet: Fernet, old_fernet: Fernet | None) -> str:
    """Re-encrypt a Fernet token stored as UTF-8 string."""
    plaintext = decrypt_with_fallback(ciphertext_str.encode(), new_fernet, old_fernet)
    return new_fernet.encrypt(plaintext).decode()


# ── Table-specific rotation helpers ────────────────────────────────────────


async def _rotate_secrets_table(session: AsyncSession, new_fernet: Fernet, old_fernet: Fernet | None) -> int:
    """Re-encrypt all rows in the ``secrets`` table."""
    rows = (await session.execute(text("SELECT id, encrypted_value FROM secrets"))).all()
    count = 0
    for row in rows:
        new_ct = re_encrypt_bytes(row.encrypted_value, new_fernet, old_fernet)
        await session.execute(
            text("UPDATE secrets SET encrypted_value = :ct WHERE id = :id"),
            {"ct": new_ct, "id": row.id},
        )
        count += 1
    return count


async def _rotate_connector_instances(session: AsyncSession, new_fernet: Fernet, old_fernet: Fernet | None) -> int:
    """Re-encrypt credentials_ciphertext in connector_instances."""
    rows = (await session.execute(text("SELECT id, credentials_ciphertext FROM connector_instances"))).all()
    count = 0
    for row in rows:
        new_ct = re_encrypt_bytes(row.credentials_ciphertext, new_fernet, old_fernet)
        await session.execute(
            text("UPDATE connector_instances SET credentials_ciphertext = :ct WHERE id = :id"),
            {"ct": new_ct, "id": row.id},
        )
        count += 1
    return count


async def _rotate_model_backends(session: AsyncSession, new_fernet: Fernet, old_fernet: Fernet | None) -> int:
    """Re-encrypt credentials_ciphertext in model_backends."""
    rows = (await session.execute(text("SELECT id, credentials_ciphertext FROM model_backends"))).all()
    count = 0
    for row in rows:
        new_ct = re_encrypt_bytes(row.credentials_ciphertext, new_fernet, old_fernet)
        await session.execute(
            text("UPDATE model_backends SET credentials_ciphertext = :ct WHERE id = :id"),
            {"ct": new_ct, "id": row.id},
        )
        count += 1
    return count


async def _rotate_notification_endpoints(session: AsyncSession, new_fernet: Fernet, old_fernet: Fernet | None) -> int:
    """Re-encrypt secret_ciphertext in notification_endpoints (nullable)."""
    rows = (
        await session.execute(
            text("SELECT id, secret_ciphertext FROM notification_endpoints WHERE secret_ciphertext IS NOT NULL")
        )
    ).all()
    count = 0
    for row in rows:
        new_ct = re_encrypt_bytes(row.secret_ciphertext, new_fernet, old_fernet)
        await session.execute(
            text("UPDATE notification_endpoints SET secret_ciphertext = :ct WHERE id = :id"),
            {"ct": new_ct, "id": row.id},
        )
        count += 1
    return count


def _reencrypt_trigger_secret_or_classify(
    stored: str,
    *,
    row_id: Any,
    key: str,
    new_fernet: Fernet,
    old_fernet: Fernet | None,
    new_key: str,
    old_key: str,
) -> str | None:
    """Classify ONE stored trigger secret and return its re-encrypted token.

    Returns the re-encrypted token when *stored* is ciphertext readable under
    the new key (or under the rotation window's old key), or ``None`` when it
    is legacy plaintext — the data migration's job, so rotation SKIPS it. A
    value the probe cannot decrypt that is nonetheless a structurally complete
    Fernet token is broken ciphertext: it fails LOUD (:class:`InvalidToken`)
    rather than being silently left behind.
    """
    if is_encrypted_secret(stored, new_key, old_key):
        return re_encrypt_str(stored, new_fernet, old_fernet)
    if _is_complete_fernet_token(stored):
        raise InvalidToken(
            f"trigger {row_id} config key {key}: stored value is a structurally complete "
            "Fernet token that neither the new nor the old key can decrypt"
        )
    return None


def _rotated_trigger_secrets(
    config: dict[str, Any],
    *,
    row_id: Any,
    new_fernet: Fernet,
    old_fernet: Fernet | None,
    new_key: str,
    old_key: str,
) -> dict[str, str]:
    """Return ``{config_key: re-encrypted token}`` for every secret that rotates.

    Iterates the CANONICAL shared key set
    (:data:`modulo.core.secret_patterns.TRIGGER_CONFIG_SECRET_KEYS`) — never a
    module-local list — so no secret key can drift out of rotation's reach.
    Non-secret, non-string, empty and legacy-plaintext values are omitted;
    broken ciphertext propagates :class:`InvalidToken` via
    :func:`_reencrypt_trigger_secret_or_classify`.
    """
    rotated: dict[str, str] = {}
    for key in TRIGGER_CONFIG_SECRET_KEYS:
        stored = config.get(key)
        if not isinstance(stored, str) or not stored:
            continue
        # Decryptability probe + fail-loud arm: a value the probe cannot
        # decrypt is plaintext UNLESS it is a structurally complete Fernet
        # token — in that case no configured key reads it and the broken
        # ciphertext must surface (InvalidToken), never be skipped silently.
        # A gAAAA-prefixed PLAINTEXT (incomplete token shape) is skipped,
        # never fed to the decryptor.
        new_ct = _reencrypt_trigger_secret_or_classify(
            stored,
            row_id=row_id,
            key=key,
            new_fernet=new_fernet,
            old_fernet=old_fernet,
            new_key=new_key,
            old_key=old_key,
        )
        if new_ct is not None:
            rotated[key] = new_ct
    return rotated


async def _rotate_triggers(
    session: AsyncSession,
    new_fernet: Fernet,
    old_fernet: Fernet | None,
    *,
    new_key: str,
    old_key: str = "",
) -> int:
    """Re-encrypt HMAC/signing secrets inside triggers.config_json (FAR-1732).

    Classification is the :func:`modulo.auth.secret_storage.is_encrypted_secret`
    DECRYPTABILITY probe (FAR-1735), never the ``gAAAA`` prefix heuristic:

    * a value that decrypts under the new key — or under the rotation
      window's old key — rotates;
    * a value that decrypts under NEITHER key but is a structurally complete
      Fernet token
      (:func:`modulo.auth.secret_storage._is_complete_fernet_token`) is a
      broken ciphertext: it fails LOUD (:class:`InvalidToken` propagates) —
      rotation surfaces an unreadable ciphertext row instead of silently
      leaving it behind;
    * any other value — including a plaintext secret that merely begins with
      ``gAAAA`` but is not a complete token — is LEGACY PLAINTEXT (the data
      migration's job) and is SKIPPED, so a ``gAAAA``-prefixed plaintext row
      can never be fed to the decryptor and abort the whole rotation run.

    The candidate keys are the CANONICAL shared set
    (:data:`modulo.core.secret_patterns.TRIGGER_CONFIG_SECRET_KEYS`, the same
    object the write path persists through). The SELECT is deliberately NOT
    narrowed by a key list in SQL: a second, module-local key list is exactly
    how a future secret key escapes — it would sit in the rotation loop while
    the WHERE clause never selected the rows carrying it as their only secret.
    Every row's ``config_json`` is checked in Python against the ONE shared
    set instead; ``triggers`` is a small config table and rotation is a rare
    admin operation, so the scan costs nothing next to the drift it removes.

    A row whose ``config_json`` is not a JSON OBJECT (a bare array or scalar,
    left behind by schema drift or a manual edit) is SKIPPED with a warning:
    it has no top-level keys, so it carries no secret to rotate, and one
    malformed row must never abort the sweep.

    FAR-1735 lost-update fix: the write is NARROWED to the secret keys via
    ``jsonb_set`` (one per rotated key) instead of rewriting the whole
    ``config_json`` document — a concurrent trigger edit between this
    function's SELECT and its UPDATE can no longer have its NON-secret keys
    clobbered by rotation.
    """
    rows = (await session.execute(text("SELECT id, config_json FROM triggers"))).all()
    count = 0
    for row in rows:
        config = row.config_json
        if not isinstance(config, dict):
            # ``config_json`` is a JSONB column, so schema drift or a manual
            # edit can leave a non-object document (a bare array/scalar). Such
            # a value cannot carry a TOP-LEVEL secret key, so there is nothing
            # to rotate — skip it and keep sweeping. The same schema-drift
            # guard migration 0306 and the webhook ingress route apply: a
            # malformed row must never AttributeError out the whole rotation.
            _log.warning("rotation.trigger_config_not_object", extra={"trigger_id": str(row.id)})
            continue
        rotated = _rotated_trigger_secrets(
            config,
            row_id=row.id,
            new_fernet=new_fernet,
            old_fernet=old_fernet,
            new_key=new_key,
            old_key=old_key,
        )
        if not rotated:
            continue
        for key, new_ct in rotated.items():
            # CAST(:ct AS text), NOT :ct::text — SQLAlchemy's bind-param
            # regex skips a ":name" immediately followed by "::" (it reads
            # the pair as a cast), leaving ":ct" literal in the SQL and
            # failing on real Postgres with "syntax error at or near ':'"
            # (caught by the integration test; the unit-tier SQL-text
            # assertion cannot see it).
            await session.execute(
                text(
                    "UPDATE triggers SET config_json = "
                    "jsonb_set(config_json, ARRAY[:key]::text[], to_jsonb(CAST(:ct AS text))) "
                    "WHERE id = :id"
                ),
                {"key": key, "ct": new_ct, "id": row.id},
            )
        count += 1
    return count


async def _rotate_otel_config(session: AsyncSession, new_fernet: Fernet, old_fernet: Fernet | None) -> int:
    """Re-encrypt langsmith_api_key_ciphertext inside otel_config_json on organisations.

    FAR-1832 QA F5: the write is NARROWED to the one rotated key via
    ``jsonb_set`` (the same pattern ``_rotate_triggers`` uses above) instead of
    rewriting the whole ``otel_config_json`` document from the SELECT's
    snapshot — an operator saving observability settings between this
    function's SELECT and its UPDATE can no longer have their other keys
    silently dropped by the rotation (the previous whole-column write made the
    docstring's "no interleaving writer's keys can be lost" claim false for
    this column). The SELECT still reads the whole document (it only needs the
    stored ciphertext to re-encrypt); only the WRITE is narrow.
    """
    rows = (
        await session.execute(
            text(
                "SELECT id, otel_config_json FROM organisations "
                "WHERE otel_config_json->>'langsmith_api_key_ciphertext' IS NOT NULL"
            )
        )
    ).all()
    count = 0
    for row in rows:
        config: dict[str, Any] = row.otel_config_json
        stored = config.get("langsmith_api_key_ciphertext")
        if not stored:
            continue
        new_ct = re_encrypt_str(stored, new_fernet, old_fernet)
        # CAST(:ct AS text), NOT :ct::text — SQLAlchemy's bind-param regex
        # skips a ":name" immediately followed by "::" (see _rotate_triggers).
        await session.execute(
            text(
                "UPDATE organisations SET otel_config_json = "
                "jsonb_set(COALESCE(otel_config_json, '{}'::jsonb), "
                "ARRAY['langsmith_api_key_ciphertext'], to_jsonb(CAST(:ct AS text))) "
                "WHERE id = :id"
            ),
            {"ct": new_ct, "id": row.id},
        )
        count += 1
    return count


async def _rotate_checkpoints(
    session: AsyncSession,
    new_fernet: Fernet,
    old_fernet: Fernet | None,
) -> int:
    """Re-encrypt checkpoint JSONB data (``__encrypted__`` wrapper)."""
    rows = (
        await session.execute(
            text(
                "SELECT organisation_id, thread_id, checkpoint_ns, checkpoint_id, checkpoint "
                "FROM checkpoints WHERE checkpoint->>'__encrypted__' = 'true'"
            )
        )
    ).all()
    count = 0
    for row in rows:
        wrapper: dict[str, Any] = row.checkpoint
        stored = wrapper.get("data", "")
        if not stored:
            continue
        new_ct = re_encrypt_str(stored, new_fernet, old_fernet)
        wrapper["data"] = new_ct
        await session.execute(
            text(
                "UPDATE checkpoints SET checkpoint = :checkpoint "
                "WHERE organisation_id = :org_id AND thread_id = :thread_id "
                "AND checkpoint_ns = :ns AND checkpoint_id = :ckpt_id"
            ),
            {
                "checkpoint": json.dumps(wrapper),
                "org_id": row.organisation_id,
                "thread_id": row.thread_id,
                "ns": row.checkpoint_ns,
                "ckpt_id": row.checkpoint_id,
            },
        )
        count += 1
    return count


async def _rotate_checkpoint_blobs(session: AsyncSession, new_fernet: Fernet, old_fernet: Fernet | None) -> int:
    """Re-encrypt BYTEA blobs in checkpoint_blobs."""
    rows = (
        await session.execute(
            text(
                "SELECT organisation_id, thread_id, checkpoint_ns, channel, version, blob "
                "FROM checkpoint_blobs WHERE blob IS NOT NULL"
            )
        )
    ).all()
    count = 0
    for row in rows:
        new_blob = re_encrypt_bytes(row.blob, new_fernet, old_fernet)
        await session.execute(
            text(
                "UPDATE checkpoint_blobs SET blob = :blob "
                "WHERE organisation_id = :org_id AND thread_id = :thread_id "
                "AND checkpoint_ns = :ns AND channel = :channel AND version = :version"
            ),
            {
                "blob": new_blob,
                "org_id": row.organisation_id,
                "thread_id": row.thread_id,
                "ns": row.checkpoint_ns,
                "channel": row.channel,
                "version": row.version,
            },
        )
        count += 1
    return count


async def _rotate_checkpoint_writes(session: AsyncSession, new_fernet: Fernet, old_fernet: Fernet | None) -> int:
    """Re-encrypt BYTEA blobs in checkpoint_writes."""
    rows = (
        await session.execute(
            text(
                "SELECT organisation_id, thread_id, checkpoint_ns, checkpoint_id, task_id, idx, blob "
                "FROM checkpoint_writes"
            )
        )
    ).all()
    count = 0
    for row in rows:
        new_blob = re_encrypt_bytes(row.blob, new_fernet, old_fernet)
        await session.execute(
            text(
                "UPDATE checkpoint_writes SET blob = :blob "
                "WHERE organisation_id = :org_id AND thread_id = :thread_id "
                "AND checkpoint_ns = :ns AND checkpoint_id = :ckpt_id "
                "AND task_id = :task_id AND idx = :idx"
            ),
            {
                "blob": new_blob,
                "org_id": row.organisation_id,
                "thread_id": row.thread_id,
                "ns": row.checkpoint_ns,
                "ckpt_id": row.checkpoint_id,
                "task_id": row.task_id,
                "idx": row.idx,
            },
        )
        count += 1
    return count


# ── Public rotation API ────────────────────────────────────────────────────


async def rotate_all_encrypted_data(
    session: AsyncSession,
    new_key: str,
    old_key: str = "",
) -> RotationResult:
    """Re-encrypt all Fernet-encrypted data across all stores.

    Iterates all 9 data stores and re-encrypts each row with *new_key*,
    falling back to *old_key* for decryption of existing data.

    Args:
        session: An active SQLAlchemy async session. Must be CROSS-ORG: pass a
            session on the ``modulo_system`` (BYPASSRLS) role. On the app role
            (``modulo_app``, NOBYPASSRLS) the org-scoped ``rls_org_isolation``
            policies on ``secrets``/``connector_instances``/``model_backends``/
            ``notification_endpoints`` have no ``app.organisation_id`` context,
            so those tables fail-closed to zero rows and the rotation silently
            no-ops. See ``admin_rotation._run_rotation_background``.
        new_key: The new Fernet key (32+ bytes).
        old_key: The previous Fernet key (32+ bytes), or empty string if none.

    Returns:
        A ``RotationResult`` with per-table counts.

    """
    new_fernet = Fernet(new_key.encode())
    old_fernet = Fernet(old_key.encode()) if old_key else None

    # Ordered: secrets first (most critical), then connectors/backends,
    # then notifications, then triggers, then observability, then checkpoints.
    # ``_rotate_triggers`` also needs the raw key STRINGS (its plaintext-vs-
    # ciphertext classification is the is_encrypted_secret decryptability
    # probe, which takes keys) — bound here so every other rotator keeps the
    # uniform ``(session, new_fernet, old_fernet)`` signature.
    rotators: list[tuple[str, Any]] = [
        ("secrets", _rotate_secrets_table),
        ("connector_instances", _rotate_connector_instances),
        ("model_backends", _rotate_model_backends),
        ("notification_endpoints", _rotate_notification_endpoints),
        ("triggers", partial(_rotate_triggers, new_key=new_key, old_key=old_key)),
        ("observability_config", _rotate_otel_config),
        ("checkpoints", _rotate_checkpoints),
        ("checkpoint_blobs", _rotate_checkpoint_blobs),
        ("checkpoint_writes", _rotate_checkpoint_writes),
    ]

    result = RotationResult()

    for table_name, rotator_fn in rotators:
        try:
            count = await rotator_fn(session, new_fernet, old_fernet)
            result.details[table_name] = count
            result.total_rows_reencrypted += count
            result.tables_processed.append(table_name)
            _log.info("rotation.table_complete", extra={"table": table_name, "rows": count})
        except Exception:
            _log.exception("rotation.table_failed", extra={"table": table_name})
            raise

    return result
