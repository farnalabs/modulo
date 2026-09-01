"""Unit tests for modulo.core.pipeline_engine.runaway_protection — the runaway run guards.

QA lens pass (correctness, bugs, edge cases) on the circuit breaker that guards
every pipeline run: ``PipelineExecutor`` constructs a ``RunawayGuard`` per run
and calls ``check_duration`` before each streamed event, ``record_step`` on each
completed node, and ``record_tokens`` on each LLM token report. A fired guard
ends the run as ``failed/runaway`` — so its exact boundary semantics are the
last line of defence against infinite pipelines burning tokens.

These tests lock the three independent guards (duration, steps, tokens) and the
contract that every limit is optional (``None`` = no limit, zero-downtime):

  * **Duration** — wall-clock ``max_duration_seconds``, strictly-greater bound
    (exactly-at-limit does NOT fire), and precise (non-truncated) ``current``
    in the raised error so a fired guard never reports ``current == limit``.
  * **Steps** — cumulative completed-node count vs ``max_steps``, firing on the
    step that first exceeds the limit and staying armed afterwards.
  * **Tokens** — cumulative usage vs ``token_budget``, the negative-input guard
    (``ValueError`` without mutating the counter), and zero-budget behaviour.
  * **Independence** — the guards never bleed state into each other.

Mock/fake based — no pipeline, executor, or DB required.
"""

from datetime import UTC, datetime

import pytest

import modulo.core.pipeline_engine.runaway_protection as rp
from modulo.core.pipeline_engine.runaway_protection import RunawayGuard, RunawayRunError

_T0 = datetime(2025, 1, 1, 0, 0, 0, tzinfo=UTC)


class _Clock:
    """Minimal stand-in for ``datetime`` with a mutable ``now``.

    The module calls ``datetime.now(UTC)`` both when the guard is constructed
    (capturing ``_start_time``) and on every ``check_duration`` call, so the
    clock must be frozen before the guard is built and advanced afterwards.
    """

    now_value = _T0

    @classmethod
    def now(cls, _tz=None) -> datetime:
        return cls.now_value


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> type[_Clock]:
    """Freeze the module's ``datetime`` so duration tests are deterministic.

    ``now_value`` is a shared class attribute, so each test starts from a clean
    clock (the guard's ``_start_time`` is captured at construction).
    """
    _Clock.now_value = _T0
    monkeypatch.setattr(rp, "datetime", _Clock)
    return _Clock


# ---------------------------------------------------------------------------
# RunawayRunError — the fired-guard payload consumed by the executor
# ---------------------------------------------------------------------------


class TestRunawayRunError:
    def test_exposes_guard_current_limit(self) -> None:
        exc = RunawayRunError("max_steps", 5, 3)
        assert exc.guard == "max_steps"
        assert exc.current == 5
        assert exc.limit == 3

    def test_message_mentions_all_three_fields(self) -> None:
        exc = RunawayRunError("token_budget", 250, 200)
        assert str(exc) == "Runaway run: token_budget exceeded (current=250, limit=200)"

    def test_is_a_runtime_error(self) -> None:
        assert issubclass(RunawayRunError, RuntimeError)


# ---------------------------------------------------------------------------
# Duration guard — check_duration
# ---------------------------------------------------------------------------


class TestDurationGuard:
    def test_no_limit_never_raises(self, clock: type[_Clock]) -> None:
        guard = RunawayGuard()
        clock.now_value = _T0.replace(hour=1)  # one hour later
        assert guard.check_duration() is None

    def test_exact_limit_does_not_raise(self, clock: type[_Clock]) -> None:
        guard = RunawayGuard(max_duration_seconds=10)
        clock.now_value = _T0.replace(second=10)  # exactly 10.0s elapsed
        assert guard.check_duration() is None

    def test_just_beyond_limit_raises(self, clock: type[_Clock]) -> None:
        guard = RunawayGuard(max_duration_seconds=10)
        clock.now_value = _T0.replace(second=10, microsecond=1)
        with pytest.raises(RunawayRunError) as excinfo:
            guard.check_duration()
        assert excinfo.value.guard == "max_duration"
        assert excinfo.value.limit == 10

    def test_current_reports_precise_elapsed(self, clock: type[_Clock]) -> None:
        """A fired duration guard must report the true elapsed time — never an
        ``int()``-truncated value equal to the limit (which would make the
        ``current=10, limit=10`` message read as though nothing was exceeded)."""
        guard = RunawayGuard(max_duration_seconds=10)
        clock.now_value = _T0.replace(second=10, microsecond=500_000)  # 10.5s
        with pytest.raises(RunawayRunError) as excinfo:
            guard.check_duration()
        assert excinfo.value.current == pytest.approx(10.5)
        assert "current=10.5" in str(excinfo.value)

    def test_no_elapsed_time_does_not_raise(self, clock: type[_Clock]) -> None:
        guard = RunawayGuard(max_duration_seconds=10)
        assert guard.check_duration() is None  # now_value still == _T0

    def test_zero_limit_fires_on_first_check(self, clock: type[_Clock]) -> None:
        guard = RunawayGuard(max_duration_seconds=0)
        clock.now_value = _T0.replace(microsecond=1)
        with pytest.raises(RunawayRunError) as excinfo:
            guard.check_duration()
        assert excinfo.value.guard == "max_duration"
        assert excinfo.value.limit == 0


# ---------------------------------------------------------------------------
# Steps guard — record_step
# ---------------------------------------------------------------------------


class TestStepGuard:
    def test_no_limit_accumulates_without_raising(self) -> None:
        guard = RunawayGuard()
        for _ in range(1000):
            guard.record_step()
        assert guard._step_count == 1000

    def test_exactly_max_steps_does_not_raise(self) -> None:
        guard = RunawayGuard(max_steps=3)
        for _ in range(3):
            guard.record_step()
        assert guard._step_count == 3

    def test_first_exceeding_step_raises(self) -> None:
        guard = RunawayGuard(max_steps=3)
        for _ in range(3):
            guard.record_step()
        with pytest.raises(RunawayRunError) as excinfo:
            guard.record_step()
        assert excinfo.value.guard == "max_steps"
        assert excinfo.value.current == 4
        assert excinfo.value.limit == 3

    def test_guard_stays_armed_after_firing(self) -> None:
        guard = RunawayGuard(max_steps=1)
        guard.record_step()  # 1 == max — no raise
        with pytest.raises(RunawayRunError) as excinfo:
            guard.record_step()
        assert excinfo.value.guard == "max_steps"
        assert excinfo.value.current == 2
        with pytest.raises(RunawayRunError) as excinfo2:
            guard.record_step()
        assert excinfo2.value.current == 3

    def test_zero_max_steps_fires_on_first_step(self) -> None:
        guard = RunawayGuard(max_steps=0)
        with pytest.raises(RunawayRunError) as excinfo:
            guard.record_step()
        assert excinfo.value.guard == "max_steps"
        assert excinfo.value.current == 1


# ---------------------------------------------------------------------------
# Token budget guard — record_tokens
# ---------------------------------------------------------------------------


class TestTokenGuard:
    def test_no_limit_accumulates_without_raising(self) -> None:
        guard = RunawayGuard()
        for i in range(100):
            guard.record_tokens(i)
        assert guard._token_count == sum(range(100))

    def test_exactly_budget_does_not_raise(self) -> None:
        guard = RunawayGuard(token_budget=100)
        guard.record_tokens(60)
        guard.record_tokens(40)  # exactly at the budget
        assert guard._token_count == 100

    def test_first_token_over_budget_raises(self) -> None:
        guard = RunawayGuard(token_budget=100)
        guard.record_tokens(60)
        guard.record_tokens(30)
        with pytest.raises(RunawayRunError) as excinfo:
            guard.record_tokens(20)
        assert excinfo.value.guard == "token_budget"
        assert excinfo.value.current == 110
        assert excinfo.value.limit == 100

    def test_single_oversized_record_raises(self) -> None:
        guard = RunawayGuard(token_budget=100)
        with pytest.raises(RunawayRunError) as excinfo:
            guard.record_tokens(1000)
        assert excinfo.value.guard == "token_budget"
        assert excinfo.value.current == 1000

    def test_zero_token_record_is_a_noop(self) -> None:
        guard = RunawayGuard(token_budget=0)
        guard.record_tokens(0)  # 0 is not negative, 0 > 0 is False
        assert guard._token_count == 0

    def test_negative_record_is_rejected_without_mutation(self) -> None:
        guard = RunawayGuard(token_budget=100)
        guard.record_tokens(10)
        with pytest.raises(ValueError, match="Negative token count"):
            guard.record_tokens(-5)
        assert guard._token_count == 10

    def test_zero_budget_fires_on_first_token(self) -> None:
        guard = RunawayGuard(token_budget=0)
        with pytest.raises(RunawayRunError) as excinfo:
            guard.record_tokens(1)
        assert excinfo.value.guard == "token_budget"
        assert excinfo.value.current == 1


# ---------------------------------------------------------------------------
# Independence — the three guards never bleed state into each other
# ---------------------------------------------------------------------------


class TestIndependentGuards:
    def test_each_guard_fires_its_own_error(self) -> None:
        guard = RunawayGuard(max_steps=2, token_budget=10)
        guard.record_step()  # 1
        guard.record_step()  # 2 — at the step limit, no raise
        guard.record_tokens(6)  # 6 — under budget
        with pytest.raises(RunawayRunError) as excinfo:
            guard.record_tokens(5)  # 11 > 10 → token_budget fires
        assert excinfo.value.guard == "token_budget"
        assert excinfo.value.current == 11
        assert excinfo.value.limit == 10
        with pytest.raises(RunawayRunError) as excinfo2:
            guard.record_step()  # 3 > 2 → max_steps fires
        assert excinfo2.value.guard == "max_steps"
        assert excinfo2.value.current == 3

    def test_fired_step_guard_does_not_block_token_tracking(self) -> None:
        guard = RunawayGuard(max_steps=1)
        guard.record_step()  # 1 — at the limit, no raise
        guard.record_tokens(7)
        with pytest.raises(RunawayRunError) as excinfo:
            guard.record_step()  # 2 > 1 → max_steps fires
        assert excinfo.value.guard == "max_steps"
        assert guard._token_count == 7
