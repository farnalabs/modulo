"""Unit tests for the reusable circuit breaker (``modulo.core.circuit_breaker``).

The breaker is a new public API surface guarding external service calls
(model backends, connectors), so every state transition, the ``call``
wrapper, the introspection helpers and the module-level registries are
covered here.

Time is driven by a fake clock injected over the module's ``time``
reference, so recovery-timeout transitions are deterministic and the suite
never sleeps.
"""

from typing import Any

import pytest

from modulo.core import circuit_breaker as cb_module
from modulo.core.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerOpenError,
    CircuitState,
    get_all_breakers,
    get_connector_breaker,
    get_model_backend_breaker,
)


class FakeClock:
    """Minimal stand-in for the ``time`` module exposing only ``monotonic``."""

    def __init__(self, start: float = 1_000.0) -> None:
        self.now = start

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    """Replace the module-level ``time`` reference with a controllable clock."""
    fake = FakeClock()
    monkeypatch.setattr(cb_module, "time", fake)
    return fake


@pytest.fixture(autouse=True)
def _clean_registries() -> Any:
    """Isolate the module-global breaker registries between tests."""
    model_snapshot = dict(cb_module._model_backend_breakers)
    connector_snapshot = dict(cb_module._connector_breakers)
    cb_module._model_backend_breakers.clear()
    cb_module._connector_breakers.clear()
    yield
    cb_module._model_backend_breakers.clear()
    cb_module._model_backend_breakers.update(model_snapshot)
    cb_module._connector_breakers.clear()
    cb_module._connector_breakers.update(connector_snapshot)


async def _ok(value: str = "ok") -> str:
    return value


async def _boom(message: str = "downstream exploded") -> str:
    raise RuntimeError(message)


def _trip(breaker: CircuitBreaker) -> None:
    """Drive the breaker to OPEN by recording threshold-many failures."""
    for _ in range(breaker.failure_threshold):
        breaker._record_failure()
    assert breaker._state is CircuitState.OPEN


# ---------------------------------------------------------------------------
# CircuitState / CircuitBreakerOpenError
# ---------------------------------------------------------------------------


def test_circuit_state_values_are_stable_wire_strings() -> None:
    assert CircuitState.CLOSED.value == "closed"
    assert CircuitState.OPEN.value == "open"
    assert CircuitState.HALF_OPEN.value == "half_open"


def test_open_error_carries_name_remaining_and_readable_message() -> None:
    err = CircuitBreakerOpenError("model_backend:gpt", 12.34)

    assert err.name == "model_backend:gpt"
    assert err.remaining_seconds == pytest.approx(12.34)
    assert str(err) == "Circuit breaker 'model_backend:gpt' is open. Retry in 12.3s."


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def test_defaults_start_closed_with_zeroed_counters() -> None:
    breaker = CircuitBreaker()

    assert breaker.name == "default"
    assert breaker.failure_threshold == 5
    assert breaker.recovery_timeout == pytest.approx(30.0)
    assert breaker.success_threshold == 1
    assert breaker.state is CircuitState.CLOSED
    assert breaker.is_available() is True


def test_explicit_configuration_is_retained() -> None:
    breaker = CircuitBreaker(
        name="connector:github",
        failure_threshold=2,
        recovery_timeout=7.5,
        success_threshold=3,
    )

    assert breaker.name == "connector:github"
    assert breaker.failure_threshold == 2
    assert breaker.recovery_timeout == pytest.approx(7.5)
    assert breaker.success_threshold == 3


# ---------------------------------------------------------------------------
# Failure accounting: CLOSED -> OPEN
# ---------------------------------------------------------------------------


def test_failures_below_threshold_keep_the_circuit_closed(clock: FakeClock) -> None:
    breaker = CircuitBreaker(failure_threshold=3)

    breaker._record_failure()
    breaker._record_failure()

    assert breaker._failure_count == 2
    assert breaker.state is CircuitState.CLOSED
    assert breaker._last_failure_time == pytest.approx(clock.now)


def test_reaching_the_threshold_opens_the_circuit(clock: FakeClock) -> None:
    breaker = CircuitBreaker(failure_threshold=3)

    _trip(breaker)

    assert breaker._failure_count == 3
    assert breaker.state is CircuitState.OPEN
    assert breaker.is_available() is False


def test_success_in_closed_state_resets_the_failure_run(clock: FakeClock) -> None:
    breaker = CircuitBreaker(failure_threshold=3)
    breaker._record_failure()
    breaker._record_failure()

    breaker._record_success()

    assert breaker._failure_count == 0
    assert breaker._success_count == 0
    assert breaker.state is CircuitState.CLOSED


# ---------------------------------------------------------------------------
# Recovery: OPEN -> HALF_OPEN -> CLOSED / OPEN
# ---------------------------------------------------------------------------


def test_open_circuit_stays_open_before_the_recovery_timeout(clock: FakeClock) -> None:
    breaker = CircuitBreaker(failure_threshold=1, recovery_timeout=30.0)
    _trip(breaker)

    clock.advance(29.9)

    assert breaker.state is CircuitState.OPEN


def test_open_circuit_auto_transitions_to_half_open_at_the_timeout(clock: FakeClock) -> None:
    breaker = CircuitBreaker(failure_threshold=1, recovery_timeout=30.0)
    _trip(breaker)

    clock.advance(30.0)

    assert breaker.state is CircuitState.HALF_OPEN
    assert breaker.is_available() is True


def test_half_open_success_below_threshold_stays_half_open(clock: FakeClock) -> None:
    breaker = CircuitBreaker(failure_threshold=1, recovery_timeout=1.0, success_threshold=2)
    _trip(breaker)
    clock.advance(1.0)
    assert breaker.state is CircuitState.HALF_OPEN

    breaker._record_success()

    assert breaker._success_count == 1
    assert breaker._state is CircuitState.HALF_OPEN


def test_half_open_closes_once_success_threshold_is_met(clock: FakeClock) -> None:
    breaker = CircuitBreaker(failure_threshold=1, recovery_timeout=1.0, success_threshold=2)
    _trip(breaker)
    clock.advance(1.0)
    assert breaker.state is CircuitState.HALF_OPEN

    breaker._record_success()
    breaker._record_success()

    assert breaker.state is CircuitState.CLOSED
    assert breaker._failure_count == 0
    assert breaker._success_count == 0


def test_half_open_failure_reopens_immediately_ignoring_threshold(clock: FakeClock) -> None:
    breaker = CircuitBreaker(failure_threshold=10, recovery_timeout=1.0)
    breaker._record_failure()
    breaker._set_state(CircuitState.HALF_OPEN)

    breaker._record_failure()

    assert breaker._state is CircuitState.OPEN
    assert breaker._success_count == 0


def test_failure_clears_accumulated_half_open_successes(clock: FakeClock) -> None:
    breaker = CircuitBreaker(failure_threshold=10, recovery_timeout=1.0, success_threshold=3)
    breaker._set_state(CircuitState.HALF_OPEN)
    breaker._record_success()
    assert breaker._success_count == 1

    breaker._record_failure()

    assert breaker._success_count == 0


# ---------------------------------------------------------------------------
# State-change callback
# ---------------------------------------------------------------------------


def test_state_change_callback_receives_name_and_both_states(clock: FakeClock) -> None:
    seen: list[tuple[str, CircuitState, CircuitState]] = []
    breaker = CircuitBreaker(
        name="svc",
        failure_threshold=1,
        recovery_timeout=5.0,
        on_state_change=lambda name, old, new: seen.append((name, old, new)),
    )

    _trip(breaker)
    clock.advance(5.0)
    assert breaker.state is CircuitState.HALF_OPEN

    assert seen == [
        ("svc", CircuitState.CLOSED, CircuitState.OPEN),
        ("svc", CircuitState.OPEN, CircuitState.HALF_OPEN),
    ]


def test_redundant_transition_is_a_noop_and_does_not_fire_the_callback() -> None:
    calls: list[Any] = []
    breaker = CircuitBreaker(on_state_change=lambda *a: calls.append(a))

    breaker._set_state(CircuitState.CLOSED)

    assert calls == []
    assert breaker.state is CircuitState.CLOSED


def test_callback_exception_is_swallowed_so_the_breaker_keeps_working(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def explode(name: str, old: CircuitState, new: CircuitState) -> None:
        raise ValueError("observer is broken")

    breaker = CircuitBreaker(name="svc", failure_threshold=1, on_state_change=explode)

    with caplog.at_level("ERROR", logger=cb_module.__name__):
        breaker._record_failure()

    assert breaker._state is CircuitState.OPEN
    assert "circuit_breaker.on_state_change_error" in caplog.text


# ---------------------------------------------------------------------------
# call()
# ---------------------------------------------------------------------------


async def test_call_returns_the_result_and_forwards_args(clock: FakeClock) -> None:
    breaker = CircuitBreaker()

    async def echo(a: int, *, b: int) -> int:
        return a + b

    assert await breaker.call(echo, 2, b=3) == 5
    assert breaker.state is CircuitState.CLOSED


async def test_call_success_resets_a_partial_failure_run(clock: FakeClock) -> None:
    breaker = CircuitBreaker(failure_threshold=3)
    breaker._record_failure()

    await breaker.call(_ok)

    assert breaker._failure_count == 0


async def test_call_reraises_the_downstream_error_and_records_the_failure(clock: FakeClock) -> None:
    breaker = CircuitBreaker(failure_threshold=2)

    with pytest.raises(RuntimeError, match="downstream exploded"):
        await breaker.call(_boom)

    assert breaker._failure_count == 1
    assert breaker.state is CircuitState.CLOSED


async def test_call_opens_the_circuit_once_failures_reach_the_threshold(clock: FakeClock) -> None:
    breaker = CircuitBreaker(failure_threshold=2)

    for _ in range(2):
        with pytest.raises(RuntimeError):
            await breaker.call(_boom)

    assert breaker.state is CircuitState.OPEN


async def test_call_is_rejected_while_open_without_invoking_the_callable(clock: FakeClock) -> None:
    breaker = CircuitBreaker(name="svc", failure_threshold=1, recovery_timeout=30.0)
    calls = 0

    async def tracked() -> str:
        nonlocal calls
        calls += 1
        return "ok"

    _trip(breaker)
    clock.advance(10.0)

    with pytest.raises(CircuitBreakerOpenError) as excinfo:
        await breaker.call(tracked)

    assert calls == 0
    assert excinfo.value.name == "svc"
    assert excinfo.value.remaining_seconds == pytest.approx(20.0)


async def test_half_open_probe_success_closes_the_circuit(clock: FakeClock) -> None:
    breaker = CircuitBreaker(failure_threshold=1, recovery_timeout=5.0)
    _trip(breaker)
    clock.advance(5.0)

    assert await breaker.call(_ok) == "ok"

    assert breaker.state is CircuitState.CLOSED
    assert breaker._failure_count == 0


async def test_half_open_probe_failure_reopens_the_circuit(clock: FakeClock) -> None:
    breaker = CircuitBreaker(failure_threshold=1, recovery_timeout=5.0)
    _trip(breaker)
    clock.advance(5.0)
    assert breaker.state is CircuitState.HALF_OPEN

    with pytest.raises(RuntimeError):
        await breaker.call(_boom)

    assert breaker._state is CircuitState.OPEN


# ---------------------------------------------------------------------------
# Introspection / manual control
# ---------------------------------------------------------------------------


def test_reset_closes_an_open_circuit_and_clears_counters(clock: FakeClock) -> None:
    breaker = CircuitBreaker(failure_threshold=1)
    _trip(breaker)

    breaker.reset()

    assert breaker.state is CircuitState.CLOSED
    assert breaker._failure_count == 0
    assert breaker._success_count == 0
    assert breaker.is_available() is True


def test_get_stats_reports_the_current_snapshot(clock: FakeClock) -> None:
    breaker = CircuitBreaker(name="svc", failure_threshold=2, recovery_timeout=15.0)

    assert breaker.get_stats() == {
        "name": "svc",
        "state": "closed",
        "failure_count": 0,
        "success_count": 0,
        "failure_threshold": 2,
        "recovery_timeout": 15.0,
    }

    _trip(breaker)

    stats = breaker.get_stats()
    assert stats["state"] == "open"
    assert stats["failure_count"] == 2


# ---------------------------------------------------------------------------
# Module-level registries
# ---------------------------------------------------------------------------


def test_model_backend_breaker_is_created_with_backend_defaults() -> None:
    breaker = get_model_backend_breaker("gpt-4o")

    assert breaker.name == "model_backend:gpt-4o"
    assert breaker.failure_threshold == 3
    assert breaker.recovery_timeout == pytest.approx(60.0)


def test_model_backend_breaker_is_memoised_per_backend_id() -> None:
    first = get_model_backend_breaker("gpt-4o")

    assert get_model_backend_breaker("gpt-4o") is first
    assert get_model_backend_breaker("claude") is not first


def test_connector_breaker_is_created_with_connector_defaults() -> None:
    breaker = get_connector_breaker("github")

    assert breaker.name == "connector:github"
    assert breaker.failure_threshold == 3
    assert breaker.recovery_timeout == pytest.approx(60.0)


def test_connector_breaker_is_memoised_per_connector_id() -> None:
    first = get_connector_breaker("github")

    assert get_connector_breaker("github") is first
    assert get_connector_breaker("slack") is not first


def test_get_all_breakers_merges_both_registries() -> None:
    assert not get_all_breakers()

    model = get_model_backend_breaker("gpt-4o")
    connector = get_connector_breaker("github")

    assert get_all_breakers() == {"gpt-4o": model, "github": connector}


def test_get_all_breakers_returns_a_detached_copy() -> None:
    get_model_backend_breaker("gpt-4o")

    snapshot = get_all_breakers()
    snapshot.clear()

    assert "gpt-4o" in cb_module._model_backend_breakers
    assert set(get_all_breakers()) == {"gpt-4o"}
