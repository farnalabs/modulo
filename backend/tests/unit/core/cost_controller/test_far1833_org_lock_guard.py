"""FAR-1833 structural guard: the finalize cost path must not couple on an
``organisations`` row lock.

The D9 deadlock was an FK ``FOR KEY SHARE`` on the ``organisations`` row (taken
implicitly by any insert that FKs the org) colliding with the spend-ceiling
gate's org-row row-lock, together with the ceiling endpoints' org-row write.
This guard pins the shape that replaced it: the finalize gate takes the per-org
cost-state advisory lock (``lock_org_cost_state``) and reads its ceilings from
``organisation_cost_state``; the ceiling endpoints write that row (under the same
advisory lock), never an org-row cost column.

This is a source-scanning guard in the token-set style of the FAR-1832
lock-timeout guard (``_WRITER_TOKENS`` / ``_KNOWN_WRITER_MODULES`` /
``_CLASSIFIER_NAME``). Note: ``tests/unit/api/test_far1832_lock_timeout.py`` does
not exist in this tree, so the shape is reproduced here rather than imported.
"""

from __future__ import annotations

import inspect
import io
import tokenize

from modulo.api.routes import costs as costs_routes
from modulo.core.cost_controller import finalize


def _code_only(src: str) -> str:
    """Strip comments and string literals (docstrings) so prose cannot false-positive.

    The module docstrings legitimately NAME the retired lock
    (``with_for_update(key_share=True)``) to explain why it is gone; the guard
    must scan executable code, not the explanation of its removal.
    """
    tokens = [
        tok.string
        for tok in tokenize.generate_tokens(io.StringIO(src).readline)
        if tok.type not in (tokenize.COMMENT, tokenize.STRING, tokenize.INDENT, tokenize.DEDENT, tokenize.NL)
    ]
    return "\n".join(tokens)


# Tokens that mark an SQLAlchemy row lock. The finalize ceiling gate must not
# contain either; the org-row write coupling is exactly what FAR-1833 removed.
_ORG_ROW_LOCK_TOKENS: tuple[str, ...] = ("with_for_update", "key_share")

# The replacement mechanism: the per-org cost-state advisory lock + the CRUD.
_COST_STATE_LOCK_TOKEN = "lock_org_cost_state"
_COST_STATE_READ_TOKEN = "get_org_cost_state"
_COST_STATE_WRITE_TOKEN = "upsert_org_cost_state"

# Org-row writers on the endpoint path. ``_cost_controls_patch`` mutates
# ``org.daily_spend_limit`` and ``update_org_settings_patch`` writes
# ``settings_json`` — both must happen AFTER the advisory.
_ORG_ROW_MUTATOR_CALL_TOKEN = "_cost_controls_patch"
_ORG_ROW_SETTINGS_CALL_TOKEN = "update_org_settings_patch"
_ORG_DAILY_SPEND_ATTR = "org.daily_spend_limit"


def _token_index(src: str, token: str) -> int:
    idx = src.find(token)
    assert idx != -1, f"expected token {token!r} in source"
    return idx


# Direct writes to the (now-dropped) organisations cost columns — the endpoints
# must go through ``upsert_org_cost_state`` instead.
_RETIRED_ORG_WRITE_TOKENS: tuple[str, ...] = (
    "org.max_run_cost_cents=",
    "org.spend_ceiling_cents=",
    "org.org_cumulative_spend_cents=",
)


def test_finalize_gate_takes_cost_advisory_lock_and_no_org_row_lock() -> None:
    """The ceiling gate locks the cost state, not the organisation row."""
    gate_src = _code_only(inspect.getsource(finalize._apply_spend_ceiling_gate))
    assert _COST_STATE_LOCK_TOKEN in gate_src, "the ceiling gate must take the cost-state advisory lock"
    assert _COST_STATE_READ_TOKEN in gate_src, "the ceiling gate must read the cost-state row"
    for token in _ORG_ROW_LOCK_TOKENS:
        assert token not in gate_src, f"the ceiling gate must not take an org row lock ({token!r})"


def test_finalize_module_has_no_retired_key_share_org_lock() -> None:
    """The FAR-1624 A22 ``with_for_update(key_share=True)`` org lock is gone."""
    src = _code_only(inspect.getsource(finalize))
    assert "key_share=True" not in src


def test_ceiling_endpoints_write_the_cost_state_row_not_the_org_row() -> None:
    """The admin ceiling endpoints read/write the cost-state row under the lock."""
    src = _code_only(inspect.getsource(costs_routes))
    assert _COST_STATE_WRITE_TOKEN in src, "ceiling writes must use upsert_org_cost_state"
    assert _COST_STATE_LOCK_TOKEN in src, "ceiling writes must take the cost-state advisory lock"
    assert _COST_STATE_READ_TOKEN in src, "ceiling reads must use get_org_cost_state"
    for token in _RETIRED_ORG_WRITE_TOKENS:
        assert token not in src, f"ceiling endpoints must not write org cost columns ({token!r})"


def test_update_cost_controls_takes_advisory_before_org_row_mutation() -> None:
    """P1 ORDERING guard — a pure token scan cannot see the lock-order inversion.

    ``update_cost_controls`` used to call ``_cost_controls_patch`` (which sets
    ``org.daily_spend_limit``) BEFORE ``lock_org_cost_state``; because the
    session autoflushes, that took the ``organisations`` row lock first and the
    advisory second — an ABBA cycle with finalize. This asserts the ORDERING
    directly: the advisory acquisition precedes every organisations-row mutation
    on the endpoint path.
    """
    src = _code_only(inspect.getsource(costs_routes.update_cost_controls))
    lock_at = _token_index(src, _COST_STATE_LOCK_TOKEN)
    patch_at = _token_index(src, _ORG_ROW_MUTATOR_CALL_TOKEN)
    settings_at = _token_index(src, _ORG_ROW_SETTINGS_CALL_TOKEN)
    assert lock_at < patch_at, (
        "the cost advisory must be acquired BEFORE _cost_controls_patch, which "
        "mutates the organisations row (org.daily_spend_limit) — the P1 lock-order inversion"
    )
    assert lock_at < settings_at, "the advisory must precede every organisations-row write"

    # ``_cost_controls_patch`` really does mutate the org row (so the ordering
    # above is load-bearing, not vacuous). Raw source: the assignment must exist.
    patch_src = inspect.getsource(costs_routes._cost_controls_patch)
    assert _ORG_DAILY_SPEND_ATTR in patch_src, (
        "_cost_controls_patch must mutate org.daily_spend_limit; if that moved, this "
        "guard's premise changed and must be revisited"
    )


def test_set_spend_ceiling_takes_advisory_before_cost_state_write() -> None:
    """The ceiling set/clear endpoint acquires the advisory before its write."""
    src = _code_only(inspect.getsource(costs_routes.set_spend_ceiling))
    lock_at = _token_index(src, _COST_STATE_LOCK_TOKEN)
    write_at = _token_index(src, _COST_STATE_WRITE_TOKEN)
    assert lock_at < write_at
