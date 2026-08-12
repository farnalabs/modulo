"""Shared Pydantic mixin enforcing team-visibility consistency.

Reused by every team-scoped request schema (model backends, connectors,
library primitives, lifecycle maps, pipelines). Keeps the ``visibility`` /
``owner_team_id`` invariant and its error message in a single place.
"""

from __future__ import annotations

from typing import Self

from pydantic import BaseModel, model_validator


class TeamVisibilityMixin(BaseModel):
    @model_validator(mode="after")
    def _validate_team_visibility(self) -> Self:
        if self.visibility == "team" and self.owner_team_id is None:
            raise ValueError("owner_team_id is required when visibility is 'team'")
        return self
