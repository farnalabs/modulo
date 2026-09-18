"""Unit tests for the pre-deploy Fly machine-id parser.

These prove the deploy script extracts the launched machine id from the
real ``fly machine run`` (flyctl v0.4.87) human output (" Machine ID: <id>")
rather than a non-existent ``--json`` document. CI does not exercise
deploy.yml itself (it only runs on push to main), so this test is the only
gate that keeps the parser correct.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make the deploy/ helper importable without installing it as a package.
REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "deploy" / "fly"))

from extract_machine_id import extract_machine_id  # noqa: E402

# Exact flyctl v0.4.87 launch line (fmt.Fprintf(io.Out, " Machine ID: %s\n", id)).
V0_4_87_LAUNCH = " Machine ID: 3dXg9aB2cF1eH7k\n"

# Realistic multi-line launch output with leading provisioning chatter.
V0_4_87_LAUNCH_MULTILINE = (
    "=> Provisioning a new machine\n"
    "=> Creating a new machine\n"
    " Machine ID: 8kLm2NpQ4rSt6UvW\n"
    "Attempting to start instance\n"
)


def test_exact_v0_4_87_line() -> None:
    assert extract_machine_id(V0_4_87_LAUNCH) == "3dXg9aB2cF1eH7k"


def test_multiline_v0_4_87_output() -> None:
    assert extract_machine_id(V0_4_87_LAUNCH_MULTILINE) == "8kLm2NpQ4rSt6UvW"


def test_id_with_no_leading_space() -> None:
    # Defensive: some flyctl builds omit the leading space.
    assert extract_machine_id("Machine ID: aB1cD2eF3gH4") == "aB1cD2eF3gH4"


def test_no_machine_id_returns_none() -> None:
    assert extract_machine_id("launch failed: image not found\n") is None
    assert extract_machine_id("") is None


def test_machine_id_only_alphanumeric() -> None:
    # The id stops at the first non-alphanumeric char (e.g. trailing newline/space).
    assert extract_machine_id(" Machine ID: AbC123\n") == "AbC123"
    assert extract_machine_id(" Machine ID: AbC123 ") == "AbC123"
