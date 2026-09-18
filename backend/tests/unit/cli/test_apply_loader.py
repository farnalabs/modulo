"""Unit tests for modulo.cli.apply.loader (FAR-681 slice 1)."""

from __future__ import annotations

from pathlib import Path

import pytest

from modulo.cli.apply.loader import ApplyLoadError, load_apply_file, parse_apply_documents

SINGLE_DOC = """
api_version: modulo.dev/v1
entities:
  schemas:
    - name: alpha
      description: The alpha schema
"""


class TestSingleDoc:
    def test_single_document_parsed(self) -> None:
        config = parse_apply_documents(SINGLE_DOC)
        assert config.api_version == "modulo.dev/v1"
        assert [s.name for s in config.entities.schemas] == ["alpha"]

    def test_parse_error_raises_apply_load_error(self) -> None:
        with pytest.raises(ApplyLoadError, match="YAML parse error"):
            parse_apply_documents("api_version: [unclosed")

    def test_empty_file_rejected(self) -> None:
        with pytest.raises(ApplyLoadError, match="empty"):
            parse_apply_documents("# only comments\n")

    def test_non_mapping_document_rejected(self) -> None:
        with pytest.raises(ApplyLoadError, match="must be a mapping"):
            parse_apply_documents("- a\n- b\n")

    def test_invalid_document_rejected(self) -> None:
        with pytest.raises(ApplyLoadError, match="not a valid apply config"):
            parse_apply_documents("api_version: modulo.dev/v9\n")


class TestMultiDoc:
    def test_multiple_documents_merged(self) -> None:
        text = (
            "---\n"
            "api_version: modulo.dev/v1\n"
            "entities:\n"
            "  schemas:\n"
            "    - name: alpha\n"
            "---\n"
            "api_version: modulo.dev/v1\n"
            "entities:\n"
            "  model_backends:\n"
            "    - name: beta\n"
            "      display_name: Beta\n"
            "      provider: openai\n"
            "      model_id: gpt-x\n"
            "      api_key: ${env:SK}\n"
        )
        config = parse_apply_documents(text)
        assert [s.name for s in config.entities.schemas] == ["alpha"]
        assert [b.name for b in config.entities.model_backends] == ["beta"]

    def test_between_documents_major_mismatch_rejected(self) -> None:
        text = "---\napi_version: modulo.dev/v1\nentities: {}\n---\napi_version: modulo.dev/v2\nentities: {}\n"
        with pytest.raises(ApplyLoadError):
            parse_apply_documents(text)

    def test_between_documents_minor_suffix_merges(self) -> None:
        text = "---\napi_version: modulo.dev/v1\nentities: {}\n---\napi_version: modulo.dev/v1.3\nentities: {}\n"
        config = parse_apply_documents(text)
        assert config.api_version == "modulo.dev/v1"

    def test_duplicate_name_across_documents_rejected(self) -> None:
        text = (
            "---\n"
            "api_version: modulo.dev/v1\n"
            "entities:\n"
            "  schemas:\n"
            "    - name: dup\n"
            "---\n"
            "api_version: modulo.dev/v1\n"
            "entities:\n"
            "  schemas:\n"
            "    - name: dup\n"
        )
        with pytest.raises(ApplyLoadError):
            parse_apply_documents(text)


class TestLoadFile:
    def test_loads_from_disk(self, tmp_path: Path) -> None:
        path = tmp_path / "config.yaml"
        path.write_text(SINGLE_DOC, encoding="utf-8")
        config = load_apply_file(path)
        assert config.entities.schemas[0].name == "alpha"

    def test_missing_file_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ApplyLoadError, match="cannot read"):
            load_apply_file(tmp_path / "missing.yaml")
