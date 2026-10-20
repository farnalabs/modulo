"""Focused branch coverage for the S3776 decomposition of ``apply.executor``.

Covers the per-entity helpers split out of ``apply_schemas``/``apply_backends``:
a schema update that changes both patchable fields and skips an existing
version, and a model-backend update (PATCH) rather than a create.
"""

from typing import Any
from unittest.mock import MagicMock

import pytest

from modulo.cli.apply.executor import ApplyExecutor
from modulo.cli.apply.models import ApplyVersionSpec, ModelBackendEntity, SchemaEntity
from modulo.cli.apply.plan import KIND_BACKEND, KIND_SCHEMA


def _executor() -> ApplyExecutor:
    return ApplyExecutor("https://api.test", "key", client=MagicMock())


def _schema() -> SchemaEntity:
    return SchemaEntity(
        name="sample",
        description="new description",
        abstract_name="new_abstract",
        versions=[
            ApplyVersionSpec(version="v1", version_number=1, definition_json={"a": 1}),
            ApplyVersionSpec(version="v2", version_number=2, definition_json={"b": 2}),
        ],
    )


def test_updated_schema_patches_changed_fields_and_posts_missing_versions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executor = _executor()
    patched: list[tuple[str, dict[str, Any]]] = []
    posted: list[tuple[str, dict[str, Any] | None]] = []
    monkeypatch.setattr(
        executor,
        "_patch",
        lambda path, payload: patched.append((path, payload)),
    )
    monkeypatch.setattr(
        executor,
        "_post",
        lambda path, payload=None: posted.append((path, payload)) or {"id": "new"},
    )
    report: dict[str, list[dict[str, Any]]] = {
        "created": [],
        "updated": [{"kind": KIND_SCHEMA, "name": "sample"}],
        "failed": [],
    }
    current = {
        KIND_SCHEMA: {
            "sample": {
                "id": "sid",
                "description": "old description",
                "abstract_name": "old_abstract",
                "versions": [{"version": "v1"}],
            }
        }
    }

    executor.apply_schemas({KIND_SCHEMA: [("sample", _schema())]}, current, report)

    assert patched == [("/schemas/sid", {"description": "new description", "abstract_name": "new_abstract"})]
    assert len(posted) == 1
    assert posted[0][0] == "/schemas/sid/versions"
    assert report["updated"] == [{"kind": KIND_SCHEMA, "name": "sample"}]
    assert not report["failed"]


def test_updated_backend_patches_existing_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    executor = _executor()
    entity = ModelBackendEntity(
        name="primary",
        display_name="Primary",
        provider="openai",
        model_id="gpt-x",
        api_key="${env:OPENAI_KEY}",
        default_params={"temperature": 0.1},
    )
    patched: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        executor,
        "_patch",
        lambda path, payload: patched.append((path, payload)),
    )
    monkeypatch.setattr(executor, "_verify_backend_health", lambda backend_id, name: None)
    report: dict[str, list[dict[str, Any]]] = {
        "created": [],
        "updated": [{"kind": KIND_BACKEND, "name": "primary"}],
        "failed": [],
    }
    current = {KIND_BACKEND: {"primary": {"id": "bid"}}}

    executor.apply_backends({KIND_BACKEND: [("primary", entity)]}, current, {}, report)

    assert len(patched) == 1
    assert patched[0][0] == "/model-backends/bid"
    assert patched[0][1]["display_name"] == "Primary"
    assert report["updated"] == [{"kind": KIND_BACKEND, "name": "primary"}]
    assert not report["failed"]


def test_updated_schema_with_no_changed_fields_skips_the_patch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executor = _executor()
    entity = SchemaEntity(name="sample", description="same", abstract_name="same_abstract")
    patched: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(executor, "_patch", lambda path, payload: patched.append((path, payload)))
    report: dict[str, list[dict[str, Any]]] = {
        "created": [],
        "updated": [{"kind": KIND_SCHEMA, "name": "sample"}],
        "failed": [],
    }
    current = {
        KIND_SCHEMA: {
            "sample": {
                "id": "sid",
                "description": "same",
                "abstract_name": "same_abstract",
                "versions": [],
            }
        }
    }

    executor.apply_schemas({KIND_SCHEMA: [("sample", entity)]}, current, report)

    assert not patched
    assert report["updated"] == [{"kind": KIND_SCHEMA, "name": "sample"}]
