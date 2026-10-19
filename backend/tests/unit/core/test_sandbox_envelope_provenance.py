"""FAR-1780 / ADR-042 amendment: sandbox_agent BYO envelope datums are typed
``reported`` — per datum, engine measurements are never stamped.

The sandbox envelope is the BYO surface where agent-claimed and engine-observed
facts live side by side: the producer's ``output.json`` report (verbatim
``output_json``, the LLM-extraction keys, the declared model cost and token
usage) next to Modulo's own measurements (``exit_code``, ``wall_clock_time_ms``,
``status``, the estimated sandbox infra cost). The whole-dict stamp the
dispatch arm applies would over-claim the engine side, so the envelope types
its agent-claimed datums under the ONE ``provenance`` key as a PER-DATUM map
(``{datum_name: PROVENANCE_REPORTED}``) and leaves every engine-observed datum
unstamped (``core.provenance.pipeline_engine.node_runner``.

This module pins:

1. the stamp lands in BOTH envelope views (the full artifact ``output`` and
   the reduced top-level telemetry view) so every rendering / persisting
   surface sees the class;
2. script mode never claims the LLM-extraction keys (its ``summary`` is
   engine-generated) but still types ``output_json`` (the verbatim substrate
   report) and any agent-declared cost/token fields;
3. the engine-substituted failure summary is NOT an agent report;
4. engine-observed datums are never stamped (no self-attestation of Modulo's
   own measurements — the taxonomy over-claim failure mode);
5. the stamps survive the P1b splitter into ``node_telemetry_json`` shape,
   and an envelope with nothing to claim carries no ``provenance`` key at all;
6. the cost breakdown excludes the self_reported component from the summed
   total (declared money never becomes system money).

No DB and no network.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from modulo.core.cost_controller.breakdown.aggregate import build_cost_breakdown
from modulo.core.cost_controller.breakdown.params import CostComponentConfig, RunCostTelemetry
from modulo.core.node_output_split import _split_sandbox_agent
from modulo.core.pipeline_engine.node_runner import (
    _build_model_cost_fields,
    _build_sandbox_node_envelope,
    _build_token_usage_fields,
    _SandboxNodeOutput,
)
from modulo.core.provenance import PROVENANCE_KEY, PROVENANCE_REPORTED

_COST_JSON: dict[str, Any] = {
    "model_cost_usd": 0.04,
    "schema_drift": False,
    "token_usage": {"input": 100, "output": 200, "total": 300},
}
_COST_NODE_FIELDS: dict[str, Any] = {
    **_build_model_cost_fields(_COST_JSON),
    **_build_token_usage_fields(_COST_JSON),
}
# The ENGINE-COMPUTED cost annotations (_build_model_cost_fields writes them
# UNCONDITIONALLY per report — display clamp + explicit flags), never tagged
# as agent claims in the envelope map (see node_runner's
# _SANDBOX_AGENT_COST_DATUM_FIELDS comment + finalize's
# _UNION_PROVENANCE_DATUM_KEYS, which excludes the same three).
_ENGINE_COST_KEYS: frozenset[str] = {"model_cost_display_usd", "model_cost_clamped", "model_cost_out_of_band_high"}
# The agent-claimed subset of the extracted cost/token datums.
_AGENT_CLAIMED_COST_KEYS: frozenset[str] = frozenset(_COST_NODE_FIELDS) - _ENGINE_COST_KEYS

_ENGINE_KEYS = ("status", "exit_code", "wall_clock_time_ms", "cost_estimate_usd")


def _llm_output(**overrides: Any) -> _SandboxNodeOutput:
    base: dict[str, Any] = {
        "status": "completed",
        "summary": "agent summary",
        "exit_code": 0,
        "wall_clock_time_ms": 1500,
        "cost_estimate_usd": 0.06,
        "cost_source": _COST_JSON,
        "output_json": {
            "summary": "agent summary",
            "changed_files": ["backend/a.py"],
            "pr_url": "https://x/pr/1",
            # The producer's OWN report keys — the envelope's ``agent_status``
            # / ``agent_outcome`` datums are lifted from ``status``/``outcome``
            # (see node_runner's ``_EXTRACTED_DATUM_SOURCE_KEYS``), so the
            # report never carries the envelope datum names themselves.
            "status": "success",
            "outcome": "PR created",
        },
        "changed_files": ["backend/a.py"],
        "pr_url": "https://x/pr/1",
        "agent_status": "success",
        "agent_outcome": "PR created",
        "attempt_key": "n1:0",
    }
    base.update(overrides)
    return _SandboxNodeOutput(**base)


def _llm_call(**overrides: Any) -> dict[str, Any]:
    return _build_sandbox_node_envelope(
        node_id="n1",
        output=_llm_output(**overrides),
        provider="opencode-go",
        via_provider=True,
        sandbox_mode="llm",
    )


def _report() -> dict[str, Any]:
    """The artifact ``output`` view of an llm-mode envelope."""
    return _llm_call()["artifacts"][0]["output"]


def test_stamp_rides_both_split_views() -> None:
    """After the P1b split the map lives on ``node_telemetry_json``'s shape.

    ``_split_sandbox_agent``'s return value is the producer's verbatim
    ``output_json`` (its own report), NOT an envelope view — the typed
    map belongs to the ENVELOPE datums and therefore rides the telemetry
    view (built from BOTH the outer ``output`` and the inner artifact
    fields), which is what ``node_telemetry_json`` persists.
    """
    envelope = _llm_call()
    return_value, telemetry = _split_sandbox_agent(envelope)
    assert return_value is envelope["artifacts"][0]["output"]["output_json"]
    assert PROVENANCE_KEY in telemetry


def test_reported_map_names_exactly_the_agent_claimed_datums() -> None:
    prov = _report()[PROVENANCE_KEY]
    expected_extraction = {
        "output_json",
        "summary",
        "changed_files",
        "pr_url",
        "agent_status",
        "agent_outcome",
    }
    assert expected_extraction <= set(prov)
    assert prov["summary"] == PROVENANCE_REPORTED
    # cost/token fields: the AGENT-CLAIMED datums (declared cost + raw) are
    # stamped per presence of the extraction betas; the ENGINE-COMPUTED
    # annotations are NEVER stamped (attesting them would over-claim engine
    # derivations as producer claims).
    assert set(prov) >= _AGENT_CLAIMED_COST_KEYS
    assert all(prov[k] == PROVENANCE_REPORTED for k in _AGENT_CLAIMED_COST_KEYS)
    for engine_cost_key in _ENGINE_COST_KEYS:
        assert engine_cost_key not in prov


def test_engine_observed_keys_are_never_stamped() -> None:
    prov = _report()[PROVENANCE_KEY]
    for engine_key in _ENGINE_KEYS:
        assert engine_key not in prov


def test_map_is_per_datum_not_a_whole_dict_value() -> None:
    """Every value is the class string for ITS datum; the map is flat."""
    prov = _report()[PROVENANCE_KEY]
    assert all(isinstance(v, str) for v in prov.values())
    assert set(prov.values()) == {PROVENANCE_REPORTED}


def test_script_mode_never_claims_the_extraction_keys() -> None:
    output = _llm_output()
    envelope = _build_sandbox_node_envelope(node_id="n1", output=output, sandbox_mode="script")
    prov = envelope["artifacts"][0]["output"][PROVENANCE_KEY]
    for extraction_key in ("summary", "changed_files", "pr_url", "agent_status", "agent_outcome"):
        assert extraction_key not in prov
    # substrate's verbatim report + its declared cost stay claimed (the
    # agent-claimed datums only — the engine-computed annotations stay
    # unstamped in every mode).
    assert prov["output_json"] == PROVENANCE_REPORTED
    assert set(prov) >= _AGENT_CLAIMED_COST_KEYS
    for engine_cost_key in _ENGINE_COST_KEYS:
        assert engine_cost_key not in prov


def test_engine_substituted_failure_summary_is_not_claimed() -> None:
    output = _llm_output(status="failed", summary="", output_json={"summary": ""})
    envelope = _build_sandbox_node_envelope(
        node_id="n1",
        output=output,
        provider="opencode-go",
        via_provider=True,
        sandbox_mode="llm",
    )
    prov = envelope["artifacts"][0]["output"][PROVENANCE_KEY]
    assert "summary" not in prov


def test_envelope_with_no_reports_carries_no_provenance_key() -> None:
    output = _SandboxNodeOutput(
        status="failed",
        summary="",
        exit_code=1,
        wall_clock_time_ms=100,
        cost_estimate_usd=0.01,
        cost_source=None,
        attempt_key="n1:0",
    )
    envelope = _build_sandbox_node_envelope(node_id="n1", output=output)
    inner = envelope["artifacts"][0]["output"]
    assert PROVENANCE_KEY not in inner
    assert PROVENANCE_KEY not in envelope["output"]


def test_no_caller_stamp_argument_is_accepted() -> None:
    """The wire never asserts a provenance class (ADR-034 rule, reused).

    The stamp is computed from the builder's own classification; a hostile
    ``provenance`` key riding _inside_ ``output_json`` (the producer's own
    body) cannot affect the envelope's map — the map's keys are engine-chosen
    datum names, not producer-controlled keys. The report matches the
    envelope's summary value so the summary equality rule stamps, leaving
    exactly ONE hostile key the engine would never name a datum after.
    """
    output = _llm_output(
        output_json={
            "summary": "agent summary",
            "changed_files": ["backend/a.py"],
            "pr_url": "https://x/pr/1",
            "status": "success",
            "outcome": "PR created",
            PROVENANCE_KEY: "attested",
        }
    )
    envelope = _build_sandbox_node_envelope(node_id="n1", output=output, sandbox_mode="llm")
    prov = envelope["artifacts"][0]["output"][PROVENANCE_KEY]
    assert prov["summary"] == PROVENANCE_REPORTED
    assert PROVENANCE_KEY in envelope["artifacts"][0]["output"]["output_json"]
    assert PROVENANCE_KEY not in prov


# ---------------------------------------------------------------------------
# FAR-1780: the breakdown side — declared cost never sums into the total
# ---------------------------------------------------------------------------


def test_self_reported_component_is_excluded_from_breakdown_total() -> None:
    tele = RunCostTelemetry(
        wall_clock_elapsed_s=Decimal(720),
        reported={"model_cost_usd": Decimal("0.04")},
        raw_reported={"node1": 0.0412},
        eligible_sandbox_node_count=1,
    )
    components = [
        CostComponentConfig(
            name="sandbox_infra",
            display_name="Sandbox Infrastructure",
            kind="calculated",
            rate_usd=Decimal("1.0"),
            formula="rate * wall_clock_hours",
        ),
        CostComponentConfig(
            name="model_tokens",
            display_name="Model cost (self-reported)",
            kind="self_reported",
            report_key="model_cost_usd",
        ),
    ]
    breakdown, total = build_cost_breakdown(tele, components)
    by_component = {entry["component"]: entry for entry in breakdown}
    declared = by_component["model_tokens"]
    assert declared["provenance"] == "reported"
    assert declared["billable"] is False
    assert declared["amount_usd"] == "0.040000"
    # The engine-observed sandbox bill is the only money in the total.
    assert total == Decimal("0.200000")


def test_declared_only_run_totals_zero() -> None:
    """A run whose ONLY cost content is the agent's declared report bills
    nothing: the breakdown carries the (non-billable) declared component for
    display, and the TOTAL is zero — spend gates (which consume the total)
    approve a declared-only run."""
    tele = RunCostTelemetry(
        wall_clock_elapsed_s=Decimal(0),
        reported={"model_cost_usd": Decimal("0.04")},
        raw_reported={"node1": 0.0412},
        eligible_sandbox_node_count=1,
    )
    components = [
        CostComponentConfig(
            name="model_tokens",
            display_name="Model cost (self-reported)",
            kind="self_reported",
            report_key="model_cost_usd",
        ),
    ]
    breakdown, total = build_cost_breakdown(tele, components)
    assert len(breakdown) == 1
    assert breakdown[0]["component"] == "model_tokens"
    assert breakdown[0]["amount_usd"] == "0.040000"
    assert total == Decimal("0.000000")
