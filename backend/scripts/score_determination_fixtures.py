#!/usr/bin/env python3
"""Determination fixture scorer — runs the determination pipeline against fixtures
and compares the generated pipeline draft to the expected answer key.

Exit code: 0 if all fixtures match, 1 if any mismatch.
"""

import json
import sys
import uuid
from pathlib import Path
from typing import Any

from modulo.connectors.base import ConnectorType
from modulo.determination.draft import PipelineDraft, generate_draft
from modulo.determination.inference import Finding, infer
from modulo.determination.scanner import ScanSample

FIXTURES_DIR = Path(__file__).parent.parent / "tests" / "determination" / "fixtures"


def load_fixture(fixture_dir: Path) -> tuple[list[ScanSample], dict[str, Any]]:
    """Load input samples and expected draft from a fixture directory."""
    input_path = fixture_dir / "input" / "scan_samples.json"
    expected_path = fixture_dir / "expected" / "pipeline_draft.json"

    with input_path.open() as f:
        input_data = json.load(f)

    with expected_path.open() as f:
        expected = json.load(f)

    samples = []
    for s in input_data["samples"]:
        # Parse connector_id as UUID
        connector_id = uuid.UUID(s["connector_id"])
        connector_type = ConnectorType(s["connector_type"])
        samples.append(
            ScanSample(
                connector_id=connector_id,
                connector_type=connector_type,
                resource=s["resource"],
                records=s["records"],
                sample_count=s["sample_count"],
                error=s.get("error"),
            )
        )

    return samples, expected


def draft_to_dict(draft: PipelineDraft) -> dict[str, Any]:
    """Convert a PipelineDraft to a comparable dict."""
    return {
        "nodes": [
            {
                "id": n.id,
                "node_type": n.node_type,
                "label": n.label,
                "connector_type": n.connector_type,
                "required_capabilities": n.required_capabilities,
            }
            for n in draft.nodes
        ],
        "edges": [
            {
                "source": e.source,
                "target": e.target,
                "edge_type": e.edge_type,
                "hitl_review": e.hitl_review,
            }
            for e in draft.edges
        ],
        "automation_suggestions": draft.automation_suggestions,
    }


def compare_nodes(actual: list[dict[str, Any]], expected: list[dict[str, Any]]) -> list[str]:
    """Compare node lists, return list of mismatch descriptions."""
    mismatches = []
    actual_by_id = {n["id"]: n for n in actual}
    expected_by_id = {n["id"]: n for n in expected}

    # Check for missing/extra nodes
    actual_ids = set(actual_by_id.keys())
    expected_ids = set(expected_by_id.keys())

    for missing in expected_ids - actual_ids:
        mismatches.append(f"Missing node: {missing}")
    for extra in actual_ids - expected_ids:
        mismatches.append(f"Extra node: {extra}")

    # Compare common nodes
    for node_id in actual_ids & expected_ids:
        a = actual_by_id[node_id]
        e = expected_by_id[node_id]
        for key in ("node_type", "label", "connector_type", "required_capabilities"):
            if a.get(key) != e.get(key):
                mismatches.append(f"Node {node_id}.{key}: expected {e.get(key)!r}, got {a.get(key)!r}")

    return mismatches


def compare_edges(actual: list[dict[str, Any]], expected: list[dict[str, Any]]) -> list[str]:
    """Compare edge lists, return list of mismatch descriptions."""
    mismatches = []

    # Normalize edges for comparison (order doesn't matter)
    actual_set = {(e["source"], e["target"], e["edge_type"], e["hitl_review"]) for e in actual}
    expected_set = {(e["source"], e["target"], e["edge_type"], e["hitl_review"]) for e in expected}

    for missing in expected_set - actual_set:
        mismatches.append(f"Missing edge: {missing}")
    for extra in actual_set - expected_set:
        mismatches.append(f"Extra edge: {extra}")

    return mismatches


def compare_automation_suggestions(actual: list[dict[str, Any]], expected: list[dict[str, Any]]) -> list[str]:
    """Compare automation suggestions (order-insensitive, key fields)."""
    mismatches = []

    actual_set = {(s.get("stage"), s.get("connector_type"), s.get("suggestion")) for s in actual}
    expected_set = {(s.get("stage"), s.get("connector_type"), s.get("suggestion")) for s in expected}

    for missing in expected_set - actual_set:
        mismatches.append(f"Missing automation suggestion: {missing}")
    for extra in actual_set - expected_set:
        mismatches.append(f"Extra automation suggestion: {extra}")

    return mismatches


def compare_findings(actual_findings: list[Finding], expected: dict[str, Any]) -> list[str]:
    """Compare findings by checking expected category/keyword presence."""
    mismatches = []

    actual_categories = {f.category for f in actual_findings}
    expected_categories = set(expected.get("expected_findings_categories", []))

    for missing in expected_categories - actual_categories:
        mismatches.append(f"Missing findings category: {missing}")

    # Check for expected keyword patterns in findings
    for category, expected_keywords in [
        ("stage", expected.get("expected_stage_findings", [])),
        ("automation", expected.get("expected_automation_findings", [])),
        ("bottleneck", expected.get("expected_bottleneck_findings", [])),
        ("transition", expected.get("expected_transition_findings", [])),
        ("overview", expected.get("expected_overview_findings", [])),
    ]:
        category_findings = [f for f in actual_findings if f.category == category]
        for keyword in expected_keywords:
            if not any(keyword in f.finding for f in category_findings):
                mismatches.append(f"Missing expected {category} finding containing: {keyword!r}")

    return mismatches


def score_fixture(fixture_dir: Path) -> tuple[bool, list[str]]:
    """Score a single fixture. Returns (passed, mismatch_list)."""
    samples, expected = load_fixture(fixture_dir)

    # Run determination pipeline
    findings = infer(samples)
    draft = generate_draft(samples, findings)

    actual = draft_to_dict(draft)

    all_mismatches = []
    all_mismatches.extend(compare_nodes(actual["nodes"], expected["nodes"]))
    all_mismatches.extend(compare_edges(actual["edges"], expected["edges"]))
    all_mismatches.extend(
        compare_automation_suggestions(actual["automation_suggestions"], expected["automation_suggestions"])
    )
    all_mismatches.extend(compare_findings(findings, expected))

    passed = len(all_mismatches) == 0
    return passed, all_mismatches


def main() -> int:
    if not FIXTURES_DIR.exists():
        print(f"ERROR: Fixtures directory not found: {FIXTURES_DIR}", file=sys.stderr)
        return 1

    fixture_dirs = sorted([d for d in FIXTURES_DIR.iterdir() if d.is_dir()])
    if not fixture_dirs:
        print("ERROR: No fixture directories found", file=sys.stderr)
        return 1

    print(f"Found {len(fixture_dirs)} fixture(s): {[d.name for d in fixture_dirs]}")
    print()

    all_passed = True
    for fixture_dir in fixture_dirs:
        print(f"Scoring {fixture_dir.name}...")
        passed, mismatches = score_fixture(fixture_dir)

        if passed:
            print("  ✓ PASS")
        else:
            print("  ✗ FAIL")
            for m in mismatches:
                print(f"    - {m}")
            all_passed = False
        print()

    if all_passed:
        print("All fixtures PASSED")
        return 0
    print("Some fixtures FAILED")
    return 1


if __name__ == "__main__":
    sys.exit(main())
