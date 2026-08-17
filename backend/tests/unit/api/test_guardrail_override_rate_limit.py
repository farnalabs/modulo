"""Rate-limit unit tests for POST /api/v1/runs/{run_id}/guardrail-override.

The HTTP-level behaviour (within-limit succeeds, N+1th exceeds returns 429)
is covered in ``test_runs_endpoint.py`` where the ``client`` fixture lives.
This module holds the pure, fixture-free assertions on the limiter's key
scoping so an exhausted (org, actor) bucket never blocks a distinct actor.
"""

import asyncio

from modulo.core.rate_limiter import TokenBucketRegistry


def test_guardrail_override_rate_limit_is_org_actor_scoped() -> None:
    """Exhausting one (org, actor) bucket must not block a different key.

    The endpoint keys the limiter on ``guardrail-override:<org>:<account>``,
    so a distinct actor (or org) has its own bucket and keeps the ability to
    override.
    """
    limiter = TokenBucketRegistry(rate=1 / 60.0, burst=1)
    key_a = "guardrail-override:org-1:actor-1"
    key_b = "guardrail-override:org-1:actor-2"

    async def run() -> None:
        assert await limiter.consume(key_a) is True
        assert await limiter.consume(key_a) is False  # actor-1 bucket exhausted
        assert await limiter.consume(key_b) is True  # actor-2 unaffected

    asyncio.run(run())
