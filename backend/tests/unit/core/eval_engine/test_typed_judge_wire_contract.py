"""Wire-contract proof for the frontend typed-judge builder (ADR 066 D4).

The builder's payload lives in ONE shared artefact,
``frontend/src/__tests__/fixtures/typed-judge-payload.json``:

* ``frontend/src/__tests__/typedJudge.spec.ts`` proves ``buildTypedJudgeConfig``
  emits each fixture exactly (a frontend-side round-trip);
* this module proves the real write-time validator
  (:func:`validate_typed_judge_config`) accepts those exact fixtures.

Together they are a genuine contract round-trip rather than a hand-maintained
mirror: a server-schema change that rejects the builder's output fails here,
and a builder change that drifts from the accepted shape fails the frontend
test. The fixture also pins ``cascade`` and a decision-model
``backing.model_class`` so the editor can never silently drop them.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from modulo.core.eval_engine.typed_judge.config import validate_typed_judge_config

_REPO_ROOT = Path(__file__).resolve().parents[5]
_FIXTURE = _REPO_ROOT / "frontend" / "src" / "__tests__" / "fixtures" / "typed-judge-payload.json"


def _payloads() -> dict[str, dict[str, object]]:
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))


def test_shared_wire_fixture_exists() -> None:
    """The shared artefact must be present (a missing fixture is not a pass)."""
    assert _FIXTURE.is_file(), f"shared typed-judge wire fixture is missing: {_FIXTURE}"


def test_fixture_pins_cascade_and_decision_model_class() -> None:
    """The artefact itself must carry the two keys the editor used to drop."""
    payloads = _payloads()
    assert "cascade" in payloads["modelBackendJudge"]
    assert payloads["decisionModelJudge"]["backing"]["model_class"] == "system_one"
    assert "cascade" in payloads["decisionModelJudge"]


@pytest.mark.parametrize("name", ["modelBackendJudge", "decisionModelJudge"])
def test_frontend_builder_payload_is_accepted_by_the_write_validator(name: str) -> None:
    """The exact payload the builder emits passes the real server schema."""
    validate_typed_judge_config(_payloads()[name])
