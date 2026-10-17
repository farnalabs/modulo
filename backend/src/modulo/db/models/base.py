import uuid
from datetime import datetime
from typing import Final

from sqlalchemy import JSON, DateTime, ForeignKey, Uuid, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# SQL referential action shared by every nullable FK across the model layer:
# when the referenced row is deleted, the column is nulled rather than cascading.
# Single home so model modules import it instead of re-declaring the literal.
ONDELETE_SET_NULL: Final[str] = "SET NULL"

# Column type for every JSON column whose physical Postgres type is ``jsonb``
# because migration ``0147_json_to_jsonb_standardize`` promoted it (and for the
# pre-existing jsonb columns created as such). Generic ``JSON`` keeps SQLite and
# MariaDB working; the ``postgresql`` variant makes the ORM declare what the
# database actually holds.
#
# This matters beyond cosmetics: a column declared bare ``JSON`` over a physical
# ``jsonb`` column binds equality/containment predicates as ``$1::JSON`` and
# Postgres raises ``operator does not exist: jsonb = json``. FAR-1856 was
# exactly that failure on ``lifecycle_maps.content_json`` — a scanner swallowed
# the per-scanner exception and silently returned nothing in production.
#
# ONE definition (FAR-1878): import this alias rather than writing the
# ``JSON().with_variant(JSONB(), "postgresql")`` expression inline. The
# architecture gate
# ``tests/architecture/test_model_jsonb_column_parity.py`` enforces both the
# dialect parity of every 0147-converted column and this single-definition rule.
JSONB_COL: Final[JSON] = JSON().with_variant(JSONB(), "postgresql")


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.current_timestamp(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
        nullable=False,
    )


class SoftDeleteMixin:
    """Mixin that adds soft-delete support. Set deleted_at to mark as deleted.

    A global ORM ``do_orm_execute`` listener (registered by
    :func:`~modulo.db.soft_delete.register_soft_delete_filter`) automatically
    applies ``WHERE deleted_at IS NULL`` to every SELECT that targets a model
    inheriting this mixin. Call sites that legitimately need to see soft-deleted
    rows (restore, purge, historical lookups, version resolution) must opt out::

        from modulo.db.soft_delete import include_soft_deleted

        stmt = include_soft_deleted(select(Model).where(...))

    Or equivalently::

        stmt = select(Model).execution_options(include_deleted=True)

    **Every model with a ``deleted_at`` column MUST inherit this mixin.**
    The architecture test ``tests/architecture/test_soft_delete_mixin_coverage.py``
    enforces this invariant — a model declaring ``deleted_at`` without the mixin
    is a CI failure.
    """

    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, default=None)


class OrgScoped(Base, TimestampMixin):
    """Mixin for entities that belong to an organisation.

    All subclasses are automatically covered by RLS policy
    ``rls_org_isolation`` which filters on ``organisation_id``.
    """

    __abstract__ = True

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        primary_key=True,
        default=uuid.uuid4,
        # Server-side safety net (FAR-718): raw-SQL INSERTs that omit the id
        # column get a random v4 uuid instead of a NOT NULL violation. The ORM
        # still supplies client-side ids, which win whenever present.
        server_default=text("gen_random_uuid()"),
    )
    organisation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        ForeignKey("organisations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
