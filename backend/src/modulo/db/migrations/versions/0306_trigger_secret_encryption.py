"""Encrypt plaintext trigger webhook secrets at rest (FAR-1732).

Revision ID: 0306_trigger_secret_encryption
Revises: 0305_guardrail_escalate_action
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
from typing import Any, TypeGuard

from alembic import op
from sqlalchemy import text

revision: str = "0306_trigger_secret_encryption"
down_revision: str | None = "0305_guardrail_escalate_action"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SECRET_CONFIG_KEYS: frozenset[str] = frozenset({"hmac_secret", "signing_secret"})


def _fernet_key() -> str:
    """Resolve the at-rest Fernet key from application settings (test seam)."""
    from modulo.settings import get_settings

    return get_settings().fernet_key


def _candidate_rows(bind: Any) -> list[Any]:
    """Rows whose config_json carries either secret key.

    The SQL narrows with the ``->>`` text-extraction operator (the same
    type-agnostic form ``core.fernet_rotation._rotate_triggers`` uses): it
    works on both ``json`` and ``jsonb`` columns, so the migration does not
    depend on 0147 having converted ``config_json`` to ``jsonb`` (the
    jsonb-only ``?`` operator would). ``->>`` returns NULL for an absent key,
    so ``IS NOT NULL`` matches exactly the rows that carry the key at any
    JSON type — the Python-side skip semantics below then filter to the
    rows that actually need encryption. Non-Postgres dialects (the
    structural unit tests never have a real table) run the same statement
    against their mock and get the broad behaviour for free.
    """
    stmt = text(
        "SELECT id, config_json FROM triggers "
        "WHERE config_json->>'hmac_secret' IS NOT NULL "
        "OR config_json->>'signing_secret' IS NOT NULL"
    )
    return list(bind.execute(stmt).all())


def _secret_needs_encryption(value: Any) -> TypeGuard[str]:
    """True (and narrows to ``str``) when *value* is a plaintext secret.

    Mirrors the canonical write-path helper exactly: only a non-empty plaintext
    string that is neither the sensitive-value mask sentinel nor an existing
    Fernet token needs encryption. Anything else (non-strings, empty strings,
    the mask, already-encrypted tokens) passes through untouched — which is
    what makes a second run write nothing (idempotency).
    """
    from modulo.auth.secret_storage import _is_encrypted_token
    from modulo.core.secret_patterns import SENSITIVE_VALUE_MASK

    return isinstance(value, str) and bool(value) and value != SENSITIVE_VALUE_MASK and not _is_encrypted_token(value)


def _config_needs_encryption(config: dict[str, Any]) -> bool:
    """True when *config* carries at least one plaintext secret key."""
    return any(_secret_needs_encryption(config.get(key)) for key in _SECRET_CONFIG_KEYS)


def _encrypt_config(config: dict[str, Any], fernet_key: str) -> dict[str, Any]:
    """Return *config* with every plaintext secret key replaced by a token.

    Fail closed: an encryptor rejection raises out of here, and because the
    caller encrypts every pending row in memory before writing any, the abort
    happens before a single UPDATE — a half-encrypted store is never persisted.
    """
    from modulo.auth.secret_storage import encrypt_stored_secret

    updated = dict(config)
    for key in _SECRET_CONFIG_KEYS:
        value = updated.get(key)
        if _secret_needs_encryption(value):
            updated[key] = encrypt_stored_secret(value, fernet_key).decode()
    return updated


def upgrade() -> None:
    bind = op.get_bind()
    rows = _candidate_rows(bind)

    # Filter to rows that actually need encryption. Python-side filtering is
    # the idempotency mechanism: second run, every candidate's secret keys are
    # already tokens/masked/absent → nothing is written.
    pending: list[tuple[Any, dict[str, Any]]] = [
        (row.id, row.config_json)
        for row in rows
        if isinstance(row.config_json, dict) and _config_needs_encryption(row.config_json)
    ]
    if not pending:
        return

    fernet_key = _fernet_key()

    # Phase 1 — encrypt every pending row's payload in memory. Any failure
    # here aborts BEFORE a single write, so a partially-encrypted store can
    # never be left behind.
    updated_payloads: list[tuple[Any, dict[str, Any]]] = [
        (row_id, _encrypt_config(config, fernet_key)) for row_id, config in pending
    ]

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
