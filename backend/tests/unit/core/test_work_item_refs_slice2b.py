"""Unit tests for FAR-794 slice 2b: the unified work_item_refs cap, the
finalize merge/drop semantics, node output source stamping, sandbox node
input injection, and the webhook required-refs 422 mapping.

No DB: the merge/stamp/injection paths are pure functions over dicts; the
webhook mapping test patches ``create_run`` at the trigger_engine module
boundary and asserts the ``HTTPException(422)`` envelope.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from modulo.core.cost_controller.finalize import (
    _collect_node_emission_sources,
    _confirm_reported_refs,
    _merge_effective_refs,
    _resolve_effective_refs,
    _stamp_node_sources,
)
from modulo.core.lifecycle_map.self_report import validate_and_normalise_reported_refs
from modulo.core.pipeline_engine.node_runner import (
    _node_declares_work_item_refs,
    _node_input_work_item_refs,
)
from modulo.core.trigger_engine import (
    TriggerEngine,
    _RateLimitState,
    _WebhookDelivery,
)
from modulo.db.lifecycle_refs import REPORTED_SOURCE, WORK_ITEM_REFS_KEY, validate_ref_entry
from modulo.db.models.trigger import Trigger
from modulo.settings import work_item_refs_cap

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _ref(kind: str, ref: str, source: str) -> dict[str, object]:
    return {"kind": kind, "ref": ref, "source": source}


_CAP = "modulo.core.cost_controller.finalize.work_item_refs_cap"


# ---------------------------------------------------------------------------
# _merge_effective_refs
# ---------------------------------------------------------------------------


class TestMergeEffectiveRefs:
    def test_within_cap_returns_typed_union_in_order(self) -> None:
        refs = [_ref("jira", "X-1", "caller"), _ref("github_pr", "#2", "agent")]
        kept, dropped = _merge_effective_refs(refs, [], org_id=uuid.uuid4())
        assert [(r["kind"], r["ref"]) for r in kept] == [("jira", "X-1"), ("github_pr", "#2")]
        assert dropped == 0

    def test_dedupes_wire_and_self_report_by_canonical_id(self) -> None:
        wire = [_ref("jira", "x-1", "caller")]
        claimed = [_ref("jira", " X-1 ", "agent")]
        kept, dropped = _merge_effective_refs(wire, claimed, org_id=uuid.uuid4())
        assert len(kept) == 1
        assert dropped == 0

    def test_cap_drops_agent_before_derived_before_caller(self) -> None:
        refs = [
            _ref("agent-only", "a1", "agent"),
            _ref("wire", "w1", "caller"),
            _ref("claim-a", "a2", "agent"),
            _ref("claim-d", "d1", "derived"),
        ]
        with patch(_CAP, lambda: 2):
            kept, dropped = _merge_effective_refs(refs, [], org_id=uuid.uuid4())
        assert [(r["kind"], r["ref"]) for r in kept] == [
            ("wire", "w1"),
            ("claim-d", "d1"),
        ]
        assert dropped == 2
        survivor_kinds = [str(r["kind"]) for r in kept]
        assert "agent-only" not in survivor_kinds
        assert "claim-a" not in survivor_kinds

    def test_caller_is_never_dropped_while_lower_rank_survives(self) -> None:
        refs = [_ref("a", "1", "agent"), _ref("w", "2", "caller")]
        with patch(_CAP, lambda: 1):
            kept, dropped = _merge_effective_refs(refs, [], org_id=uuid.uuid4())
        assert len(kept) == 1
        assert kept[0]["source"] == "caller"
        assert dropped == 1

    def test_cap_drop_events_are_counted(self) -> None:
        events: list[tuple[object, ...]] = []
        refs = [_ref("a", "1", "agent"), _ref("b", "2", "agent"), _ref("w", "3", "caller")]
        with (
            patch(_CAP, lambda: 1),
            patch(
                "modulo.core.cost_controller.finalize.notify_refs_event",
                side_effect=lambda name, *a, **kw: events.append((name, kw.get("ref"))),
            ),
        ):
            _merge_effective_refs(refs, [], org_id=uuid.uuid4())
        assert len(events) == 2
        assert all(e[0] == "refs_cap_dropped" for e in events)
        assert {e[1] for e in events} == {"1", "2"}


# ---------------------------------------------------------------------------
# node output source stamping
# ---------------------------------------------------------------------------


class TestNodeEmissionSources:
    def test_first_emitting_node_wins(self) -> None:
        merged = {
            "node-a": {"output": {"work_item_refs": [_ref("jira", "X-1", "agent")]}},
            "node-b": {"output": {"work_item_refs": [_ref("jira", "X-1", "agent")]}},
        }
        assert _collect_node_emission_sources(merged) == [
            ("jira", "X-1", "node-a"),
            ("jira", "X-1", "node-b"),
        ]

    def test_stamp_marks_all_entries_of_the_canonical_ref(self) -> None:
        entries = [
            _ref("jira", "X-1", "agent"),
            _ref("jira", "X-1", "agent"),
            _ref("github_pr", "#9", "caller"),
        ]
        _stamp_node_sources(entries, [("jira", "X-1", "node-a"), ("jira", "X-1", "node-b")])
        assert entries[0]["source_node_id"] == "node-a"
        assert entries[1]["source_node_id"] == "node-b"
        assert "source_node_id" not in entries[2]  # caller refs are never stamped

    def test_resolve_effective_refs_confirmed_reported_gets_node_stamp(self) -> None:
        run = MagicMock()
        run.organisation_id = uuid.uuid4()
        run.work_item_refs = [_ref("jira", "X-1", "caller")]
        merged = {"node-a": {"output": {"work_item_refs": [_ref("jira", "X-9", "agent")]}}}
        confirmed = [_ref("jira", "X-9", "reported")]
        with (
            patch(
                "modulo.core.cost_controller.finalize._confirm_reported_refs",
                new=AsyncMock(return_value=confirmed),
            ),
            patch(_CAP, lambda: 100),
        ):
            import asyncio

            resolution = asyncio.run(_resolve_effective_refs(MagicMock(), run, merged))
        assert len(resolution.confirmed) == 1
        assert resolution.cap_dropped == 0
        by_ref = {str(r["ref"]): r for r in resolution.effective}
        # FAR-794 persisted invariant: the stored source is ``agent`` — the
        # legacy ``reported`` marker is normalised at the confirm boundary,
        # never persisted.
        assert by_ref["X-9"]["source"] == "agent"
        assert by_ref["X-9"]["source_node_id"] == "node-a"
        assert by_ref["X-1"]["source"] == "caller"  # wire untouched

    def test_resolve_effective_refs_keeps_unconfirmed_reports_unmerged(self) -> None:
        run = MagicMock()
        run.organisation_id = uuid.uuid4()
        run.work_item_refs = []
        merged = {"node-a": {"output": {"work_item_refs": [_ref("jira", "X-9", "agent")]}}}
        with patch(
            "modulo.core.cost_controller.finalize._confirm_reported_refs",
            new=AsyncMock(return_value=[]),
        ):
            import asyncio

            resolution = asyncio.run(_resolve_effective_refs(MagicMock(), run, merged))
        assert not resolution.confirmed  # advisory: unconfirmed claims never merge
        assert len(resolution.reported) == 1
        assert not resolution.effective


# ---------------------------------------------------------------------------
# confirm-gate normalisation — the persisted-source invariant (FAR-794)
# ---------------------------------------------------------------------------


def _session_with_journey(found: bool) -> MagicMock:
    """A mock AsyncSession whose per-entry EXISTS probe yields a row id."""
    row = MagicMock()
    row.scalar_one_or_none = lambda: uuid.uuid4() if found else None
    session = MagicMock()
    session.execute = AsyncMock(return_value=row)
    return session


class TestConfirmReportedRefsNormalisation:
    async def test_the_confirm_gate_still_matches_legacy_reported_claims(self) -> None:
        # (b) the gate itself is untouched: the EXISTS match keys on
        # (org, kind, ref) — a legacy ``reported`` claim that has a journey
        # row still confirms. The gate output keeps the raw canonicalised
        # claim; the normalisation happens at the merge boundary.
        entries = [_ref("jira", "FAR-1", REPORTED_SOURCE)]
        confirmed = await _confirm_reported_refs(_session_with_journey(True), uuid.uuid4(), entries)
        assert confirmed == [{"kind": "jira", "ref": "FAR-1", "source": REPORTED_SOURCE}]

    async def test_unmatched_claim_is_dropped_and_never_normalised(self) -> None:
        entries = [_ref("jira", "GHOST", REPORTED_SOURCE)]
        confirmed = await _confirm_reported_refs(_session_with_journey(False), uuid.uuid4(), entries)
        assert not confirmed
        # The unconfirmed claim keeps its raw shape (it is never persisted).
        assert entries == [{"kind": "jira", "ref": "GHOST", "source": REPORTED_SOURCE}]

    def test_stored_effective_list_never_carries_reported(self) -> None:
        # (a) the persisted invariant end-to-end through the resolver: a
        # confirmed legacy ``reported`` claim is normalised to ``agent``
        # BEFORE anything reaches ``run.work_item_refs``.
        run = MagicMock()
        run.organisation_id = uuid.uuid4()
        run.work_item_refs = []
        merged = {"node-a": {"output": {"work_item_refs": [_ref("jira", "X-9", "agent")]}}}
        confirmed = [_ref("jira", "X-9", REPORTED_SOURCE)]
        with (
            patch(
                "modulo.core.cost_controller.finalize._confirm_reported_refs",
                new=AsyncMock(return_value=confirmed),
            ),
            patch(_CAP, lambda: 100),
        ):
            import asyncio

            resolution = asyncio.run(_resolve_effective_refs(MagicMock(), run, merged))
        assert resolution.effective
        assert all(e.get("source") != REPORTED_SOURCE for e in resolution.effective)
        assert resolution.confirmed[0]["source"] == "agent"

    def test_reported_is_accepted_on_read_paths(self) -> None:
        # (c) ``reported`` stays accepted on READ paths — the default
        # ``_READ_ACCEPTED_SOURCES`` vocabulary keeps tolerating
        # pre-normalisation rows; only the intake/merge boundary normalises.
        entry = validate_ref_entry({"kind": "github_pr", "ref": "#456", "source": "reported"})
        assert entry == {"kind": "github_pr", "ref": "456", "source": "reported"}


# ---------------------------------------------------------------------------
# sandbox node input injection
# ---------------------------------------------------------------------------


class TestNodeInputInjection:
    def test_declares_true_only_for_work_item_refs_property(self) -> None:
        assert _node_declares_work_item_refs(
            {"input_schema_json": {"properties": {WORK_ITEM_REFS_KEY: {"type": "array"}}}}
        )
        assert not _node_declares_work_item_refs({"input_schema_json": {"properties": {"other": {"type": "string"}}}})
        assert not _node_declares_work_item_refs({})
        assert not _node_declares_work_item_refs({"input_schema_json": "not-a-dict"})

    def test_read_only_clone_and_cap_keeps_callers(self) -> None:
        run_context = {
            "input": {
                WORK_ITEM_REFS_KEY: [
                    _ref("jira", "X-1", "caller"),
                    _ref("slack", "n/a", "agent"),
                ]
            }
        }
        with patch("modulo.core.pipeline_engine.node_runner.work_item_refs_cap", lambda: 1):
            refs = _node_input_work_item_refs("node-1", run_context)
        assert len(refs) == 1
        assert refs[0]["source"] == "caller"
        # The caller's context is untouched (read-only view).
        assert len(run_context["input"][WORK_ITEM_REFS_KEY]) == 2

    def test_malformed_entries_are_counted_not_fatal(self) -> None:
        run_context = {"input": {WORK_ITEM_REFS_KEY: ["garbage", _ref("jira", "X-1", "caller")]}}
        refs = _node_input_work_item_refs("node-1", run_context)
        assert [r["ref"] for r in refs] == ["X-1"]

    def test_read_failure_is_fail_open(self) -> None:
        refs = _node_input_work_item_refs("node-1", {"input": None})
        assert refs == []


# ---------------------------------------------------------------------------
# unified cap setting
# ---------------------------------------------------------------------------


class TestWorkItemRefsCapSetting:
    @pytest.fixture(autouse=True)
    def clear_cap_cache(self) -> None:
        from modulo.settings import get_settings

        work_item_refs_cap.cache_clear()
        get_settings.cache_clear()
        yield
        work_item_refs_cap.cache_clear()
        get_settings.cache_clear()

    def test_default_is_100(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MODULO_WORK_ITEM_REFS_CAP", raising=False)
        assert work_item_refs_cap() == 100

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("25", 25), ("0", 1), ("999999", 100000), ("abc", 100), ("-5", 1)],
    )
    def test_clamps_out_of_range_and_garbage(self, monkeypatch: pytest.MonkeyPatch, raw: str, expected: int) -> None:
        monkeypatch.setenv("MODULO_WORK_ITEM_REFS_CAP", raw)
        assert work_item_refs_cap() == expected

    def test_self_report_normaliser_uses_explicit_max_refs(self) -> None:
        entries = [_ref("jira", f"X-{i}", "agent") for i in range(5)]
        valid, counters = validate_and_normalise_reported_refs(entries, max_refs=2)
        assert len(valid) == 2
        assert counters["capped"] == 3
        assert counters["valid"] == 2


# ---------------------------------------------------------------------------
# webhook required-refs 422 mapping
# ---------------------------------------------------------------------------


def _delivery() -> _WebhookDelivery:
    trigger = MagicMock(spec=Trigger)
    trigger.id = uuid.uuid4()
    trigger.pipeline_id = uuid.uuid4()
    trigger.organisation_id = uuid.uuid4()
    trigger.config_json = {}
    return _WebhookDelivery(
        org_id=trigger.organisation_id,
        trigger=trigger,
        raw_body=b"{}",
        raw_payload={},
        snapshot_id=uuid.uuid4(),
    )


class TestWebhookRequiredRefs422:
    async def test_work_item_refs_required_maps_to_http_422(self) -> None:
        from modulo.db.crud.run import WorkItemRefsRequiredError

        engine = TriggerEngine()
        delivery = _delivery()
        session = AsyncMock()
        engine._log_event = AsyncMock(return_value=MagicMock(id=uuid.uuid4()))  # type: ignore[method-assign]

        with (
            patch(
                "modulo.core.trigger_engine.create_run",
                new=AsyncMock(side_effect=WorkItemRefsRequiredError("work_item_refs required")),
            ),
            patch(
                "modulo.core.trigger_engine.evaluate_backpressure",
                new=AsyncMock(return_value=(False, None)),
            ),
            pytest.raises(HTTPException) as exc_info,
        ):
            await engine._create_webhook_run(
                session,
                delivery,
                input_payload={},
                payload_hash="h",
                rate_limit=_RateLimitState(key=None),
            )

        assert exc_info.value.status_code == 422
        detail = exc_info.value.detail
        assert detail["error"] == "work_item_refs_required"
        # A rejection event is recorded BEFORE the raise so the delivery is
        # auditable even though the run was never created.
        assert engine._log_event.await_count == 1
        assert engine._log_event.await_args.kwargs["result"] == "required_refs_missing"
