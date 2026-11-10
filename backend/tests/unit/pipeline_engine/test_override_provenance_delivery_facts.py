"""Regression pins for the override-provenance rule on asserted delivery facts.

FAR-2492 — "Asserted delivery facts and PR bindings" slice of the provenance
override rule (adopted 2026-11-09). Two related contracts are pinned here.

**A. The delivery-fact vocabulary cannot express connector observation.**

``runs.run_classification`` is the platform's "did this run deliver?" record.
Its ``delivered_pr_urls`` are harvested from the run's OWN output, so they are
the AGENT's account of what happened — never something Modulo observed. That is
what ``delivery_confidence = "agent_reported"`` says, and what
``pr_url_provenance = "declared" | "matched"`` records per URL. The taxonomy is
deliberately CLOSED and contains no value that could label such a claim as
``observed`` / ``connector_observed`` / ``attested``: the vocabulary is the
structural guarantee that an agent-claimed PR URL can never be upcast into a
connector-observed fact (ADR 042 / ADR 055).

**B. The connector-observed PR surface is a SEPARATE fact, never merged into the
classifier record.**

The only connector-observed PR facts Modulo holds are the display-time
enrichment entries fetched through the org's GitHub connector
(``core.work_item_enrichment``). That surface carries live upstream state
(``state`` / ``merged``) and no delivery-confidence key at all — it is a
distinct projection, not an alternate spelling of the classifier record, so a
reader cannot mistake one for the other.

There is no path in this codebase where a HUMAN associates a PR with a run
(no route writes ``run_classification``, and ``runs.work_item_refs`` only
accepts engine-assigned provenance at create time), so a ``human_asserted``
value has no writer and is deliberately not added.
"""

from __future__ import annotations

import pytest

from modulo.core.pipeline_engine.classify import (
    DELIVERY_CONFIDENCE_AGENT_REPORTED,
    DELIVERY_CONFIDENCE_AGENT_REPORTED_LEGACY,
    DELIVERY_CONFIDENCE_VALUES,
    DELIVERY_CONFIDENCE_VERIFIED,
    PR_URL_PROVENANCE_VALUES,
    classify_run,
)
from modulo.core.work_item_enrichment import _to_item

_PR = "https://github.com/farnalabs/modulo/pull/123"

#: Provenance values that assert an OBSERVATION. None of them may be reachable
#: from a delivery claim: the classifier only ever sees the run's own output.
_OBSERVATION_CLASSES = frozenset({"observed", "connector_observed", "attested", "derived", "human_asserted"})


def _node_return_with_pr(pr_url: str) -> dict[str, object]:
    """A sandbox_agent node return — the run's structured output contract."""
    return {"pr_url": pr_url, "summary": "done"}


def _markers(pr_url: str) -> dict[str, dict[str, object]]:
    """Raw-output markers (keyed by attempt) carrying a pr_url."""
    return {
        "attempt-0": {
            "_modulo_marker": True,
            "status": "failed",
            "pr_url": pr_url,
            "parse_error": "",
            "attempt_key": "attempt-0",
        }
    }


# ---------------------------------------------------------------------------
# A. The classifier's delivery-provenance vocabulary
# ---------------------------------------------------------------------------


def test_delivery_confidence_vocabulary_is_closed_to_agent_reported() -> None:
    """Exactly three values: agent-reported, its deprecated spelling, and the
    reserved (unwritten) ``verified``. A new value — ``connector_observed`` in
    particular — fails here rather than silently becoming expressible."""
    assert {
        DELIVERY_CONFIDENCE_AGENT_REPORTED,
        DELIVERY_CONFIDENCE_AGENT_REPORTED_LEGACY,
        DELIVERY_CONFIDENCE_VERIFIED,
    } == DELIVERY_CONFIDENCE_VALUES


def test_delivery_confidence_vocabulary_cannot_express_an_observation() -> None:
    """No delivery-confidence value may name a provenance OBSERVATION class.

    This is the "never collapsed into connector_observed" guarantee: if the
    vocabulary gains such a value, a record could claim Modulo observed a
    delivery it only received as a report.
    """
    assert DELIVERY_CONFIDENCE_VALUES.isdisjoint(_OBSERVATION_CLASSES)


def test_pr_url_provenance_vocabulary_is_the_two_agent_harvest_routes_only() -> None:
    """``declared`` / ``matched`` are both AGENT-REPORTED harvest routes.

    ``declared`` = the run's own output contract asserted the URL; ``matched`` =
    the URL was merely found in emitted output. Neither is an observation, and
    neither may be replaced by one.
    """
    assert {"declared", "matched"} == PR_URL_PROVENANCE_VALUES
    assert PR_URL_PROVENANCE_VALUES.isdisjoint(_OBSERVATION_CLASSES)


@pytest.mark.parametrize(
    ("status", "error_code", "kwargs"),
    [
        ("complete", None, {"outputs_json": {"n1": _node_return_with_pr(_PR)}}),
        ("complete", None, {"raw_output_markers": _markers(_PR)}),
        ("complete", None, {}),
        ("failed", None, {"raw_output_markers": {**_markers(_PR), "x": {"delivery_done": True, "status": "ok"}}}),
        ("failed", "harness.sandbox.no_output_json", {}),
    ],
    ids=["declared", "matched-marker", "no-delivery", "delivered-then-failed", "source-error"],
)
def test_every_classifier_verdict_stamps_agent_reported(
    status: str, error_code: str | None, kwargs: dict[str, object]
) -> None:
    """No verdict — delivered, no_delivery, or a delivered-then-failed rescue —
    can be written with any confidence other than ``agent_reported``.

    Nothing cross-checks a delivered URL against an SCM of record, so stamping
    anything stronger would over-claim exactly what this record exists to
    disclose.
    """
    record = classify_run(status, error_code, **kwargs).to_dict()

    assert record["delivery_confidence"] == DELIVERY_CONFIDENCE_AGENT_REPORTED
    assert record["delivery_confidence"] != DELIVERY_CONFIDENCE_VERIFIED


# ---------------------------------------------------------------------------
# B. The connector-observed PR surface is a distinct projection
# ---------------------------------------------------------------------------


def test_connector_observed_pr_state_carries_no_delivery_provenance_keys() -> None:
    """The live connector-read PR projection holds state, never a confidence.

    Keys are pinned exactly: adding ``delivery_confidence`` (or any provenance
    key) here would merge the connector-observed surface with the agent-reported
    classifier record and erase the distinction the rule protects.
    """
    item = _to_item(
        "farnalabs/modulo#123",
        "farnalabs/modulo",
        123,
        {"title": "Fix", "state": "closed", "merged": True, "html_url": _PR},
    )

    assert set(item) == {"ref", "repo", "number", "title", "state", "merged", "html_url"}
    assert "delivery_confidence" not in item
    assert "pr_url_provenance" not in item
    # The observed upstream state is present in its own right.
    assert item["state"] == "closed"
    assert item["merged"] is True
