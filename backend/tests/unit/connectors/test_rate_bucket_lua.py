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


def _lua51_available() -> bool:
    """True when ``lupa`` and its Lua 5.1 runtime are importable."""
    return importlib.util.find_spec("lupa") is not None and importlib.util.find_spec("lupa.lua51") is not None


def _compile_via_lupa() -> str | None:
    """Return an error string if the script fails to compile, else ``None``.

    Uses ``lupa``'s embedded Lua **5.1** runtime — the Lua version Redis
    actually executes scripts with. Pinning the version is deliberate: ``lupa``
    2.x ships several Lua runtimes (5.1-5.5), and creating one runtime after a
    different one has already been loaded in the same process can segfault the
    interpreter. ``fakeredis`` (a test dependency used elsewhere in the unit
    suite) imports ``lupa.lua51`` at import time, so selecting lupa's default
    (5.5) here crashed the pytest-xdist worker whenever both modules ran in one
    process. Lua 5.1 also matches Redis, so it is the faithful VM for the
    check. ``loadstring`` exists in Lua 5.1 / LuaJIT; Lua 5.2+ renamed it to
    ``load``, so the runtime probe below keeps the check robust.
    """
    if not _lua51_available():
        return None  # signal "no Lua 5.1 VM available"
    from lupa import lua51

    lua = lua51.LuaRuntime(unpack_returned_tuples=True)
    check = (
        "function(s)\n"
        "  local f, err\n"
        "  if loadstring then f, err = loadstring(s) else f, err = load(s) end\n"
        "  if not f then return tostring(err) else return nil end\n"
        "end\n"
    )
    return lua.eval(check)(_CONSUME_LUA)


@pytest.mark.skipif(
    not _lua51_available(),
    reason="lupa Lua 5.1 VM not installed; cannot compile-check the Lua script",
)
def test_consume_lua_compiles() -> None:
    """``_CONSUME_LUA`` must be valid Lua — a syntax error fails closed at runtime."""
    err = _compile_via_lupa()
    assert err is None, f"_CONSUME_LUA failed to compile in the Lua VM: {err}"
