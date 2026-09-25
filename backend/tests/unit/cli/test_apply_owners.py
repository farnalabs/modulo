"""FAR-1161: ``modulo apply`` manages the pipeline accountability owners.

Config pipelines reference owners by EMAIL; the executor resolves them to
account ids via the org member directory (GET /admin/users, fetched lazily
only when an owner email is declared). The two fields are managed
UNCONDITIONALLY with declarative omission semantics: a config that omits an
owner CLEARS a UI/API-set owner (the owner keys are always hashed and always
written on POST/PATCH). An email that does not resolve — or a directory the
credential cannot read — BLOCKS the pipeline at plan time with a specific
reason; it is never silently nulled.
"""

from __future__ import annotations

import json
import uuid

import httpx
import respx

from modulo.api.routes.pipelines import PipelineCreate, PipelineUpdate
from modulo.cli.apply.drift import has_drift
from modulo.cli.apply.executor import PAGE_SIZE, ApplyExecutor
from modulo.cli.apply.loader import parse_apply_documents
from tests.unit.cli.test_apply_pipeline import _mock_current_with_pipelines, _pipeline_item

_EXISTING_ID = "00000000-0000-0000-0000-0000000000aa"
_OWNER_ID = "00000000-0000-0000-0000-0000000000cc"
_OTHER_OWNER_ID = "00000000-0000-0000-0000-0000000000dd"
_ALICE = "alice@example.com"
_USERS_LIST_PARAMS = {"page": "1", "page_size": str(PAGE_SIZE)}


def _config(*, business_line: str | None = None, reliability_line: str | None = None) -> str:
    owner_lines = ""
    if business_line is not None:
        owner_lines += f"\n      {business_line}"
    if reliability_line is not None:
        owner_lines += f"\n      {reliability_line}"
    return f"""
api_version: modulo.dev/v1
entities:
  pipelines:
    - name: sample
      description: Sample pipeline
      max_concurrent_runs: 3{owner_lines}
"""


def _run(config_text: str, *, dry_run: bool, drift: bool = False) -> dict:
    config = parse_apply_documents(config_text)
    with httpx.Client() as client:
        executor = ApplyExecutor("https://api.test", "key", client=client)
        return executor.run(config, dry_run=dry_run, drift=drift)


def _names(report: dict, status: str) -> list[str]:
    return [e["name"] for e in report[status] if e["kind"] == "pipeline"]


def _mock_users(*, items: list[dict] | None = None, status_code: int = 200) -> respx.Route:
    body = {"items": items or [], "total": len(items or []), "page": 1, "page_size": PAGE_SIZE}
    return respx.get("https://api.test/api/v1/admin/users", params=_USERS_LIST_PARAMS).respond(
        status_code=status_code, json=body
    )


def _existing(*, owner_id: str | None = None) -> dict:
    row = dict(_pipeline_item("sample", _EXISTING_ID))
    if owner_id is not None:
        row["business_owner_id"] = owner_id
    return row


class TestPlan:
    """Plan-phase behaviour: hash parity, blocking, declarative omission."""

    @respx.mock
    def test_resolved_owner_matching_live_is_unchanged(self) -> None:
        _mock_current_with_pipelines([_existing(owner_id=_OWNER_ID)])
        _mock_users(items=[{"id": _OWNER_ID, "email": _ALICE}])
        report = _run(_config(business_line=f"business_owner_email: {_ALICE}"), dry_run=True)
        assert _names(report, "unchanged") == ["sample"]
        assert not report["updated"]
        assert not report["blocked"]

    @respx.mock
    def test_changed_owner_is_updated(self) -> None:
        _mock_current_with_pipelines([_existing(owner_id=_OTHER_OWNER_ID)])
        _mock_users(items=[{"id": _OWNER_ID, "email": _ALICE}])
        report = _run(_config(business_line=f"business_owner_email: {_ALICE}"), dry_run=True)
        assert _names(report, "updated") == ["sample"]

    @respx.mock
    def test_unresolvable_email_blocks_loudly(self) -> None:
        _mock_current_with_pipelines([_existing()])
        _mock_users(items=[{"id": _OWNER_ID, "email": "bob@example.com"}])
        report = _run(_config(business_line=f"business_owner_email: {_ALICE}"), dry_run=True)
        blocked = [e for e in report["blocked"] if e["kind"] == "pipeline"]
        assert [e["name"] for e in blocked] == ["sample"]
        assert "does not resolve" in blocked[0]["reason"]
        assert not report["updated"]
        assert not report["created"]

    @respx.mock
    def test_users_directory_failure_blocks_owner_pipelines(self) -> None:
        """A directory the credential cannot read blocks with a specific reason (never a silent null)."""
        _mock_current_with_pipelines([_existing()])
        _mock_users(status_code=403)
        report = _run(_config(business_line=f"business_owner_email: {_ALICE}"), dry_run=True)
        blocked = [e for e in report["blocked"] if e["kind"] == "pipeline"]
        assert [e["name"] for e in blocked] == ["sample"]
        assert "cannot resolve owner emails" in blocked[0]["reason"]

    @respx.mock
    def test_omitted_owner_clears_live_owner_is_updated(self) -> None:
        """Declarative omission: a config without owner keys plans an update against a live owner."""
        _mock_current_with_pipelines([_existing(owner_id=_OWNER_ID)])
        # No /admin/users route is registered: a config that declares no owner
        # email must never fetch the directory (respx fails on unmocked calls).
        report = _run(_config(), dry_run=True)
        assert _names(report, "updated") == ["sample"]


class TestExecution:
    """Apply-phase payloads: resolved ids on POST/PATCH, explicit null on omission."""

    @respx.mock
    def test_create_posts_resolved_owner_ids(self) -> None:
        routes = _mock_current_with_pipelines([])
        _mock_users(items=[{"id": _OWNER_ID, "email": _ALICE}])
        report = _run(_config(business_line=f"business_owner_email: {_ALICE}"), dry_run=False)
        assert not report["failed"]
        payload = json.loads(routes["pipelines_post"].calls.last.request.content)
        assert payload["business_owner_id"] == _OWNER_ID
        assert payload["reliability_owner_id"] is None
        created = PipelineCreate.model_validate(payload)
        assert created.business_owner_id == uuid.UUID(_OWNER_ID)
        assert created.reliability_owner_id is None

    @respx.mock
    def test_update_patches_resolved_owner_id(self) -> None:
        routes = _mock_current_with_pipelines([_existing()])
        _mock_users(items=[{"id": _OWNER_ID, "email": _ALICE}])
        report = _run(_config(business_line=f"business_owner_email: {_ALICE}"), dry_run=False)
        assert _names(report, "updated") == ["sample"]
        payload = json.loads(routes["pipeline_patch"].calls.last.request.content)
        assert payload["business_owner_id"] == _OWNER_ID
        assert payload["reliability_owner_id"] is None
        update = PipelineUpdate.model_validate(payload)
        assert update.business_owner_id == uuid.UUID(_OWNER_ID)

    @respx.mock
    def test_omitted_owner_sends_explicit_null(self) -> None:
        """Declarative omission: the PATCH carries an explicit null that clears the live owner."""
        routes = _mock_current_with_pipelines([_existing(owner_id=_OWNER_ID)])
        report = _run(_config(), dry_run=False)
        assert _names(report, "updated") == ["sample"]
        payload = json.loads(routes["pipeline_patch"].calls.last.request.content)
        assert "business_owner_id" in payload
        assert payload["business_owner_id"] is None
        assert "reliability_owner_id" in payload
        assert payload["reliability_owner_id"] is None
        PipelineUpdate.model_validate(payload)


class TestDiffMode:
    """``--diff`` (drift mode) detects owner changes and never writes."""

    @respx.mock
    def test_diff_detects_owner_change(self) -> None:
        routes = _mock_current_with_pipelines([_existing(owner_id=_OTHER_OWNER_ID)])
        _mock_users(items=[{"id": _OWNER_ID, "email": _ALICE}])
        report = _run(_config(business_line=f"business_owner_email: {_ALICE}"), dry_run=False, drift=True)
        assert report["mode"] == "drift"
        assert _names(report, "updated") == ["sample"]
        assert has_drift(report)
        assert routes["pipeline_patch"].call_count == 0
        assert routes["pipelines_post"].call_count == 0

    @respx.mock
    def test_diff_no_drift_when_owner_matches(self) -> None:
        routes = _mock_current_with_pipelines([_existing(owner_id=_OWNER_ID)])
        _mock_users(items=[{"id": _OWNER_ID, "email": _ALICE}])
        report = _run(_config(business_line=f"business_owner_email: {_ALICE}"), dry_run=False, drift=True)
        assert _names(report, "unchanged") == ["sample"]
        assert not has_drift(report)
        assert routes["pipeline_patch"].call_count == 0

    @respx.mock
    def test_diff_detects_cleared_owner(self) -> None:
        routes = _mock_current_with_pipelines([_existing(owner_id=_OWNER_ID)])
        report = _run(_config(), dry_run=False, drift=True)
        assert _names(report, "updated") == ["sample"]
        assert has_drift(report)
        assert routes["pipeline_patch"].call_count == 0
