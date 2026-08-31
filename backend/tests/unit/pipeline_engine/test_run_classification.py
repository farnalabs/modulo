"""Unit tests for FAR-189: run-outcome classification persisted at terminalization.

Covers the pure classifier decision table, the pr_url validity + extraction
matrix (node returns via the node-return accessors AND the FAR-188
raw_output_markers column), and the persistence hook wired into the shared
terminal write (``db.crud.run.update_run_status`` / the fenced variant /
``request_cancellation``) — including failure injection (classifier/persist
failures never block terminalization; an ``unclassified`` marker is written),
idempotency (UNIQUE(run_id)), and re-terminalization refresh (upsert).
"""

import builtins
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC
from typing import Any, Self, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import StaticPool, Table, event, select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import Session as SASession

from modulo.core.pipeline_engine.classify import (
    REASON_BUDGET_EXCEEDED,
    REASON_CANCELLED,
    REASON_COMPENSATION_FAILED,
    REASON_DELIVERED,
    REASON_DELIVERED_EMAIL,
    REASON_NEEDS_HUMAN,
    REASON_NO_DELIVERY,
    REASON_NO_WORK,
    REASON_PARSE_ERROR,
    REASON_ROUTER_NO_MATCH,
    REASON_SOURCE_ERROR,
    ClassificationResult,
    RunClassificationValue,
    classify_and_persist_run,
    classify_run,
    collect_pr_urls,
    persist_classification,
    reconcile_missing_classifications,
)
from modulo.core.pipeline_engine.error_codes import class_for
from modulo.db.crud.run import update_run_status
from modulo.db.models.base import Base
from modulo.db.models.run import TERMINAL_STATUSES, Run

_ORG = uuid.UUID("00000000-0000-0000-0000-000000000001")
_PIPELINE = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
_SNAPSHOT = uuid.UUID("00000000-0000-0000-0000-0000000000b1")

_PR = "https://github.com/farnalabs/modulo/pull/123"
_PR_2 = "https://github.com/farnalabs/modulo/pull/456"


def _node_return_with_pr(pr_url: str) -> dict[str, Any]:
    """A P1 sandbox_agent node return — the pure output_json carrying pr_url."""
    return {"pr_url": pr_url, "summary": "done", "changed_files": ["a.py"]}


def _legacy_envelope_with_pr(pr_url: str) -> dict[str, Any]:
    """A legacy mixed envelope with the pr_url inside ``output``."""
    return {"output": {"pr_url": pr_url, "status": "completed"}, "summary": "done"}


def _artifacts_envelope_with_pr(pr_url: str) -> dict[str, Any]:
    return {"artifacts": [{"output": {"output_json": {"pr_url": pr_url}, "status": "completed"}}]}


def _markers(*pr_urls: str) -> dict[str, dict[str, Any]]:
    """raw_output_markers keyed by attempt_key, each carrying a pr_url."""
    return {
        f"attempt-{i}": {
            "_modulo_marker": True,
            "status": "failed",
            "pr_url": pr_url,
            "parse_error": "",
            "attempt_key": f"attempt-{i}",
        }
        for i, pr_url in enumerate(pr_urls)
    }


# ---------------------------------------------------------------------------
# Pure decision-table tests
# ---------------------------------------------------------------------------


class TestDecisionTable:
    """Spec §6 — keyed on (status, error_code), never prose."""

    @pytest.mark.parametrize(
        ("status", "pr_urls", "expected", "expected_reason"),
        [
            ("complete", (), RunClassificationValue.no_delivery, REASON_NO_WORK),
            ("complete", (_PR,), RunClassificationValue.delivered, REASON_DELIVERED),
            ("failed", (), RunClassificationValue.no_delivery, None),
            ("failed", (_PR,), RunClassificationValue.no_delivery, None),
            ("eval_failed", (), RunClassificationValue.no_delivery, None),
            ("eval_failed", (_PR,), RunClassificationValue.no_delivery, None),
            ("stalled", (), RunClassificationValue.no_delivery, None),
            ("stalled", (_PR,), RunClassificationValue.no_delivery, None),
            ("cancelled", (), RunClassificationValue.excluded, REASON_CANCELLED),
            ("cancelled", (_PR,), RunClassificationValue.excluded, REASON_CANCELLED),
            ("budget_exceeded", (), RunClassificationValue.excluded, REASON_BUDGET_EXCEEDED),
            ("budget_exceeded", (_PR,), RunClassificationValue.excluded, REASON_BUDGET_EXCEEDED),
            ("router_no_match", (), RunClassificationValue.excluded, REASON_ROUTER_NO_MATCH),
            ("router_no_match", (_PR,), RunClassificationValue.excluded, REASON_ROUTER_NO_MATCH),
            # FAR-402 P5: compensation_failed is a COUNTABLE no_delivery with its
            # own explicit reason (a watched node AND its compensation path both
            # failed) — never fail-safe. A complementary `unknown` row asserts a
            # terminal status outside the excluded/countable buckets is EXCLUDED
            # (not silently counted as no_delivery).
            ("compensation_failed", (), RunClassificationValue.no_delivery, REASON_COMPENSATION_FAILED),
            ("compensation_failed", (_PR,), RunClassificationValue.no_delivery, REASON_COMPENSATION_FAILED),
            ("unknown", (), RunClassificationValue.excluded, "unrecognized_status:unknown"),
            ("unknown", (_PR,), RunClassificationValue.excluded, "unrecognized_status:unknown"),
        ],
        ids=[
            "complete-no-pr",
            "complete-with-pr",
            "failed-no-pr",
            "failed-with-pr",
            "eval_failed-no-pr",
            "eval_failed-with-pr",
            "stalled-no-pr",
            "stalled-with-pr",
            "cancelled-no-pr",
            "cancelled-with-pr",
            "budget_exceeded-no-pr",
            "budget_exceeded-with-pr",
            "router_no_match-no-pr",
            "router_no_match-with-pr",
            "compensation_failed-no-pr",
            "compensation_failed-with-pr",
            "unknown-no-pr",
            "unknown-with-pr",
        ],
    )
    def test_terminal_status_matrix(
        self,
        status: str,
        pr_urls: tuple[str, ...],
        expected: RunClassificationValue,
        expected_reason: str | None,
    ) -> None:
        outputs = {f"n{i}": _node_return_with_pr(url) for i, url in enumerate(pr_urls)}
        telemetry = {f"n{i}": {"agent_status": "completed", "agent_outcome": "success"} for i in range(len(pr_urls))}
        result = classify_run(status, None, outputs_json=outputs, telemetry_json=telemetry)
        assert result.value == expected
        if expected_reason is not None:
            assert result.reason == expected_reason

    def test_router_no_match_is_not_mislabeled_budget_exceeded(self) -> None:
        """Regression guard (FAR-415): a ``router_no_match`` run is an excluded
        terminal status but must NOT inherit the ``budget_exceeded`` reason — a
        distinct, user-visible mislabel that would otherwise surface in
        analytics/reporting as a budget attribution. It gets its own reason."""
        result = classify_run("router_no_match", None)
        assert result.value == RunClassificationValue.excluded
        assert result.reason == REASON_ROUTER_NO_MATCH
        assert result.reason != REASON_BUDGET_EXCEEDED

    def test_complete_with_invalid_pr_url_is_no_delivery(self) -> None:
        # A pr_url that does not parse as http(s) + netloc is NOT a delivery.
        outputs = {"n1": _node_return_with_pr("not a url")}
        result = classify_run("complete", None, outputs_json=outputs, telemetry_json={"n1": {}})
        assert result.value == RunClassificationValue.no_delivery
        assert result.reason == REASON_NO_WORK

    def test_non_terminal_status_is_guarded_excluded(self) -> None:
        result = classify_run("running", None)
        assert result.value == RunClassificationValue.excluded
        assert result.reason.startswith("unrecognized_status")

    def test_new_terminal_status_fails_loudly_not_complete(self) -> None:
        """FIX 6: a terminal status outside the excluded/countable buckets AND
        != complete classifies as excluded — a new status added to
        TERMINAL_STATUSES must fail loudly in tests, never silently inherit
        complete semantics."""
        with patch(
            "modulo.db.models.run.TERMINAL_STATUSES",
            frozenset({*TERMINAL_STATUSES, "expired"}),
        ):
            result = classify_run("expired", None)
        assert result.value == RunClassificationValue.excluded
        assert result.reason.startswith("unrecognized_status")


class TestPrUrlValidity:
    """Spec §2 — urlsplit with scheme http/https + non-empty netloc."""

    @pytest.mark.parametrize(
        ("url", "valid"),
        [
            ("https://github.com/farnalabs/modulo/pull/1", True),
            ("https://github.com/farnalabs/modulo", True),
            ("http://example.com/x", True),
            ("https://", False),
            ("http://", False),
            ("ftp://github.com/farnalabs/modulo/pull/2", False),
            ("not a url", False),
            ("github.com/farnalabs/modulo/pull/3", False),
            ("", False),
        ],
        ids=[
            "github-pull",
            "github-repo",
            "http-example",
            "scheme-only-https",
            "scheme-only-http",
            "ftp-blocked",
            "not-a-url",
            "bare-host",
            "empty",
        ],
    )
    def test_validity(self, url: str, valid: bool) -> None:
        result = classify_run(
            "complete",
            None,
            outputs_json={"n1": _node_return_with_pr(url)},
            telemetry_json={"n1": {}},
        )
        expected = RunClassificationValue.delivered if valid else RunClassificationValue.no_delivery
        assert result.value == expected


class TestPrUrlSources:
    """delivered signals recovered from node returns AND raw_output_markers."""

    def test_pr_url_in_node_return_direct(self) -> None:
        outputs = {"n1": _node_return_with_pr(_PR)}
        result = classify_run(
            "complete",
            None,
            outputs_json=outputs,
            telemetry_json={"n1": {"agent_status": "completed", "agent_outcome": "success"}},
        )
        assert result.value == RunClassificationValue.delivered
        assert _PR in result.delivered_pr_urls

    def test_pr_url_in_legacy_envelope(self) -> None:
        outputs = {"n1": _legacy_envelope_with_pr(_PR)}
        result = classify_run("complete", None, outputs_json=outputs, telemetry_json={})
        assert result.value == RunClassificationValue.delivered
        assert _PR in result.delivered_pr_urls

    def test_pr_url_in_artifacts_envelope(self) -> None:
        outputs = {"n1": _artifacts_envelope_with_pr(_PR)}
        result = classify_run("complete", None, outputs_json=outputs, telemetry_json={})
        assert result.value == RunClassificationValue.delivered

    def test_pr_url_nested_in_raw_output_markers_any_attempt_key(self) -> None:
        """A pr_url recovered from ANY attempt_key is a valid delivery signal —
        first-attempt PRs created before a sandbox stall/retry are real."""
        result = classify_run(
            "complete",
            None,
            outputs_json=None,
            telemetry_json=None,
            raw_output_markers=_markers(_PR, _PR_2),
        )
        assert result.value == RunClassificationValue.delivered
        assert result.delivered_pr_urls == (_PR, _PR_2)

    def test_pr_url_both_sources_deduplicated(self) -> None:
        outputs = {"n1": _node_return_with_pr(_PR)}
        markers = _markers(_PR)
        urls = collect_pr_urls(outputs, {"n1": {}}, markers)
        assert urls == [_PR]

    def test_pr_url_only_in_telemetry_value_is_delivered(self) -> None:
        """FIX 2: a pr_url carried ONLY in a node telemetry VALUE (not the node
        return) is a real delivery signal — telemetry VALUES are scanned, not
        just keys."""
        outputs = {"n1": {"summary": "no pr_url here"}}
        telemetry = {"n1": {"agent_status": "completed", "agent_outcome": "success", "pr_url": _PR}}
        result = classify_run("complete", None, outputs_json=outputs, telemetry_json=telemetry)
        assert result.value == RunClassificationValue.delivered
        assert _PR in result.delivered_pr_urls

    def test_pr_url_in_both_node_and_telemetry_deduplicated(self) -> None:
        outputs = {"n1": _node_return_with_pr(_PR)}
        telemetry = {"n1": {"agent_status": "completed", "agent_outcome": "success", "pr_url": _PR}}
        urls = collect_pr_urls(outputs, telemetry, None)
        assert urls == [_PR]

    def test_failed_with_pr_url_from_markers_is_still_no_delivery(self) -> None:
        # A failed run is COUNTABLE no_delivery regardless of any pr_url
        # evidence (the pr_url matters only for the complete verdict).
        result = classify_run(
            "failed",
            "node.cancelled",
            outputs_json=None,
            telemetry_json=None,
            raw_output_markers=_markers(_PR),
        )
        assert result.value == RunClassificationValue.no_delivery


def _email_markers(*attempt_keys: str) -> dict[str, dict[str, Any]]:
    """raw_output_markers where each marker carries delivery_done (FAR-228)."""
    return {
        key: {
            "_modulo_marker": True,
            "status": "failed",
            "pr_url": "",
            "parse_error": "email sent then sandbox crashed",
            "attempt_key": key,
            "delivery_done": True,
        }
        for key in attempt_keys
    }


class TestDeliveryDoneClassification:
    """FAR-228: a complete run whose raw-output marker carries delivery_done is
    classified delivered (REASON_DELIVERED_EMAIL) — the delivery was made even
    though the node later failed/retried. pr_url STILL wins when both exist."""

    def test_complete_with_delivery_done_marker_is_delivered(self) -> None:
        result = classify_run(
            "complete",
            None,
            outputs_json={},
            telemetry_json={},
            raw_output_markers=_email_markers("run:run-1:node:n1:1"),
        )
        assert result.value == RunClassificationValue.delivered
        assert result.reason == REASON_DELIVERED_EMAIL

    def test_complete_without_marker_still_no_work(self) -> None:
        result = classify_run("complete", None, outputs_json={}, telemetry_json={})
        assert result.value == RunClassificationValue.no_delivery
        assert result.reason == REASON_NO_WORK

    def test_real_pr_url_still_wins_over_email_marker(self) -> None:
        """A valid pr_url takes precedence — a run that made a PR AND sent the
        email records REASON_DELIVERED (pr_delivered), not email_delivered."""
        result = classify_run(
            "complete",
            None,
            outputs_json={"n1": _node_return_with_pr(_PR)},
            telemetry_json={"n1": {}},
            raw_output_markers=_email_markers("run:run-1:node:n1:1"),
        )
        assert result.value == RunClassificationValue.delivered
        assert result.reason == REASON_DELIVERED
        assert result.delivered_pr_urls == (_PR,)

    def test_failed_with_delivery_done_marker_is_still_no_delivery(self) -> None:
        """The email verdict only applies to the complete bucket — a failed run
        remains countable no_delivery (mirrors the pr_url rule)."""
        result = classify_run(
            "failed",
            "node.cancelled",
            outputs_json={},
            telemetry_json={},
            raw_output_markers=_email_markers("run:run-1:node:n1:1"),
        )
        assert result.value == RunClassificationValue.no_delivery

    def test_complete_with_success_path_marker_is_email_delivered(self) -> None:
        """FAR-228 review fix: a marker persisted on the SUCCESS path (status
        ``completed``, empty parse_error, exit_code 0 — the shape written by the
        node's success-path retention) classifies a pr_url-less complete run as
        delivered/email_delivered, NOT no_delivery/no_work. This is the
        observable outcome the success marker must produce."""
        result = classify_run(
            "complete",
            None,
            outputs_json={},
            telemetry_json={},
            raw_output_markers={
                "run:run-1:node:n1:1": {
                    "_modulo_marker": True,
                    "status": "completed",
                    "summary": "Sandbox agent completed with delivery sentinel observed (idempotency gate)",
                    "raw_output": "email sent\nEMAIL_SENT\n",
                    "parse_error": "",
                    "pr_url": "",
                    "exit_code": 0,
                    "stdout_length": 10,
                    "stderr_length": 0,
                    "attempt_key": "run:run-1:node:n1:1",
                    "node_id": "n1",
                    "delivery_done": True,
                }
            },
        )
        assert result.value == RunClassificationValue.delivered
        assert result.reason == REASON_DELIVERED_EMAIL
        assert not result.delivered_pr_urls

    def test_complete_without_success_marker_still_no_work(self) -> None:
        """FAR-228: without a delivery_done marker (and without pr_url) a
        successful sentinel-free run stays no_delivery/no_work — the marker is
        what flips the verdict, never the run status alone."""
        result = classify_run(
            "complete",
            None,
            outputs_json={},
            telemetry_json={},
            raw_output_markers={
                "run:run-1:node:n1:1": {
                    "_modulo_marker": True,
                    "status": "completed",
                    "parse_error": "",
                    "pr_url": "",
                    "attempt_key": "run:run-1:node:n1:1",
                    "node_id": "n1",
                }
            },
        )
        assert result.value == RunClassificationValue.no_delivery
        assert result.reason == REASON_NO_WORK


class TestReasons:
    """Spec §7 — reason on no_delivery: no_work / needs_human / source_error /
    parse_error when derivable, else no_delivery."""

    def test_complete_no_pr_url_reason_no_work(self) -> None:
        result = classify_run("complete", None)
        assert result.value == RunClassificationValue.no_delivery
        assert result.reason == REASON_NO_WORK

    def test_failed_plain_reason_no_delivery(self) -> None:
        result = classify_run("failed", None)
        assert result.reason == "no_delivery"

    def test_failed_source_error(self) -> None:
        # infra/sandbox crash elevated to failed — source_error (PO: counts).
        result = classify_run("failed", "node.cancelled")
        assert result.value == RunClassificationValue.no_delivery
        assert result.reason == REASON_SOURCE_ERROR

    def test_failed_sandbox_no_output_json_source_error(self) -> None:
        """FAR-227: a sandbox session-lost run (the fallback echo) classifies as
        a COUNTABLE no_delivery with reason source_error — the sandbox class is
        in ``_SOURCE_ERROR_CLASSES``, so a transient session death counts (and is
        retryable)."""
        result = classify_run("failed", "sandbox.no_output_json")
        assert result.value == RunClassificationValue.no_delivery
        assert result.reason == REASON_SOURCE_ERROR

    def test_failed_legacy_code_resolved_to_source_error(self) -> None:
        result = classify_run("stalled", "node_timeout")
        assert result.reason == REASON_SOURCE_ERROR

    def test_failed_provider_unavailable_source_error(self) -> None:
        result = classify_run("failed", "provider.unavailable")
        assert result.value == RunClassificationValue.no_delivery
        assert result.reason == REASON_SOURCE_ERROR

    def test_failed_provider_rate_limited_source_error(self) -> None:
        result = classify_run("failed", "provider.rate_limited")
        assert result.value == RunClassificationValue.no_delivery
        assert result.reason == REASON_SOURCE_ERROR

    def test_failed_legacy_provider_code_resolved_to_source_error(self) -> None:
        """Legacy alias: raw exception class name -> provider.rate_limited.

        The classifier must pass the CASE-PRESERVED code to ``class_for`` — the
        pre-fix bug lowercased it, so ``"ratelimiterror"`` fell through to
        ``harness.unknown`` and this test passed via the harness fallback
        instead of the provider alias. The argument capture proves the alias
        path actually fires (it fails without the case-preservation fix).
        """
        seen: list[str] = []
        real_class_for = class_for

        def _capturing_class_for(code: str | None) -> str:
            seen.append(code or "")
            return real_class_for(code)

        with patch("modulo.core.pipeline_engine.error_codes.class_for", side_effect=_capturing_class_for):
            result = classify_run("failed", "RateLimitError")
        assert seen
        assert seen[-1] == "RateLimitError"
        assert real_class_for("RateLimitError") == "provider"
        assert result.reason == REASON_SOURCE_ERROR

    def test_failed_legacy_sandbox_class_name_resolves_its_alias(self) -> None:
        """A mixed-case NON-provider exception class name resolves its true
        alias through the case-preserved lookup — SandboxNodeFailedError ->
        sandbox.no_output_json -> class sandbox -> source_error (not the
        harness.unknown fallback)."""
        assert class_for("SandboxNodeFailedError") == "sandbox"
        assert class_for("SandboxNodeFailedError") != class_for("sandboxnodefailederror")
        result = classify_run("failed", "SandboxNodeFailedError")
        assert result.reason == REASON_SOURCE_ERROR

    def test_failed_needs_human(self) -> None:
        result = classify_run("failed", "harness.gate_creation_failed")
        assert result.reason == REASON_NEEDS_HUMAN

    def test_failed_parse_error_from_marker(self) -> None:
        markers = {
            "attempt-0": {
                "_modulo_marker": True,
                "status": "failed",
                "pr_url": "",
                "parse_error": "JSONDecodeError: Expecting value",
                "attempt_key": "attempt-0",
            }
        }
        result = classify_run("failed", "sandbox.no_output_json", raw_output_markers=markers)
        assert result.reason == REASON_PARSE_ERROR

    def test_cancelled_unparseable_reason_is_excluded(self) -> None:
        # Spec: unparseable-reason default for status=cancelled is excluded,
        # never countable.
        result = classify_run("cancelled", "junk_error_code", raw_output_markers=_markers(_PR))
        assert result.value == RunClassificationValue.excluded
        assert result.reason == REASON_CANCELLED

    def test_declared_success_nodes_recorded(self) -> None:
        outputs = {
            "n1": _node_return_with_pr(_PR),
            "n2": {"summary": "no agent_outcome"},
        }
        telemetry = {
            "n1": {"agent_status": "completed", "agent_outcome": "success"},
            "n2": {"agent_status": "completed", "agent_outcome": "failed"},
        }
        result = classify_run("complete", None, outputs_json=outputs, telemetry_json=telemetry)
        assert result.declared_success_nodes == 1

    def test_work_intact_recorded_as_metadata(self) -> None:
        result = classify_run("complete", None, work_intact=True)
        assert result.work_intact is True


# ---------------------------------------------------------------------------
# Persistence hook — in-memory SQLite (real Run table + real update_run_status)
# ---------------------------------------------------------------------------


_TABLES: list[Table] = cast(list[Table], [Run.__table__])


@pytest.fixture
async def engine() -> AsyncGenerator[AsyncEngine, None]:
    # StaticPool: an in-memory SQLite DB is per-connection; the pool shares ONE
    # connection so sessions AND the independent _read_classification
    # connections all observe the same database.
    eng = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool, echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=_TABLES))
        await conn.exec_driver_sql("PRAGMA foreign_keys = OFF")
    yield eng
    await eng.dispose()


@pytest.fixture
async def session(engine: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    # autobegin=False matches the production DI factory: every DB operation must
    # sit inside an explicit ``async with session.begin():`` block.
    maker = async_sessionmaker(engine, expire_on_commit=False, autobegin=False)
    async with maker() as s:
        yield s


async def _seed_run(
    session: AsyncSession,
    run_id: uuid.UUID,
    *,
    status: str = "running",
    outputs: dict[str, Any] | None = None,
    telemetry: dict[str, Any] | None = None,
    markers: dict[str, Any] | None = None,
    error_code: str | None = None,
    work_intact: bool | None = None,
) -> Run:
    run = Run(
        id=run_id,
        organisation_id=_ORG,
        pipeline_id=_PIPELINE,
        snapshot_id=_SNAPSHOT,
        trigger_type="manual",
        status=status,
        run_number=int(run_id.int % 10**9) + 1,
        input_hash="ih",
        input_payload={},
        langgraph_thread_id=f"thread-{run_id}",
        claim_token="tok-a",
        cancellation_requested=False,
        raw_output_markers=markers,
        outputs_json=outputs,
        node_telemetry_json=telemetry,
        error_code=error_code,
        work_intact=work_intact,
    )
    session.add(run)
    await session.flush()
    return run


async def _read_classification(engine: AsyncEngine, run_id: uuid.UUID) -> dict[str, Any] | None:
    # ORM select (not raw text): SQLAlchemy applies the Uuid() type conversion
    # — a raw ``str(uuid)`` bind silently misses SQLite's CHAR(32) id storage.
    maker = async_sessionmaker(engine, expire_on_commit=False, autobegin=False)
    async with maker() as s, s.begin():
        run = (await s.execute(select(Run).where(Run.id == run_id))).scalar_one_or_none()
    return run.run_classification if run is not None else None


class TestPrUrlEdgeCases:
    """Pure-helper edge paths in the pr_url extraction / validity logic."""

    def test_validation_error_url_is_invalid(self) -> None:
        """A URL whose urlsplit raises ValueError is invalid (no delivery)."""
        bad = "http://[::1"  # malformed IPv6 bracket -> urlsplit ValueError
        result = classify_run("complete", None, outputs_json={"n1": _node_return_with_pr(bad)}, telemetry_json={})
        assert result.value == RunClassificationValue.no_delivery

    def test_non_dict_node_value_extracts_nothing(self) -> None:
        """A scalar node value carries no pr_url (no crash)."""
        urls = collect_pr_urls({"n1": "just-a-string"}, {"n1": "also-a-string"}, None)
        assert urls == []

    def test_cyclic_node_value_terminates_scan(self) -> None:
        """A self-referencing node value does not loop forever."""
        cyclic: dict[str, Any] = {}
        cyclic["self"] = cyclic
        urls = collect_pr_urls({"n1": {"pr_url": "not-a-valid-url", "nested": cyclic}}, {}, None)
        assert urls == []

    def test_non_dict_marker_values_skipped(self) -> None:
        """markers whose values are not dicts are skipped in collect."""
        urls = collect_pr_urls({}, {}, {"attempt-0": "not-a-dict", "attempt-1": {"pr_url": _PR}})
        assert urls == [_PR]

    def test_invalid_marker_url_skipped(self) -> None:
        """A marker carrying an invalid pr_url is skipped."""
        urls = collect_pr_urls({}, {}, {"attempt-0": {"pr_url": "not a url"}})
        assert urls == []

    def test_parse_error_skips_non_dict_marker(self) -> None:
        """_any_marker_parse_error ignores non-dict marker values."""
        markers = {"attempt-0": "not-a-dict", "attempt-1": {"parse_error": ""}}
        result = classify_run("failed", "node.cancelled", raw_output_markers=markers)
        assert result.reason != REASON_PARSE_ERROR

    def test_delivery_done_skips_non_dict_marker(self) -> None:
        """_any_marker_delivery_done ignores non-dict marker values."""
        markers = {"attempt-0": "not-a-dict", "attempt-1": {"delivery_done": True}}
        result = classify_run("complete", None, raw_output_markers=markers)
        assert result.value == RunClassificationValue.delivered

    def test_class_for_failure_falls_back_to_no_delivery(self) -> None:
        """A class_for lookup failure yields no_delivery, not a crash."""
        with patch(
            "modulo.core.pipeline_engine.error_codes.class_for",
            side_effect=RuntimeError("registry broken"),
        ):
            result = classify_run("failed", "agent.failed")
        assert result.value == RunClassificationValue.no_delivery
        assert result.reason == REASON_NO_DELIVERY


class TestPersistenceHook:
    async def test_update_run_status_writes_classification_atomically(
        self,
        engine: AsyncEngine,
        session: AsyncSession,
    ) -> None:
        run_id = uuid.uuid4()
        async with session.begin():
            await _seed_run(session, run_id)
            updated = await update_run_status(
                session,
                run_id,
                "complete",
                outputs_json={"n1": _node_return_with_pr(_PR)},
                node_telemetry_json={"n1": {"agent_status": "completed", "agent_outcome": "success"}},
            )
        assert updated is not None
        assert updated.status == "complete"

        record = await _read_classification(engine, run_id)
        assert record is not None
        assert record["value"] == "delivered"
        assert _PR in record["delivered_pr_urls"]

    async def test_failed_run_classifies_no_delivery(self, engine: AsyncEngine, session: AsyncSession) -> None:
        run_id = uuid.uuid4()
        async with session.begin():
            await _seed_run(session, run_id)
            await update_run_status(session, run_id, "failed", error_code="node.cancelled")
        record = await _read_classification(engine, run_id)
        assert record is not None
        assert record["value"] == "no_delivery"
        assert record["reason"] == REASON_SOURCE_ERROR

    async def test_cancelled_via_request_cancellation_classifies_excluded(
        self,
        engine: AsyncEngine,
        session: AsyncSession,
    ) -> None:
        from modulo.db.crud.run import request_cancellation

        run_id = uuid.uuid4()
        async with session.begin():
            await _seed_run(session, run_id)
            run = await request_cancellation(session, run_id)
        assert run is not None
        assert run.status == "cancelled"
        record = await _read_classification(engine, run_id)
        assert record is not None
        assert record["value"] == "excluded"
        assert record["reason"] == REASON_CANCELLED

    async def test_non_terminal_write_leaves_no_record(self, engine: AsyncEngine, session: AsyncSession) -> None:
        run_id = uuid.uuid4()
        async with session.begin():
            await _seed_run(session, run_id)
            await update_run_status(session, run_id, "running", claimed_by="worker")
        assert await _read_classification(engine, run_id) is None

    async def test_work_intact_flows_through_orm_persist(
        self,
        engine: AsyncEngine,
        session: AsyncSession,
    ) -> None:
        """FIX 3: work_intact is read from the MAPPED ORM column (migration 0091)
        and recorded as metadata — the terminalization write observes it."""
        run_id = uuid.uuid4()
        async with session.begin():
            await _seed_run(session, run_id, status="complete", work_intact=True)
            await update_run_status(session, run_id, "complete")
        record = await _read_classification(engine, run_id)
        assert record is not None
        assert record["value"] == "no_delivery"
        assert record["work_intact"] is True

    async def test_executor_ordering_persists_work_intact_after_refresh(
        self,
        engine: AsyncEngine,
        session: AsyncSession,
    ) -> None:
        """FIX 3 (round-2): the EXECUTOR terminalization ordering must leave the
        classification record carrying the REAL work_intact.

        The executor calls ``finalize_cost`` (terminal write → inline classify
        reading ``run.work_intact``) BEFORE ``_apply_work_intact``, so the first
        record persists ``work_intact=None``. The fix re-persists the
        classification AFTER the work_intact write, in the same transaction, so
        the record carries the real value. This test reproduces that exact
        ordering against a real DB and asserts the final record is corrected —
        without the reclassify-after-write step the assertion fails."""
        from modulo.core.pipeline_engine.executor import _apply_work_intact, _reclassify_after_work_intact

        run_id = uuid.uuid4()
        # 1. Terminalize WITHOUT work_intact set — finalize_cost's inline
        #    classify runs before the work_intact write, persisting None.
        async with session.begin():
            await _seed_run(session, run_id, status="complete", work_intact=None)
            await update_run_status(session, run_id, "complete")
        record = await _read_classification(engine, run_id)
        assert record is not None
        assert record["work_intact"] is None

        # 2. The executor's work_intact write + reclassify, in ONE transaction
        #    (the same transaction finalize_cost and _apply_work_intact share).
        maker = async_sessionmaker(engine, expire_on_commit=False, autobegin=False)
        async with maker() as s, s.begin():
            await _apply_work_intact(s, run_id, True, claim_token=None)
            await _reclassify_after_work_intact(s, run_id)

        record = await _read_classification(engine, run_id)
        assert record is not None
        assert record["work_intact"] is True
        assert record["value"] == "no_delivery"


class TestCrossTenantIsolation:
    async def test_cross_tenant_terminalization_never_classifies_other_org(
        self,
        engine: AsyncEngine,
        session: AsyncSession,
    ) -> None:
        """FIX 9: org A terminalizing a run must not classify org B's run.

        Exercises the generic-backend tenant filter path (the SQLite analogue
        of Postgres RLS): with ``session.info["org_id"] = org_a`` the terminal
        select is scoped to org A, so org B's run is invisible and no
        classification record can be written for it.
        """
        from modulo.db.rls import _inject_tenant_filter

        org_a = uuid.UUID("00000000-0000-0000-0000-0000000000a9")
        run_id = uuid.uuid4()
        async with session.begin():
            await _seed_run(session, run_id)  # organisation_id = _ORG (org B)

        event.listen(SASession, "do_orm_execute", _inject_tenant_filter)
        try:
            maker = async_sessionmaker(engine, expire_on_commit=False, autobegin=False)
            async with maker() as org_a_session:
                org_a_session.info["org_id"] = org_a
                async with org_a_session.begin():
                    updated = await update_run_status(
                        org_a_session,
                        run_id,
                        "complete",
                        outputs_json={"n1": _node_return_with_pr(_PR)},
                        node_telemetry_json={"n1": {"agent_status": "completed", "agent_outcome": "success"}},
                    )
                # The tenant filter injects WHERE organisation_id = org_a, so org A
                # cannot even see org B's run — nothing is terminalized, nothing
                # classified.
                assert updated is None
        finally:
            event.remove(SASession, "do_orm_execute", _inject_tenant_filter)
        assert await _read_classification(engine, run_id) is None


class TestFailureAndIdempotency:
    async def test_classifier_failure_persists_unclassified_and_terminalization_survives(
        self,
        engine: AsyncEngine,
        session: AsyncSession,
    ) -> None:
        run_id = uuid.uuid4()
        async with session.begin():
            await _seed_run(session, run_id)
            with patch("modulo.core.pipeline_engine.classify.classify_run", side_effect=RuntimeError("boom")):
                updated = await update_run_status(session, run_id, "complete")
        assert updated is not None
        assert updated.status == "complete"
        record = await _read_classification(engine, run_id)
        assert record is not None
        assert record["value"] == "unclassified"

    async def test_persist_failure_never_blocks_terminalization(
        self,
        engine: AsyncEngine,
        session: AsyncSession,
    ) -> None:
        run_id = uuid.uuid4()
        async with session.begin():
            await _seed_run(session, run_id)
            with patch(
                "modulo.core.pipeline_engine.classify.persist_classification",
                side_effect=RuntimeError("boom"),
            ):
                updated = await update_run_status(session, run_id, "complete")
        assert updated is not None
        assert updated.status == "complete"

    async def test_classifier_run_twice_is_one_record(
        self,
        engine: AsyncEngine,
        session: AsyncSession,
    ) -> None:
        run_id = uuid.uuid4()
        async with session.begin():
            run = await _seed_run(
                session,
                run_id,
                status="complete",
                outputs={"n1": _node_return_with_pr(_PR)},
                telemetry={"n1": {"agent_status": "completed", "agent_outcome": "success"}},
            )
            await classify_and_persist_run(session, run)
            await classify_and_persist_run(session, run)
        record = await _read_classification(engine, run_id)
        assert record is not None
        assert record["value"] == "delivered"
        assert record["delivered_pr_urls"] == [_PR]

    async def test_re_terminalization_refreshes_classification(
        self,
        engine: AsyncEngine,
        session: AsyncSession,
    ) -> None:
        """Retry policy re-flips a classified failed run to pending, then a
        re-run terminalizes with new evidence — the record is UPSERTED (refreshed),
        not duplicated and not frozen at the stale verdict."""
        run_id = uuid.uuid4()
        async with session.begin():
            await _seed_run(session, run_id)
            await update_run_status(session, run_id, "failed", error_code="node.cancelled")
        record = await _read_classification(engine, run_id)
        assert record is not None
        assert record["value"] == "no_delivery"

        async with session.begin():
            await update_run_status(session, run_id, "pending", clear_error_code=True)
        # The pending flip is non-terminal — the stale record must be untouched.
        assert (await _read_classification(engine, run_id))["value"] == "no_delivery"

        async with session.begin():
            await update_run_status(
                session,
                run_id,
                "complete",
                outputs_json={"n1": _node_return_with_pr(_PR)},
                node_telemetry_json={"n1": {"agent_status": "completed", "agent_outcome": "success"}},
            )
        record = await _read_classification(engine, run_id)
        assert record is not None
        assert record["value"] == "delivered"
        assert _PR in record["delivered_pr_urls"]

    async def test_persist_failure_fallback_returns_false(
        self,
        engine: AsyncEngine,
        session: AsyncSession,
    ) -> None:
        async with session.begin():
            run = await _seed_run(session, uuid.uuid4(), status="complete")
            with patch(
                "modulo.core.pipeline_engine.classify.persist_classification",
                new=AsyncMock(return_value=False),
            ):
                ok = await classify_and_persist_run(session, run)
        assert ok is False

    async def test_terminalization_survives_classifier_import_failure(
        self,
        engine: AsyncEngine,
        session: AsyncSession,
    ) -> None:
        """FIX 4: an unguarded classifier import raising inside the terminal write
        must NOT roll back the terminal status write — an unclassified marker is
        written directly instead."""
        run_id = uuid.uuid4()
        async with session.begin():
            await _seed_run(session, run_id)
        real_import = builtins.__import__

        def _failing_import(name: str, *args: Any, **kwargs: Any) -> Any:
            if name == "modulo.core.pipeline_engine.classify":
                raise ImportError("simulated classifier import failure")
            return real_import(name, *args, **kwargs)

        async with session.begin():
            with patch("builtins.__import__", side_effect=_failing_import):
                updated = await update_run_status(session, run_id, "failed", error_code="node.cancelled")
        assert updated is not None
        assert updated.status == "failed"
        record = await _read_classification(engine, run_id)
        assert record is not None
        assert record["value"] == "unclassified"

    async def test_status_guarded_persist_cannot_overwrite_stale_record(
        self,
        engine: AsyncEngine,
        session: AsyncSession,
    ) -> None:
        """FIX 5/10: a stale verdict persisted with a status guard that no longer
        matches writes 0 rows and returns failure — the fresh record survives."""
        run_id = uuid.uuid4()
        async with session.begin():
            run = await _seed_run(session, run_id, status="complete")
            await classify_and_persist_run(session, run)
        existing = await _read_classification(engine, run_id)
        assert existing is not None
        assert existing["value"] == "no_delivery"

        async with session.begin():
            run = (await session.execute(select(Run).where(Run.id == run_id))).scalar_one()
            stale = ClassificationResult(
                RunClassificationValue.delivered,
                REASON_DELIVERED,
                delivered_pr_urls=(_PR,),
            )
            ok = await persist_classification(session, run, stale, expected_status="failed")
            assert ok is False
        after = await _read_classification(engine, run_id)
        assert after == existing

    async def test_persist_zero_rows_reports_failure_not_success(
        self,
        engine: AsyncEngine,
        session: AsyncSession,
    ) -> None:
        """FIX 10: a silently RLS-filtered / status-guarded 0-row UPDATE must never
        report success with no record."""
        run_id = uuid.uuid4()
        async with session.begin():
            await _seed_run(session, run_id, status="complete")
        async with session.begin():
            run = (await session.execute(select(Run).where(Run.id == run_id))).scalar_one()
            result = classify_run("complete", None)
            ok = await persist_classification(session, run, result, expected_status="failed")
        assert ok is False
        assert await _read_classification(engine, run_id) is None

    async def test_persist_failure_never_leaves_record_missing(
        self,
        engine: AsyncEngine,
        session: AsyncSession,
    ) -> None:
        """FIX 11: on persist failure the simpler unclassified marker is written —
        a terminal run NEVER commits with run_classification = NULL."""
        run_id = uuid.uuid4()
        async with session.begin():
            run = await _seed_run(session, run_id, status="complete")
            with patch(
                "modulo.core.pipeline_engine.classify.persist_classification",
                new=AsyncMock(return_value=False),
            ):
                ok = await classify_and_persist_run(session, run)
        assert ok is False
        record = await _read_classification(engine, run_id)
        assert record is not None
        assert record["value"] == "unclassified"

    def test_result_to_dict_roundtrip(self) -> None:
        result = ClassificationResult(
            RunClassificationValue.delivered,
            REASON_DELIVERED,
            delivered_pr_urls=(_PR,),
        )
        payload = result.to_dict()
        assert payload["value"] == "delivered"
        assert payload["delivered_pr_urls"] == [_PR]
        assert "computed_at" in payload


class TestSweep:
    async def test_reconcile_backfills_terminal_runs_without_record(
        self,
        engine: AsyncEngine,
        session: AsyncSession,
    ) -> None:
        run_id = uuid.uuid4()
        async with session.begin():
            await _seed_run(session, run_id, status="failed", error_code="task_failure")

        maker = async_sessionmaker(engine, expire_on_commit=False, autobegin=False)
        summary = await reconcile_missing_classifications(maker, org_ids=[_ORG], max_runs=10, budget_seconds=30.0)

        assert summary["scanned"] >= 1
        assert summary["classified"] == 1
        record = await _read_classification(engine, run_id)
        assert record is not None
        assert record["value"] == "no_delivery"

    async def test_reconcile_skips_already_classified(
        self,
        engine: AsyncEngine,
        session: AsyncSession,
    ) -> None:
        run_id = uuid.uuid4()
        async with session.begin():
            run = await _seed_run(session, run_id, status="failed")
            await classify_and_persist_run(session, run)

        maker = async_sessionmaker(engine, expire_on_commit=False, autobegin=False)
        summary = await reconcile_missing_classifications(maker, org_ids=[_ORG], max_runs=10, budget_seconds=30.0)
        assert summary["scanned"] == 0

    async def test_sweep_backfills_run_failed_by_saq_hooks_mark_run_failed(
        self,
    ) -> None:
        """FAR-224: a run terminalised by ``saq_hooks._mark_run_failed`` — a
        raw-SQL terminalizer that bypasses the inline classification hook — must
        be classified by the reconcile sweep within one tick.

        Drives the REAL ``_mark_run_failed`` path (claim-token-fenced
        task_failure write) against the test engine, asserts the run is left
        with ``run_classification = NULL``, then runs the periodic
        ``run_classification_reconcile`` entrypoint and asserts the record is
        backfilled. A directly-seeded ``status='failed'`` row (as in
        ``test_reconcile_backfills_terminal_runs_without_record``) could not
        catch a regression where ``_mark_run_failed`` stops writing the
        terminal shape the sweep's predicate depends on.

        Uses its OWN engine (not the shared ``engine`` fixture) so the raw
        UPDATE's Postgres-style ``now()`` can be exposed on SQLite via
        ``create_function`` without affecting any other test.
        """
        from datetime import datetime

        from sqlalchemy import event

        from modulo.core import cron_helpers
        from modulo.core.error_tracking import saq_hooks

        eng = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool, echo=False)

        @event.listens_for(eng.sync_engine, "connect")
        def _sqlite_now(dbapi_connection: Any, connection_record: Any) -> None:
            dbapi_connection.create_function("now", 0, lambda: datetime.now(UTC).isoformat())

        async with eng.begin() as conn:
            await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=_TABLES))
        try:
            run_id = uuid.uuid4()
            maker = async_sessionmaker(eng, expire_on_commit=False, autobegin=False)
            async with maker() as s, s.begin():
                await _seed_run(s, run_id)  # status="running", claim_token="tok-a"

            with patch.object(saq_hooks, "_open_factory", return_value=maker):
                # Pass the id strings in SQLite's hyphenless-hex storage form:
                # SQLAlchemy's Uuid() stores a CHAR(32) hex string here, so the
                # production ``str(uuid)`` bind would match 0 rows on this test
                # engine (on Postgres both forms coerce to the same native
                # UUID — this is a SQLite test-infra accommodation only).
                rowcount = await saq_hooks._mark_run_failed(
                    run_id.hex,
                    _ORG.hex,
                    claim_token="tok-a",
                    error_detail="task failure",
                )
            assert rowcount == 1

            # The raw-SQL write bypasses the classification hook — the exact gap
            # the 60s dispatcher_reconcile sweep exists to close.
            assert await _read_classification(eng, run_id) is None

            with patch.object(cron_helpers, "_open_system_factory", return_value=maker):
                summary = await cron_helpers.run_classification_reconcile()
            assert summary["classified"] == 1
            record = await _read_classification(eng, run_id)
            assert record is not None
            assert record["value"] == "no_delivery"
        finally:
            await eng.dispose()


class _MockBegin:
    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False


class _MockSession:
    """Minimal session double for dispatcher_reconcile's row loop.

    Mirrors ``test_dispatcher_reconcile.py``'s ``_MockSession``: pops canned
    results in order, tolerates the org-id select, ``set_config``, and the
    terminalizer UPDATE statements, and returns zero terminalized rows by
    default.
    """

    def __init__(self, results: list[Any]) -> None:
        self._results = list(results)
        self.terminalizer_rows: dict[str, list[uuid.UUID]] = {}
        self.begin_cm = _MockBegin()
        bind = MagicMock()
        bind.dialect.name = "postgresql"
        self._get_bind = MagicMock(return_value=bind)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False

    def begin(self) -> _MockBegin:
        return self.begin_cm

    def get_bind(self) -> Any:
        return self._get_bind()

    async def execute(self, stmt: Any, params: dict[str, Any] | None = None) -> MagicMock:
        s = str(stmt)
        if "set_config" in s:
            return MagicMock()
        if "UPDATE runs SET" in s:
            ids = self.terminalizer_rows.get("executor_superseded", [])
            if "claim_cap_exhausted" in s:
                ids = self.terminalizer_rows.get("claim_cap_exhausted", [])
            r = MagicMock()
            r.all.return_value = [(uid,) for uid in ids]
            r.rowcount = len(ids)
            return r
        if not self._results:
            return MagicMock()
        return self._results.pop(0)


def _org_result(org_ids: list[uuid.UUID]) -> MagicMock:
    r = MagicMock()
    r.scalars.return_value = org_ids
    return r


def _rows_result(rows: list[Any]) -> MagicMock:
    r = MagicMock()
    r.all.return_value = rows
    return r


def _settings(**overrides: object) -> MagicMock:
    base: dict[str, object] = {
        "saq_runs_queue": "runs",
        "saq_reenqueue_window": 600,
        "saq_job_heartbeat": 300,
        "saq_claimed_nodeless_minutes": 45,
        "redis_url": "redis://localhost:6379/0",
        "saq_redis_pool_size": 5,
        "saq_run_claim_cap": 20,
        "modulo_telemetry_enabled": False,
    }
    base.update(overrides)
    return MagicMock(**base)


def _make_queue(redis_client: MagicMock) -> MagicMock:
    q = MagicMock()
    q.name = "runs"
    q.job_id.side_effect = lambda key: f"saq:job:runs:{key}"
    q.job = AsyncMock(return_value=None)
    return q


def _patch_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://localhost/test")
    monkeypatch.setenv("SECRET_KEY", "a" * 40)
    monkeypatch.setenv("FERNET_KEY", "b" * 44)


class TestPeriodicWiring:
    """FIX 1: the reconciliation sweep is wired into a periodic production path."""

    async def test_reconcile_sweep_wired_into_periodic_cron_path(self) -> None:
        """The cron_helpers entrypoint (invoked by dispatcher_reconcile every
        60s) actually calls the sweep — proven by patching the sweep."""
        from modulo.core import cron_helpers
        from modulo.core.pipeline_engine import classify as classify_module

        with (
            patch.object(
                classify_module,
                "reconcile_missing_classifications",
                new=AsyncMock(return_value={"scanned": 0, "classified": 0, "unclassified": 0, "errors": 0}),
            ) as sweep_mock,
            patch.object(cron_helpers, "_open_system_factory"),
        ):
            summary = await cron_helpers.run_classification_reconcile()
        assert sweep_mock.await_count == 1
        assert summary == {"scanned": 0, "classified": 0, "unclassified": 0, "errors": 0}

    async def test_dispatcher_reconcile_invokes_classification_sweep(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """FIX 2 (round-2): the SWEEP wiring is regression-tested at the real
        integration point — ``dispatcher_reconcile`` itself (not just the
        ``run_classification_reconcile`` entrypoint) must invoke the sweep.

        Deleting the ``await run_classification_reconcile()`` line from
        ``cron_helpers.dispatcher_reconcile`` must leave this test red — the
        round-1 dead-code critical that a direct entrypoint-call test cannot
        catch. Follows the ``test_dispatcher_reconcile.py`` ``_run_reconcile``
        fixture pattern: the session factory, Redis client, and queue are mocked
        the same way, then ``dispatcher_reconcile`` is awaited for real and the
        sweep call is asserted.
        """
        from modulo.core import cron_helpers as ch

        _patch_env(monkeypatch)
        session = _MockSession([_org_result([_ORG]), _rows_result([])])
        factory = MagicMock(return_value=session)
        redis_client = AsyncMock()
        q = _make_queue(redis_client)
        redis_cls = MagicMock()
        redis_cls.from_url.return_value = redis_client

        with (
            patch.object(ch, "_open_system_factory", return_value=factory),
            patch.object(ch, "get_settings", return_value=_settings()),
            patch.object(ch, "AsyncRedis", redis_cls),
            patch.object(ch, "RedisQueue", MagicMock(return_value=q)),
            patch.object(
                ch,
                "run_classification_reconcile",
                new=AsyncMock(return_value={"scanned": 0, "classified": 0, "unclassified": 0, "errors": 0}),
            ) as sweep_mock,
            patch.object(ch, "_re_enqueue_run", new_callable=AsyncMock, return_value=("enqueued", "new-job-id")),
            patch.object(ch, "_ingest_saq_error", new_callable=AsyncMock),
            patch.object(
                ch,
                "_awaiting_human_has_committed_decision",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch.object(ch, "_record_fact_for_terminalized_run", new_callable=AsyncMock),
            patch("modulo.db.crud.run.count_active_runs_for_pipeline", new_callable=AsyncMock, return_value=0),
        ):
            summary = await ch.dispatcher_reconcile()

        assert sweep_mock.await_count == 1
        assert summary["classification_classified"] == 0
        assert summary["classification_unclassified"] == 0

    async def test_run_classification_reconcile_classifies_through_periodic_path(
        self,
        engine: AsyncEngine,
    ) -> None:
        """End-to-end: the periodic entrypoint backfills an unclassified terminal
        run (raw-SQL-terminalizer shape) against a real DB."""
        from modulo.core import cron_helpers

        run_id = uuid.uuid4()
        maker = async_sessionmaker(engine, expire_on_commit=False, autobegin=False)
        async with maker() as s, s.begin():
            await _seed_run(s, run_id, status="failed", error_code="task_failure")

        with patch.object(cron_helpers, "_open_system_factory", return_value=maker):
            summary = await cron_helpers.run_classification_reconcile()
        assert summary["scanned"] >= 1
        assert summary["classified"] == 1
        record = await _read_classification(engine, run_id)
        assert record is not None
        assert record["value"] == "no_delivery"
