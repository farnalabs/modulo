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
"""

from __future__ import annotations

from modulo.db.models.base import Base, SoftDeleteMixin


def test_every_deleted_at_model_inherits_soft_delete_mixin() -> None:
    """Enumerate all registered mappers and check the invariant."""
    violations: list[str] = []

    for mapper in Base.registry.mappers:
        cls = mapper.class_

        # Skip abstract classes and the mixin itself
        if getattr(cls, "__abstract__", False) or cls is SoftDeleteMixin:
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


def test_soft_delete_mixin_provides_deleted_at() -> None:
    """Verify the mixin actually provides the deleted_at column."""
    assert hasattr(SoftDeleteMixin, "deleted_at"), "SoftDeleteMixin must provide a deleted_at attribute"
