"""Global soft-delete scoping via ORM ``do_orm_execute`` listener.

Mechanism
---------
Every ORM SELECT that targets a model inheriting ``SoftDeleteMixin`` automatically
receives a ``WHERE deleted_at IS NULL`` clause via ``with_loader_criteria``.
This means any new read path automatically excludes soft-deleted rows without
requiring an explicit hand-written predicate — the ~100 existing
``deleted_at.is_(None)`` predicates become redundant but harmless.

Opt-out
-------
Legitimate read paths that *must* see soft-deleted rows (restore flows,
historical lookups, purge operations, version resolution) opt out via:

    from modulo.db.soft_delete import include_soft_deleted

    stmt = select(Model).where(...)
    stmt = include_soft_deleted(stmt)

Or equivalently via the execution_options API directly:

    stmt = stmt.execution_options(include_deleted=True)

The opt-out is scoped to the individual statement and does not leak across
concurrent operations on the same session.

Precedent
---------
This mirrors the existing ``register_tenant_filter`` pattern in
``modulo.db.rls``, which injects ``WHERE organisation_id = :oid`` via the same
``do_orm_execute`` event. The soft-delete filter is NOT backend-gated (unlike
the tenant filter, which is skipped on Postgres because RLS handles it) —
soft-delete is an application-level concern that applies identically to every
backend.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from sqlalchemy import event
from sqlalchemy.orm import ORMExecuteState, with_loader_criteria
from sqlalchemy.orm import Session as SASession

if TYPE_CHECKING:
    from sqlalchemy.sql import Select

from modulo.db.models.base import SoftDeleteMixin

_log = logging.getLogger(__name__)

_REGISTERED = False


def include_soft_deleted[T: Select[Any]](stmt: T) -> T:
    """Opt-out helper: mark *stmt* so the global soft-delete filter is skipped.

    Usage::

        stmt = include_soft_deleted(select(Model).where(Model.id == uid))

    Equivalently::

        stmt = select(Model).where(Model.id == uid).execution_options(include_deleted=True)
    """
    return stmt.execution_options(include_deleted=True)


def _apply_soft_delete_filter(orm_execute_state: ORMExecuteState) -> None:
    """``do_orm_execute`` listener that injects ``deleted_at IS NULL``.

    Only acts on SELECT statements. Skipped entirely when the statement carries
    ``execution_options(include_deleted=True)`` via :func:`include_soft_deleted`.
    """
    if not orm_execute_state.is_select:
        return

    if orm_execute_state.execution_options.get("include_deleted"):
        return

    stmt = orm_execute_state.statement
    orm_execute_state.statement = stmt.options(
        with_loader_criteria(
            SoftDeleteMixin,
            lambda cls: cls.deleted_at.is_(None),
            include_aliases=True,
            propagate_to_loaders=True,
        )
    )


def register_soft_delete_filter() -> None:
    """Register a ``do_orm_execute`` listener on the ORM ``Session`` class.

    Propagates to ``AsyncSession`` instances. Applies ``deleted_at IS NULL``
    automatically to every SELECT targeting a ``SoftDeleteMixin`` model.

    Idempotent — safe to call multiple times (the module-level ``_REGISTERED``
    flag prevents double-registration).
    """
    global _REGISTERED
    if _REGISTERED:
        return
    _REGISTERED = True

    _log.info("Registering global soft-delete filter")
    event.listen(SASession, "do_orm_execute", _apply_soft_delete_filter)
