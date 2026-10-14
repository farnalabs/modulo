"""FAR-1476: HITL decisions are delegable — the boundary is the gate's ``human_only`` policy.

Decision record (2026-10-09, Duncan): the five ``hitl.*`` decision keys were
removed from ``NON_DELEGABLE_PERMISSIONS``. A human-linked credential (a
human's OAuth connection) MAY hold them — the registry no longer blocks the
grant. The real boundary is the gate's ``human_only`` policy, enforced at
RUNTIME: the MCP surface denies a ``human_only`` gate outright regardless of
credential class (``mcp_server._check_human_only_gate``) and REST denies a
non-browser credential (``routes/hitl._enforce_human_only_gate``).

This module pins BOTH halves of that split with ONE token through the REAL
``review_hitl`` tool path (only the auth re-validation, the DB session seam
and the HITLManager seam are patched):

* a grant-set carrying the HITL decision keys (incl. ``hitl.review``) CAN
  decide a non-``human_only`` gate — the scope chokepoint lets it through and
  the manager's approve actually runs; and
* the SAME grant-set is DENIED on a ``human_only`` gate — by the runtime
  policy (``human_only_gate``), never by a registry bar.

Fail-before / pass-after: against the pre-fix registry (the ``hitl.*`` keys in
``NON_DELEGABLE_PERMISSIONS``) BOTH halves fail, because the scope chokepoint
rejects the grant-set with ``insufficient_scope`` BEFORE the policy hook is
reached. After the fix both halves pass, and the ``human_only`` denial comes
from ``_check_human_only_gate``.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from modulo.api.mcp_server import review_hitl
from modulo.auth.permissions import is_delegable

_PLACEHOLDER_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000011")
_PLACEHOLDER_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000013")
#: A human's OAuth token grant-set: the review surface plus every decision
#: action the ``review_hitl`` tool can dispatch. One token, used for BOTH the
#: allowed and the denied leg below.
_HITL_GRANTS = frozenset(
    {
        "hitl.review",
        "hitl.claim",
        "hitl.approve",
        "hitl.reject",
        "hitl.deliver_manual",
    }
)


def _make_session_context(session: AsyncMock) -> AsyncMock:
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _make_run_lookup_result(run: MagicMock | None = None) -> MagicMock:
    """A session.execute() result that resolves a row (run / snapshot / miss)."""
    result = MagicMock()
    result.scalar_one_or_none.return_value = run if run is not None else MagicMock()
    return result


class _DelegatedTokenContext:
    """Hydrate the MCP request ContextVars as a human-linked OAuth token.

    Mirrors ``mcp_server``'s OAuth dispatch: an operator principal whose
    grant-set is the HITL decision set (``_ctx_key_grants``), auth class
    ``oauth`` / caller scope ``user`` — the credential class the widened
    registry decision (2026-10-09) is about.
    """

    def setup_method(self) -> None:
        from modulo.api.mcp_server import (
            _ctx_auth_token,
            _ctx_auth_type,
            _ctx_key_grants,
            _ctx_key_scope,
            _ctx_org_id,
            _ctx_role,
            _ctx_user_id,
        )

        _ctx_org_id.set(_PLACEHOLDER_ORG_ID)
        _ctx_role.set("operator")
        _ctx_user_id.set(_PLACEHOLDER_USER_ID)
        _ctx_auth_token.set("oauth-access-token")
        _ctx_auth_type.set("oauth")
        _ctx_key_scope.set("user")
        _ctx_key_grants.set(_HITL_GRANTS)

    def teardown_method(self) -> None:
        from modulo.api.mcp_server import (
            _ctx_auth_token,
            _ctx_auth_type,
            _ctx_key_grants,
            _ctx_key_scope,
            _ctx_org_id,
            _ctx_role,
            _ctx_user_id,
        )

        _ctx_org_id.set(None)
        _ctx_role.set(None)
        _ctx_user_id.set(None)
        _ctx_auth_token.set(None)
        _ctx_auth_type.set(None)
        _ctx_key_scope.set(None)
        _ctx_key_grants.set(None)


def _run_and_snapshot(human_only: bool) -> tuple[MagicMock, MagicMock, str]:
    """A run whose frozen snapshot graph carries ONE gated edge.

    The returned ``review_id`` is the topology-derived gate id
    (``hitl_review_<source>_<target>``) for that exact edge, so the shared
    config resolver finds it by topology (never by position).
    """
    src = uuid.uuid4()
    tgt = uuid.uuid4()
    review_id = f"hitl_review_{src}_{tgt}"
    run = MagicMock()
    run.id = uuid.uuid4()
    run.snapshot_id = uuid.uuid4()
    snapshot = MagicMock()
    snapshot.graph_json = {
        "nodes": [],
        "edges": [
            {"source": str(src), "target": str(tgt), "hitl_review_config": {"human_only": human_only}},
        ],
    }
    return run, snapshot, review_id


class TestHitlDelegationTokenBoundary(_DelegatedTokenContext):
    """One delegated token: allowed on a non-``human_only`` gate, denied on one."""

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server.HITLManager")
    @patch("modulo.api.mcp_server._session")
    async def test_hitl_grant_decides_non_human_only_gate(
        self,
        mock_session: AsyncMock,
        mock_manager_cls: MagicMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        """A token holding the HITL decision keys CAN decide an opted-out gate.

        The scope chokepoint (``_check_agent_tool_scope`` -> ``check_tool_scope``
        -> ``resolve_tool_access`` leg 5) must let the grant-set through, and the
        real ``HITLManager.approve`` dispatch must then run. Before the registry
        change this leg returned ``insufficient_scope`` (the registry bar denied
        ``hitl.approve`` regardless of the gate's policy).
        """
        run, snapshot, review_id = _run_and_snapshot(human_only=False)

        manager = MagicMock()
        manager.approve = AsyncMock()
        mock_sesh = AsyncMock()
        mock_sesh.execute = AsyncMock(
            side_effect=[
                _make_run_lookup_result(run),
                # FAR-634 claim-stamp lookup (no stamped row - legacy).
                _make_run_lookup_result(None),
                _make_run_lookup_result(snapshot),
            ]
        )
        mock_session.return_value = _make_session_context(mock_sesh)
        mock_manager_cls.return_value = manager

        result = await review_hitl(run_id=str(run.id), review_id=review_id, action="approve", claim_token="tok-123")

        assert result.get("status") == "approved", result
        assert result.get("review_id") == review_id
        manager.approve.assert_awaited_once()

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server.HITLManager")
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.api.mcp_server._append_hitl_human_only_denied_audit", new=AsyncMock())
    async def test_same_hitl_grant_denied_at_runtime_on_human_only_gate(
        self,
        mock_session: AsyncMock,
        mock_manager_cls: MagicMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        """The SAME token is DENIED on a ``human_only`` gate — by the policy.

        The denial shape must be the runtime ``human_only_gate`` verdict from
        ``_check_human_only_gate`` (MCP denies ``human_only`` gates outright,
        regardless of credential class), NOT the scope chokepoint's
        ``insufficient_scope`` — that is the distinction the registry bar used
        to blur: removing it must open non-``human_only`` gates while leaving
        ``human_only`` gates exactly as closed.
        """
        run, snapshot, review_id = _run_and_snapshot(human_only=True)

        manager = MagicMock()
        manager.approve = AsyncMock()
        mock_sesh = AsyncMock()
        mock_sesh.execute = AsyncMock(
            side_effect=[
                _make_run_lookup_result(run),
                _make_run_lookup_result(None),
                _make_run_lookup_result(snapshot),
            ]
        )
        mock_session.return_value = _make_session_context(mock_sesh)
        mock_manager_cls.return_value = manager

        result = await review_hitl(run_id=str(run.id), review_id=review_id, action="approve", claim_token="tok-123")

        assert result.get("error") == "human_only_gate", result
        manager.approve.assert_not_called()

    def test_hitl_decision_keys_are_registry_delegable(self) -> None:
        """The registry itself must offer the keys (the decision-record half).

        Pins 3a directly: if any ``hitl.*`` decision key returns to
        ``NON_DELEGABLE_PERMISSIONS``, the delegated-credential story collapses
        and the two tests above fail at the scope chokepoint.
        """
        for key in ("hitl.review", "hitl.claim", "hitl.approve", "hitl.reject", "hitl.deliver_manual"):
            assert is_delegable(key) is True, key
