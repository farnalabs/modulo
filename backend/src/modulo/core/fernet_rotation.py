"""Fernet key rotation service — re-encrypts all encrypted data stores with a new key.

No-downtime: during rotation, reads fall back to the old key if the new key fails.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

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


async def _rotate_otel_config(session: AsyncSession, new_fernet: Fernet, old_fernet: Fernet | None) -> int:
    """Re-encrypt langsmith_api_key_ciphertext inside otel_config_json on organisations."""
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
        config["langsmith_api_key_ciphertext"] = new_ct
        await session.execute(
            text("UPDATE organisations SET otel_config_json = :config WHERE id = :id"),
            {"config": json.dumps(config), "id": row.id},
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

    Iterates all 8 data stores and re-encrypts each row with *new_key*,
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
    # then notifications, then observability, then checkpoints.
    rotators: list[tuple[str, Any]] = [
        ("secrets", _rotate_secrets_table),
        ("connector_instances", _rotate_connector_instances),
        ("model_backends", _rotate_model_backends),
        ("notification_endpoints", _rotate_notification_endpoints),
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
