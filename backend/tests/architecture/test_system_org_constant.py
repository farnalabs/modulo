"""Single-sourcing guard for the nil-UUID system sentinel org (FAR-1505).

``SYSTEM_ORG_ID`` (00000000-0000-0000-0000-000000000000) is the system /
no-tenant / instance sentinel organisation: public error ingest, org-less
system errors, the cron system context, and the ``token_families`` default.

Before FAR-1505 it was defined three times under two names — the canonical
``db.models.organisation`` constant (then named ``ORPHAN_ORG_ID``) plus two
locally re-typed literals (``core.cron_helpers.SYSTEM_ORG_ID`` and
``core.error_tracking.saq_hooks._SYSTEM_ORG_ID``). Three independent
definitions of one value is a drift hazard.

This module pins both halves of the fix:

1. Each dependent module's ``SYSTEM_ORG_ID`` is the SAME object as the
   canonical one (identity, not merely equality), so a re-typed literal —
   even a value-identical one — fails.
2. The nil-UUID literal appears ONLY in the canonical module, so any future
   re-declaration anywhere else fails immediately.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from types import ModuleType

from modulo.core import cron_helpers
from modulo.core.error_tracking import saq_hooks
from modulo.db.models import organisation

_NIL_UUID_LITERAL = "00000000-0000-0000-0000-000000000000"
_NIL_UUID = uuid.UUID(int=0)

# Modules that must IMPORT the canonical constant, never re-type the literal.
_DEPENDENT_MODULES = (cron_helpers, saq_hooks)


def _source_of(module: ModuleType) -> str:
    """Read the source file of *module* (fails loudly if unresolvable)."""
    file_attr = getattr(module, "__file__", None)
    assert file_attr is not None, f"{module!r} has no __file__"
    return Path(file_attr).read_text(encoding="utf-8")


def test_system_org_id_is_the_nil_uuid() -> None:
    """The canonical constant is the nil UUID the DB row was seeded with."""
    assert organisation.SYSTEM_ORG_ID == _NIL_UUID


def test_dependent_modules_reuse_the_canonical_constant() -> None:
    """cron_helpers and saq_hooks must expose the canonical OBJECT, not a copy.

    ``is`` (identity) rather than ``==``: a separately constructed
    ``uuid.UUID("00000000-0000-0000-0000-000000000000")`` compares equal but
    would mean the literal had been re-typed somewhere again.
    """
    for module in _DEPENDENT_MODULES:
        assert module.SYSTEM_ORG_ID is organisation.SYSTEM_ORG_ID, (
            f"{module.__name__}.SYSTEM_ORG_ID is not the canonical "
            f"modulo.db.models.organisation.SYSTEM_ORG_ID — import it instead "
            f"of re-declaring the nil-UUID (FAR-1505)."
        )


def test_nil_uuid_literal_lives_only_in_the_canonical_module() -> None:
    """No dependent module may carry its own copy of the nil-UUID literal."""
    for module in _DEPENDENT_MODULES:
        source = _source_of(module)
        assert _NIL_UUID_LITERAL not in source, (
            f"{module.__name__} re-types the nil-UUID literal — import "
            f"modulo.db.models.organisation.SYSTEM_ORG_ID instead (FAR-1505)."
        )


def test_canonical_module_defines_the_literal_exactly_once() -> None:
    """The canonical module holds the single definition — and only one."""
    assert _source_of(organisation).count(_NIL_UUID_LITERAL) == 1
