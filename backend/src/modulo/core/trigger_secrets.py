"""Explicit resolution of stored TRIGGER config secrets (FAR-1735).

Post-migration ``0306_trigger_secret_encryption`` (FAR-1732) every legitimate
stored trigger secret — ``hmac_secret`` / ``signing_secret`` inside
``triggers.config_json`` — is a Fernet token. The five delivery-time readers
(routes/slack.py, routes/webhooks.py x2, trigger_engine, slack_app_mention)
historically each carried their own unbounded "decrypt failed → use the raw
stored value" fallback, which silently accepted a future plaintext write and
used undecryptable ciphertext as if it were the secret.

This module owns ONE resolution policy for all of them:

* a value that DECRYPTS under the configured key is decrypted via the
  scope-bound helper and returned;
* a structurally complete Fernet token that does NOT decrypt is broken
  ciphertext and FAILS CLOSED (:class:`~modulo.auth.secret_storage.SecretStorageError`)
  — ciphertext is never used as the secret;
* anything else is LEGACY PLAINTEXT (a pre-0306 row, or a plaintext write
  that bypassed the encrypting write path). It is accepted so pre-migration
  deployments keep delivering — but with a LOUD, dated warning, so the
  bypass is surfaced in logs rather than silently accepted.

The legacy-plaintext shim is **warn-only**: it has NO cutoff date.
:data:`LEGACY_PLAINTEXT_SHIM_SINCE` (migration 0306's deploy date on
Farnalabs' own deployment) is interpolated into the warning so operators can
see how stale a plaintext row is, but it is deliberately NOT enforced as a
reject boundary — Modulo is self-hosted and every deployment runs migration
0306 at its own time, so a fixed literal date would wrongly refuse plaintext
rows for any customer who migrates after it. The loud warning is the
enforcement; the accept-forever behaviour is intentional.
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING

from modulo.auth.secret_storage import (
    _is_complete_fernet_token,
    decode_stored_secret_scoped,
    is_encrypted_secret,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

_log = logging.getLogger(__name__)

# The deploy date of migration 0306_trigger_secret_encryption (FAR-1732) on
# Farnalabs' own deployment. The legacy-plaintext shim is WARN-ONLY — this
# date is interpolated into the ``trigger_secret.legacy_plaintext`` warning so
# operators can see how stale a plaintext row is, but it is NOT enforced as a
# cutoff: self-hosted deployments run migration 0306 at their own time, so a
# hard reject against this fixed literal would break any customer who
# migrates later than the date it records.
LEGACY_PLAINTEXT_SHIM_SINCE = "2026-10-13"


async def resolve_trigger_secret_scoped(
    session: AsyncSession,
    *,
    stored_raw: str,
    fernet_key: str,
    org_id: uuid.UUID,
    trigger_id: uuid.UUID,
    config_key: str,
    context: str,
) -> str:
    """Resolve one stored trigger secret — warn-only plaintext shim, fail-closed decrypt.

    Classification is the :func:`is_encrypted_secret` DECRYPTABILITY probe —
    never the ``gAAAA`` prefix heuristic — so a legacy plaintext value that
    merely begins with ``gAAAA`` is correctly accepted as legacy plaintext
    (with the dated warning) instead of being fed to the decryptor and
    failing closed. A structurally complete Fernet token the configured key
    cannot decrypt still FAILS CLOSED via the scope-bound decode below.

    Args:
        session: Active request/DB session (the scope-bound decrypt enforces
            transaction + RLS org context).
        stored_raw: The raw stored value from ``config_json``.
        fernet_key: The at-rest Fernet key.
        org_id: The trigger's organisation (RLS scope for the decrypt).
        trigger_id: For the structured warning log.
        config_key: The config key (``hmac_secret`` / ``signing_secret``).
        context: Short reader identifier (``slack_route``, ``webhooks_route``,
            ``webhook_engine``, ``slack_engine``) for the warning log.

    Returns:
        The decrypted plaintext (or the legacy plaintext, shim-logged — the
        shim is warn-only and has no cutoff date; see the module docstring).

    Raises:
        SecretStorageError: A structurally complete Fernet token that cannot
            be decrypted (bad key, corrupt token, missing transaction/RLS
            context) — fail closed, never fall back to the ciphertext.
    """
    if is_encrypted_secret(stored_raw, fernet_key) or _is_complete_fernet_token(stored_raw):
        return await decode_stored_secret_scoped(session, stored_raw, fernet_key, org_id=org_id)
    _log.warning(
        "trigger_secret.legacy_plaintext trigger=%s secret=%s context=%s since=%s — "
        "stored trigger secret is not a Fernet token; expected only for rows written "
        "before migration 0306 (FAR-1732). A value here after that deploy means a "
        "write bypassed the encrypting write path.",
        trigger_id,
        config_key,
        context,
        LEGACY_PLAINTEXT_SHIM_SINCE,
    )
    return stored_raw
