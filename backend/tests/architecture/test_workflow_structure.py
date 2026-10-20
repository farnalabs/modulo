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
- **Merge-queue CI gate is product-CI-only** — every per-branch CI gate in
  ``merge-queue.yml`` must query only the product-CI workflow
  (``CI: Fast Validation``), never the review workflow, so a review-dispatch
  outage can never block merges again.
- **An undelivered Branch Fixer notification is never silent** — every webhook
  POST in ``merge-queue.yml`` must name the affected PR(s) and comment on them
  when the dispatch ultimately fails (FAR-1939).
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

#: The curl invocation every Branch Fixer webhook POST funnels through. All
#: three notify steps (merge-conflict, migration-collision, CI-failure) send
#: their payload with exactly this line.
BRANCH_FIXER_POST = '-X POST "$WEBHOOK_URL"'

#: The guard that opens a notify step's ultimate-failure arm (after the retry
#: loop). Everything from here to the end of the ``run`` block is the failure
#: path that must be attributable, durable and still failing.
DISPATCH_FAILURE_GUARD = 'if [ "$DISPATCHED" != "true" ]; then'


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
    affected PR(s), comment on each of them, and still fail the step.
    """
    steps = _queue_steps(_load_yaml(MERGE_QUEUE_YML))
    posting = [step for step in steps if BRANCH_FIXER_POST in (step.get("run") or "")]
    assert len(posting) >= 3, (
        "merge-queue.yml: expected the three Branch Fixer webhook POST steps "
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
        attributed = [line.strip() for line in arm.splitlines() if "::error::" in line and "not delivered" in line]
        if not attributed:
            violations.append(f"  {name!r}: failure arm has no 'was not delivered' ::error:: annotation")
        for line in attributed:
            if "PR" not in line or "${" not in line:
                violations.append(f"  {name!r}: failure-arm ::error:: does not name the affected PR(s): {line!r}")
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
                f"  {name!r}: failure arm has no `exit 1` — an undelivered notification must keep failing the step"
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
