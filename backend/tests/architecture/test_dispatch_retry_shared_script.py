"""Architecture test: the webhook-dispatch retry policy must be the ONE shared
script, applied by every workflow step that POSTs to a Modulo webhook.

Enforced against REPO_ROOT/.github/workflows:

* no non-allowlisted workflow computes the webhook HMAC inline any more
  (that is the duplicated policy this sweep removed);
* no non-allowlisted workflow inlines a five-step webhook retry ladder;
* every non-allowlisted MODULO_*_WEBHOOK_URL usage flows through the script
  invocation (`DISPATCH_BODY=... bash scripts/dispatch_with_retry.sh`).

ALLOWLIST (justified, not blanket):
* ci.yml owns an inline copy whose own structure test pins its two sites
  byte-identically while a PR is in flight; folding it into the script is a
  separate follow-up (that structure test flips at the same time).
* merge-queue.yml is excluded from this sweep entirely (its sites are being
  reconciled by another in-flight PR).
* runner-ci-harness.yml and other non-webhook retry loops (compose boot,
  readiness polls, Playwright installs) are NOT webhook dispatches and stay
  untouched by design — the HMAC/ladder rules here are scoped to webhook
  signatures, so plain retry loops elsewhere are not this test's business.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"

# Temporarily allowed to keep inlining the policy (justified above).
ALLOWLIST = {"ci.yml", "merge-queue.yml"}

# Per-workflow artifact call counts the sweep landed: pin the migrated shape.
EXPECTED_SCRIPT_CALLS = {
    "branch-fixer.yml": 1,
    "deploy.yml": 5,
    "pr-review.yml": 1,
    "fast-lane-suspension.yml": 1,
    "fast-lane-post-merge.yml": 1,
    "pr-conflict-reconcile.yml": 1,
}

SCRIPT_MARKER = "bash scripts/dispatch_with_retry.sh"


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


def test_no_non_authorized_workflow_inlines_webhook_hmac_signing() -> None:
    inline_markers = [
        "openssl dgst -sha256 -hmac",
        "X-Modulo-Webhook-Secret: sha256=",
    ]
    for path in _workflow_files():
        if path.name in ALLOWLIST:
            continue
        text = path.read_text(encoding="utf-8")
        for marker in inline_markers:
            assert marker not in text, (
                f"{path.name}: webhook HMAC policy inlined ({marker!r}); "
                "migrate to scripts/dispatch_with_retry.sh or extend the allowlist "
                "with justification"
            )


def test_no_non_authorized_workflow_inlines_the_webhook_retry_ladder() -> None:
    # Exact 5-attempt ladder spellings: exactly the inline webhook loops this
    # sweep removed. Shorter "for attempt in 1 2 3" loops (compute-gate
    # retries, deploy handshakes) are NOT webhook dispatches and are
    # deliberately out of scope, as are merge-queue.yml's remaining sites
    # (another PR owns that file). The rule scopes to webhook-bearing files
    # so unrelated retry loops elsewhere (e.g. runner-ci-harness.yml's
    # docker-compose boot sequence) are not this test's business.
    ladder_markers = [
        "for i in 1 2 3 4 5",
        "for attempt in 1 2 3 4 5",
    ]
    for path in _workflow_files():
        if path.name in ALLOWLIST:
            continue
        text = path.read_text(encoding="utf-8")
        if "WEBHOOK_URL" not in text:
            continue
        for marker in ladder_markers:
            assert marker not in text, (
                f"{path.name}: a 5-attempt webhook retry ladder is inlined "
                f"({marker!r}); migrate to scripts/dispatch_with_retry.sh or "
                "extend the allowlist with justification"
            )
