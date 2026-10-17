"""Architecture tests: workflow-structure invariants for the autonomous PR
lifecycle (FAR-1843).

CI never exercises ``.github/workflows/`` itself, so structural drift in the
wiring is invisible to every other suite. These tests pin the load-bearing
invariants of the PR-reviewer / Branch-Fixer / merge-queue triangle:

- **Reviewer dispatch decoupling** — the PR-reviewer webhook dispatch lives in
  ``pr-review.yml`` (driven by a ``workflow_run`` completion trigger), NOT as a
  job inside ``ci.yml``. When the dispatch lived inside ``CI: Fast Validation``
  (job ``notify-pr-review``), a dispatch outage (e.g. an app HTTP 429) marked
  the whole CI run failed, which the merge-queue CI gate could not distinguish
  from a product-CI failure — blocking every merge while red CI fanned Branch
  Fixer runs into the org sandbox cap, starving the reviewer itself
  (self-amplifying outage, 2026-10-15). Re-coupling the dispatch into
  ``ci.yml`` reintroduces that failure mode; this test fails if it returns.
- **Merge-queue CI gate is product-CI-only** — every per-branch CI gate in
  ``merge-queue.yml`` must query only the product-CI workflow
  (``CI: Fast Validation``), never the review workflow, so a review-dispatch
  outage can never block merges again.

Every lens reports actionable diagnostics instead of a bare failure, mirroring
the sibling architecture tests.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

BACKEND = Path(__file__).resolve().parent.parent.parent
REPO = BACKEND.parent
WORKFLOWS = REPO / ".github" / "workflows"

CI_YML = WORKFLOWS / "ci.yml"
PR_REVIEW_YML = WORKFLOWS / "pr-review.yml"
MERGE_QUEUE_YML = WORKFLOWS / "merge-queue.yml"

#: The product-CI workflow's display name — the only workflow the merge
#: queue's per-branch CI gate may consult.
PRODUCT_CI_WORKFLOW_NAME = "CI: Fast Validation"

#: ``gh run list --workflow`` / ``gh workflow run`` values that are legitimate
#: inside merge-queue.yml. Anything else (in particular the PR-reviewer
#: workflow) must never be consulted as a merge gate.
ALLOWED_MERGE_QUEUE_WORKFLOW_REFS = frozenset(
    {
        PRODUCT_CI_WORKFLOW_NAME,
        "ci.yml",  # filename form of the same product-CI workflow
        "deploy.yml",  # post-merge dispatch, never a merge gate
        "merge-queue.yml",  # the queue's own run bookkeeping
    }
)


def _load_yaml(path: Path) -> dict:
    assert path.exists(), f"Workflow file missing: {path}"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict), f"YAML root of {path.name} is not a mapping"
    return data


def _on_block(workflow: dict) -> dict:
    """Return the trigger mapping — PyYAML parses the bare key ``on`` as True."""
    on = workflow.get("on", workflow.get(True))
    assert isinstance(on, dict), "Top-level 'on' block is not a mapping"
    return on


def _job_keys(workflow: dict) -> set[str]:
    jobs = workflow.get("jobs") or {}
    return set(jobs.keys())


def _job_names(workflow: dict) -> set[str]:
    jobs = workflow.get("jobs") or {}
    return {job.get("name", "") for job in jobs.values() if isinstance(job, dict)}


def _workflow_refs(raw: str) -> list[str]:
    """All ``gh ... --workflow <ref>`` argument values in the file."""
    pattern = re.compile(r"--workflow\s+(?:\"([^\"]+)\"|'([^']+)'|(\S+))")
    return [next(group for group in match.groups() if group is not None) for match in pattern.finditer(raw)]


def _code(raw: str) -> str:
    """The file with full-line comments stripped — structure assertions key
    on live YAML/script content, never on explanatory comments (a comment may
    legitimately name the decoupled workflow while documenting the invariant)."""
    return "\n".join(line for line in raw.splitlines() if not line.lstrip().startswith("#"))


# --- D1/D5: reviewer dispatch must not live inside ci.yml -------------------


def test_ci_yml_has_no_pr_reviewer_dispatch_job() -> None:
    """``ci.yml`` must not re-couple the PR-reviewer dispatch.

    FAR-1843: job ``notify-pr-review`` (name "Trigger PR Reviewer") lived in
    ``CI: Fast Validation`` with ``always()`` + ``needs:`` every failing-capable
    job, so a webhook dispatch failure (HTTP 429 backpressure) concluded the
    whole CI run as ``failure`` — indistinguishable from product-CI failure to
    the merge-queue gate, blocking every merge. The dispatch now lives in
    ``pr-review.yml``, driven by a ``workflow_run: completed`` trigger; a
    dispatch failure there fails only that workflow's own run."""
    raw = _code(CI_YML.read_text(encoding="utf-8"))
    wf = _load_yaml(CI_YML)
    violations: list[str] = []
    if "notify-pr-review" in _job_keys(wf):
        violations.append("  ci.yml: job key 'notify-pr-review' present — reviewer dispatch must live in pr-review.yml")
    if "Trigger PR Reviewer" in _job_names(wf):
        violations.append(
            "  ci.yml: a job named 'Trigger PR Reviewer' present — reviewer dispatch must live in pr-review.yml"
        )
    if "pr-review.yml" in raw:
        violations.append("  ci.yml: references pr-review.yml — ci.yml must not dispatch the reviewer workflow")
    if "MODULO_PR_REVIEW_WEBHOOK_URL" in raw:
        violations.append("  ci.yml: embeds the PR-review webhook secret — that dispatch belongs in pr-review.yml")
    assert not violations, "ci.yml has re-coupled the PR-reviewer dispatch into product CI (FAR-1843):\n" + "\n".join(
        violations
    )


def test_pr_review_yml_dispatches_on_product_ci_completion() -> None:
    """``pr-review.yml`` must keep a ``workflow_run`` completion trigger on the
    product-CI workflow, plus ``workflow_dispatch`` for manual re-triggers."""
    wf = _load_yaml(PR_REVIEW_YML)
    on = _on_block(wf)
    violations: list[str] = []
    if "workflow_dispatch" not in on:
        violations.append("  pr-review.yml: workflow_dispatch trigger missing (operational manual re-trigger)")
    workflow_run = on.get("workflow_run")
    if not isinstance(workflow_run, dict):
        violations.append(
            "  pr-review.yml: workflow_run trigger missing — the reviewer is no longer driven by CI completion"
        )
    else:
        workflows = workflow_run.get("workflows") or []
        if PRODUCT_CI_WORKFLOW_NAME not in workflows:
            violations.append(
                f"  pr-review.yml: workflow_run.workflows must include {PRODUCT_CI_WORKFLOW_NAME!r} (got {workflows!r})"
            )
        types = workflow_run.get("types") or []
        if "completed" not in types:
            violations.append(f"  pr-review.yml: workflow_run.types must include 'completed' (got {types!r})")
    assert not violations, "pr-review.yml no longer dispatches on product-CI completion (FAR-1843):\n" + "\n".join(
        violations
    )


def test_ci_yml_dispatches_no_reviewer_webhook() -> None:
    """``ci.yml`` must not merely rename the job — the reviewer webhook body
    (payload construction or HMAC signing against the review secret) must be
    gone from product CI too."""
    raw = _code(CI_YML.read_text(encoding="utf-8"))
    assert "MODULO_PR_REVIEW_WEBHOOK_SECRET" not in raw, (
        "ci.yml signs PR-review webhooks (MODULO_PR_REVIEW_WEBHOOK_SECRET) — "
        "reviewer dispatch must live only in pr-review.yml (FAR-1843)"
    )


# --- D2/D5: merge-queue CI gate is product-CI-only -------------------------


def test_merge_queue_ci_gate_references_only_product_ci() -> None:
    """Every ``--workflow`` reference in ``merge-queue.yml`` must be a
    product-CI / queue / post-merge workflow — never the reviewer workflow.

    FAR-1843: the merge gate ``gh run list --branch ... --workflow "CI: Fast
    Validation"`` must reflect product CI alone. If the reviewer dispatch job
    is re-added to ``ci.yml`` (see the test above) or the gate is pointed at
    another workflow, a non-product failure can again block every merge."""
    raw = _code(MERGE_QUEUE_YML.read_text(encoding="utf-8"))
    violations: list[str] = []
    for ref in _workflow_refs(raw):
        if ref not in ALLOWED_MERGE_QUEUE_WORKFLOW_REFS:
            violations.append(
                f"  merge-queue.yml: --workflow {ref!r} is not in the allowed set "
                f"{sorted(ALLOWED_MERGE_QUEUE_WORKFLOW_REFS)} — the merge gate may consult product CI only"
            )
    if "pr-review" in raw or "Review: Modulo Agent" in raw:
        violations.append(
            "  merge-queue.yml: references the PR-reviewer workflow — a review outage must not gate merges"
        )
    assert not violations, "merge-queue.yml CI gate is no longer product-CI-only (FAR-1843):\n" + "\n".join(violations)


def test_merge_queue_per_branch_gate_uses_product_ci_name() -> None:
    """The per-branch merge gate must query the product-CI workflow by its
    display name (the stable identifier across renames of the file).

    Only the per-PR-branch gate lines are asserted — the ``--branch main``
    bookkeeping query (queue self-health) legitimately uses the filename form
    of the same product-CI workflow."""
    raw = _code(MERGE_QUEUE_YML.read_text(encoding="utf-8"))
    gate_lines = [
        line for line in raw.splitlines() if "gh run list" in line and '"$CI_BRANCH"' in line and "--workflow" in line
    ]
    assert gate_lines, (
        "merge-queue.yml: no per-branch 'gh run list --workflow' gate found — "
        "the merge gate moved or was removed; update this test deliberately"
    )
    bad = [line.strip() for line in gate_lines if PRODUCT_CI_WORKFLOW_NAME not in line]
    assert not bad, (
        "merge-queue.yml: per-branch CI gate(s) no longer query "
        f"{PRODUCT_CI_WORKFLOW_NAME!r}:\n" + "\n".join(f"  {line}" for line in bad)
    )
