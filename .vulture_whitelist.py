# Vulture whitelist: framework-required symbols that vulture cannot resolve.
#
# These are NOT dead code — removing them breaks framework contracts. Each
# symbol is referenced only through a string annotation, a framework callback
# signature, or a route path parameter that is unused by design, so static
# analysis (vulture) reports it as dead while it is in fact load-bearing.
#
# Mechanism: vulture's documented whitelist is a Python file passed as an
# additional PATH argument. Names reach vulture's `used_names` set (which is
# what suppresses "unused" reports) when the whitelist module declares them in
# an `__all__` list — vulture special-cases `__all__ = [...]` in visit_Assign
# and adds every string element to used_names. A plain `ignored = [...]` list
# does NOT suppress anything; only `__all__` (or bare Load references) does.
# (Verified empirically against vulture 2.16: stub `def`/`class` definitions
# do NOT suppress findings — they only enter defined_funcs/defined_classes.)
#
# The names are intentionally undefined here (they exist in backend/src) —
# that is the whole point of a whitelist — so suppress ruff's F822 for this
# file. This file lives at the repo root, outside the backend/ ruff scan scope;
# the directive only protects against a future root-level lint.
# ruff: noqa: F822
__all__ = [
    "CursorResult",  # TYPE_CHECKING imports used as string annotations
    #   api/routes/admin_orgs.py:15 ("CursorResult[Any]" at :580)
    #   db/crud/token_family.py:13 ("CursorResult[Any]" at :144)
    "Dialect",  # TYPE_CHECKING import used in a cast string annotation
    "compiler",  # SQLAlchemy @compiles() hook callback params
    "element",  # SQLAlchemy @compiles() hook callback params
    #   db/models/run.py:77 and :82 (@compiles(_GenRandomUuid) callbacks)
    "input_str",  # LangChain callback interface params
    "inputs",  # LangChain callback interface params
    #   otel_bridge/handler.py:269 (on_chain_start), :436/:442 (on_tool_start)
    "q_or_none",  # documented placeholder param (seam parity)
    #   core/cron_helpers.py:1477
    "version_id",  # FastAPI path params (route shape, unused by design)
    #   api/routes/lifecycle_maps.py:975 and :1090 (routes /versions/{version_id})
]
