"""FAR-1182: ``modulo apply`` manages the pipeline spend circuit breaker.

``circuit_breaker_threshold`` is opt-in per pipeline: managed (hashed and
written) only when the key is declared, so configs that never mention it do
not clobber a UI/API-set threshold. Payloads round-trip through the REAL API
models.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx
from pydantic import ValidationError

from modulo.api.routes.pipelines import PipelineCreate, PipelineUpdate
from modulo.cli.apply.executor import ApplyExecutor
from modulo.cli.apply.loader import parse_apply_documents
from modulo.cli.apply.models import PipelineEntity, quantize_circuit_breaker_threshold
from tests.unit.cli.test_apply_pipeline import _mock_current_with_pipelines, _pipeline_item

_EXISTING_ID = "00000000-0000-0000-0000-0000000000aa"


def _config(threshold_line: str | None) -> str:
    extra = f"\n      {threshold_line}" if threshold_line is not None else ""
    return f"""
api_version: modulo.dev/v1
entities:
  pipelines:
    - name: sample
      description: Sample pipeline
      max_concurrent_runs: 3{extra}
"""


def _existing(threshold: float | None) -> dict:
    row = dict(_pipeline_item("sample", _EXISTING_ID))
    row["circuit_breaker_threshold"] = threshold
    row["circuit_breaker_tripped"] = False
    return row


def _run(config_text: str, *, dry_run: bool) -> dict:
    config = parse_apply_documents(config_text)
    with httpx.Client() as client:
        executor = ApplyExecutor("https://api.test", "key", client=client)
        return executor.run(config, dry_run=dry_run)


def _names(report: dict, status: str) -> list[str]:
    return [e["name"] for e in report[status] if e["kind"] == "pipeline"]


class TestModel:
    def test_undeclared_threshold_is_unmanaged(self) -> None:
        entity = PipelineEntity(name="p")
        assert entity.manages_circuit_breaker is False
        assert "circuit_breaker_threshold" not in entity.managed_view()

    def test_declared_null_is_managed(self) -> None:
        entity = PipelineEntity(name="p", circuit_breaker_threshold=None)
        assert entity.manages_circuit_breaker is True
        assert entity.managed_view()["circuit_breaker_threshold"] is None

    def test_declared_value_is_quantized_in_view(self) -> None:
        entity = PipelineEntity(name="p", circuit_breaker_threshold=12.3456789)
        assert entity.managed_view()["circuit_breaker_threshold"] == pytest.approx(12.345679, abs=1e-9)

    @pytest.mark.parametrize("bad", [0, -1, 0.0000001, 1e9])
    def test_invalid_threshold_rejected(self, bad: float) -> None:
        with pytest.raises(ValidationError, match="circuit_breaker_threshold"):
            PipelineEntity(name="p", circuit_breaker_threshold=bad)

    def test_quantize_passthrough_none(self) -> None:
        assert quantize_circuit_breaker_threshold(None) is None


class TestPlan:
    @respx.mock
    def test_undeclared_threshold_never_drifts(self) -> None:
        _mock_current_with_pipelines([_existing(50.0)])
        report = _run(_config(None), dry_run=True)
        assert _names(report, "unchanged") == ["sample"]

    @respx.mock
    def test_matching_threshold_is_unchanged(self) -> None:
        _mock_current_with_pipelines([_existing(50.0)])
        report = _run(_config("circuit_breaker_threshold: 50"), dry_run=True)
        assert _names(report, "unchanged") == ["sample"]

    @respx.mock
    def test_different_threshold_is_updated(self) -> None:
        _mock_current_with_pipelines([_existing(50.0)])
        report = _run(_config("circuit_breaker_threshold: 75.5"), dry_run=True)
        assert _names(report, "updated") == ["sample"]

    @respx.mock
    def test_declared_null_against_set_threshold_is_updated(self) -> None:
        _mock_current_with_pipelines([_existing(50.0)])
        report = _run(_config("circuit_breaker_threshold: null"), dry_run=True)
        assert _names(report, "updated") == ["sample"]


class TestExecution:
    @respx.mock
    def test_create_sends_declared_threshold(self) -> None:
        routes = _mock_current_with_pipelines([])
        report = _run(_config("circuit_breaker_threshold: 20"), dry_run=False)
        assert not report["failed"]
        payload = json.loads(routes["pipelines_post"].calls.last.request.content)
        assert payload["circuit_breaker_threshold"] == 20
        assert PipelineCreate.model_validate(payload).circuit_breaker_threshold == 20.0

    @respx.mock
    def test_create_omits_undeclared_threshold(self) -> None:
        routes = _mock_current_with_pipelines([])
        _run(_config(None), dry_run=False)
        payload = json.loads(routes["pipelines_post"].calls.last.request.content)
        assert "circuit_breaker_threshold" not in payload

    @respx.mock
    def test_update_patches_declared_threshold(self) -> None:
        routes = _mock_current_with_pipelines([_existing(50.0)])
        report = _run(_config("circuit_breaker_threshold: 75.5"), dry_run=False)
        assert _names(report, "updated") == ["sample"]
        payload = json.loads(routes["pipeline_patch"].calls.last.request.content)
        assert PipelineUpdate.model_validate(payload).circuit_breaker_threshold == 75.5

    @respx.mock
    def test_update_declared_null_clears_threshold(self) -> None:
        routes = _mock_current_with_pipelines([_existing(50.0)])
        _run(_config("circuit_breaker_threshold: null"), dry_run=False)
        payload = json.loads(routes["pipeline_patch"].calls.last.request.content)
        assert "circuit_breaker_threshold" in payload
        assert payload["circuit_breaker_threshold"] is None

    @respx.mock
    def test_update_of_other_field_leaves_undeclared_threshold_alone(self) -> None:
        existing = _existing(50.0)
        existing["max_concurrent_runs"] = 9
        routes = _mock_current_with_pipelines([existing])
        _run(_config(None), dry_run=False)
        payload = json.loads(routes["pipeline_patch"].calls.last.request.content)
        assert "circuit_breaker_threshold" not in payload


class TestServerSidePermissionRefusal:
    """FAR-1184: apply surfaces the REST 403 (raise/clear needs cost.manage).

    ``modulo apply`` authenticates as an API key and sends PATCH/POST to the
    REST API — the shared raise/clear gate runs SERVER-SIDE, and the CLI
    reports the 403 as a failed entity with the server's detail. The CLI
    cannot determine ``cost.manage`` locally (it has only the key, no
    permission map), so it fails closed by trusting the server's verdict.
    """

    _DENIAL_DETAIL = "Raising or clearing circuit_breaker_threshold requires the 'cost.manage' permission (org admin)"

    @respx.mock
    def test_update_raise_refused_by_server_fails_entity(self) -> None:
        routes = _mock_current_with_pipelines([_existing(50.0)])
        routes["pipeline_patch"].mock(return_value=httpx.Response(403, json={"detail": self._DENIAL_DETAIL}))

        report = _run(_config("circuit_breaker_threshold: 75.5"), dry_run=False)

        assert not _names(report, "updated")
        failed = [e for e in report["failed"] if e["kind"] == "pipeline"]
        assert failed
        assert failed[0]["name"] == "sample"
        assert "403" in failed[0]["error"]
        assert "cost.manage" in failed[0]["error"]

    @respx.mock
    def test_update_clear_refused_by_server_fails_entity(self) -> None:
        routes = _mock_current_with_pipelines([_existing(50.0)])
        routes["pipeline_patch"].mock(return_value=httpx.Response(403, json={"detail": self._DENIAL_DETAIL}))

        report = _run(_config("circuit_breaker_threshold: null"), dry_run=False)

        assert not _names(report, "updated")
        failed = [e for e in report["failed"] if e["kind"] == "pipeline"]
        assert failed
        assert failed[0]["name"] == "sample"
        assert "cost.manage" in failed[0]["error"]

    @respx.mock
    def test_update_lower_still_applies(self) -> None:
        # A lowering change gets a 200 from the server (operator-key path) —
        # apply must record it as updated, not failed.
        _mock_current_with_pipelines([_existing(75.5)])

        report = _run(_config("circuit_breaker_threshold: 50"), dry_run=False)

        assert _names(report, "updated") == ["sample"]
        assert not report["failed"]
