"""Shared break-glass expiry predicate builder (deliverable B, chunk 1).

Single source of truth for the "is a break-glass account deny-eligible" rule.
TWO named predicates are emitted from ONE builder (plan v17, "shared expiry
rule = ONE builder"):

* ``denied_predicate`` — TRUE when the account is deny-eligible: ``is_break_glass``
  AND (NULL-expiry OR expired OR deactivated OR inactive). The membership JOIN
  exclusion in ``resolve_role_from_membership`` uses ``NOT denied``.
* ``live_predicate`` — TRUE when the credential is currently usable:
  ``is_break_glass`` AND not deactivated AND unexpired AND active. This is the
  (B) login-hook gate predicate and the live-rows gauge predicate.

The builder emits both SQLAlchemy ORM expressions (for the JOIN) and rendered
PostgreSQL SQL (compiled with an explicit postgresql dialect) so future chunks
(login-hook CAS WHERE, watchdog, API-key deny) reuse the exact same rule
without duplicating it. The pure ``is_break_glass_denied`` /
``is_break_glass_live`` decision functions are DB-free and unit-testable.

The DB clock (``current_timestamp``) is authoritative for the SQL predicates;
an injected ``now`` expression is accepted for deterministic tests only.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import and_, or_
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql import ColumnElement
from sqlalchemy.sql.expression import text

from modulo.db.models.account import Account

#: The three break-glass columns. Single-sourced so the migration GRANT list,
#: the ORM model, and the boot-time column assertions can't drift apart.
BREAK_GLASS_COLUMNS = (
    "is_break_glass",
    "break_glass_expires_at",
    "break_glass_deactivated_at",
)


def _now_expr(now: ColumnElement | None) -> ColumnElement:
    """DB clock by default; injected expression for deterministic tests."""
    return now if now is not None else text("current_timestamp")


def denied_predicate(now: ColumnElement | None = None) -> ColumnElement[bool]:
    """SQLAlchemy predicate: TRUE when the ``accounts`` row is deny-eligible.

    Mirrors the pure ``is_break_glass_denied`` decision so every deny site and
    the JOIN share one rule. The deactivated/inactive branches are redundant
    with the JOIN's ``active IS TRUE`` guard for normal accounts, but including
    them here keeps the builder the single source of the expiry rule.
    """
    now_expr = _now_expr(now)
    return and_(
        Account.is_break_glass.is_(True),
        or_(
            Account.break_glass_expires_at.is_(None),
            Account.break_glass_expires_at <= now_expr,
            Account.break_glass_deactivated_at.isnot(None),
            Account.active.isnot(True),
        ),
    )


def live_predicate(now: ColumnElement | None = None) -> ColumnElement[bool]:
    """SQLAlchemy predicate: TRUE when the credential is currently usable."""
    now_expr = _now_expr(now)
    return and_(
        Account.is_break_glass.is_(True),
        Account.break_glass_deactivated_at.is_(None),
        Account.break_glass_expires_at.isnot(None),
        Account.break_glass_expires_at > now_expr,
        Account.active.is_(True),
    )


def render_sql(predicate: ColumnElement[bool]) -> str:
    """Render a predicate as explicit PostgreSQL SQL for raw-text call sites.

    Future chunks (login-hook CAS WHERE) compile the SAME builder output here
    so the SQL-text rule and the ORM JOIN rule can never diverge.
    """
    return str(predicate.compile(dialect=postgresql.dialect()))


def is_break_glass_denied(
    *,
    is_break_glass: bool,
    break_glass_expires_at: datetime | None,
    break_glass_deactivated_at: datetime | None,
    active: bool,
    now: datetime,
) -> bool:
    """Pure deny decision (no DB session). TRUE = deny-eligible at ``now``."""
    if not is_break_glass:
        return False
    if break_glass_deactivated_at is not None:
        return True
    if not active:
        return True
    if break_glass_expires_at is None:
        return True
    return break_glass_expires_at <= now


def is_break_glass_live(
    *,
    is_break_glass: bool,
    break_glass_expires_at: datetime | None,
    break_glass_deactivated_at: datetime | None,
    active: bool,
    now: datetime,
) -> bool:
    """Pure live decision (no DB session). TRUE = credential usable at ``now``."""
    if not is_break_glass:
        return False
    if break_glass_deactivated_at is not None:
        return False
    if not active:
        return False
    if break_glass_expires_at is None:
        return False
    return break_glass_expires_at > now
