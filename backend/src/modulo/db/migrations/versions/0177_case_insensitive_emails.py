"""Case-insensitive emails: backfill + functional unique index on accounts.

Revision ID: 0177_case_insensitive_emails
Revises: 0176_trigger_event_validation_results
Create Date: 2026-09-03

FAR-584: email addresses are case-insensitive in the real world. This migration
converts the ``accounts.email`` key from case-sensitive uniqueness to
case-insensitive uniqueness, in three ordered steps:

1. **Collision guard (fail-loud, no silent merges)** — BEFORE any write, the
   migration detects rows whose ``lower(trim(email))`` values collide and
   REFUSES to upgrade, raising with the colliding address list. This is a
   documented product decision: accounts are never silently merged. The guard
   must run first because the backfill (step 2) would otherwise violate the
   still-active case-sensitive ``accounts_email_key`` constraint mid-UPDATE —
   a raw UniqueViolation instead of an actionable operator message. The guard
   counts INACTIVE (deactivated) rows too — deactivation alone does not clear
   it. To resolve, an operator must fix the data outside the application
   (there is NO admin-UI rename for account emails): rename one of the
   colliding rows via SQL, or delete the duplicate row (deactivate first if
   tooling requires, then DELETE), then re-run the upgrade.
2. **Backfill** — every stored email is canonicalised in place
   (``lower(trim(email))``), matching the ``normalize_email`` boundary
   normalisation the application now applies at every account write. Safe
   after the guard: the remaining rows are distinct case-insensitively, so
   canonicalising them cannot violate the case-sensitive constraint.
3. **Index swap** — the case-sensitive unique constraint
   ``accounts_email_key`` is dropped and the functional unique index
   ``uq_accounts_email_lower`` (``UNIQUE (LOWER(email))``) is created. The
   ORM model mirrors the functional index, keeping schema parity.

Unicode note: Python's ``str.lower()`` and Postgres ``LOWER()`` can disagree
for exotic non-ASCII local parts (e.g. dotted/dotless İ under some
collations). Emails are treated as ASCII-canonical in practice; if a
pathological Unicode case-fold mismatch ever produces one, it surfaces as a
uniqueness IntegrityError (mapped to 409) rather than a silent duplicate.

Downgrade notes: the downgrade restores the case-sensitive
``accounts_email_key`` constraint and drops the functional index. The
backfill is NOT reversed — pre-upgrade mixed-case spellings stay lowercased
(the original casing is unrecoverable). Downgrade cannot fail on uniqueness:
the upgrade's collision guard guarantees the stored emails are distinct
case-insensitively, which implies they are distinct case-sensitively.
"""

from __future__ import annotations

import logging

from alembic import op
from sqlalchemy import text

logger = logging.getLogger(__name__)

revision: str = "0177_case_insensitive_emails"
down_revision: str | None = "0176_trigger_event_validation_results"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

_ORIGINAL_CONSTRAINT = "accounts_email_key"
_FUNCTIONAL_INDEX = "uq_accounts_email_lower"


def upgrade() -> None:
    bind = op.get_bind()

    # 1. Collision guard — fail LOUD before ANY write. Never silently merge
    # accounts: list the colliding addresses and tell the operator how to fix
    # it. Runs before the backfill because canonicalising case-variant
    # duplicates would otherwise violate the still-active case-sensitive
    # constraint mid-UPDATE (a raw UniqueViolation, not an operator message).
    collisions = (
        bind.execute(
            text(
                "SELECT lower(trim(email)) AS canonical_email, count(*) AS rows "
                "FROM public.accounts GROUP BY 1 HAVING count(*) > 1 ORDER BY canonical_email"
            )
        )
        .mappings()
        .all()
    )
    if collisions:
        colliding = ", ".join(f"{row['canonical_email']!r} x{row['rows']}" for row in collisions)
        raise RuntimeError(
            "Migration 0177_case_insensitive_emails found existing accounts whose emails "
            f"collide case-insensitively: {colliding}. Emails are now case-insensitive, so these "
            "accounts cannot coexist. Resolve BEFORE upgrading, outside the application — there "
            "is NO admin-UI rename for account emails: rename one of the colliding rows via SQL "
            "(an UPDATE of accounts.email to a fresh, still-unique address), or delete the "
            "duplicate row (deactivate it first if tooling requires, then DELETE). Note the "
            "guard counts INACTIVE (deactivated) rows too — deactivating a duplicate does NOT "
            "clear this guard. No accounts were merged or modified by this failure."
        )

    # 2. Backfill: canonicalise every stored email in place. Safe after the
    # guard — the remaining rows are distinct case-insensitively.
    result = bind.execute(
        text("UPDATE public.accounts SET email = lower(trim(email)) WHERE email != lower(trim(email))")
    )
    logger.info("0177 emails: canonicalised %d account email(s)", result.rowcount)

    # 3. Swap the case-sensitive unique constraint for the functional unique index.
    bind.execute(text(f"ALTER TABLE public.accounts DROP CONSTRAINT IF EXISTS {_ORIGINAL_CONSTRAINT}"))
    bind.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_accounts_email_lower ON public.accounts (LOWER(email));"))
    logger.info("0177 emails: dropped %s, created %s on public.accounts", _ORIGINAL_CONSTRAINT, _FUNCTIONAL_INDEX)


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(text("DROP INDEX IF EXISTS uq_accounts_email_lower;"))
    bind.execute(text(f"ALTER TABLE public.accounts ADD CONSTRAINT {_ORIGINAL_CONSTRAINT} UNIQUE (email)"))
    logger.info("0177 emails downgrade: restored %s (backfilled lowercasing is NOT reversed)", _ORIGINAL_CONSTRAINT)
