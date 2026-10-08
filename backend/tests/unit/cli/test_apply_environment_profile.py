"""FAR-1599: ``modulo apply`` and the per-pipeline environment-profile binding.

``environment_profile_id`` follows the FAR-1182/FAR-1221 opt-in rule: managed
ONLY when declared — an omitted key is neither hashed nor written (a UI/API-set
binding survives a config that never mentions it), a declared id binds, and a
declared ``null`` CLEARS the binding back to the default route.

Every behaviour here FAILS without its production change:

* ``test_declared_id_is_managed`` / ``test_declared_null_is_managed`` —
  without the ``manages_environment_profile`` managed-view guard the key
  never reaches the drift hash;
* plan tests — without the response-side key + the ``_pipeline_current_view``
  coercion, drift reads wrong (always-unchanged or always-updated);
* execution tests — without the ``patch_payload`` key in ``apply_pipelines``
  no write happens at all, and without the force-PATCH condition a CREATED
  pipeline never binds (``PipelineCreate`` cannot carry the field).
"""

from __future__ import annotations

import json
import uuid

import httpx
import pytest
import respx

from modulo.cli.apply.executor import ApplyExecutor
from modulo.cli.apply.loader import ApplyLoadError, parse_apply_documents
from modulo.cli.apply.models import PipelineEntity
from tests.unit.cli.test_apply_pipeline import _mock_current_with_pipelines, _pipeline_item

_EXISTING_ID = "00000000-0000-0000-0000-0000000000aa"
_PROFILE_ID = "00000000-0000-0000-0000-0000000000e1"
_OTHER_PROFILE_ID = "00000000-0000-0000-0000-0000000000e2"


def _config(environment_profile_line: str | None) -> str:
    extra = f"\n      {environment_profile_line}" if environment_profile_line is not None else ""
    return f"""
api_version: modulo.dev/v1
entities:
  pipelines:
    - name: sample
      description: Sample pipeline
      max_concurrent_runs: 3{extra}
"""


def _existing(environment_profile_id: str | None) -> dict:
    row = dict(_pipeline_item("sample", _EXISTING_ID))
    row["environment_profile_id"] = environment_profile_id
    return row


def _run(config_text: str, *, dry_run: bool) -> dict:
    config = parse_apply_documents(config_text)
    with httpx.Client() as client:
        executor = ApplyExecutor("https://api.test", "key", client=client)
        return executor.run(config, dry_run=dry_run)


def _names(report: dict, status: str) -> list[str]:
    return [e["name"] for e in report[status] if e["kind"] == "pipeline"]


class TestModel:
    def test_undeclared_binding_is_unmanaged(self) -> None:
        entity = PipelineEntity(name="p")
        assert entity.manages_environment_profile is False
        assert "environment_profile_id" not in entity.managed_view()

    def test_declared_id_is_managed(self) -> None:
        """Without the managed-view guard this FAILS: the key never enters
        the drift hash, so a wrong binding never shows as drift."""
        entity = PipelineEntity(name="p", environment_profile_id=_PROFILE_ID)
        assert entity.manages_environment_profile is True
        # Hashes as a UUID STRING (the id-space the API returns).
        assert entity.managed_view()["environment_profile_id"] == _PROFILE_ID

    def test_declared_null_is_managed(self) -> None:
        """A declared null CLEARS — it must be hashed (drift is detectable),
        unlike an omitted key, which is never in the view at all."""
        entity = PipelineEntity(name="p", environment_profile_id=None)
        assert entity.manages_environment_profile is True
        assert entity.managed_view()["environment_profile_id"] is None

    def test_invalid_uuid_is_rejected_at_config_load(self) -> None:
        with pytest.raises(ApplyLoadError, match="environment_profile_id"):
            parse_apply_documents(_config("environment_profile_id: not-a-uuid"))

    def test_declared_id_parses_through_the_yaml_loader(self) -> None:
        config = parse_apply_documents(_config(f"environment_profile_id: {_PROFILE_ID}"))
        entity = config.entities.pipelines[0]
        assert entity.manages_environment_profile is True
        assert entity.environment_profile_id == uuid.UUID(_PROFILE_ID)


class TestPlan:
    @respx.mock
    def test_declared_id_matching_the_live_binding_is_unchanged(self) -> None:
        _mock_current_with_pipelines([_existing(_PROFILE_ID)])
        report = _run(_config(f"environment_profile_id: {_PROFILE_ID}"), dry_run=True)
        assert _names(report, "unchanged") == ["sample"]

    @respx.mock
    def test_declared_id_differing_from_the_live_binding_is_updated(self) -> None:
        _mock_current_with_pipelines([_existing(_OTHER_PROFILE_ID)])
        report = _run(_config(f"environment_profile_id: {_PROFILE_ID}"), dry_run=True)
        assert _names(report, "updated") == ["sample"]

    @respx.mock
    def test_declared_id_against_an_unbound_pipeline_is_updated(self) -> None:
        _mock_current_with_pipelines([_existing(None)])
        report = _run(_config(f"environment_profile_id: {_PROFILE_ID}"), dry_run=True)
        assert _names(report, "updated") == ["sample"]

    @respx.mock
    def test_declared_null_against_a_bound_pipeline_is_updated(self) -> None:
        _mock_current_with_pipelines([_existing(_PROFILE_ID)])
        report = _run(_config("environment_profile_id: null"), dry_run=True)
        assert _names(report, "updated") == ["sample"]

    @respx.mock
    def test_omitted_never_reports_drift_against_a_ui_set_binding(self) -> None:
        """An operator-set binding must NOT show as drift against a config
        that does not mention it."""
        _mock_current_with_pipelines([_existing(_PROFILE_ID)])
        report = _run(_config(None), dry_run=True)
        assert _names(report, "unchanged") == ["sample"]

    @respx.mock
    def test_omitted_against_an_unbound_pipeline_is_unchanged(self) -> None:
        _mock_current_with_pipelines([_existing(None)])
        report = _run(_config(None), dry_run=True)
        assert _names(report, "unchanged") == ["sample"]


class TestExecution:
    @respx.mock
    def test_update_sends_a_declared_id(self) -> None:
        routes = _mock_current_with_pipelines([_existing(None)])
        report = _run(_config(f"environment_profile_id: {_PROFILE_ID}"), dry_run=False)

        assert _names(report, "updated") == ["sample"]
        patch_payload = json.loads(routes["pipeline_patch"].calls.last.request.content)
        assert patch_payload["environment_profile_id"] == _PROFILE_ID

    @respx.mock
    def test_update_sends_a_declared_null_to_clear(self) -> None:
        routes = _mock_current_with_pipelines([_existing(_PROFILE_ID)])
        report = _run(_config("environment_profile_id: null"), dry_run=False)

        assert _names(report, "updated") == ["sample"]
        patch_payload = json.loads(routes["pipeline_patch"].calls.last.request.content)
        assert "environment_profile_id" in patch_payload
        assert patch_payload["environment_profile_id"] is None

    @respx.mock
    def test_update_omitting_the_key_never_sends_it(self) -> None:
        """FAILS without the managed-view guard: the key would hash drift and
        every apply would overwrite a UI/API-set binding."""
        routes = _mock_current_with_pipelines([_existing(_PROFILE_ID)])
        report = _run(_config(None), dry_run=False)

        assert _names(report, "unchanged") == ["sample"]
        assert not routes["pipeline_patch"].called

    @respx.mock
    def test_create_binds_through_the_follow_up_patch_not_the_post(self) -> None:
        """``PipelineCreate`` cannot carry the field, so a CREATED pipeline
        binds on the PATCH that follows the POST (force-sent even with no
        graph). Without the force-PATCH condition no bind ever happens."""
        routes = _mock_current_with_pipelines([])
        report = _run(_config(f"environment_profile_id: {_PROFILE_ID}"), dry_run=False)

        assert not report["failed"]
        assert _names(report, "created") == ["sample"]
        create_payload = json.loads(routes["pipelines_post"].calls.last.request.content)
        assert "environment_profile_id" not in create_payload
        patch_payload = json.loads(routes["pipeline_patch"].calls.last.request.content)
        assert patch_payload["environment_profile_id"] == _PROFILE_ID

    @respx.mock
    def test_create_omitting_the_key_never_patches_to_bind(self) -> None:
        routes = _mock_current_with_pipelines([])
        report = _run(_config(None), dry_run=False)

        assert _names(report, "created") == ["sample"]
        assert not routes["pipeline_patch"].called
