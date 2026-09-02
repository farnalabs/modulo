"""Unit tests for modulo.core.workflow_import_export — the portable workflow-bundle
import/export service (v1 ZIP bundles + v2 YAML bundles).

Covers the service's pure helper contracts, the archive/name/retry-policy
sanitisation gates, the local-equivalent resolution chain, and the export /
materialize entry points:

  * ``_safe_uuid`` / ``_sanitize_slug`` / ``suggest_import_name`` — UUID
    coercion (with descriptive errors), URL-safe slugging (with the
    ``imported-pipeline`` fallback) and collision-free naming.
  * ``_sanitize_retry_policy`` — a malformed bundled policy is coerced to ``{}``
    so an imported pipeline never hard-fails pre-run validation.
  * ``extract_bundle_json_from_zip`` — size cap, missing-manifest, non-ZIP and
    malformed-JSON handling.
  * ``_get_existing_names`` / ``_get_latest_published_version`` — org-scoped
    column SELECT name collection (incl. ``for_update``) and published-version
    resolution, with error/CancelledError propagation.
  * ``resolve_schema`` / ``resolve_connector_type`` / ``resolve_model_backend``
    — local-equivalent resolution chains and their "not found locally" warnings.
  * ``export_pipeline_bundle`` (v1) / ``export_pipeline_bundle_v2`` (YAML) —
    org-private field stripping, trigger/owner-team/author enrichment.
  * ``materialize_import`` — the format gate, owner-team validation, schema /
    agent / pipeline / edge creation, reference rewiring, and the warning paths
    for unresolved refs, malformed retry_policy, non-list graph nodes, name
    collisions and invalid edges.

Mock/fake based — no Postgres.
"""

from __future__ import annotations

import asyncio
import io
import json
import uuid
import zipfile
from types import SimpleNamespace
from typing import Any, Self
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
import yaml
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

import modulo.core.workflow_import_export as mod
from modulo.core.workflow_import_export import (
    _get_existing_names,
    _get_latest_published_version,
    _safe_uuid,
    _sanitize_retry_policy,
    _sanitize_slug,
    export_pipeline_bundle,
    export_pipeline_bundle_v2,
    extract_bundle_json_from_zip,
    get_existing_agent_names,
    get_existing_pipeline_names,
    materialize_import,
    resolve_connector_type,
    resolve_model_backend,
    resolve_schema,
    suggest_import_name,
)

_ORG_ID = uuid.uuid4()
_ACCOUNT_ID = uuid.uuid4()


class _NestedTransaction:
    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: object,
    ) -> bool:
        return False


class _FakeSession:
    """Async mock session that also supports ``async with session.begin_nested()``."""

    def __init__(self, *execute_results: Any) -> None:
        self.execute = AsyncMock(side_effect=list(execute_results))
        self.add = Mock()
        self.flush = AsyncMock()
        self.begin_nested = Mock(return_value=_NestedTransaction())


def _zip_bytes(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# _safe_uuid — coercion + descriptive errors
# ---------------------------------------------------------------------------


def test_safe_uuid_passthroughs_uuid_instance() -> None:
    value = uuid.uuid4()
    assert _safe_uuid(value, "field") is value


def test_safe_uuid_parses_valid_string() -> None:
    value = uuid.uuid4()
    assert _safe_uuid(str(value), "field") == value


def test_safe_uuid_rejects_invalid_string_with_label() -> None:
    with pytest.raises(ValueError, match=r"Invalid UUID for edge.source_node_id"):
        _safe_uuid("not-a-uuid", "edge.source_node_id")


def test_safe_uuid_rejects_none() -> None:
    with pytest.raises(ValueError, match="Invalid UUID for field"):
        _safe_uuid(None, "field")


# ---------------------------------------------------------------------------
# _sanitize_slug — URL-safe pipeline slug
# ---------------------------------------------------------------------------


def test_sanitize_slug_lowercases_and_hyphenates() -> None:
    assert _sanitize_slug("My Pipeline Name") == "my-pipeline-name"


def test_sanitize_slug_converts_underscores() -> None:
    assert _sanitize_slug("my_pipeline_v2") == "my-pipeline-v2"


def test_sanitize_slug_strips_non_alphanumeric() -> None:
    assert _sanitize_slug("Café & Co.") == "caf-co"


def test_sanitize_slug_collapses_repeated_dashes() -> None:
    assert _sanitize_slug("a--b___c") == "a-b-c"


def test_sanitize_slug_strips_leading_trailing_dashes() -> None:
    assert _sanitize_slug("-Leading-") == "leading"


def test_sanitize_slug_falls_back_when_nothing_left() -> None:
    assert _sanitize_slug("!!!") == "imported-pipeline"
    assert _sanitize_slug("") == "imported-pipeline"


# ---------------------------------------------------------------------------
# suggest_import_name — collision-free naming
# ---------------------------------------------------------------------------


def test_suggest_import_name_keeps_free_name() -> None:
    assert suggest_import_name({"other"}, "My Pipeline") == "My Pipeline"


def test_suggest_import_name_appends_suffix_once() -> None:
    assert suggest_import_name({"My Pipeline"}, "My Pipeline") == "My Pipeline (imported)"


def test_suggest_import_name_numbers_after_suffix_collision() -> None:
    taken = {"My Pipeline", "My Pipeline (imported)"}
    assert suggest_import_name(taken, "My Pipeline") == "My Pipeline (imported) 2"


def test_suggest_import_name_increments_past_multiple_collisions() -> None:
    taken = {"P", "P (imported)", "P (imported) 2"}
    assert suggest_import_name(taken, "P") == "P (imported) 3"


def test_suggest_import_name_supports_custom_suffix() -> None:
    assert suggest_import_name({"Copy"}, "Copy", suffix="(copy)") == "Copy (copy)"


def test_suggest_import_name_clamps_base_to_fit_column_width() -> None:
    """A max-width name must not overflow String(255) once a suffix is appended.

    FAR-174 review: ``suggest_import_name`` appended ``" (imported)"`` without
    clamping, so a 255-char name (the Pydantic max) became 255+ chars and hit a
    ``DataError`` mapped to a misleading 503.
    """
    long_name = "N" * 255
    taken = {long_name}
    candidate = suggest_import_name(taken, long_name)
    assert len(candidate) <= 255, f"suffixed name overflowed: {len(candidate)} chars"
    assert candidate.startswith("N" * 239)
    assert candidate.endswith("(imported)")

    taken.add(candidate)
    numbered = suggest_import_name(taken, long_name)
    assert len(numbered) <= 255, f"numbered name overflowed: {len(numbered)} chars"
    assert numbered.endswith("(imported) 2")


# ---------------------------------------------------------------------------
# extract_bundle_json_from_zip — archive gates
# ---------------------------------------------------------------------------


def test_extract_bundle_round_trips_manifest() -> None:
    bundle = {"format_version": "1", "pipeline": {"name": "P"}}
    data = _zip_bytes({"bundle.json": json.dumps(bundle), "extra.txt": "ignored"})
    assert extract_bundle_json_from_zip(data) == bundle


def test_extract_bundle_rejects_oversized_archive() -> None:
    with pytest.raises(ValueError, match="max 100 MB"):
        extract_bundle_json_from_zip(b"x" * (100 * 1024 * 1024 + 1))


def test_extract_bundle_rejects_non_zip() -> None:
    with pytest.raises(ValueError, match="not a valid ZIP archive"):
        extract_bundle_json_from_zip(b"this is not a zip")


def test_extract_bundle_rejects_malformed_json() -> None:
    data = _zip_bytes({"bundle.json": "{oops"})
    with pytest.raises(ValueError, match="contains malformed JSON"):
        extract_bundle_json_from_zip(data)


def test_extract_bundle_rejects_missing_manifest() -> None:
    data = _zip_bytes({"other.json": "{}"})
    with pytest.raises(LookupError, match=r"bundle.json not found"):
        extract_bundle_json_from_zip(data)


# ---------------------------------------------------------------------------
# _sanitize_retry_policy — malformed policies must not break imported runs
# ---------------------------------------------------------------------------


def test_sanitize_retry_policy_keeps_valid_dict() -> None:
    policy = {"on": ["stall", "timeout", "failure"], "max_retries": 3}
    sanitized, fault = _sanitize_retry_policy(policy)
    assert sanitized == policy
    assert fault is None


def test_sanitize_retry_policy_keeps_minimal_valid_dict() -> None:
    policy = {"on": ["timeout"], "max_retries": 1}
    sanitized, fault = _sanitize_retry_policy(policy)
    assert sanitized == policy
    assert fault is None


def test_sanitize_retry_policy_drops_unknown_event() -> None:
    sanitized, fault = _sanitize_retry_policy({"on": ["bogus"], "max_retries": 2})
    assert not sanitized
    assert fault == "core"


def test_sanitize_retry_policy_drops_out_of_range_budget() -> None:
    sanitized, fault = _sanitize_retry_policy({"on": ["failure"], "max_retries": 9})
    assert not sanitized
    assert fault == "core"


def test_sanitize_retry_policy_drops_non_integer_budget() -> None:
    sanitized, fault = _sanitize_retry_policy({"on": ["failure"], "max_retries": "lots"})
    assert not sanitized
    assert fault == "core"


def test_sanitize_retry_policy_drops_present_non_dict_values() -> None:
    for bad in ("stall", ["stall"], 42):
        sanitized, fault = _sanitize_retry_policy(bad)
        assert not sanitized
        assert fault == "core"


def test_sanitize_retry_policy_absent_is_not_a_fault() -> None:
    """``None`` (no retry_policy on the imported pipeline) is NOT a fault class
    of "core" — the legacy warning only fired for a PRESENT malformed policy."""
    sanitized, fault = _sanitize_retry_policy(None)
    assert not sanitized
    assert fault is None


def test_sanitize_retry_policy_keeps_empty_policy() -> None:
    sanitized, fault = _sanitize_retry_policy({})
    assert not sanitized
    assert fault is None


def test_sanitize_retry_policy_returns_copy_not_reference() -> None:
    policy = {"on": ["failure"], "max_retries": 2}
    sanitized, _fault = _sanitize_retry_policy(policy)
    assert sanitized is not policy
    assert sanitized == policy


# ---------------------------------------------------------------------------
# FAR-525 — backoff_schedule sanitisation (fault-classed)
# ---------------------------------------------------------------------------


def test_sanitize_retry_policy_keeps_valid_schedule() -> None:
    policy = {"on": ["failure"], "max_retries": 2, "backoff_schedule": {"delay_seconds": 30, "multiplier": 1.5}}
    sanitized, fault = _sanitize_retry_policy(policy)
    assert sanitized == policy
    assert fault is None


def test_sanitize_retry_policy_nested_drops_malformed_schedule_keeps_core() -> None:
    """A schedule-level fault removes ONLY backoff_schedule — on/max_retries are KEPT."""
    policy = {"on": ["failure"], "max_retries": 2, "backoff_schedule": {"delay_seconds": 0}}
    sanitized, fault = _sanitize_retry_policy(policy)
    assert sanitized == {"on": ["failure"], "max_retries": 2}
    assert fault == "schedule"


def test_sanitize_retry_policy_whole_drops_mixed_error() -> None:
    """Mixed core + schedule faults WHOLE-DROP (the core fault is fatal)."""
    policy = {"on": ["bogus"], "max_retries": 2, "backoff_schedule": {"delay_seconds": 0}}
    sanitized, fault = _sanitize_retry_policy(policy)
    assert sanitized == {}
    assert fault == "core"


def test_sanitize_retry_policy_unexpected_schedule_exception_degrades_to_schedule_fault(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FAR-525 qa gate defense-in-depth: the schedule validator raising an
    UNEXPECTED exception must degrade to the schedule-fault class (nested
    drop, core kept) — a malformed bundled schedule must never break the
    import. The CORE check keeps its semantics (only unexpected-exception
    containment is broadened here)."""
    from modulo.core.graph_validator import GraphValidator

    policy = {"on": ["failure"], "max_retries": 2, "backoff_schedule": {"delay_seconds": 45}}

    def _boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("simulated validator defect")

    monkeypatch.setattr(GraphValidator, "check_retry_policy_schedule", _boom)
    sanitized, fault = _sanitize_retry_policy(policy)
    assert sanitized == {"on": ["failure"], "max_retries": 2}
    assert fault == "schedule"


def test_sanitize_retry_policy_canonicalise_valueerror_degrades_to_schedule_fault(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FAR-525 iteration 2 containment completeness: the canonicalise call sits
    INSIDE the same containment try as the schedule validator, so a canonicalise
    ValueError (e.g. an un-representable value the validator let through) is
    classified as the SCHEDULE fault (nested drop, core kept) — never an import
    break."""
    from modulo.core.pipeline_engine import retry_compensation

    policy = {"on": ["failure"], "max_retries": 2, "backoff_schedule": {"delay_seconds": 45}}

    def _boom(schedule: object) -> None:
        raise ValueError("simulated canonicalise fault")

    monkeypatch.setattr(retry_compensation, "canonicalise_backoff_schedule", _boom)
    sanitized, fault = _sanitize_retry_policy(policy)
    assert sanitized == {"on": ["failure"], "max_retries": 2}
    assert fault == "schedule"


def test_sanitize_retry_policy_containment_logs_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """FAR-525 iteration 2 observability: an unexpected exception inside the
    containment try logs a distinguishable warning (exc_info attached) so a
    genuine validator defect is separable from malformed bundled data."""
    import logging

    from modulo.core.graph_validator import GraphValidator

    policy = {"on": ["failure"], "max_retries": 2, "backoff_schedule": {"delay_seconds": 45}}

    def _boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("simulated validator defect")

    monkeypatch.setattr(GraphValidator, "check_retry_policy_schedule", _boom)
    with caplog.at_level(logging.WARNING, logger="modulo.core.workflow_import_export"):
        _sanitized, fault = _sanitize_retry_policy(policy)
    assert fault == "schedule"
    assert "workflow_import.retry_policy_schedule_check_failed" in caplog.text


def test_sanitize_retry_policy_canonicalization_only_delta_does_not_fault() -> None:
    """A canonicalisation-only delta (300.0 -> 300, int multiplier -> float)
    is NOT a fault: the policy is kept and no warning-worthy fault class is
    returned."""
    policy = {"on": ["failure"], "max_retries": 2, "backoff_schedule": {"delay_seconds": 300.0, "multiplier": 2}}
    sanitized, fault = _sanitize_retry_policy(policy)
    assert sanitized == {
        "on": ["failure"],
        "max_retries": 2,
        "backoff_schedule": {"delay_seconds": 300, "multiplier": 2.0},
    }
    assert fault is None


def test_apply_imported_retry_policy_schedule_fault_emits_schedule_warning() -> None:
    """The warning site derives warnings from the fault class: a nested drop
    emits the schedule-specific message, NOT the 'dropped to {}' message."""
    from modulo.core.workflow_import_export import _apply_imported_retry_policy

    pipeline = MagicMock()
    pipeline_info = {"retry_policy": {"on": ["failure"], "max_retries": 2, "backoff_schedule": {"nope": 1}}}
    warnings: list[str] = []
    _apply_imported_retry_policy(pipeline, pipeline_info, warnings)
    assert pipeline.retry_policy == {"on": ["failure"], "max_retries": 2}
    assert len(warnings) == 1
    assert "backoff_schedule" in warnings[0]
    assert "dropped to the no-policy default" not in warnings[0]


def test_apply_imported_retry_policy_core_fault_emits_legacy_warning() -> None:
    from modulo.core.workflow_import_export import _apply_imported_retry_policy

    pipeline = MagicMock()
    pipeline_info = {"retry_policy": {"on": ["bogus"], "max_retries": 2}}
    warnings: list[str] = []
    _apply_imported_retry_policy(pipeline, pipeline_info, warnings)
    assert not pipeline.retry_policy
    assert warnings == ["Imported pipeline 'retry_policy' was malformed; dropped to the no-policy default ({})."]


def test_apply_imported_retry_policy_canonicalization_only_no_warning() -> None:
    from modulo.core.workflow_import_export import _apply_imported_retry_policy

    pipeline = MagicMock()
    pipeline_info = {"retry_policy": {"on": ["failure"], "max_retries": 2, "backoff_schedule": {"delay_seconds": 45.0}}}
    warnings: list[str] = []
    _apply_imported_retry_policy(pipeline, pipeline_info, warnings)
    assert pipeline.retry_policy == {"on": ["failure"], "max_retries": 2, "backoff_schedule": {"delay_seconds": 45}}
    assert warnings == []


def test_apply_imported_retry_policy_absent_policy_no_warning() -> None:
    """A pipeline WITHOUT a retry_policy imports silently: the legacy behaviour
    never warned for an absent policy — only for a PRESENT malformed one."""
    from modulo.core.workflow_import_export import _apply_imported_retry_policy

    pipeline = MagicMock()
    warnings: list[str] = []
    _apply_imported_retry_policy(pipeline, {}, warnings)
    assert not pipeline.retry_policy
    assert warnings == []


# ---------------------------------------------------------------------------
# _get_existing_names / get_existing_*_names — org-scoped column SELECT
# ---------------------------------------------------------------------------


async def test_get_existing_names_returns_row_set() -> None:
    session = AsyncMock()
    session.execute.return_value = [("alpha",), ("beta",), ("alpha",)]
    assert await _get_existing_names(session, _ORG_ID, mod.Pipeline) == {"alpha", "beta"}


async def test_get_existing_names_empty() -> None:
    session = AsyncMock()
    session.execute.return_value = []
    assert not await _get_existing_names(session, _ORG_ID, mod.Pipeline)


async def test_get_existing_names_forwards_for_update(monkeypatch: pytest.MonkeyPatch) -> None:
    select_mock = Mock()
    select_mock.where.return_value = select_mock
    select_mock.with_for_update.return_value = select_mock
    monkeypatch.setattr(mod, "select", Mock(return_value=select_mock))
    session = AsyncMock()
    session.execute.return_value = [("p",)]
    assert await _get_existing_names(session, _ORG_ID, mod.Pipeline, for_update=True) == {"p"}
    select_mock.with_for_update.assert_called_once()


async def test_get_existing_names_propagates_db_error() -> None:
    session = AsyncMock()
    session.execute.side_effect = SQLAlchemyError("read failed")
    with pytest.raises(SQLAlchemyError):
        await _get_existing_names(session, _ORG_ID, mod.Pipeline)


async def test_get_existing_names_reraise_cancelled() -> None:
    session = AsyncMock()
    session.execute.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await _get_existing_names(session, _ORG_ID, mod.Pipeline)


async def test_get_existing_pipeline_names_delegates_with_pipeline_model(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = AsyncMock(return_value={"a"})
    monkeypatch.setattr(mod, "_get_existing_names", fake)
    assert await get_existing_pipeline_names(AsyncMock(), _ORG_ID) == {"a"}
    assert fake.await_args.args[2] is mod.Pipeline


async def test_get_existing_agent_names_delegates_with_agent_model(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = AsyncMock(return_value={"a"})
    monkeypatch.setattr(mod, "_get_existing_names", fake)
    assert await get_existing_agent_names(AsyncMock(), _ORG_ID) == {"a"}
    assert fake.await_args.args[2] is mod.Agent


# ---------------------------------------------------------------------------
# _get_latest_published_version — newest published version resolution
# ---------------------------------------------------------------------------


async def test_latest_published_version_returns_sv() -> None:
    session = AsyncMock()
    sv = SimpleNamespace(version="3.0")
    result = MagicMock()
    result.scalar_one_or_none.return_value = sv
    session.execute = AsyncMock(return_value=result)
    assert await _get_latest_published_version(session, uuid.uuid4()) is sv


async def test_latest_published_version_returns_none_when_missing() -> None:
    session = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    session.execute = AsyncMock(return_value=result)
    assert await _get_latest_published_version(session, uuid.uuid4()) is None


async def test_latest_published_version_propagates_db_error() -> None:
    session = AsyncMock()
    session.execute.side_effect = SQLAlchemyError("read failed")
    with pytest.raises(SQLAlchemyError):
        await _get_latest_published_version(session, uuid.uuid4())


async def test_latest_published_version_reraise_cancelled() -> None:
    session = AsyncMock()
    session.execute.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await _get_latest_published_version(session, uuid.uuid4())


# ---------------------------------------------------------------------------
# resolve_schema — local-equivalent resolution
# ---------------------------------------------------------------------------


async def test_resolve_schema_missing_name_warns() -> None:
    result = await resolve_schema(AsyncMock(), _ORG_ID, {"id": "x"})
    assert result["schema_id"] is None
    assert "missing 'name' field" in result["warning"]


async def test_resolve_schema_matches_by_abstract_name(monkeypatch: pytest.MonkeyPatch) -> None:
    session = AsyncMock()
    schema = SimpleNamespace(id=uuid.uuid4())
    result = MagicMock()
    result.scalar_one_or_none.return_value = schema
    session.execute = AsyncMock(return_value=result)
    monkeypatch.setattr(
        mod,
        "_get_latest_published_version",
        AsyncMock(return_value=SimpleNamespace(version="2.0")),
    )
    resolved = await resolve_schema(session, _ORG_ID, {"name": "S", "abstract_name": "abs", "definition_json": {}})
    assert resolved == {"schema_id": str(schema.id), "version": "2.0", "warning": None}


async def test_resolve_schema_matches_by_definition() -> None:
    session = AsyncMock()
    s1 = SimpleNamespace(id=uuid.uuid4())
    s2 = SimpleNamespace(id=uuid.uuid4())
    sv1 = SimpleNamespace(schema_id=s1.id, version="1.0", definition_json={"type": "object"})
    sv2 = SimpleNamespace(schema_id=s2.id, version="2.0", definition_json={"other": True})
    name_result = MagicMock()
    name_result.scalar_one_or_none.return_value = None
    schemas_result = MagicMock()
    schemas_result.scalars.return_value.all.return_value = [s1, s2]
    versions_result = MagicMock()
    versions_result.scalars.return_value.all.return_value = [sv1, sv2]
    session.execute = AsyncMock(side_effect=[name_result, schemas_result, versions_result])
    resolved = await resolve_schema(
        session, _ORG_ID, {"name": "S", "abstract_name": "abs", "definition_json": {"type": "object"}}
    )
    assert resolved == {"schema_id": str(s1.id), "version": "1.0", "warning": None}


async def test_resolve_schema_not_found_warns() -> None:
    session = AsyncMock()
    name_result = MagicMock()
    name_result.scalar_one_or_none.return_value = None
    schemas_result = MagicMock()
    schemas_result.scalars.return_value.all.return_value = []
    session.execute = AsyncMock(side_effect=[name_result, schemas_result])
    resolved = await resolve_schema(session, _ORG_ID, {"name": "S", "abstract_name": "abs", "definition_json": {}})
    assert resolved["schema_id"] is None
    assert "not found locally" in resolved["warning"]


async def test_resolve_schema_propagates_db_error() -> None:
    session = AsyncMock()
    session.execute.side_effect = SQLAlchemyError("read failed")
    with pytest.raises(SQLAlchemyError):
        await resolve_schema(session, _ORG_ID, {"name": "S", "abstract_name": "abs", "definition_json": {}})


async def test_resolve_schema_reraise_cancelled() -> None:
    session = AsyncMock()
    session.execute.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await resolve_schema(session, _ORG_ID, {"name": "S", "abstract_name": "abs", "definition_json": {}})


async def test_resolve_schema_by_definition_when_no_abstract_name() -> None:
    session = AsyncMock()
    name_result = MagicMock()
    name_result.scalar_one_or_none.return_value = None
    schemas_result = MagicMock()
    schemas_result.scalars.return_value.all.return_value = []
    session.execute = AsyncMock(side_effect=[name_result, schemas_result])
    resolved = await resolve_schema(session, _ORG_ID, {"name": "S", "abstract_name": "", "definition_json": {"a": 1}})
    assert resolved["schema_id"] is None
    assert "not found locally" in resolved["warning"]


async def test_resolve_schema_definition_match_with_duplicate_versions() -> None:
    s1 = SimpleNamespace(id=uuid.uuid4())
    s2 = SimpleNamespace(id=uuid.uuid4())
    sv1 = SimpleNamespace(schema_id=s1.id, version="1.0", definition_json={"type": "object"})
    sv1b = SimpleNamespace(schema_id=s1.id, version="0.9", definition_json={"other": True})
    sv2 = SimpleNamespace(schema_id=s2.id, version="1.0", definition_json={"kind": "different"})
    schemas_result = MagicMock()
    schemas_result.scalars.return_value.all.return_value = [s1, s2]
    versions_result = MagicMock()
    versions_result.scalars.return_value.all.return_value = [sv1, sv1b, sv2]
    session = AsyncMock()
    session.execute = AsyncMock(side_effect=[schemas_result, versions_result])
    resolved = await resolve_schema(
        session, _ORG_ID, {"name": "S", "abstract_name": "", "definition_json": {"missing": True}}
    )
    assert resolved["schema_id"] is None
    assert "not found locally" in resolved["warning"]


# ---------------------------------------------------------------------------
# resolve_connector_type — active local instance lookup
# ---------------------------------------------------------------------------


async def test_resolve_connector_type_returns_active_instance() -> None:
    session = AsyncMock()
    inst = SimpleNamespace(id=uuid.uuid4(), name="GitHub")
    result = MagicMock()
    result.scalars.return_value = [inst]
    session.execute = AsyncMock(return_value=result)
    resolved = await resolve_connector_type(session, _ORG_ID, "github")
    assert resolved == {"instance_id": str(inst.id), "instance_name": "GitHub", "warning": None}


async def test_resolve_connector_type_not_found_warns() -> None:
    session = AsyncMock()
    result = MagicMock()
    result.scalars.return_value = []
    session.execute = AsyncMock(return_value=result)
    resolved = await resolve_connector_type(session, _ORG_ID, "github")
    assert resolved["instance_id"] is None
    assert "not found locally" in resolved["warning"]


async def test_resolve_connector_type_propagates_db_error() -> None:
    session = AsyncMock()
    session.execute.side_effect = SQLAlchemyError("read failed")
    with pytest.raises(SQLAlchemyError):
        await resolve_connector_type(session, _ORG_ID, "github")


async def test_resolve_connector_type_reraise_cancelled() -> None:
    session = AsyncMock()
    session.execute.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await resolve_connector_type(session, _ORG_ID, "github")


# ---------------------------------------------------------------------------
# resolve_model_backend — name then provider+model_id fallback
# ---------------------------------------------------------------------------


async def test_resolve_model_backend_missing_fields_warns() -> None:
    resolved = await resolve_model_backend(AsyncMock(), _ORG_ID, {"name": "MB"})
    assert resolved["model_backend_id"] is None
    assert "missing required fields" in resolved["warning"]


async def test_resolve_model_backend_matches_by_name() -> None:
    session = AsyncMock()
    backend = SimpleNamespace(id=uuid.uuid4())
    result = MagicMock()
    result.scalar_one_or_none.return_value = backend
    session.execute = AsyncMock(return_value=result)
    resolved = await resolve_model_backend(session, _ORG_ID, {"name": "MB", "provider": "openai", "model_id": "gpt-4o"})
    assert resolved == {"model_backend_id": str(backend.id), "warning": None}


async def test_resolve_model_backend_falls_back_to_provider_model() -> None:
    session = AsyncMock()
    backend = SimpleNamespace(id=uuid.uuid4())
    name_result = MagicMock()
    name_result.scalar_one_or_none.return_value = None
    pair_result = MagicMock()
    pair_result.scalar_one_or_none.return_value = backend
    session.execute = AsyncMock(side_effect=[name_result, pair_result])
    resolved = await resolve_model_backend(session, _ORG_ID, {"name": "MB", "provider": "openai", "model_id": "gpt-4o"})
    assert resolved == {"model_backend_id": str(backend.id), "warning": None}


async def test_resolve_model_backend_not_found_warns() -> None:
    session = AsyncMock()
    name_result = MagicMock()
    name_result.scalar_one_or_none.return_value = None
    pair_result = MagicMock()
    pair_result.scalar_one_or_none.return_value = None
    session.execute = AsyncMock(side_effect=[name_result, pair_result])
    resolved = await resolve_model_backend(session, _ORG_ID, {"name": "MB", "provider": "openai", "model_id": "gpt-4o"})
    assert resolved["model_backend_id"] is None
    assert "not found locally" in resolved["warning"]


async def test_resolve_model_backend_reraise_cancelled() -> None:
    session = AsyncMock()
    session.execute.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await resolve_model_backend(session, _ORG_ID, {"name": "MB", "provider": "openai", "model_id": "gpt-4o"})


async def test_resolve_model_backend_propagates_db_error() -> None:
    session = AsyncMock()
    session.execute.side_effect = SQLAlchemyError("read failed")
    with pytest.raises(SQLAlchemyError):
        await resolve_model_backend(session, _ORG_ID, {"name": "MB", "provider": "openai", "model_id": "gpt-4o"})


# ---------------------------------------------------------------------------
# export_pipeline_bundle (v1) — ZIP bundle with org-private fields stripped
# ---------------------------------------------------------------------------


def _pipeline_fakes() -> dict[str, Any]:
    schema_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    mb_id = uuid.uuid4()
    pipeline = SimpleNamespace(
        id=uuid.uuid4(),
        name="My Pipeline",
        description="desc",
        graph_nodes_json=[{"agent_id": str(agent_id), "output_schema_id": str(schema_id)}],
        run_context_defaults={"key": "val"},
        node_timeout_seconds=600,
        retry_policy={"on": ["failure"], "max_retries": 2},
        owner_team_id=uuid.uuid4(),
        account_id=_ACCOUNT_ID,
        visibility="org",
    )
    agent = SimpleNamespace(
        id=agent_id,
        name="Agent A",
        description="agent desc",
        input_schema_id=schema_id,
        input_schema_version="2.0",
        output_schema_id=schema_id,
        output_schema_version="2.0",
        prompt_template="pt",
        model_backend_id=mb_id,
        connector_type_refs=[{"connector_type_id": "github"}],
        evals=[{"name": "e"}],
        retry_policy={"on": ["failure"], "max_retries": 1},
        token_budget=100,
        template_id=None,
        agent_command=None,
    )
    schema = SimpleNamespace(id=schema_id, name="Schema A", description="sd", abstract_name="abs")
    sv = SimpleNamespace(version="3.0", definition_json={"type": "object"})
    backend = SimpleNamespace(id=mb_id, name="MB", provider="openai", model_id="gpt-4o")
    edge = SimpleNamespace(
        id=uuid.uuid4(),
        source_node_id=uuid.uuid4(),
        target_node_id=uuid.uuid4(),
        edge_type="normal",
        hitl_gate_config=None,
    )
    return {
        "pipeline": pipeline,
        "agent": agent,
        "schema": schema,
        "sv": sv,
        "backend": backend,
        "edge": edge,
    }


def _export_session(fakes: dict[str, Any]) -> AsyncMock:
    session = AsyncMock()
    pipeline_result = MagicMock()
    pipeline_result.scalar_one_or_none.return_value = fakes["pipeline"]
    agents_result = MagicMock()
    agents_result.scalars.return_value = [fakes["agent"]]
    schemas_result = MagicMock()
    schemas_result.scalars.return_value = [fakes["schema"]]
    backends_result = MagicMock()
    backends_result.scalars.return_value = [fakes["backend"]]
    edges_result = MagicMock()
    edges_result.scalars.return_value = [fakes["edge"]]
    session.execute = AsyncMock(
        side_effect=[pipeline_result, agents_result, schemas_result, backends_result, edges_result]
    )
    return session


async def test_export_pipeline_bundle_not_found() -> None:
    session = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    session.execute = AsyncMock(return_value=result)
    with pytest.raises(ValueError, match="not found"):
        await export_pipeline_bundle(session, uuid.uuid4())


async def test_export_pipeline_bundle_builds_portable_zip(monkeypatch: pytest.MonkeyPatch) -> None:
    fakes = _pipeline_fakes()
    monkeypatch.setattr(mod, "_get_latest_published_version", AsyncMock(return_value=fakes["sv"]))
    data = await export_pipeline_bundle(_export_session(fakes), fakes["pipeline"].id)

    bundle = extract_bundle_json_from_zip(data)
    assert bundle["format_version"] == "1"
    assert bundle["pipeline"]["name"] == "My Pipeline"
    assert bundle["pipeline"]["node_timeout_seconds"] == 600
    assert bundle["pipeline"]["retry_policy"] == {"on": ["failure"], "max_retries": 2}
    assert "owner_team_id" not in bundle["pipeline"]
    assert bundle["pipeline"]["visibility"] == "org"
    assert bundle["agents"] == [
        {
            "id": str(fakes["agent"].id),
            "name": "Agent A",
            "description": "agent desc",
            "input_schema_id": str(fakes["schema"].id),
            "input_schema_version": "2.0",
            "output_schema_id": str(fakes["schema"].id),
            "output_schema_version": "2.0",
            "prompt_template": "pt",
            "model_backend_id": str(fakes["backend"].id),
            "connector_type_refs": [{"connector_type_id": "github"}],
            "evals": [{"name": "e"}],
            "retry_policy": {"on": ["failure"], "max_retries": 1},
            "token_budget": 100,
        }
    ]
    assert bundle["schemas"] == [
        {
            "id": str(fakes["schema"].id),
            "name": "Schema A",
            "description": "sd",
            "abstract_name": "abs",
            "latest_version": "3.0",
            "definition_json": {"type": "object"},
        }
    ]
    assert bundle["model_backends"] == [
        {"id": str(fakes["backend"].id), "name": "MB", "provider": "openai", "model_id": "gpt-4o"}
    ]
    assert bundle["edges"] == [
        {
            "id": str(fakes["edge"].id),
            "source_node_id": str(fakes["edge"].source_node_id),
            "target_node_id": str(fakes["edge"].target_node_id),
            "edge_type": "normal",
            "hitl_gate_config": None,
        }
    ]


async def test_export_pipeline_bundle_skips_invalid_agent_id(monkeypatch: pytest.MonkeyPatch) -> None:
    fakes = _pipeline_fakes()
    fakes["pipeline"].graph_nodes_json = [
        {"agent_id": "not-a-uuid"},
        {"agent_id": str(fakes["agent"].id), "output_schema_id": str(fakes["schema"].id)},
    ]
    monkeypatch.setattr(mod, "_get_latest_published_version", AsyncMock(return_value=fakes["sv"]))
    data = await export_pipeline_bundle(_export_session(fakes), fakes["pipeline"].id)
    bundle = extract_bundle_json_from_zip(data)
    assert len(bundle["agents"]) == 1


async def test_export_pipeline_bundle_skips_invalid_output_schema_id(monkeypatch: pytest.MonkeyPatch) -> None:
    fakes = _pipeline_fakes()
    fakes["pipeline"].graph_nodes_json = [
        {"agent_id": str(fakes["agent"].id), "output_schema_id": "not-a-uuid"},
    ]
    monkeypatch.setattr(mod, "_get_latest_published_version", AsyncMock(return_value=fakes["sv"]))
    data = await export_pipeline_bundle(_export_session(fakes), fakes["pipeline"].id)
    bundle = extract_bundle_json_from_zip(data)
    assert len(bundle["agents"]) == 1
    assert bundle["schemas"] == [
        {
            "id": str(fakes["schema"].id),
            "name": "Schema A",
            "description": "sd",
            "abstract_name": "abs",
            "latest_version": "3.0",
            "definition_json": {"type": "object"},
        }
    ]


async def test_export_pipeline_bundle_reraise_cancelled() -> None:
    session = AsyncMock()
    session.execute.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await export_pipeline_bundle(session, uuid.uuid4())


async def test_export_pipeline_bundle_without_graph_nodes(monkeypatch: pytest.MonkeyPatch) -> None:
    fakes = _pipeline_fakes()
    fakes["pipeline"].graph_nodes_json = None
    monkeypatch.setattr(mod, "_get_latest_published_version", AsyncMock(return_value=fakes["sv"]))
    session = AsyncMock()
    pipeline_result = MagicMock()
    pipeline_result.scalar_one_or_none.return_value = fakes["pipeline"]
    edges_result = MagicMock()
    edges_result.scalars.return_value = [fakes["edge"]]
    session.execute = AsyncMock(side_effect=[pipeline_result, edges_result])
    data = await export_pipeline_bundle(session, fakes["pipeline"].id)
    bundle = extract_bundle_json_from_zip(data)
    assert not bundle["agents"]
    assert not bundle["schemas"]
    assert not bundle["model_backends"]
    assert bundle["edges"] == [
        {
            "id": str(fakes["edge"].id),
            "source_node_id": str(fakes["edge"].source_node_id),
            "target_node_id": str(fakes["edge"].target_node_id),
            "edge_type": "normal",
            "hitl_gate_config": None,
        }
    ]


async def test_export_pipeline_bundle_mixed_node_refs(monkeypatch: pytest.MonkeyPatch) -> None:
    fakes = _pipeline_fakes()
    fakes["pipeline"].graph_nodes_json = [
        {"agent_id": str(fakes["agent"].id)},
        {"output_schema_id": str(fakes["schema"].id)},
    ]
    monkeypatch.setattr(mod, "_get_latest_published_version", AsyncMock(return_value=fakes["sv"]))
    data = await export_pipeline_bundle(_export_session(fakes), fakes["pipeline"].id)
    bundle = extract_bundle_json_from_zip(data)
    assert len(bundle["agents"]) == 1
    assert len(bundle["schemas"]) == 1


async def test_export_pipeline_bundle_agent_without_refs(monkeypatch: pytest.MonkeyPatch) -> None:
    bare_agent = SimpleNamespace(
        id=uuid.uuid4(),
        name="Bare",
        description=None,
        input_schema_id=None,
        input_schema_version=None,
        output_schema_id=None,
        output_schema_version=None,
        prompt_template="",
        model_backend_id=None,
        connector_type_refs=[],
        evals=[],
        retry_policy={},
        token_budget=None,
    )
    fakes = _pipeline_fakes()
    fakes["pipeline"].graph_nodes_json = [{"agent_id": str(bare_agent.id)}]
    monkeypatch.setattr(mod, "_get_latest_published_version", AsyncMock(return_value=fakes["sv"]))
    session = AsyncMock()
    pipeline_result = MagicMock()
    pipeline_result.scalar_one_or_none.return_value = fakes["pipeline"]
    agents_result = MagicMock()
    agents_result.scalars.return_value = [bare_agent]
    edges_result = MagicMock()
    edges_result.scalars.return_value = [fakes["edge"]]
    session.execute = AsyncMock(side_effect=[pipeline_result, agents_result, edges_result])
    data = await export_pipeline_bundle(session, fakes["pipeline"].id)
    bundle = extract_bundle_json_from_zip(data)
    assert bundle["agents"] == [
        {
            "id": str(bare_agent.id),
            "name": "Bare",
            "description": None,
            "input_schema_id": None,
            "input_schema_version": "1.0",
            "output_schema_id": None,
            "output_schema_version": "1.0",
            "prompt_template": "",
            "model_backend_id": None,
            "connector_type_refs": [],
            "evals": [],
            "retry_policy": {},
            "token_budget": None,
        }
    ]
    assert not bundle["schemas"]
    assert not bundle["model_backends"]


# ---------------------------------------------------------------------------
# export_pipeline_bundle_v2 — YAML bundle with trigger/owner/author enrichment
# ---------------------------------------------------------------------------


def _v2_session(fakes: dict[str, Any], *, trigger_error: bool = False, no_creator: bool = False) -> AsyncMock:
    session = AsyncMock()
    pipeline_result = MagicMock()
    pipeline_result.scalar_one_or_none.return_value = fakes["pipeline"]
    agents_result = MagicMock()
    agents_result.scalars.return_value = [fakes["agent"]]
    schemas_result = MagicMock()
    schemas_result.scalars.return_value = [fakes["schema"]]
    edges_result = MagicMock()
    edges_result.scalars.return_value = [fakes["edge"]]
    if trigger_error:
        triggers_result = MagicMock()
        triggers_result.scalars.side_effect = Exception("table missing")
    else:
        triggers_result = MagicMock()
        triggers_result.scalars.return_value.all.return_value = [
            SimpleNamespace(trigger_type="cron", config_json={"schedule": "* * * * *"}, active=True)
        ]
    team_result = MagicMock()
    team_result.scalar_one_or_none.return_value = SimpleNamespace(name="Team X")
    if no_creator:
        account_result = MagicMock()
        account_result.scalar_one_or_none.return_value = None
    else:
        account_result = MagicMock()
        account_result.scalar_one_or_none.return_value = SimpleNamespace(email="a@b.c")
    session.execute = AsyncMock(
        side_effect=[
            pipeline_result,
            agents_result,
            schemas_result,
            edges_result,
            triggers_result,
            team_result,
            account_result,
        ]
    )
    return session


async def test_export_pipeline_bundle_v2_not_found() -> None:
    session = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    session.execute = AsyncMock(return_value=result)
    with pytest.raises(ValueError, match="not found"):
        await export_pipeline_bundle_v2(session, uuid.uuid4())


async def test_export_pipeline_bundle_v2_yaml_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    fakes = _pipeline_fakes()
    monkeypatch.setattr(mod, "_get_latest_published_version", AsyncMock(return_value=fakes["sv"]))
    text = await export_pipeline_bundle_v2(_v2_session(fakes), fakes["pipeline"].id)

    doc = yaml.safe_load(text)
    workflow = doc["modulo_workflow"]
    assert workflow["name"] == "My Pipeline"
    assert workflow["version"] == "1.0.0"
    assert workflow["author"] == "a@b.c"
    assert workflow["owner_team"] == "Team X"
    assert workflow["visibility"] == "org"
    assert workflow["partial"] is False
    assert workflow["requires"]["connector_types"] == ["github"]
    assert workflow["requires"]["abstract_schemas"] == ["abs"]
    assert workflow["triggers"] == [{"trigger_type": "cron", "config": {"schedule": "* * * * *"}, "active": True}]
    assert workflow["agents"][0]["name"] == "Agent A"
    assert workflow["edges"][0]["edge_type"] == "normal"


async def test_export_pipeline_bundle_v2_falls_back_on_trigger_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    fakes = _pipeline_fakes()
    monkeypatch.setattr(mod, "_get_latest_published_version", AsyncMock(return_value=fakes["sv"]))
    text = await export_pipeline_bundle_v2(_v2_session(fakes, trigger_error=True), fakes["pipeline"].id)
    doc = yaml.safe_load(text)
    assert not doc["modulo_workflow"]["triggers"]


async def test_export_pipeline_bundle_v2_author_falls_back_to_account_id(monkeypatch: pytest.MonkeyPatch) -> None:
    fakes = _pipeline_fakes()
    monkeypatch.setattr(mod, "_get_latest_published_version", AsyncMock(return_value=fakes["sv"]))
    text = await export_pipeline_bundle_v2(_v2_session(fakes, no_creator=True), fakes["pipeline"].id)
    doc = yaml.safe_load(text)
    assert doc["modulo_workflow"]["author"] == str(_ACCOUNT_ID)


def _v2_aux_session(fakes: dict[str, Any], *, team_error: bool = False, account_error: bool = False) -> AsyncMock:
    session = AsyncMock()
    pipeline_result = MagicMock()
    pipeline_result.scalar_one_or_none.return_value = fakes["pipeline"]
    agents_result = MagicMock()
    agents_result.scalars.return_value = [fakes["agent"]]
    schemas_result = MagicMock()
    schemas_result.scalars.return_value = [fakes["schema"]]
    edges_result = MagicMock()
    edges_result.scalars.return_value = [fakes["edge"]]
    triggers_result = MagicMock()
    triggers_result.scalars.return_value.all.return_value = []
    team_result = MagicMock()
    if team_error:
        team_result.scalar_one_or_none.side_effect = Exception("team read failed")
    else:
        team_result.scalar_one_or_none.return_value = SimpleNamespace(name="Team X")
    account_result = MagicMock()
    if account_error:
        account_result.scalar_one_or_none.side_effect = Exception("account read failed")
    else:
        account_result.scalar_one_or_none.return_value = SimpleNamespace(email="a@b.c")
    session.execute = AsyncMock(
        side_effect=[
            pipeline_result,
            agents_result,
            schemas_result,
            edges_result,
            triggers_result,
            team_result,
            account_result,
        ]
    )
    return session


async def test_export_pipeline_bundle_v2_skips_invalid_node_refs(monkeypatch: pytest.MonkeyPatch) -> None:
    fakes = _pipeline_fakes()
    fakes["pipeline"].graph_nodes_json = [{"agent_id": "not-a-uuid", "output_schema_id": "not-a-uuid"}]
    monkeypatch.setattr(mod, "_get_latest_published_version", AsyncMock(return_value=fakes["sv"]))
    session = AsyncMock()
    pipeline_result = MagicMock()
    pipeline_result.scalar_one_or_none.return_value = fakes["pipeline"]
    edges_result = MagicMock()
    edges_result.scalars.return_value = [fakes["edge"]]
    triggers_result = MagicMock()
    triggers_result.scalars.return_value.all.return_value = []
    team_result = MagicMock()
    team_result.scalar_one_or_none.return_value = SimpleNamespace(name="Team X")
    account_result = MagicMock()
    account_result.scalar_one_or_none.return_value = SimpleNamespace(email="a@b.c")
    session.execute = AsyncMock(
        side_effect=[pipeline_result, edges_result, triggers_result, team_result, account_result]
    )
    text = await export_pipeline_bundle_v2(session, fakes["pipeline"].id)
    doc = yaml.safe_load(text)
    assert not doc["modulo_workflow"]["agents"]
    assert not doc["modulo_workflow"]["schemas"]


async def test_export_pipeline_bundle_v2_skips_invalid_output_schema_id(monkeypatch: pytest.MonkeyPatch) -> None:
    fakes = _pipeline_fakes()
    fakes["pipeline"].graph_nodes_json = [
        {"agent_id": str(fakes["agent"].id), "output_schema_id": "not-a-uuid"},
    ]
    monkeypatch.setattr(mod, "_get_latest_published_version", AsyncMock(return_value=fakes["sv"]))
    text = await export_pipeline_bundle_v2(_v2_aux_session(fakes), fakes["pipeline"].id)
    doc = yaml.safe_load(text)
    assert len(doc["modulo_workflow"]["agents"]) == 1
    assert doc["modulo_workflow"]["schemas"] == [
        {
            "id": str(fakes["schema"].id),
            "name": "Schema A",
            "description": "sd",
            "abstract_name": "abs",
            "latest_version": "3.0",
            "definition_json": {"type": "object"},
        }
    ]


async def test_export_pipeline_bundle_v2_owner_team_lookup_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    fakes = _pipeline_fakes()
    monkeypatch.setattr(mod, "_get_latest_published_version", AsyncMock(return_value=fakes["sv"]))
    text = await export_pipeline_bundle_v2(_v2_aux_session(fakes, team_error=True), fakes["pipeline"].id)
    doc = yaml.safe_load(text)
    assert doc["modulo_workflow"]["owner_team"] is None


async def test_export_pipeline_bundle_v2_creator_lookup_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    fakes = _pipeline_fakes()
    monkeypatch.setattr(mod, "_get_latest_published_version", AsyncMock(return_value=fakes["sv"]))
    text = await export_pipeline_bundle_v2(_v2_aux_session(fakes, account_error=True), fakes["pipeline"].id)
    doc = yaml.safe_load(text)
    assert doc["modulo_workflow"]["author"] == str(_ACCOUNT_ID)


async def test_export_pipeline_bundle_v2_reraise_cancelled() -> None:
    session = AsyncMock()
    session.execute.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await export_pipeline_bundle_v2(session, uuid.uuid4())


async def test_export_pipeline_bundle_v2_without_graph_nodes(monkeypatch: pytest.MonkeyPatch) -> None:
    fakes = _pipeline_fakes()
    fakes["pipeline"].graph_nodes_json = None
    monkeypatch.setattr(mod, "_get_latest_published_version", AsyncMock(return_value=fakes["sv"]))
    session = AsyncMock()
    pipeline_result = MagicMock()
    pipeline_result.scalar_one_or_none.return_value = fakes["pipeline"]
    edges_result = MagicMock()
    edges_result.scalars.return_value = [fakes["edge"]]
    triggers_result = MagicMock()
    triggers_result.scalars.return_value.all.return_value = []
    team_result = MagicMock()
    team_result.scalar_one_or_none.return_value = SimpleNamespace(name="Team X")
    account_result = MagicMock()
    account_result.scalar_one_or_none.return_value = SimpleNamespace(email="a@b.c")
    session.execute = AsyncMock(
        side_effect=[pipeline_result, edges_result, triggers_result, team_result, account_result]
    )
    text = await export_pipeline_bundle_v2(session, fakes["pipeline"].id)
    doc = yaml.safe_load(text)
    assert not doc["modulo_workflow"]["agents"]
    assert not doc["modulo_workflow"]["schemas"]


async def test_export_pipeline_bundle_v2_mixed_node_refs(monkeypatch: pytest.MonkeyPatch) -> None:
    fakes = _pipeline_fakes()
    fakes["pipeline"].graph_nodes_json = [
        {"agent_id": str(fakes["agent"].id)},
        {"output_schema_id": str(fakes["schema"].id)},
    ]
    monkeypatch.setattr(mod, "_get_latest_published_version", AsyncMock(return_value=fakes["sv"]))
    text = await export_pipeline_bundle_v2(_v2_aux_session(fakes), fakes["pipeline"].id)
    doc = yaml.safe_load(text)
    assert len(doc["modulo_workflow"]["agents"]) == 1
    assert len(doc["modulo_workflow"]["schemas"]) == 1


async def test_export_pipeline_bundle_v2_agent_without_refs(monkeypatch: pytest.MonkeyPatch) -> None:
    bare_agent = SimpleNamespace(
        id=uuid.uuid4(),
        name="Bare",
        description=None,
        input_schema_id=None,
        output_schema_id=None,
        prompt_template="",
        template_id=None,
        agent_command=None,
        model_backend_id=None,
        connector_type_refs=[],
        evals=[],
        retry_policy={},
        token_budget=None,
    )
    fakes = _pipeline_fakes()
    fakes["pipeline"].graph_nodes_json = [{"agent_id": str(bare_agent.id)}]
    monkeypatch.setattr(mod, "_get_latest_published_version", AsyncMock(return_value=fakes["sv"]))
    session = AsyncMock()
    pipeline_result = MagicMock()
    pipeline_result.scalar_one_or_none.return_value = fakes["pipeline"]
    agents_result = MagicMock()
    agents_result.scalars.return_value = [bare_agent]
    edges_result = MagicMock()
    edges_result.scalars.return_value = [fakes["edge"]]
    triggers_result = MagicMock()
    triggers_result.scalars.return_value.all.return_value = []
    team_result = MagicMock()
    team_result.scalar_one_or_none.return_value = SimpleNamespace(name="Team X")
    account_result = MagicMock()
    account_result.scalar_one_or_none.return_value = SimpleNamespace(email="a@b.c")
    session.execute = AsyncMock(
        side_effect=[pipeline_result, agents_result, edges_result, triggers_result, team_result, account_result]
    )
    text = await export_pipeline_bundle_v2(session, fakes["pipeline"].id)
    doc = yaml.safe_load(text)
    assert doc["modulo_workflow"]["agents"][0]["name"] == "Bare"
    assert not doc["modulo_workflow"]["schemas"]


async def test_export_pipeline_bundle_v2_without_owner_team(monkeypatch: pytest.MonkeyPatch) -> None:
    fakes = _pipeline_fakes()
    fakes["pipeline"].owner_team_id = None
    monkeypatch.setattr(mod, "_get_latest_published_version", AsyncMock(return_value=fakes["sv"]))
    session = AsyncMock()
    pipeline_result = MagicMock()
    pipeline_result.scalar_one_or_none.return_value = fakes["pipeline"]
    agents_result = MagicMock()
    agents_result.scalars.return_value = [fakes["agent"]]
    schemas_result = MagicMock()
    schemas_result.scalars.return_value = [fakes["schema"]]
    edges_result = MagicMock()
    edges_result.scalars.return_value = [fakes["edge"]]
    triggers_result = MagicMock()
    triggers_result.scalars.return_value.all.return_value = []
    account_result = MagicMock()
    account_result.scalar_one_or_none.return_value = SimpleNamespace(email="a@b.c")
    session.execute = AsyncMock(
        side_effect=[pipeline_result, agents_result, schemas_result, edges_result, triggers_result, account_result]
    )
    text = await export_pipeline_bundle_v2(session, fakes["pipeline"].id)
    doc = yaml.safe_load(text)
    assert doc["modulo_workflow"]["owner_team"] is None


async def test_export_pipeline_bundle_v2_unresolved_owner_team(monkeypatch: pytest.MonkeyPatch) -> None:
    fakes = _pipeline_fakes()
    monkeypatch.setattr(mod, "_get_latest_published_version", AsyncMock(return_value=fakes["sv"]))
    session = AsyncMock()
    pipeline_result = MagicMock()
    pipeline_result.scalar_one_or_none.return_value = fakes["pipeline"]
    agents_result = MagicMock()
    agents_result.scalars.return_value = [fakes["agent"]]
    schemas_result = MagicMock()
    schemas_result.scalars.return_value = [fakes["schema"]]
    edges_result = MagicMock()
    edges_result.scalars.return_value = [fakes["edge"]]
    triggers_result = MagicMock()
    triggers_result.scalars.return_value.all.return_value = []
    team_result = MagicMock()
    team_result.scalar_one_or_none.return_value = None
    account_result = MagicMock()
    account_result.scalar_one_or_none.return_value = SimpleNamespace(email="a@b.c")
    session.execute = AsyncMock(
        side_effect=[
            pipeline_result,
            agents_result,
            schemas_result,
            edges_result,
            triggers_result,
            team_result,
            account_result,
        ]
    )
    text = await export_pipeline_bundle_v2(session, fakes["pipeline"].id)
    doc = yaml.safe_load(text)
    assert doc["modulo_workflow"]["owner_team"] is None


async def test_export_pipeline_bundle_v2_empty_requires_collections(monkeypatch: pytest.MonkeyPatch) -> None:
    fakes = _pipeline_fakes()
    fakes["agent"].connector_type_refs = [{"connector_type_id": ""}]
    fakes["schema"].abstract_name = None
    monkeypatch.setattr(mod, "_get_latest_published_version", AsyncMock(return_value=fakes["sv"]))
    text = await export_pipeline_bundle_v2(_v2_aux_session(fakes), fakes["pipeline"].id)
    doc = yaml.safe_load(text)
    assert not doc["modulo_workflow"]["requires"]["connector_types"]
    assert not doc["modulo_workflow"]["requires"]["abstract_schemas"]


# ---------------------------------------------------------------------------
# materialize_import — schema/agent/pipeline/edge creation + warning paths
# ---------------------------------------------------------------------------


def _make_bundle(**overrides: Any) -> dict[str, Any]:
    bundle: dict[str, Any] = {
        "format_version": "1",
        "pipeline": {
            "name": "Imported",
            "description": "d",
            "graph_nodes_json": [],
            "retry_policy": {"on": ["failure"], "max_retries": 2},
            "node_timeout_seconds": 900,
            "run_context_defaults": {"foo": "bar"},
        },
        "agents": [],
        "schemas": [],
        "edges": [],
        "model_backends": [],
    }
    bundle.update(overrides)
    return bundle


def _patch_crud(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    new_schema = SimpleNamespace(id=uuid.uuid4())
    new_sv = SimpleNamespace(version="1.0")
    new_agent = SimpleNamespace(id=uuid.uuid4())
    new_pipeline = SimpleNamespace(id=uuid.uuid4())
    new_prim = SimpleNamespace(id=uuid.uuid4())
    create_schema_mock = AsyncMock(return_value=new_schema)
    create_schema_version_mock = AsyncMock(return_value=new_sv)
    create_agent_mock = AsyncMock(return_value=new_agent)
    create_pipeline_mock = AsyncMock(return_value=new_pipeline)
    create_prim_mock = AsyncMock(return_value=new_prim)
    monkeypatch.setattr(mod, "create_schema", create_schema_mock)
    monkeypatch.setattr(mod, "create_schema_version", create_schema_version_mock)
    monkeypatch.setattr(mod, "create_agent", create_agent_mock)
    monkeypatch.setattr(mod, "create_pipeline", create_pipeline_mock)
    monkeypatch.setattr(mod, "create_library_primitive", create_prim_mock)
    monkeypatch.setattr(mod, "get_existing_agent_names", AsyncMock(return_value=set()))
    monkeypatch.setattr(mod, "get_existing_pipeline_names", AsyncMock(return_value=set()))
    return {
        "schema": create_schema_mock,
        "sv": create_schema_version_mock,
        "agent": create_agent_mock,
        "pipeline": create_pipeline_mock,
        "prim": create_prim_mock,
    }


def _no_existing_schema_result() -> MagicMock:
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    result.scalars.return_value.all.return_value = []
    return result


async def test_materialize_rejects_unsupported_format_version() -> None:
    bundle = _make_bundle(format_version="99")
    with pytest.raises(ValueError, match="Unsupported bundle format version"):
        await materialize_import(_FakeSession(), _ORG_ID, _ACCOUNT_ID, bundle)


async def test_materialize_rejects_unknown_owner_team() -> None:
    bundle = _make_bundle()
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    with pytest.raises(ValueError, match="not found in this organisation"):
        await materialize_import(_FakeSession(result), _ORG_ID, _ACCOUNT_ID, bundle, owner_team_id=uuid.uuid4())


async def test_materialize_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    created = _patch_crud(monkeypatch)
    export_schema_id = uuid.uuid4()
    export_agent_id = uuid.uuid4()
    export_mb_id = uuid.uuid4()
    edge_id = uuid.uuid4()
    bundle = _make_bundle(
        pipeline={
            "name": "Imported",
            "description": "d",
            "graph_nodes_json": [{"agent_id": str(export_agent_id), "output_schema_id": str(export_schema_id)}],
            "retry_policy": {"on": ["failure"], "max_retries": 2},
            "node_timeout_seconds": 900,
            "run_context_defaults": {"foo": "bar"},
        },
        agents=[
            {
                "id": str(export_agent_id),
                "name": "Agent A",
                "input_schema_id": str(export_schema_id),
                "input_schema_version": "1.0",
                "output_schema_id": str(export_schema_id),
                "output_schema_version": "1.0",
                "prompt_template": "pt",
                "model_backend_id": str(export_mb_id),
                "_resolved_model_backend_id": str(uuid.uuid4()),
            }
        ],
        schemas=[
            {
                "id": str(export_schema_id),
                "name": "Schema A",
                "description": "sd",
                "abstract_name": "abs",
                "latest_version": "1.0",
                "definition_json": {"type": "object"},
            }
        ],
        edges=[
            {
                "id": str(edge_id),
                "source_node_id": str(uuid.uuid4()),
                "target_node_id": str(uuid.uuid4()),
                "edge_type": "normal",
            }
        ],
    )
    result = await materialize_import(_FakeSession(_no_existing_schema_result()), _ORG_ID, _ACCOUNT_ID, bundle)

    assert not result["warnings"]
    assert result["pipeline_id"] == str(created["pipeline"].return_value.id)
    assert result["agent_count"] == 1
    assert result["schema_count"] == 1
    assert result["edge_count"] == 1
    assert created["schema"].await_args.kwargs["name"] == "Schema A"
    assert created["agent"].await_args.kwargs["name"] == "Agent A"
    assert created["pipeline"].await_args.kwargs["name"] == "Imported"
    assert created["pipeline"].await_args.kwargs["node_timeout_seconds"] == 900
    assert created["pipeline"].await_args.kwargs["owner_team_id"] is None
    assert created["pipeline"].await_args.kwargs["visibility"] == "org"
    assert created["prim"].await_args.kwargs["slug"] == "imported"
    assert created["prim"].await_args.kwargs["tags"] == ["imported"]
    assert created["pipeline"].return_value.graph_nodes_json == [
        {
            "agent_id": str(created["agent"].return_value.id),
            "output_schema_id": str(created["schema"].return_value.id),
        }
    ]
    assert created["pipeline"].return_value.retry_policy == {"on": ["failure"], "max_retries": 2}


async def test_materialize_rewires_connector_binding_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    created = _patch_crud(monkeypatch)
    export_schema_id = uuid.uuid4()
    export_agent_id = uuid.uuid4()
    local_conn = uuid.uuid4()
    bundle = _make_bundle(
        pipeline={
            "name": "Imported",
            "graph_nodes_json": [
                {
                    "agent_id": str(export_agent_id),
                    "output_schema_id": str(export_schema_id),
                    "connector_binding": {"instance_id": "exp-conn"},
                }
            ],
            "retry_policy": {},
        },
        agents=[{"id": str(export_agent_id), "name": "Agent A"}],
        schemas=[{"id": str(export_schema_id), "name": "Schema A", "definition_json": {"type": "object"}}],
        edges=[],
    )
    await materialize_import(
        _FakeSession(_no_existing_schema_result()),
        _ORG_ID,
        _ACCOUNT_ID,
        bundle,
        connector_instance_overrides={"exp-conn": str(local_conn)},
    )
    nodes = created["pipeline"].return_value.graph_nodes_json
    assert nodes[0]["connector_binding"]["instance_id"] == str(local_conn)
    assert nodes[0]["agent_id"] == str(created["agent"].return_value.id)


async def test_materialize_applies_schema_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    created = _patch_crud(monkeypatch)
    export_schema_id = uuid.uuid4()
    bundle = _make_bundle(
        pipeline={"name": "Imported", "graph_nodes_json": [], "retry_policy": {}},
        schemas=[{"id": str(export_schema_id), "name": "Schema A", "definition_json": {"type": "object"}}],
    )
    result = await materialize_import(
        _FakeSession(),
        _ORG_ID,
        _ACCOUNT_ID,
        bundle,
        schema_id_overrides={str(export_schema_id): "local-id"},
        schema_version_overrides={str(export_schema_id): "9"},
    )
    created["schema"].assert_not_awaited()
    assert not result["warnings"]


async def test_materialize_drops_malformed_retry_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    created = _patch_crud(monkeypatch)
    bundle = _make_bundle(pipeline={"name": "Imported", "graph_nodes_json": [], "retry_policy": {"on": ["bogus"]}})
    result = await materialize_import(_FakeSession(), _ORG_ID, _ACCOUNT_ID, bundle)
    assert not created["pipeline"].return_value.retry_policy
    assert any("malformed" in w for w in result["warnings"])


async def test_materialize_non_list_graph_nodes_warns(monkeypatch: pytest.MonkeyPatch) -> None:
    created = _patch_crud(monkeypatch)
    bundle = _make_bundle(pipeline={"name": "Imported", "graph_nodes_json": {"not": "list"}, "retry_policy": {}})
    result = await materialize_import(_FakeSession(), _ORG_ID, _ACCOUNT_ID, bundle)
    assert not created["pipeline"].return_value.graph_nodes_json
    assert any("graph_nodes_json" in w for w in result["warnings"])


async def test_materialize_skips_schema_without_id(monkeypatch: pytest.MonkeyPatch) -> None:
    created = _patch_crud(monkeypatch)
    bundle = _make_bundle(
        pipeline={"name": "Imported", "graph_nodes_json": [], "retry_policy": {}},
        schemas=[{"id": ""}],
    )
    result = await materialize_import(_FakeSession(), _ORG_ID, _ACCOUNT_ID, bundle)
    assert result["schema_count"] == 1
    created["schema"].assert_not_awaited()
    assert any("no 'id'" in w for w in result["warnings"])


async def test_materialize_skips_schema_without_definition(monkeypatch: pytest.MonkeyPatch) -> None:
    created = _patch_crud(monkeypatch)
    export_schema_id = uuid.uuid4()
    bundle = _make_bundle(
        pipeline={"name": "Imported", "graph_nodes_json": [], "retry_policy": {}},
        schemas=[{"id": str(export_schema_id), "name": "Schema A", "definition_json": None}],
    )
    result = await materialize_import(_FakeSession(), _ORG_ID, _ACCOUNT_ID, bundle)
    created["schema"].assert_not_awaited()
    assert any("no definition JSON" in w for w in result["warnings"])


async def test_materialize_skips_invalid_edge_uuid(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_crud(monkeypatch)
    bundle = _make_bundle(
        pipeline={"name": "Imported", "graph_nodes_json": [], "retry_policy": {}},
        edges=[{"id": "bad-id", "source_node_id": "bad-src", "target_node_id": "bad-tgt", "edge_type": "normal"}],
    )
    result = await materialize_import(_FakeSession(), _ORG_ID, _ACCOUNT_ID, bundle)
    assert result["edge_count"] == 0
    assert any("Skipping edge" in w for w in result["warnings"])


async def test_materialize_defaults_unknown_edge_type(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_crud(monkeypatch)
    session = _FakeSession()
    bundle = _make_bundle(
        pipeline={"name": "Imported", "graph_nodes_json": [], "retry_policy": {}},
        edges=[
            {
                "id": str(uuid.uuid4()),
                "source_node_id": str(uuid.uuid4()),
                "target_node_id": str(uuid.uuid4()),
                "edge_type": "bogus",
            }
        ],
    )
    result = await materialize_import(session, _ORG_ID, _ACCOUNT_ID, bundle)
    assert result["edge_count"] == 1
    assert any("Unknown edge type" in w for w in result["warnings"])
    added_edge = session.add.call_args.args[0]
    assert added_edge.edge_type == "normal"


async def test_materialize_warns_on_unresolved_agent_refs(monkeypatch: pytest.MonkeyPatch) -> None:
    created = _patch_crud(monkeypatch)
    export_agent_id = uuid.uuid4()
    export_schema_id = uuid.uuid4()
    bundle = _make_bundle(
        pipeline={"name": "Imported", "graph_nodes_json": [], "retry_policy": {}},
        agents=[
            {
                "id": str(export_agent_id),
                "name": "Agent A",
                "input_schema_id": str(export_schema_id),
                "output_schema_id": str(export_schema_id),
                "prompt_template": "pt",
            }
        ],
    )
    result = await materialize_import(_FakeSession(), _ORG_ID, _ACCOUNT_ID, bundle)
    agent_kwargs = created["agent"].await_args.kwargs
    assert "input_schema_id" not in agent_kwargs
    assert "output_schema_id" not in agent_kwargs
    assert any("unresolved input schema" in w for w in result["warnings"])
    assert any("unresolved output schema" in w for w in result["warnings"])


async def test_materialize_renames_schema_on_definition_collision(monkeypatch: pytest.MonkeyPatch) -> None:
    created = _patch_crud(monkeypatch)
    existing = SimpleNamespace(id=uuid.uuid4(), name="Schema A")
    existing_sv = SimpleNamespace(version="1.0", definition_json={"different": True})
    monkeypatch.setattr(mod, "_get_latest_published_version", AsyncMock(return_value=existing_sv))
    export_schema_id = uuid.uuid4()
    bundle = _make_bundle(
        pipeline={"name": "Imported", "graph_nodes_json": [], "retry_policy": {}},
        schemas=[{"id": str(export_schema_id), "name": "Schema A", "definition_json": {"type": "object"}}],
    )
    existing_result = MagicMock()
    existing_result.scalar_one_or_none.return_value = existing
    all_result = MagicMock()
    all_result.scalars.return_value.all.return_value = [existing]
    session = _FakeSession(existing_result, all_result)
    result = await materialize_import(session, _ORG_ID, _ACCOUNT_ID, bundle)
    assert created["schema"].await_args.kwargs["name"] == "Schema A (imported)"
    assert any("different structure" in w for w in result["warnings"])


async def test_materialize_retries_schema_create_on_integrity_error(monkeypatch: pytest.MonkeyPatch) -> None:
    created = _patch_crud(monkeypatch)
    export_schema_id = uuid.uuid4()
    bundle = _make_bundle(
        pipeline={"name": "Imported", "graph_nodes_json": [], "retry_policy": {}},
        schemas=[{"id": str(export_schema_id), "name": "Schema A", "definition_json": {"type": "object"}}],
    )
    create_schema_mock = AsyncMock(side_effect=[IntegrityError("s", None, "dup"), created["schema"].return_value])
    monkeypatch.setattr(mod, "create_schema", create_schema_mock)
    all_result = MagicMock()
    all_result.scalars.return_value.all.return_value = [SimpleNamespace(name="Schema A")]
    session = _FakeSession(_no_existing_schema_result(), all_result)
    result = await materialize_import(session, _ORG_ID, _ACCOUNT_ID, bundle)
    assert create_schema_mock.await_count == 2
    assert create_schema_mock.await_args.kwargs["name"] == "Schema A (imported)"
    assert any("collided" in w for w in result["warnings"])


async def test_materialize_uses_pipeline_name_override(monkeypatch: pytest.MonkeyPatch) -> None:
    created = _patch_crud(monkeypatch)
    bundle = _make_bundle(pipeline={"name": "Bundled Name", "graph_nodes_json": [], "retry_policy": {}})
    await materialize_import(
        _FakeSession(),
        _ORG_ID,
        _ACCOUNT_ID,
        bundle,
        pipeline_name_override="Overridden",
    )
    assert created["pipeline"].await_args.kwargs["name"] == "Overridden"


async def test_materialize_uses_resolved_schema_id(monkeypatch: pytest.MonkeyPatch) -> None:
    created = _patch_crud(monkeypatch)
    export_schema_id = uuid.uuid4()
    local_schema_id = uuid.uuid4()
    bundle = _make_bundle(
        pipeline={"name": "Imported", "graph_nodes_json": [], "retry_policy": {}},
        schemas=[
            {
                "id": str(export_schema_id),
                "name": "Schema A",
                "definition_json": {"type": "object"},
                "_resolved_id": str(local_schema_id),
                "_resolved_version": "5.0",
            }
        ],
    )
    result = await materialize_import(_FakeSession(), _ORG_ID, _ACCOUNT_ID, bundle)
    created["schema"].assert_not_awaited()
    assert result["schemas"] == {str(export_schema_id): str(local_schema_id)}


async def test_materialize_reuses_schema_with_matching_definition(monkeypatch: pytest.MonkeyPatch) -> None:
    created = _patch_crud(monkeypatch)
    existing = SimpleNamespace(id=uuid.uuid4(), name="Schema A")
    monkeypatch.setattr(
        mod,
        "_get_latest_published_version",
        AsyncMock(return_value=SimpleNamespace(version="2.0", definition_json={"type": "object"})),
    )
    export_schema_id = uuid.uuid4()
    bundle = _make_bundle(
        pipeline={"name": "Imported", "graph_nodes_json": [], "retry_policy": {}},
        schemas=[{"id": str(export_schema_id), "name": "Schema A", "definition_json": {"type": "object"}}],
    )
    existing_result = MagicMock()
    existing_result.scalar_one_or_none.return_value = existing
    result = await materialize_import(_FakeSession(existing_result), _ORG_ID, _ACCOUNT_ID, bundle)
    created["schema"].assert_not_awaited()
    assert result["schemas"] == {str(export_schema_id): str(existing.id)}


async def test_materialize_raises_when_schema_retries_exhausted(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_crud(monkeypatch)
    export_schema_id = uuid.uuid4()
    bundle = _make_bundle(
        pipeline={"name": "Imported", "graph_nodes_json": [], "retry_policy": {}},
        schemas=[{"id": str(export_schema_id), "name": "Schema A", "definition_json": {"type": "object"}}],
    )
    schema_mock = AsyncMock(side_effect=IntegrityError("s", None, "dup"))
    monkeypatch.setattr(mod, "create_schema", schema_mock)
    with pytest.raises(IntegrityError):
        await materialize_import(
            _FakeSession(_no_existing_schema_result(), _no_existing_schema_result()),
            _ORG_ID,
            _ACCOUNT_ID,
            bundle,
        )
    assert schema_mock.await_count == mod._MAX_NAME_RETRIES


async def test_materialize_raises_on_schema_create_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_crud(monkeypatch)
    export_schema_id = uuid.uuid4()
    bundle = _make_bundle(
        pipeline={"name": "Imported", "graph_nodes_json": [], "retry_policy": {}},
        schemas=[{"id": str(export_schema_id), "name": "Schema A", "definition_json": {"type": "object"}}],
    )
    monkeypatch.setattr(mod, "create_schema", AsyncMock(side_effect=RuntimeError("boom")))
    with pytest.raises(RuntimeError, match="boom"):
        await materialize_import(_FakeSession(_no_existing_schema_result()), _ORG_ID, _ACCOUNT_ID, bundle)


async def test_materialize_raises_on_schema_version_create_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_crud(monkeypatch)
    export_schema_id = uuid.uuid4()
    bundle = _make_bundle(
        pipeline={"name": "Imported", "graph_nodes_json": [], "retry_policy": {}},
        schemas=[{"id": str(export_schema_id), "name": "Schema A", "definition_json": {"type": "object"}}],
    )
    monkeypatch.setattr(mod, "create_schema_version", AsyncMock(side_effect=RuntimeError("boom")))
    with pytest.raises(RuntimeError, match="boom"):
        await materialize_import(_FakeSession(_no_existing_schema_result()), _ORG_ID, _ACCOUNT_ID, bundle)


async def test_materialize_warns_on_unresolved_model_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    created = _patch_crud(monkeypatch)
    export_agent_id = uuid.uuid4()
    export_mb_id = uuid.uuid4()
    bundle = _make_bundle(
        pipeline={"name": "Imported", "graph_nodes_json": [], "retry_policy": {}},
        agents=[{"id": str(export_agent_id), "name": "Agent A", "model_backend_id": str(export_mb_id)}],
    )
    result = await materialize_import(_FakeSession(), _ORG_ID, _ACCOUNT_ID, bundle)
    assert "model_backend_id" not in created["agent"].await_args.kwargs
    assert any("unresolved model backend" in w for w in result["warnings"])


async def test_materialize_retries_agent_create_on_integrity_error(monkeypatch: pytest.MonkeyPatch) -> None:
    created = _patch_crud(monkeypatch)
    export_agent_id = uuid.uuid4()
    bundle = _make_bundle(
        pipeline={"name": "Imported", "graph_nodes_json": [], "retry_policy": {}},
        agents=[{"id": str(export_agent_id), "name": "Agent A"}],
    )
    agent_mock = AsyncMock(side_effect=[IntegrityError("s", None, "dup"), created["agent"].return_value])
    monkeypatch.setattr(mod, "create_agent", agent_mock)
    result = await materialize_import(_FakeSession(), _ORG_ID, _ACCOUNT_ID, bundle)
    assert agent_mock.await_count == 2
    assert agent_mock.await_args.kwargs["name"] == "Agent A (imported)"
    assert any("collided" in w for w in result["warnings"])


async def test_materialize_raises_when_agent_retries_exhausted(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_crud(monkeypatch)
    export_agent_id = uuid.uuid4()
    bundle = _make_bundle(
        pipeline={"name": "Imported", "graph_nodes_json": [], "retry_policy": {}},
        agents=[{"id": str(export_agent_id), "name": "Agent A"}],
    )
    agent_mock = AsyncMock(side_effect=IntegrityError("s", None, "dup"))
    monkeypatch.setattr(mod, "create_agent", agent_mock)
    with pytest.raises(IntegrityError):
        await materialize_import(_FakeSession(), _ORG_ID, _ACCOUNT_ID, bundle)
    assert agent_mock.await_count == mod._MAX_NAME_RETRIES


async def test_materialize_raises_on_agent_create_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_crud(monkeypatch)
    export_agent_id = uuid.uuid4()
    bundle = _make_bundle(
        pipeline={"name": "Imported", "graph_nodes_json": [], "retry_policy": {}},
        agents=[{"id": str(export_agent_id), "name": "Agent A"}],
    )
    monkeypatch.setattr(mod, "create_agent", AsyncMock(side_effect=SQLAlchemyError("boom")))
    with pytest.raises(SQLAlchemyError):
        await materialize_import(_FakeSession(), _ORG_ID, _ACCOUNT_ID, bundle)


async def test_materialize_retries_pipeline_create_on_integrity_error(monkeypatch: pytest.MonkeyPatch) -> None:
    created = _patch_crud(monkeypatch)
    bundle = _make_bundle(pipeline={"name": "Imported", "graph_nodes_json": [], "retry_policy": {}})
    pipeline_mock = AsyncMock(side_effect=[IntegrityError("s", None, "dup"), created["pipeline"].return_value])
    monkeypatch.setattr(mod, "create_pipeline", pipeline_mock)
    result = await materialize_import(_FakeSession(), _ORG_ID, _ACCOUNT_ID, bundle)
    assert pipeline_mock.await_count == 2
    assert pipeline_mock.await_args.kwargs["name"] == "Imported (imported)"
    assert any("conflicted" in w for w in result["warnings"])


async def test_materialize_raises_when_pipeline_retries_exhausted(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_crud(monkeypatch)
    bundle = _make_bundle(pipeline={"name": "Imported", "graph_nodes_json": [], "retry_policy": {}})
    pipeline_mock = AsyncMock(side_effect=IntegrityError("s", None, "dup"))
    monkeypatch.setattr(mod, "create_pipeline", pipeline_mock)
    with pytest.raises(IntegrityError):
        await materialize_import(_FakeSession(), _ORG_ID, _ACCOUNT_ID, bundle)
    assert pipeline_mock.await_count == mod._MAX_NAME_RETRIES


async def test_materialize_raises_on_pipeline_create_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_crud(monkeypatch)
    bundle = _make_bundle(pipeline={"name": "Imported", "graph_nodes_json": [], "retry_policy": {}})
    monkeypatch.setattr(mod, "create_pipeline", AsyncMock(side_effect=RuntimeError("boom")))
    with pytest.raises(RuntimeError, match="boom"):
        await materialize_import(_FakeSession(), _ORG_ID, _ACCOUNT_ID, bundle)


async def test_materialize_raises_on_library_primitive_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_crud(monkeypatch)
    bundle = _make_bundle(pipeline={"name": "Imported", "graph_nodes_json": [], "retry_policy": {}})
    monkeypatch.setattr(mod, "create_library_primitive", AsyncMock(side_effect=RuntimeError("boom")))
    with pytest.raises(RuntimeError, match="boom"):
        await materialize_import(_FakeSession(), _ORG_ID, _ACCOUNT_ID, bundle)


async def test_materialize_accepts_existing_owner_team(monkeypatch: pytest.MonkeyPatch) -> None:
    created = _patch_crud(monkeypatch)
    owner_team_id = uuid.uuid4()
    bundle = _make_bundle(pipeline={"name": "Imported", "graph_nodes_json": [], "retry_policy": {}})
    team_result = MagicMock()
    team_result.scalar_one_or_none.return_value = SimpleNamespace(id=owner_team_id)
    await materialize_import(
        _FakeSession(team_result),
        _ORG_ID,
        _ACCOUNT_ID,
        bundle,
        owner_team_id=owner_team_id,
    )
    assert created["pipeline"].await_args.kwargs["owner_team_id"] == owner_team_id


async def test_materialize_applies_schema_id_override_without_version(monkeypatch: pytest.MonkeyPatch) -> None:
    created = _patch_crud(monkeypatch)
    export_schema_id = uuid.uuid4()
    bundle = _make_bundle(
        pipeline={"name": "Imported", "graph_nodes_json": [], "retry_policy": {}},
        schemas=[{"id": str(export_schema_id), "name": "Schema A", "definition_json": {"type": "object"}}],
    )
    result = await materialize_import(
        _FakeSession(),
        _ORG_ID,
        _ACCOUNT_ID,
        bundle,
        schema_id_overrides={str(export_schema_id): "local-id"},
    )
    created["schema"].assert_not_awaited()
    assert result["schemas"] == {str(export_schema_id): "local-id"}


async def test_materialize_uses_resolved_schema_id_without_version(monkeypatch: pytest.MonkeyPatch) -> None:
    created = _patch_crud(monkeypatch)
    export_schema_id = uuid.uuid4()
    local_schema_id = uuid.uuid4()
    bundle = _make_bundle(
        pipeline={"name": "Imported", "graph_nodes_json": [], "retry_policy": {}},
        schemas=[{"id": str(export_schema_id), "name": "Schema A", "_resolved_id": str(local_schema_id)}],
    )
    result = await materialize_import(_FakeSession(), _ORG_ID, _ACCOUNT_ID, bundle)
    created["schema"].assert_not_awaited()
    assert result["schemas"] == {str(export_schema_id): str(local_schema_id)}


async def test_materialize_renames_multiple_schema_collisions(monkeypatch: pytest.MonkeyPatch) -> None:
    created = _patch_crud(monkeypatch)
    existing_a = SimpleNamespace(id=uuid.uuid4(), name="Schema A")
    existing_b = SimpleNamespace(id=uuid.uuid4(), name="Schema B")
    monkeypatch.setattr(
        mod,
        "_get_latest_published_version",
        AsyncMock(return_value=SimpleNamespace(version="1.0", definition_json={"different": True})),
    )
    e1, e2 = uuid.uuid4(), uuid.uuid4()
    bundle = _make_bundle(
        pipeline={"name": "Imported", "graph_nodes_json": [], "retry_policy": {}},
        schemas=[
            {"id": str(e1), "name": "Schema A", "definition_json": {"type": "object"}},
            {"id": str(e2), "name": "Schema B", "definition_json": {"type": "object"}},
        ],
    )
    a_result = MagicMock()
    a_result.scalar_one_or_none.return_value = existing_a
    b_result = MagicMock()
    b_result.scalar_one_or_none.return_value = existing_b
    all_result = MagicMock()
    all_result.scalars.return_value.all.return_value = [existing_a, existing_b]
    session = _FakeSession(a_result, all_result, b_result)
    result = await materialize_import(session, _ORG_ID, _ACCOUNT_ID, bundle)
    names = [call.kwargs["name"] for call in created["schema"].await_args_list]
    assert names == ["Schema A (imported)", "Schema B (imported)"]
    assert result["schema_count"] == 2


async def test_materialize_creates_schema_when_existing_has_no_version(monkeypatch: pytest.MonkeyPatch) -> None:
    created = _patch_crud(monkeypatch)
    existing = SimpleNamespace(id=uuid.uuid4(), name="Schema A")
    monkeypatch.setattr(mod, "_get_latest_published_version", AsyncMock(return_value=None))
    export_schema_id = uuid.uuid4()
    bundle = _make_bundle(
        pipeline={"name": "Imported", "graph_nodes_json": [], "retry_policy": {}},
        schemas=[{"id": str(export_schema_id), "name": "Schema A", "definition_json": {"type": "object"}}],
    )
    existing_result = MagicMock()
    existing_result.scalar_one_or_none.return_value = existing
    await materialize_import(_FakeSession(existing_result), _ORG_ID, _ACCOUNT_ID, bundle)
    assert created["schema"].await_args.kwargs["name"] == "Schema A"


async def test_materialize_leaves_unmapped_graph_node_refs_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    created = _patch_crud(monkeypatch)
    bundle = _make_bundle(
        pipeline={
            "name": "Imported",
            "retry_policy": {},
            "graph_nodes_json": [
                {
                    "agent_id": "unknown-agent",
                    "output_schema_id": "unknown-schema",
                    "connector_binding": {"instance_id": "unmapped-conn"},
                }
            ],
        }
    )
    await materialize_import(_FakeSession(), _ORG_ID, _ACCOUNT_ID, bundle)
    assert created["pipeline"].return_value.graph_nodes_json == [
        {
            "agent_id": "unknown-agent",
            "output_schema_id": "unknown-schema",
            "connector_binding": {"instance_id": "unmapped-conn"},
        }
    ]
