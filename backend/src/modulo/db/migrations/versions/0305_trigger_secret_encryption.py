"""Encrypt plaintext trigger webhook secrets at rest (FAR-1732).

Revision ID: 0305_trigger_secret_encryption
Revises: 0304_metrics_staging_recorded_at_index
Create Date: 2026-10-13

Why
---
FAR-1732: the REST trigger route encrypts ``hmac_secret``/``signing_secret``
before persisting, but pre-existing deployment rows store those secrets as
PLAINTEXT inside ``triggers.config_json``. At-rest encryption is only real
once the historical rows are encrypted too.

What
---
A one-pass, IDEMPOTENT data migration over ``triggers.config_json``: every
row whose config carries ``hmac_secret`` or ``signing_secret`` as a plaintext
string is re-written with the value under a Fernet token. The skip semantics
mirror the canonical write-path helper
(:func:`modulo.api.routes.triggers._encrypt_trigger_config_secrets`) exactly,
via the same light primitives:

* non-string / empty values, the sensitive-value mask sentinel and values
  that are ALREADY Fernet tokens pass through untouched (a second run
  therefore performs zero UPDATEs);
* non-secret keys are preserved verbatim (the row update writes the full
  dict back with only the two secret keys changed);
* a value that cannot be encrypted ABORTS the migration (fail closed) — a
  half-encrypted secret store is never silently left behind.

No downgrade
------------
``downgrade()`` intentionally does nothing: un-encrypting secrets into
plaintext on rollback would be a security regression, not a schema
restoration, and the column shape is unchanged either way. The migration is
safe in both directions (encrypted rows just stay encrypted).

Key source
----------
The Fernet key comes from the running application settings
(``fernet_key``, the SAME key the write path encrypts with). It is resolved
lazily inside ``upgrade()`` so loading this module (e.g. mocked structural
tests, head checks) does not require settings; the key is only needed when
there is plaintext to encrypt.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from alembic import op
from sqlalchemy import text

revision: str = "0305_trigger_secret_encryption"
down_revision: str | None = "0304_metrics_staging_recorded_at_index"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SECRET_CONFIG_KEYS: frozenset[str] = frozenset({"hmac_secret", "signing_secret"})


def _fernet_key() -> str:
    """Resolve the at-rest Fernet key from application settings (test seam)."""
    from modulo.settings import get_settings

    return get_settings().fernet_key


def _candidate_rows(bind: Any) -> list[Any]:
    """Rows whose config_json carries either secret key.

    The SQL narrows by JSONB key existence on Postgres; non-Postgres dialects
    (the structural unit tests never have a real table) get the broad select
    and the Python-side skip semantics below do the real filtering.
    """
    if bind.dialect.name == "postgresql":
        stmt = text(
            "SELECT id, config_json FROM triggers WHERE config_json ? 'hmac_secret' OR config_json ? 'signing_secret'"
        )
    else:
        stmt = text("SELECT id, config_json FROM triggers WHERE config_json IS NOT NULL")
    return list(bind.execute(stmt).all())


def upgrade() -> None:
    from modulo.auth.secret_storage import _is_encrypted_token, encrypt_stored_secret
    from modulo.core.secret_patterns import SENSITIVE_VALUE_MASK

    bind = op.get_bind()
    rows = _candidate_rows(bind)

    # Filter to rows that actually need encryption. Python-side filtering is
    # the idempotency mechanism: second run, every candidate's secret keys are
    # already tokens/masked/absent → nothing is written.
    pending: list[tuple[Any, dict[str, Any]]] = []
    for row in rows:
        if not isinstance(row.config_json, dict):
            continue
        needs = False
        for key in _SECRET_CONFIG_KEYS:
            val = row.config_json.get(key)
            if isinstance(val, str) and val and val != SENSITIVE_VALUE_MASK and not _is_encrypted_token(val):
                needs = True
        if needs:
            pending.append((row.id, row.config_json))
    if not pending:
        return

    fernet_key = _fernet_key()

    # Phase 1 — encrypt every pending row's payload in memory. Any failure
    # here aborts BEFORE a single write, so a partially-encrypted store can
    # never be left behind.
    updated_payloads: list[tuple[Any, dict[str, Any]]] = []
    for row_id, config in pending:
        updated = dict(config)
        for key in _SECRET_CONFIG_KEYS:
            val = updated.get(key)
            if not (isinstance(val, str) and val and val != SENSITIVE_VALUE_MASK and not _is_encrypted_token(val)):
                continue
            # Fail closed: raise so the plaintext secret is never half-persisted.
            updated[key] = encrypt_stored_secret(val, fernet_key).decode()
        updated_payloads.append((row_id, updated))

    # Phase 2 — write the already-validated payloads.
    for row_id, updated in updated_payloads:
        bind.execute(
            text("UPDATE triggers SET config_json = :config WHERE id = :id"),
            {"config": json.dumps(updated), "id": row_id},
        )


def downgrade() -> None:
    # Deliberate no-op: encrypted secrets must never be rolled back into
    # plaintext, and the column shape is unchanged. See module docstring.
    pass
