"""Bounded, explicit resolution of stored TRIGGER config secrets (FAR-1735).

Post-migration ``0306_trigger_secret_encryption`` (FAR-1732) every legitimate
stored trigger secret — ``hmac_secret`` / ``signing_secret`` inside
``triggers.config_json`` — is a Fernet token. The five delivery-time readers
(routes/slack.py, routes/webhooks.py x2, trigger_engine, slack_app_mention)
historically each carried their own unbounded "decrypt failed → use the raw
stored value" fallback, which silently accepted a future plaintext write and
used undecryptable ciphertext as if it were the secret.

This module owns ONE resolution policy for all of them:

* a value that is NOT Fernet-token-shaped is LEGACY PLAINTEXT (a pre-0306
  row, or a plaintext write that bypassed the encrypting write path). It is
  accepted so pre-migration deployments keep delivering — but with a LOUD,
  dated warning, so the bypass is surfaced in logs rather than silently
  accepted. The shim is bounded by :data:`LEGACY_PLAINTEXT_SHIM_SINCE`, the
  date migration 0306 shipped;
* a token-shaped value is decrypted via the scope-bound helper and FAILS
  CLOSED (:class:`~modulo.auth.secret_storage.SecretStorageError`) when it
  cannot be decrypted — ciphertext is never used as the secret.
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING

from modulo.auth.secret_storage import (
    _is_encrypted_token,
    decode_stored_secret_scoped,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

_log = logging.getLogger(__name__)

# The deploy date of migration 0306_trigger_secret_encryption (FAR-1732):
# after it ran, no legitimate plaintext trigger-secret row remains. The
# legacy-plaintext shim exists ONLY so pre-migration deployments keep
# delivering; anything seen after this date is a write-path bypass and is
# surfaced loudly, never silently accepted.
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
    """Resolve one stored trigger secret — bounded plaintext tolerance, fail-closed decrypt.

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
        The decrypted plaintext (or the legacy plaintext, shim-logged).

    Raises:
        SecretStorageError: A token-shaped value that cannot be decrypted
            (bad key, corrupt token, missing transaction/RLS context) — fail
            closed, never fall back to the ciphertext.
    """
    if not _is_encrypted_token(stored_raw):
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
    return await decode_stored_secret_scoped(session, stored_raw, fernet_key, org_id=org_id)
