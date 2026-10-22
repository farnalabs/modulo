"""Architecture test: the webhook-dispatch retry policy must be the ONE shared
script, applied by every workflow step that POSTs to a Modulo webhook.

Enforced against REPO_ROOT/.github/workflows, with NO allowlist:

* no workflow computes the webhook HMAC inline or defines the retry policy's
  helpers (that is the duplicated policy this sweep removed);
* no workflow inlines a five-step webhook retry ladder;
* every workflow that references a MODULO_*_WEBHOOK_URL secret flows through
  the script invocation (`DISPATCH_BODY=... bash scripts/dispatch_with_retry.sh`),
  exactly once per dispatch site.

The former allowlist (`ci.yml`, `merge-queue.yml`) is gone: both were migrated
to the shared script. `runner-ci-harness.yml` and other non-webhook retry loops
(compose boot, readiness polls, Playwright installs) remain out of scope by
design — the rules here are scoped to files that reference a webhook secret, so
plain retry loops elsewhere are not this test's business.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"

# Per-workflow call counts the sweep landed: pin the migrated shape, so a
# dispatch site cannot be added or dropped silently.
EXPECTED_SCRIPT_CALLS = {
    "branch-fixer.yml": 1,
    "ci.yml": 2,
    "deploy.yml": 5,
    "merge-queue.yml": 3,
    "pr-review.yml": 1,
    "fast-lane-suspension.yml": 1,
    "fast-lane-post-merge.yml": 1,
    "pr-conflict-reconcile.yml": 1,
}

SCRIPT_MARKER = "bash scripts/dispatch_with_retry.sh"

#: The Modulo webhook secrets. Any workflow that names one is a dispatch site
#: and must route through the shared script.
WEBHOOK_SECRET_MARKERS = (
    "MODULO_BRANCH_FIXER_WEBHOOK_URL",
    "MODULO_PR_REVIEW_WEBHOOK_URL",
)

#: The retry policy's helpers. Their DEFINITION in a workflow means the policy
#: has been re-inlined in some shape (the shared script defines them once).
INLINE_POLICY_MARKERS = (
    "classify_http() {",
    "parse_retry_after() {",
    "retry_delay() {",
)


def _workflow_files() -> list[Path]:
    files = sorted(WORKFLOWS_DIR.glob("*.yml")) + sorted(WORKFLOWS_DIR.glob("*.yaml"))
    assert files, "no workflow files found — search space is wrong"
    return files


def test_shared_dispatch_script_exists_and_parses() -> None:
    script = REPO_ROOT / "scripts" / "dispatch_with_retry.sh"
    assert script.is_file()
    assert script.stat().st_mode & 0o111  # executable bit
    check = subprocess.run(  # noqa: S603 — fixed argv, repo script under test
        ["bash", "-n", str(script)],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert check.returncode == 0
    assert not check.stderr


@pytest.mark.parametrize(("workflow", "count"), sorted(EXPECTED_SCRIPT_CALLS.items()))
def test_migrated_workflows_invoke_the_script_exactly_once_per_site(
    workflow: str,
    count: int,
) -> None:
    text = (WORKFLOWS_DIR / workflow).read_text(encoding="utf-8")
    assert text.count(SCRIPT_MARKER) == count
    assert "DISPATCH_BODY=" in text


def test_no_workflow_inlines_webhook_hmac_signing_or_the_policy() -> None:
    inline_markers = [
        "openssl dgst -sha256 -hmac",
        "X-Modulo-Webhook-Secret: sha256=",
        *INLINE_POLICY_MARKERS,
    ]
    for path in _workflow_files():
        text = path.read_text(encoding="utf-8")
        for marker in inline_markers:
            assert marker not in text, (
                f"{path.name}: webhook dispatch policy inlined ({marker!r}); "
                "every dispatch step must call scripts/dispatch_with_retry.sh"
            )


def test_every_webhook_workflow_routes_through_the_shared_script() -> None:
    referencing = [
        path for path in _workflow_files() if any(m in path.read_text(encoding="utf-8") for m in WEBHOOK_SECRET_MARKERS)
    ]
    assert referencing, "no workflow references a Modulo webhook secret — search space is wrong"
    for path in referencing:
        text = path.read_text(encoding="utf-8")
        assert SCRIPT_MARKER in text, (
            f"{path.name}: references a Modulo webhook secret but never calls the shared "
            "dispatch script — a dispatch site is bypassing the one retry policy"
        )


def test_no_workflow_inlines_the_webhook_retry_ladder() -> None:
    # Exact 5-attempt ladder spellings: the inline webhook loops this sweep
    # removed. Shorter "for attempt in 1 2 3" loops (compute-gate retries,
    # deploy handshakes) are NOT webhook dispatches and are deliberately out of
    # scope; the rule scopes to webhook-bearing files so unrelated retry loops
    # elsewhere (e.g. runner-ci-harness.yml's docker-compose boot sequence) are
    # not this test's business.
    ladder_markers = [
        "for i in 1 2 3 4 5",
        "for attempt in 1 2 3 4 5",
    ]
    for path in _workflow_files():
        text = path.read_text(encoding="utf-8")
        if not any(marker in text for marker in WEBHOOK_SECRET_MARKERS):
            continue
        for marker in ladder_markers:
            assert marker not in text, (
                f"{path.name}: a 5-attempt webhook retry ladder is inlined "
                f"({marker!r}); every dispatch step must call scripts/dispatch_with_retry.sh"
            )
