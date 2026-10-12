"""FAR-1688: the per-eval policy-gate advisory lock has ONE derivation.

The declarative graph writer (``modulo.db.crud.pipeline``) and the in-product
authoring routes (``modulo.api.routes.evals``) must serialise on the SAME
transaction-scoped advisory lock. Before this change each module carried its
own copy of the key/SQL, so an edit to one would silently desynchronise the
shared lock key. These tests pin the single derivation both layers import.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

from modulo.api.routes import evals
from modulo.db.crud import pipeline
from modulo.db.crud._gate_advisory_lock import (
    GATE_LOCK_KEY_PREFIX,
    policy_gate_lock_key,
    with_gate_advisory_lock,
)

_EVAL_ID = uuid.UUID("660e8400-e29b-41d4-a716-446655440001")


def test_lock_key_derivation_is_canonical() -> None:
    assert GATE_LOCK_KEY_PREFIX == "policy_gate:"
    assert policy_gate_lock_key(_EVAL_ID) == f"policy_gate:{_EVAL_ID}"


def test_both_layers_share_the_one_helper() -> None:
    # Identity, not equality: a re-introduced local copy on either side would
    # no longer be the SAME function object, so this would fail — the
    # regression this test exists to catch.
    assert pipeline.with_gate_advisory_lock is with_gate_advisory_lock
    assert evals.with_gate_advisory_lock is with_gate_advisory_lock


async def test_helper_locks_on_the_derived_key() -> None:
    session = AsyncMock()

    async def _fn() -> str:
        return "gate"

    result = await with_gate_advisory_lock(_EVAL_ID, session, _fn)  # type: ignore[arg-type]

    assert result == "gate"
    # statement_timeout, then the advisory lock keyed on the derivation.
    assert session.execute.await_count == 2
    lock_call = session.execute.await_args_list[1]
    assert lock_call.args[1] == {"key": f"policy_gate:{_EVAL_ID}"}
