"""Sandbox policy ENFORCEMENT surface (FAR-212 PR B).

PR A derived the sandbox capability surface (``sandbox.egress``,
``sandbox.write_files``, ``sandbox.git_credentials``) MECHANICALLY from the
node's validated config, but ``sandbox.write_files`` and
``sandbox.git_credentials`` stayed unknown (None) because their enforcement
surfaces did not exist — node_runner/e2b never made writes impossible or scoped
git credentials, so certifying those capabilities would have been a
deny-guarantee nothing enforced (fail-open through the raw import path).

This module is that missing enforcement surface. It builds shell scripts that
node_runner runs inside the E2B sandbox AFTER provisioning (and after the
Modulo-owned context files / prompt / input are written, but BEFORE the agent
or script command executes), so each declared control is genuinely in force
when the agent runs:

  - ``read_only`` (write_files = False certification): chmod the workspace tree
    (``/home/user``) read-only for the non-root agent/script user, so a write
    attempt by the agent fails at the filesystem layer. The agent's OWN runtime
    files — the stdout/stderr redirect target (``/home/user/agent.log``) and the
    ``/home/user/output.json`` deliverable — are pre-created and re-opened
    writable AFTER the seal, so the agent can emit its log and result without
    being able to modify anything else in the workspace.
  - ``git_credentials`` (scoped git certification): for ``scoped``, configure a
    git credential helper that ONLY grants the provisioned token to
    ``github.com`` and refuses every other host; for ``none``, configure a
    credential helper that always refuses (no git credentials reach the agent).
  - ``egress_policy="selected"`` (selected-mode allowlist): drop all
    firewall/route-based egress, then add back ONLY the allowlisted host:port
    pairs. This upgrades ``selected`` from the FAR-296 Phase 3b-3
    "functionally equivalent to deny_all" state to a REAL allowlist.
  - ``single_pr_per_run`` (FAR-1273; guard introduced by FAR-1264): install a
    run-scoped ``gh`` shim that permits exactly ONE ``gh pr create`` per
    sandbox run — a bounded, best-effort, E2B-only defence-in-depth layer
    behind the prompt-level "exactly one PR per run" rule (FAR-1254), NOT an
    absolute guarantee on its own. For the ``gh`` binaries it managed to
    guard, a second ``gh pr create`` in the same run exits non-zero WITHOUT
    invoking the real ``gh``; every other ``gh`` invocation passes through
    untouched. Coverage is bounded: ``gh api`` PR creation, a ``gh`` copy
    outside the PATH, shell aliases/functions, a ``gh`` installed into the
    PATH after the install, and every non-E2B dispatch (docker / local) are
    NOT intercepted — non-E2B runners are simply not covered unless the
    FAR-1315 exec install below lands the shim (it does not on the shipped
    runner image; see that bullet). The
    one-PR-per-run guarantee is this shim TOGETHER WITH the post-run
    detection + admin alert (FAR-1274); neither half prevents a second PR
    alone. The claim is held only for a create that SUCCEEDED — a non-zero
    exit releases it, so a transient failure does not burn the run's only
    attempt — and the install's own diagnostics (including the "no gh on
    PATH" case, where the shim is ABSENT) are mirrored into the policy log
    rather than discarded with the step result.
  - ``single_pr_per_run`` RUN scope across nodes (FAR-1315): the marker above is
    per-SANDBOX, so two flagged nodes in one run would each install a fresh,
    claimable marker and could produce two PRs. A platform-side claim LEDGER
    keyed by the run id (``acquire_run_pr_guard`` / ``settle_run_pr_guard`` -
    process-local, shared by every node of the run in the engine process, and
    BOUNDED to ``_MAX_RUN_PR_GUARD_CLAIMS`` entries) fixes that: the FIRST
    flagged node of a run installs the live guard, every later flagged node
    installs a PRE-PLANTED refusal (its marker directory already exists, so its
    first ``gh pr create`` is refused without calling gh). The dispatch layer
    settles the node's slot when the node finishes, from SPEND EVIDENCE that is
    robust to output truncation and not forgeable by merely reading the shim
    (the shim is mode 755 in a PATH dir, so ``cat $(command -v gh)`` exposes a
    fixed sentinel string - a sentinel alone must therefore NEVER spend a run):
      1. a platform-side HARVEST of the shim's success RECEIPT file
         (``$MARKER/<receipt>``, written only when ``gh pr create`` exited 0 -
         see ``harvest_gh_pr_claim_via_exec``, driven by the bounded,
         cancellation-safe ``harvest_gh_pr_claim_bounded``), taken after the
         node while the sandbox/container is still alive - but ONLY COUNTS AS
         DEFINITIVE when
         the install status (threaded into ``settle_run_pr_guard``) says a shim
         actually landed (``installed`` / ``pre_planted``): a probe against a
         path no shim ever wrote (install ``absent``/``failed``, e.g. the
         shipped runner image with no ``gh``) answers ABSENT just the same, so
         it is treated as UNKNOWN and falls through to the other signals;
       2. a URL-valid delivered ``pr_url`` that the platform ALSO corroborates
          in its own capture of the node's transcript (the same URL must appear
          in the captured streams - see ``_pr_url_seen_in_streams``; raw
          ``output.json`` text alone NEVER spends, and never overrides a
          definitive receipt);
       3. ONLY when the receipt is unknown (harvest unavailable - e.g. a
          stall/timeout kill - or a non-live install) does the stdout claim
          SENTINEL count, as the best-effort fallback.
    Receipt CONFIRMED ABSENT by a LIVE shim -> ``pr_url`` and the sentinel are
    both ignored and the owner's hold is RELEASED, so a node that merely
    printed the sentinel (by reading the shim) can never burn the run's
    attempt. The harvest itself runs through
    ``harvest_gh_pr_claim_bounded`` (bounded AND cancellation-safe: the probe
    task is always awaited-or-cancelled, and a cancellation landing in the
    dispatch ``finally`` is recorded so the teardown still runs before it is
    re-raised). The marker/receipt PATH and the ledger key both canonicalise a
    UUID-shaped run scope first, so the install side (``str(spec.run_id)``) and
    the harvest side (raw run id) can never disagree about which path to probe.
    SCOPE CALL (see ``build_gh_pr_guard_script``'s TAMPER MODEL):
    DELIBERATE evasion - pre-creating or deleting the marker/receipt - is OUT
    OF SCOPE, and deleting the marker/receipt now ALSO neutralises the
    sentinel arm. The residual (harvest unavailable AND sentinel observed) is
    documented in ``docs/product-map/core/runtime-provider-core.md``. What is
    shared across nodes is the LEDGER, never the sandbox filesystem: the
    marker itself stays inside each sandbox.
  - Non-E2B tiers (FAR-1315): ``apply_sandbox_policy`` runs in the E2B sandbox
    only. ``install_gh_pr_guard_via_exec`` installs the SAME shim (with the same
    ledger plan) through any provider's ``exec_command`` primitive - the Bundled
    Runner (``runner_docker``) dispatch calls it for flagged nodes and surfaces a
    LOUD warning naming the tier when the guard could not be installed.
    HONEST TIER BOUNDARY: the shipped first-party runner image
    (``deploy/docker/runner-opencode.Dockerfile``) ships NO ``gh`` and runs
    ``ReadonlyRootfs`` as uid 1001 with no writable PATH dir, so on that image
    the install always reports ``absent``/``failed`` and the tier is
    prompt-level-guarded only. The machinery is best-effort and becomes
    effective only on an image that provides a writable PATH entry holding
    ``gh``; it is NOT effective on the shipped image today (see
    ``GhPrGuardInstallResult`` and the product map).

The enforcement is REAL (the sandbox cannot write / egress is scoped), never a
declared flag. Script builders are pure string functions (unit-testable without
a sandbox); :func:`apply_sandbox_policy` runs them in the sandbox with bounded
timeouts. The git-credential steps and the read-only seal are
ENFORCEMENT-CRITICAL and RAISE on failure (a failed chmod / helper install must
dispatch a failure, never silently certify a deny-guarantee nothing enforces);
the egress step and the ``gh``-guard install are best-effort (their failures are
logged-and-continued: egress is drop-first fail-closed, and a missing ``gh``
guard simply degrades to the prompt-level guard). node_runner invokes
:func:`apply_sandbox_policy` when ANY of the policy fields — including the
FAR-1273 ``single_pr_per_run`` flag — is set.

This module is dependency-free (no LangGraph, no DB) so it can be imported by
node_runner and the unit tests without dragging in the pipeline engine.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shlex
import threading
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from modulo.core.pipeline_engine.sandbox_mode import _SANDBOX_GIT_CREDENTIAL_ALLOWED_HOST as _GIT_ALLOWED_HOST
from modulo.core.pipeline_engine.sandbox_mode import is_valid_egress_host

_log = logging.getLogger(__name__)

# FAR-798 (PR #430 review finding #3): host names are interpolated raw into a
# shell ``case`` pattern position, so shell metacharacters (``| * ? [ ]``) could
# corrupt the generated case statement / inject. Reject anything that is not a
# strict hostname label, and reject env-var names that are not valid POSIX
# identifiers, BEFORE the script is built — fail-closed (ValueError) rather than
# emitting a malformed or injectable helper.
_HOSTNAME_RE = re.compile(r"[A-Za-z0-9.\-]+")
_ENV_VAR_RE = re.compile(r"[A-Za-z_]\w*", re.ASCII)

# The E2B sandbox runs the agent as the DEFAULT NON-ROOT user (node_runner
# starts the agent command without a ``user`` override — see
# node_runner.py:3786 — so it runs as the sandbox's default unprivileged user,
# whose home is ``/home/user``). Root has write access regardless of file mode
# bits, so chmodding the workspace read-only only binds the agent's unprivileged
# user — which is exactly what we enforce.
_WORKSPACE = "/home/user"

# The agent's ``git`` reads ``$HOME/.gitconfig`` (= ``/home/user/.gitconfig``),
# never ``/root/.gitconfig``. Any ``git config --global`` run as ROOT would
# therefore write the ROOT user's config and silently never bind the agent's
# git (fail-open). All git-credential policy steps must install the helper into
# ``_AGENT_GIT_CONFIG``, the file the agent's git actually reads. The allowlisted
# host for a SCOPED git credential is imported from ``sandbox_mode`` (single
# source of truth for the allowlisted host).
_AGENT_GIT_CONFIG = f"{_WORKSPACE}/.gitconfig"

# FAR-1273: the one-PR-per-run guard's trigger is the explicit node flag
# ``single_pr_per_run``, carried on the typed ``IsolationPolicy`` (the single
# carrier from node_runner's policy call site to the E2B provider's
# ``apply_isolation``). The pre-FAR-1273 ``workspace_metadata`` sentinel key
# (``DELIVERY_SENTINEL_SPEC_KEY``) is deliberately GONE: a delivery sentinel
# keeps only its FAR-228 idempotency meaning and must never arm this guard.

# FAR-1264: the marker ROOT. ``/tmp`` is deliberately OUTSIDE the read-only
# workspace seal (``build_read_only_script`` only chmods ``/home/user``), so
# the agent user can always claim the marker at ``gh pr create`` time even on
# a read-only node. ``run_scope`` (the run id) keys the marker so a workspace
# that somehow outlived its run can never leak a claimed marker into the next
# run.
#
# FAR-1315 CORRECTION: the marker is scoped to a run only WITHIN ONE sandbox.
# Each sandbox node provisions its own workspace, so a pipeline with TWO
# flagged nodes has two separate marker filesystems and two independently
# claimable markers — run-scoping the NAME does not stop one PR per node.
# The cross-node guarantee is the platform-side run ledger above
# (``acquire_run_pr_guard`` / ``settle_run_pr_guard``): the second node's
# install is DENIED by the ledger and pre-plants its marker instead. What is
# shared across nodes is that ledger (process-local), never this /tmp marker.
#
# The /tmp root is a deliberate, code-reviewed S5443 tradeoff — not an
# unchecked host tempfile. It is a fixed in-SANDBOX path that must survive the
# workspace seal; the marker is a claim DIRECTORY created by atomic ``mkdir``
# as the sandbox agent user, its name is run-scoped and sanitised to
# ``[A-Za-z0-9._-]``, it holds NO credentials, and the whole guard is
# best-effort: a hostile/pre-created marker can at worst refuse one run's
# ``gh pr create`` (fail-closed), never read, redirect, or escalate anything.
# The NOSONAR below documents that rationale on the flagged line (matching the
# docker.py / db/bootstrap.py /tmp-literal precedent) so the rule stays
# suppressed through review instead of re-opening as a false positive.
_GH_PR_GUARD_MARKER_ROOT = "/tmp"  # noqa: S108  # nosec B108  # NOSONAR S5443 - documented run-scoped in-sandbox marker root (see block comment above)

# Scope fragments are embedded in a filesystem path and single-quoted into a
# shell script - reduce them to a safe alphabet instead of raising (the guard
# step is best-effort; it must never fail the dispatch).
_GH_PR_GUARD_SCOPE_RE = re.compile(r"[^A-Za-z0-9._-]")

# FAR-1315: the claim SENTINEL the shim prints on stdout after its
# ``gh pr create`` exited 0. The dispatch layer scans the platform-captured
# node output for this exact string and, on a hit, marks the run's ledger claim
# SPENT - which is how node N+1's guard install learns that node N really did
# create a PR (the sandbox filesystem where the marker lives is destroyed with
# node N's sandbox, so the sentinel in the captured transcript is the only
# cross-node observation channel). stdout is the right stream: the agent
# command's output is captured with ``2>&1`` on every dispatch path, and the
# platform already relies on stdout capture for PR-URL extraction.
_GH_PR_GUARD_CLAIM_SENTINEL = "modulo: one-PR-per-run guard: RUN CLAIM ACQUIRED"

# FAR-1315 hardening: the sentinel above is a FIXED literal printed by a shim
# the guarded agent can READ (mode 755 in a PATH dir), so substring-matching it
# in captured output lets an agent that merely cats the shim spend the run's
# claim (a fail-closed DoS: every later flagged node pre-plants and the run
# delivers no PR). The unforgeable channel is the success RECEIPT: a file the
# shim writes INSIDE the marker directory only when ``gh pr create`` exited 0.
# Pre-planted markers (denied installs) never contain it, so its presence is a
# platform-observable, agent-reading-proof "this node created the PR".
_GH_PR_GUARD_CLAIM_RECEIPT = "claimed"

# FAR-1315 (re-gate MAJOR 1): how long the bounded receipt-harvest wrapper
# waits for an already-cancelled probe to finish winding down before giving up
# on it (the probe then finishes in the background with its outcome consumed).
# Short relative to the 20s harvest bound - it only has to let a cancelled
# exec unwind.
_HARVEST_DRAIN_TIMEOUT = 5.0

# Harvest probe tokens: what ``harvest_gh_pr_claim_via_exec``'s one-shot shell
# probe prints for receipt-present / receipt-absent. Kept as constants so the
# probe script and the parser that reads it back cannot drift apart.
_GH_PR_CLAIM_RECEIPT_PRESENT = "MODULO_CLAIM_RECEIPT_PRESENT"
_GH_PR_CLAIM_RECEIPT_ABSENT = "MODULO_CLAIM_RECEIPT_ABSENT"

# FAR-1315 re-gate: install statuses under which a HARVESTED RECEIPT is
# meaningful. Only these say a shim actually landed in this node's workspace,
# so its receipt path is a real path. ``absent`` (no ``gh`` on PATH - the
# shipped runner image) and ``failed`` mean the probe runs against a path no
# shim ever wrote, answers ABSENT, and must NOT be read as "confirmed no
# create": the settle treats it as UNKNOWN and falls back to the other signals
# (see ``settle_run_pr_guard``). ``None`` (status not threaded / isolation
# never ran) is unknown by the same rule.
_GH_PR_GUARD_INSTALL_LIVE_STATUSES = frozenset({"installed", "pre_planted"})

# FAR-1315: the run claim LEDGER is process-local and lives for the life of the
# engine process, so it must be BOUNDED. Beyond this many run scopes the OLDEST
# entries (least likely to still have flagged nodes in flight - insertion
# order) are evicted under the lock. Eviction degrades exactly one run to
# "ledger entry forgotten" (its later flagged nodes can claim again); with the
# bound far above any realistic number of CONCURRENT flagged runs, the hold in
# flight is never the entry that is evicted.
_MAX_RUN_PR_GUARD_CLAIMS = 512

# Installer-outcome substrings (emitted by ``build_gh_pr_guard_script`` on
# stderr; kept as constants so the script text and the classifier that reads it
# back cannot drift apart).
_GH_PR_GUARD_NO_GH_NOTE = "no gh on PATH"
_GH_PR_GUARD_PRE_PLANT_FAILED_NOTE = "could not pre-plant"

# Sentinel returned by ``apply_sandbox_policy``'s best-effort ``_run_step``
# when a step raised and the failure was swallowed (enforce=False). Distinct
# from ``None`` so "the step FAILED" is never conflated with "the step ran and
# returned no result object" when classifying the gh-guard install for the
# FAR-1315 settle.
_POLICY_STEP_FAILED = object()

# ---------------------------------------------------------------------------
# FAR-1315: the platform-side run claim ledger.
#
# A dict keyed by the sanitised run scope, mapping to (state, owner) where
# state is ``"held"`` (a flagged node currently owns the run's one-PR slot) or
# ``"spent"`` (a create succeeded somewhere in this run) and owner identifies
# the holding node (so only THAT node can release its own hold - a node whose
# install was denied must never release the holder's slot). Process-local:
# every node of a run executes in the engine process, so the dict IS the
# shared state the second node observes; it is deliberately NOT a claim about
# cross-process durability (documented in the product map). Lock-guarded
# because flagged nodes may execute concurrently in one event loop.
# ---------------------------------------------------------------------------

_RUN_PR_GUARD_CLAIMS: dict[str, tuple[str, str]] = {}
_RUN_PR_GUARD_LOCK = threading.Lock()


def _canonical_scope_uuid(run_scope: str | None) -> str | None:
    """Canonicalise a UUID-shaped run scope, or ``None`` when it is not one.

    FAR-1315 latent fix: the marker/receipt PATH is built from the run scope
    while the install side threads ``str(spec.run_id)`` (canonical) and the
    harvest/settle sides thread the RAW run id. Today run ids are canonical so
    the two agree by accident; a braced / uppercase / ``urn:uuid:`` / hex-less
    form would make the harvest probe read a path the shim never wrote and
    report a definitive-looking ``False`` against it. Every path/ledger key
    therefore canonicalises FIRST, on both the install and the harvest side.
    """
    if not run_scope:
        return None
    try:
        return str(uuid.UUID(str(run_scope)))
    except (ValueError, TypeError, AttributeError):
        return None


def _run_pr_guard_key(run_scope: str | None) -> str | None:
    """Normalise a run scope into a ledger key, or ``None`` when unscoped.

    UUID-shaped scopes are canonicalised (``str(uuid.UUID(x))``) so the key is
    identical no matter which string form each call site threads (the E2B path
    canonicalises through ``WorkspaceSpec.run_id``; dispatch sites pass the raw
    run id). Non-UUID scopes fall back to the same sanitisation the marker path
    uses. An empty/absent scope returns ``None``: no run id means no cross-node
    sharing, and callers keep the pre-FAR-1315 live-guard behaviour.
    """
    if not run_scope:
        return None
    canonical = _canonical_scope_uuid(run_scope)
    if canonical is not None:
        return canonical
    scope = _GH_PR_GUARD_SCOPE_RE.sub("_", str(run_scope))[:64].strip("._-")
    return scope or None


def acquire_run_pr_guard(run_scope: str | None, guard_owner: str | None = None) -> str:
    """Atomically take the run's one-PR guard slot. Returns the claim status.

    Statuses: ``"acquired"`` (this node may install the LIVE guard - either it
    took the free slot or it re-acquires its OWN hold, so a node retry installs
    normally), ``"spent"`` (a create already succeeded in this run - install a
    pre-planted refusal), ``"held"`` (a DIFFERENT node of this run holds the
    slot - install a pre-planted refusal; this is the concurrent-node case),
    ``"unscoped"`` (no run id - live guard, no sharing possible).
    """
    key = _run_pr_guard_key(run_scope)
    if key is None:
        return "unscoped"
    owner = guard_owner or ""
    with _RUN_PR_GUARD_LOCK:
        entry = _RUN_PR_GUARD_CLAIMS.get(key)
        if entry is None:
            _RUN_PR_GUARD_CLAIMS[key] = ("held", owner)
            # Bounded ledger: evict the OLDEST run scopes (insertion order)
            # once over capacity. The entry just written is the NEWEST, so it
            # is never the one evicted here.
            while len(_RUN_PR_GUARD_CLAIMS) > _MAX_RUN_PR_GUARD_CLAIMS:
                del _RUN_PR_GUARD_CLAIMS[next(iter(_RUN_PR_GUARD_CLAIMS))]
            return "acquired"
        state, entry_owner = entry
        if state == "spent":
            return "spent"
        return "acquired" if entry_owner == owner else "held"


def settle_run_pr_guard(
    run_scope: str | None,
    guard_owner: str | None,
    *streams: str | None,
    pr_url: str | None = None,
    claim_receipt: bool | None = None,
    guard_install_status: str | None = None,
) -> str:
    """Settle this node's ledger slot from platform-observed spend evidence.

    Returns:

    ``"spent"`` - sufficient spend evidence was observed, so the run's slot is
    now spent for every later flagged node; ``"released"`` - no spend evidence
    and this owner held the slot, so the hold is dropped and a later flagged
    node may claim it; ``"noop"`` - this run has no ledger entry for us (never
    acquired, or another owner's hold) - nothing changes. Never raises: it
    runs in a dispatch ``finally``.

    SPEND EVIDENCE (FAR-1315 hardening - chosen so a lost/truncated sentinel
    can never RELEASE a genuinely-spent run, a READ shim can never SPEND an
    unspent one, and agent-authored ``output.json`` text can never OUTRANK a
    definitive receipt):

    1. ``claim_receipt is True`` -> SPENT. The shim wrote its success receipt
       inside the marker dir; unaffected by stream truncation and unforgeable
       by reading the shim (a hand-pre-created receipt is deliberate evasion -
       out of scope, see ``build_gh_pr_guard_script``'s TAMPER MODEL).
    2. ``claim_receipt is False`` AND the install status says a shim actually
       LANDED (``guard_install_status`` in ``installed`` / ``pre_planted``) ->
       NOT spent: ``pr_url`` and the sentinel are BOTH ignored and the hold is
       released. This is the definitive "the live shim ran, no create
       succeeded" answer and it outranks every agent-authored signal - in
       particular ``pr_url``, which comes from the node's own ``output.json``
       and is validated for URL SYNTAX only (a node whose create FAILED can
       still report a URL-shaped ``pr_url``; spending there pre-plants every
       later flagged node and the run then delivers nothing).
    3. The receipt is UNKNOWN - the harvest could not run (``None``: sandbox
       already destroyed, exec failed, cancelled), OR the probe ran against a
       path NO SHIM EVER WROTE (``guard_install_status`` ``absent`` / ``failed``
       / unthreaded, e.g. the shipped runner image has no ``gh``). A probe
       against a non-existent path answers ABSENT just like a real "no receipt"
       probe, so it must NOT be read as "confirmed no create": fall back to the
       other signals - a URL-valid ``pr_url`` CORROBORATED by the platform's own
       capture (below) -> SPENT, else the stdout claim SENTINEL in *streams* ->
       SPENT, else released.
    4. ``pr_url`` corroboration: ``_is_valid_delivered_pr_url(pr_url)`` alone is
       NEVER enough - a URL-valid ``pr_url`` must ALSO appear in the captured
       *streams*, i.e. the platform's own transcription of what the node
       printed (the same ``_PR_URL_PATTERN`` derivation node_runner persists
       into the FAR-188 raw-output marker's ``pr_url``). Raw ``output.json``
       text on its own is agent-authored and never spends. Junk under the key
       (``"N/A"``, ``""``, non-http) never spends either.

    ``guard_install_status`` is threaded by BOTH dispatch call sites from the
    install step itself (``apply_sandbox_policy`` / ``install_gh_pr_guard_via_exec``)
    - see :data:`_GH_PR_GUARD_INSTALL_LIVE_STATUSES`.
    """
    key = _run_pr_guard_key(run_scope)
    if key is None:
        return "noop"
    owner = guard_owner or ""
    observed = any(_GH_PR_GUARD_CLAIM_SENTINEL in stream for stream in streams if stream)
    if claim_receipt is True:
        spend = True
    elif claim_receipt is False and guard_install_status in _GH_PR_GUARD_INSTALL_LIVE_STATUSES:
        # Definitive negative: a LIVE shim's receipt path was probed and the
        # receipt is absent. Agent-authored pr_url and the readable sentinel
        # must both lose to it.
        spend = False
    else:
        # Receipt unknown (harvest unavailable, or the probe ran against a
        # path no shim wrote): fall back to a CORROBORATED pr_url, then the
        # sentinel. The corroboration is what stops a failed create that
        # merely reports a URL-shaped pr_url in output.json from spending.
        spend = (_is_valid_delivered_pr_url(pr_url) and _pr_url_seen_in_streams(pr_url, streams)) or observed
    with _RUN_PR_GUARD_LOCK:
        entry = _RUN_PR_GUARD_CLAIMS.get(key)
        if entry is None:
            if not spend:
                return "noop"
            # Spend evidence with no entry (the holder's entry was evicted by
            # the ledger bound, or the claim happened in a dead process):
            # record SPENT so a later flagged node of this run is still
            # denied rather than installing a fresh live guard.
            _RUN_PR_GUARD_CLAIMS[key] = ("spent", owner)
            while len(_RUN_PR_GUARD_CLAIMS) > _MAX_RUN_PR_GUARD_CLAIMS:
                del _RUN_PR_GUARD_CLAIMS[next(iter(_RUN_PR_GUARD_CLAIMS))]
            return "spent"
        state, entry_owner = entry
        if spend:
            _RUN_PR_GUARD_CLAIMS[key] = ("spent", entry_owner)
            return "spent"
        if state == "held" and entry_owner == owner:
            del _RUN_PR_GUARD_CLAIMS[key]
            return "released"
        return "noop"


def _is_valid_delivered_pr_url(pr_url: Any) -> bool:
    """A delivered-``pr_url`` spend signal must actually PARSE as http(s).

    Mirrors the classifier's spec (``classify._is_valid_pr_url``: scheme in
    http/https AND a non-empty netloc) without importing it, so a sloppy
    agent's ``pr_url: "N/A"`` / ``"none"`` never spends the run's claim while
    a real PR URL survives stream truncation as spend evidence.
    """
    if not isinstance(pr_url, str) or not pr_url.strip():
        return False
    try:
        parts = urlsplit(pr_url.strip())
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and bool(parts.netloc)


# FAR-1315 corroboration: the PR-URL shape node_runner extracts from the
# platform-captured raw output (``node_runner._PR_URL_PATTERN``) - MIRRORED
# here because sandbox_policy must stay dependency-free (no pipeline_engine
# import). ``tests/unit/pipeline_engine/test_sandbox_policy.py`` pins the two
# patterns together so they cannot drift.
_GH_PR_URL_RE = re.compile(r"https?://github\.com/[A-Za-z\d_.-]+/[A-Za-z\d_.-]+/pull/\d+")


def _pr_url_seen_in_streams(pr_url: str | None, streams: tuple[str | None, ...]) -> bool:
    """True when the reported ``pr_url`` also appears in the captured *streams*.

    FAR-1315 (MAJOR 2a): ``output.json`` is AGENT-AUTHORED, so a URL-valid
    ``pr_url`` alone proves nothing - a node whose ``gh pr create`` FAILED can
    still report a URL-shaped ``pr_url`` (an unrelated or pre-existing PR) and
    would otherwise mark the run spent, pre-planting every later flagged node
    so the run delivers nothing. The corroboration is the platform's OWN
    transcription of what the node printed (the same extraction node_runner
    persists into the FAR-188 raw-output marker's ``pr_url``): a genuine create
    echoes the URL through ``gh pr create`` stdout, which the dispatch captures.

    Normalisation is a trailing-slash strip only - the reported and printed
    forms are the same string gh emitted. Deliberate fabrication (printing a
    URL the agent never created) is out of scope: see the TAMPER MODEL.
    """
    if not isinstance(pr_url, str) or not pr_url.strip():
        return False
    target = pr_url.strip().rstrip("/")
    for stream in streams:
        if not stream:
            continue
        for match in _GH_PR_URL_RE.finditer(stream):
            if match.group(0).rstrip("/") == target:
                return True
    return False


def reset_run_pr_guard_claims() -> None:
    """Clear the ledger (test/diagnostic seam - never called in production)."""
    with _RUN_PR_GUARD_LOCK:
        _RUN_PR_GUARD_CLAIMS.clear()


def _gh_pr_guard_plan(run_scope: str | None, guard_owner: str | None) -> tuple[str, bool, str]:
    """Resolve (marker_path, pre_spent, claim_status) for an install."""
    marker_path = gh_pr_guard_marker_path(run_scope)
    status = acquire_run_pr_guard(run_scope, guard_owner)
    if status in ("held", "spent"):
        return marker_path, True, status
    return marker_path, False, status


def gh_pr_guard_marker_path(run_scope: str | None = None) -> str:
    """Build the run-scoped marker path the ``gh`` shim claims exactly once.

    The marker is created as a DIRECTORY (``mkdir`` is atomic on POSIX: the
    first ``gh pr create`` wins it, every later one sees it and is refused).
    ``run_scope`` is normally the run id; UUID-shaped forms are canonicalised
    first (FAR-1315 latent fix - the install side threads ``str(spec.run_id)``
    while the harvest side threads the raw run id, so a braced/uppercase run id
    must not make the two disagree), then sanitised to ``[A-Za-z0-9._-]`` and
    truncated so it can never escape ``/tmp`` or inject shell metacharacters
    into the installed shim.
    """
    if not run_scope:
        return f"{_GH_PR_GUARD_MARKER_ROOT}/modulo-gh-pr-create.marker"
    scope = _canonical_scope_uuid(run_scope) or str(run_scope)
    scope = _GH_PR_GUARD_SCOPE_RE.sub("_", scope)[:64].strip("._-")
    if not scope:
        return f"{_GH_PR_GUARD_MARKER_ROOT}/modulo-gh-pr-create.marker"
    return f"{_GH_PR_GUARD_MARKER_ROOT}/modulo-gh-pr-create.{scope}.marker"


def gh_pr_claim_receipt_path(run_scope: str | None = None) -> str:
    """Path of the shim's success RECEIPT inside the run-scoped marker dir.

    Written by the shim ONLY when ``gh pr create`` exited 0 (never by the
    pre-plant, which only ``mkdir -p``s the marker directory), so its
    existence is the platform's unforgeable-by-reading spend signal. Same
    canonicalised scope handling as :func:`gh_pr_guard_marker_path` - the probe
    must ask about the EXACT path the shim wrote, whatever string form the run
    id arrived in.
    """
    return f"{gh_pr_guard_marker_path(run_scope)}/{_GH_PR_GUARD_CLAIM_RECEIPT}"


def build_read_only_script() -> str:
    """Build the shell script that makes the workspace read-only.

    Runs as root AFTER the Modulo-owned files (prompt / input / context) are
    written and AFTER the git-credential policy files are installed. ``chmod -R
    a-w`` revokes write on every file and directory in the workspace — this
    binds the agent's non-root user (root bypasses mode bits regardless), which
    is exactly the enforcement we certify as ``write_files=False``.

    The agent's RUNTIME must still be able to write two files: the stdout/stderr
    redirect target (``/home/user/agent.log`` — node_runner redirects the agent
    command's output there) and the ``/home/user/output.json`` deliverable. Both
    are pre-created as root and re-opened writable AFTER the seal, so the agent
    can emit its log and result while every other file and directory in the
    workspace stays read-only. A deliberately-mounted read-only filesystem
    (``mount --bind ... -o remount,ro``) is NOT used: it would also block these
    two runtime writes, and the chmod against the non-root agent user is the
    complete enforcement surface from the app's side.
    """
    return (
        "set -e\n"
        f"touch {_WORKSPACE}/agent.log {_WORKSPACE}/output.json\n"
        f"chmod -R a-w {_WORKSPACE}\n"
        # Re-open the agent's own runtime writes after the seal removed all
        # write bits. Writing to an EXISTING file needs only the file's mode,
        # not the parent directory's — so a 666 log/output.json stays writable
        # inside an otherwise sealed workspace.
        f"chmod 666 {_WORKSPACE}/agent.log {_WORKSPACE}/output.json\n"
        "true\n"
    )


def _credential_helper_script() -> str:
    """A credential helper that only grants the provisioned token to the allowlisted host.

    ``git credential fill`` feeds the credential description (protocol, host,
    path, username) on stdin — it never sends the password — so the helper reads
    the provisioned token from the ``GITHUB_TOKEN`` environment variable (the
    Modulo runtime already injects it into the agent command's environment).
    The helper checks the ``host`` field equals the allowlisted host and only
    then prints the token; for any other host it outputs nothing, so git cannot
    obtain credentials for it (a scoped credential that is genuinely limited to
    github.com). No secret is embedded in the helper script itself.
    """
    return f"""#!/bin/sh
host=""
while read -r l; do
  [ "$l" = "" ] && break
  case "$l" in
    host=*) host="${{l#host=}}" ;;
  esac
done
if [ "$host" = "{_GIT_ALLOWED_HOST}" ] && [ -n "$GITHUB_TOKEN" ]; then
  printf 'username=x-access-token\\npassword=%s\\n' "$GITHUB_TOKEN"
fi
"""


def build_git_scoped_script() -> str:
    """Build the shell script that installs a host-scoped git credential helper.

    The helper reads the host from stdin and only echoes the token back when
    the host is the allowlisted github.com — git can authenticate to github.com
    but no other host.

    The helper is registered in the AGENT's git config (``_AGENT_GIT_CONFIG`` =
    ``/home/user/.gitconfig``) — the file ``git`` reads when the AGENT (the
    sandbox's default non-root user) clones/pushes. This policy step runs as
    root, so ``git config --global`` alone would write ``/root/.gitconfig`` and
    the scoped helper would never be active for the agent (fail-open). Writing
    the helper explicitly into the agent's config file guarantees the scoped
    credential is genuinely enforced for every git operation the agent performs.
    """
    return (
        "set -e\n"
        f"mkdir -p {_WORKSPACE}/.git-policy\n"
        f"cat > {_WORKSPACE}/.git-policy/cred-helper.sh <<'POLICY_EOF'\n"
        f"{_credential_helper_script()}"
        f"POLICY_EOF\n"
        f"chmod +x {_WORKSPACE}/.git-policy/cred-helper.sh\n"
        # Register the helper in the AGENT's git config file (not /root's —
        # the agent runs as the sandbox default non-root user and reads
        # /home/user/.gitconfig). The agent reads .gitconfig, so the scoped
        # helper is genuinely in force for its git operations. Note: the flag
        # is ONLY ``--file`` — ``--global --file`` together makes git exit with
        # "error: only one config file at a time" (exit 129), which would fail
        # this enforcement-critical step for every scoped/none sandbox.
        f"git config --file {_AGENT_GIT_CONFIG} credential.helper "
        f'"{_WORKSPACE}/.git-policy/cred-helper.sh"\n'
    )


def _multi_host_credential_helper_script(hosts: dict[str, str]) -> str:
    """A credential helper that grants per-host tokens via exact literal match.

    ``git credential fill`` feeds the credential description (protocol, host,
    path, username) on stdin — it never sends the password — so the helper reads
    the host from stdin and matches it against the ordered host list by EXACT
    literal equality (no globs, no file reads — no TOCTOU). Each host's token
    comes from its own per-host env var (e.g. ``MODULO_GIT_CRED_0``), never a
    single shared var, so host A's token is never visible to host B.

    *hosts* is an ordered dict mapping hostname -> env var name containing the
    token. The script uses a ``case`` statement for exact match: each host is a
    separate arm, and only the matching arm's env var is read. Unknown hosts
    produce no output (deny).
    """
    cases = []
    for host, env_var in hosts.items():
        if not isinstance(host, str) or not _HOSTNAME_RE.fullmatch(host):
            raise ValueError(f"invalid git-credential host {host!r}: must be a hostname containing only [A-Za-z0-9.-]")
        if not isinstance(env_var, str) or not _ENV_VAR_RE.fullmatch(env_var):
            raise ValueError(
                f"invalid git-credential env var {env_var!r} for host {host!r}: must be a valid POSIX identifier"
            )
        cases.append(
            f'  "{host}")\n'
            f'    if [ -n "${{{env_var}}}" ]; then\n'
            f"      printf 'username=x-access-token\\npassword=%s\\n' \"${{{env_var}}}\"\n"
            f"    fi\n"
            f"    ;;"
        )
    return f"""#!/bin/sh
host=""
while read -r l; do
  [ "$l" = "" ] && break
  case "$l" in
    host=*) host="${{l#host=}}" ;;
  esac
done
case "$host" in
{chr(10).join(cases)}
esac
"""


def build_git_multi_host_script(hosts: dict[str, str]) -> str:
    """Build the shell script that installs a multi-host git credential helper.

    FAR-798: extends the single-host scoped helper to support multiple hosts.
    Each host's token comes from its own per-host env var, and the generated
    script matches hosts by EXACT literal equality (no globs, no TOCTOU).

    The helper is registered in the AGENT's git config (``_AGENT_GIT_CONFIG``)
    — same as :func:`build_git_scoped_script` — and the same
    ``--file``-only register constraint applies.
    """
    return (
        "set -e\n"
        f"mkdir -p {_WORKSPACE}/.git-policy\n"
        f"cat > {_WORKSPACE}/.git-policy/cred-helper.sh <<'POLICY_EOF'\n"
        f"{_multi_host_credential_helper_script(hosts)}"
        f"POLICY_EOF\n"
        f"chmod +x {_WORKSPACE}/.git-policy/cred-helper.sh\n"
        f"git config --file {_AGENT_GIT_CONFIG} credential.helper "
        f'"{_WORKSPACE}/.git-policy/cred-helper.sh"\n'
    )


def build_git_none_script() -> str:
    """Build the shell script that provisions NO git credentials.

    A credential helper that always refuses prevents git from reaching any
    credential the sandbox may otherwise inherit (e.g. a baked-in template
    token). ``git_credentials="none"`` certifies no git credential reaches the
    agent. Like the scoped script, the helper is registered in the AGENT's git
    config file (``_AGENT_GIT_CONFIG``) so it binds the agent's ``git``, not a
    root config the agent never reads.
    """
    return (
        "set -e\n"
        "printf '#!/bin/sh\\nexit 1\\n' > /tmp/modulo-git-refuse-helper.sh\n"
        "chmod +x /tmp/modulo-git-refuse-helper.sh\n"
        # --file only, never --global --file together (git rejects that combo
        # with "only one config file at a time", exit 129).
        f"git config --file {_AGENT_GIT_CONFIG} credential.helper /tmp/modulo-git-refuse-helper.sh\n"
    )


def build_egress_selected_script(egress_allowlist: list[dict[str, Any]]) -> str:
    """Build the shell script that enforces the host:port egress allowlist.

    Drops ALL egress (both IPv4 and, where available, IPv6), then re-adds only
    the allowlisted host:port pairs. Hostnames are resolved at build time by
    the runner (node_runner resolves them and passes the numeric addresses in
    the script via the allowlist we embed) — the allowlist entries carry
    ``host`` and ``port``; node_runner pre-resolves ``host`` to an IP so the
    iptables rule binds the actual destination, not a DNS name iptables cannot
    match. The script is fail-closed: any resolution failure leaves the
    sandbox with no egress (deny-all fallback), never a permissive one.

    IP-ONLY RESTRICTION (MEDIUM): the OUTPUT DROP also drops UDP 53, so in-sandbox
    DNS resolution does not work — the agent cannot resolve hostnames by name,
    and an allowlisted host is only reachable BY the pre-resolved IP the runner
    embeds. This is intentional and fail-closed: node_runner resolves each
    allowlisted host to a concrete IPv4 before building the rules, so the
    product's git/API surface is reached by IP, and any host the agent would
    need to resolve by name is simply unreachable (denied) unless it is in the
    allowlist. The egress script never opens UDP 53, so it never weakens the
    allowlist.
    """
    lines = [
        "set -e\n",
        # Fail-closed baseline: drop all egress first.
        "iptables -P OUTPUT DROP 2>/dev/null || true\n",
        "iptables -F OUTPUT 2>/dev/null || true\n",
        "ip6tables -P OUTPUT DROP 2>/dev/null || true\n",
        "ip6tables -F OUTPUT 2>/dev/null || true\n",
        # Allow loopback and established connections so the agent's local tooling
        # (the Modulo bridge, git credential negotiation) keeps working.
        "iptables -A OUTPUT -o lo -j ACCEPT 2>/dev/null || true\n",
        "iptables -A OUTPUT -m state --state ESTABLISHED,RELATED -j ACCEPT 2>/dev/null || true\n",
        "ip6tables -A OUTPUT -o lo -j ACCEPT 2>/dev/null || true\n",
        "ip6tables -A OUTPUT -m state --state ESTABLISHED,RELATED -j ACCEPT 2>/dev/null || true\n",
    ]
    for entry in egress_allowlist:
        host = entry.get("host")
        port = entry.get("port")
        ip = entry.get("_resolved_ip")
        if isinstance(host, str) and isinstance(port, int) and 1 <= port <= 65535:
            target = ip if isinstance(ip, str) and ip else host
            # Defense-in-depth (#1175): even post-validation, never let an
            # unvalidated string reach the root shell; quote it as well.
            if not is_valid_egress_host(target):
                raise ValueError(f"invalid egress allowlist target {target!r}")
            lines.append(
                f"iptables -A OUTPUT -d {shlex.quote(target)} -p tcp --dport {port} -j ACCEPT 2>/dev/null || true\n"
            )
    lines.append("true\n")
    return "".join(lines)


# FAR-1264 (M4): the shim's SELF-IDENTIFYING marker — the first comment line
# of every shim we install. The install step greps for this exact string
# inside a candidate ``gh`` to tell "our shim is installed here" from "a
# preserved real gh / partial install": a ``gh.modulo-real`` file ALONE proves
# neither, because the preserved copy outlives a stale shim on a reused
# workspace. Keep it unique to this shim (it is grepped for, unescaped).
_GH_PR_GUARD_FINGERPRINT = "# modulo-gh-pr-guard-shim"

# FAR-1264: the shim body, embedded verbatim (quoted heredoc) into the install
# script. Keep it POSIX ``sh`` only (no bashisms — the sandbox agent runs
# ``sh -c``) and keep REAL_BIN / MARKER as the two header lines the install
# script writes above it. FAR-1315: f-string so the claim sentinel constant is
# interpolated verbatim (the body contains no shell braces, verified) — the
# sentinel and the classifier that reads it back can never drift apart.
_GH_PR_GUARD_SHIM_BODY = (
    _GH_PR_GUARD_FINGERPRINT
    + "\n"
    + f"""\
is_pr_create=0
saw_pr=0
expect_val=0
for arg in "$@"; do
  if [ "$expect_val" -eq 1 ]; then
    # Value of a global flag we already saw (e.g. the X in `gh --repo X pr
    # create`) — never a subcommand.
    expect_val=0
    continue
  fi
  case "$arg" in
    --repo|-R|--hostname|--config|-c)
      expect_val=1
      continue
      ;;
    --repo=*|--hostname=*|--config=*)
      continue
      ;;
    -*)
      # Any other flag, before or between positionals (gh's persistent flags
      # may appear anywhere): never a subcommand.
      continue
      ;;
  esac
  if [ "$saw_pr" -eq 1 ]; then
    if [ "$arg" = "create" ]; then
      is_pr_create=1
      break
    fi
    # First non-`create` positional after `pr` ends that attempt (e.g.
    # `gh pr list ...`); it may itself start a new `pr` pair.
    saw_pr=0
    if [ "$arg" = "pr" ]; then saw_pr=1; fi
  else
    if [ "$arg" = "pr" ]; then saw_pr=1; fi
  fi
done

if [ "$is_pr_create" -ne 1 ]; then
  # Every non-`pr create` invocation (other subcommands, `gh --version`,
  # `gh issue create`, ...) passes straight through, untouched.
  exec "$REAL_BIN" "$@"
fi

# Claim the run's single `gh pr create`. mkdir is atomic: the winner runs the
# real gh, everyone else is refused WITHOUT calling it. The claim is held ONLY
# for a SUCCESSFUL create: a non-zero exit (rate limit, empty diff, network
# flap) releases it before exiting with that code, so the run's retry can
# claim again — burning the run's only attempt on a transient failure would
# refuse a legitimate FIRST create while reporting that one had succeeded.
if mkdir "$MARKER" 2>/dev/null; then
  "$REAL_BIN" "$@"
  _rc=$?
  if [ "$_rc" -ne 0 ]; then
    rmdir "$MARKER" 2>/dev/null || true
  else
    # FAR-1315: claim RECEIPT - the primary cross-node observation channel.
    # A file INSIDE the marker directory, written ONLY when the real gh exited
    # 0. The platform harvests it from the sandbox after the node
    # (harvest_gh_pr_claim_via_exec) before the sandbox dies. It survives
    # stdout truncation, and unlike the sentinel below it is NOT exposed by
    # reading this shim (`cat $(command -v gh)`), so merely reading the guard
    # can never mark the run spent. A pre-planted marker never contains it.
    printf '%s\\n' "1" > "$MARKER/{_GH_PR_GUARD_CLAIM_RECEIPT}" 2>/dev/null || true
    # FAR-1315: claim SENTINEL on stdout - the FALLBACK observation channel
    # for when the harvest is unavailable (sandbox already destroyed). Fixed
    # literal the guarded agent can read, so the platform only trusts it when
    # the receipt harvest could not run at all.
    printf '%s\\n' "{_GH_PR_GUARD_CLAIM_SENTINEL}"
  fi
  exit "$_rc"
fi
if [ -d "$MARKER" ]; then
  printf '%s\\n' "modulo: one-PR-per-run guard (FAR-1264): refusing a second 'gh pr create' attempt in this run." >&2
  printf '%s\\n' "modulo: the run's one attempt is spent - an earlier 'gh pr create' exited 0 (claim: $MARKER)." >&2
  printf '%s\\n' "modulo: (no PR? that claim is stale or hand-planted - out of scope, see the install docstring)" >&2
  exit 1
fi
# mkdir failed for an environmental reason (marker dir does NOT exist — e.g.
# an unwritable /tmp). The install step is best-effort, so the runtime claim
# degrades the same way: warn loudly and let the create through rather than
# blocking the run's ONE legitimate PR; the prompt-level guard still applies.
printf '%s\\n' "modulo: one-PR-per-run guard (FAR-1264): WARNING could not claim $MARKER" >&2
printf '%s\\n' "modulo: guard degraded - allowing this gh pr create unguarded; prompt-level guard still applies." >&2
exec "$REAL_BIN" "$@"
"""
)


def build_gh_pr_guard_script(marker_path: str, *, pre_spent: bool = False) -> str:
    """Build the shell script that installs the one-PR-per-run ``gh`` shim.

    FAR-1264. Runs as root before the agent command executes:

      1. Resolve the REAL ``gh`` at install time: every ``gh`` found in the
         install-time ``PATH`` is copied aside to ``<dir>/gh.modulo-real``
         BEFORE its path is shadowed by the shim, so the shim can never
         recurse into itself. The shim lives AT the path ``command -v gh``
         would have resolved — PATH *order* therefore does not decide whether
         the guard is reached; the shim replaces the very binary the agent's
         ``sh -c`` resolves (the agent command runs with the sandbox's default
         env, the same image PATH the policy step sees).
      2. Re-installation is keyed off the shim's OWN fingerprint (its
         ``# modulo-gh-pr-guard-shim`` line) plus the run scope embedded in
         it — NOT off the presence of ``gh.modulo-real``. "Real gh preserved"
         is not "guard installed": the preserved copy outlives a stale shim on
         a reused workspace, so a scope-matching shim counts as already
         guarded, a stale-scope shim (or a ``gh`` that is not our shim at all)
         is rewritten in place, and only a ``gh`` that genuinely could not be
         guarded is reported as a failure.
       3. Each shim claims ``marker_path`` atomically (``mkdir``) on its first
          ``gh pr create``; later ones exit non-zero WITHOUT calling the real
          gh. The claim is held only for a SUCCESSFUL create — a non-zero exit
          releases it, so a transient failure (rate limit, empty diff, network)
          does not burn the run's only attempt. The marker path is embedded at
          install time and is run-scoped (see :func:`gh_pr_guard_marker_path`).
          On a SUCCESSFUL create the shim writes the FAR-1315 claim RECEIPT
          inside the marker (see :func:`gh_pr_claim_receipt_path` — the
          platform harvests it after the node) and prints the claim SENTINEL
          to stdout (see :data:`_GH_PR_GUARD_CLAIM_SENTINEL` — the fallback
          channel when the harvest cannot run).
       4. FAR-1315 ``pre_spent=True``: the install PRE-PLANTS the claim
          directory (``mkdir -p "$MARKER"``) before the shim lands, so this
          node's FIRST ``gh pr create`` is refused by the already-existing
          marker — the run's one attempt was already spent (or is being held)
          by an earlier flagged node. A failed plant degrades loudly: it is
          echoed to stderr (``could not pre-plant``) so the classifier reports
          the run-scope enforcement as ABSENT rather than silently live.

    TAMPER MODEL (explicit - do not overstate what this buys): the claim is an
    in-sandbox marker file under ``/tmp``. It is predictable and writable by
    the agent, so it is NOT tamper-proof against a deliberately evasive agent
    (which could ``rmdir`` the marker, delete the shim or the
    ``gh.modulo-real`` copy, or invoke a non-PATH ``gh``). What it guards
    against is the OBSERVED failure mode - the ACCIDENTAL second
    ``gh pr create`` within one run, where the agent retries or repeats the
    create while following its prompt.

    SCOPE CALL (FAR-1315 re-gate - stated, not implied):
    (1) ADVERSARIAL EVASION IS OUT OF SCOPE. The marker lives under ``/tmp``
    and the shim (mode 755, PATH-resolvable) prints its exact path, so a
    deliberately malicious agent can PRE-CREATE the marker/receipt (false
    SPEND: later flagged nodes are refused and the run delivers nothing) or
    DELETE it (false RELEASE). No heuristic in this module is designed to
    defeat a hostile agent - hardening against that is deliberately not
    attempted here. (2) DELETING THE MARKER/RECEIPT NOW ALSO NEUTRALISES THE
    SENTINEL ARM: the receipt harvest answers ABSENT against the deleted
    path, and a definitive ABSENT receipt (live shim + probe ran) suppresses
    the stdout claim sentinel as well as ``pr_url`` - so a hostile deletion
    both releases this node's hold and silences the fallback channel that
    would otherwise have spent the run. (3) The ACCIDENTAL model is what must
    stay correct, and is what the tests pin: a normal create -> receipt True
    -> spent; a truncated stream -> harvest still reads receipt True -> spent;
    merely READING the shim -> no create -> receipt False (live install) ->
    released, never spent. Deliberate-evasion hardening remains tracked
    separately (as before this ticket).

    SCOPE: the guard is armed by the explicit ``single_pr_per_run`` node flag
    (FAR-1273) — nothing else. A non-empty ``delivery_sentinel`` no longer
    installs it (FAR-228 idempotency is a separate concern), so a sentinel
    used for any other purpose never arms an unrelated PR guard.

    BEST-EFFORT: any per-directory failure (read-only dir, no write
    permission) skips that directory with a stderr note; if NOTHING could be
    guarded while a ``gh`` does exist, the script exits 1 so the step is
    logged — the caller never raises. If there is no ``gh`` on PATH at all the
    script says so loudly on stderr (an unguarded flagged run must be
    observable) and exits 0: there is nothing to guard.

    Forms NOT intercepted (documented per the ticket): ``gh api`` calls that
    create a PR through the REST API; a ``gh`` invoked by absolute path from
    a copy outside the install-time PATH; a shell function/alias or a ``gh``
    installed into a NEW PATH directory after this script ran; and ``gh pr
    <flags> create`` interleavings are only missed if a flag between ``pr``
    and ``create`` takes a value that is not in the skip list (the realistic
    invocations ``gh pr create ...`` and ``gh --repo X pr create ...`` are
    both handled).
    """
    # FAR-1315: pre-plant the claim directory when this node's install was
    # DENIED by the run ledger (spent / held by another node). ``set -e`` is
    # active, but a command used as an ``if`` condition never triggers it, so
    # an unwritable marker root degrades to a loud stderr note instead of
    # aborting the install.
    pre_plant = (
        'if ! mkdir -p "$MARKER" 2>/dev/null; then\n'
        f'  echo "modulo: gh guard: WARNING {_GH_PR_GUARD_PRE_PLANT_FAILED_NOTE} the run claim at $MARKER" >&2\n'
        "fi\n"
        if pre_spent
        else ""
    )
    return (
        "set -e\n"
        f"MARKER='{marker_path}'\n" + pre_plant + "guard_installed=0\n"
        # PATH entries are ':'-separated (E2B sandboxes are Linux); entries
        # containing whitespace are not supported by this word-split (the
        # sandbox image PATH has none).
        "for d in $(printf '%s' \"$PATH\" | tr ':' ' '); do\n"
        '  [ -n "$d" ] || continue\n'
        '  tgt="$d/gh"\n'
        '  if [ ! -f "$tgt" ]; then continue; fi\n'
        '  real="$tgt.modulo-real"\n'
        # M4: "a preserved real gh exists" must NEVER be read as "our shim is
        # installed". Detect OUR shim by its fingerprint line and verify the
        # embedded run scope; a scope-matching shim counts as guarded, anything
        # else falls through to a rewrite (the preserved copy is then already
        # there, or this path holds a real gh we are about to shadow).
        "  is_ours=0\n"
        f'  if grep -qF "{_GH_PR_GUARD_FINGERPRINT}" "$tgt" 2>/dev/null; then is_ours=1; fi\n'
        '  if [ "$is_ours" -eq 1 ]; then\n'
        '    if grep -qF "MARKER=\'$MARKER\'" "$tgt" 2>/dev/null; then\n'
        "      guard_installed=$((guard_installed + 1))\n"
        "      continue\n"
        "    fi\n"
        '    echo "modulo: gh guard: rewriting shim with a stale run scope in $d" >&2\n'
        '  elif [ -f "$real" ]; then\n'
        '    echo "modulo: gh guard: $real exists but $tgt is not a guard shim; reinstalling in $d" >&2\n'
        "  fi\n"
        '  tmp="$d/.gh.modulo-shim.$$"\n'
        # Write the shim to a temp file FIRST (a failure here leaves the real
        # gh untouched), then preserve the real gh, then shadow the path.
        "  if ! { printf '%s\\n' '#!/bin/sh'\n"
        '    printf "REAL_BIN=\'%s\'\\n" "$real"\n'
        '    printf "MARKER=\'%s\'\\n" "$MARKER"\n'
        "    cat <<'MODULO_GH_GUARD_EOF'\n"
        f"{_GH_PR_GUARD_SHIM_BODY}"
        "MODULO_GH_GUARD_EOF\n"
        f'  }} > "$tmp" 2>/dev/null; then rm -f "$tmp" 2>/dev/null || true; '
        'echo "modulo: gh guard: cannot write shim in $d" >&2; continue; fi\n'
        '  if ! chmod 755 "$tmp" 2>/dev/null; then rm -f "$tmp" 2>/dev/null || true; '
        'echo "modulo: gh guard: cannot chmod shim in $d" >&2; continue; fi\n'
        # Preserve the real gh ONLY when this path does not already hold our
        # shim: copying a shim over gh.modulo-real would create exactly the
        # self-recursion the preserve step exists to prevent.
        '  if [ "$is_ours" -eq 0 ] && [ ! -f "$real" ]; then\n'
        '    if ! cp -p "$tgt" "$real" 2>/dev/null; then rm -f "$tmp" 2>/dev/null || true; '
        'echo "modulo: gh guard: cannot preserve real gh in $d" >&2; continue; fi\n'
        "  fi\n"
        '  if ! mv "$tmp" "$tgt" 2>/dev/null; then rm -f "$tmp" 2>/dev/null || true; '
        'echo "modulo: gh guard: cannot shadow gh in $d" >&2; continue; fi\n'
        "  guard_installed=$((guard_installed + 1))\n"
        "done\n"
        'if [ "$guard_installed" -eq 0 ]; then\n'
        "  found_gh=0\n"
        "  for d in $(printf '%s' \"$PATH\" | tr ':' ' '); do\n"
        '    if [ -f "$d/gh" ]; then found_gh=1; break; fi\n'
        "  done\n"
        '  if [ "$found_gh" -eq 1 ]; then\n'
        '    echo "modulo: gh guard: FAILED - a gh exists on PATH but none could be guarded" >&2\n'
        "    exit 1\n"
        "  fi\n"
        # XS: an unguarded flagged run must be OBSERVABLE — this exits 0 (the
        # install step is best-effort and must not wedge the dispatch), so the
        # note is the only signal that the platform guard is absent; the
        # caller also mirrors it into the policy log.
        '  echo "modulo: gh guard: WARNING no gh on PATH; nothing to guard" >&2\n'
        '  echo "modulo: gh guard: flagged run is NOT platform-guarded (prompt-level only)" >&2\n'
        "fi\n"
        "exit 0\n"
    )


@dataclass(frozen=True)
class GhPrGuardInstallResult:
    """FAR-1315 outcome of an exec-based one-PR guard install (non-E2B tiers).

    ``status`` vocabulary:

    - ``"installed"`` — the live guard landed (this node owns the run's slot);
    - ``"pre_planted"`` — the shim landed with the run's claim already spent /
      held by another node, so its first ``gh pr create`` is refused;
    - ``"absent"`` — nothing to guard: no ``gh`` on PATH (the run is NOT
      platform-guarded — the caller must surface this loudly);
    - ``"failed"`` — the install could not be verified (non-zero exit, exec
      error, or a failed pre-plant — i.e. no run-scope enforcement).

    SHIPPED-IMAGE TRUTH (FAR-1315): on the first-party runner image
    (``deploy/docker/runner-opencode.Dockerfile`` — no ``gh`` installed,
    ``ReadonlyRootfs``, uid 1001, no writable PATH dir) this resolves to
    ``absent``/``failed`` by construction: the tier is NOT guarded there
    today. ``"installed"``/``"pre_planted"`` require an image that ships a
    ``gh`` in a PATH directory writable by the workspace user.
    """

    status: str
    detail: str
    marker_path: str


def _classify_gh_pr_guard_outcome(exit_code: Any, stderr: str, *, pre_spent: bool) -> tuple[str, str]:
    """Map an install script result to (status, detail) — shared by both install paths."""
    detail = (stderr or "").strip()
    if exit_code != 0:
        return "failed", detail or f"guard install exited with {exit_code}"
    if _GH_PR_GUARD_NO_GH_NOTE in detail:
        return "absent", detail
    if pre_spent and _GH_PR_GUARD_PRE_PLANT_FAILED_NOTE in detail:
        # The shim may have landed, but the run-scoped refusal marker did not —
        # without it the FIRST create in this (already-spent) run would be
        # allowed: the run-scope enforcement is gone, so report absence loudly.
        return "failed", detail
    return ("pre_planted" if pre_spent else "installed"), detail


async def install_gh_pr_guard_via_exec(
    exec_command: Callable[[list[str]], Awaitable[Any]],
    *,
    run_scope: str | None,
    guard_owner: str | None,
) -> GhPrGuardInstallResult:
    """Install the one-PR-per-run ``gh`` shim through a provider exec (FAR-1315).

    The non-E2B twin of the guard step inside :func:`apply_sandbox_policy`: it
    runs the SAME :func:`build_gh_pr_guard_script` (with the same run-ledger
    plan — first flagged node of the run gets the live guard, later ones get a
    pre-planted refusal) through an arbitrary provider's ``exec_command``
    primitive, so a tier whose dispatch never reaches ``apply_isolation`` (the
    Bundled Runner / ``runner_docker``) has the guard ATTEMPTED at dispatch
    instead of silently skipped. ``exec_command`` is a single-argument
    coroutine the CALLER binds to its provider ref and timeout (the ABC
    primitive's shape), keeping this module free of any runtime-provider
    import.

    BEST-EFFORT by contract (parity with the E2B guard step): a failure never
    raises — it is returned as status ``"failed"``/``"absent"`` for the caller
    to surface loudly. The ledger claim taken here is settled by the caller
    via :func:`settle_run_pr_guard` when the node finishes (spend evidence:
    harvested receipt → a corroborated delivered ``pr_url`` → sentinel
    fallback; the returned ``status`` is threaded in as
    ``guard_install_status`` so a non-live install leaves the receipt
    meaningless). Attempted
    is not the same as effective — see :class:`GhPrGuardInstallResult` for the
    shipped runner image's boundary (no ``gh``, read-only rootfs: the install
    reports ``absent`` there and the run is prompt-level-guarded only).
    """
    marker_path, pre_spent, _claim_status = _gh_pr_guard_plan(run_scope, guard_owner)
    script = build_gh_pr_guard_script(marker_path, pre_spent=pre_spent)
    try:
        result = await exec_command(["sh", "-c", script])
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        return GhPrGuardInstallResult("failed", f"{type(exc).__name__}: {exc}"[:500], marker_path)
    status, detail = _classify_gh_pr_guard_outcome(
        getattr(result, "exit_code", None),
        str(getattr(result, "stderr", "") or ""),
        pre_spent=pre_spent,
    )
    return GhPrGuardInstallResult(status, detail[:1000], marker_path)


async def harvest_gh_pr_claim_via_exec(
    exec_command: Callable[[list[str]], Awaitable[Any]],
    *,
    run_scope: str | None,
) -> bool | None:
    """Harvest the shim's success RECEIPT from the sandbox after the node (FAR-1315).

    The unforgeable spend channel behind :func:`settle_run_pr_guard`'s first
    arm: run ONE bounded shell probe through the provider's ``exec_command``
    primitive (the sandbox/container is still alive - the dispatch harvests
    BEFORE its teardown) and ask whether the receipt file the shim writes only
    on a SUCCESSFUL ``gh pr create`` exists.

    Returns ``True`` (receipt present - this node created the PR), ``False``
    (probe ran, receipt genuinely absent - no spend), or ``None`` (the harvest
    could NOT run: exec raised, non-zero exit, or an unparseable reply - the
    caller must fall back to the weaker stream evidence rather than guess).

    BEST-EFFORT by contract: never raises except ``CancelledError``. The
    receipt path is built from the sanitised run scope (``[A-Za-z0-9._-]``
    only) and double-quoted into the probe, so no caller-controlled text can
    reach the shell unquoted.
    """
    receipt_path = gh_pr_claim_receipt_path(run_scope)
    script = (
        f'if [ -f "{receipt_path}" ]; then printf "%s" "{_GH_PR_CLAIM_RECEIPT_PRESENT}"; '
        f'else printf "%s" "{_GH_PR_CLAIM_RECEIPT_ABSENT}"; fi'
    )
    try:
        result = await exec_command(["sh", "-c", script])
    except asyncio.CancelledError:
        raise
    except Exception:
        return None
    if getattr(result, "exit_code", None) != 0:
        return None
    reply = str(getattr(result, "stdout", "") or "")
    if _GH_PR_CLAIM_RECEIPT_PRESENT in reply:
        return True
    if _GH_PR_CLAIM_RECEIPT_ABSENT in reply:
        return False
    return None


async def _cancel_and_drain_harvest(task: asyncio.Future[bool | None]) -> None:
    """Cancel the probe task (if still live) and wait for it to finish.

    Always awaited-or-cancelled: the probe must never be left running against a
    container the teardown that follows is about to destroy (the re-gate
    finding on the old ``asyncio.shield`` form, which returned on timeout while
    the shielded inner task kept running, and propagated on cancellation while
    that task lived on). The wait is itself bounded so a probe that ignores its
    cancellation cannot wedge the dispatch ``finally``; if it is still alive
    after the bound, a done-callback consumes its outcome so a late failure is
    never reported as "never retrieved". Cancellation and probe-side failures
    are swallowed here - the receipt is simply unknown - so a drain failure can
    never mask the caller's own cancellation.
    """
    if not task.done():
        task.cancel()
    try:
        await asyncio.wait({task}, timeout=_HARVEST_DRAIN_TIMEOUT)
    except asyncio.CancelledError:  # NOSONAR S7497
        # A second cancellation landed while draining: the probe is already
        # cancelled, so nothing is left to wait for. The caller re-raises its
        # own recorded cancellation after teardown.
        return
    if not task.done():
        task.add_done_callback(_consume_harvest_probe_outcome)


def _consume_harvest_probe_outcome(task: asyncio.Future[bool | None]) -> None:
    """Done-callback: retrieve a leftover probe's exception (never raises)."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        _log.debug("sandbox_policy.gh_harvest_probe_leftover_failed", exc_info=exc)


async def harvest_gh_pr_claim_bounded(
    exec_command: Callable[[list[str]], Awaitable[Any]],
    *,
    run_scope: str | None,
    timeout: float = 20.0,  # noqa: ASYNC109 - the dispatch's own harvest bound, not a client API timeout
) -> bool | None:
    """Bounded, cancellation-aware receipt harvest for a dispatch ``finally`` (FAR-1315).

    Wraps :func:`harvest_gh_pr_claim_via_exec` so the dispatch's teardown
    ordering stays safe:

    - **Bounded.** The probe runs as its own task under ``asyncio.wait``. On
      timeout the probe is CANCELLED and awaited, never left running against a
      container that is about to be destroyed (the old
      ``wait_for(shield(...))`` form did the opposite of its comment: on
      timeout it returned while the shielded inner task kept running, and on
      cancellation it propagated while that task lived on).
    - **Cancellation propagates AFTER the probe is cleaned up.**
      ``asyncio.CancelledError`` is re-raised here only once the probe task is
      cancelled and drained, so the CALLER can catch it, treat the receipt as
      UNKNOWN, run its teardown, and re-raise at the end of its ``finally``.
    - Any other failure (exec error, non-zero exit, unparseable reply) returns
      ``None`` - receipt unknown - never raising into the teardown.
    """
    task: asyncio.Future[bool | None] = asyncio.create_task(
        harvest_gh_pr_claim_via_exec(exec_command, run_scope=run_scope)
    )
    try:
        await asyncio.wait({task}, timeout=timeout)
    except asyncio.CancelledError:
        await _cancel_and_drain_harvest(task)
        raise
    if not task.done():
        # Bound expired: cancel so the probe can never outlive the teardown.
        await _cancel_and_drain_harvest(task)
        return None
    if task.cancelled():
        return None
    if task.exception() is not None:
        return None
    return task.result()


async def apply_sandbox_policy(
    sandbox: Any,
    *,
    read_only: bool,
    git_credentials: str | None,
    egress_policy: str | None,
    egress_allowlist: list[dict[str, Any]] | None,
    allowed_hosts: dict[str, str] | None = None,
    command_timeout: float = 60.0,
    single_pr_per_run: bool = False,
    run_scope: str | None = None,
    guard_owner: str | None = None,
) -> str | None:
    """Run the enforced sandbox policy in the sandbox (FAR-212 PR B).

    FAR-1315: returns the one-PR guard's INSTALL STATUS
    (``installed``/``pre_planted``/``absent``/``failed``, classified from the
    install script's own exit/stderr) when ``single_pr_per_run`` armed the
    step, else ``None``. The dispatch ``finally`` threads it into
    :func:`settle_run_pr_guard`: only a LIVE install (``installed``/
    ``pre_planted``) makes a definitive ``receipt=False`` mean "confirmed no
    create" - an ``absent``/``failed`` install leaves the receipt meaningless
    (the probe ran against a path no shim ever wrote).

    Executes the git-credential scope, the selected-mode egress allowlist, the
    FAR-1264 one-PR-per-run ``gh`` guard, and the read-only chmod scripts as
    root inside the sandbox, each wrapped in a bounded ``asyncio.wait_for``
    (fresh coroutines per call, safe to cancel).

    STEP ORDER MATTERS: the git-credential scripts WRITE files into the
    workspace (``/home/user/.git-policy/cred-helper.sh`` + the agent's
    ``/home/user/.gitconfig``) and the read-only script SEALS the workspace
    read-only — so every step that writes runs FIRST and the read-only seal
    runs LAST, otherwise the seal would block the git helper install. The
    egress step uses iptables (no filesystem writes) and runs between them.
    The FAR-1264 ``gh``-guard install also writes (system ``PATH`` dirs +
    ``/tmp``) and therefore runs BEFORE the seal; it does not touch
    ``/home/user`` itself.

    USER CONTEXT (critical for the git steps): the git-credential scripts must
    register the helper in the AGENT's git config (``/home/user/.gitconfig``),
    because the agent runs as the sandbox's DEFAULT NON-ROOT user (node_runner
    starts it without a user override) and reads ``/home/user/.gitconfig`` —
    never ``/root/.gitconfig``. The helper scripts do this explicitly via
    ``_AGENT_GIT_CONFIG`` (see :func:`build_git_scoped_script` /
    :func:`build_git_none_script`), so a scoped/refuse helper is genuinely in
    force for every git operation the agent performs. Without this, the
    certified ``sandbox.git_credentials`` scope would be a deny-guarantee
    nothing enforces (fail-open).

    FAILURE SEMANTICS: the git-credential steps and the read-only seal are
    ENFORCEMENT-CRITICAL — their success is what makes the certified
    ``sandbox.git_credentials`` scope and ``sandbox.write_files=False``
    guarantee TRUE. If any of them fails, ``apply_sandbox_policy`` RAISES, so
    the run dispatches as a FAILURE rather than silently certifying a
    deny-guarantee nothing enforced (a failed ``chmod -R a-w`` leaves the
    workspace writable while ``write_files=False`` stays certified). The egress
    step is BEST-EFFORT (failures are logged-and-continued): its script is
    drop-first fail-closed, so a failure / missing-iptables no-op leaves the
    sandbox with NO egress (deny-all) — the safe direction, never a permissive
    one. The FAR-1264 ``gh``-guard install is BEST-EFFORT too — a failed
    install is logged-and-continued and the run degrades to the prompt-level
    one-PR-per-run guard (it must never wedge a dispatch; it does NOT copy the
    enforcement-critical raise semantics of the git/seal steps).

    ``sandbox`` is the e2b ``AsyncSandbox``. ``egress_allowlist`` entries may
    carry an extra ``_resolved_ip`` key (resolved by node_runner before calling)
    used to bind the iptables rule to a concrete address.

    FAR-1273: when ``single_pr_per_run`` is true, the one-PR-per-run
    ``gh`` shim is installed (:func:`build_gh_pr_guard_script`) with the
    run-scoped marker from ``run_scope``. Both arguments are optional and
    default to ``False``/unset, so every existing caller is unaffected. The
    flag is only a gate — the shim's refusal message is fixed — so no
    caller-controlled text is interpolated into the shell scripts. The flag
    is the ONLY trigger: a non-empty ``delivery_sentinel`` never arms the
    guard (it keeps its FAR-228 idempotency meaning alone).

    FAR-1315 (run scope across MULTIPLE flagged nodes): the marker filesystem
    lives inside THIS sandbox, so run-scoping the marker name alone still
    allows one PR per node. ``guard_owner`` (the node id) plus ``run_scope``
    are therefore first claimed against the process-local run ledger
    (:func:`acquire_run_pr_guard`): the first flagged node of a run installs
    the LIVE guard; a later one (or a concurrent one) finds the slot
    held/spent and installs a PRE-PLANTED refusal instead — a warning names
    the claim status so the denial is observable. The claim is settled by the
    dispatch layer from PLATFORM-OBSERVED spend evidence
    (:func:`settle_run_pr_guard`: the harvested shim claim receipt, a
    corroborated delivered ``pr_url``, and the captured output only as a
    fallback when the receipt is unknown — with THIS function's returned
    install status threaded in so a probe against a path no shim ever wrote
    is never read as "confirmed no create"); an unscoped call (no
    ``run_scope``) keeps the pre-FAR-1315 behaviour exactly (live guard, no
    ledger entry).
    """

    async def _run_step(script: str, *, user: str, enforce: bool) -> Any:
        """Run one policy step; return its CommandResult (or
        :data:`_POLICY_STEP_FAILED` on a swallowed best-effort failure).

        The e2b SDK's ``commands.run`` RAISES on a non-zero exit
        (``CommandExitException``), so a returned result is a successful step —
        one whose stderr can still carry a diagnostic the caller must not
        discard (see the gh-guard reporting below). A swallowed best-effort
        failure returns the ``_POLICY_STEP_FAILED`` sentinel rather than
        ``None`` so the gh-guard caller can tell "the install FAILED" from "the
        install ran and the SDK handed back no result object" — conflating the
        two would report a live install as failed (or vice versa) to the FAR-1315
        settle.
        """
        try:
            return await asyncio.wait_for(
                asyncio.shield(sandbox.commands.run(script, user=user, timeout=command_timeout)),
                timeout=command_timeout,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            if enforce:
                raise
            # Best-effort (egress + the gh guard): a failure is logged-and-
            # continued, never raised into the dispatch. Egress is drop-first
            # fail-closed (a failure leaves NO egress — never fail-open); the
            # guard install degrades to the prompt-level one-PR-per-run guard.
            _log.warning("sandbox_policy.step_failed", exc_info=True)
            return _POLICY_STEP_FAILED

    # Enforcement-critical steps run as root (the read-only seal must override
    # every file's mode bits regardless of ownership; the git helper install
    # writes into /home/user before the seal). The git helper is still
    # registered into the AGENT's config file (see _AGENT_GIT_CONFIG), so the
    # executing (root) user is irrelevant to where the agent reads its config.
    _guard_install_status: str | None = None
    if git_credentials == "scoped":
        # FAR-798: when allowed_hosts is provided, use the multi-host helper;
        # when None (default), use the single-host scoped helper — BYTE-
        # IDENTICAL to today for the github.com-only case.
        if allowed_hosts:
            await _run_step(build_git_multi_host_script(allowed_hosts), user="root", enforce=True)
        else:
            await _run_step(build_git_scoped_script(), user="root", enforce=True)
    elif git_credentials == "none":
        await _run_step(build_git_none_script(), user="root", enforce=True)
    if egress_policy == "selected" and egress_allowlist:
        await _run_step(build_egress_selected_script(egress_allowlist), user="root", enforce=False)
    if single_pr_per_run:
        # FAR-1273 (guard introduced by FAR-1264): install the run-scoped
        # one-PR-per-run gh shim. BEST-EFFORT (enforce=False): a failed install
        # is logged-and-continued and the run degrades to the prompt-level
        # guard — never raises into the dispatch. Runs BEFORE the read-only
        # seal (it writes: system PATH dirs + /tmp).
        #
        # FAR-1315: claim the run's one-PR slot FIRST. A denied claim
        # ("spent" — a create already succeeded in this run; "held" — a
        # CONCURRENT flagged node owns the slot) flips the install to a
        # pre-planted refusal and is logged loudly, so a second flagged node
        # can never install a fresh claimable marker in the same run.
        _marker_path, _pre_spent, _claim_status = _gh_pr_guard_plan(run_scope, guard_owner)
        if _pre_spent:
            _log.warning(
                "sandbox_policy.gh_guard_run_claim_denied: claim=%s scope=%s owner=%s - "
                "installing a pre-planted refusal for this node (FAR-1315)",
                _claim_status,
                _run_pr_guard_key(run_scope),
                guard_owner,
            )
        _guard_result = await _run_step(
            build_gh_pr_guard_script(_marker_path, pre_spent=_pre_spent),
            user="root",
            enforce=False,
        )
        # XS: the install's own diagnostics exit 0 with only a stderr note —
        # most importantly "no gh on PATH ... NOT platform-guarded", i.e. a
        # flagged run whose platform guard is ABSENT. Without this mirror the
        # note is discarded with the result and the degraded run is invisible.
        _guard_report = str(getattr(_guard_result, "stderr", "") or "").strip()
        if _guard_report:
            _log.warning("sandbox_policy.gh_guard_install_reported: %s", _guard_report[:1000])
        # FAR-1315: classify the install for the dispatch settle. The step
        # RAISED and was swallowed (enforce=False) -> "failed", never a live
        # status: an unverified install must leave the receipt meaningless
        # rather than "confirmed no create". A RETURNED value is a successful
        # step by ``_run_step``'s contract (the e2b SDK raises on non-zero
        # exit), so a result without an ``exit_code`` attribute defaults to 0
        # rather than faking a failure.
        if _guard_result is _POLICY_STEP_FAILED:
            _guard_install_status = "failed"
        else:
            _guard_install_status, _ = _classify_gh_pr_guard_outcome(
                getattr(_guard_result, "exit_code", 0),
                _guard_report,
                pre_spent=_pre_spent,
            )
    if read_only:
        await _run_step(build_read_only_script(), user="root", enforce=True)
    return _guard_install_status


__all__ = [
    "GhPrGuardInstallResult",
    "acquire_run_pr_guard",
    "apply_sandbox_policy",
    "build_egress_selected_script",
    "build_gh_pr_guard_script",
    "build_git_multi_host_script",
    "build_git_none_script",
    "build_git_scoped_script",
    "build_read_only_script",
    "gh_pr_claim_receipt_path",
    "gh_pr_guard_marker_path",
    "harvest_gh_pr_claim_bounded",
    "harvest_gh_pr_claim_via_exec",
    "install_gh_pr_guard_via_exec",
    "reset_run_pr_guard_claims",
    "settle_run_pr_guard",
]
