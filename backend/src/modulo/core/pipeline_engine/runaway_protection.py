"""Runaway run protection — circuit breaker guards for pipeline runs.

Three independent guards:
  - **Max duration**: wall-clock time since run start exceeds ``max_duration_seconds``.
  - **Max steps**: number of completed nodes exceeds ``max_steps``.
  - **Token budget**: cumulative token usage exceeds ``token_budget``.

Each guard is a no-op when its limit is ``None`` (the default — zero-downtime).
"""

import logging
from datetime import UTC, datetime

_log = logging.getLogger(__name__)


class RunawayRunError(RuntimeError):
    """Raised when a pipeline run exceeds a configured guard limit.

    Attributes:
        guard: The guard that triggered (``max_duration``, ``max_steps``, or
            ``token_budget``).
        current: The current value at the time of the violation.
        limit: The configured limit that was exceeded.
    """

    def __init__(self, guard: str, current: float, limit: float) -> None:
        self.guard = guard
        self.current = current
        self.limit = limit
        super().__init__(f"Runaway run: {guard} exceeded (current={current}, limit={limit})")


class RunawayGuard:
    """Circuit breaker that raises ``RunawayRunError`` when configured limits are hit.

    All limits are optional (``None`` = no limit). Usage:

    .. code-block:: python

        guard = RunawayGuard(max_steps=100, max_duration_seconds=3600)
        while True:
            guard.check_duration()
            ...
            guard.record_step()
            ...
            guard.record_tokens(42)
    """

    def __init__(
        self,
        *,
        max_duration_seconds: int | None = None,
        max_steps: int | None = None,
        token_budget: int | None = None,
    ) -> None:
        self._max_duration_seconds = max_duration_seconds
        self._max_steps = max_steps
        self._token_budget = token_budget
        self._step_count = 0
        self._token_count = 0
        self._start_time = datetime.now(UTC)

    # ------------------------------------------------------------------
    # Duration guard
    # ------------------------------------------------------------------

    def check_duration(self) -> None:
        """Raise ``RunawayRunError`` if wall-clock time exceeds *max_duration_seconds*."""
        if self._max_duration_seconds is not None:
            elapsed = (datetime.now(UTC) - self._start_time).total_seconds()
            if elapsed > self._max_duration_seconds:
                _log.warning(
                    "runaway.duration_exceeded",
                    extra={
                        "elapsed_seconds": int(elapsed),
                        "max_duration_seconds": self._max_duration_seconds,
                    },
                )
                raise RunawayRunError("max_duration", int(elapsed), self._max_duration_seconds)

    # ------------------------------------------------------------------
    # Steps guard
    # ------------------------------------------------------------------

    def record_step(self) -> None:
        """Increment the completed-node counter and check against *max_steps*."""
        self._step_count += 1
        if self._max_steps is not None and self._step_count > self._max_steps:
            _log.warning(
                "runaway.steps_exceeded",
                extra={
                    "step_count": self._step_count,
                    "max_steps": self._max_steps,
                },
            )
            raise RunawayRunError("max_steps", self._step_count, self._max_steps)

    # ------------------------------------------------------------------
    # Token budget guard
    # ------------------------------------------------------------------

    def record_tokens(self, tokens: int) -> None:
        """Accumulate token usage and check against *token_budget*."""
        if tokens < 0:
            _log.warning("runaway.negative_tokens", extra={"tokens": tokens})
            return
        self._token_count += tokens
        if self._token_budget is not None and self._token_count > self._token_budget:
            _log.warning(
                "runaway.tokens_exceeded",
                extra={
                    "token_count": self._token_count,
                    "token_budget": self._token_budget,
                },
            )
            raise RunawayRunError("token_budget", self._token_count, self._token_budget)
