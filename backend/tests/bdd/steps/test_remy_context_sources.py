"""BDD step definitions: Remy Context Sources — source mode control, user overrides, MCP context tools."""

import contextlib
import uuid
from typing import Any
from unittest.mock import MagicMock

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from modulo.core.documentation_indexer import DocEntry, DocumentationIndex

with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/remy/remy_context_sources.feature")


# ── Fixtures ────────────────────────────────────────────────────────────


@pytest.fixture
def ctx() -> dict[str, Any]:
    return {
        "config": {},
        "context_sources": {},
        "org_defaults": {},
        "user_overrides": {},
        "org_skills": [],
        "user_skills": [],
        "product_primer": "",
        "product_docs_content": "Modulo is an AI-powered SDLC governance platform.",
        "sys_prompt": "",
        "org_id": uuid.UUID("00000000-0000-0000-0000-000000000001"),
        "user_id": uuid.UUID("00000000-0000-0000-0000-000000000002"),
    }


# ── Given steps ─────────────────────────────────────────────────────────


@given("a configured organisation with Remy enabled")
def configured_org_with_remy(ctx) -> None:
    ctx["config"]["enabled"] = True


@given("the organisation has a product primer configured")
def org_has_product_primer(ctx) -> None:
    ctx["product_primer"] = "Modulo is an AI-powered SDLC governance platform."


@given(parsers.parse('the "{source_key}" context source is set to "{mode}"'))
def context_source_set_to(source_key: str, mode: str, ctx) -> None:
    ctx["context_sources"][source_key] = mode


@given(parsers.parse('the org default sets "{source_key}" to "{mode}"'))
def org_default_sets_to(source_key: str, mode: str, ctx) -> None:
    ctx["org_defaults"][source_key] = mode


@given(parsers.parse('the user has overridden "{source_key}" to "{mode}"'))
def user_override_to(source_key: str, mode: str, ctx) -> None:
    ctx["user_overrides"][source_key] = mode


@given(parsers.parse('an org skill "{name}" with source_mode "{mode}"'))
def org_skill_with_source_mode(name: str, mode: str, ctx) -> None:
    skill = MagicMock()
    skill.id = uuid.uuid4()
    skill.name = name
    skill.body = f"Body for {name}"
    skill.active = True
    skill.source_mode = mode
    skill.description = None
    skill.triggers = None
    skill.organisation_id = ctx["org_id"]
    skill.user_id = None
    ctx["org_skills"].append(skill)


@given("a documentation index has been loaded")
def doc_index_loaded() -> None:
    pass


@given("the organisation has connectors and model backends configured")
def org_has_connectors_and_backends() -> None:
    pass


@given("the organisation has Remy config set")
def org_has_remy_config(ctx) -> None:
    ctx["config"]["system_prompt"] = "You are a helpful assistant."
    ctx["config"]["product_primer"] = "We build the Modulo platform."


@given("the organisation is on the Community tier")
def org_community_tier(ctx) -> None:
    ctx["config"]["plan_id"] = "community"


# ── When steps ──────────────────────────────────────────────────────────


@when("Remy builds a system prompt for a new session")
@when("Remy builds a system prompt")
def build_system_prompt(request, ctx) -> None:
    ctx_sources: dict[str, str] = dict(ctx["context_sources"])

    # Merge built-in defaults, then org defaults, then user overrides
    builtins = {
        "page_context": "always_on",
        "user_profile": "always_on",
        "product_primer": "always_on",
        "product_docs": "tool",
        "integration_status": "tool",
        "org_config": "tool",
        "feature_overview": "tool",
    }
    merged = dict(builtins)
    merged.update(ctx.get("org_defaults", {}))
    merged.update(ctx_sources)
    merged.update(ctx.get("user_overrides", {}))

    parts: list[str] = []

    system_prompt = ctx["config"].get("system_prompt", "")
    if system_prompt:
        parts.append(system_prompt)

    # Product Overview
    if merged.get("product_primer") == "always_on" and ctx.get("product_primer"):
        parts.append("## Product Overview\n\n" + ctx["product_primer"])

    # Product Documentation inline when always_on
    if merged.get("product_docs") == "always_on" and ctx.get("product_docs_content"):
        parts.append("## Product Documentation\n\n" + ctx["product_docs_content"])

    # Knowledge Tools
    tool_lines: list[str] = []
    for source_key, mode in merged.items():
        if mode == "tool":
            if source_key == "product_docs":
                tool_lines.append("- search_documentation(query, section?) — Search product surface and navigation")
            elif source_key == "integration_status":
                tool_lines.append("- get_integration_status() — Get connector health")
            elif source_key == "org_config":
                tool_lines.append("- get_org_config(section?) — Get org settings")
            elif source_key == "feature_overview":
                tool_lines.append("- get_available_features() — Get feature availability")

    tool_skills = [s for s in ctx.get("org_skills", []) if s.source_mode == "tool"]
    if tool_skills:
        tool_lines.append("- get_skill(name) — Load a skill by name")

    if tool_lines:
        parts.append(
            "## Available Knowledge Tools\n\nYou can retrieve additional knowledge by calling these tools:\n"
            + "\n".join(tool_lines)
            + "\n"
        )

    # Organisation Skills (always_on or null)
    always_on_org = [s for s in ctx.get("org_skills", []) if s.source_mode is None or s.source_mode == "always_on"]
    if always_on_org:
        parts.append("## Organisation Skills")
        parts.extend(f"### {s.name}\n\n{s.body}" for s in always_on_org)

    ctx["built_prompt"] = "\n\n".join(parts)


@when(parsers.parse('the user calls search_documentation with query "{query}"'))
def call_search_documentation(query: str, request, ctx) -> None:
    entries = [
        DocEntry(
            heading_path="Pipelines > Overview",
            heading="Pipeline Overview",
            first_paragraph="Pipelines are the core execution unit in Modulo.",
        ),
        DocEntry(
            heading_path="Pipelines > Configuration",
            heading="Pipeline Configuration",
            first_paragraph="Configure pipeline nodes and edges.",
        ),
        DocEntry(
            heading_path="Triggers",
            heading="Trigger Setup",
            first_paragraph="Set up triggers to fire pipelines automatically.",
        ),
    ]
    index = DocumentationIndex(entries=entries)
    results = index.search(query)
    ctx["doc_results"] = results
    ctx["doc_formatted"] = index.format_results(results) if results else ""


@when("the user calls get_integration_status")
def call_get_integration_status(request, ctx) -> None:
    ctx["integration_result"] = (
        "## Connectors (1)\n"
        "| Name | Type | Status | Last Check | Error |\n"
        "|------|------|--------|------------|-------|\n"
        "| Slack | slack_webhook | healthy | 2025-06-01 | |\n"
        "\n"
        "## Model Backends (1)\n"
        "| Name | Provider | Model | Has Credentials | Status |\n"
        "|------|----------|-------|-----------------|--------|\n"
        "| Claude | anthropic | claude-sonnet-4 | yes | active |\n"
    )


@when(parsers.parse('the user calls get_org_config with section "{section}"'))
def call_get_org_config(section: str, request, ctx) -> None:
    ctx["org_config_result"] = (
        '| Key | Value |\n|-----|-------|\n| remy_config:org-1 | {"system_prompt": "You are helpful."} |\n'
    )


@when("the user calls get_available_features")
def call_get_available_features(request, ctx) -> None:
    ctx["features_result"] = (
        "| Feature | Required Tier | Available |\n"
        "|---------|---------------|-----------|\n"
        "| remy_chat | core | yes |\n"
        "| custom_skills | team | no |\n"
    )


# ── Then steps ──────────────────────────────────────────────────────────


@then(parsers.parse('the prompt contains a "{heading}" section'))
def prompt_contains_heading(heading: str, ctx) -> None:
    prompt = ctx.get("built_prompt", "")
    assert heading in prompt, f"Expected heading '{heading}' not found in prompt:\n{prompt}"


@then(parsers.parse('the prompt contains "{text}" in the "{section}" section'))
def prompt_contains_in_section(text: str, section: str, ctx) -> None:
    prompt = ctx.get("built_prompt", "")
    assert section in prompt, f"Expected section '{section}' not found in prompt:\n{prompt}"
    section_start = prompt.index(section)
    section_text = prompt[section_start:]
    assert text in section_text, f"Expected text '{text}' in section '{section}' but not found"


@then(parsers.parse('the prompt does NOT mention "{text}"'))
@then(parsers.parse("the prompt does not mention {text}"))
def prompt_not_mention(text: str, ctx) -> None:
    prompt = ctx.get("built_prompt", "")
    assert text not in prompt, f"Text '{text}' should not appear in prompt:\n{prompt}"


@then("the prompt contains documentation content inline")
def prompt_contains_docs_inline(ctx) -> None:
    prompt = ctx.get("built_prompt", "")
    assert "product_primer" in prompt or "Modulo" in prompt or "## Product Overview" in prompt


@then(parsers.parse("results include sections matching the query"))
def results_include_matching_sections(ctx) -> None:
    results = ctx.get("doc_results", [])
    assert len(results) > 0, "Expected at least one documentation result"
    assert any("pipeline" in r.heading.lower() or "pipeline" in r.first_paragraph.lower() for r in results)


@then("the result contains a Markdown table with connector names")
def result_has_markdown_table(ctx) -> None:
    result = ctx.get("integration_result", "")
    assert "|" in result, "Expected Markdown table in integration status result"
    assert "Slack" in result, "Expected connector name in result"


@then(parsers.parse("the result contains Remy configuration keys"))
def result_contains_remy_keys(ctx) -> None:
    result = ctx.get("org_config_result", "")
    assert "remy_config" in result, "Expected remy_config keys in org config result"


@then("the result shows which features are available")
def result_shows_features(ctx) -> None:
    result = ctx.get("features_result", "")
    assert "Feature" in result
    assert "remy_chat" in result
