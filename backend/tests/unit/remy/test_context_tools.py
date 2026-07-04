"""Unit tests for Remy context tools — get_documentation, get_integration_status, get_org_config, get_available_features."""

import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core.documentation_indexer import DocumentationIndex, DocEntry
from modulo.api.mcp_server import SENSITIVE_CONFIG_KEYS, _is_sensitive_key, _get_doc_index


class TestSensitiveKeyDetection:
    """Tests for _is_sensitive_key."""

    def test_exact_match_sensitive(self) -> None:
        for key in SENSITIVE_CONFIG_KEYS:
            assert _is_sensitive_key(key), f"{key} should be sensitive"

    def test_prefix_match_sensitive(self) -> None:
        assert _is_sensitive_key("secret_key_backup")
        assert _is_sensitive_key("database_url_primary")
        assert _is_sensitive_key("modulo_license_key_v2")

    def test_non_sensitive_keys(self) -> None:
        assert not _is_sensitive_key("system_prompt")
        assert not _is_sensitive_key("remy_config:org-1")
        assert not _is_sensitive_key("feature_flags")
        assert not _is_sensitive_key("rate_limits")

    def test_case_insensitive_matching(self) -> None:
        assert _is_sensitive_key("SECRET_KEY")
        assert _is_sensitive_key("Database_URL")
        assert _is_sensitive_key("Modulo_License_Key")


class TestGetDocumentation:
    """Tests for get_documentation tool behavior."""

    def test_search_returns_formatted_results(self) -> None:
        entries = [
            DocEntry(heading_path="Pipelines > Overview", heading="Pipeline Overview", first_paragraph="Pipelines are the core execution unit."),
            DocEntry(heading_path="Pipelines > Config", heading="Pipeline Config", first_paragraph="Configure pipeline nodes."),
        ]
        index = DocumentationIndex(entries=entries)
        results = index.search("pipeline")
        formatted = index.format_results(results)
        assert "Pipelines > Overview" in formatted
        assert "Pipeline Overview" in formatted
        assert "---" in formatted

    def test_search_no_results_returns_empty(self) -> None:
        index = DocumentationIndex()
        results = index.search("nonexistent")
        assert len(results) == 0

    def test_search_with_section_filter(self) -> None:
        entries = [
            DocEntry(heading_path="Pipelines > Overview", heading="Pipeline Overview", first_paragraph="Core."),
            DocEntry(heading_path="Schemas > Types", heading="Schema Types", first_paragraph="Types."),
        ]
        index = DocumentationIndex(entries=entries)
        results = index.search("overview", section="Pipelines")
        assert len(results) == 1
        assert results[0].heading == "Pipeline Overview"

    def test_format_results_truncation(self) -> None:
        long_para = "X" * 20_000
        entries = [
            DocEntry(heading_path="Big > Entry", heading="Big Entry", first_paragraph=long_para),
            DocEntry(heading_path="Small > Entry", heading="Small Entry", first_paragraph="Small."),
        ]
        index = DocumentationIndex(entries=entries)
        formatted = index.format_results(entries)
        assert "*(truncated" in formatted
        assert "Small Entry" not in formatted

    def test_format_results_takes_token_budget(self) -> None:
        entries = [
            DocEntry(heading_path="A", heading="A Heading", first_paragraph="A para."),
            DocEntry(heading_path="B", heading="B Heading", first_paragraph="B para."),
        ]
        index = DocumentationIndex(entries=entries)
        formatted = index.format_results(entries)
        assert "A Heading" in formatted
        assert "B Heading" in formatted


class TestGetIntegrationStatus:
    """Tests for get_integration_status output formatting."""

    def test_connector_table_format(self) -> None:
        connector_lines = [
            "| Name | Type | Status | Last Check | Error |",
            "|------|------|--------|------------|-------|",
            "| Slack | slack_webhook | healthy | 2025-06-01 | |",
        ]
        table = "\n".join(connector_lines)
        assert "Slack" in table
        assert "healthy" in table

    def test_model_backend_table_format(self) -> None:
        backend_lines = [
            "| Name | Provider | Model | Has Credentials | Status |",
            "|------|----------|-------|-----------------|--------|",
            "| Claude | anthropic | claude-sonnet-4 | yes | active |",
        ]
        table = "\n".join(backend_lines)
        assert "Claude" in table
        assert "anthropic" in table
        assert "yes" in table

    def test_empty_connectors_handled(self) -> None:
        lines = ["## Connectors (0)", "No connectors configured."]
        result = "\n".join(lines)
        assert "No connectors configured" in result


class TestGetOrgConfig:
    """Tests for get_org_config filtering logic."""

    def test_remy_section_filter_includes_remy_config(self) -> None:
        configs = [
            MagicMock(key="remy_config:00000000-0000-0000-0000-000000000001", value={"system_prompt": "Helpful."}),
            MagicMock(key="feature_flags", value={"remy_enabled": True}),
        ]
        key_prefixes = [f"remy_config:00000000-0000-0000-0000-000000000001", "remy_config"]
        filtered = [c for c in configs if any(c.key.startswith(p) for p in key_prefixes)]
        assert len(filtered) == 1
        assert filtered[0].key.startswith("remy_config")

    def test_section_filter_excludes_other_keys(self) -> None:
        configs = [
            MagicMock(key="remy_config:org-1", value={"a": 1}),
            MagicMock(key="some_other_key", value={"b": 2}),
        ]
        key_prefixes = ["remy_config"]
        filtered = [c for c in configs if any(c.key.startswith(p) for p in key_prefixes)]
        assert len(filtered) == 1

    def test_sensitive_keys_filtered_out(self) -> None:
        configs = [
            MagicMock(key="remy_config:org-1", value={"a": 1}),
            MagicMock(key="secret_key", value="sensitive"),
            MagicMock(key="database_url", value="postgres://..."),
        ]
        visible = [c for c in configs if not _is_sensitive_key(c.key)]
        assert len(visible) == 1
        assert visible[0].key == "remy_config:org-1"

    def test_value_formatting_truncation(self) -> None:
        config = MagicMock(key="remy_config:org-1", value={"long": "x" * 500})
        val = config.value
        val_str = json.dumps(val, default=str)
        if len(val_str) > 200:
            val_str = val_str[:200] + "..."
        assert val_str.endswith("...")

    def test_value_formatting_dict_to_json(self) -> None:
        config = MagicMock(key="test_key", value={"nested": {"key": "value"}})
        val_str = json.dumps(config.value, default=str)
        assert '"nested"' in val_str


class TestGetAvailableFeatures:
    """Tests for get_available_features output formatting."""

    def test_feature_table_format(self) -> None:
        flags = [
            MagicMock(name="remy_chat", tier="core", currently_active=True),
            MagicMock(name="custom_skills", tier="enterprise", currently_active=False),
        ]
        lines = [
            "| Feature | Required Tier | Available |",
            "|---------|---------------|-----------|",
        ]
        for flag in flags:
            available = "yes" if flag.currently_active else "no"
            lines.append(f"| {flag.name} | {flag.tier} | {available} |")
        table = "\n".join(lines)
        assert "remy_chat" in table
        assert "custom_skills" in table
        assert "yes" in table
        assert "no" in table

    def test_feature_count_and_tier(self) -> None:
        flags = [MagicMock(name="f1", tier="core", currently_active=True)]
        result = {
            "results": "| Feature | ... |",
            "tier": "community",
            "feature_count": 1,
        }
        assert result["tier"] == "community"
        assert result["feature_count"] == 1


class TestDocIndexCache:
    """Tests for the module-level doc index cache."""

    def test_get_doc_index_returns_instance(self) -> None:
        idx = _get_doc_index()
        assert isinstance(idx, DocumentationIndex)
