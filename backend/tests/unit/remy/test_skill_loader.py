"""Unit tests for SkillLoader — YAML frontmatter parsing and system prompt assembly."""

import asyncio
import uuid
from collections.abc import Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.exc import SQLAlchemyError

from modulo.core.remy.config_service import RemyConfig
from modulo.core.remy.skill_loader import SkillEntry, SkillLoader
from modulo.db.models.remy_skill import RemySkill


def _mock_skill(
    name: str,
    body: str = "Body",
    source_mode: str | None = None,
    **kwargs: Any,
) -> MagicMock:
    skill = MagicMock(spec=RemySkill)
    skill.id = uuid.uuid4()
    skill.name = name
    skill.description = kwargs.get("description")
    skill.triggers = kwargs.get("triggers")
    skill.body = body
    skill.active = True
    skill.source_mode = source_mode
    return skill


class TestParseSkillMarkdown:
    """Tests for SkillLoader.parse_skill_markdown static method."""

    def test_with_valid_frontmatter(self) -> None:
        md = """---
name: code-review
triggers: [on_pr, on_push]
active: true
---
Review all code changes for security vulnerabilities."""
        fm, body = SkillLoader.parse_skill_markdown(md)
        assert fm is not None
        assert fm["name"] == "code-review"
        assert fm["triggers"] == ["on_pr", "on_push"]
        assert fm["active"] is True
        assert "security vulnerabilities" in body

    def test_with_no_frontmatter(self) -> None:
        md = "Just a plain skill body without frontmatter."
        fm, body = SkillLoader.parse_skill_markdown(md)
        assert fm is None
        assert body == md

    def test_with_empty_input(self) -> None:
        fm, body = SkillLoader.parse_skill_markdown("")
        assert fm is None
        assert body == ""

    def test_with_none_input(self) -> None:
        fm, body = SkillLoader.parse_skill_markdown(None)
        assert fm is None
        assert body == ""

    def test_with_whitespace_only_input(self) -> None:
        fm, body = SkillLoader.parse_skill_markdown("   \n  ")
        assert fm is None
        assert body == "   \n  "

    def test_with_malformed_frontmatter_no_end(self) -> None:
        md = """---
name: broken"""
        fm, body = SkillLoader.parse_skill_markdown(md)
        assert fm is None
        assert body == md

    def test_with_empty_frontmatter(self) -> None:
        md = """---
---
Body content only."""
        fm, body = SkillLoader.parse_skill_markdown(md)
        assert fm == {}
        assert body == "Body content only."

    def test_with_boolean_false_value(self) -> None:
        md = """---
active: false
---
Content."""
        fm, _ = SkillLoader.parse_skill_markdown(md)
        assert fm is not None
        assert fm["active"] is False

    def test_with_quoted_string_value(self) -> None:
        md = '---\ndescription: "A skill description"\n---\nBody.'
        fm, _ = SkillLoader.parse_skill_markdown(md)
        assert fm is not None
        assert fm["description"] == "A skill description"

    def test_with_list_values_ignore_quotes(self) -> None:
        md = """---
tags: ['tag1', "tag2", tag3]
---
Body."""
        fm, _ = SkillLoader.parse_skill_markdown(md)
        assert fm is not None
        assert fm["tags"] == ["tag1", "tag2", "tag3"]

    def test_with_blank_lines_in_frontmatter(self) -> None:
        md = """---
name: test

version: 2
---
Body."""
        fm, _ = SkillLoader.parse_skill_markdown(md)
        assert fm is not None
        assert fm["name"] == "test"
        assert fm["version"] == "2"

    def test_with_missing_colon_skips_line(self) -> None:
        md = """---
name: test
invalid line no colon
version: 3
---
Body."""
        fm, _ = SkillLoader.parse_skill_markdown(md)
        assert fm is not None
        assert fm["name"] == "test"
        assert fm["version"] == "3"
        assert "invalid line no colon" not in str(fm)

    def test_with_empty_key_skips_line(self) -> None:
        md = """---
name: test
: value with no key
version: 3
---
Body."""
        fm, _ = SkillLoader.parse_skill_markdown(md)
        assert fm is not None
        assert fm["name"] == "test"
        assert fm["version"] == "3"
        assert "value with no key" not in str(fm)

    def test_body_stripped_of_leading_whitespace(self) -> None:
        md = """---
name: test
---

    Indented body content."""
        fm, body = SkillLoader.parse_skill_markdown(md)
        assert fm is not None
        assert body.startswith("Indented"), f"Expected stripped body, got: {body!r}"


class TestSkillLoaderGetSkills:
    """Tests for SkillLoader.get_org_skills and get_user_skills."""

    @pytest.fixture
    def loader(self, mock_session: AsyncMock) -> SkillLoader:
        return SkillLoader(mock_session)

    async def test_get_org_skills_returns_skill_entries(self, loader: SkillLoader, mock_session: AsyncMock) -> None:
        mock_skill = _mock_skill("code-review", source_mode=None, description="Review code changes")

        scalars_mock = MagicMock()
        scalars_mock.all = MagicMock(return_value=[mock_skill])
        mock_result = MagicMock()
        mock_result.scalars = MagicMock(return_value=scalars_mock)
        mock_session.execute = AsyncMock(return_value=mock_result)

        skills = await loader.get_org_skills(uuid.uuid4())
        assert len(skills) == 1
        assert isinstance(skills[0], SkillEntry)
        assert skills[0].name == "code-review"
        assert skills[0].source_mode is None

    async def test_get_org_skills_empty(self, loader: SkillLoader, mock_session: AsyncMock) -> None:
        scalars_mock = MagicMock()
        scalars_mock.all = MagicMock(return_value=[])
        mock_result = MagicMock()
        mock_result.scalars = MagicMock(return_value=scalars_mock)
        mock_session.execute = AsyncMock(return_value=mock_result)

        skills = await loader.get_org_skills(uuid.uuid4())
        assert skills == []

    async def test_get_user_skills_returns_skill_entries(self, loader: SkillLoader, mock_session: AsyncMock) -> None:
        mock_skill = _mock_skill("my-prompt", source_mode=None, description=None)

        scalars_mock = MagicMock()
        scalars_mock.all = MagicMock(return_value=[mock_skill])
        mock_result = MagicMock()
        mock_result.scalars = MagicMock(return_value=scalars_mock)
        mock_session.execute = AsyncMock(return_value=mock_result)

        skills = await loader.get_user_skills(uuid.uuid4())
        assert len(skills) == 1
        assert skills[0].name == "my-prompt"

    async def test_get_user_skills_empty(self, loader: SkillLoader, mock_session: AsyncMock) -> None:
        scalars_mock = MagicMock()
        scalars_mock.all = MagicMock(return_value=[])
        mock_result = MagicMock()
        mock_result.scalars = MagicMock(return_value=scalars_mock)
        mock_session.execute = AsyncMock(return_value=mock_result)

        skills = await loader.get_user_skills(uuid.uuid4())
        assert skills == []

    async def test_get_skills_unknown_filter_column_is_ignored(
        self, loader: SkillLoader, mock_session: AsyncMock
    ) -> None:
        mock_skill = _mock_skill("code-review")
        scalars_mock = MagicMock()
        scalars_mock.all = MagicMock(return_value=[mock_skill])
        mock_result = MagicMock()
        mock_result.scalars = MagicMock(return_value=scalars_mock)
        mock_session.execute = AsyncMock(return_value=mock_result)

        skills = await loader._get_skills(organisation_id=uuid.uuid4(), not_a_real_column="x")
        assert len(skills) == 1
        assert skills[0].name == "code-review"

    async def test_get_skills_returns_empty_on_sqlalchemy_error(
        self, loader: SkillLoader, mock_session: AsyncMock
    ) -> None:
        mock_session.execute = AsyncMock(side_effect=SQLAlchemyError("db down"))

        skills = await loader.get_org_skills(uuid.uuid4())
        assert skills == []

    async def test_get_skills_reraises_cancelled_error(self, loader: SkillLoader, mock_session: AsyncMock) -> None:
        mock_session.execute = AsyncMock(side_effect=asyncio.CancelledError())

        with pytest.raises(asyncio.CancelledError):
            await loader.get_org_skills(uuid.uuid4())

    async def test_get_skills_returns_empty_on_unexpected_error(
        self, loader: SkillLoader, mock_session: AsyncMock
    ) -> None:
        mock_session.execute = AsyncMock(side_effect=RuntimeError("boom"))

        skills = await loader.get_org_skills(uuid.uuid4())
        assert skills == []


class TestSkillLoaderBuildUserProfile:
    """Tests for SkillLoader._build_user_profile."""

    @pytest.fixture
    def loader(self, mock_session: AsyncMock) -> SkillLoader:
        return SkillLoader(mock_session)

    def _result(self, scalar_value: Any) -> MagicMock:
        result = MagicMock()
        result.scalar_one_or_none = MagicMock(return_value=scalar_value)
        return result

    async def test_builds_full_profile_when_all_relations_present(
        self, loader: SkillLoader, mock_session: AsyncMock
    ) -> None:
        account = MagicMock()
        account.display_name = "Ada Lovelace"
        account.email = "ada@example.com"
        membership = MagicMock()
        membership.role = "admin"
        org = MagicMock()
        org.name = "Acme"
        org.plan_id = "team"

        mock_session.execute = AsyncMock(
            side_effect=[self._result(account), self._result(membership), self._result(org)]
        )

        profile = await loader._build_user_profile(uuid.uuid4(), uuid.uuid4())
        assert profile is not None
        assert "User Profile" in profile
        assert "**Name:** Ada Lovelace" in profile
        assert "**Email:** ada@example.com" in profile
        assert "**Role:** admin" in profile
        assert "**Organisation:** Acme" in profile
        assert "**Plan:** team" in profile

    async def test_builds_minimal_profile_without_relations(self, loader: SkillLoader, mock_session: AsyncMock) -> None:
        account = MagicMock()
        account.display_name = ""
        account.email = "ada@example.com"

        mock_session.execute = AsyncMock(side_effect=[self._result(account), self._result(None), self._result(None)])

        profile = await loader._build_user_profile(uuid.uuid4(), uuid.uuid4())
        assert profile is not None
        assert "User Profile" in profile
        assert "**Name:** —" in profile
        assert "**Email:** ada@example.com" in profile
        assert "**Role:**" not in profile
        assert "**Organisation:**" not in profile

    async def test_returns_none_when_account_not_found(self, loader: SkillLoader, mock_session: AsyncMock) -> None:
        mock_session.execute = AsyncMock(return_value=self._result(None))

        profile = await loader._build_user_profile(uuid.uuid4(), uuid.uuid4())
        assert profile is None

    async def test_returns_none_on_sqlalchemy_error(self, loader: SkillLoader, mock_session: AsyncMock) -> None:
        mock_session.execute = AsyncMock(side_effect=SQLAlchemyError("db down"))

        profile = await loader._build_user_profile(uuid.uuid4(), uuid.uuid4())
        assert profile is None

    async def test_returns_none_on_unexpected_error(self, loader: SkillLoader, mock_session: AsyncMock) -> None:
        mock_session.execute = AsyncMock(side_effect=RuntimeError("boom"))

        profile = await loader._build_user_profile(uuid.uuid4(), uuid.uuid4())
        assert profile is None

    async def test_reraises_cancelled_error(self, loader: SkillLoader, mock_session: AsyncMock) -> None:
        mock_session.execute = AsyncMock(side_effect=asyncio.CancelledError())

        with pytest.raises(asyncio.CancelledError):
            await loader._build_user_profile(uuid.uuid4(), uuid.uuid4())


class _CtxSourceStub:
    """Stub that returns a config with context source modes, overridable per key."""

    def __init__(self, overrides: dict[str, str] | None = None) -> None:
        self._overrides = overrides or {}

    async def get_effective_config(self, org_id: uuid.UUID, user_id: uuid.UUID) -> RemyConfig:
        builtins: dict[str, str] = {
            "page_context": "always_on",
            "user_profile": "always_on",
            "product_primer": "always_on",
            "product_docs": "tool",
            "integration_status": "tool",
            "org_config": "tool",
            "feature_overview": "tool",
        }
        merged = dict(builtins)
        merged.update(self._overrides)
        cfg = RemyConfig()
        cfg.context_sources = merged
        return cfg


class _ConfigServiceStub:
    """Stub that returns a canned RemyConfig."""

    def __init__(self, **attrs: Any) -> None:
        self._config = RemyConfig(**attrs)

    async def get_config(self, org_id: uuid.UUID) -> RemyConfig:
        return self._config


class TestSkillLoaderBuildSystemPrompt:
    """Tests for SkillLoader.build_system_prompt."""

    @pytest.fixture
    def loader(self, mock_session: AsyncMock) -> SkillLoader:
        return SkillLoader(mock_session)

    async def _run(
        self,
        loader: SkillLoader,
        org_id: uuid.UUID,
        user_id: uuid.UUID,
        config_kwargs: dict[str, Any] | None = None,
        ctx_overrides: dict[str, str] | None = None,
        page_context: str | None = None,
        system_prompt_override: str | None = None,
        include_ui_tools_text: bool = False,
        ui_tools_text_fn: Callable[[], str] | None = None,
    ) -> str:
        cfg_stub = _ConfigServiceStub(**(config_kwargs or {}))
        ctx_stub = _CtxSourceStub(overrides=ctx_overrides)
        with (
            patch.object(loader, "_config_service", cfg_stub),
            patch.object(loader, "_ui_tools_text_fn", ui_tools_text_fn),
            patch("modulo.core.remy.skill_loader.RemyContextSourceService", return_value=ctx_stub),
        ):
            return await loader.build_system_prompt(
                org_id=org_id,
                user_id=user_id,
                page_context=page_context,
                system_prompt_override=system_prompt_override,
                include_ui_tools_text=include_ui_tools_text,
            )

    # ── Existing tests (adapted to new architecture) ──────────────────

    async def test_with_config_system_prompt_only(
        self,
        loader: SkillLoader,
        mock_session: AsyncMock,
        org_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> None:
        prompt = await self._run(
            loader, org_id, user_id, config_kwargs={"system_prompt": "You are a helpful assistant."}
        )
        assert "You are a helpful assistant." in prompt
        assert "Organisation Skills" not in prompt
        assert "User Skills" not in prompt

    async def test_with_page_context(
        self,
        loader: SkillLoader,
        mock_session: AsyncMock,
        org_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> None:
        prompt = await self._run(
            loader,
            org_id,
            user_id,
            config_kwargs={"system_prompt": "System prompt."},
            page_context="User is on the Reports page",
        )
        assert "Page Context" in prompt
        assert "User is on the Reports page" in prompt

    async def test_with_org_and_user_skills(
        self,
        loader: SkillLoader,
        mock_session: AsyncMock,
        org_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> None:
        org_skill = _mock_skill("org-skill", body="Org skill body", source_mode=None)
        user_skill = _mock_skill("user-skill", body="User skill body", source_mode=None)

        org_scalars = MagicMock()
        org_scalars.all = MagicMock(return_value=[org_skill])
        user_scalars = MagicMock()
        user_scalars.all = MagicMock(return_value=[user_skill])

        org_result = MagicMock()
        org_result.scalars = MagicMock(return_value=org_scalars)
        user_result = MagicMock()
        user_result.scalars = MagicMock(return_value=user_scalars)

        mock_session.execute = AsyncMock(
            side_effect=[org_result, user_result],
        )

        prompt = await self._run(
            loader,
            org_id,
            user_id,
            config_kwargs={"system_prompt": "", "additional_guidance": ""},
            ctx_overrides={
                "user_profile": "off",
                "product_docs": "off",
                "integration_status": "off",
                "org_config": "off",
                "feature_overview": "off",
                "product_primer": "off",
            },
        )
        assert "Organisation Skills" in prompt
        assert "org-skill" in prompt
        assert "Org skill body" in prompt
        assert "User Skills" in prompt
        assert "user-skill" in prompt
        assert "User skill body" in prompt

    async def test_with_additional_guidance(
        self,
        loader: SkillLoader,
        mock_session: AsyncMock,
        org_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> None:
        prompt = await self._run(
            loader,
            org_id,
            user_id,
            config_kwargs={"system_prompt": "You are helpful.", "additional_guidance": "Always be concise."},
        )
        assert "You are helpful." in prompt
        assert "Always be concise." in prompt

    async def test_with_no_config_or_skills(
        self,
        loader: SkillLoader,
        mock_session: AsyncMock,
        org_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> None:
        prompt = await self._run(
            loader,
            org_id,
            user_id,
            config_kwargs={"system_prompt": "", "additional_guidance": ""},
            ctx_overrides={
                "product_docs": "off",
                "integration_status": "off",
                "org_config": "off",
                "feature_overview": "off",
                "user_profile": "off",
                "product_primer": "off",
            },
        )
        assert prompt.startswith("## Behaviour\n\n")
        assert "direct visual access to the application UI" in prompt
        assert prompt.count("## ") == 1

    async def test_with_include_ui_tools_text_false_excludes_tools(
        self,
        loader: SkillLoader,
        mock_session: AsyncMock,
        org_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> None:
        prompt = await self._run(loader, org_id, user_id, config_kwargs={"system_prompt": "You are helpful."})
        assert "Browser Tools Available (Text Mode)" not in prompt

    async def test_with_include_ui_tools_text_true_includes_tools(
        self,
        loader: SkillLoader,
        mock_session: AsyncMock,
        org_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> None:
        prompt = await self._run(
            loader,
            org_id,
            user_id,
            config_kwargs={"system_prompt": "You are helpful."},
            include_ui_tools_text=True,
            ui_tools_text_fn=lambda: "# Browser Tools Available (Text Mode)\n**navigate**(path: 'url')\n",
        )
        assert "Browser Tools Available (Text Mode)" in prompt
        assert "**navigate**(path:" in prompt

    # ── New tests for context source filtering ────────────────────────

    async def test_product_primer_included_when_always_on(
        self,
        loader: SkillLoader,
        mock_session: AsyncMock,
        org_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> None:
        prompt = await self._run(
            loader, org_id, user_id, config_kwargs={"system_prompt": "Base.", "product_primer": "We build Modulo."}
        )
        assert "Product Overview" in prompt
        assert "We build Modulo." in prompt

    async def test_product_primer_skipped_when_off(
        self,
        loader: SkillLoader,
        mock_session: AsyncMock,
        org_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> None:
        prompt = await self._run(
            loader,
            org_id,
            user_id,
            config_kwargs={"system_prompt": "Base.", "product_primer": "We build Modulo."},
            ctx_overrides={"product_primer": "off"},
        )
        assert "Product Overview" not in prompt

    async def test_product_primer_skipped_when_empty(
        self,
        loader: SkillLoader,
        mock_session: AsyncMock,
        org_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> None:
        prompt = await self._run(
            loader, org_id, user_id, config_kwargs={"system_prompt": "Base.", "product_primer": ""}
        )
        assert "Product Overview" not in prompt

    async def test_knowledge_tools_section_includes_tool_sources(
        self,
        loader: SkillLoader,
        mock_session: AsyncMock,
        org_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> None:
        prompt = await self._run(loader, org_id, user_id, config_kwargs={"system_prompt": "Base."})
        assert "Available Knowledge Tools" in prompt
        assert "search_documentation" in prompt
        assert "get_integration_status" in prompt
        assert "get_org_config" in prompt
        assert "get_available_features" in prompt

    async def test_knowledge_tools_skipped_when_no_tool_sources(
        self,
        loader: SkillLoader,
        mock_session: AsyncMock,
        org_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> None:
        prompt = await self._run(
            loader,
            org_id,
            user_id,
            config_kwargs={"system_prompt": "Base."},
            ctx_overrides={
                "product_docs": "off",
                "integration_status": "off",
                "org_config": "off",
                "feature_overview": "off",
            },
        )
        assert "Available Knowledge Tools" not in prompt

    async def test_user_profile_included_when_always_on(
        self,
        loader: SkillLoader,
        mock_session: AsyncMock,
        org_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> None:
        prompt = await self._run(
            loader,
            org_id,
            user_id,
            config_kwargs={"system_prompt": "Base."},
            ctx_overrides={
                "user_profile": "always_on",
                "product_docs": "off",
                "integration_status": "off",
                "org_config": "off",
                "feature_overview": "off",
            },
        )
        # No DB match => no profile block, but method is called
        assert "User Profile" not in prompt

    async def test_user_profile_skipped_when_off(
        self,
        loader: SkillLoader,
        mock_session: AsyncMock,
        org_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> None:
        prompt = await self._run(
            loader,
            org_id,
            user_id,
            config_kwargs={"system_prompt": "Base."},
            ctx_overrides={
                "user_profile": "off",
                "product_docs": "off",
                "integration_status": "off",
                "org_config": "off",
                "feature_overview": "off",
            },
        )
        assert "User Profile" not in prompt

    async def test_skills_filtered_by_source_mode(
        self,
        loader: SkillLoader,
        mock_session: AsyncMock,
        org_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> None:
        always_on = _mock_skill("always-on-skill", source_mode="always_on", body="Always")
        tool_mode = _mock_skill("tool-skill", source_mode="tool", body="Tool")
        off_mode = _mock_skill("off-skill", source_mode="off", body="Off")
        null_mode = _mock_skill("null-skill", source_mode=None, body="Null")

        scalars = MagicMock()
        scalars.all = MagicMock(return_value=[always_on, tool_mode, off_mode, null_mode])
        mock_result = MagicMock()
        mock_result.scalars = MagicMock(return_value=scalars)

        # user_profile is off → only 2 execute calls (org_skills, user_skills)
        mock_session.execute = AsyncMock(
            side_effect=[mock_result, mock_result],
        )

        prompt = await self._run(
            loader,
            org_id,
            user_id,
            config_kwargs={"system_prompt": "Base."},
            ctx_overrides={
                "product_docs": "off",
                "integration_status": "off",
                "org_config": "off",
                "feature_overview": "off",
                "user_profile": "off",
                "product_primer": "off",
            },
        )
        # always-on and null skills appear in the skills block
        assert "always-on-skill" in prompt
        assert "Always" in prompt
        assert "null-skill" in prompt
        assert "Null" in prompt

        # tool-mode skill is NOT injected directly
        assert "tool-skill" not in prompt

        # off-mode skill is absent entirely
        assert "off-skill" not in prompt

    async def test_tool_skills_listed_in_knowledge_tools(
        self,
        loader: SkillLoader,
        mock_session: AsyncMock,
        org_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> None:
        tool_skill = _mock_skill("qa-review", source_mode="tool", description="Review quality")
        always_on_skill = _mock_skill("auto-fix", source_mode="always_on", body="Auto")

        scalars = MagicMock()
        scalars.all = MagicMock(return_value=[tool_skill, always_on_skill])
        mock_result = MagicMock()
        mock_result.scalars = MagicMock(return_value=scalars)

        mock_session.execute = AsyncMock(
            side_effect=[mock_result, mock_result],
        )

        prompt = await self._run(
            loader,
            org_id,
            user_id,
            config_kwargs={"system_prompt": "Base."},
            ctx_overrides={
                "product_docs": "off",
                "integration_status": "off",
                "org_config": "off",
                "feature_overview": "off",
                "user_profile": "off",
                "product_primer": "off",
            },
        )
        # get_skill tool appears when tool-mode skills exist
        assert "Available Knowledge Tools" in prompt
        assert "get_skill(name)" in prompt

    async def test_prompt_composition_order(
        self,
        loader: SkillLoader,
        mock_session: AsyncMock,
        org_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> None:
        prompt = await self._run(
            loader,
            org_id,
            user_id,
            config_kwargs={
                "system_prompt": "System prompt.",
                "additional_guidance": "Additional guidance.",
                "product_primer": "Product overview.",
            },
            page_context="Page context.",
            ctx_overrides={"user_profile": "off"},
        )
        # Check sections appear in order
        sys_idx = prompt.index("System prompt.")
        add_idx = prompt.index("Additional guidance.")
        prod_idx = prompt.index("Product Overview")
        page_idx = prompt.index("Page Context")
        tools_idx = prompt.index("Available Knowledge Tools")

        assert sys_idx < add_idx < prod_idx < page_idx < tools_idx

    async def test_builds_with_config_fetch_failure(
        self,
        loader: SkillLoader,
        mock_session: AsyncMock,
        org_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> None:
        class _FailingConfigService:
            async def get_config(self, org_id: uuid.UUID) -> RemyConfig:
                raise RuntimeError("boom")

        with patch.object(loader, "_config_service", _FailingConfigService()):
            prompt = await loader.build_system_prompt(org_id=org_id, user_id=user_id)

        # Config-dependent sections are dropped, behaviour block still present
        assert "You are a helpful assistant." not in prompt
        assert "## Behaviour" in prompt

    async def test_builds_with_context_service_fetch_failure(
        self,
        loader: SkillLoader,
        mock_session: AsyncMock,
        org_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> None:
        class _FailingCtxService:
            async def get_effective_config(self, org_id: uuid.UUID, user_id: uuid.UUID) -> RemyConfig:
                raise RuntimeError("boom")

        with patch.object(loader, "_ctx_service", _FailingCtxService()):
            prompt = await loader.build_system_prompt(org_id=org_id, user_id=user_id)

        assert "Available Knowledge Tools" not in prompt
        assert "## Behaviour" in prompt

    async def test_reraises_cancelled_error_on_config_fetch(
        self,
        loader: SkillLoader,
        mock_session: AsyncMock,
        org_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> None:
        class _CancellingConfigService:
            async def get_config(self, org_id: uuid.UUID) -> RemyConfig:
                raise asyncio.CancelledError

        with (
            patch.object(loader, "_config_service", _CancellingConfigService()),
            pytest.raises(asyncio.CancelledError),
        ):
            await loader.build_system_prompt(org_id=org_id, user_id=user_id)

    async def test_reraises_cancelled_error_on_context_fetch(
        self,
        loader: SkillLoader,
        mock_session: AsyncMock,
        org_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> None:
        class _CancellingCtxService:
            async def get_effective_config(self, org_id: uuid.UUID, user_id: uuid.UUID) -> RemyConfig:
                raise asyncio.CancelledError

        with (
            patch.object(loader, "_ctx_service", _CancellingCtxService()),
            pytest.raises(asyncio.CancelledError),
        ):
            await loader.build_system_prompt(org_id=org_id, user_id=user_id)

    async def test_ui_tools_text_failure_is_skipped(
        self,
        loader: SkillLoader,
        mock_session: AsyncMock,
        org_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> None:
        def _boom() -> str:
            raise RuntimeError("boom")

        prompt = await self._run(
            loader,
            org_id,
            user_id,
            config_kwargs={"system_prompt": "You are helpful."},
            include_ui_tools_text=True,
            ui_tools_text_fn=_boom,
        )
        assert "Browser Tools Available (Text Mode)" not in prompt
        assert "## Behaviour" in prompt

    async def test_reraises_cancelled_error_on_ui_tools_text(
        self,
        loader: SkillLoader,
        mock_session: AsyncMock,
        org_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> None:
        def _cancel() -> str:
            raise asyncio.CancelledError

        with patch.object(loader, "_ui_tools_text_fn", _cancel), pytest.raises(asyncio.CancelledError):
            await loader.build_system_prompt(org_id=org_id, user_id=user_id, include_ui_tools_text=True)

    async def test_user_profile_included_when_always_on_with_account(
        self,
        loader: SkillLoader,
        mock_session: AsyncMock,
        org_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> None:
        account = MagicMock()
        account.display_name = "Ada Lovelace"
        account.email = "ada@example.com"
        membership = MagicMock()
        membership.role = "admin"

        def _result(value: Any) -> MagicMock:
            result = MagicMock()
            result.scalar_one_or_none = MagicMock(return_value=value)
            return result

        # First three executes are the profile queries (account, membership, org),
        # the remaining two return empty skill lists.
        empty_scalars = MagicMock()
        empty_scalars.all = MagicMock(return_value=[])
        empty_result = MagicMock()
        empty_result.scalars = MagicMock(return_value=empty_scalars)
        mock_session.execute = AsyncMock(
            side_effect=[
                _result(account),
                _result(membership),
                _result(None),
                empty_result,
                empty_result,
            ]
        )

        prompt = await self._run(
            loader,
            org_id,
            user_id,
            config_kwargs={"system_prompt": "Base."},
            ctx_overrides={
                "product_docs": "off",
                "integration_status": "off",
                "org_config": "off",
                "feature_overview": "off",
                "product_primer": "off",
            },
        )
        assert "User Profile" in prompt
        assert "**Name:** Ada Lovelace" in prompt
        assert "**Role:** admin" in prompt


class TestSkillLoaderSectionBuilders:
    """Direct unit tests for the private section-builder helpers."""

    def test_build_config_section_with_override(self) -> None:
        loader = SkillLoader.__new__(SkillLoader)
        config = RemyConfig(system_prompt="Default prompt.")
        section = loader._build_config_section(config, system_prompt_override="Override prompt.")
        assert section == "Override prompt."

    def test_build_config_section_returns_none_when_no_prompt(self) -> None:
        loader = SkillLoader.__new__(SkillLoader)
        config = RemyConfig(system_prompt="")
        assert loader._build_config_section(config, system_prompt_override=None) is None

    def test_build_guidance_section_returns_guidance(self) -> None:
        loader = SkillLoader.__new__(SkillLoader)
        config = RemyConfig(additional_guidance="Be concise.")
        assert loader._build_guidance_section(config) == "Be concise."

    def test_build_guidance_section_returns_none_when_empty(self) -> None:
        loader = SkillLoader.__new__(SkillLoader)
        config = RemyConfig(additional_guidance="")
        assert loader._build_guidance_section(config) is None

    def test_build_overview_section_when_always_on(self) -> None:
        loader = SkillLoader.__new__(SkillLoader)
        config = RemyConfig(product_primer="We build Modulo.")
        section = loader._build_overview_section(config, {"product_primer": "always_on"})
        assert section is not None
        assert "Product Overview" in section
        assert "We build Modulo." in section

    def test_build_overview_section_skipped_when_not_always_on(self) -> None:
        loader = SkillLoader.__new__(SkillLoader)
        config = RemyConfig(product_primer="We build Modulo.")
        assert loader._build_overview_section(config, {"product_primer": "off"}) is None
        assert loader._build_overview_section(config, {}) is None

    def test_build_knowledge_tools_skips_unknown_source(self) -> None:
        loader = SkillLoader.__new__(SkillLoader)
        section = loader._build_knowledge_tools_section([], {"unknown_source": "tool"})
        assert section is None

    def test_build_knowledge_tools_empty_when_no_tool_sources(self) -> None:
        loader = SkillLoader.__new__(SkillLoader)
        section = loader._build_knowledge_tools_section([], {"product_docs": "always_on"})
        assert section is None

    def test_filter_always_on_keeps_null_and_always_on(self) -> None:
        loader = SkillLoader.__new__(SkillLoader)
        always_on = SkillEntry(id=uuid.uuid4(), name="always", body="A", source_mode="always_on")
        tool = SkillEntry(id=uuid.uuid4(), name="tool", body="T", source_mode="tool")
        off = SkillEntry(id=uuid.uuid4(), name="off", body="O", source_mode="off")
        null = SkillEntry(id=uuid.uuid4(), name="null", body="N", source_mode=None)

        filtered = loader._filter_always_on([always_on, tool, off, null])
        assert [s.name for s in filtered] == ["always", "null"]

    def test_append_skills_block_skips_empty(self) -> None:
        loader = SkillLoader.__new__(SkillLoader)
        parts: list[str] = []
        loader._append_skills_block(parts, [], "## Organisation Skills")
        assert parts == []


class TestSkillLoaderToEntry:
    """Tests for SkillLoader._to_entry private method."""

    def test_converts_orm_to_entry_with_frontmatter(self) -> None:
        loader = SkillLoader.__new__(SkillLoader)
        mock_skill = MagicMock(spec=RemySkill)
        mock_skill.id = uuid.uuid4()
        mock_skill.name = "test"
        mock_skill.description = "desc"
        mock_skill.triggers = ["trigger"]
        mock_skill.body = "---\nversion: 1\n---\nBody content"
        mock_skill.active = True
        mock_skill.source_mode = "tool"

        entry = loader._to_entry(mock_skill)
        assert entry.name == "test"
        assert entry.frontmatter == {"version": "1"}
        assert entry.body == "Body content"
        assert entry.source_mode == "tool"

    def test_converts_orm_to_entry_without_frontmatter(self) -> None:
        loader = SkillLoader.__new__(SkillLoader)
        mock_skill = MagicMock(spec=RemySkill)
        mock_skill.id = uuid.uuid4()
        mock_skill.name = "test"
        mock_skill.description = None
        mock_skill.triggers = None
        mock_skill.body = "Plain body text"
        mock_skill.active = True
        mock_skill.source_mode = None

        entry = loader._to_entry(mock_skill)
        assert entry.frontmatter is None
        assert entry.body == "Plain body text"
        assert entry.source_mode is None
