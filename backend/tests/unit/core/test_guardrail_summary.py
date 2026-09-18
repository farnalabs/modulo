"""FAR-223 item 11 — guardrail_summary telemetry (pure core).

Unit tests for the summary derivation (bucket counting, the
``evaluated + errored + skipped == bound`` invariant, expected vs unexpected
skips), the per-pattern fired-signature regression log, the summary
persistable round-trip, and the unexpected-skip alert. No DB, no FastAPI.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

import pytest

from modulo.core import guardrails as guardrails_module
from modulo.core.eval_engine import EvalDefinition, EvalResult, EvalType
from modulo.core.guardrails import (
    GuardrailSkip,
    GuardrailSummary,
    RedactionEntry,
    alert_unexpected_guardrail_skip,
    build_guardrail_summary,
    guardrail_pattern_hash,
    log_guardrail_fired_signatures,
)

_ORG = uuid.UUID("00000000-0000-0000-0000-000000000001")
_PIPELINE = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
_RUN = uuid.UUID("00000000-0000-0000-0000-0000000000b1")


def _def(name: str, action: str, *, config: dict[str, Any] | None = None) -> EvalDefinition:
    cfg: dict[str, Any] = {
        "action": action,
        "interception_point": "input",
        "type": "regex",
        "field": "body",
        "pattern": r"SECRET_[A-Z0-9]{8}",
    }
    if config:
        cfg.update(config)
    return EvalDefinition(
        id=uuid.uuid4(),
        org_id=_ORG,
        pipeline_id=_PIPELINE,
        node_id=None,
        name=name,
        eval_type=EvalType.GUARDRAIL,
        config=cfg,
        failure_behaviour="block",
    )


def _result(eval_def: EvalDefinition, *, passed: bool, detail: str = "") -> EvalResult:
    return EvalResult(
        run_id=_RUN,
        node_id="",
        eval_id=eval_def.id,
        passed=passed,
        score=1.0 if passed else 0.0,
        detail=detail,
    )


# ---------------------------------------------------------------------------
# Bucket counting + the invariant
# ---------------------------------------------------------------------------


def test_build_summary_counts_buckets_and_holds_invariant() -> None:
    """Regex passed=True = pattern matched = a VIOLATION (engine semantics);
    json_schema passed=True = validated = clean. Mechanism-error rows split out
    of evaluated into errored; evaluated + errored + skipped == bound."""
    regex_def = _def("regex-guard", "block")
    schema_def = _def(
        "schema-guard",
        "block",
        config={"type": "json_schema", "field": "body", "schema": {"type": "object"}},
    )
    error_def = _def("error-guard", "observe")
    results = [
        _result(regex_def, passed=True, detail="regex matched: /SECRET_[A-Z0-9]{8}/ on body"),
        _result(schema_def, passed=True, detail="JSON Schema validation passed"),
        _result(error_def, passed=False, detail="guardrail 'error-guard' mechanism error: detection error: boom"),
    ]
    redactions = [
        RedactionEntry(path="credentials.api_key", mode="transform", applied=True),
        RedactionEntry(path="missing.path", mode="transform", applied=False, reason="field-absent"),
    ]
    skipped = [GuardrailSkip(name="ghost", reason="soft_deleted")]

    summary = build_guardrail_summary(
        bound=len([regex_def, schema_def, error_def]) + len(skipped),
        definitions=[regex_def, schema_def, error_def],
        results=results,
        redactions=redactions,
        skipped=skipped,
        observed_by_eval={schema_def.id: True},
    )

    assert summary.bound == 4
    assert summary.evaluated == 2
    assert summary.passed == 1
    assert summary.violated == 1
    assert summary.observed == 1
    assert summary.errored == 1
    assert summary.redacted == 1
    assert summary.skipped == 1
    assert summary.expected_skips == 1
    assert summary.unexpected_skips == 0
    assert summary.evaluated + summary.errored + summary.skipped == summary.bound


def test_build_summary_json_schema_failed_is_violation() -> None:
    """json_schema passed=False = validation failed = a violation; regex
    passed=False = no match = clean."""
    regex_def = _def("regex-guard", "block")
    schema_def = _def(
        "schema-guard",
        "block",
        config={"type": "json_schema", "field": "body", "schema": {"type": "object"}},
    )
    summary = build_guardrail_summary(
        bound=2,
        definitions=[regex_def, schema_def],
        results=[
            _result(regex_def, passed=False, detail="regex no match: /SECRET_[A-Z0-9]{8}/ on body"),
            _result(schema_def, passed=False, detail="json_schema validation failed on field 'body'"),
        ],
    )
    assert summary.passed == 1
    assert summary.violated == 1


def test_build_summary_distinguishes_expected_from_unexpected_skip() -> None:
    """soft_deleted (pin-state) skips are expected; any other reason is
    unexpected and alert-worthy."""
    skipped = [
        GuardrailSkip(name="pinned-ghost", reason="soft_deleted"),
        GuardrailSkip(name="evaded", reason="cap_evaded"),
    ]
    summary = build_guardrail_summary(bound=2, definitions=[], results=[], skipped=skipped)
    assert summary.skipped == 2
    assert summary.expected_skips == 1
    assert summary.unexpected_skips == 1


def test_build_summary_errored_absorbs_no_clean_detection() -> None:
    """A run blocked BEFORE any detection (cap violation / conformance block)
    has zero results — errored absorbs every bound guardrail so the invariant
    still holds."""
    summary = build_guardrail_summary(bound=5, definitions=[], results=[], skipped=[])
    assert summary.errored == 5
    assert summary.evaluated + summary.errored + summary.skipped == summary.bound


# ---------------------------------------------------------------------------
# Persistable round-trip
# ---------------------------------------------------------------------------


def test_summary_to_dict_and_from_mapping_round_trip() -> None:
    summary = GuardrailSummary(
        bound=3,
        evaluated=2,
        passed=1,
        violated=1,
        observed=1,
        errored=0,
        redacted=0,
        skipped=1,
        expected_skips=1,
    )
    data = summary.to_dict()
    assert data["unexpected_skips"] == 0
    assert GuardrailSummary.from_mapping(data) == summary


def test_summary_from_mapping_rejects_malformed() -> None:
    with pytest.raises(ValueError, match="malformed guardrail summary"):
        GuardrailSummary.from_mapping({"bound": "not-an-int"})
    with pytest.raises(ValueError, match="must be a mapping"):
        GuardrailSummary.from_mapping(None)
    with pytest.raises(ValueError, match="malformed guardrail summary"):
        GuardrailSummary.from_mapping({"bound": 1})


# ---------------------------------------------------------------------------
# Per-pattern fired-signature regression (item 11, 4c)
# ---------------------------------------------------------------------------


def test_fired_signature_log_emits_per_pattern(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="modulo.core.guardrails")
    regex_def = _def("regex-guard", "block")
    schema_def = _def(
        "schema-guard",
        "block",
        config={"type": "json_schema", "field": "body", "schema": {"type": "object"}},
    )
    error_def = _def("error-guard", "observe")
    results = [
        _result(regex_def, passed=True, detail="regex matched: /SECRET_[A-Z0-9]{8}/ on body"),
        _result(schema_def, passed=False, detail="json_schema validation failed on field 'body'"),
        _result(error_def, passed=False, detail="guardrail 'error-guard' mechanism error: detection error: boom"),
    ]
    defs = [regex_def, schema_def, error_def]
    log_guardrail_fired_signatures(org_id=_ORG, run_id=_RUN, definitions=defs, results=results)

    records = [r for r in caplog.records if r.message == "guardrails.fired_signature"]
    # The mechanism-error row carries no detection signature — only 2 fire.
    assert len(records) == 2
    by_name = {r.guardrail: r for r in records}
    assert by_name["regex-guard"].fired is True
    assert by_name["schema-guard"].fired is True
    assert by_name["regex-guard"].pattern_hash == guardrail_pattern_hash(defs, regex_def.id)
    assert len(by_name["regex-guard"].pattern_hash) == 12
    assert by_name["schema-guard"].pattern_hash != by_name["regex-guard"].pattern_hash


def test_pattern_hash_is_deterministic_and_empty_for_unknown() -> None:
    regex_def = _def("regex-guard", "block")
    defs = [regex_def]
    first = guardrail_pattern_hash(defs, regex_def.id)
    second = guardrail_pattern_hash(defs, regex_def.id)
    assert first == second
    assert not guardrail_pattern_hash(defs, uuid.uuid4())


# ---------------------------------------------------------------------------
# Unexpected-skip alert (item 11, 4b)
# ---------------------------------------------------------------------------


async def test_alert_unexpected_guardrail_skip_fires(monkeypatch: pytest.MonkeyPatch) -> None:
    notified: list[dict[str, Any]] = []

    async def _fake_notify(org_id: uuid.UUID, event_type: str, payload: dict[str, Any], **kwargs: Any) -> None:
        notified.append({"event_type": event_type, "payload": payload})

    monkeypatch.setattr(guardrails_module, "notify_guardrail_event", _fake_notify)
    await alert_unexpected_guardrail_skip(_ORG, _RUN, GuardrailSkip(name="evaded", reason="cap_evaded"))

    assert notified
    assert notified[0]["event_type"] == "guardrail_unexpected_skip"
    assert notified[0]["payload"]["guardrail"] == "evaded"
    assert notified[0]["payload"]["reason"] == "cap_evaded"


async def test_alert_unexpected_guardrail_skip_is_best_effort(monkeypatch: pytest.MonkeyPatch) -> None:
    """A dispatch failure never propagates — the alert is observability."""

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("notifier unavailable")

    monkeypatch.setattr("modulo.core.notifier.Notifier", _boom)
    # Must not raise, even though the notifier cannot be built.
    await alert_unexpected_guardrail_skip(_ORG, _RUN, GuardrailSkip(name="evaded", reason="cap_evaded"))
