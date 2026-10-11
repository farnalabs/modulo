"""Principal-kind classification and human-path exclusion (FAR-1385 slice 1).

ADR 056 / ADR 058 make every actor in Modulo a **principal**. ``Account.kind``
classifies one as ``human`` or ``service``. This module is the single shared
vocabulary and the small set of helpers every human-only path uses, so the
rules are stated once rather than re-checked ad hoc at each call site.

Two rules from the ADRs are encoded here:

* **Human-path exclusion** — a service principal must never reach a human-only
  surface (login, invite, SSO/SCIM, ``/me``, ...). :func:`is_service_account` is
  the shared predicate; a service row's ``email`` / ``auth_provider`` /
  ``password_hash`` being NULL is the structural reason most lookups miss it,
  and this helper is the explicit second line for the paths that load by
  primary key.
* **Registry-level exclusion** — the delegable-permission boundary is NOT
  re-listed here. :func:`service_permission_allowed` delegates straight to
  FAR-1477's :func:`modulo.auth.permissions.is_delegable`, the one registry flag
  read live at enforcement and mint time. A second list would be a fourth grant
  vocabulary (ADR 058 explicitly forbids it).

``clamp_service_role`` is the ``ceiling <= operator`` rule (ADR 058: service
principals are org-owned, never a super-principal). It is consumed by the
service-account mint/assignment surface that slice 2 (FAR-1386) delivers; the
clamp is defined here now so that surface cannot grant above the ceiling.
"""

from __future__ import annotations

from typing import Any

from modulo.auth.permissions import is_delegable
from modulo.auth.team_rbac import ORG_ROLE_HIERARCHY
from modulo.db.models.account import ACCOUNT_KINDS, KIND_HUMAN, KIND_SERVICE

__all__ = [
    "KIND_HUMAN",
    "KIND_SERVICE",
    "SERVICE_ROLE_CEILING",
    "account_kind",
    "clamp_service_role",
    "is_human_account",
    "is_service_account",
    "service_permission_allowed",
]

#: Service principals hold team roles with a ceiling of ``operator`` (ADR 058:
#: org-owned, never a super-principal).
SERVICE_ROLE_CEILING = "operator"

#: Alias used for the human kind's unset/transient default.
_DEFAULT_KIND = KIND_HUMAN


def account_kind(account: Any) -> str:
    """Return the principal kind of ``account`` with an honest default.

    A not-yet-flushed transient row (``kind`` unset) is ``human``: it is the
    server default and, in slice 1, the only kind any existing row can be.
    An unrecognised value is also treated as ``human`` for *classification*
    only — the DB CHECK rejects unrecognised values on the way in.
    """
    kind = getattr(account, "kind", None)
    return kind if kind in ACCOUNT_KINDS else _DEFAULT_KIND


def is_service_account(account: Any) -> bool:
    """True when ``account`` is a service principal."""
    return account_kind(account) == KIND_SERVICE


def is_human_account(account: Any) -> bool:
    """True when ``account`` is a human principal (the default)."""
    return not is_service_account(account)


def service_permission_allowed(permission: str) -> bool:
    """May a service principal be granted ``permission``?

    Delegates to FAR-1477's registry-level exclusion flag
    (:func:`modulo.auth.permissions.is_delegable`) — deliberately NOT a second
    exclusion list. Unknown keys deny (fail-closed), exactly as the grant
    resolver does.
    """
    return is_delegable(permission)


def clamp_service_role(role: str | None) -> str:
    """Clamp a role assigned to a service principal to the ``operator`` ceiling.

    Returns ``""`` (the deny sentinel) for an unknown/absent role, so a caller
    that already clamps through :func:`modulo.auth.permissions._clamp_role`
    keeps its fail-closed behaviour instead of widening. A service principal
    can never hold ``admin`` (ADR 058).
    """
    level = ORG_ROLE_HIERARCHY.get(role) if role is not None else None
    if level is None:
        return ""
    ceiling = ORG_ROLE_HIERARCHY[SERVICE_ROLE_CEILING]
    effective = min(level, ceiling)
    for name, name_level in ORG_ROLE_HIERARCHY.items():
        if name_level == effective:
            return name
    return ""
