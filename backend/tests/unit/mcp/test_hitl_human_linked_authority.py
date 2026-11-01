"""ADR 063 step 1 (FAR-1957): human-linked MCP authority for ``human_only`` gates.

Drives the REAL ``review_hitl`` tool through the REAL ``_check_human_only_gate``
and the REAL shared resolver (``auth.human_linkage.resolve_human_linkage``);
only the DB seam behind the resolver (``resolve_live_role_and_kind``), the
org-flag read and the session/manager seams are patched.

The anchor is ``test_human_linked_oauth_token_may_claim_a_human_only_gate_via_mcp``:
against pre-ADR-063 ``origin/main`` the gate hard-coded ``non_browser_credential
=True``, so the SAME scenario returns ``{"error": "human_only_gate"}``. This
module exercises the post-step-1 path, so on the pre-change tree it fails (the
new resolver module does not exist and the verdict is the blanket deny).
"""

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

from modulo.api.mcp_server import _hitl_human_only_denial_payload, review_hitl
from modulo.auth.human_linkage import (
    LINKAGE_NONE,
    LINKAGE_OAUTH_CONSENT,
    HumanLinkage,
)

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000011")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000013")
_TOKEN_FAMILY = "oauth-family-1"
_HITL_GRANTS = frozenset(
    {
        "hitl.review",
        "hitl.claim",
        "hitl.approve",
        "hitl.reject",
        "hitl.deliver_manual",
    }
)
#: Grants WITHOUT the action's consent scope (keeps ``hitl.review`` so the base
#: tool is permitted, but omits the per-action key the scope chokepoint needs).
_GRANTS_WITHOUT_CLAIM = frozenset({"hitl.review", "hitl.approve"})


def _make_session_context(session: AsyncMock) -> AsyncMock:
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _make_run_lookup_result(row: MagicMock | None = None) -> MagicMock:
    result = MagicMock()
    result.scalar_one_or_none.return_value = row if row is not None else MagicMock()
    return result


def _run_and_snapshot(human_only: bool) -> tuple[MagicMock, MagicMock, str]:
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


def _linked() -> HumanLinkage:
    return HumanLinkage(
        linked=True,
        auth_type="oauth",
        account_id=_USER_ID,
        token_family=_TOKEN_FAMILY,
        principal_kind="human",
        linkage=LINKAGE_OAUTH_CONSENT,
    )


def _session_with_run(run: MagicMock, snapshot: MagicMock) -> AsyncMock:
    session = AsyncMock()
    session.execute = AsyncMock(
        side_effect=[
            _make_run_lookup_result(run),
            # claim-stamp lookup (no stamped row — legacy)
            _make_run_lookup_result(None),
            _make_run_lookup_result(snapshot),
        ]
    )
    return session


@contextmanager
def _mcp_patches(
    session: AsyncMock,
    manager: MagicMock,
    *,
    flag_enabled: bool = True,
    membership: tuple[str, str] | None = ("operator", "human"),
) -> Iterator[None]:
    """Patch the auth/session/manager seams plus the resolver's DB + flag legs."""
    with (
        patch("modulo.api.mcp_server.validate_current_auth", return_value=True),
        patch("modulo.api.mcp_server.HITLManager", return_value=manager),
        patch("modulo.api.mcp_server._session", return_value=_make_session_context(session)),
        patch(
            "modulo.api.mcp_server.resolve_mcp_human_linked_hitl_authority",
            AsyncMock(return_value=flag_enabled),
        ),
        patch(
            "modulo.auth.human_linkage.resolve_live_role_and_kind",
            AsyncMock(return_value=membership),
        ),
    ):
        yield


class _OAuthContext:
    """Hydrate the MCP ContextVars as a consent-derived OAuth credential."""

    def setup_method(self) -> None:
        from modulo.api.mcp_server import (
            _ctx_auth_token,
            _ctx_auth_type,
            _ctx_key_grants,
            _ctx_key_scope,
            _ctx_org_id,
            _ctx_role,
            _ctx_token_family,
            _ctx_user_id,
        )

        _ctx_org_id.set(_ORG_ID)
        _ctx_role.set("operator")
        _ctx_user_id.set(_USER_ID)
        _ctx_auth_token.set("oauth-access-token")
        _ctx_auth_type.set("oauth")
        _ctx_token_family.set(_TOKEN_FAMILY)
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
            _ctx_token_family,
            _ctx_user_id,
        )

        _ctx_org_id.set(None)
        _ctx_role.set(None)
        _ctx_user_id.set(None)
        _ctx_auth_token.set(None)
        _ctx_auth_type.set(None)
        _ctx_token_family.set(None)
        _ctx_key_scope.set(None)
        _ctx_key_grants.set(None)


class TestHumanLinkedMcpAuthority(_OAuthContext):
    @patch("modulo.api.mcp_server._append_hitl_human_only_denied_audit", new=AsyncMock())
    async def test_human_linked_oauth_token_may_claim_a_human_only_gate_via_mcp(self) -> None:
        """THE anchor (ADR 063 step 1): a human-linked OAuth token may claim.

        Fails today (pre-change) with ``{"error": "human_only_gate"}`` because
        the gate passed a hard-coded ``non_browser_credential=True``; passes
        after step 1 because the verdict is computed from the linkage.
        """
        run, snapshot, review_id = _run_and_snapshot(human_only=True)
        session = _session_with_run(run, snapshot)
        manager = MagicMock()
        gate = MagicMock()
        gate.claim_token = "claim-abc"
        gate.expires_at = None
        manager.claim = AsyncMock(return_value=gate)

        with _mcp_patches(session, manager):
            result = await review_hitl(run_id=str(run.id), review_id=review_id, action="claim")

        assert result.get("status") == "claimed", result
        manager.claim.assert_awaited_once()

    @patch("modulo.api.mcp_server._append_hitl_human_only_denied_audit", new=AsyncMock())
    async def test_org_flag_off_restores_the_blanket_denial(self) -> None:
        """The org-wide escape hatch restores today's exact MCP denial."""
        run, snapshot, review_id = _run_and_snapshot(human_only=True)
        session = _session_with_run(run, snapshot)
        manager = MagicMock()
        manager.claim = AsyncMock()

        with _mcp_patches(session, manager, flag_enabled=False):
            result = await review_hitl(run_id=str(run.id), review_id=review_id, action="claim")

        assert result.get("error") == "human_only_gate", result
        manager.claim.assert_not_called()

    @patch("modulo.api.mcp_server._append_hitl_human_only_denied_audit", new=AsyncMock())
    async def test_deliver_manual_denied_even_when_human_linked(self) -> None:
        """``deliver_manual`` stays browser-only in step 1 (ADR 035)."""
        run, snapshot, review_id = _run_and_snapshot(human_only=True)
        session = _session_with_run(run, snapshot)
        manager = MagicMock()
        manager.deliver_manual = AsyncMock()

        with _mcp_patches(session, manager, flag_enabled=True):
            result = await review_hitl(
                run_id=str(run.id),
                review_id=review_id,
                action="deliver_manual",
                claim_token="claim-abc",
                output={"status": "ok"},
            )

        assert result.get("error") == "human_only_gate", result
        manager.deliver_manual.assert_not_called()


class TestMcpNonLinkedDenials(_OAuthContext):
    """Every non-human-linked credential stays denied on a human_only gate."""

    @patch("modulo.api.mcp_server._append_hitl_human_only_denied_audit", new=AsyncMock())
    async def test_api_key_denied(self) -> None:
        from modulo.api.mcp_server import _ctx_auth_type, _ctx_key_scope, _ctx_token_family

        _ctx_auth_type.set("api_key")
        _ctx_token_family.set(None)
        # An org-scoped ``mk_`` key passes the caller-scope chokepoint so the
        # human_only policy hook (not the scope leg) is what denies it here.
        _ctx_key_scope.set("org")
        run, snapshot, review_id = _run_and_snapshot(human_only=True)
        session = _session_with_run(run, snapshot)
        manager = MagicMock()

        with _mcp_patches(session, manager, membership=None):
            result = await review_hitl(run_id=str(run.id), review_id=review_id, action="claim")

        assert result.get("error") == "human_only_gate", result

    async def test_user_scoped_human_key_denied_in_slice_1(self) -> None:
        """FAR-2281 slice 1: a user-scoped mk_ key IS human-linked (persona) but
        the human_only decision authority is unchanged — only the consent-derived
        OAuth class may decide. Slice 2 extends this behind the persona flag.

        Drives the REAL ``_check_human_only_gate`` directly: ``review_hitl`` is
        itself org-scoped, so a user-scoped key is refused by the caller-scope
        chokepoint before this hook (an unchanged slice-1 behaviour tested in
        ``test_mcp_user_key_scope_gate.py``). The denial here proves the
        linkage resolved to the user-scoped class yet was NOT granted HITL
        authority.
        """
        from modulo.api.mcp_server import (
            _check_human_only_gate,
            _ctx_auth_type,
            _ctx_key_grants,
            _ctx_key_scope,
            _ctx_token_family,
        )
        from modulo.auth.human_linkage import LINKAGE_USER_SCOPED_KEY
        from modulo.db.crud.hitl_review_config import MSG_HUMAN_ONLY_DENY

        _ctx_auth_type.set("api_key")
        _ctx_token_family.set(None)
        _ctx_key_scope.set("user")
        _ctx_key_grants.set(None)
        run, snapshot, review_id = _run_and_snapshot(human_only=True)
        session = AsyncMock()
        # The direct gate call skips the run read: (1) claim-stamp lookup (no
        # row — legacy), (2) the run's snapshot graph.
        session.execute = AsyncMock(
            side_effect=[
                _make_run_lookup_result(None),
                _make_run_lookup_result(snapshot),
            ]
        )
        audit = AsyncMock()

        with (
            _mcp_patches(session, MagicMock(), membership=("admin", "human")),
            patch("modulo.api.mcp_server._append_hitl_human_only_denied_audit", new=audit),
        ):
            verdict = await _check_human_only_gate(session, _ORG_ID, run, review_id, "claim")

        assert verdict is not None
        assert verdict["error"] == "human_only_gate"
        assert verdict["detail"] == MSG_HUMAN_ONLY_DENY
        audit.assert_awaited_once()
        linkage = audit.await_args.kwargs["linkage"]
        assert linkage.linked is True
        assert linkage.linkage == LINKAGE_USER_SCOPED_KEY
        assert linkage.principal_kind == "human"

    @patch("modulo.api.mcp_server._append_hitl_human_only_denied_audit", new=AsyncMock())
    async def test_browser_class_password_jwt_denied(self) -> None:
        """FAR-634's path stays closed: a non-OAuth JWT is never human-linked."""
        from modulo.api.mcp_server import _ctx_auth_type, _ctx_token_family

        _ctx_auth_type.set("jwt")
        _ctx_token_family.set(None)
        run, snapshot, review_id = _run_and_snapshot(human_only=True)
        session = _session_with_run(run, snapshot)
        manager = MagicMock()

        with _mcp_patches(session, manager, membership=None):
            result = await review_hitl(run_id=str(run.id), review_id=review_id, action="claim")

        assert result.get("error") == "human_only_gate", result

    @patch("modulo.api.mcp_server._append_hitl_human_only_denied_audit", new=AsyncMock())
    async def test_service_account_oauth_denied(self) -> None:
        run, snapshot, review_id = _run_and_snapshot(human_only=True)
        session = _session_with_run(run, snapshot)
        manager = MagicMock()

        with _mcp_patches(session, manager, membership=("operator", "service")):
            result = await review_hitl(run_id=str(run.id), review_id=review_id, action="claim")

        assert result.get("error") == "human_only_gate", result

    @patch("modulo.api.mcp_server._append_hitl_human_only_denied_audit", new=AsyncMock())
    async def test_inactive_membership_denied(self) -> None:
        run, snapshot, review_id = _run_and_snapshot(human_only=True)
        session = _session_with_run(run, snapshot)
        manager = MagicMock()

        with _mcp_patches(session, manager, membership=None):
            result = await review_hitl(run_id=str(run.id), review_id=review_id, action="claim")

        assert result.get("error") == "human_only_gate", result

    @patch("modulo.api.mcp_server._append_hitl_human_only_denied_audit", new=AsyncMock())
    async def test_missing_hitl_scope_is_a_normal_scope_denial(self) -> None:
        """Without the action's ``hitl.*`` scope the scope chokepoint denies."""
        from modulo.api.mcp_server import _ctx_key_grants

        _ctx_key_grants.set(_GRANTS_WITHOUT_CLAIM)
        run, snapshot, review_id = _run_and_snapshot(human_only=True)
        session = _session_with_run(run, snapshot)
        manager = MagicMock()

        with _mcp_patches(session, manager):
            result = await review_hitl(run_id=str(run.id), review_id=review_id, action="claim")

        assert result.get("error") == "insufficient_scope", result


class TestDenialAuditProvenance:
    def test_payload_records_observed_and_derived_provenance(self) -> None:
        payload = _hitl_human_only_denial_payload(
            run_id=uuid.uuid4(),
            review_id="hitl_review_a_b",
            action="claim",
            verdict="denied",
            linkage=_linked(),
        )
        assert payload["surface"] == "mcp"
        assert payload["auth_type"] == "oauth"
        assert payload["account_id"] == str(_USER_ID)
        assert payload["token_family"] == _TOKEN_FAMILY
        assert payload["principal_kind"] == "human"
        assert payload["linkage"] == LINKAGE_OAUTH_CONSENT
        assert payload["reason"] == "denied"
        # The pre-ADR-063 always-None client_kind field is gone (a field that
        # is always None cannot be distinguished from one never evaluated).
        assert "client_kind" not in payload

    def test_payload_for_api_key_has_no_derived_kind(self) -> None:
        linkage = HumanLinkage(
            linked=False,
            auth_type="api_key",
            account_id=None,
            token_family=None,
            principal_kind=None,
            linkage=LINKAGE_NONE,
        )
        payload = _hitl_human_only_denial_payload(
            run_id=uuid.uuid4(),
            review_id="hitl_review_a_b",
            action="approve",
            verdict="denied",
            linkage=linkage,
        )
        assert payload["auth_type"] == "api_key"
        assert payload["account_id"] is None
        assert payload["principal_kind"] is None
        assert payload["linkage"] == LINKAGE_NONE
