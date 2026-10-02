"""improve-database(oauth_token): client-revoke lookup indexes.

Revision ID: 0272_oauth_client_revoke_lookup_indexes
Revises: 0271_org_api_keys_revocation_sweep_indexes
Create Date: 2026-10-01

Covers ``backend/src/modulo/db/models/oauth_token.py``
(``oauth_authorization_codes``, ``oauth_consent_states``,
``oauth_token_families``), an area with no prior improve-database visit.
Existing coverage is single-column only (0001/0108/0142/0164):
``(client_id)``, ``(organisation_id)``, ``(account_id)`` on the codes and
families tables, ``(organisation_id)`` / ``(account_id)`` on the consent
states, plus the S256 ``CHECK`` and RLS (0108 consent states, 0110/0163
codes + families). Two read paths still filter on column pairs with no
composite to serve them:

* ``ix_oauth_auth_codes_org_client`` — the client-revoke DELETE
  (``auth/oauth.py::delete_oauth_client``,
  ``organisation_id = $1 AND client_id = $2``) ran on a bitmap-AND of the
  single-column org/client indexes over every live code in the org.
* ``ix_oauth_token_families_org_client`` — the symmetric revoke DELETE
  for token families in the same function; same bitmap-AND shape.

The per-request hot paths need nothing: the code redemption lookup is by
PK ``code`` (``WITH FOR UPDATE``), the consent claim is by PK ``state``
(``state = $1 AND consumed = false AND expires_at > now()``), and the
family rotation check is by PK ``family_id`` — each already pinpoints a
single row. ``expires_at`` / ``used`` / ``consumed`` / ``is_blacklisted``
are checked in Python post-fetch, never in a DB sweep predicate (no
housekeeping scan touches these tables), so no TTL indexes. No FK from
``client_id`` to ``oauth_clients``: the application deletes codes and
families explicitly before the client row, and the 10-minute code TTL
bounds any orphan window — recorded as a deferred gap instead.

Additive indexes only — no column/table changes. Same
``CREATE INDEX IF NOT EXISTS`` pattern as 0128/0155/0267/0271 (Alembic
wraps each revision in a transaction, so ``CONCURRENTLY`` is unavailable).
Both indexes lead on ``organisation_id`` (all three tables are RLS
org-isolated, so the tenant column must be the index prefix).
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision: str = "0272_oauth_client_revoke_lookup_indexes"
down_revision: str | None = "0271_org_api_keys_revocation_sweep_indexes"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

_INDEXES = [
    (
        "ix_oauth_auth_codes_org_client",
        (
            "CREATE INDEX IF NOT EXISTS ix_oauth_auth_codes_org_client "
            'ON public."oauth_authorization_codes" (organisation_id, client_id);'
        ),
    ),
    (
        "ix_oauth_token_families_org_client",
        (
            "CREATE INDEX IF NOT EXISTS ix_oauth_token_families_org_client "
            'ON public."oauth_token_families" (organisation_id, client_id);'
        ),
    ),
]


_DROPS = [
    "DROP INDEX IF EXISTS ix_oauth_auth_codes_org_client;",
    "DROP INDEX IF EXISTS ix_oauth_token_families_org_client;",
]


def upgrade() -> None:
    bind = op.get_bind()
    for _name, stmt in _INDEXES:
        bind.execute(text(stmt))


def downgrade() -> None:
    bind = op.get_bind()
    for stmt in _DROPS:
        bind.execute(text(stmt))
