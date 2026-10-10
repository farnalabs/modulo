"""``modulo`` CLI package.

The published console script is ``modulo.cli.main:cli``. Importing
``modulo.cli.main`` imports this package FIRST (importing a submodule
always executes the parent package), so anything this module imports at
module scope lands on EVERY console-script invocation — including
``modulo apply``, which only talks to a remote instance over
``MODULO_URL`` + ``MODULO_API_KEY`` and needs neither the database nor
the settings graph.

FAR-1586: ``migrate_org`` imported ``modulo.db.session`` at module scope,
and that module builds its async engine AT IMPORT TIME via
``get_settings()`` — so merely importing the CLI demanded
``DATABASE_URL`` / ``SECRET_KEY`` / ``FERNET_KEY``. The names below are
therefore resolved LAZILY through PEP 562 ``__getattr__``: behaviour is
unchanged for every existing ``from modulo.cli import ...`` caller (the
first access imports the real module and caches the attribute in the
package namespace), but nothing is imported until it is actually used.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

_MIGRATE_ORG_MODULE = "modulo.cli.migrate_org"

if TYPE_CHECKING:
    from modulo.cli.break_glass import cli as break_glass_cli
    from modulo.cli.migrate_org import build_parser, cmd_export, cmd_import, main

__all__ = [
    "break_glass_cli",
    "build_parser",
    "cmd_export",
    "cmd_import",
    "main",
]

#: attribute name -> (module, attribute) resolved on first access.
_LAZY_ATTRS: dict[str, tuple[str, str]] = {
    "break_glass_cli": ("modulo.cli.break_glass", "cli"),
    "build_parser": (_MIGRATE_ORG_MODULE, "build_parser"),
    "cmd_export": (_MIGRATE_ORG_MODULE, "cmd_export"),
    "cmd_import": (_MIGRATE_ORG_MODULE, "cmd_import"),
    "main": (_MIGRATE_ORG_MODULE, "main"),
}


def __getattr__(name: str) -> Any:
    """Resolve a re-exported CLI symbol on first access (PEP 562)."""
    try:
        module_name, attr_name = _LAZY_ATTRS[name]
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
    import importlib

    value = getattr(importlib.import_module(module_name), attr_name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted({*globals(), *_LAZY_ATTRS})
