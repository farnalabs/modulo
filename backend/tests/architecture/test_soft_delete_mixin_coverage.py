"""Architecture gate: every model with a ``deleted_at`` column must inherit SoftDeleteMixin.

The global soft-delete filter (``do_orm_execute`` listener in
``modulo.db.soft_delete``) only covers models that inherit
:class:`~modulo.db.models.base.SoftDeleteMixin`.  A model that declares its own
``deleted_at`` column *without* the mixin escapes the global filter — the column
exists in the DB but the ``WHERE deleted_at IS NULL`` injection never fires,
creating a silent divergence between models that are filtered and those that are
not.

This test enumerates every mapper registered against the app
:class:`~modulo.db.models.base.Base` and asserts that any mapped class with a
``deleted_at`` column is also a subclass of ``SoftDeleteMixin``.  If this test
fails, the offending model must either:

1. inherit ``SoftDeleteMixin`` (preferred — the column type already matches), or
2. be explicitly excluded with a comment explaining why.

Added 2026-09-20 as part of FAR-1025 fix pass.

FAR-1025 verification fix (2026-09-20): the original gate used
``getattr(cls, "__abstract__", False)`` which inherits ``True`` from
``OrgScoped.__abstract__`` into every concrete subclass, making the gate
skip all ~20 OrgScoped-derived models.  Fixed to ``cls.__dict__.get()``
which only checks the class's own dict — the same check SQLAlchemy uses
to decide whether to register a mapper.
"""

from __future__ import annotations

from modulo.db.models.base import Base, SoftDeleteMixin


def test_every_deleted_at_model_inherits_soft_delete_mixin() -> None:
    """Enumerate all registered mappers and check the invariant.

    Uses ``cls.__dict__.get("__abstract__")`` (own-dict check) rather than
    ``getattr(cls, "__abstract__")`` (MRO walk).  ``OrgScoped`` declares
    ``__abstract__ = True`` in its own dict; every concrete subclass inherits
    it but does NOT override it, so ``getattr`` returns ``True`` and skips
    them — making the gate vacuous.  SQLAlchemy registers these concrete
    classes because it also uses the own-dict check.
    """
    violations: list[str] = []

    for mapper in Base.registry.mappers:
        cls = mapper.class_

        # Skip classes that THEMSELVES declare __abstract__ (own dict only).
        # The mixin itself is also excluded — it has deleted_at by design.
        if cls.__dict__.get("__abstract__") or cls is SoftDeleteMixin:
            continue

        # Check if the class (or any of its mapped columns) has deleted_at
        has_deleted_at = any(
            col.key == "deleted_at" for col in mapper.column_attrs if hasattr(col, "columns")
        ) or hasattr(cls, "deleted_at")

        if has_deleted_at and not issubclass(cls, SoftDeleteMixin):
            violations.append(
                f"{cls.__module__}.{cls.__qualname__} declares deleted_at but does not inherit SoftDeleteMixin"
            )

    assert not violations, (
        "The following models declare deleted_at without SoftDeleteMixin — "
        "the global soft-delete filter will NOT cover them:\n" + "\n".join(f"  - {v}" for v in violations)
    )


def test_gate_enumerates_known_orgscoped_models() -> None:
    """Load-bearing: prove the gate actually inspects concrete OrgScoped models.

    ``OrgScoped`` declares ``__abstract__ = True`` and all its concrete
    subclasses (EvalDefinition, ErrorNotificationRule, …) inherit it.  If
    someone re-introduces a broad ``getattr(cls, "__abstract__")`` skip,
    these models disappear from the enumeration and the gate becomes
    vacuous again.  This self-check catches that regression.
    """
    from modulo.db.models.error_notification_rule import ErrorNotificationRule
    from modulo.db.models.eval_definition import EvalDefinition

    enumerated_classes: set[type] = set()
    for mapper in Base.registry.mappers:
        cls = mapper.class_
        if cls.__dict__.get("__abstract__") or cls is SoftDeleteMixin:
            continue
        enumerated_classes.add(cls)

    # Both are concrete OrgScoped subclasses with deleted_at — must appear
    assert EvalDefinition in enumerated_classes, (
        "EvalDefinition is a concrete OrgScoped model with deleted_at but was "
        "not enumerated — the gate may have re-introduced a vacuous getattr skip"
    )
    assert ErrorNotificationRule in enumerated_classes, (
        "ErrorNotificationRule is a concrete OrgScoped model with deleted_at but was not enumerated"
    )


def test_soft_delete_mixin_provides_deleted_at() -> None:
    """Verify the mixin actually provides the deleted_at column."""
    assert hasattr(SoftDeleteMixin, "deleted_at"), "SoftDeleteMixin must provide a deleted_at attribute"
