"""Unit tests for the incomplete-scan detection in scripts/run_semgrep.py.

Semgrep exits **0** when a per-rule match times out on a file: it SKIPS that
file and prints only ``Warning: N timeout error(s) ...``. A gate that reads
the exit code alone therefore reports green on an incomplete scan — the exact
silent-decay class FAR-1859 exists to close.

The wrapper detects this two ways: primarily from the structured
``--json-output`` payload (an ``errors`` entry of ``type == "Timeout"``), and
secondarily from ANCHORED text signatures. These tests pin both detectors,
including the mislabel the anchoring prevents: an unanchored ``"timeout
error"`` substring matches a FINDING that merely quotes the phrase
(``pipelines.py`` contains the literal ``dispatch.wait_timeout error``), which
would report a clean scan as incomplete. The final test asserts the old bare
substring *would* have mislabelled that text — so this suite fails if the
detector is reverted to it (prove-the-fix).

The module is loaded from the repo-root ``scripts/`` directory (not a package)
via ``SourceFileLoader``, matching the sibling ``tests/unit/scripts/`` suite
(e.g. ``test_run_validate_semgrep_rules.py``).
"""

from __future__ import annotations

import json
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path

for parent in Path(__file__).resolve().parents:
    script_path = parent / "scripts" / "run_semgrep.py"
    if script_path.exists():
        break
else:
    raise RuntimeError("Could not find repo root (scripts/run_semgrep.py)")

_loader = SourceFileLoader("run_semgrep", str(script_path))
mod = module_from_spec(spec_from_loader("run_semgrep", _loader))
_loader.exec_module(mod)

#: The two real skip signatures, verbatim from the pinned semgrep's output.
_TIMEOUT_WARNING = (
    "Warning: 1 timeout error(s) in backend/src/modulo/api/mcp_server.py when running the following rules: "
    "[semgrep.split-on-db-url]\n"
)
_TIMEOUT_STOPPED = (
    "Semgrep stopped running rules on backend/src/modulo/api/mcp_server.py after 1 timeout error(s). "
    "See `--timeout-threshold` for more info.\n"
)
#: A FINDING excerpt that quotes the phrase: must NOT be read as a skip.
_FINDING_QUOTING_PHRASE = (
    "backend/src/modulo/api/routes/pipelines.py:1617: dispatch.wait_timeout error not surfaced to the caller\n"
)
#: The non-fatal parse skip mcp_server.py reports on every run (FAR-1933).
_PARTIAL_PARSING = "Partially analyzed backend/src/modulo/api/mcp_server.py (PEP 695 parse skip)\n"


def _write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


# ---------------------------------------------------------------------------
# Secondary net — anchored text signatures
# ---------------------------------------------------------------------------


def test_text_detector_matches_real_timeout_warning() -> None:
    assert mod._semgrep_skipped_files(_TIMEOUT_WARNING)


def test_text_detector_matches_real_stopped_running_rules_line() -> None:
    assert mod._semgrep_skipped_files(_TIMEOUT_STOPPED)


def test_text_detector_ignores_clean_output() -> None:
    clean = "Scan completed successfully.\nFindings: 0 (0 blocking)\nRan 47 rules on 1 file: 0 findings.\n"
    assert not mod._semgrep_skipped_files(clean)


def test_anchored_detector_does_not_match_a_finding_quoting_the_phrase() -> None:
    """The phrase inside a finding excerpt is not a skip — anchoring prevents it."""
    assert not mod._semgrep_skipped_files(_FINDING_QUOTING_PHRASE)


def test_anchored_detector_does_not_match_partially_analyzed() -> None:
    assert not mod._semgrep_skipped_files(_PARTIAL_PARSING)


# ---------------------------------------------------------------------------
# Primary detector — structured --json-output errors
# ---------------------------------------------------------------------------


def test_json_detector_true_for_timeout_error(tmp_path: Path) -> None:
    payload = {
        "errors": [{"type": "Timeout", "message": "Timeout when running semgrep.split-on-db-url on ..."}],
        "results": [],
    }
    path = tmp_path / "timeout.json"
    _write_json(path, payload)
    assert mod._semgrep_json_timeout(path)


def test_json_detector_false_for_clean_payload(tmp_path: Path) -> None:
    path = tmp_path / "clean.json"
    _write_json(path, {"errors": [], "results": []})
    assert not mod._semgrep_json_timeout(path)


def test_json_detector_false_for_partial_parsing_only(tmp_path: Path) -> None:
    """PartialParsing is expected and non-fatal — it must never fail the gate."""
    path = tmp_path / "partial.json"
    _write_json(path, {"errors": [{"type": "PartialParsing", "message": "PEP 695 parse skip"}], "results": []})
    assert not mod._semgrep_json_timeout(path)


def test_json_detector_false_when_file_missing(tmp_path: Path) -> None:
    assert not mod._semgrep_json_timeout(tmp_path / "does-not-exist.json")


def test_json_detector_false_when_payload_malformed(tmp_path: Path) -> None:
    path = tmp_path / "malformed.json"
    path.write_text("not json{", encoding="utf-8")
    assert not mod._semgrep_json_timeout(path)


# ---------------------------------------------------------------------------
# Combined detector — JSON primary, text net
# ---------------------------------------------------------------------------


def test_combined_detector_true_from_json_even_with_clean_text(tmp_path: Path) -> None:
    path = tmp_path / "timeout.json"
    _write_json(path, {"errors": [{"type": "Timeout"}], "results": []})
    assert mod._semgrep_scan_incomplete("Scan completed successfully.\n", path)


def test_combined_detector_true_from_text_when_json_absent(tmp_path: Path) -> None:
    assert mod._semgrep_scan_incomplete(_TIMEOUT_WARNING, tmp_path / "missing.json")


def test_combined_detector_false_on_clean_scan(tmp_path: Path) -> None:
    path = tmp_path / "clean.json"
    _write_json(path, {"errors": [], "results": []})
    assert not mod._semgrep_scan_incomplete("Ran 47 rules on 1 file: 0 findings.\n", path)


# ---------------------------------------------------------------------------
# Prove-the-fix: the anchored detector is what prevents the mislabel
# ---------------------------------------------------------------------------


def _legacy_skipped_files(output: str) -> bool:
    """The pre-FAR-1859 detector: an unanchored, lowercased substring test."""
    lowered = output.lower()
    return "timeout error" in lowered or "stopped running rules" in lowered


def test_anchoring_prevents_the_legacy_mislabel() -> None:
    """Prove-the-fix: the legacy substring detector mislabels a finding that
    merely quotes the phrase, while the anchored detector does not. If the
    detector is reverted to the legacy form, the second assertion fails."""
    assert _legacy_skipped_files(_FINDING_QUOTING_PHRASE)
    assert not mod._semgrep_skipped_files(_FINDING_QUOTING_PHRASE)
