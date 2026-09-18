"""Team-level RBAC: role hierarchy and effective access model.

Org roles and team roles share the same hierarchy levels, but a user's
team role is capped by their org role — the effective team role is the
lower of the two.
"""

import logging

_log = logging.getLogger(__name__)

TEAM_ROLE_HIERARCHY: dict[str, int] = {
    "viewer": 0,
    "runner": 1,
    "operator": 2,
}

ORG_ROLE_HIERARCHY: dict[str, int] = {
    "viewer": 0,
    "runner": 1,
    "operator": 2,
    "admin": 3,
}

VALID_TEAM_ROLES = frozenset(TEAM_ROLE_HIERARCHY)
VALID_ORG_ROLES = frozenset(ORG_ROLE_HIERARCHY)


def get_effective_team_role(org_role: str, team_role: str) -> str:
    """Return the effective team role after applying the privilege cap.

    The effective role is the lower of the org role and team role in the
    hierarchy.  If either input is unrecognised the fallback is ``viewer``.
    """
    org = ORG_ROLE_HIERARCHY.get(org_role, -1)
    team = TEAM_ROLE_HIERARCHY.get(team_role, -1)
    if org == -1 or team == -1:
        _log.warning(
            "rbac.unknown_role_fallback",
            extra={"org_role": org_role, "team_role": team_role, "fallback": "viewer"},
        )
        return "viewer"
    effective_lvl = min(org, team)
    for role, lvl in sorted(TEAM_ROLE_HIERARCHY.items(), key=lambda x: -x[1]):
        if lvl <= effective_lvl:
            return role
    return "viewer"


def team_role_level(role: str) -> int:
    """Return the numeric level for a team role (or -1 if unknown)."""
    return TEAM_ROLE_HIERARCHY.get(role, -1)


def org_role_level(role: str) -> int:
    """Return the numeric level for an org role (or -1 if unknown)."""
    return ORG_ROLE_HIERARCHY.get(role, -1)
