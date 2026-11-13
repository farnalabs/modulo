"""Structural gate: local reusable-workflow call sites must match the callee's
``workflow_call`` contract, and a called workflow must not read
caller-owned ``github.event.inputs``.

Inside a called workflow the ``github.event`` context belongs to the CALLER, so
``github.event.inputs`` is the caller's payload and never the callee's inputs.
If a callee reads a field from ``github.event.inputs`` that the caller does not
declare in its own ``workflow_dispatch``, that read evaluates to an empty string
and silently blanks the callee's input.

This module owns repo-wide call-site contracts:
* every ``with:`` key the caller passes is declared in the callee's
  ``workflow_call.inputs``;
* every REQUIRED ``workflow_call`` input is supplied by the caller;
* called workflows do not read caller-owned ``github.event.inputs`` fields;
* release installer call sites are verified to remain covered.

macOS normalize behaviour (resolving direct workflow_dispatch vs reusable
workflow_call inputs via its normalize-inputs job) lives in
``test_macos_installer_normalize.py``.
"""

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any, NamedTuple

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"

#: Every callee the release pipeline depends on. A call site disappearing means
#: the release shape changed: update this list deliberately, never silently.
EXPECTED_CALLEES = ("bundle-release.yml", "macos-installer.yml", "windows-installer.yml")

#: Jobs with bespoke input normalization that multiplex direct dispatch and
#: reusable calls (e.g. macos-installer.yml's normalize-inputs step).
#: Their normalization behavior is tested by dedicated unit tests
#: (test_macos_installer_normalize.py); this test ensures no other job
#: in the called workflow reads github.event.inputs directly.
CALLEE_EXEMPT_JOBS: dict[str, set[str]] = {
    "macos-installer.yml": {"normalize-inputs"},
}

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
    workflow_any: dict[Any, Any] = workflow
    raw = workflow_any.get("on", workflow_any.get(True))
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


def _event_input_reads(workflow: dict[str, Any], *, exclude_jobs: set[str] | None = None) -> set[str]:
    """Every ``github.event.inputs`` access in the workflow's live values.

    Parsed values only: a YAML comment is not part of any value, and shell
    comment lines inside a ``run:`` block are dropped too, so prose that names
    the context (this module's own guidance, for example) is not a finding.
    """
    exclude = exclude_jobs or set()
    jobs = workflow.get("jobs")
    if isinstance(jobs, dict) and exclude:
        scanned_workflow: dict[str, Any] = {k: v for k, v in workflow.items() if k != "jobs"}
        scanned_workflow["jobs"] = {jid: job for jid, job in jobs.items() if jid not in exclude}
    else:
        scanned_workflow = workflow

    reads: set[str] = set()
    for text in _strings(scanned_workflow):
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
                problems.append(f"{site.caller.name} job '{site.job_id}' passes with.{key} to {site.callee_name}, which does not declare it under workflow_call.inputs (declared: {sorted(declared)})")
    assert not problems, "\n".join(problems)


def test_every_required_workflow_call_input_is_supplied() -> None:
    problems: list[str] = []
    for site in _local_call_sites():
        declared = _workflow_call_inputs(_load(_callee_path(site)))
        for name, spec in declared.items():
            if isinstance(spec, dict) and spec.get("required") is True and name not in site.with_inputs:
                problems.append(f"{site.caller.name} job '{site.job_id}' calls {site.callee_name} without its required workflow_call input '{name}'")
    assert not problems, "\n".join(problems)


def test_called_workflows_do_not_read_caller_owned_event_inputs() -> None:
    problems: list[str] = []
    checked: set[tuple[Path, str]] = set()
    for site in _local_call_sites():
        if (site.caller, site.callee_name) in checked:
            continue
        checked.add((site.caller, site.callee_name))
        caller_inputs = _dispatch_inputs(_load(site.caller))
        exempt_jobs = CALLEE_EXEMPT_JOBS.get(site.callee_name)
        reads = _event_input_reads(_load(_callee_path(site)), exclude_jobs=exempt_jobs)
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
    assert not missing, f"no local call site exercises {missing} - if the release shape genuinely changed, update EXPECTED_CALLEES deliberately"
