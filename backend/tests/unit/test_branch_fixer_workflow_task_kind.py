"""Structural gate: branch-fixer.yml carries a STRUCTURED ``taskKind`` (FAR-2039).

The scheduled PR Housekeeper relay dispatches this workflow, and for
conflict-class work it used to encode ``[phase=merge-conflict]`` INSIDE the
feedback prose. The Branch Fixer selects its task profile from
``input.taskKind``, so those dispatches arrived with no task kind and inherited
the CI-failure profile - the same defect FAR-2033 fixed for the two
conflict-class producers in merge-queue.yml, one relay layer out.

Only a live scheduled relay tick can prove the field crosses the webhook
boundary into the receiver, and CI never produces one. These tests are the gate
that keeps both halves honest:

* the relay accepts ``taskKind`` and forwards it as a payload key; and
* a caller that omits it still sends the payload it always sent - no key and no
  behaviour change, because this adds a capability rather than changing a
  default.

The task kind must remain a structured input: deriving it from the feedback
string is forbidden, because a substring parse breaks silently the moment the
prose is reworded.
"""

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "branch-fixer.yml"
WEBHOOK_STEP = "- name: Send webhook to Modulo Branch Fixer"
BASE_OBJECT = (
    "{branchName: $branchName, prNumber: $prNumber, failureDescription: $failureDescription, repository: $repo}"
)


def _raw() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def _workflow() -> dict:
    return yaml.safe_load(_raw())


def _dispatch_inputs() -> dict:
    # PyYAML resolves the bare ``on:`` key to True under YAML 1.1.
    triggers = _workflow().get(True) or _workflow().get("on")
    return triggers["workflow_dispatch"]["inputs"]


def _webhook_block() -> str:
    """Raw text of the webhook step, up to the next step at the same indentation."""
    raw = _raw()
    start = raw.index(WEBHOOK_STEP)
    rest = raw[start + len(WEBHOOK_STEP) :]
    end = rest.find("\n      - name: ")
    return rest if end == -1 else rest[:end]


def test_workflow_file_exists_and_parses() -> None:
    assert WORKFLOW.exists(), f"Workflow file missing: {WORKFLOW}"
    assert isinstance(_workflow(), dict), "YAML root is not a mapping"


def test_relay_declares_an_optional_task_kind_input() -> None:
    """The relay could only receive pr_number/branch/feedback, so a conflict
    dispatch had nowhere to put the task kind except the feedback prose."""
    inputs = _dispatch_inputs()
    assert "taskKind" in inputs, "branch-fixer.yml declares no taskKind input (FAR-2039)"
    assert inputs["taskKind"]["type"] == "string", "taskKind must be a string input"
    assert not inputs["taskKind"]["required"], "taskKind must stay optional so existing callers are unaffected"


def test_relay_forwards_task_kind_from_the_dispatch_input() -> None:
    block = _webhook_block()
    assert "TASK_KIND: ${{ inputs.taskKind }}" in block, "webhook step does not read inputs.taskKind"
    assert '--arg taskKind "${TASK_KIND}"' in block, "taskKind is not passed to jq"


def test_task_kind_is_added_only_when_set_so_absent_is_unchanged() -> None:
    """`+ {}` leaves the object - and its serialisation - byte-identical, which
    is what makes an omitted taskKind behaviour-preserving rather than a
    silent default change."""
    block = _webhook_block()
    assert BASE_OBJECT in block, "the always-sent payload shape changed"
    assert 'if $taskKind == "" then {} else {taskKind: $taskKind} end' in block, (
        "taskKind must be added conditionally, so an absent value sends the old payload"
    )
    assert block.count("taskKind: $taskKind") == 1, "taskKind must be added once, inside the conditional"


def test_relay_refuses_an_unknown_task_kind_loudly() -> None:
    """A typo must not silently downgrade a conflict dispatch to the CI-failure
    profile - that silent misclassification is the defect being fixed."""
    block = _webhook_block()
    assert 'case "${TASK_KIND}" in' in block, "no guard validating the taskKind vocabulary"
    assert '""|conflict)' in block, "the accepted taskKind vocabulary is not empty|conflict"
    assert "::error::Unsupported taskKind" in block, "an unknown taskKind must fail loudly"


def test_task_kind_is_never_derived_from_the_feedback_prose() -> None:
    """A substring marker in feedback breaks silently when the prose is
    reworded; the task kind must stay a structured field."""
    raw = _raw()
    assert "phase=merge-conflict" not in raw, "the relay embeds or parses the prose phase marker"
    passthrough_only = _webhook_block()
    passthrough_only = passthrough_only.replace("FEEDBACK: ${{ inputs.feedback }}", "")
    passthrough_only = passthrough_only.replace('--arg failureDescription "${FEEDBACK}"', "")
    assert "FEEDBACK" not in passthrough_only, "the feedback text is inspected instead of passed through"
