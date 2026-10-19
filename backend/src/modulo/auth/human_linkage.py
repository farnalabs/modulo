"""Human-linked credential authority for ``human_only`` HITL gates (ADR 063 step 1).

A credential is **human-linked** when Modulo can verify — server-side, from
values it observed or derived, never from a client-asserted field — that it
belongs to a live human principal who consented to the corresponding ``hitl.*``
scope. Step 1 claims **persona** only (attribution/accountability); presence
(step 2, FAR-1953) and possession (step 3, FAR-1959) are deliberately later.

This is the ONE resolver both surfaces call: the MCP ``review_hitl`` gate
(``api.mcp_server._check_human_only_gate``) and the REST gate
(``api.routes.hitl._enforce_human_only_gate``). The historical asymmetry — REST
short-circuiting on ``client_kind == "browser"`` while MCP denied via a
hard-coded ``non_browser_credential=True`` — was an implementation accident, not
a designed boundary.

Conditions (ALL must hold; any failure — including any verification error —
resolves to *not human-linked*, i.e. the deny):

1. **Purpose-bound signed token.** ``auth_type == "oauth"`` — set by
   ``McpAuthMiddleware`` only AFTER ``decode_oauth_access_token`` verified the
   signature and ``purpose == "oauth_access"`` — with a non-empty
   ``account_id`` and ``token_family``.
2. **Consent-derived account binding.** There is no ``client_credentials``
   grant, so every OAuth access token that exists today was minted by the
   ``authorization_code`` grant from the browser approve POST; the server (not
   the client) places ``account_id`` in the signed claims.
3. **Live human principal.** ``account_id`` resolves to a globally active
   account whose ``kind == 'human'`` and to a live ``org_memberships`` row for
   the token's org (the ADR 047 live-role clamp, reused).
4. **Scope covers the action.** The consented grant set contains the
   corresponding ``hitl.*`` permission.

Structurally excluded: ``mk_`` API keys, non-OAuth JWTs (including
browser-class password JWTs), blacklisted token families (denied earlier at
the auth boundary), inactive accounts and absent/deactivated memberships.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.crud.org_membership import resolve_live_role_and_kind
from modulo.db.models.account import KIND_HUMAN

#: Auth-type vocabulary (observed, carried from the auth boundary). Values match
#: ``McpAuthMiddleware``'s ``_ctx_auth_type`` and the REST principal's
#: ``via_api_key``/``client_kind`` classification.
AUTH_TYPE_API_KEY = "api_key"
AUTH_TYPE_JWT = "jwt"
AUTH_TYPE_OAUTH = "oauth"

#: Linkage vocabulary (derived).
LINKAGE_NONE = "none"
LINKAGE_OAUTH_CONSENT = "oauth_consent"

#: Registry permission key for approval decisions. Both ``approve`` and
#: ``approve_with_modification`` map to it, and an unmapped action defaults to
#: it (approval is the strictest common HITL decision, so an unknown action
#: fails to the highest decision bar rather than an unverified scope).
HITL_APPROVE_PERMISSION = "hitl.approve"

#: The consented scope the ``review_hitl`` / REST actions require. The values are
#: the registry permission keys (``auth.permissions``); ``hitl.*`` is delegable
#: (FAR-1476), so a consenting human's grant set can carry them.
HITL_ACTION_PERMISSIONS: dict[str, str] = {
    "claim": "hitl.claim",
    "approve": HITL_APPROVE_PERMISSION,
    "approve_with_modification": HITL_APPROVE_PERMISSION,
    "deliver_manual": "hitl.deliver_manual",
    "submit_manual": "hitl.deliver_manual",
    "reject": "hitl.reject",
}
#: Permission required when the action is not in the map above.
HITL_ACTION_DEFAULT_PERMISSION = HITL_APPROVE_PERMISSION


@dataclass(frozen=True)
class HumanLinkage:
    """How (and whether) a credential is human-linked, with per-field provenance.

    ``auth_type``/``account_id``/``token_family`` are **observed** (from the
    verified credential and the signed token claims); ``principal_kind`` and
    ``linkage`` are **derived** (from the account row, live membership and the
    consent conditions). No client-supplied field is represented here.
    """

    linked: bool
    auth_type: str
    account_id: uuid.UUID | None
    token_family: str | None
    #: ``Account.kind`` (``human``/``service``) when derived for an OAuth
    #: candidate, else ``None`` (not evaluated because the credential is not an
    #: OAuth candidate — the only class that can be human-linked).
    principal_kind: str | None
    linkage: str


async def resolve_human_linkage(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    auth_type: str,
    account_id: uuid.UUID | None,
    token_family: str | None,
    grants: frozenset[str] | None,
    required_permission: str,
) -> HumanLinkage:
    """Resolve the step-1 human-linkage decision for one credential.

    Fails closed: any condition that cannot be verified — including a missing
    account, an inactive account, a service-kind account, an absent/deactivated
    membership, or a missing consent scope — returns ``linked=False``. Never
    defaults to allow.
    """
    base = HumanLinkage(
        linked=False,
        auth_type=auth_type,
        account_id=account_id,
        token_family=token_family,
        principal_kind=None,
        linkage=LINKAGE_NONE,
    )
    # (a) Purpose-bound signed OAuth access token with a non-empty account_id
    # and token_family. `auth_type == "oauth"` is only ever set after
    # `decode_oauth_access_token` verified the signature and purpose; the raw
    # bearer is never re-read or trusted here.
    if auth_type != AUTH_TYPE_OAUTH or account_id is None or not token_family:
        return base
    # (b) Consent-derived: there is no `client_credentials` grant, so every
    # OAuth access token is `authorization_code`-minted from the browser
    # approve POST — no separate check needed; it is a property of the flow.
    # (c) Live human principal: active account (kind == 'human') + live
    # membership, via the reused ADR 047 live-role clamp.
    membership = await resolve_live_role_and_kind(session, str(account_id), str(org_id))
    if membership is None:
        return base
    _live_role, kind = membership
    base = HumanLinkage(
        linked=base.linked,
        auth_type=base.auth_type,
        account_id=base.account_id,
        token_family=base.token_family,
        principal_kind=kind,
        linkage=base.linkage,
    )
    if kind != KIND_HUMAN:
        return base
    # (d) The consented grant set must cover the action.
    if grants is None or required_permission not in grants:
        return base
    return HumanLinkage(
        linked=True,
        auth_type=base.auth_type,
        account_id=base.account_id,
        token_family=base.token_family,
        principal_kind=base.principal_kind,
        linkage=LINKAGE_OAUTH_CONSENT,
    )


def required_permission_for_action(action: str) -> str:
    """The registry permission a human-linked credential must hold for ``action``."""
    return HITL_ACTION_PERMISSIONS.get(action, HITL_ACTION_DEFAULT_PERMISSION)
