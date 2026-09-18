"""Unit tests for modulo.api.models.team_visibility.TeamVisibilityMixin.

QA lens pass (correctness, bugs, maintainability) on the shared team-visibility
mixin. It is composed into every team-scoped request schema (model backends,
connectors, library primitives, lifecycle maps, pipelines — see
``modulo.api.routes.*``), so its single invariant — ``visibility: team``
requires an ``owner_team_id`` — must be locked down here. Route-level tests
exercise the invariant through the HTTP API; this file pins the mixin contract
directly and confirms it still composes with concrete subclasses.
"""

from __future__ import annotations

import uuid

import pytest
from pydantic import BaseModel, ValidationError

from modulo.api.models.team_visibility import TeamVisibilityMixin

TEAM_ID = "123e4567-e89b-12d3-a456-426614174000"


class _ConcreteSchema(TeamVisibilityMixin):
    name: str


class TestTeamVisibilityDefaults:
    def test_all_fields_default_to_none(self) -> None:
        schema = _ConcreteSchema(name="x")
        assert schema.visibility is None
        assert schema.owner_team_id is None

    def test_base_mixin_instantiates_with_defaults(self) -> None:
        schema = TeamVisibilityMixin()
        assert schema.visibility is None
        assert schema.owner_team_id is None


class TestTeamVisibilityRequiresOwnerTeam:
    def test_team_visibility_without_owner_team_id_rejected(self) -> None:
        with pytest.raises(ValidationError, match="owner_team_id is required when visibility is 'team'"):
            _ConcreteSchema(name="x", visibility="team")

    def test_team_visibility_with_owner_team_id_accepted(self) -> None:
        schema = _ConcreteSchema(name="x", visibility="team", owner_team_id=TEAM_ID)
        assert schema.visibility == "team"
        assert schema.owner_team_id == uuid.UUID(TEAM_ID)

    def test_team_visibility_accepts_uuid_object(self) -> None:
        team_uuid = uuid.uuid4()
        schema = _ConcreteSchema(name="x", visibility="team", owner_team_id=team_uuid)
        assert schema.owner_team_id == team_uuid


class TestTeamVisibilityOtherValues:
    def test_org_visibility_does_not_require_owner_team_id(self) -> None:
        schema = _ConcreteSchema(name="x", visibility="org")
        assert schema.visibility == "org"
        assert schema.owner_team_id is None

    def test_org_visibility_with_owner_team_id_accepted(self) -> None:
        schema = _ConcreteSchema(name="x", visibility="org", owner_team_id=TEAM_ID)
        assert schema.visibility == "org"
        assert schema.owner_team_id == uuid.UUID(TEAM_ID)

    def test_arbitrary_visibility_without_owner_team_id_accepted(self) -> None:
        schema = _ConcreteSchema(name="x", visibility="public")
        assert schema.visibility == "public"
        assert schema.owner_team_id is None


class TestTeamVisibilityComposition:
    def test_mixin_ignores_unrelated_fields(self) -> None:
        class _WithExtra(TeamVisibilityMixin):
            name: str
            enabled: bool

        schema = _WithExtra(name="x", enabled=False)
        assert schema.model_dump() == {
            "visibility": None,
            "owner_team_id": None,
            "name": "x",
            "enabled": False,
        }

    def test_base_model_round_trip_preserves_valid_state(self) -> None:
        schema = _ConcreteSchema(name="x", visibility="team", owner_team_id=TEAM_ID)
        assert _ConcreteSchema.model_validate(schema.model_dump()).model_dump() == schema.model_dump()

    def test_mixin_does_not_mutate_subclass_fields(self) -> None:
        schema = _ConcreteSchema(name="x", visibility="team", owner_team_id=TEAM_ID)
        assert schema.model_fields_set == {"name", "visibility", "owner_team_id"}
        assert isinstance(schema, BaseModel)
