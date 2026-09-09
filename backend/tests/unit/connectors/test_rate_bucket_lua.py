"""Compile-check the Redis rate-limit Lua script (FAR-439).

The shared per-destination limiter's correctness lives entirely in
``_CONSUME_LUA`` (atomic server-side token bucket). ``redis-py`` compiles that
script *lazily* on first ``consume()``, so a syntax defect (e.g. a duplicate
``end``) does not fail import or unit tests that stub Redis — it surfaces at
runtime as a Redis compile error that propagates to
``SharedBudgetUnavailableError``, failing CLOSED every Redis-governed outbound
REST call. This module compiles the script up front through a real Lua VM so
such a regression is caught at unit time, not in production.
"""

from __future__ import annotations

import importlib.util

import pytest

from modulo.connectors._rate_bucket import _CONSUME_LUA


def _compile_via_lupa() -> str | None:
    """Return an error string if the script fails to compile, else ``None``.

    Uses ``lupa`` (a real embedded Lua VM). ``loadstring`` exists in Lua 5.1 /
    LuaJIT; Lua 5.2+ renamed it to ``load``. We accept whichever the runtime
    provides so the check is robust across lupa builds.
    """
    if importlib.util.find_spec("lupa") is None:
        return None  # signal "no Lua VM available"
    from lupa import LuaRuntime

    lua = LuaRuntime(unpack_returned_tuples=True)
    check = (
        "function(s)\n"
        "  local f, err\n"
        "  if loadstring then f, err = loadstring(s) else f, err = load(s) end\n"
        "  if not f then return tostring(err) else return nil end\n"
        "end\n"
    )
    return lua.eval(check)(_CONSUME_LUA)


@pytest.mark.skipif(
    importlib.util.find_spec("lupa") is None,
    reason="lupa (real Lua VM) not installed; cannot compile-check the Lua script",
)
def test_consume_lua_compiles() -> None:
    """``_CONSUME_LUA`` must be valid Lua — a syntax error fails closed at runtime."""
    err = _compile_via_lupa()
    assert err is None, f"_CONSUME_LUA failed to compile in the Lua VM: {err}"
