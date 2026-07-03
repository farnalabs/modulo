"""Unit tests for SkillLoader — YAML frontmatter parsing and system prompt assembly."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core.remy.skill_loader import SkillEntry, SkillLoader
from modulo.db.models.remy_skill import RemySkill


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
        mock_skill = MagicMock(spec=RemySkill)
        mock_skill.id = uuid.uuid4()
        mock_skill.name = "code-review"
        mock_skill.description = "Review code changes"
        mock_skill.triggers = ["on_pr"]
        mock_skill.body = "Check for security issues"
        mock_skill.active = True

        scalars_mock = MagicMock()
        scalars_mock.all = MagicMock(return_value=[mock_skill])
        mock_result = MagicMock()
        mock_result.scalars = MagicMock(return_value=scalars_mock)
        mock_session.execute = AsyncMock(return_value=mock_result)

        skills = await loader.get_org_skills(uuid.uuid4())
        assert len(skills) == 1
        assert isinstance(skills[0], SkillEntry)
        assert skills[0].name == "code-review"

    async def test_get_org_skills_empty(self, loader: SkillLoader, mock_session: AsyncMock) -> None:
        scalars_mock = MagicMock()
        scalars_mock.all = MagicMock(return_value=[])
        mock_result = MagicMock()
        mock_result.scalars = MagicMock(return_value=scalars_mock)
        mock_session.execute = AsyncMock(return_value=mock_result)

        skills = await loader.get_org_skills(uuid.uuid4())
        assert skills == []

    async def test_get_user_skills_returns_skill_entries(self, loader: SkillLoader, mock_session: AsyncMock) -> None:
        mock_skill = MagicMock(spec=RemySkill)
        mock_skill.id = uuid.uuid4()
        mock_skill.name = "my-prompt"
        mock_skill.description = None
        mock_skill.triggers = None
        mock_skill.body = "Be concise"
        mock_skill.active = True

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


class TestSkillLoaderBuildSystemPrompt:
    """Tests for SkillLoader.build_system_prompt."""

    @pytest.fixture
    def loader(self, mock_session: AsyncMock) -> SkillLoader:
        return SkillLoader(mock_session)

    async def test_with_config_system_prompt_only(
        self, loader: SkillLoader, mock_session: AsyncMock, org_id: uuid.UUID, user_id: uuid.UUID,
    ) -> None:
        with (
            patch("modulo.core.remy.config_service.RemyConfigService") as mock_cfg_svc,
        ):
            mock_config = MagicMock()
            mock_config.system_prompt = "You are a helpful assistant."
            mock_config.additional_guidance = ""
            mock_instance = AsyncMock()
            mock_instance.get_config = AsyncMock(return_value=mock_config)
            mock_cfg_svc.return_value = mock_instance

            # Empty queries for skills
            scalars_mock = MagicMock()
            scalars_mock.all = MagicMock(return_value=[])
            mock_result = MagicMock()
            mock_result.scalars = MagicMock(return_value=scalars_mock)
            mock_session.execute = AsyncMock(return_value=mock_result)

            prompt = await loader.build_system_prompt(org_id, user_id)
            assert "You are a helpful assistant." in prompt
            assert "Organisation Skills" not in prompt
            assert "User Skills" not in prompt

    async def test_with_page_context(
        self, loader: SkillLoader, mock_session: AsyncMock, org_id: uuid.UUID, user_id: uuid.UUID,
    ) -> None:
        with (
            patch("modulo.core.remy.config_service.RemyConfigService") as mock_cfg_svc,
        ):
            mock_config = MagicMock()
            mock_config.system_prompt = "System prompt."
            mock_config.additional_guidance = ""
            mock_instance = AsyncMock()
            mock_instance.get_config = AsyncMock(return_value=mock_config)
            mock_cfg_svc.return_value = mock_instance

            scalars_mock = MagicMock()
            scalars_mock.all = MagicMock(return_value=[])
            mock_result = MagicMock()
            mock_result.scalars = MagicMock(return_value=scalars_mock)
            mock_session.execute = AsyncMock(return_value=mock_result)

            prompt = await loader.build_system_prompt(org_id, user_id, page_context="User is on the Reports page")
            assert "Page Context" in prompt
            assert "User is on the Reports page" in prompt

    async def test_with_org_and_user_skills(
        self, loader: SkillLoader, mock_session: AsyncMock, org_id: uuid.UUID, user_id: uuid.UUID,
    ) -> None:
        with (
            patch("modulo.core.remy.config_service.RemyConfigService") as mock_cfg_svc,
        ):
            mock_config = MagicMock()
            mock_config.system_prompt = ""
            mock_config.additional_guidance = ""
            mock_instance = AsyncMock()
            mock_instance.get_config = AsyncMock(return_value=mock_config)
            mock_cfg_svc.return_value = mock_instance

            org_skill = MagicMock(spec=RemySkill)
            org_skill.id = uuid.uuid4()
            org_skill.name = "org-skill"
            org_skill.body = "Org skill body"
            org_skill.description = None
            org_skill.triggers = None
            org_skill.active = True

            user_skill = MagicMock(spec=RemySkill)
            user_skill.id = uuid.uuid4()
            user_skill.name = "user-skill"
            user_skill.body = "User skill body"
            user_skill.description = None
            user_skill.triggers = None
            user_skill.active = True

            # First execute call = org skills, second = user skills
            org_scalars = MagicMock()
            org_scalars.all = MagicMock(return_value=[org_skill])
            user_scalars = MagicMock()
            user_scalars.all = MagicMock(return_value=[user_skill])

            mock_session.execute = AsyncMock(
                side_effect=[
                    MagicMock(scalars=MagicMock(return_value=org_scalars)),
                    MagicMock(scalars=MagicMock(return_value=user_scalars)),
                ],
            )

            prompt = await loader.build_system_prompt(org_id, user_id)
            assert "Organisation Skills" in prompt
            assert "org-skill" in prompt
            assert "Org skill body" in prompt
            assert "User Skills" in prompt
            assert "user-skill" in prompt
            assert "User skill body" in prompt

    async def test_with_additional_guidance(
        self, loader: SkillLoader, mock_session: AsyncMock, org_id: uuid.UUID, user_id: uuid.UUID,
    ) -> None:
        with (
            patch("modulo.core.remy.config_service.RemyConfigService") as mock_cfg_svc,
        ):
            mock_config = MagicMock()
            mock_config.system_prompt = "You are helpful."
            mock_config.additional_guidance = "Always be concise."
            mock_instance = AsyncMock()
            mock_instance.get_config = AsyncMock(return_value=mock_config)
            mock_cfg_svc.return_value = mock_instance

            scalars_mock = MagicMock()
            scalars_mock.all = MagicMock(return_value=[])
            mock_result = MagicMock()
            mock_result.scalars = MagicMock(return_value=scalars_mock)
            mock_session.execute = AsyncMock(return_value=mock_result)

            prompt = await loader.build_system_prompt(org_id, user_id)
            assert "You are helpful." in prompt
            assert "Always be concise." in prompt

    async def test_with_no_config_or_skills(
        self, loader: SkillLoader, mock_session: AsyncMock, org_id: uuid.UUID, user_id: uuid.UUID,
    ) -> None:
        with (
            patch("modulo.core.remy.config_service.RemyConfigService") as mock_cfg_svc,
        ):
            mock_config = MagicMock()
            mock_config.system_prompt = ""
            mock_config.additional_guidance = ""
            mock_instance = AsyncMock()
            mock_instance.get_config = AsyncMock(return_value=mock_config)
            mock_cfg_svc.return_value = mock_instance

            scalars_mock = MagicMock()
            scalars_mock.all = MagicMock(return_value=[])
            mock_result = MagicMock()
            mock_result.scalars = MagicMock(return_value=scalars_mock)
            mock_session.execute = AsyncMock(return_value=mock_result)

            prompt = await loader.build_system_prompt(org_id, user_id)
            assert prompt == ""

    async def test_with_include_ui_tools_text_false_excludes_tools(
        self, loader: SkillLoader, mock_session: AsyncMock, org_id: uuid.UUID, user_id: uuid.UUID,
    ) -> None:
        with (
            patch("modulo.core.remy.config_service.RemyConfigService") as mock_cfg_svc,
        ):
            mock_config = MagicMock()
            mock_config.system_prompt = "You are helpful."
            mock_config.additional_guidance = ""
            mock_instance = AsyncMock()
            mock_instance.get_config = AsyncMock(return_value=mock_config)
            mock_cfg_svc.return_value = mock_instance

            scalars_mock = MagicMock()
            scalars_mock.all = MagicMock(return_value=[])
            mock_result = MagicMock()
            mock_result.scalars = MagicMock(return_value=scalars_mock)
            mock_session.execute = AsyncMock(return_value=mock_result)

            prompt = await loader.build_system_prompt(org_id, user_id, include_ui_tools_text=False)
            assert "Browser Tools Available (Text Mode)" not in prompt
            assert "**navigate**" not in prompt

    async def test_with_include_ui_tools_text_true_includes_tools(
        self, loader: SkillLoader, mock_session: AsyncMock, org_id: uuid.UUID, user_id: uuid.UUID,
    ) -> None:
        with (
            patch("modulo.core.remy.config_service.RemyConfigService") as mock_cfg_svc,
        ):
            mock_config = MagicMock()
            mock_config.system_prompt = "You are helpful."
            mock_config.additional_guidance = ""
            mock_instance = AsyncMock()
            mock_instance.get_config = AsyncMock(return_value=mock_config)
            mock_cfg_svc.return_value = mock_instance

            scalars_mock = MagicMock()
            scalars_mock.all = MagicMock(return_value=[])
            mock_result = MagicMock()
            mock_result.scalars = MagicMock(return_value=scalars_mock)
            mock_session.execute = AsyncMock(return_value=mock_result)

            prompt = await loader.build_system_prompt(org_id, user_id, include_ui_tools_text=True)
            assert "Browser Tools Available (Text Mode)" in prompt
            assert "**navigate**(path:" in prompt
            assert "**click**(selector:" in prompt


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

        entry = loader._to_entry(mock_skill)
        assert entry.name == "test"
        assert entry.frontmatter == {"version": "1"}
        assert entry.body == "Body content"

    def test_converts_orm_to_entry_without_frontmatter(self) -> None:
        loader = SkillLoader.__new__(SkillLoader)
        mock_skill = MagicMock(spec=RemySkill)
        mock_skill.id = uuid.uuid4()
        mock_skill.name = "test"
        mock_skill.description = None
        mock_skill.triggers = None
        mock_skill.body = "Plain body text"

        entry = loader._to_entry(mock_skill)
        assert entry.frontmatter is None
        assert entry.body == "Plain body text"
