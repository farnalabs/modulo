"""Shared constants and factories for SCIM unit tests."""

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

if TYPE_CHECKING:
    from farna.settings import Settings

VALID_32 = "a" * 32
ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
TEAM_ID = uuid.UUID("00000000-0000-0000-0000-000000000003")
NOW = datetime(2025, 1, 1, tzinfo=UTC)

SCIM_TOKEN = "test-scim-token-12345"
AUTH_HEADERS = {"Authorization": f"Bearer {SCIM_TOKEN}"}

USER_CREATE_BODY = {
    "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
    "userName": "jane@example.com",
    "name": {"givenName": "Jane", "familyName": "Doe"},
    "emails": [{"value": "jane@example.com", "primary": True}],
    "active": True,
}

GROUP_CREATE_BODY = {
    "schemas": ["urn:ietf:params:scim:schemas:core:2.0:Group"],
    "displayName": "Engineering",
    "members": [{"value": str(USER_ID), "type": "User"}],
}

PATCH_USER_BODY = {
    "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
    "Operations": [{"op": "replace", "path": "active", "value": False}],
}

PATCH_GROUP_ADD_MEMBER = {
    "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
    "Operations": [{"op": "add", "path": "members", "value": [{"value": str(USER_ID)}]}],
}


def make_settings(**overrides: object) -> "Settings":
    """Build a minimal Settings for SCIM tests; override any field via kwargs."""
    from farna.settings import Settings

    base: dict[str, object] = {
        "database_url": "postgresql+asyncpg://localhost/test",
        "secret_key": VALID_32,
        "fernet_key": VALID_32,
        "modulo_admin_password": "testpass",
        "modulo_license_key": "team-license",
        "modulo_scim_token": SCIM_TOKEN,
        "modulo_public_url": "http://localhost:8000",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def make_mock_session() -> MagicMock:
    """Build a MagicMock session whose transaction context is fully mocked."""
    session = MagicMock()
    session.execute = AsyncMock(return_value=MagicMock())
    session.flush = AsyncMock()
    session.delete = AsyncMock()
    session.rollback = AsyncMock()
    session.refresh = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


def make_user(**overrides: object) -> MagicMock:
    """Build a user row double; any column can be overridden via kwargs."""
    user = MagicMock()
    user.id = overrides.get("id", USER_ID)
    user.organisation_id = overrides.get("organisation_id", ORG_ID)
    user.email = overrides.get("email", "jane@example.com")
    user.display_name = overrides.get("display_name", "Jane Doe")
    user.active = overrides.get("active", True)
    user.org_role = overrides.get("org_role", "runner")
    user.auth_provider = overrides.get("auth_provider", "scim")
    user.created_at = overrides.get("created_at", NOW)
    user.updated_at = overrides.get("updated_at", NOW)
    return user


def make_team(**overrides: object) -> MagicMock:
    """Build a team row double; any column can be overridden via kwargs."""
    team = MagicMock()
    team.id = overrides.get("id", TEAM_ID)
    team.organisation_id = overrides.get("organisation_id", ORG_ID)
    team.name = overrides.get("name", "Engineering")
    team.description = overrides.get("description")
    team.created_by = overrides.get("created_by", USER_ID)
    team.created_at = overrides.get("created_at", NOW)
    team.updated_at = overrides.get("updated_at", NOW)
    return team


def make_membership(**overrides: object) -> MagicMock:
    """Build a team-membership row double; any column can be overridden via kwargs."""
    membership = MagicMock()
    membership.id = overrides.get("id", uuid.uuid4())
    membership.team_id = overrides.get("team_id", TEAM_ID)
    membership.user_id = overrides.get("user_id", USER_ID)
    membership.role = overrides.get("role", "member")
    membership.created_at = overrides.get("created_at", NOW)
    return membership


def only_org_owns(user_id: uuid.UUID) -> object:
    """Side effect for ``scim_get_user``: only ``user_id`` is visible to the caller.

    SCIM looks users up scoped to the caller's organisation, so a foreign-org
    id resolves to ``None`` and must be skipped rather than added to a group.
    """
    default_user = make_user()

    def _get_user(_session: MagicMock, _org_id: uuid.UUID, uid: uuid.UUID) -> MagicMock | None:
        return default_user if uid == user_id else None

    return _get_user
