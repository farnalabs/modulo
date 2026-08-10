"""Guard: the shared _conformance helper module must stay byte-identical.

``tests/connectors/_conformance.py`` has a sibling copy under
``backend/tests/connectors/``. The module docstring mandates the copies stay
byte-identical. This test enforces that invariant so silent drift is caught in
CI instead of surfacing as confusing import mismatches.
"""

from pathlib import Path


def test_conformance_helper_copies_are_byte_identical() -> None:
    local = Path(__file__).resolve().parent / "_conformance.py"
    sibling = Path(__file__).resolve().parents[3] / "tests" / "connectors" / "_conformance.py"
    assert sibling.is_file(), f"Missing sibling copy at {sibling}"
    assert local.read_bytes() == sibling.read_bytes(), (
        f"Conformance helper copies drifted:\n  {local}\n  {sibling}\n"
        "Apply identical changes to both copies (they must stay byte-identical)."
    )
