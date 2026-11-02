"""Human-linked credential authority for ``human_only`` HITL gates (ADR 063 step 1).

A credential is **human-linked** when Modulo can verify — server-side, from
values it observed or derived, never from a client-asserted field — that it
belongs to a live human principal who consented to the corresponding ``hitl.*``
scope. Step 1 claims **persona** only (attribution/accountability); presence
(step 2, FAR-1953) and possession (step 3, FAR-1959) are deliberately later.

This is the resolver the MCP ``review_hitl`` gate calls
(``api.mcp_server._check_human_only_gate``). The REST gate
(``api.routes.hitl._enforce_human_only_gate``) does NOT consult it: its
principals are browser-login JWTs only, and a non-browser REST credential (a
programmatic JWT, FAR-634) is always denied a ``human_only`` gate. The
user-scoped human-linked API-key authority is therefore an MCP-only path.

Conditions (ALL must hold; any failure — including any verification error —
resolves to *not human-linked*, i.e. the deny):

1. **Credential class is human-linkable.** Either a purpose-bound signed OAuth
   token (``auth_type == "oauth"`` — set by ``McpAuthMiddleware`` only AFTER
   ``decode_oauth_access_token`` verified the signature and
   ``purpose == "oauth_access"`` — with a non-empty ``account_id`` and
   ``token_family``), OR a **user-scoped** ``mk_`` API key (``auth_type ==
   "api_key"`` and ``key_scope == "user"``). An org-scoped / team-scoped /
   run-scoped ``mk_`` key is an unattributed machine identity and is never
   human-linked.
2. **Account binding is server-derived.** OAuth: there is no
   ``client_credentials`` grant, so every OAuth access token that exists today
   was minted by the ``authorization_code`` grant from the browser approve POST;
   the server (not the client) places ``account_id`` in the signed claims.
   User-scoped key: the key row's owning ``account_id`` is read server-side.
3. **Live human principal.** ``account_id`` resolves to a globally active
   account whose ``kind == 'human'`` and to a live ``org_memberships`` row for
   the credential's org (the ADR 047 live-role clamp, reused). A service-owned
   user-scoped key therefore resolves NOT human-linked.
4. **Scope covers the action (OAuth only).** The consented grant set contains
   the corresponding ``hitl.*`` permission. A user-scoped key carries no
   consent grant-set: its authority is its live-clamped role, enforced by the
   tool/route permission chokepoint, so condition (d) does not apply to it.

Step 1 distinguishes the two linked classes in ``HumanLinkage.linkage``
(``oauth_consent`` vs ``user_scoped_key``). The MCP ``human_only`` gate honours
both only when an organisation admin enables the default-OFF
``mcp_human_linked_hitl_authority`` flag (FAR-2281); ``deliver_manual`` stays
browser-only on every surface. The REST gate honours neither (see above).

Structurally excluded: **org-scoped** ``mk_`` API keys, non-OAuth JWTs
(including browser-class password JWTs), blacklisted token families (denied
earlier at the auth boundary), inactive accounts, service principals and
absent/deactivated memberships.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, replace

from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.crud.org_membership import resolve_live_role_and_kind
from modulo.db.models.account import KIND_HUMAN

_log = logging.getLogger(__name__)

#: Auth-type vocabulary (observed, carried from the auth boundary). Values match
#: ``McpAuthMiddleware``'s ``_ctx_auth_type`` and the REST principal's
#: ``via_api_key``/``client_kind`` classification.
AUTH_TYPE_API_KEY = "api_key"
AUTH_TYPE_JWT = "jwt"
AUTH_TYPE_OAUTH = "oauth"

#: Caller-scope value that marks a per-user (human-linked) ``mk_`` key. Mirrors
#: ``modulo.auth.api_key.KEY_SCOPE_USER``; kept local so this pure resolver does
#: not import the key-mint module (which pulls the feature-flag registry).
USER_KEY_SCOPE = "user"

#: Linkage vocabulary (derived).
LINKAGE_NONE = "none"
LINKAGE_OAUTH_CONSENT = "oauth_consent"
LINKAGE_USER_SCOPED_KEY = "user_scoped_key"

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
    #: ``Account.kind`` (``human``/``service``) when derived for a linkable
    #: credential candidate, else ``None`` (not evaluated because the
    #: credential is not human-linkable at all).
    principal_kind: str | None
    linkage: str


async def _live_membership_kind(
    session: AsyncSession,
    *,
    account_id: uuid.UUID,
    org_id: uuid.UUID,
) -> tuple[str, str] | None:
    """Read ``(live_role, account.kind)``, failing CLOSED on any verification error.

    A DB/verification error must never be mistaken for a positive linkage: the
    caller treats ``None`` as "not human-linked" (the deny). ``CancelledError``
    always propagates.
    """
    try:
        return await resolve_live_role_and_kind(session, str(account_id), str(org_id))
    except Exception:
        _log.warning(
            "human_linkage.membership_read_failed",
            extra={"account_id": str(account_id), "org_id": str(org_id)},
            exc_info=True,
        )
        return None


async def resolve_human_linkage(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    auth_type: str,
    account_id: uuid.UUID | None,
    token_family: str | None,
    grants: frozenset[str] | None,
    required_permission: str,
    key_scope: str | None = None,
) -> HumanLinkage:
    """Resolve the step-1 human-linkage decision for one credential.

    Fails closed: any condition that cannot be verified — including a missing
    account, an inactive account, a service-kind account, an absent/deactivated
    membership, a missing consent scope, or a verification error — returns
    ``linked=False``. Never defaults to allow.

    ``key_scope`` is the caller-scope of an ``mk_`` credential (``org`` /
    ``user``); it is ignored for non-API-key credentials. Only a user-scoped
    key can be human-linked.
    """
    base = HumanLinkage(
        linked=False,
        auth_type=auth_type,
        account_id=account_id,
        token_family=token_family,
        principal_kind=None,
        linkage=LINKAGE_NONE,
    )
    if auth_type == AUTH_TYPE_API_KEY:
        # FAR-2281: a USER-scoped mk_ key is human-linked to its owning live
        # human. Org/team/run-scoped keys are unattributed machine identities
        # and are excluded here. Condition (b) is server-derived: the key
        # row's account_id, never a client field.
        if key_scope != USER_KEY_SCOPE or account_id is None:
            return base
        membership = await _live_membership_kind(session, account_id=account_id, org_id=org_id)
        if membership is None:
            return base
        _live_role, kind = membership
        base = replace(base, principal_kind=kind)
        if kind != KIND_HUMAN:
            return base
        # No consent grant-set on a key: authority is its live-clamped role,
        # enforced by the tool/route permission chokepoint.
        return replace(base, linked=True, linkage=LINKAGE_USER_SCOPED_KEY)

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
    membership = await _live_membership_kind(session, account_id=account_id, org_id=org_id)
    if membership is None:
        return base
    _live_role, kind = membership
    base = replace(base, principal_kind=kind)
    if kind != KIND_HUMAN:
        return base
    # (d) The consented grant set must cover the action.
    if grants is None or required_permission not in grants:
        return base
    return replace(base, linked=True, linkage=LINKAGE_OAUTH_CONSENT)


def required_permission_for_action(action: str) -> str:
    """The registry permission a human-linked credential must hold for ``action``."""
    return HITL_ACTION_PERMISSIONS.get(action, HITL_ACTION_DEFAULT_PERMISSION)
