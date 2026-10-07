"""FAR-1530: ``modulo apply`` and the per-pipeline Paused execution state.

``run_enabled`` is opt-in (FAR-1182/FAR-1221 precedent) with a HARDER rule
than any sibling field: the ONLY value a config may express is ``false``
(pause). ``true`` — and an explicit ``null`` — are rejected at config LOAD
(``ApplyLoadError``), because resume is authority-bearing: a declarative
``true`` would silently fight a UI pause or revive a tripped spend circuit
breaker. The disable REASON is system-owned (the server stamps ``'operator'``
when a config-driven pause lands), never config-expressible.

Every behaviour here FAILS without its production change:

* ``test_true_is_rejected_at_config_load`` / ``test_null_is_rejected`` —
  without the ``_run_enabled_may_only_pause`` validator these load fine;
* ``test_declared_false_is_managed`` — without the ``manages_run_enabled``
  managed-view guard the key never reaches the drift hash;
* plan tests — without the response-side ``run_enabled`` field + view guard,
  drift reads wrong (always-unchanged or always-updated);
* execution tests — without the ``/pause`` POST in ``apply_pipelines`` no
  write happens at all.
"""

from __future__ import annotations

import json
import re

import httpx
import pytest
import respx

from modulo.cli.apply.executor import ApplyExecutor
from modulo.cli.apply.loader import ApplyLoadError, parse_apply_documents
from modulo.cli.apply.models import PipelineEntity
from tests.unit.cli.test_apply_pipeline import _mock_current_with_pipelines, _pipeline_item

_EXISTING_ID = "00000000-0000-0000-0000-0000000000aa"
_PAUSE_RE = re.compile(r"https://api\.test/api/v1/pipelines/[^/]+/pause")


def _config(run_enabled_line: str | None) -> str:
    extra = f"\n      {run_enabled_line}" if run_enabled_line is not None else ""
    return f"""
api_version: modulo.dev/v1
entities:
  pipelines:
    - name: sample
      description: Sample pipeline
      max_concurrent_runs: 3{extra}
"""


def _existing(run_enabled: bool | None) -> dict:
    row = dict(_pipeline_item("sample", _EXISTING_ID))
    if run_enabled is not None:
        row["run_enabled"] = run_enabled
        row["run_disabled_reason"] = None if run_enabled else "operator"
    return row


def _mock_pause_route() -> respx.Route:
    return respx.post(_PAUSE_RE).mock(return_value=httpx.Response(200, json=_pipeline_item("sample", _EXISTING_ID)))


def _run(config_text: str, *, dry_run: bool) -> dict:
    config = parse_apply_documents(config_text)
    with httpx.Client() as client:
        executor = ApplyExecutor("https://api.test", "key", client=client)
        return executor.run(config, dry_run=dry_run)


def _names(report: dict, status: str) -> list[str]:
    return [e["name"] for e in report[status] if e["kind"] == "pipeline"]


class TestModel:
    def test_undeclared_run_enabled_is_unmanaged(self) -> None:
        entity = PipelineEntity(name="p")
        assert entity.manages_run_enabled is False
        assert "run_enabled" not in entity.managed_view()

    def test_declared_false_is_managed(self) -> None:
        """Without the managed-view guard this FAILS: the key never enters the
        drift hash, so a running pipeline never shows as drift."""
        entity = PipelineEntity(name="p", run_enabled=False)
        assert entity.manages_run_enabled is True
        assert entity.managed_view()["run_enabled"] is False

    def test_true_is_rejected_at_config_load(self) -> None:
        """REGRESSION: a config can NEVER express a resume."""
        with pytest.raises(ValueError, match="run_enabled may only be declared as"):
            PipelineEntity(name="p", run_enabled=True)

    def test_null_is_rejected(self) -> None:
        """An explicit null is 'declared but meaningless' — the managed-view
        guard keys on declaration, so null would hash drift with nothing to
        write. Omitting the key is the way to leave the live state alone."""
        with pytest.raises(ValueError, match="run_enabled may only be declared as"):
            PipelineEntity(name="p", run_enabled=None)

    def test_true_rejected_through_the_yaml_loader(self) -> None:
        """The rejection fires at YAML LOAD (ApplyLoadError), not mid-apply."""
        with pytest.raises(ApplyLoadError, match="run_enabled"):
            parse_apply_documents(_config("run_enabled: true"))

    def test_false_parses_through_the_yaml_loader(self) -> None:
        config = parse_apply_documents(_config("run_enabled: false"))
        entity = config.entities.pipelines[0]
        assert entity.manages_run_enabled is True
        assert entity.run_enabled is False


class TestPlan:
    @respx.mock
    def test_declared_false_against_running_pipeline_is_updated(self) -> None:
        _mock_current_with_pipelines([_existing(True)])
        report = _run(_config("run_enabled: false"), dry_run=True)
        assert _names(report, "updated") == ["sample"]

    @respx.mock
    def test_declared_false_against_paused_pipeline_is_unchanged(self) -> None:
        _mock_current_with_pipelines([_existing(False)])
        report = _run(_config("run_enabled: false"), dry_run=True)
        assert _names(report, "unchanged") == ["sample"]

    @respx.mock
    def test_omitted_leaves_a_live_pause_untouched(self) -> None:
        """An operator- or breaker-caused pause must NEVER show as drift
        against a config that does not mention run_enabled."""
        _mock_current_with_pipelines([_existing(False)])
        report = _run(_config(None), dry_run=True)
        assert _names(report, "unchanged") == ["sample"]

    @respx.mock
    def test_omitted_against_running_pipeline_is_unchanged(self) -> None:
        _mock_current_with_pipelines([_existing(True)])
        report = _run(_config(None), dry_run=True)
        assert _names(report, "unchanged") == ["sample"]


class TestExecution:
    @respx.mock
    def test_create_declaring_false_pauses_the_new_pipeline(self) -> None:
        routes = _mock_current_with_pipelines([])
        pause_route = _mock_pause_route()

        report = _run(_config("run_enabled: false"), dry_run=False)

        assert not report["failed"]
        assert _names(report, "created") == ["sample"]
        # The create payload never carries the key - pause rides /pause. The
        # `.calls.last` access below fails loudly when no pause was recorded.
        create_payload = json.loads(routes["pipelines_post"].calls.last.request.content)
        assert "run_enabled" not in create_payload
        paused_url = pause_route.calls.last.request.url.path
        assert paused_url.endswith("/pause")

    @respx.mock
    def test_create_omitting_run_enabled_never_pauses(self) -> None:
        _mock_current_with_pipelines([])
        pause_route = _mock_pause_route()

        _run(_config(None), dry_run=False)

        assert not pause_route.called

    @respx.mock
    def test_update_declaring_false_pauses_after_the_patch(self) -> None:
        routes = _mock_current_with_pipelines([_existing(True)])
        pause_route = _mock_pause_route()

        report = _run(_config("run_enabled: false"), dry_run=False)

        assert _names(report, "updated") == ["sample"]
        # The PATCH never carries run_enabled (the /pause route is the single
        # transition surface — it owns the audit + 409 traps).
        patch_payload = json.loads(routes["pipeline_patch"].calls.last.request.content)
        assert "run_enabled" not in patch_payload
        assert pause_route.called

    @respx.mock
    def test_update_omitting_leaves_a_live_pause_untouched(self) -> None:
        """FAILS without the managed-view guard: the key would hash drift and
        the executor would re-apply writes against a paused pipeline."""
        routes = _mock_current_with_pipelines([_existing(False)])
        pause_route = _mock_pause_route()

        report = _run(_config(None), dry_run=False)

        assert _names(report, "unchanged") == ["sample"]
        assert not pause_route.called
        assert not routes["pipeline_patch"].called

    @respx.mock
    def test_update_already_paused_with_declared_false_makes_no_writes(self) -> None:
        """Declared-but-converged: desired == live, so the plan has no update
        entry and neither PATCH nor /pause fires."""
        routes = _mock_current_with_pipelines([_existing(False)])
        pause_route = _mock_pause_route()

        report = _run(_config("run_enabled: false"), dry_run=False)

        assert _names(report, "unchanged") == ["sample"]
        assert not pause_route.called
        assert not routes["pipeline_patch"].called
