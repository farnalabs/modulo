"""Unit tests for the FAR-748 MCP HITL per-key decision budget (20/min).

The 2026-09-05 sweep vector rode the general 200/min ``/mcp`` rule; the
FAR-611 sweep alarm detects it post-hoc, but the transport itself was never
throttled. Covers: the per-key derivation (same FAR-620 contract as the
trigger_pipeline bucket), the 20/min process-local bucket behaviour, the
fail-open posture on limiter failure, the rate_limited error shape emitted
by ``_dispatch_hitl_action`` for decision actions, and the budget NOT
applying to non-decision actions (claim / reject).
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from modulo.api.mcp_server import (
    _dispatch_hitl_action,
    _hitl_decision_client_key,
    _hitl_decision_rate_limited_response,
)
from modulo.core.rate_limiter import TokenBucketRegistry

_PLACEHOLDER_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_PLACEHOLDER_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000003")
_PLACEHOLDER_KEY_ID = uuid.UUID("00000000-0000-0000-0000-000000000005")
_GATE_ID = "gate-1"


class _AuthContext:
    def setup_method(self) -> None:
        from modulo.api.mcp_server import (
            _ctx_auth_type,
            _ctx_key_id,
            _ctx_key_scope,
            _ctx_org_id,
            _ctx_user_id,
        )

        _ctx_org_id.set(_PLACEHOLDER_ORG_ID)
        _ctx_user_id.set(_PLACEHOLDER_USER_ID)
        _ctx_key_id.set(_PLACEHOLDER_KEY_ID)
        _ctx_auth_type.set("api_key")
        _ctx_key_scope.set("org")

    def teardown_method(self) -> None:
        from modulo.api.mcp_server import (
            _ctx_auth_type,
            _ctx_key_id,
            _ctx_key_scope,
            _ctx_org_id,
            _ctx_user_id,
        )

        _ctx_org_id.set(None)
        _ctx_user_id.set(None)
        _ctx_key_id.set(None)
        _ctx_auth_type.set(None)
        _ctx_key_scope.set(None)


class TestHitlDecisionClientKey(_AuthContext):
    def test_org_scoped_api_key_keyed_by_org_and_key_id(self) -> None:
        assert _hitl_decision_client_key() == (f"hitl_decision:{_PLACEHOLDER_ORG_ID}:api_key:ak:{_PLACEHOLDER_KEY_ID}")

    def test_user_scoped_api_key_buckets_by_account(self) -> None:
        """FAR-620 contract: a user-scoped key acts as its creator — decisions
        are attributed to the account, so the bucket is the identity bucket."""
        from modulo.api.mcp_server import _ctx_key_scope

        _ctx_key_scope.set("user")
        assert _hitl_decision_client_key() == (
            f"hitl_decision:{_PLACEHOLDER_ORG_ID}:api_key:user:{_PLACEHOLDER_USER_ID}"
        )

    def test_oauth_caller_buckets_by_user(self) -> None:
        from modulo.api.mcp_server import _ctx_auth_type

        _ctx_auth_type.set("oauth")
        assert _hitl_decision_client_key() == (f"hitl_decision:{_PLACEHOLDER_ORG_ID}:oauth:user:{_PLACEHOLDER_USER_ID}")

    def test_distinct_clients_get_distinct_keys(self) -> None:
        from modulo.api.mcp_server import _ctx_key_id

        key_a = _hitl_decision_client_key()
        _ctx_key_id.set(uuid.UUID("00000000-0000-0000-0000-000000000099"))
        key_b = _hitl_decision_client_key()
        assert key_a != key_b


class TestHitlDecisionBudgetAllowed(_AuthContext):
    def _burst3(self) -> TokenBucketRegistry:
        return TokenBucketRegistry(rate=1.0, burst=3)

    def _exhausted(self) -> TokenBucketRegistry:
        return TokenBucketRegistry(rate=0.0, burst=1)

    async def test_allowed_within_burst_then_denied(self) -> None:
        from modulo.api.mcp_server import _hitl_decision_budget_allowed

        with patch("modulo.api.mcp_server._hitl_decision_limiter", self._burst3()):
            assert await _hitl_decision_budget_allowed() is True
            assert await _hitl_decision_budget_allowed() is True
            assert await _hitl_decision_budget_allowed() is True
            assert await _hitl_decision_budget_allowed() is False

    async def test_fail_open_on_limiter_failure(self) -> None:
        """A limiter defect must never block the human's decision: fail-open
        (availability over enforcement — the REST limiter's registry-failure
        posture)."""
        from modulo.api.mcp_server import _hitl_decision_budget_allowed

        with patch(
            "modulo.api.mcp_server._hitl_decision_limiter",
            MagicMock(consume=AsyncMock(side_effect=RuntimeError("limiter down"))),
        ):
            assert await _hitl_decision_budget_allowed() is True

    async def test_exhausted_bucket_marks_consumed(self) -> None:
        from modulo.api.mcp_server import _hitl_decision_budget_allowed

        limiter = self._burst3()
        with patch("modulo.api.mcp_server._hitl_decision_limiter", limiter):
            # burst=3: the first three consumes succeed (to 0 tokens), the
            # fourth is denied.
            assert await _hitl_decision_budget_allowed() is True
            assert await _hitl_decision_budget_allowed() is True
            assert await _hitl_decision_budget_allowed() is True
            assert await _hitl_decision_budget_allowed() is False
            assert limiter._buckets[_hitl_decision_client_key()]._tokens < 1.0


class TestDispatchHitlActionBudget(_AuthContext):
    async def test_approve_returns_rate_limited_when_exhausted(self) -> None:
        """The exhausted budget returns the error shape BEFORE any manager
        call — a throttled decision never reaches the DB."""
        mgr = MagicMock()
        mgr.approve = AsyncMock()
        with patch(
            "modulo.api.mcp_server._hitl_decision_limiter",
            TokenBucketRegistry(rate=1.0, burst=1),
        ) as limiter:
            await limiter.consume(_hitl_decision_client_key())
            result = await _dispatch_hitl_action(
                mgr,
                AsyncMock(),
                "approve",
                uuid.uuid4(),
                _GATE_ID,
                _PLACEHOLDER_ORG_ID,
                _PLACEHOLDER_USER_ID,
                "tok",
                None,
                None,
            )
        assert result["error"] == "rate_limited"
        assert result == _hitl_decision_rate_limited_response()
        mgr.approve.assert_not_awaited()

    async def test_deliver_manual_returns_rate_limited_when_exhausted(self) -> None:
        mgr = MagicMock()
        mgr.deliver_manual = AsyncMock()
        with patch(
            "modulo.api.mcp_server._hitl_decision_limiter",
            TokenBucketRegistry(rate=1.0, burst=1),
        ) as limiter:
            await limiter.consume(_hitl_decision_client_key())
            result = await _dispatch_hitl_action(
                mgr,
                AsyncMock(),
                "deliver_manual",
                uuid.uuid4(),
                _GATE_ID,
                _PLACEHOLDER_ORG_ID,
                _PLACEHOLDER_USER_ID,
                "tok",
                {},
                None,
            )
        assert result["error"] == "rate_limited"
        mgr.deliver_manual.assert_not_awaited()

    async def test_claim_and_reject_are_not_budgeted(self) -> None:
        """Non-decision actions (claim / reject) are NOT budgeted — reject
        lets no run continue, so it is gated by the human_only guard rather
        than the budget."""
        mgr = MagicMock()
        gate_id = _GATE_ID
        gate = MagicMock()
        gate.claim_token = "tok-value"
        gate.expires_at = None
        mgr.claim = AsyncMock(return_value=gate)
        mgr.reject = AsyncMock(return_value=gate)

        with patch(
            "modulo.api.mcp_server._hitl_decision_limiter",
            TokenBucketRegistry(rate=1.0, burst=1),
        ):
            claimed = await _dispatch_hitl_action(
                mgr,
                AsyncMock(),
                "claim",
                uuid.uuid4(),
                gate_id,
                _PLACEHOLDER_ORG_ID,
                _PLACEHOLDER_USER_ID,
                None,
                None,
                None,
            )
            rejected = await _dispatch_hitl_action(
                mgr,
                AsyncMock(),
                "reject",
                uuid.uuid4(),
                gate_id,
                _PLACEHOLDER_ORG_ID,
                _PLACEHOLDER_USER_ID,
                "tok-value",
                None,
                "no",
            )

        assert claimed["status"] == "claimed"
        assert rejected["status"] == "rejected"
        mgr.claim.assert_awaited_once()
        mgr.reject.assert_awaited_once()
