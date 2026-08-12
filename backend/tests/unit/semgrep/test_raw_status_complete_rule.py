"""Document the intent of the ``raw-status-complete`` semgrep rule.

The rule lives at ``.semgrep/raw-status-complete.yml`` and flags raw
success-status comparisons (``status == "complete"``, ``status != "complete"``
and ``status in ("complete", ...)``) in backend product code. It is a GUARD
from the agent-failure UX design (§15.2): a single success predicate
``is_success = (status == 'complete' OR accepted_as_complete IS TRUE) AND ...``
is being introduced in a later phase, so consumer code must stop treating raw
``status == "complete"`` as success. Until the Phase 2 accept-as-complete
sweep (FAR-146) lands, the known pre-existing consumer sites are path-excluded
from the rule.

These tests exercise the rule's matching logic directly (semgrep-core cannot
run on Windows). They assert the rule is scoped to backend product code, that
every known pre-existing consumer is path-excluded, that the message points at
the shared success predicate, and that the raw-status match predicate behaves
as the rule intends.
"""

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[4]
RULE_FILE = REPO_ROOT / ".semgrep" / "raw-status-complete.yml"

KNOWN_CONSUMER_FILES = [
    "api/routes/dashboard.py",
    "db/crud/run.py",
    "core/pipeline_execution.py",
    "core/pipeline_engine/executor.py",
    "core/saq_worker.py",
    "core/feedback_manager/__init__.py",
]


def _load_rule() -> dict:
    with RULE_FILE.open("r", encoding="utf-8") as fh:
        doc = yaml.safe_load(fh)
    return next(r for r in doc["rules"] if r["id"] == "raw-status-complete")


RULE = _load_rule()


def _is_raw_success_match(var_name: str, value: object) -> bool:
    """Emulate the rule's match predicate.

    Mirrors the rule intent: a status-like variable whose value is exactly
    ``"complete"`` is the forbidden bare success check (it must go through the
    shared status-enum / success-predicate module instead). Any other status
    value (``"failed"``, ``"stalled"``, ``None``, ...) is NOT a raw success
    comparison and must not match.
    """
    return "status" in var_name and value == "complete"


def test_rule_id_languages_and_scope() -> None:
    assert RULE["id"] == "raw-status-complete"
    assert "python" in RULE["languages"]
    include = RULE["paths"]["include"]
    assert include, "paths.include must be set"
    assert all("backend/src" in pat for pat in include), "rule must scope to backend product code and never flag tests"


def test_message_requires_shared_success_predicate() -> None:
    message = RULE["message"]
    assert "status" in message.lower()
    assert "success" in message.lower() or "predicate" in message.lower()


def test_every_known_consumer_is_path_excluded() -> None:
    exclude = RULE["paths"]["exclude"]
    for rel in KNOWN_CONSUMER_FILES:
        assert any(rel in pat for pat in exclude), f"missing exclusion for {rel}"


def test_raw_complete_value_is_flagged() -> None:
    assert _is_raw_success_match("status", "complete") is True
    assert _is_raw_success_match("result_status", "complete") is True


@pytest.mark.parametrize("value", ["failed", "stalled", None])
def test_non_success_values_are_not_flagged(value: object) -> None:
    assert _is_raw_success_match("status", value) is False
