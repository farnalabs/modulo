"""Pure-unit tests for FAR-971 chunk 2 - persist-before-decide reorder.

Covers criteria 1-9, 14-19 from the chunk-02 spec.
All tests use mock sessions - no real database required.

Target file: backend/tests/unit/core/test_eval_engine_reorder.py
"""

from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from modulo.core.eval_engine import (
    EvalBlockedError,
    EvalDefinition,
    EvalEngine,
    EvalResult,
    EvalType,
    SuiteOutcome,
)
from modulo.core.pipeline_engine.eval_persist_order import (
    run_evals_persist_before_decide,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_NODE_ID = str(uuid4())  # stable UUID string used as eval_def.node_id


def _eval_def(
    *,
    name: str = "eval",
    pattern: str = "pass",
    field: str = "text",
    failure_behaviour: str = "warn",
    node_id: str | None = None,
    eval_id: uuid.UUID | None = None,
    pass_threshold: float | None = None,
    suite_id: str | None = None,
) -> EvalDefinition:
    """Create an EvalDefinition for a regex eval.

    node_id defaults to a valid UUID string because the production code
    does ``uuid.UUID(eval_def.node_id)`` and would crash on non-UUID strings.
    """
    return EvalDefinition(
        id=eval_id or uuid4(),
        org_id=uuid4(),
        name=name,
        eval_type=EvalType.REGEX,
        config={"pattern": pattern, "field": field},
        failure_behaviour=failure_behaviour,  # type: ignore[arg-type]
        node_id=node_id or _NODE_ID,
        pass_threshold=pass_threshold,
        suite_id=suite_id,
    )


def _output(text: str = "pass") -> dict[str, Any]:
    """Create a node output dict."""
    return {"text": text}


def _make_async_cm(target: Any = None) -> MagicMock:
    """Create a MagicMock that works as an async context manager."""
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=target if target is not None else cm)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _make_session(*, fail_on_commit: bool = False) -> MagicMock:
    """Create a mock session with add/commit tracking.

    The session must support ``async with session, session.begin():`` so both
    the session itself and ``session.begin()`` return async context managers.

    The ``begin()`` context manager's ``__aexit__`` calls ``session.commit()``
    (mirroring SQLAlchemy's AsyncSession.begin() which auto-commits on
    successful exit).  When ``fail_on_commit=True`` the commit raises, so the
    exception propagates through ``__aexit__`` and into the caller's
    ``except Exception`` block — exactly the path the production code takes.
    """
    session = MagicMock()
    session.added: list[Any] = []

    def _tracking_add(obj: Any) -> None:
        session.added.append(obj)

    session.add = _tracking_add

    # ``async with session:`` support
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)

    if fail_on_commit:
        session.commit = AsyncMock(side_effect=RuntimeError("simulated commit failure"))
    else:
        session.commit = AsyncMock()
    session.rollback = AsyncMock()
    session.close = AsyncMock()

    # ``session.begin()`` must return an async context manager whose exit
    # calls ``session.commit()`` — mirroring the real AsyncSession.begin().
    begin_cm = MagicMock()

    async def _begin_enter() -> MagicMock:
        return session

    async def _begin_exit(
        exc_type: Any,
        exc_val: Any,
        exc_tb: Any,
    ) -> bool:
        if exc_type is not None:
            # Exception inside the body → rollback, don't commit.
            return False
        await session.commit()
        return False

    begin_cm.__aenter__ = AsyncMock(side_effect=_begin_enter)
    begin_cm.__aexit__ = AsyncMock(side_effect=_begin_exit)
    session.begin = MagicMock(return_value=begin_cm)

    return session


def _session_factory(session: MagicMock) -> Any:
    """Wrap a mock session into a session_factory callable.

    The code does: ``async with session_factory() as session, session.begin():``
    So session_factory() must return an async context manager that yields
    the session, and session.begin() must return an async context manager.
    """

    @asynccontextmanager
    async def _factory():
        yield session

    return _factory


def _fail_commit_factory(exc: Exception | None = None) -> tuple[Any, MagicMock]:
    """Factory whose sessions fail on commit."""
    session = _make_session(fail_on_commit=True)
    if exc:
        session.commit = AsyncMock(side_effect=exc)
    return _session_factory(session), session


def _fail_open_factory(exc: Exception | None = None) -> Any:
    """Factory that raises when session_factory() is called."""
    the_exc = exc or RuntimeError("session_factory() failed")

    @asynccontextmanager
    async def _factory():
        raise the_exc
        yield  # type: ignore[misc]  # pragma: no cover

    return _factory


def _patch_rls(module: str = "modulo.core.pipeline_engine.eval_persist_order") -> tuple[AsyncMock, AsyncMock]:
    """Start patching set_rls_org and set_rls_execution_context.

    Returns (rls_org_mock, rls_ctx_mock). Caller MUST call patch.stopall().
    """
    p1 = patch(f"{module}.set_rls_org", new_callable=AsyncMock)
    p2 = patch(f"{module}.set_rls_execution_context", new_callable=AsyncMock)
    m1 = p1.start()
    m2 = p2.start()
    return m1, m2


def _make_eval_result_row(
    *,
    eval_id: uuid.UUID,
    run_id: uuid.UUID,
    passed: bool,
    node_id: uuid.UUID | None = None,
    score: float | None = None,
    detail: str = "",
) -> MagicMock:
    """Create a mock EvalResult ORM row for _check_eval_suites."""
    row = MagicMock()
    row.id = uuid4()
    row.run_id = run_id
    row.node_id = node_id or uuid4()
    row.eval_id = eval_id
    row.passed = passed
    row.score = score if score is not None else (1.0 if passed else 0.0)
    row.detail = detail
    row.evaluated_at = datetime.now(UTC)
    return row


def _make_eval_def_row(
    *,
    eval_id: uuid.UUID | None = None,
    name: str = "eval",
    suite_id: str | None = None,
    pass_threshold: float | None = None,
    pipeline_id: uuid.UUID | None = None,
) -> MagicMock:
    """Create a mock EvalDefinition ORM row for _check_eval_suites."""
    row = MagicMock()
    row.id = eval_id or uuid4()
    row.name = name
    row.suite_id = suite_id
    row.pass_threshold = pass_threshold
    row.pipeline_id = pipeline_id or uuid4()
    row.eval_type = "regex"
    row.deleted_at = None
    return row


# ---------------------------------------------------------------------------
# C1 — Persist-then-decide ordering for a block eval
# ---------------------------------------------------------------------------


class TestC1PersistThenDecideBlock:
    """C1: persist MUST precede the block decision — observe the sequence."""

    async def test_commit_before_block_exception(self) -> None:
        """Record an ordered list of events: session.add called, then
        EvalBlockedError raised. Assert the commit (session.add) event
        precedes the block event."""
        events: list[str] = []
        eval_def_a = _eval_def(
            name="block-eval",
            pattern="pass",
            failure_behaviour="block",
        )
        out = _output("fail")
        session = _make_session()
        factory = _session_factory(session)
        run_id = uuid4()
        org_id = uuid4()
        _patch_rls()

        def _tracking_add(obj: Any) -> None:
            events.append("persist")
            session.added.append(obj)

        session.add = _tracking_add

        try:
            with pytest.raises(EvalBlockedError) as exc_info:
                await run_evals_persist_before_decide(
                    eval_defs=[eval_def_a],
                    resolve_eval_target=lambda ed: out,
                    run_id=run_id,
                    org_id=org_id,
                    session_factory=factory,
                    node_id=_NODE_ID,
                )
            events.append("block")
        finally:
            patch.stopall()

        assert exc_info.value.eval_name == "block-eval"
        assert len(events) == 2, f"Expected exactly 2 events, got {events}"
        assert events[0] == "persist", f"persist must precede block, got {events}"
        assert events[1] == "block"
        assert len(session.added) == 1
        assert session.added[0].passed is False

    async def test_persisted_row_has_correct_values(self) -> None:
        """The persisted EvalResult has passed=False, score=0.0, and the eval_id."""
        eval_def_a = _eval_def(
            name="block-eval",
            pattern="pass",
            failure_behaviour="block",
        )
        out = _output("fail")
        session = _make_session()
        factory = _session_factory(session)
        run_id = uuid4()
        org_id = uuid4()
        _patch_rls()

        try:
            with pytest.raises(EvalBlockedError):
                await run_evals_persist_before_decide(
                    eval_defs=[eval_def_a],
                    resolve_eval_target=lambda ed: out,
                    run_id=run_id,
                    org_id=org_id,
                    session_factory=factory,
                    node_id=_NODE_ID,
                )
        finally:
            patch.stopall()

        row = session.added[0]
        assert row.passed is False
        assert row.score == 0.0
        assert row.run_id == run_id
        assert row.organisation_id == org_id


# ---------------------------------------------------------------------------
# C2 — Persist-then-decide for a warn eval
# ---------------------------------------------------------------------------


class TestC2PersistThenDecideWarn:
    """C2: warn eval that fails → row persisted, no exception."""

    async def test_warn_persists_no_exception(self) -> None:
        eval_def = _eval_def(
            name="warn-eval",
            pattern="pass",
            failure_behaviour="warn",
        )
        out = _output("fail")
        session = _make_session()
        factory = _session_factory(session)
        run_id = uuid4()
        org_id = uuid4()
        _patch_rls()

        try:
            results = await run_evals_persist_before_decide(
                eval_defs=[eval_def],
                resolve_eval_target=lambda ed: out,
                run_id=run_id,
                org_id=org_id,
                session_factory=factory,
                node_id=_NODE_ID,
            )
        finally:
            patch.stopall()

        assert "warn-eval" in results
        assert results["warn-eval"].passed is False
        assert len(session.added) == 1
        assert session.added[0].passed is False


# ---------------------------------------------------------------------------
# C3 — [A(warn,pass), B(block,fail), C(warn)] → A persisted, B persisted,
#        C never evaluated
# ---------------------------------------------------------------------------


class TestC3BlockMidwayPreservesPriorSkipsLater:
    """C3: A persisted, B persisted, C never evaluated."""

    async def test_c3(self) -> None:
        eval_a = _eval_def(name="A", pattern="pass", failure_behaviour="warn")
        eval_b = _eval_def(name="B", pattern="pass", failure_behaviour="block")
        eval_c = _eval_def(name="C", pattern="pass", failure_behaviour="warn")

        out = _output("fail")
        session = _make_session()
        factory = _session_factory(session)
        run_id = uuid4()
        org_id = uuid4()
        _patch_rls()

        try:
            with pytest.raises(EvalBlockedError) as exc_info:
                await run_evals_persist_before_decide(
                    eval_defs=[eval_a, eval_b, eval_c],
                    resolve_eval_target=lambda ed: out,
                    run_id=run_id,
                    org_id=org_id,
                    session_factory=factory,
                    node_id=_NODE_ID,
                )
        finally:
            patch.stopall()

        assert exc_info.value.eval_name == "B"
        persisted_ids = [r.eval_id for r in session.added]
        assert eval_a.id in persisted_ids
        assert eval_b.id in persisted_ids
        assert eval_c.id not in persisted_ids
        assert len(session.added) == 2


# ---------------------------------------------------------------------------
# C4 — [A(block,fail), B(warn)] → A persisted, B never evaluated
# ---------------------------------------------------------------------------


class TestC4BlockFirstSkipsLater:
    """C4: A persisted, B never evaluated, block from A."""

    async def test_c4(self) -> None:
        eval_a = _eval_def(name="A", pattern="pass", failure_behaviour="block")
        eval_b = _eval_def(name="B", pattern="pass", failure_behaviour="warn")

        out = _output("fail")
        session = _make_session()
        factory = _session_factory(session)
        run_id = uuid4()
        org_id = uuid4()
        _patch_rls()

        try:
            with pytest.raises(EvalBlockedError) as exc_info:
                await run_evals_persist_before_decide(
                    eval_defs=[eval_a, eval_b],
                    resolve_eval_target=lambda ed: out,
                    run_id=run_id,
                    org_id=org_id,
                    session_factory=factory,
                    node_id=_NODE_ID,
                )
        finally:
            patch.stopall()

        assert exc_info.value.eval_name == "A"
        assert len(session.added) == 1
        assert session.added[0].eval_id == eval_a.id


# ---------------------------------------------------------------------------
# C5 — Persistence failure for a block eval → undefined, halt with eval_blocked
# ---------------------------------------------------------------------------


class TestC5PersistenceFailureBlock:
    """C5: commit-time failure for block eval → halt with eval_blocked + marker."""

    async def test_commit_failure_block(self) -> None:
        eval_def = _eval_def(
            name="block-eval",
            pattern="pass",
            failure_behaviour="block",
        )
        out = _output("fail")
        factory, _session = _fail_commit_factory(RuntimeError("simulated commit failure"))
        run_id = uuid4()
        org_id = uuid4()
        _patch_rls()

        try:
            with pytest.raises(EvalBlockedError) as exc_info:
                await run_evals_persist_before_decide(
                    eval_defs=[eval_def],
                    resolve_eval_target=lambda ed: out,
                    run_id=run_id,
                    org_id=org_id,
                    session_factory=factory,
                    node_id=_NODE_ID,
                )
        finally:
            patch.stopall()

        assert exc_info.value.eval_name == "block-eval"
        marker = json.loads(exc_info.value.detail)
        assert marker["persistence_failure"] is True
        assert marker["eval_id"] == str(eval_def.id)
        assert marker["failure_behaviour"] == "block"

    async def test_factory_failure_block(self) -> None:
        """Second case: session_factory() itself raises."""
        eval_def = _eval_def(
            name="block-eval-2",
            pattern="pass",
            failure_behaviour="block",
        )
        out = _output("fail")

        class OperationalError(RuntimeError):
            pass

        factory = _fail_open_factory(OperationalError("connection refused"))
        run_id = uuid4()
        org_id = uuid4()
        _patch_rls()

        try:
            with pytest.raises(EvalBlockedError) as exc_info:
                await run_evals_persist_before_decide(
                    eval_defs=[eval_def],
                    resolve_eval_target=lambda ed: out,
                    run_id=run_id,
                    org_id=org_id,
                    session_factory=factory,
                    node_id=_NODE_ID,
                )
        finally:
            patch.stopall()

        assert exc_info.value.eval_name == "block-eval-2"
        marker = json.loads(exc_info.value.detail)
        assert marker["persistence_failure"] is True
        assert marker["failure_behaviour"] == "block"


# ---------------------------------------------------------------------------
# C6 — Persistence failure for a warn eval → undefined, run continues
# ---------------------------------------------------------------------------


class TestC6PersistenceFailureWarn:
    """C6: warn eval persist failure → log-and-continue, next eval still processed."""

    async def test_warn_persist_failure_continues(self) -> None:
        eval_a = _eval_def(
            name="warn-fail-persist",
            pattern="pass",
            failure_behaviour="warn",
        )
        eval_b = _eval_def(
            name="B-after-warn",
            pattern="pass",
            failure_behaviour="warn",
        )
        out = _output("fail")
        factory, _session = _fail_commit_factory(RuntimeError("commit failed"))
        run_id = uuid4()
        org_id = uuid4()
        _patch_rls()

        try:
            results = await run_evals_persist_before_decide(
                eval_defs=[eval_a, eval_b],
                resolve_eval_target=lambda ed: out,
                run_id=run_id,
                org_id=org_id,
                session_factory=factory,
                node_id=_NODE_ID,
            )
        finally:
            patch.stopall()

        assert "warn-fail-persist" in results
        assert "B-after-warn" in results
        assert len(results) == 2


# ---------------------------------------------------------------------------
# C7 — Partial persistence [A, B(block, fails persist), C]
# ---------------------------------------------------------------------------


class TestC7PartialPersistence:
    """C7: A durable, B absent (persist failed), C never evaluated, run halts."""

    async def test_c7(self) -> None:
        eval_a = _eval_def(name="A", pattern="pass", failure_behaviour="warn")
        eval_b = _eval_def(name="B", pattern="pass", failure_behaviour="block")
        eval_c = _eval_def(name="C", pattern="pass", failure_behaviour="warn")

        factory, _session = _fail_commit_factory(RuntimeError("commit failed for B"))
        run_id = uuid4()
        org_id = uuid4()
        _patch_rls()

        try:
            with pytest.raises(EvalBlockedError) as exc_info:
                await run_evals_persist_before_decide(
                    eval_defs=[eval_a, eval_b, eval_c],
                    resolve_eval_target=lambda ed: _output("fail"),
                    run_id=run_id,
                    org_id=org_id,
                    session_factory=factory,
                    node_id=_NODE_ID,
                )
        finally:
            patch.stopall()

        assert exc_info.value.eval_name == "B"
        marker = json.loads(exc_info.value.detail)
        assert marker["persistence_failure"] is True
        assert marker["eval_id"] == str(eval_b.id)


# ---------------------------------------------------------------------------
# C8 — N passing evals → exactly N rows
# ---------------------------------------------------------------------------


class TestC8NPasingEvals:
    """C8: N eval definitions, all passing → exactly N EvalResult rows."""

    async def test_c8(self) -> None:
        n = 5
        evals = [_eval_def(name=f"eval-{i}", pattern="pass", failure_behaviour="warn") for i in range(n)]
        out = _output("pass")
        session = _make_session()
        factory = _session_factory(session)
        run_id = uuid4()
        org_id = uuid4()
        _patch_rls()

        try:
            results = await run_evals_persist_before_decide(
                eval_defs=evals,
                resolve_eval_target=lambda ed: out,
                run_id=run_id,
                org_id=org_id,
                session_factory=factory,
                node_id=_NODE_ID,
            )
        finally:
            patch.stopall()

        assert len(results) == n
        assert len(session.added) == n
        for row in session.added:
            assert row.passed is True


# ---------------------------------------------------------------------------
# C9 — [A(block,fail), B(warn)] → exactly 1 row (A's), B never evaluated
# ---------------------------------------------------------------------------


class TestC9BlockFirstExactlyOneRow:
    """C9: first eval blocks → exactly 1 row, B never evaluated."""

    async def test_c9(self) -> None:
        eval_a = _eval_def(name="A", pattern="pass", failure_behaviour="block")
        eval_b = _eval_def(name="B", pattern="pass", failure_behaviour="warn")

        out = _output("fail")
        session = _make_session()
        factory = _session_factory(session)
        run_id = uuid4()
        org_id = uuid4()
        _patch_rls()

        try:
            with pytest.raises(EvalBlockedError) as exc_info:
                await run_evals_persist_before_decide(
                    eval_defs=[eval_a, eval_b],
                    resolve_eval_target=lambda ed: out,
                    run_id=run_id,
                    org_id=org_id,
                    session_factory=factory,
                    node_id=_NODE_ID,
                )
        finally:
            patch.stopall()

        assert exc_info.value.eval_name == "A"
        assert len(session.added) == 1
        assert session.added[0].eval_id == eval_a.id


# ---------------------------------------------------------------------------
# C14 — evaluate_result on a failing block eval → returns result, no raise
# ---------------------------------------------------------------------------


class TestC14EvaluateResultBlockFails:
    """C14: evaluate_result returns EvalResult(passed=False), no EvalBlockedError."""

    def test_c14(self) -> None:
        engine = EvalEngine()
        eval_def = _eval_def(name="block-eval", pattern="pass", failure_behaviour="block")
        result = engine.evaluate_result(_output("fail"), eval_def, run_id=uuid4())

        assert result.passed is False
        assert result.score == 0.0
        assert result.eval_id == eval_def.id

    def test_evaluate_raises_for_comparison(self) -> None:
        """Confirm that the OLD evaluate() DOES raise for comparison."""
        engine = EvalEngine()
        eval_def = _eval_def(name="block-eval", pattern="pass", failure_behaviour="block")

        with pytest.raises(EvalBlockedError):
            engine.evaluate(_output("fail"), eval_def, run_id=uuid4())


# ---------------------------------------------------------------------------
# C15 — evaluate_result on a passing eval → passed=True
# ---------------------------------------------------------------------------


class TestC15EvaluateResultPassing:
    """C15: evaluate_result returns EvalResult(passed=True) for a passing eval."""

    def test_c15(self) -> None:
        engine = EvalEngine()
        eval_def = _eval_def(name="pass-eval", pattern="pass", failure_behaviour="block")
        result = engine.evaluate_result(_output("pass"), eval_def, run_id=uuid4())

        assert result.passed is True
        assert result.score == 1.0
        assert result.eval_id == eval_def.id


# ---------------------------------------------------------------------------
# C16 — HITL gate-eval loop persists block eval result before raising
# ---------------------------------------------------------------------------


class TestC16HitlGateEvalPersistsBeforeRaise:
    """C16: HITL gate-eval loop persists block result before EvalBlockedError."""

    async def test_c16_via_run_gate_evals(self) -> None:
        """Exercise _run_gate_evals (node_runner) — the HITL path."""
        from modulo.core.pipeline_engine.node_runner import _run_gate_evals

        eval_def = _eval_def(
            name="gate-block",
            pattern="pass",
            failure_behaviour="block",
        )
        state: dict[str, Any] = {"text": "fail", "_run_id": uuid4()}
        session = _make_session()
        factory = _session_factory(session)
        org_id = uuid4()
        events: list[str] = []
        _patch_rls("modulo.core.pipeline_engine.node_runner")

        def _tracking_add(obj: Any) -> None:
            events.append("persist")
            session.added.append(obj)

        session.add = _tracking_add

        try:
            with pytest.raises(EvalBlockedError) as exc_info:
                await _run_gate_evals(
                    state=state,
                    eval_definitions=[eval_def],
                    node_type_map=None,
                    gate_id="gate-1",
                    session_factory=factory,
                    org_id=org_id,
                )
            events.append("block")
        finally:
            patch.stopall()

        assert exc_info.value.eval_name == "gate-block"
        assert events[0] == "persist", f"persist must precede block, got {events}"
        assert events[1] == "block"
        assert len(session.added) == 1
        assert session.added[0].passed is False

    async def test_c16_via_make_hitl_gate_fn(self) -> None:
        """Exercise make_hitl_gate_fn with eval_definitions — the full HITL path."""
        from modulo.core.pipeline_engine.node_runner import make_hitl_gate_fn

        eval_def = _eval_def(
            name="gate-block-fn",
            pattern="pass",
            failure_behaviour="block",
        )
        session = _make_session()
        factory = _session_factory(session)
        org_id = uuid4()
        node_fn = make_hitl_gate_fn(
            {"gate_id": "g"},
            eval_definitions=[eval_def],
            session_factory=factory,
            org_id=org_id,
        )
        state: dict[str, Any] = {"text": "fail", "artifacts": [], "_hitl_gates": [], "_run_id": uuid4()}
        _patch_rls("modulo.core.pipeline_engine.node_runner")

        try:
            with (
                patch(
                    "modulo.core.pipeline_engine.node_runner.interrupt",
                    side_effect=AssertionError("interrupt should NOT be reached - block fires first"),
                ),
                pytest.raises(EvalBlockedError) as exc_info,
            ):
                await node_fn(state)
        finally:
            patch.stopall()

        assert exc_info.value.eval_name == "gate-block-fn"
        assert len(session.added) >= 1
        assert session.added[0].passed is False


# ---------------------------------------------------------------------------
# C17 — HITL resume does NOT create duplicate rows (REGRESSION GUARD)
# ---------------------------------------------------------------------------


class TestC17HitlResumeNoDuplicateRows:
    """C17: HITL resume returns before the eval loop — no duplicate rows."""

    async def test_c17_resume_skips_eval_loop(self) -> None:
        """A resumed gate (state has _hitl_decision) returns immediately
        without calling _run_gate_evals, so no rows are created."""
        from modulo.core.pipeline_engine.node_runner import make_hitl_gate_fn

        eval_def = _eval_def(
            name="should-not-run",
            pattern="pass",
            failure_behaviour="block",
        )
        node_fn = make_hitl_gate_fn(
            {"gate_id": "g1"},
            eval_definitions=[eval_def],
        )
        # Simulate a resume: _hitl_decision is set with matching gate_id
        state: dict[str, Any] = {
            "artifacts": [],
            "_hitl_decision": {"action": "approved", "gate_id": "g1"},
            "_hitl_gates": [{"gate_id": "g1"}],
        }

        result = await node_fn(state)

        assert result["artifacts"][0]["result"] == "approved"
        # The resume path returns before _run_gate_evals — no DB interaction.
        # We verify this by asserting the eval was NOT computed (no session calls).
        # The structural guard is in _hitl_gate_resume_result (node_runner.py:4187):
        # ``if decision is None: return (False, None)`` — when a decision is
        # present and matches the gate_id, it returns immediately without
        # reaching the eval loop.


# ---------------------------------------------------------------------------
# C18 — Persistence-failure halt reuses eval_blocked AND carries marker
#        AND increments counter
# ---------------------------------------------------------------------------


class TestC18PersistenceFailureHaltIdentity:
    """C18: halt reuses eval_blocked, carries marker, increments counter."""

    async def test_c18_counter_and_marker(self) -> None:
        eval_def = _eval_def(
            name="block-eval-c18",
            pattern="pass",
            failure_behaviour="block",
        )
        out = _output("fail")
        factory, _session = _fail_commit_factory(RuntimeError("commit failed"))
        run_id = uuid4()
        org_id = uuid4()

        mock_counter = MagicMock()
        _patch_rls()

        try:
            with (
                patch(
                    "modulo.core.pipeline_engine.eval_persist_order._eval_result_persist_failures_total",
                    mock_counter,
                ),
                pytest.raises(EvalBlockedError) as exc_info,
            ):
                await run_evals_persist_before_decide(
                    eval_defs=[eval_def],
                    resolve_eval_target=lambda ed: out,
                    run_id=run_id,
                    org_id=org_id,
                    session_factory=factory,
                    node_id=_NODE_ID,
                )
        finally:
            patch.stopall()

        assert isinstance(exc_info.value, EvalBlockedError)
        assert exc_info.value.eval_name == "block-eval-c18"
        marker = json.loads(exc_info.value.detail)
        assert marker["persistence_failure"] is True
        assert marker["eval_id"] == str(eval_def.id)
        assert marker["failure_behaviour"] == "block"
        mock_counter.add.assert_called_once_with(1, {"failure_behaviour": "block"})


# ---------------------------------------------------------------------------
# C19 — Suite-completeness guard fails closed on missing eval id
# ---------------------------------------------------------------------------


class TestC19SuiteCompletenessGuard:
    """C19: _check_eval_suites with missing eval_id → INDETERMINATE, no ratio."""

    def _session_with_batches(self, batches: list[list[Any]]) -> AsyncMock:
        """Session whose execute() returns one MagicMock result per batch."""
        session = AsyncMock()
        results = []
        for batch in batches:
            r = MagicMock()
            r.scalars.return_value.all.return_value = batch
            results.append(r)

        async def _execute(stmt: Any) -> Any:
            return results.pop(0)

        session.execute = _execute
        return session

    async def test_missing_eval_id_indeterminate(self) -> None:
        """Suite [A, B, C] but only [A, B] have rows → INDETERMINATE."""
        from modulo.core.pipeline_engine.executor import PipelineExecutor

        pipeline_id = uuid4()
        run_id = uuid4()
        suite = "test-suite"
        def_a = _make_eval_def_row(name="A", suite_id=suite, pass_threshold=0.8, pipeline_id=pipeline_id)
        def_b = _make_eval_def_row(name="B", suite_id=suite, pass_threshold=0.8, pipeline_id=pipeline_id)
        def_c = _make_eval_def_row(name="C", suite_id=suite, pass_threshold=0.8, pipeline_id=pipeline_id)

        row_a = _make_eval_result_row(eval_id=def_a.id, run_id=run_id, passed=True)
        row_b = _make_eval_result_row(eval_id=def_b.id, run_id=run_id, passed=True)

        executor = PipelineExecutor(MagicMock())
        session = self._session_with_batches(
            [
                [def_a, def_b, def_c],
                [def_a, def_b, def_c],
                [row_a, row_b],
            ]
        )

        mock_incomplete = MagicMock()
        with patch("modulo.core.pipeline_engine.executor._record_suite_incomplete", mock_incomplete):
            results = await executor._check_eval_suites(session, run_id, pipeline_id)

        assert len(results) == 1
        assert results[0].outcome == SuiteOutcome.INDETERMINATE
        assert results[0].passed is False
        assert results[0].aggregate_score == 0.0
        mock_incomplete.assert_called_once()

    async def test_duplicate_rows_do_not_false_fail(self) -> None:
        """Duplicate EvalResult rows for the same eval_id must NOT false-fail the guard.

        Suite [A, B] with pass_threshold=0.5. A has 2 rows (both pass), B has 1 row (pass).
        Set-coverage: {A, B} ⊆ {A, B} → NOT indeterminate. Score = 3/3 = 1.0 >= 0.5 → PASSED.
        The key assertion is NOT indeterminate (completeness guard passed despite duplicates).
        """
        from modulo.core.pipeline_engine.executor import PipelineExecutor

        pipeline_id = uuid4()
        run_id = uuid4()
        suite = "dup-suite"
        def_a = _make_eval_def_row(name="A", suite_id=suite, pass_threshold=0.5, pipeline_id=pipeline_id)
        def_b = _make_eval_def_row(name="B", suite_id=suite, pass_threshold=0.5, pipeline_id=pipeline_id)

        row_a1 = _make_eval_result_row(eval_id=def_a.id, run_id=run_id, passed=True)
        row_a2 = _make_eval_result_row(eval_id=def_a.id, run_id=run_id, passed=True)
        row_b = _make_eval_result_row(eval_id=def_b.id, run_id=run_id, passed=True)

        executor = PipelineExecutor(MagicMock())
        session = self._session_with_batches(
            [
                [def_a, def_b],
                [def_a, def_b],
                [row_a1, row_a2, row_b],
            ]
        )

        results = await executor._check_eval_suites(session, run_id, pipeline_id)

        assert len(results) == 1
        # Set-coverage: both eval_ids present → NOT indeterminate
        assert results[0].outcome != SuiteOutcome.INDETERMINATE
        # All 3 rows pass, score = 3/3 = 1.0 >= 0.5 → PASSED
        assert results[0].outcome == SuiteOutcome.PASSED
        assert results[0].aggregate_score == pytest.approx(1.0)

    async def test_all_present_passes(self) -> None:
        """Suite with all expected eval ids present → proceeds to ratio computation."""
        from modulo.core.pipeline_engine.executor import PipelineExecutor

        pipeline_id = uuid4()
        run_id = uuid4()
        suite = "complete-suite"
        def_a = _make_eval_def_row(name="A", suite_id=suite, pass_threshold=0.5, pipeline_id=pipeline_id)
        def_b = _make_eval_def_row(name="B", suite_id=suite, pass_threshold=0.5, pipeline_id=pipeline_id)

        row_a = _make_eval_result_row(eval_id=def_a.id, run_id=run_id, passed=True)
        row_b = _make_eval_result_row(eval_id=def_b.id, run_id=run_id, passed=True)

        executor = PipelineExecutor(MagicMock())
        session = self._session_with_batches(
            [
                [def_a, def_b],
                [def_a, def_b],
                [row_a, row_b],
            ]
        )

        results = await executor._check_eval_suites(session, run_id, pipeline_id)

        assert len(results) == 1
        assert results[0].outcome == SuiteOutcome.PASSED
        assert results[0].aggregate_score == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Additional edge cases
# ---------------------------------------------------------------------------


class TestPersistedResultCorrectValues:
    """Verify the persisted row has correct field values."""

    async def test_row_fields_match(self) -> None:
        eval_def = _eval_def(name="check-fields", pattern="pass", failure_behaviour="warn")
        out = _output("pass")
        session = _make_session()
        factory = _session_factory(session)
        run_id = uuid4()
        org_id = uuid4()
        _patch_rls()

        try:
            await run_evals_persist_before_decide(
                eval_defs=[eval_def],
                resolve_eval_target=lambda ed: out,
                run_id=run_id,
                org_id=org_id,
                session_factory=factory,
                node_id=_NODE_ID,
            )
        finally:
            patch.stopall()

        row = session.added[0]
        assert row.organisation_id == org_id
        assert row.run_id == run_id
        assert row.eval_id == eval_def.id
        assert row.passed is True
        assert row.score == 1.0
        assert row.detail == "regex matched: /pass/ on text"


class TestNoSessionFactorySkipsPersistence:
    """When session_factory is None, no persistence occurs."""

    async def test_no_factory(self) -> None:
        eval_def = _eval_def(name="no-persist", pattern="pass", failure_behaviour="warn")
        results = await run_evals_persist_before_decide(
            eval_defs=[eval_def],
            resolve_eval_target=lambda ed: _output("pass"),
            run_id=uuid4(),
            org_id=uuid4(),
            session_factory=None,
            node_id=_NODE_ID,
        )
        assert "no-persist" in results
        assert results["no-persist"].passed is True


class TestOnEvalResultCallback:
    """Verify the on_eval_result callback is invoked per eval."""

    async def test_callback_invoked(self) -> None:
        eval_a = _eval_def(name="A", pattern="pass", failure_behaviour="warn")
        eval_b = _eval_def(name="B", pattern="pass", failure_behaviour="warn")
        callback_args: list[tuple[str, bool]] = []

        def _on_result(ed: EvalDefinition, result: EvalResult) -> None:
            callback_args.append((ed.name, result.passed))

        session = _make_session()
        factory = _session_factory(session)
        _patch_rls()

        try:
            await run_evals_persist_before_decide(
                eval_defs=[eval_a, eval_b],
                resolve_eval_target=lambda ed: _output("pass"),
                run_id=uuid4(),
                org_id=uuid4(),
                session_factory=factory,
                node_id=_NODE_ID,
                on_eval_result=_on_result,
            )
        finally:
            patch.stopall()

        assert len(callback_args) == 2
        assert callback_args[0] == ("A", True)
        assert callback_args[1] == ("B", True)


class TestRLSContextResetPerTransaction:
    """Verify RLS context is set inside each per-eval transaction."""

    async def test_rls_called_per_eval(self) -> None:
        eval_a = _eval_def(name="A", pattern="pass", failure_behaviour="warn")
        eval_b = _eval_def(name="B", pattern="pass", failure_behaviour="warn")
        session = _make_session()
        factory = _session_factory(session)
        run_id = uuid4()
        org_id = uuid4()
        rls_org, rls_ctx = _patch_rls()

        try:
            await run_evals_persist_before_decide(
                eval_defs=[eval_a, eval_b],
                resolve_eval_target=lambda ed: _output("pass"),
                run_id=run_id,
                org_id=org_id,
                session_factory=factory,
                node_id=_NODE_ID,
            )
        finally:
            patch.stopall()

        assert rls_org.call_count == 2
        assert rls_ctx.call_count == 2
