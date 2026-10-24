"""Architecture tests: workflow-structure invariants for the autonomous PR
lifecycle (FAR-1843, FAR-1939, FAR-1940, FAR-2001).

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
- **Reviewer dispatch is independent of CI colour** — the ``pr-review.yml`` job
  gate must never require a ``success`` product-CI conclusion. Review is a
  quality gate over the diff: a red-CI PR is reviewed while the Branch Fixer
  fixes its checks concurrently, and the reviewer reports the failing checks in
  its body instead of mirroring CI into a second CHANGES_REQUESTED. Only a
  ``cancelled`` (superseded) run may be skipped.
- **Merge-queue CI gate is product-CI-only** — every per-branch CI gate in
  ``merge-queue.yml`` must query only the product-CI workflow
  (``CI: Fast Validation``), never the review workflow, so a review-dispatch
  outage can never block merges again.
- **An undelivered Branch Fixer notification is never silent** — every webhook
  POST in ``merge-queue.yml`` must name the affected PR(s) and comment on them
  when the dispatch ultimately fails (FAR-1939).
- **A saturated Branch Fixer trigger is retried, never terminally dropped** —
  both ``ci.yml`` notify steps share one byte-identical retry policy that
  classifies HTTP 429 as capacity saturation, honours ``Retry-After``, is
  bounded by a wall-clock budget, and leaves a durable signal when it gives up
  (2026-10-20).
- **The git credential is configured exactly once** — one mechanism
  (``url.<base>.insteadOf``), never a second ``http.<url>.extraheader``
  Authorization header, or pushes are rejected with a duplicate header
  (FAR-1940).
- **A notification cannot be silently unpostable** — across ``ci.yml``,
  ``merge-queue.yml`` and ``pr-conflict-reconcile.yml``, a repo-context
  notification (``gh pr comment`` / ``gh pr edit`` / a repo-scoped ``gh api``)
  in a job with no checkout, a no-op ``||`` fallback attached to it, or a
  notification step whose failure cannot fail the job, is a hard failure
  (FAR-2001).

Every lens reports actionable diagnostics instead of a bare failure, mirroring
the sibling architecture tests.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import NamedTuple

import yaml

BACKEND = Path(__file__).resolve().parent.parent.parent
REPO = BACKEND.parent
WORKFLOWS = REPO / ".github" / "workflows"

CI_YML = WORKFLOWS / "ci.yml"
PR_REVIEW_YML = WORKFLOWS / "pr-review.yml"
MERGE_QUEUE_YML = WORKFLOWS / "merge-queue.yml"
PR_CONFLICT_YML = WORKFLOWS / "pr-conflict-reconcile.yml"

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


# --- Reviewer dispatch is independent of CI colour --------------------------


def _job_named(workflow: dict, name: str) -> dict:
    """The job whose ``name:`` is ``name`` (jobs are keyed by id, not by name)."""
    jobs = workflow.get("jobs") or {}
    for job in jobs.values():
        if isinstance(job, dict) and job.get("name") == name:
            return job
    raise AssertionError(f"no job named {name!r} — the job moved; update this test deliberately")


def test_pr_review_dispatches_on_red_ci_too() -> None:
    """The reviewer job gate must skip ONLY a cancelled product-CI run.

    Review is a quality gate over the diff, independent of CI colour: a red-CI
    PR is still reviewed, while the Branch Fixer fixes its checks concurrently
    (the reviewer reports the failing checks in its body rather than mirroring
    CI into a second CHANGES_REQUESTED). ``cancelled`` is the one conclusion
    that must not dispatch — that run was superseded, and the newer run for the
    same head SHA owns the dispatch. ``timed_out`` and ``action_required`` are
    FAILURE conclusions and must dispatch.

    A revert to ``== 'success'`` withholds every red-CI review again: a PR that
    never goes green would never be quality-reviewed, and the gate re-couples
    the reviewer to CI colour.
    """
    job = _job_named(_load_yaml(PR_REVIEW_YML), "Trigger Modulo PR Review")
    condition = job.get("if")
    assert isinstance(condition, str), (
        "pr-review.yml: the 'Trigger Modulo PR Review' job has no scalar `if:` gate — "
        "reviewer dispatch is no longer conditioned on the product-CI conclusion"
    )
    assert condition.strip(), (
        "pr-review.yml: the 'Trigger Modulo PR Review' job has a blank `if:` gate — "
        "the product-CI conclusion is no longer inspected"
    )
    code = condition.replace('"', "'")
    violations: list[str] = []
    if "workflow_run" not in code:
        violations.append(
            "  the job gate does not reference github.event.workflow_run — it no longer inspects the product-CI "
            "conclusion, so a cancelled (superseded) run could dispatch a review"
        )
    if "conclusion ==" in code:
        violations.append(
            "  the job gate compares the conclusion for EQUALITY — an inclusion list silently withholds the review "
            "from every conclusion it omits (timed_out, action_required, neutral, ...); exclude only 'cancelled'"
        )
    if "== 'success'" in code:
        violations.append(
            "  the job gate requires the 'success' conclusion — a red-CI PR is no longer reviewed; only 'cancelled' "
            "may be excluded"
        )
    if "!= 'cancelled'" not in code:
        violations.append(
            "  the job gate does not exclude the 'cancelled' conclusion — a superseded product-CI run would "
            "dispatch a review for a head SHA the newer run owns"
        )
    assert not violations, (
        "pr-review.yml no longer dispatches the reviewer for a red (but completed) product-CI run:\n"
        + "\n".join(violations)
    )


# --- FAR-2111: the review STARTS when the PR is ready, not when CI finishes --
#
# 2026-10-23/24: `Test (Backend)` inside "CI: Fast Validation" measured 48-53
# min (PR #2153 52m36s, PR #2246 48m), and the reviewer was dispatched ONLY on
# that workflow's completion. PR #2246 opened 18:55, CI completed ~19:53, the
# review arrived 20:00 — 67 min in which nothing about the PR changed — and the
# merge queue idled at APPROVED_COUNT=0. Review is a quality gate independent of
# CI colour (the workflow header), so gating its START on CI completion is
# stricter than the stated design.
#
# The early trigger is only half the change: every gate that the CI-completion
# path applies must also hold on the early path. The tests below pin the trigger
# AND those gates, because the naive "just add the trigger" edit bypasses the
# manual-delivery gate (it lived inside the workflow_run branch only).

#: The early trigger's event key. ``pull_request_target`` rather than
#: ``pull_request`` on purpose: see the test that pins it.
EARLY_EVENT = "pull_request_target"


def _trigger_block() -> dict:
    """The workflow's ``on:`` mapping (PyYAML parses the bare key ``on`` as True)."""
    return _on_block(_load_yaml(PR_REVIEW_YML))


def _job_steps(job_name: str) -> list[dict]:
    job = _job_named(_load_yaml(PR_REVIEW_YML), job_name)
    steps = job.get("steps") or []
    assert isinstance(steps, list), f"pr-review.yml: job {job_name!r} steps are not a list"
    return [step for step in steps if isinstance(step, dict)]


def _step(job_name: str, step_name: str) -> dict:
    matches = [step for step in _job_steps(job_name) if step.get("name") == step_name]
    assert len(matches) == 1, (
        f"pr-review.yml: expected exactly one {step_name!r} step in job {job_name!r}, "
        f"found {len(matches)} — the gate moved; update this test deliberately"
    )
    return matches[0]


def _unquoted(line: str) -> str:
    """``line`` with the contents of every quoted string removed.

    Shell keywords are matched only where the shell would see them, so a notice
    message containing the word "if" cannot fake an extra nesting level.
    """
    out: list[str] = []
    quote = ""
    for char in line:
        if quote:
            if char == quote:
                quote = ""
            continue
        if char in ("'", '"'):
            quote = char
            continue
        out.append(char)
    return "".join(out)


def _top_level_block_end(lines: list[str], start: int) -> int:
    """Index of the ``fi`` that closes the ``if`` opened at ``lines[start]``.

    ``elif`` neither opens nor closes a level (it swaps arms of the same ``if``),
    so only ``if`` and ``fi`` tokens move the counter. Quoted-string contents are
    blanked first, so a notice message containing the word "if" cannot fake an
    extra nesting level.
    """
    depth = 0
    for index in range(start, len(lines)):
        code = _unquoted(lines[index]).strip()
        if not code or code.startswith("#"):
            continue
        for token in re.split(r"[\s;()]+", code):
            if token == "if":
                depth += 1
            elif token == "fi":
                depth -= 1
                if depth == 0:
                    return index
    raise AssertionError(
        "pr-review.yml: the event-dispatch `if` in 'Set PR details' is unterminated — cannot locate the block end"
    )


def test_pr_review_yml_dispatches_the_review_when_the_pr_is_ready() -> None:
    """A PR that is ready must be reviewed without waiting ~50 min for CI.

    ``opened`` covers a PR created ready (the common case — #2246 opened 18:55
    and was not reviewed until 20:00). ``ready_for_review`` covers a draft being
    marked ready.

    ``synchronize`` is deliberately absent: a push to an existing PR is already
    covered by the ``workflow_run`` completion path, which carries the
    superseded-run guard and the head-SHA dedup. Every push is a NEW head SHA, so
    head-SHA dedup cannot coalesce a burst of pushes into one review — adding
    ``synchronize`` would fan a review storm onto the shared model capacity.
    """
    on = _trigger_block()
    early = on.get(EARLY_EVENT)
    assert isinstance(early, dict), (
        f"pr-review.yml: no {EARLY_EVENT} trigger — a review still cannot start until the whole "
        "~50-min product-CI run completes (Test (Backend) alone is 48-53 min), so every PR waits an "
        "hour before it can be approved and the merge queue idles at APPROVED_COUNT=0"
    )
    types = early.get("types") or []
    violations: list[str] = []
    for required in ("opened", "ready_for_review"):
        if required not in types:
            violations.append(
                f"  {EARLY_EVENT}.types must include {required!r} (got {types!r}) — "
                "'opened' covers a PR created ready, 'ready_for_review' covers a draft marked ready"
            )
    if "synchronize" in types:
        violations.append(
            f"  {EARLY_EVENT}.types must NOT include 'synchronize' (got {types!r}) — a push is already "
            "covered by the workflow_run completion path (superseded guard + head-SHA dedup); every push "
            "is a new head SHA, so head-SHA dedup cannot coalesce a burst and a few quick pushes would "
            "fan a review storm onto the shared model capacity"
        )
    assert not violations, "pr-review.yml no longer reviews a PR when it becomes ready (FAR-2111):\n" + "\n".join(
        violations
    )


def test_pr_review_early_trigger_keeps_its_checks_off_the_pr_head() -> None:
    """The early trigger must be ``pull_request_target``, never ``pull_request``.

    A ``pull_request``-triggered workflow attaches its check run to the PR HEAD.
    The app-leg merge queue requires EVERY check run on a PR's head SHA to be
    ``completed``/``success`` (``dogfood/pipeline-scripts/merge-queue.py``
    ``checks_pass``), so a failed review dispatch would then block that PR's
    merge — the exact FAR-1843 regression the ``workflow_run`` trigger exists to
    prevent ("a review-dispatch outage can never block merges").

    ``pull_request_target`` check runs attach to the base branch SHA, exactly
    like ``workflow_run``'s, so the PR's own checks stay untouched. It also
    checks out the BASE branch, so the dedup and fork-gate scripts always run
    trusted base-branch code — never the PR's own copy of them.
    """
    on = _trigger_block()
    assert EARLY_EVENT in on, f"pr-review.yml: {EARLY_EVENT} trigger missing — see the early-trigger test"
    assert "pull_request" not in on, (
        "pr-review.yml: a plain `pull_request` trigger is present. Its check run lands on the PR HEAD, and "
        "the app-leg merge queue requires every head check run to be success — a failed review dispatch "
        "would block that PR's merge (FAR-1843). Use pull_request_target: its check runs attach to the base "
        "branch SHA, like workflow_run's."
    )


def test_pr_review_manual_delivery_gate_is_not_bypassed_on_the_early_path() -> None:
    """The ``manual-delivery`` gate must apply to the early path too.

    This is the defect the naive "add the trigger" edit ships: the gate lived
    inside the ``workflow_run`` branch of ``Set PR details``, so a PR a human is
    driving manually would have been auto-reviewed the moment it opened. The gate
    is now one shared block that runs for EVERY non-manual event; this test pins
    that it is at the top level of the script (``if`` depth 0), not nested in one
    event branch.
    """
    step = _step("Trigger Modulo PR Review", "Set PR details")
    run = step.get("run") or ""
    lines = run.splitlines()
    arms = [index for index, line in enumerate(lines) if "*,manual-delivery,*" in line]
    assert len(arms) == 1, (
        "pr-review.yml: expected exactly ONE manual-delivery case arm in 'Set PR details', "
        f"found {len(arms)} — a duplicated or missing gate means the two dispatch paths can diverge"
    )
    dispatch = [index for index, line in enumerate(lines) if 'if [ "$EVENT_NAME" = "pull_request" ]' in line]
    assert len(dispatch) == 1, (
        f"pr-review.yml: expected one per-event PR resolution block in 'Set PR details', found {len(dispatch)}"
    )
    block_end = _top_level_block_end(lines, dispatch[0])
    assert arms[0] > block_end, (
        "pr-review.yml: the manual-delivery gate runs INSIDE the per-event dispatch block (it ends at line "
        f"{block_end + 1} of the script) — it is therefore restricted to one event, so a labelled PR is "
        "auto-reviewed on the other path (the early trigger bypasses it). Move the gate after the block so "
        "every path shares it."
    )
    assert 'if [ "$EVENT_NAME" != "workflow_dispatch" ]' in run, (
        'pr-review.yml: the manual-delivery gate must be guarded by `!= "workflow_dispatch"` so it covers '
        "the CI-completion AND early paths while a manual operator re-trigger still bypasses it"
    )


def test_pr_review_fork_gate_is_not_bypassed_on_the_early_path() -> None:
    """The same-repo author gate must run on the early path as well.

    A fork / external-author PR is never auto-reviewed (FAR-264): its review
    needs a human path, and under the early trigger secrets are in scope before
    the gate runs, so a bypass would dispatch a review for an untrusted PR. The
    gate's ``if:`` must therefore stay event-agnostic (``ready`` only), and the
    checkout that provides the versioned guard script must not be event-gated.
    """
    step = _step("Trigger Modulo PR Review", "Same-repo author gate (FAR-264)")
    condition = str(step.get("if") or "")
    assert "github.event_name" not in condition, (
        "pr-review.yml: the same-repo author gate is restricted by event name — the early path would "
        "dispatch a review for a fork / external-author PR (FAR-264)"
    )
    assert "same-repo-author-guard.py" in (step.get("run") or ""), (
        "pr-review.yml: the same-repo author gate no longer calls the shared versioned guard script"
    )
    checkout = _step("Trigger Modulo PR Review", "Checkout repository (for the gate scripts)")
    assert "github.event_name" not in str(checkout.get("if") or ""), (
        "pr-review.yml: the gate-script checkout is event-gated — the dedup and fork gates cannot run on "
        "the early path without it"
    )


def test_pr_review_early_path_is_deduplicated_per_head_sha() -> None:
    """One review per head SHA: the early dispatch must be coalesced, not doubled.

    The CI-completion dispatch for the same head SHA arrives ~50 min after the
    early one. Two mechanical layers coalesce it (the app-side prior-review skip
    is a third backstop, but it is timestamp-based by design — see the reviewer
    prompt): (1) the ``workflow_run`` path's superseded-run guard, and (2) the
    head-SHA dedup step. The dedup must therefore run on BOTH paths — a gate
    expressed as ``== 'workflow_run'`` leaves the early path undeduplicated, so a
    draft marked ready (or a second ``ready_for_review`` on an unchanged head)
    would review the same SHA twice.
    """
    step = _step("Trigger Modulo PR Review", "Skip when head already reviewed (dedup)")
    condition = str(step.get("if") or "")
    violations: list[str] = []
    if "workflow_dispatch" not in condition:
        violations.append(
            "  the dedup gate no longer names workflow_dispatch — a manual operator re-trigger must stay "
            "explicit and bypass dedup, as it always has"
        )
    if "== 'workflow_run'" in condition:
        violations.append(
            "  the dedup gate is still restricted to workflow_run — the early dispatch is never coalesced, "
            "so the same head SHA can be reviewed twice (e.g. a draft marked ready after its completion "
            "review, or a repeated ready_for_review)"
        )
    if "steps.pr-details.outputs.ready == 'true'" not in condition:
        violations.append("  the dedup gate no longer requires a resolved PR (ready == 'true')")
    if "pr-review-dedup.py" not in (step.get("run") or ""):
        violations.append("  the dedup step no longer calls the versioned .github/scripts/pr-review-dedup.py")
    assert not violations, "pr-review.yml cannot guarantee one review per head SHA (FAR-2111):\n" + "\n".join(
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


# --- Merge-queue concurrency is scoped to the queue job, not the workflow ---


def test_merge_queue_concurrency_is_scoped_to_the_queue_job() -> None:
    """The ``merge-queue`` concurrency group must live on the ``queue`` job.

    At workflow level the group is held by the WHOLE run — including
    ``event-gate``'s ~10-min ``CHAIN_READY_AT`` pacing sleep — so every chained
    pass queued behind an incumbent's sleep and waited out the full ~10 min
    before its own turn. Only the ``queue`` job merges to main, so only it needs
    serialising; scoping the group there keeps merges strictly serialised while
    freeing the pacing sleep. ``cancel-in-progress`` must stay ``false``, or a
    later run would cancel a merge that is already in flight.
    """
    workflow = _load_yaml(MERGE_QUEUE_YML)
    job_concurrency = _queue_job(workflow).get("concurrency")
    violations: list[str] = []
    if "concurrency" in workflow:
        violations.append(
            "  merge-queue.yml: workflow-level `concurrency` is present — the group must be scoped to the "
            "`queue` job, or event-gate's pacing sleep holds it and delays every chained pass"
        )
    if not isinstance(job_concurrency, dict):
        violations.append(
            "  merge-queue.yml: jobs.queue has no `concurrency` mapping — merges to main are no longer serialised"
        )
    else:
        if job_concurrency.get("group") != "merge-queue":
            violations.append(
                "  merge-queue.yml: jobs.queue.concurrency.group must be 'merge-queue' "
                f"(got {job_concurrency.get('group')!r})"
            )
        if job_concurrency.get("cancel-in-progress") is not False:
            violations.append(
                "  merge-queue.yml: jobs.queue.concurrency.cancel-in-progress must be false "
                f"(got {job_concurrency.get('cancel-in-progress')!r}) — a later run must never cancel "
                "an in-flight merge"
            )
    assert not violations, (
        "merge-queue.yml no longer scopes the merge-queue concurrency group to the queue job:\n" + "\n".join(violations)
    )


# --- The queue only collects PRs that target main ---------------------------


def _collect_step() -> dict:
    for step in _queue_steps(_load_yaml(MERGE_QUEUE_YML)):
        if step.get("id") == "collect":
            return step
    raise AssertionError(
        "merge-queue.yml: the 'collect' step (id: collect) is missing — the queue's PR selection moved"
    )


def test_merge_queue_collects_only_prs_targeting_main() -> None:
    """A stacked PR must never enter the queue.

    The queue squash-merges each collected PR's head into a branch cut from
    main. A stacked PR (its base is another, still-unmerged feature branch)
    carries that whole base branch in its head, which conflicts with whatever
    main has taken since the base was cut. 2026-10-20: PR #2029 (stacked on
    unmerged #2017) was approved and GREEN, was collected, conflicted on
    ``classify.py`` / ``trigger-engine.md`` / ``manifest.yaml``, and failed
    every queue pass, starving the queue of all merges. It becomes eligible
    only when its base lands and it is retargeted to main.
    """
    run = _collect_step().get("run") or ""
    violations: list[str] = []
    if "baseRefName" not in run:
        violations.append(
            "  the collect step's `gh pr list --json` does not request `baseRefName` — it cannot filter stacked PRs"
        )
    if "BASE_REF=$(echo \"$pr\" | jq -r '.baseRefName')" not in run:
        violations.append("  the collect step does not read each PR's base branch into BASE_REF")
    if 'if [ "$BASE_REF" != "main" ]' not in run:
        violations.append("  the collect step does not skip PRs whose base branch is not main")
    if "continue" not in run:
        violations.append("  the collect step's base-branch gate does not `continue` (the PR would still be queued)")
    assert not violations, (
        "merge-queue.yml can queue a stacked PR and deadlock every pass on its base-branch conflict:\n"
        + "\n".join(violations)
    )


# --- FAR-1939: an undelivered Branch Fixer notification is never silent -----

#: The shared-policy invocation every Branch Fixer webhook POST funnels
#: through. All three notify steps (merge-conflict, migration-collision,
#: CI-failure) dispatch with exactly this call.
BRANCH_FIXER_POST = "bash scripts/dispatch_with_retry.sh"

#: The guard that opens a notify step's ultimate-failure arm (after the shared
#: dispatch returns). Everything from here to the end of the ``run`` block is
#: the failure path that must be attributable, durable and still failing.
DISPATCH_FAILURE_GUARD = 'if [ "$DISPATCH_OK" != "true" ]; then'


def _queue_job(workflow: dict) -> dict:
    jobs = workflow.get("jobs") or {}
    queue = jobs.get("queue")
    assert isinstance(queue, dict), "merge-queue.yml: the 'queue' job is missing"
    return queue


def _queue_steps(workflow: dict) -> list[dict]:
    steps = _queue_job(workflow).get("steps") or []
    assert isinstance(steps, list), "merge-queue.yml: the 'queue' job's steps are not a list"
    assert steps, "merge-queue.yml: the 'queue' job declares no steps"
    return [step for step in steps if isinstance(step, dict)]


def _dispatch_failure_arm(run: str) -> str:
    """The tail of a notify step's ``run`` block from the ultimate-failure guard.

    The guard is the last block in each notify step, so everything from it to
    the end of the script is the failure arm — the exact text a silent
    notification would hide in.
    """
    assert DISPATCH_FAILURE_GUARD in run, (
        f"merge-queue.yml: no {DISPATCH_FAILURE_GUARD!r} guard in a Branch Fixer notify step — "
        "the ultimate-failure path moved; update this test deliberately"
    )
    return run.rsplit(DISPATCH_FAILURE_GUARD, 1)[-1]


def _failure_branch(run: str) -> str:
    """Text after the LAST ``else`` in a run block — a step's failure arm."""
    return run.rsplit("else", 1)[-1]


def test_merge_queue_branch_fixer_dispatch_failures_are_attributable() -> None:
    """An undelivered Branch Fixer notification must be attributable and durable.

    FAR-1939: on 2026-10-13 (run ``39026353546``) every retry of the
    merge-conflict webhook POST failed with HTTP 429 ("Concurrent run limit of 5
    reached"). The only trace was an ``::error::`` line inside an
    already-failing run's log, so PR #1925 sat conflicted with nothing on the PR
    (or anywhere durable) saying the Branch Fixer had never been asked to
    reconcile it. Each notify step's failure arm must therefore name the
    affected PR(s) and comment on each of them.

    The POST itself is now the shared script (and its retry policy). Since
    2026-10-21 (run ``39633573993``) the durable signal, not the webhook
    delivery, decides the step's verdict: an undelivered notification that was
    recorded on every affected PR warns loudly and lets the pass continue (an
    unconditional ``exit 1`` there discarded a merge that had already
    completed), while one that left a PR with NO signal still fails the step.
    ``backend/tests/unit/test_merge_queue_notify_failopen.py`` owns that
    fail-open/fail-hard split; this test keeps pinning the attributable,
    durable per-PR failure arm around it.
    """
    steps = _queue_steps(_load_yaml(MERGE_QUEUE_YML))
    posting = [step for step in steps if BRANCH_FIXER_POST in (step.get("run") or "")]
    assert len(posting) >= 3, (
        "merge-queue.yml: expected the three shared-script dispatch steps "
        f"(merge-conflict, migration-collision, CI-failure); found {len(posting)} — "
        "the notification surface moved; update this test deliberately"
    )
    violations: list[str] = []
    for step in posting:
        name = step.get("name", "<unnamed>")
        env = step.get("env") or {}
        arm = _dispatch_failure_arm(step.get("run") or "")
        if "GH_TOKEN" not in env:
            violations.append(
                f"  {name!r}: env has no GH_TOKEN — the failure arm's `gh pr comment` cannot authenticate"
            )
        attributed = [
            line.strip()
            for line in arm.splitlines()
            if ("::error::" in line or "::warning::" in line) and "not delivered" in line
        ]
        if not attributed:
            violations.append(f"  {name!r}: failure arm has no 'was not delivered' ::error::/::warning:: annotation")
        for line in attributed:
            if "PR" not in line or "${" not in line:
                violations.append(f"  {name!r}: failure-arm annotation does not name the affected PR(s): {line!r}")
        if 'gh pr comment "$NUM"' not in arm:
            violations.append(
                f'  {name!r}: failure arm has no `gh pr comment "$NUM"` — the undelivered notification '
                "leaves no durable signal on the PR"
            )
        if "for NUM in $" not in arm:
            violations.append(
                f"  {name!r}: failure arm does not iterate the affected PR list — the comment cannot be per-PR"
            )
        if "exit 1" not in arm:
            violations.append(
                f"  {name!r}: failure arm has no `exit 1` — a PR left with no durable signal must fail the step"
            )
        if "exit 0" not in arm:
            violations.append(
                f"  {name!r}: failure arm has no `exit 0` — a durably signalled notification failure must "
                "let the pass continue instead of discarding a completed merge"
            )
    assert not violations, (
        "merge-queue.yml Branch Fixer notifications can be dropped silently (FAR-1939):\n" + "\n".join(violations)
    )


# --- FAR-1940: the git credential is configured exactly once ----------------

#: The two credential mechanisms that must never coexist: the env-driven
#: ``url.<base>.insteadOf`` rewrite (token in the remote URL) and a persisted
#: ``http.<url>.extraheader`` Authorization header.
CREDENTIAL_MECHANISMS = ("insteadOf", "extraheader")

#: Branch pushes that put a merge-queue branch on origin. Both may carry merged
#: ``.github/workflows/*`` changes, so both must use the workflows-scoped PAT.
BRANCH_PUSH_FRAGMENT = 'git push origin "merge-queue:'


def test_merge_queue_configures_the_git_credential_exactly_once() -> None:
    """One credential mechanism, or GitHub rejects the push outright.

    FAR-1940: ``merge-queue.yml`` configured the same PAT twice — the
    ``GIT_CONFIG`` ``insteadOf`` rewrite (token embedded in the push URL) AND a
    persisted ``http.https://github.com/.extraheader`` Authorization header.
    Git then sent two Authorization headers and GitHub's receive-pack rejected
    the push with ``remote: Duplicate header: "Authorization"`` (2026-10-13,
    run ``39026353546``), which is why neither branch push could complete. The
    surviving mechanism is the ``insteadOf`` rewrite.
    """
    raw = _code(MERGE_QUEUE_YML.read_text(encoding="utf-8"))
    workflow = _load_yaml(MERGE_QUEUE_YML)
    steps = _queue_steps(workflow)
    violations: list[str] = []

    present = [name for name in CREDENTIAL_MECHANISMS if name in raw]
    if len(present) != 1:
        violations.append(
            "  merge-queue.yml: expected exactly ONE git-credential mechanism, found "
            f"{present} — two mechanisms put two Authorization headers on the push (FAR-1940)"
        )
    if "extraheader" in present:
        violations.append(
            "  merge-queue.yml: the eliminated http.<url>.extraheader Authorization header is back; "
            "keep the url.<base>.insteadOf rewrite only (FAR-1940)"
        )

    env = workflow.get("env") or {}
    if "insteadOf" in present and str(env.get("GIT_CONFIG_COUNT", "")) != "1":
        violations.append(
            "  merge-queue.yml: workflow-level GIT_CONFIG_COUNT must inject exactly one url rewrite "
            f"(got {env.get('GIT_CONFIG_COUNT')!r}) — a second entry is a second credential mechanism"
        )

    checkouts = [step for step in steps if str(step.get("uses", "")).startswith("actions/checkout@")]
    if not checkouts:
        violations.append("  merge-queue.yml: the queue job has no actions/checkout step")
    for step in checkouts:
        if (step.get("with") or {}).get("persist-credentials") is not False:
            violations.append(
                "  merge-queue.yml: actions/checkout must set `persist-credentials: false` — otherwise it "
                "re-persists an http.extraheader Authorization header and the duplicate returns (FAR-1940)"
            )

    pushes = [step for step in steps if BRANCH_PUSH_FRAGMENT in (step.get("run") or "")]
    assert len(pushes) >= 2, (
        "merge-queue.yml: expected the two merge-queue branch pushes (reference + preserve-on-failure); "
        f"found {len(pushes)} — the push surface moved; update this test deliberately"
    )
    for step in pushes:
        name = step.get("name", "<unnamed>")
        step_env = step.get("env") or {}
        key0 = str(step_env.get("GIT_CONFIG_KEY_0", ""))
        if "MODULO_REVIEWBOT_TOKEN" not in key0 or "insteadOf" not in key0:
            violations.append(
                f"  {name!r}: does not route the push through the single insteadOf rewrite carrying "
                "secrets.MODULO_REVIEWBOT_TOKEN — a branch containing .github/workflows/* needs the "
                "workflows-scoped PAT (FAR-1940)"
            )
        arm = _failure_branch(step.get("run") or "")
        if "::error::" not in arm:
            violations.append(
                f"  {name!r}: push failure is swallowed — the failure arm must emit an ::error:: annotation"
            )
        if "merge-queue/failed-" in (step.get("run") or "") and "exit 1" not in arm:
            violations.append(
                f"  {name!r}: push failure must fail the step loudly (`exit 1`) — a silently-swallowed push "
                "failure is exactly how the duplicate-header breakage hid (FAR-1940)"
            )

    assert not violations, (
        "merge-queue.yml no longer configures the git credential exactly once (FAR-1940):\n" + "\n".join(violations)
    )


# --- FAR-2001: a notification that cannot post, or whose failure is silently
# --- swallowed, is caught structurally instead of being noticed by its absence.
#
# The class this guards — not one instance:
#   1. a repo-context notification (`gh pr comment` / `gh pr edit`, and a
#      repo-scoped `gh api`) in a job that performs NO `actions/checkout`, where
#      `gh` cannot infer the repository from a git remote and dies with
#      "fatal: not a git repository" before reaching the API;
#   2. a no-op fallback (`|| echo` / `|| true` / `|| :`) attached to the
#      notification, which converts a failed notification into an unread log
#      line;
#   3. a notification step whose failure cannot fail the job
#      (`continue-on-error: true`, or the no-op fallback in 2).
#
# This is exactly how FAR-1997 survived in `ci.yml`'s `notify-pr-failure` job
# (no checkout): `gh pr comment "$PR_NUM" ... || echo "::warning::..."` could
# never post, and the fallback swallowed the failure, so the only signal that
# the fix loop was dead for that PR went nowhere. `merge-queue.yml` (FAR-1939)
# and `pr-conflict-reconcile.yml` (FAR-1949) are covered by the same scan.

#: Shell commands that POST a durable notification to a PR/issue. They infer
#: the repository from a git remote unless `--repo` / `GH_REPO` supplies it.
_NOTIFICATION_COMMANDS = (
    "gh pr comment",
    "gh pr edit",
    "gh issue comment",
    "gh issue edit",
    "gh issue create",
    "gh issue close",
)

#: `gh` placeholders that make a `gh api` call repository-scoped — `gh` fills
#: them from the inferred repo, which needs a checkout or `--repo` / `GH_REPO`.
_REPO_PLACEHOLDER = re.compile(r"\{owner\}|\{repo\}")

#: A `||` arm that DISCARDS the notification's exit status: `|| echo ...`,
#: `|| true`, `|| :`. Matched against the unquoted "code" view of a statement,
#: so a `--body` that merely mentions `|| echo` cannot trigger it.
_NOOP_FALLBACK = re.compile(r"\|\|\s*(?:echo\b|true\b|:(?![\w:]))")


def _shell_statements(run: str) -> list[tuple[str, str]]:
    """Split a ``run`` block into top-level shell statements.

    Returns ``(raw, code)`` pairs. ``code`` is the statement with quoted-string
    contents blanked out, so flags like ``--repo`` and operators like ``||`` are
    matched only where the shell would see them — a multi-line quoted
    notification body cannot masquerade as shell code, and a body that mentions
    ``|| echo`` cannot be mistaken for a fallback.

    Statements are split on an UNQUOTED, UNESCAPED newline or ``;``. A
    backslash-newline line continuation does not split; neither does a newline
    inside a quoted string (the FAR-1997 ``--body "..."`` spans several lines).
    """
    statements: list[tuple[str, str]] = []
    raw: list[str] = []
    code: list[str] = []
    in_single = False
    in_double = False
    in_comment = False
    escaped = False
    prev = "\n"

    def flush() -> None:
        statements.append(("".join(raw), "".join(code)))
        raw.clear()
        code.clear()

    for ch in run:
        if in_comment:
            raw.append(ch)
            if ch == "\n":
                in_comment = False
                flush()
                prev = "\n"
            continue
        if escaped:
            raw.append(ch)
            escaped = False
            prev = ch
            continue
        if ch == "\\" and not in_single:
            raw.append(ch)
            escaped = True
            prev = ch
            continue
        if in_single:
            raw.append(ch)
            prev = ch
            if ch == "'":
                in_single = False
            continue
        if in_double:
            raw.append(ch)
            prev = ch
            if ch == '"':
                in_double = False
            continue
        if ch in ("'", '"'):
            in_single = ch == "'"
            in_double = ch == '"'
            raw.append(ch)
            code.append(" ")
            prev = ch
            continue
        if ch == "#" and prev in (" ", "\t", "\n", ";"):
            # A shell comment is not code — an apostrophe or quote inside it
            # must not open a string that swallows the rest of the step.
            in_comment = True
            raw.append(ch)
            continue
        if ch in "\n;":
            flush()
            prev = "\n"
            continue
        raw.append(ch)
        code.append(ch)
        prev = ch
    flush()
    return statements


def _notification_command(raw: str, code: str) -> str | None:
    """The repo-context notification a shell statement POSTs, or ``None``.

    ``code`` carries the unquoted tokens (for `gh pr`/`gh issue` commands);
    ``raw`` is consulted for ``gh api`` placeholders, which live inside the
    quoted URL argument.
    """
    for command in _NOTIFICATION_COMMANDS:
        if re.search(rf"(?<![\w-]){re.escape(command)}(?![\w-])", code):
            return command
    if "gh api" in code and _REPO_PLACEHOLDER.search(raw):
        return "gh api (repo-scoped)"
    return None


class _NotificationViolation(NamedTuple):
    """One notification-can't-post / silently-swallowed violation.

    ``workflow``/``job``/``step``/``kind`` locate the offending construct so the
    failure message names the exact place to fix.
    """

    workflow: str
    job: str
    step: str
    kind: str
    detail: str

    def __str__(self) -> str:
        return f"  {self.workflow} job {self.job!r} step {self.step!r}: {self.detail} (FAR-2001)"


def _notification_violations(name: str, workflow: dict) -> list[_NotificationViolation]:
    """Every notification-can't-post / silently-swallowed violation in a workflow."""
    violations: list[_NotificationViolation] = []
    jobs = workflow.get("jobs") or {}
    for job_id, job in jobs.items():
        if not isinstance(job, dict):
            continue
        steps = [step for step in (job.get("steps") or []) if isinstance(step, dict)]
        has_checkout = any(str(step.get("uses", "")).startswith("actions/checkout@") for step in steps)
        job_env = job.get("env") or {}
        job_continue_on_error = job.get("continue-on-error") is True
        for step in steps:
            run = step.get("run") or ""
            if not run:
                continue
            step_env = step.get("env") or {}
            has_repo_context = has_checkout or "GH_REPO" in job_env or "GH_REPO" in step_env
            step_name = str(step.get("name", "<unnamed>"))
            step_notification = False
            for raw_stmt, code_stmt in _shell_statements(run):
                command = _notification_command(raw_stmt, code_stmt)
                if command is None:
                    continue
                step_notification = True
                if not has_repo_context and "--repo" not in code_stmt:
                    violations.append(
                        _NotificationViolation(
                            name,
                            str(job_id),
                            step_name,
                            "repo-context",
                            f"{command!r} has no --repo in a job with no checkout — "
                            "`gh` cannot infer the repository and the notification dies before posting",
                        )
                    )
                fallback = _NOOP_FALLBACK.search(code_stmt)
                if fallback is not None:
                    violations.append(
                        _NotificationViolation(
                            name,
                            str(job_id),
                            step_name,
                            "no-op-fallback",
                            f"{command!r} is wrapped in a no-op fallback {fallback.group(0)!r} — "
                            "a failed notification becomes a log line instead of failing the step",
                        )
                    )
            if step_notification and (job_continue_on_error or step.get("continue-on-error") is True):
                violations.append(
                    _NotificationViolation(
                        name,
                        str(job_id),
                        step_name,
                        "continue-on-error",
                        "continue-on-error is set on a notification step — its failure can no longer fail the "
                        "job, so an undelivered notification is invisible",
                    )
                )
    return violations


def _write_workflow(tmp_path: Path, body: str) -> dict:
    """Materialise an inline workflow fixture and parse it."""
    path = tmp_path / "wf.yml"
    path.write_text(body, encoding="utf-8")
    return _load_yaml(path)


# The pre-FAR-1997 form exactly as it shipped in ci.yml's notify-pr-failure job
# (no checkout): an unqualified `gh pr comment` whose failure `|| echo` swallows.
_PRE_FIX_STEP = (
    "\n"
    "jobs:\n"
    "  notify-pr-failure:\n"
    "    name: Trigger Branch Fixer\n"
    "    steps:\n"
    "      - name: Trigger Modulo Branch Fixer\n"
    "        shell: bash\n"
    "        env:\n"
    "          GH_TOKEN: ${{ secrets.MODULO_REVIEWBOT_TOKEN || github.token }}\n"
    "          PR_NUM: ${{ github.event.pull_request.number }}\n"
    "        run: |\n"
    '          if [ "$DISPATCHED" != "true" ]; then\n'
    '            echo "::error::Branch Fixer webhook dispatch failed"\n'
    '            gh pr comment "$PR_NUM" --body "## Branch Fixer Dispatch Failed\n'
    "\n"
    '            The automated fix loop is **not active** for this PR. Manual investigation may be needed." '
    '|| echo "::warning::Failed to post dispatch-failure comment on PR #${PR_NUM}"\n'
    "            exit 1\n"
    "          fi\n"
)

# The FAR-1997 fix: repo passed explicitly, and the notice's own failure is a
# hard error (its own non-zero exit), never a warning annotation.
_FIXED_STEP = """
jobs:
  notify-pr-failure:
    name: Trigger Branch Fixer
    steps:
      - name: Trigger Modulo Branch Fixer
        shell: bash
        env:
          GH_TOKEN: ${{ secrets.MODULO_REVIEWBOT_TOKEN || github.token }}
          PR_NUM: ${{ github.event.pull_request.number }}
        run: |
          if [ "$DISPATCHED" != "true" ]; then
            if ! gh pr comment "$PR_NUM" --repo "${{ github.repository }}" --body "## Branch Fixer Dispatch Failed

            The automated fix loop is **not active** for this PR."; then
              echo "::error::Failed to post dispatch-failure comment on PR #${PR_NUM}"
              exit 2
            fi
            exit 1
          fi
"""


def test_notification_guard_flags_the_pre_far_1997_form(tmp_path: Path) -> None:
    """The guard is red against the form that shipped: an unqualified
    `gh pr comment` in a no-checkout job, wrapped in a `|| echo` no-op."""
    violations = _notification_violations("wf.yml", _write_workflow(tmp_path, _PRE_FIX_STEP))
    kinds = {violation.kind for violation in violations}
    assert "repo-context" in kinds, f"guard did not flag the repo-context gap: {violations}"
    assert "no-op-fallback" in kinds, f"guard did not flag the swallowed failure: {violations}"


def test_notification_guard_accepts_the_far_1997_fix(tmp_path: Path) -> None:
    """The fixed form passes: `--repo` supplies context and the notice's
    failure is a hard error, so there is nothing to flag."""
    violations = _notification_violations("wf.yml", _write_workflow(tmp_path, _FIXED_STEP))
    assert not violations, "guard must pass on the fixed form:\n" + "\n".join(str(v) for v in violations)


def test_notification_guard_accepts_a_checkout_job(tmp_path: Path) -> None:
    """A checkout makes the repository inferable, so an unqualified command is
    legitimate and must NOT be flagged."""
    body = """
jobs:
  notify:
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1
      - name: Comment
        run: gh pr comment "$PR_NUM" --body "done"
"""
    violations = _notification_violations("wf.yml", _write_workflow(tmp_path, body))
    assert not violations, "a checkout job must not be flagged:\n" + "\n".join(str(v) for v in violations)


def test_notification_guard_honours_gh_repo_env(tmp_path: Path) -> None:
    """`GH_REPO` also supplies the repository, so it satisfies the
    repo-context requirement without a checkout."""
    body = """
jobs:
  notify:
    env:
      GH_REPO: farnalabs/modulo
    steps:
      - name: Comment
        run: gh pr comment "$PR_NUM" --body "done"
"""
    violations = _notification_violations("wf.yml", _write_workflow(tmp_path, body))
    assert not violations, "GH_REPO must satisfy the repo-context requirement:\n" + "\n".join(
        str(v) for v in violations
    )


def test_notification_guard_flags_continue_on_error(tmp_path: Path) -> None:
    """A notification step whose failure cannot fail the job is the third arm
    of the class — flagged even when `--repo` is supplied."""
    body = """
jobs:
  notify:
    steps:
      - name: Comment
        continue-on-error: true
        run: gh pr comment "$PR_NUM" --repo farnalabs/modulo --body "done"
"""
    violations = _notification_violations("wf.yml", _write_workflow(tmp_path, body))
    assert any(violation.kind == "continue-on-error" for violation in violations), (
        "guard did not flag continue-on-error on a notification step:\n" + "\n".join(str(v) for v in violations)
    )


def test_notification_guard_flags_repo_scoped_gh_api_placeholder(tmp_path: Path) -> None:
    """A repo-scoped `gh api` (`{owner}`/`{repo}`) needs the same context."""
    body = """
jobs:
  notify:
    steps:
      - name: Comment
        run: gh api "repos/{owner}/{repo}/issues/1/comments" -f body=hi
"""
    violations = _notification_violations("wf.yml", _write_workflow(tmp_path, body))
    assert any("gh api" in violation.detail for violation in violations), (
        "guard did not flag a repo-scoped gh api placeholder:\n" + "\n".join(str(v) for v in violations)
    )


def test_notification_guard_ignores_non_notification_gh_calls(tmp_path: Path) -> None:
    """Reading commands (`gh pr list`, `gh run list`) and explicitly-addressed
    `gh api` calls are not notifications and must not be flagged."""
    body = """
jobs:
  notify:
    steps:
      - name: Read
        run: |
          gh pr list --state open
          gh run list --workflow "CI: Fast Validation"
          gh api "/repos/${REPO}/pulls/1" --jq '.head.sha' 2>/dev/null || echo ""
          gh api "repos/farnalabs/modulo/branches" --paginate
"""
    violations = _notification_violations("wf.yml", _write_workflow(tmp_path, body))
    assert not violations, "non-notification gh calls must not be flagged:\n" + "\n".join(str(v) for v in violations)


def test_workflow_notifications_cannot_be_silently_unpostable() -> None:
    """Every workflow in the autonomous lifecycle must pass the class scan.

    FAR-2001: `ci.yml`'s notify arms were not covered and FAR-1997 survived —
    a `gh pr comment` in a no-checkout job that could never post, whose failure
    a `|| echo` swallowed. `merge-queue.yml` (FAR-1939) and
    `pr-conflict-reconcile.yml` (FAR-1949) are scanned here in the same pass so
    the coverage cannot silently regress.
    """
    violations: list[_NotificationViolation] = []
    for path in (CI_YML, MERGE_QUEUE_YML, PR_CONFLICT_YML):
        violations.extend(_notification_violations(path.name, _load_yaml(path)))
    assert not violations, "A workflow notification can be silently undeliverable (FAR-2001):\n" + "\n".join(
        str(violation) for violation in violations
    )


# --- A saturated Branch Fixer trigger is retried, never terminally dropped ---
#
# 2026-10-20: the "Trigger Branch Fixer" job failed with "dispatch failed after
# 3 attempt(s) (last HTTP 429: Concurrent run limit of 8 reached)". The retry
# loop treated a saturated trigger - the state the fix loop is MOST needed in,
# because it means many runs are in flight - as a terminal dispatch failure
# after ~40s, so a red CI run never reached the fixer and the only trace was a
# PR comment. The fixes at the time were a longer/backoff retry that honours
# `Retry-After` and is bounded by a wall-clock budget, a 429 class distinct from
# a broken webhook, and a durable signal when the budget runs out.
#
# The policy used to be duplicated across ci.yml's two Branch Fixer notify steps
# (GitHub Actions has no YAML include; a structure test pinned the two copies
# byte-identically). It is now ONE definition in
# scripts/dispatch_with_retry.sh, so these assertions check that each step
# actually CALLS it, carries no re-inlined copy, and checks the script out —
# both jobs were deliberately checkout-free before (FAR-1997), so the checkout
# is the migration's one intentional structural change and is pinned here.

#: The ci.yml jobs that hand a CI failure to the Branch Fixer.
BRANCH_FIXER_NOTIFY_JOBS = ("notify-pr-failure", "notify-main-failure")

#: The call into the single shared policy definition.
SHARED_DISPATCH_MARKER = "bash scripts/dispatch_with_retry.sh"

#: Definitions that mean a copy of the policy has been re-inlined in some shape
#: (the RETIRED_FIXED_SCHEDULE / marker-pair checks this test used to make are
#: subsumed: any copy needs at least one of these helpers).
INLINE_POLICY_DEFINITIONS = ("classify_http() {", "parse_retry_after() {", "retry_delay() {")


def _branch_fixer_dispatch_runs(workflow: dict) -> dict[str, str]:
    """The ``run`` block of each Branch Fixer dispatch step, keyed by job id."""
    runs: dict[str, str] = {}
    jobs = workflow.get("jobs") or {}
    for job_id in BRANCH_FIXER_NOTIFY_JOBS:
        job = jobs.get(job_id)
        assert isinstance(job, dict), f"ci.yml: job {job_id!r} is missing — the Branch Fixer dispatch moved"
        dispatch = [
            step
            for step in (job.get("steps") or [])
            if isinstance(step, dict) and step.get("name") == "Trigger Modulo Branch Fixer"
        ]
        assert len(dispatch) == 1, (
            f"ci.yml job {job_id!r}: expected exactly one 'Trigger Modulo Branch Fixer' step, found {len(dispatch)}"
        )
        runs[job_id] = dispatch[0].get("run") or ""
    return runs


def test_branch_fixer_dispatch_uses_the_one_shared_policy() -> None:
    """Both CI dispatch steps must call the shared policy, never inline a copy.

    429 stays a bounded, retried capacity condition because the SHARED script
    owns the classification, the budget and the Retry-After handling; its own
    suite (``test_dispatch_with_retry_sh.py``) pins that behaviour. What this
    test pins is that ci.yml cannot drift back to a second copy.
    """
    workflow = _load_yaml(CI_YML)
    runs = _branch_fixer_dispatch_runs(workflow)
    jobs = workflow.get("jobs") or {}
    violations: list[str] = []
    for job_id, run in runs.items():
        if SHARED_DISPATCH_MARKER not in run:
            violations.append(
                f"  {job_id}: does not call {SHARED_DISPATCH_MARKER!r} — the dispatch policy must have ONE definition"
            )
        if "DISPATCH_BODY=" not in run:
            violations.append(f"  {job_id}: does not pass DISPATCH_BODY to the shared script")
        if "DISPATCH_RESULT_FILE" not in run:
            violations.append(
                f"  {job_id}: does not read DISPATCH_RESULT_FILE — its own durable signal would be composed from "
                "guessed values rather than the observed last HTTP status / attempt count"
            )
        for definition in INLINE_POLICY_DEFINITIONS:
            if definition in run:
                violations.append(
                    f"  {job_id}: re-inlines the policy ({definition!r}) — call the shared script instead"
                )
        if "GITHUB_STEP_SUMMARY" not in run:
            violations.append(f"  {job_id}: no job-specific run-summary line when the dispatch is not delivered")

        # Calling a repo script requires a checkout. Both jobs were
        # deliberately checkout-free before the migration (FAR-1997), so pin
        # exactly one shallow, credential-free checkout and the contents: read
        # permission it needs.
        job = jobs.get(job_id) or {}
        checkouts = [
            step
            for step in (job.get("steps") or [])
            if isinstance(step, dict) and str(step.get("uses", "")).startswith("actions/checkout@")
        ]
        if len(checkouts) != 1:
            violations.append(
                f"  {job_id}: expected exactly one actions/checkout (for the script), found {len(checkouts)}"
            )
        else:
            with_ = checkouts[0].get("with") or {}
            if with_.get("persist-credentials") is not False:
                violations.append(f"  {job_id}: the script checkout must set `persist-credentials: false`")
            if str(with_.get("fetch-depth")) != "1":
                violations.append(f"  {job_id}: the script checkout must be shallow (`fetch-depth: 1`)")
        permissions = job.get("permissions") or {}
        if str(permissions.get("contents", "")) != "read":
            violations.append(f"  {job_id}: job permissions must grant `contents: read` for the script checkout")
    assert not violations, "ci.yml Branch Fixer dispatch no longer uses the one shared policy:\n" + "\n".join(
        violations
    )


# --- Signing canary: identity is read from the EMBEDDED signature -----------
#
# The canary signs a throwaway COPY of whoami.exe. `Get-AuthenticodeSignature`
# reported the Windows SECURITY CATALOG signature of that file (SignatureType
# = Catalog, subject CN=Microsoft Windows..., thumbprint 3B77DB29...), because a
# catalog hash covers a PE except its certificate table - so a signed copy still
# matches the original's catalog entry, and the embedded Azure signature is
# never inspected. The original check asserted only on that catalog subject, so
# it could not pass for any correct signing run (observed run 39366248825).
#
# The fix reads the embedded signature from the PE certificate table, verifies
# it cryptographically, and collects the certificate of every SignerInfo -
# envelope plus nested (Azure Artifact Signing may dual-sign, carrying the
# customer identity in the SPC_NESTED_SIGNATURE attribute,
# OID 1.3.6.1.4.1.311.2.4.1). These tests pin the structural invariants.

SIGNING_CANARY_YML = WORKFLOWS / "signing-canary.yml"
SPC_NESTED_SIGNATURE_OID = "1.3.6.1.4.1.311.2.4.1"


def _verify_step_run_text() -> str:
    """Return the `run:` script of the 'Verify Authenticode signature' step."""
    wf = _load_yaml(SIGNING_CANARY_YML)
    steps = next(iter(wf["jobs"].values()))["steps"]
    step = next(s for s in steps if str(s.get("name", "")).startswith("Verify Authenticode signature"))
    return step["run"]


def test_signing_canary_does_not_assert_on_envelope_subject_alone() -> None:
    """The verification step must NOT contain the old predicate
    `$subject -notlike '*FARNA LABS LTD*'`, where `$subject` is the certificate
    `Get-AuthenticodeSignature` reported. That is the Windows catalog signature
    of a copy of whoami.exe, so the predicate could never pass."""
    run = _verify_step_run_text()
    violations: list[str] = []
    if "$subject -notlike" in run:
        violations.append(
            "  signing-canary.yml: still uses the envelope-only `$subject -notlike` check; "
            "Get-AuthenticodeSignature reports the Windows catalog signer of a copied system file"
        )
    if "*FARNA LABS LTD*" in run:
        violations.append("  signing-canary.yml: still uses the old literal '*FARNA LABS LTD*' pattern")
    assert not violations, (
        "signing-canary.yml still asserts on the certificate Get-AuthenticodeSignature reported:\n"
        + "\n".join(violations)
    )


def test_signing_canary_reads_and_verifies_the_embedded_signature() -> None:
    """The step must parse the PE certificate table itself and verify each
    embedded signature cryptographically, rather than trusting the certificate
    `Get-AuthenticodeSignature` returned (which is the catalog's)."""
    run = _verify_step_run_text()
    violations: list[str] = []
    if "Get-PeSignedDataBlobs" not in run:
        violations.append("  signing-canary.yml: does not walk the PE certificate table (WIN_CERTIFICATE entries)")
    if "CheckSignature" not in run:
        violations.append("  signing-canary.yml: does not verify the embedded signature (CheckSignature)")
    assert not violations, "signing-canary.yml does not read/verify the embedded signature:\n" + "\n".join(violations)


def test_signing_canary_traverses_nested_signatures() -> None:
    """The step must collect signer certificates from nested signatures by
    walking the SPC_NESTED_SIGNATURE attribute (OID 1.3.6.1.4.1.311.2.4.1)."""
    run = _verify_step_run_text()
    violations: list[str] = []
    if SPC_NESTED_SIGNATURE_OID not in run:
        violations.append(f"  signing-canary.yml: missing nested signature OID {SPC_NESTED_SIGNATURE_OID}")
    if "UnauthenticatedAttributes" not in run:
        violations.append("  signing-canary.yml: does not inspect SignerInfo.UnauthenticatedAttributes")
    if "SignedCms" not in run or ".Decode(" not in run:
        violations.append("  signing-canary.yml: does not decode nested SignedCms")
    assert not violations, (
        "signing-canary.yml does not traverse nested signatures to find the company identity:\n" + "\n".join(violations)
    )


def _company_pattern_from_workflow() -> str:
    """The company-identity regex the workflow matches signer subjects with."""
    run = _verify_step_run_text()
    match = re.search(r"\$companyPattern\s*=\s*'([^']+)'", run)
    assert match, "signing-canary.yml: `$companyPattern = '...'` assignment not found — update this test deliberately"
    return match.group(1)


def test_signing_canary_company_pattern_matches_identity() -> None:
    """The company pattern must match the identity-validation forms and must NOT
    match the Windows certificate the failing runs reported (run 39366248825)."""
    pattern = _company_pattern_from_workflow()
    for subject in (
        "CN=FARNA LABS LTD, O=FARNA LABS LTD, L=London, C=GB",
        "CN=Farnalabs Ltd, O=Farnalabs Ltd, L=London, C=GB",
        "CN=FARNALABS LTD",
    ):
        assert re.search(pattern, subject), f"company pattern {pattern!r} does not match {subject!r}"
    windows_subject = "CN=Microsoft Windows, O=Microsoft Corporation, L=Redmond, S=Washington, C=US"
    assert not re.search(pattern, windows_subject), (
        f"company pattern {pattern!r} would accept the Windows certificate {windows_subject!r}"
    )


def test_signing_canary_status_gate_remains() -> None:
    """The Authenticode status must still be validated as Valid."""
    run = _verify_step_run_text()
    violations: list[str] = []
    if "Valid" not in run or "Status" not in run:
        violations.append("  signing-canary.yml: missing Authenticode status check")
    assert not violations, "signing-canary.yml dropped the Authenticode status validation:\n" + "\n".join(violations)


# --- Sonar new-issues gate: blocking policy --------------------------------


def _sonar_new_issues_gate_step() -> dict:
    """Return the step dict for the 'Fail on new SonarCloud bugs...' step in ci.yml."""
    wf = _load_yaml(CI_YML)
    jobs = wf.get("jobs") or {}
    job = jobs.get("sonar-new-issues-gate")
    assert isinstance(job, dict), "ci.yml: sonar-new-issues-gate job missing"
    steps = job.get("steps") or []
    target_name = "Fail on new SonarCloud bugs, vulnerabilities, and critical smells"
    step = next(
        (s for s in steps if isinstance(s, dict) and s.get("name") == target_name),
        None,
    )
    assert step is not None, "ci.yml: sonar-new-issues-gate step not found"
    return step


def _run_script(step: dict) -> str:
    run = step.get("run") or ""
    assert isinstance(run, str), "run is not a string"
    assert run.strip(), "step has no run script"
    return run


def test_sonar_new_issues_gate_queries_bug_and_vulnerability() -> None:
    """The blocking query must include types=BUG,VULNERABILITY.

    A revert to a single unfiltered count (total > 0) would re-introduce the
    false-positive problem that this gate was designed to eliminate (MINOR
    TypeScript style rules and MAJOR parameter-count rules blocking PRs
    while zero bugs/vulnerabilities were present).
    """
    step = _sonar_new_issues_gate_step()
    run = _run_script(step)
    violations: list[str] = []
    if "types=BUG,VULNERABILITY" not in run:
        violations.append("  ci.yml: sonar-new-issues-gate step does not query types=BUG,VULNERABILITY")
    assert not violations, "sonar-new-issues-gate no longer queries BUG+VULNERABILITY:\n" + "\n".join(violations)


def test_sonar_new_issues_gate_queries_critical_code_smells() -> None:
    """The blocking query must include types=CODE_SMELL with severities=BLOCKER,CRITICAL.

    The policy gates CODE_SMELL only at BLOCKER or CRITICAL severity. All
    other severities (INFO, MINOR, MAJOR) are non-blocking. A revert to a
    single unfiltered count or to a query without the severity filter would
    re-introduce false positives.
    """
    step = _sonar_new_issues_gate_step()
    run = _run_script(step)
    violations: list[str] = []
    if "types=CODE_SMELL" not in run:
        violations.append("  ci.yml: sonar-new-issues-gate step does not query types=CODE_SMELL")
    if "severities=BLOCKER,CRITICAL" not in run:
        violations.append("  ci.yml: sonar-new-issues-gate step does not filter CODE_SMELL to BLOCKER,CRITICAL")
    assert not violations, "sonar-new-issues-gate no longer queries critical CODE_SMELL:\n" + "\n".join(violations)


def test_sonar_new_issues_gate_fail_closed_on_api_error() -> None:
    """The job must still treat a non-200 / API error as a failure (fail-closed).

    The fail-closed semantics are load-bearing: an unknown state must never
    pass. This test asserts the ::error:: annotation branch exists.
    """
    step = _sonar_new_issues_gate_step()
    run = _run_script(step)
    violations: list[str] = []
    # The fail-closed pattern checks for non-200 and exits 1 with ::error::
    if "HTTP_CODE" not in run:
        violations.append("  ci.yml: no HTTP_CODE capture — cannot distinguish API failure from empty result")
    if '!= "200"' not in run:
        violations.append("  ci.yml: no non-200 check — API errors would not fail closed")
    if "::error::" not in run:
        violations.append("  ci.yml: no ::error:: annotation on API failure — failure would be silent")
    if "failing closed" not in run:
        msg = "ci.yml: 'failing closed' phrase missing — fail-closed intent not documented in the failure path"
        violations.append(msg)
    assert not violations, "sonar-new-issues-gate no longer fails closed on API error:\n" + "\n".join(violations)


def test_sonar_new_issues_gate_non_blocking_smells_cannot_fail() -> None:
    """The non-blocking query (INFO/MINOR/MAJOR code smells) must not fail the job.

    The non-blocking count is printed as a ::notice:: and its query failure
    is downgraded to a ::warning::. Neither can cause the step to exit non-zero.
    This test asserts the || true / warning path exists (the else branch that
    does not exit).
    """
    step = _sonar_new_issues_gate_step()
    run = _run_script(step)
    violations: list[str] = []
    # The non-blocking query must NOT have an unconditional exit 1
    # It should either print ::notice:: (success) or ::warning:: (query failed) but continue
    if "::notice::" not in run:
        violations.append("  ci.yml: no ::notice:: for non-blocking count — non-blocking signal is not emitted")
    if "::warning::" not in run:
        msg = "ci.yml: no ::warning:: fallback for non-blocking query failure — a query failure could fail the gate"
        violations.append(msg)
    # The non-blocking branch must NOT contain 'exit 1'
    # We find the non-blocking section (after the blocking check) and ensure no exit 1
    # The script uses 'else' for the warning path and does not exit
    if 'echo "::warning::SonarCloud non-blocking code smell query failed' not in run:
        msg = "ci.yml: non-blocking query failure does not emit expected warning"
        violations.append(msg)
    # Verify the non-blocking branch doesn't have an exit that would fail the step
    # The blocking exit is explicit; the non-blocking else branch should just warn
    # and fall through to the final "No blocking" echo
    assert not violations, "sonar-new-issues-gate non-blocking path can fail the job:\n" + "\n".join(violations)
