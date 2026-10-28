"""Runtime guards on the S107 keyword-option groups (agent / env profile / library).

Each grouped function takes ``**options: Unpack[TypedDict]``: mypy validates
keyword names and types at the call site, while the body keeps a runtime
``TypeError`` for unknown keywords (a ``dict[str, Any]`` splat bypasses the
static check) and, for parameters that were required before the grouping, for
a missing one — notably ``create_library_primitive(visibility=...)``, which
must never silently default to the org-wide tier.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.crud.agent import create_agent
from modulo.db.crud.environment_profile import create_environment_profile
from modulo.db.crud.library_primitive import create_library_primitive


def _library_primitive_kwargs(**overrides: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "org_id": uuid.uuid4(),
        "source": "bundled",
        "primitive_type": "prompt",
        "name": "primitive",
        "slug": "primitive",
        "description": None,
        "author": "tester",
        "version": "1.0.0",
        "tags": [],
        "content_json": {},
        "account_id": None,
        "source_url": None,
        "forked_from": None,
        "checksum": None,
        "ed25519_signature": None,
        "verified": None,
        "download_count": None,
        "average_rating": None,
        "review_count": None,
        "owner_team_id": None,
        "visibility": "team",
    }
    kwargs.update(overrides)
    return kwargs


class TestLibraryPrimitiveRequiredOptions:
    async def test_required_options_and_defaults_reach_the_row(self) -> None:
        """Every previously-required option is accepted and passed through;
        only ``auto_update``/``tier`` fall back to their documented defaults."""
        session = AsyncMock(spec=AsyncSession)
        kwargs = _library_primitive_kwargs(visibility="team", auto_update=False, tier="community")
        with patch("modulo.db.crud.library_primitive.LibraryPrimitive") as model:
            await create_library_primitive(session, **kwargs)

        row_kwargs = model.call_args.kwargs
        assert row_kwargs["visibility"] == "team"
        assert row_kwargs["auto_update"] is False
        assert row_kwargs["tier"] == "community"
        assert row_kwargs["checksum"] is None
        session.add.assert_called_once_with(model.return_value)

    async def test_defaults_apply_when_optional_options_are_omitted(self) -> None:
        session = AsyncMock(spec=AsyncSession)
        with patch("modulo.db.crud.library_primitive.LibraryPrimitive") as model:
            await create_library_primitive(session, **_library_primitive_kwargs())

        row_kwargs = model.call_args.kwargs
        assert row_kwargs["auto_update"] is True
        assert row_kwargs["tier"] == "native"

    async def test_missing_visibility_raises_instead_of_defaulting(self) -> None:
        """``visibility`` was a required keyword parameter before the grouping."""
        kwargs = _library_primitive_kwargs()
        kwargs.pop("visibility")
        with pytest.raises(TypeError, match="missing required keyword argument: 'visibility'"):
            await create_library_primitive(session=AsyncMock(spec=AsyncSession), **kwargs)

    async def test_missing_nullable_required_option_raises(self) -> None:
        """A previously-required (but nullable) parameter stays required."""
        kwargs = _library_primitive_kwargs()
        kwargs.pop("checksum")
        with pytest.raises(TypeError, match="missing required keyword argument: 'checksum'"):
            await create_library_primitive(session=AsyncMock(spec=AsyncSession), **kwargs)

    async def test_unknown_option_raises(self) -> None:
        with pytest.raises(TypeError, match="unexpected create_library_primitive options"):
            await create_library_primitive(
                session=AsyncMock(spec=AsyncSession), **_library_primitive_kwargs(unexpected_option=True)
            )


class TestGroupedCrudUnknownOptions:
    async def test_create_agent_rejects_unknown_option(self) -> None:
        kwargs: dict[str, Any] = {
            "session": None,
            "org_id": uuid.uuid4(),
            "name": "planner",
            "account_id": uuid.uuid4(),
            "prompt_template": "prompt",
            "unexpected_option": True,
        }
        with pytest.raises(TypeError, match="unexpected create_agent options"):
            await create_agent(**kwargs)

    async def test_create_environment_profile_rejects_unknown_option(self) -> None:
        kwargs: dict[str, Any] = {
            "session": None,
            "org_id": uuid.uuid4(),
            "name": "profile",
            "account_id": uuid.uuid4(),
            "provider_type": "docker",
            "unexpected_option": True,
        }
        with pytest.raises(TypeError, match="unexpected create_environment_profile options"):
            await create_environment_profile(**kwargs)

    async def test_create_environment_profile_accepts_a_known_option_set(self) -> None:
        """The no-unknown-options path still persists the row with its defaults."""
        session = AsyncMock(spec=AsyncSession)
        await create_environment_profile(
            session,
            org_id=uuid.uuid4(),
            name="profile",
            account_id=uuid.uuid4(),
            provider_type="docker",
            visibility="team",
        )

        profile = session.add.call_args.args[0]
        assert profile.visibility == "team"
        assert profile.network_policy == "outbound"
        assert profile.persistence_policy == "ephemeral"
        session.flush.assert_awaited_once()
