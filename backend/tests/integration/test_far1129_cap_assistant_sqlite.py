"""FAR-1129 cap-assistant: SkillLoader against a REAL async DB (SQLite) boundary.

The existing Assistant tests mock ``AsyncSession`` end-to-end, so the SQL the
``SkillLoader`` actually executes — the ``AssistantSkill`` / ``Account`` /
``OrgMembership`` / ``Organisation`` and ``AssistantConfigService`` /
``AssistantContextSourceService`` reads — is never exercised against a real
database. Here the loader runs against a REAL ``aiosqlite`` async engine with
REAL ORM models, seeded through the ORM, so the queries, result mapping, and
the failure-isolation boundary are validated as executed by production code.

Boundary notes (stated honestly in the PR): the SQLite in-memory engine cannot
represent the Postgres-specific ``gen_random_uuid()`` server defaults, so seed
rows carry explicit ids; the ``AssistantRedisRegistry`` (cap-assistant's key-value half)
needs a real Redis server and lives behind the container-gated integration
tests, which CI runs against the Docker Compose Postgres/Redis stack.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from modulo.core.assistant.skill_loader import SkillLoader
from modulo.db.models.account import Account
from modulo.db.models.assistant_context_source import AssistantContextSource
from modulo.db.models.assistant_skill import AssistantSkill
from modulo.db.models.base import Base
from modulo.db.models.org_membership import OrgMembership
from modulo.db.models.organisation import Organisation
from modulo.db.models.system_config import SystemConfig

pytestmark = pytest.mark.integration

# The system_config key AssistantConfigService reads for org-level Assistant config.
_ASSISTANT_CONFIG_KEY_PREFIX = "assistant_config:"


@pytest.fixture(scope="module")
async def _sqlite_session():
    engine = create_async_engine("sqlite+aiosqlite://")
    tables = [
        Organisation.__table__,
        Account.__table__,
        OrgMembership.__table__,
        AssistantSkill.__table__,
        SystemConfig.__table__,
        AssistantContextSource.__table__,
    ]
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=tables))

    session_factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield session_factory
    await engine.dispose()


async def _seed_org_context(session: AsyncSession) -> dict[str, uuid.UUID]:
    org_id = uuid.uuid4()
    user_id = uuid.uuid4()
    token = org_id.hex[:8]
    session.add(
        Organisation(
            id=org_id,
            name=f"Loopback Org {token}",
            slug=f"loopback-{token}",
            status="active",
            settings_json={},
            otel_config_json={},
        )
    )
    session.add(
        Account(
            id=user_id,
            email=f"assistant-{token}@loopback.local",
            display_name=f"Assistant Tester {token}",
            password_hash=None,
            auth_provider="local",
            active=True,
            must_change_password=False,
            is_system_admin=False,
            is_break_glass=False,
        )
    )
    session.add(OrgMembership(organisation_id=org_id, account_id=user_id, role="admin"))
    session.add(
        SystemConfig(
            id=uuid.uuid4(),
            key=f"{_ASSISTANT_CONFIG_KEY_PREFIX}{org_id}",
            value={
                "system_prompt": "You are the Modulo assistant.",
                "additional_guidance": "Answer tersely.",
            },
        )
    )
    await session.flush()
    return {"org_id": org_id, "user_id": user_id}


async def test_build_system_prompt_reads_real_rows(
    _sqlite_session: sessionmaker[AsyncSession],  # noqa: PT019
) -> None:
    async with _sqlite_session() as session:
        ids = await _seed_org_context(session)
        session.add(
            AssistantSkill(
                id=uuid.uuid4(),
                organisation_id=ids["org_id"],
                account_id=None,
                name="Git hygiene",
                description="Commit conventions",
                triggers=["commit"],
                body="- one change per commit\n",
                active=True,
                source_mode="always_on",
            )
        )
        session.add(
            AssistantSkill(
                id=uuid.uuid4(),
                organisation_id=None,
                account_id=ids["user_id"],
                name="Personal prefix",
                description="",
                triggers=[],
                body="- sign with 'via Modulo'\n",
                active=True,
                source_mode="always_on",
            )
        )
        await session.commit()

        loader = SkillLoader(session)
        prompt = await loader.build_system_prompt(ids["org_id"], ids["user_id"], page_context="Dashboard")

    assert "You are the Modulo assistant." in prompt
    assert "Answer tersely." in prompt
    assert "Loopback Org" in prompt
    assert "Assistant Tester" in prompt
    assert "admin" in prompt
    assert "Git hygiene" in prompt
    assert "- one change per commit" in prompt
    assert "Personal prefix" in prompt
    assert "- sign with 'via Modulo'" in prompt


async def test_inactive_skill_is_not_loaded(
    _sqlite_session: sessionmaker[AsyncSession],  # noqa: PT019
) -> None:
    async with _sqlite_session() as session:
        ids = await _seed_org_context(session)
        session.add(
            AssistantSkill(
                id=uuid.uuid4(),
                organisation_id=ids["org_id"],
                account_id=None,
                name="Retired skill",
                description="",
                triggers=[],
                body="- should not appear\n",
                active=False,
                source_mode="always_on",
            )
        )
        await session.commit()

        loader = SkillLoader(session)
        prompt = await loader.build_system_prompt(ids["org_id"], ids["user_id"])

    assert "Retired skill" not in prompt
    assert "- should not appear" not in prompt


async def test_missing_table_degrades_gracefully() -> None:
    """A database error (e.g. a missing table) does not crash the run."""
    engine = create_async_engine("sqlite+aiosqlite://")
    session_factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        # No tables created — the skills query raises SQLAlchemyError.
        loader = SkillLoader(session)
        prompt = await loader.build_system_prompt(uuid.uuid4(), uuid.uuid4())

    assert isinstance(prompt, str)
    await engine.dispose()


async def test_missing_profile_is_absent_but_prompt_builds(
    _sqlite_session: sessionmaker[AsyncSession],  # noqa: PT019
) -> None:
    async with _sqlite_session() as session:
        unknown_org = uuid.uuid4()
        unknown_user = uuid.uuid4()
        loader = SkillLoader(session)
        prompt = await loader.build_system_prompt(unknown_org, unknown_user)

    # No profile section is emitted for an unknown user/org; the pipeline still
    # gets a usable prompt (the Behaviour section is always present).
    assert prompt
    assert "## Behaviour" in prompt
    assert "Assistant" not in prompt
