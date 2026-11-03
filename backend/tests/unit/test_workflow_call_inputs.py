"""Structural gate: local reusable-workflow call sites must match the callee's
``workflow_call`` contract, and a called workflow must not read
``github.event.inputs`` at all.

Inside a called workflow the ``github.event`` context belongs to the CALLER, so
``github.event.inputs`` is the caller's payload and never the callee's inputs.
``release-unified.yml`` is itself a ``workflow_dispatch`` that declares only
``mode``/``version``/``freeze``, so ``macos-installer.yml`` reading
``github.event.inputs.rc_tag`` blanked the correctly-supplied ``rc_tag`` on every
cut/recut and failed the run with "rc_tag input is required" (observed in run
41616355636); the same override also blanked ``ref`` (falling back to
``refs/heads/main``, i.e. building from main instead of the RC tag).

Nothing in CI exercises these call boundaries - the macOS and Windows legs only
run on a real release - so this test is the only gate that keeps the contract
honest. For every ``uses: ./.github/workflows/<x>.yml`` call site it asserts:

* every ``with:`` key the caller passes is declared in the callee's
  ``workflow_call.inputs``;
* every REQUIRED ``workflow_call`` input is supplied by the caller;
* the callee reads no ``github.event.inputs`` (by name or as a whole object),
  because that payload belongs to the caller.

It also executes the macOS ``normalize-inputs`` script with both entry points'
environments, so the cut/recut path is proven to resolve ``rc_tag``/``ref`` from
``inputs.*`` and to run the native build.
"""

import os
import re
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any, NamedTuple

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
MACOS_WORKFLOW = WORKFLOWS_DIR / "macos-installer.yml"

#: Every callee the release pipeline depends on. A call site disappearing means
#: the release shape changed: update this list deliberately, never silently.
EXPECTED_CALLEES = ("bundle-release.yml", "macos-installer.yml", "windows-installer.yml")

_EVENT_INPUT_DOT = re.compile(r"github\.event\.inputs\.([A-Za-z_][A-Za-z0-9_-]*)")
_EVENT_INPUT_INDEX = re.compile(r"github\.event\.inputs\[\s*['\"]([^'\"]+)['\"]\s*\]")
_EVENT_INPUT_BARE = re.compile(r"github\.event\.inputs(?![.\w\[])")
_WHOLE_INPUTS = "<whole github.event.inputs object>"


class CallSite(NamedTuple):
    caller: Path
    job_id: str
    callee_name: str
    with_inputs: dict[str, Any]


def _workflow_files() -> list[Path]:
    return sorted([*WORKFLOWS_DIR.glob("*.yml"), *WORKFLOWS_DIR.glob("*.yaml")])


def _load(path: Path) -> dict[str, Any]:
    parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(parsed, dict), f"{path.name} is not a YAML mapping"
    return parsed


def _triggers(workflow: dict[str, Any]) -> dict[str, Any]:
    # PyYAML resolves the unquoted `on` key to boolean True (YAML 1.1).
    raw = workflow.get("on", workflow.get(True))
    return raw if isinstance(raw, dict) else {}


def _declared_inputs(workflow: dict[str, Any], trigger: str) -> dict[str, Any]:
    block = _triggers(workflow).get(trigger)
    if not isinstance(block, dict):
        return {}
    inputs = block.get("inputs")
    return inputs if isinstance(inputs, dict) else {}


def _workflow_call_inputs(workflow: dict[str, Any]) -> dict[str, Any]:
    return _declared_inputs(workflow, "workflow_call")


def _dispatch_inputs(workflow: dict[str, Any]) -> dict[str, Any]:
    return _declared_inputs(workflow, "workflow_dispatch")


def _local_call_sites() -> list[CallSite]:
    sites: list[CallSite] = []
    for caller in _workflow_files():
        jobs = _load(caller).get("jobs")
        if not isinstance(jobs, dict):
            continue
        for job_id, job in jobs.items():
            if not isinstance(job, dict):
                continue
            uses = job.get("uses")
            if not isinstance(uses, str):
                continue
            if not uses.startswith("./.github/workflows/"):
                continue
            with_inputs = job.get("with")
            sites.append(
                CallSite(
                    caller=caller,
                    job_id=str(job_id),
                    callee_name=uses.rsplit("/", 1)[-1],
                    with_inputs=with_inputs if isinstance(with_inputs, dict) else {},
                )
            )
    assert sites, "no local reusable-workflow call sites found - this scan would pass blind"
    return sites


def _callee_path(site: CallSite) -> Path:
    path = WORKFLOWS_DIR / site.callee_name
    assert path.exists(), f"{site.caller.name} job '{site.job_id}' calls a missing workflow: {site.callee_name}"
    return path


def _strings(node: Any) -> Iterator[str]:
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for key, value in node.items():
            yield from _strings(key)
            yield from _strings(value)
    elif isinstance(node, list):
        for value in node:
            yield from _strings(value)


def _event_input_reads(workflow: dict[str, Any]) -> set[str]:
    """Every ``github.event.inputs`` access in the workflow's live values.

    Parsed values only: a YAML comment is not part of any value, and shell
    comment lines inside a ``run:`` block are dropped too, so prose that names
    the context (this module's own guidance, for example) is not a finding.
    """
    reads: set[str] = set()
    for text in _strings(workflow):
        for line in text.splitlines():
            if line.lstrip().startswith("#"):
                continue
            reads.update(match.group(1) for match in _EVENT_INPUT_DOT.finditer(line))
            reads.update(match.group(1) for match in _EVENT_INPUT_INDEX.finditer(line))
            if _EVENT_INPUT_BARE.search(line):
                reads.add(_WHOLE_INPUTS)
    return reads


def test_every_with_key_is_declared_by_the_called_workflow() -> None:
    problems: list[str] = []
    for site in _local_call_sites():
        declared = _workflow_call_inputs(_load(_callee_path(site)))
        for key in site.with_inputs:
            if key not in declared:
                problems.append(
                    f"{site.caller.name} job '{site.job_id}' passes with.{key} to {site.callee_name}, "
                    f"which does not declare it under workflow_call.inputs (declared: {sorted(declared)})"
                )
    assert not problems, "\n".join(problems)


def test_every_required_workflow_call_input_is_supplied() -> None:
    problems: list[str] = []
    for site in _local_call_sites():
        declared = _workflow_call_inputs(_load(_callee_path(site)))
        for name, spec in declared.items():
            if isinstance(spec, dict) and spec.get("required") is True and name not in site.with_inputs:
                problems.append(
                    f"{site.caller.name} job '{site.job_id}' calls {site.callee_name} without its "
                    f"required workflow_call input '{name}'"
                )
    assert not problems, "\n".join(problems)


def test_called_workflows_do_not_read_caller_owned_event_inputs() -> None:
    problems: list[str] = []
    checked: set[tuple[Path, str]] = set()
    for site in _local_call_sites():
        if (site.caller, site.callee_name) in checked:
            continue
        checked.add((site.caller, site.callee_name))
        caller_inputs = _dispatch_inputs(_load(site.caller))
        reads = _event_input_reads(_load(_callee_path(site)))
        for name in sorted(reads):
            if name in caller_inputs:
                continue
            problems.append(
                f"{site.callee_name} reads github.event.inputs for '{name}', which caller "
                f"{site.caller.name} does not declare (its workflow_dispatch inputs: "
                f"{sorted(caller_inputs)}); inside a called workflow that payload belongs to the "
                "caller, so the read is empty and silently blanks the callee's own input"
            )
    assert not problems, "\n".join(problems)


def test_release_installer_call_sites_are_covered() -> None:
    callees = {site.callee_name for site in _local_call_sites()}
    missing = [name for name in EXPECTED_CALLEES if name not in callees]
    assert not missing, (
        f"no local call site exercises {missing} - if the release shape genuinely changed, "
        "update EXPECTED_CALLEES deliberately"
    )


# ---------------------------------------------------------------------------
# Behavioural proof: the macOS normalize step resolves a reusable cut/recut call
# from `inputs.*` even though the event context belongs to the caller.
# ---------------------------------------------------------------------------

_BASH = shutil.which("bash")
_NEEDS_BASH = pytest.mark.skipif(_BASH is None, reason="bash is required to execute the normalize step")


def _normalize_script() -> str:
    jobs = _load(MACOS_WORKFLOW).get("jobs")
    assert isinstance(jobs, dict), "macos-installer.yml has no jobs mapping"
    for job in jobs.values():
        if not isinstance(job, dict):
            continue
        steps = job.get("steps")
        if not isinstance(steps, list):
            continue
        for step in steps:
            if isinstance(step, dict) and step.get("id") == "normalize":
                script = step.get("run")
                assert isinstance(script, str), "normalize step has no run script"
                return script
    raise AssertionError("macos-installer.yml has no step with id 'normalize'")


def _invoke_normalize(tmp_path: Path, overrides: dict[str, str]) -> "subprocess.CompletedProcess[str]":
    output_path = tmp_path / "github_output"
    output_path.write_text("", encoding="utf-8")
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "GITHUB_OUTPUT": str(output_path),
        "GITHUB_REF": "refs/heads/main",
    }
    env.update(overrides)
    return subprocess.run(  # noqa: S603 - fixed bash executable and repository-owned workflow text
        [_BASH or "bash", "-c", _normalize_script()],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


def _run_normalize(tmp_path: Path, overrides: dict[str, str]) -> dict[str, str]:
    completed = _invoke_normalize(tmp_path, overrides)
    assert completed.returncode == 0, f"normalize step failed: {completed.stderr}"
    outputs: dict[str, str] = {}
    for line in (tmp_path / "github_output").read_text(encoding="utf-8").splitlines():
        key, _, value = line.partition("=")
        outputs[key] = value
    return outputs


@_NEEDS_BASH
def test_normalize_resolves_a_reusable_cut_call_from_inputs(tmp_path: Path) -> None:
    outputs = _run_normalize(
        tmp_path,
        {
            "CALL_VERSION": "0.3.324",
            "CALL_RC_TAG": "v0.3.324-rc5",
            "CALL_REF": "v0.3.324-rc5",
            "CALL_MODE": "recut",
            # A called workflow inherits the CALLER's event: release-unified.yml
            # is a workflow_dispatch that never declares rc_tag. That is exactly
            # the environment in which the pre-fix script blanked it.
            "GITHUB_EVENT_NAME": "workflow_dispatch",
        },
    )
    assert outputs["version"] == "0.3.324"
    assert outputs["rc_tag"] == "v0.3.324-rc5"
    assert outputs["ref"] == "v0.3.324-rc5"
    assert outputs["mode"] == "recut"
    assert outputs["run_native_build"] == "true"


@_NEEDS_BASH
def test_normalize_keeps_the_manual_rehearsal_switch(tmp_path: Path) -> None:
    outputs = _run_normalize(
        tmp_path,
        {
            "CALL_VERSION": "0.3.324",
            "CALL_RC_TAG": "manual",
            "CALL_REF": "",
            "CALL_MODE": "dry-run",
            "CALL_BUILD_NATIVE": "true",
            "GITHUB_EVENT_NAME": "workflow_dispatch",
        },
    )
    # A direct dispatch may leave `ref` blank to mean "the dispatched ref".
    assert outputs["ref"] == "refs/heads/main"
    assert outputs["run_native_build"] == "true"


@_NEEDS_BASH
def test_normalize_defaults_a_manual_rehearsal_to_contract_only(tmp_path: Path) -> None:
    outputs = _run_normalize(
        tmp_path,
        {
            "CALL_VERSION": "0.3.324",
            "CALL_RC_TAG": "manual",
            "CALL_REF": "",
            "CALL_MODE": "dry-run",
            "CALL_BUILD_NATIVE": "false",
            "GITHUB_EVENT_NAME": "workflow_dispatch",
        },
    )
    assert outputs["run_native_build"] == "false"


@_NEEDS_BASH
def test_normalize_fails_closed_without_rc_tag(tmp_path: Path) -> None:
    completed = _invoke_normalize(
        tmp_path,
        {
            "CALL_VERSION": "0.3.324",
            "CALL_RC_TAG": "",
            "CALL_REF": "v0.3.324-rc5",
            "CALL_MODE": "recut",
            "GITHUB_EVENT_NAME": "workflow_dispatch",
        },
    )
    assert completed.returncode == 1
    assert "rc_tag input is required" in completed.stderr
