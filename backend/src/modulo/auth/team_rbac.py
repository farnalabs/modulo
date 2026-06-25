"""Team-level RBAC: role hierarchy and effective access model.

Org roles and team roles share the same hierarchy levels, but a user's
team role is capped by their org role — the effective team role is the
lower of the two.
"""

TEAM_ROLE_HIERARCHY: dict[str, int] = {
    "viewer": 0,
    "runner": 1,
    "operator": 2,
    "admin": 3,
}

ORG_ROLE_HIERARCHY: dict[str, int] = {
    "viewer": 0,
    "runner": 1,
    "operator": 2,
    "admin": 3,
}

VALID_TEAM_ROLES = frozenset(TEAM_ROLE_HIERARCHY)
VALID_ORG_ROLES = frozenset(ORG_ROLE_HIERARCHY)

# Pre-computed effective role for every (org_role, team_role) pair.
# effective = min(org_role_level, team_role_level)
EFFECTIVE_ACCESS_MODEL: dict[str, dict[str, str]] = {
    "viewer":  {"viewer": "viewer", "runner": "viewer", "operator": "viewer", "admin": "viewer"},
    "runner":  {"viewer": "viewer", "runner": "runner", "operator": "runner", "admin": "runner"},
    "operator":{"viewer": "viewer", "runner": "runner", "operator": "operator", "admin": "operator"},
    "admin":   {"viewer": "viewer", "runner": "runner", "operator": "operator", "admin": "admin"},
}


def get_effective_team_role(org_role: str, team_role: str) -> str:
    """Return the effective team role after applying the privilege cap.

    The effective role is the lower of the org role and team role in the
    hierarchy.  If either input is unrecognised the fallback is ``viewer``.
    """
    return EFFECTIVE_ACCESS_MODEL.get(org_role, {}).get(team_role, "viewer")


def team_role_level(role: str) -> int:
    """Return the numeric level for a team role (or -1 if unknown)."""
    return TEAM_ROLE_HIERARCHY.get(role, -1)


def org_role_level(role: str) -> int:
    """Return the numeric level for an org role (or -1 if unknown)."""
    return ORG_ROLE_HIERARCHY.get(role, -1)
