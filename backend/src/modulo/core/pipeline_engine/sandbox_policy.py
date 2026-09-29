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
  - ``delivery_sentinel`` (FAR-1264): install a run-scoped ``gh`` shim that
    permits exactly ONE ``gh pr create`` per sandbox run — the platform-side
    hard guard behind the prompt-level "exactly one PR per run" rule
    (FAR-1254). A second ``gh pr create`` in the same run exits non-zero
    WITHOUT invoking the real ``gh``; every other ``gh`` invocation passes
    through untouched. The claim is held only for a create that SUCCEEDED —
    a non-zero exit releases it, so a transient failure does not burn the
    run's only attempt — and the install's own diagnostics (including the
    "no gh on PATH" case, where the guard is ABSENT) are mirrored into the
    policy log rather than discarded with the step result.

The enforcement is REAL (the sandbox cannot write / egress is scoped), never a
declared flag. Script builders are pure string functions (unit-testable without
a sandbox); :func:`apply_sandbox_policy` runs them in the sandbox with bounded
timeouts. The git-credential steps and the read-only seal are
ENFORCEMENT-CRITICAL and RAISE on failure (a failed chmod / helper install must
dispatch a failure, never silently certify a deny-guarantee nothing enforces);
the egress step and the ``gh``-guard install are best-effort (their failures are
logged-and-continued: egress is drop-first fail-closed, and a missing ``gh``
guard simply degrades to the prompt-level guard). node_runner invokes
:func:`apply_sandbox_policy` when ANY of the policy fields — including a
non-empty ``delivery_sentinel`` — is set.

This module is dependency-free (no LangGraph, no DB) so it can be imported by
node_runner and the unit tests without dragging in the pipeline engine.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from modulo.core.pipeline_engine.sandbox_mode import _SANDBOX_GIT_CREDENTIAL_ALLOWED_HOST as _GIT_ALLOWED_HOST

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

# FAR-1264: the WorkspaceSpec ``workspace_metadata`` key that carries a node's
# ``delivery_sentinel`` from node_runner's policy call site to the E2B
# provider's ``apply_isolation`` policy call site. ``IsolationPolicy`` is a
# frozen dataclass that does not model the sentinel, and the spec is built per
# policy invocation (never persisted to the workspace in this flow), so the
# metadata dict is the carrier both allowlisted call sites share. Defined HERE
# so node_runner and e2b.py import one constant instead of duplicating a
# stringly-typed key (a typo in either direction would silently disarm the
# guard).
DELIVERY_SENTINEL_SPEC_KEY = "modulo.delivery_sentinel"

# FAR-1264: the marker ROOT. ``/tmp`` is deliberately OUTSIDE the read-only
# workspace seal (``build_read_only_script`` only chmods ``/home/user``), so
# the agent user can always claim the marker at ``gh pr create`` time even on
# a read-only node. The E2B sandbox is created per run (and destroyed after
# it), so a marker under /tmp is run-scoped by construction; ``run_scope``
# (the run id) additionally keys the marker so a workspace that somehow
# outlived its run can never leak a claimed marker into the next run.
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
# shell script — reduce them to a safe alphabet instead of raising (the guard
# step is best-effort; it must never fail the dispatch).
_GH_PR_GUARD_SCOPE_RE = re.compile(r"[^A-Za-z0-9._-]")


def gh_pr_guard_marker_path(run_scope: str | None = None) -> str:
    """Build the run-scoped marker path the ``gh`` shim claims exactly once.

    The marker is created as a DIRECTORY (``mkdir`` is atomic on POSIX: the
    first ``gh pr create`` wins it, every later one sees it and is refused).
    ``run_scope`` is normally the run id; sanitised to ``[A-Za-z0-9._-]`` and
    truncated so it can never escape ``/tmp`` or inject shell metacharacters
    into the installed shim.
    """
    if not run_scope:
        return f"{_GH_PR_GUARD_MARKER_ROOT}/modulo-gh-pr-create.marker"
    scope = _GH_PR_GUARD_SCOPE_RE.sub("_", str(run_scope))[:64].strip("._-")
    if not scope:
        return f"{_GH_PR_GUARD_MARKER_ROOT}/modulo-gh-pr-create.marker"
    return f"{_GH_PR_GUARD_MARKER_ROOT}/modulo-gh-pr-create.{scope}.marker"


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
            lines.append(f"iptables -A OUTPUT -d {target} -p tcp --dport {port} -j ACCEPT 2>/dev/null || true\n")
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
# script writes above it.
_GH_PR_GUARD_SHIM_BODY = (
    _GH_PR_GUARD_FINGERPRINT
    + "\n"
    + """\
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


def build_gh_pr_guard_script(marker_path: str) -> str:
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

    TAMPER MODEL (explicit — do not overstate what this buys): the claim is an
    in-sandbox marker file under ``/tmp``. It is predictable and writable by
    the agent, so it is NOT tamper-proof against a deliberately evasive agent
    (which could ``rmdir`` the marker, delete the shim or the
    ``gh.modulo-real`` copy, or invoke a non-PATH ``gh``). What it guards
    against is the OBSERVED failure mode — the ACCIDENTAL second
    ``gh pr create`` within one run, where the agent retries or repeats the
    create while following its prompt. Deliberate-evasion hardening is out of
    scope for this ticket and is tracked separately.

    SCOPE (known follow-up, not a defect): the guard is keyed off ANY
    non-empty ``delivery_sentinel`` — the install is not PR-specific today. A
    sentinel used for a different purpose would still install the
    one-PR-per-run guard; decoupling the two is a follow-up, not a bug here.

    BEST-EFFORT: any per-directory failure (read-only dir, no write
    permission) skips that directory with a stderr note; if NOTHING could be
    guarded while a ``gh`` does exist, the script exits 1 so the step is
    logged — the caller never raises. If there is no ``gh`` on PATH at all the
    script says so loudly on stderr (an unguarded sentinel run must be
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
    return (
        "set -e\n"
        f"MARKER='{marker_path}'\n"
        "guard_installed=0\n"
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
        # XS: an unguarded sentinel run must be OBSERVABLE — this exits 0 (the
        # install step is best-effort and must not wedge the dispatch), so the
        # note is the only signal that the platform guard is absent; the
        # caller also mirrors it into the policy log.
        '  echo "modulo: gh guard: WARNING no gh on PATH; nothing to guard" >&2\n'
        '  echo "modulo: gh guard: sentinel run is NOT platform-guarded (prompt-level only)" >&2\n'
        "fi\n"
        "exit 0\n"
    )


async def apply_sandbox_policy(
    sandbox: Any,
    *,
    read_only: bool,
    git_credentials: str | None,
    egress_policy: str | None,
    egress_allowlist: list[dict[str, Any]] | None,
    allowed_hosts: dict[str, str] | None = None,
    command_timeout: float = 60.0,
    delivery_sentinel: str | None = None,
    run_scope: str | None = None,
) -> None:
    """Run the enforced sandbox policy in the sandbox (FAR-212 PR B).

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

    FAR-1264: when ``delivery_sentinel`` is non-empty, the one-PR-per-run
    ``gh`` shim is installed (:func:`build_gh_pr_guard_script`) with the
    run-scoped marker from ``run_scope``. Both new arguments are optional and
    default to ``None``/unset, so every existing caller is unaffected. The
    sentinel VALUE is only a gate — the shim's refusal message is fixed — so
    no caller-controlled text is interpolated into the shell scripts.
    """

    async def _run_step(script: str, *, user: str, enforce: bool) -> Any:
        """Run one policy step; return its CommandResult (``None`` on a
        swallowed best-effort failure).

        The e2b SDK's ``commands.run`` RAISES on a non-zero exit
        (``CommandExitException``), so a returned result is a successful step —
        one whose stderr can still carry a diagnostic the caller must not
        discard (see the gh-guard reporting below).
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
            return None

    # Enforcement-critical steps run as root (the read-only seal must override
    # every file's mode bits regardless of ownership; the git helper install
    # writes into /home/user before the seal). The git helper is still
    # registered into the AGENT's config file (see _AGENT_GIT_CONFIG), so the
    # executing (root) user is irrelevant to where the agent reads its config.
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
    if delivery_sentinel:
        # FAR-1264: install the run-scoped one-PR-per-run gh shim. BEST-EFFORT
        # (enforce=False): a failed install is logged-and-continued and the run
        # degrades to the prompt-level guard — never raises into the dispatch.
        # Runs BEFORE the read-only seal (it writes: system PATH dirs + /tmp).
        _guard_result = await _run_step(
            build_gh_pr_guard_script(gh_pr_guard_marker_path(run_scope)),
            user="root",
            enforce=False,
        )
        # XS: the install's own diagnostics exit 0 with only a stderr note —
        # most importantly "no gh on PATH ... NOT platform-guarded", i.e. a
        # sentinel run whose platform guard is ABSENT. Without this mirror the
        # note is discarded with the result and the degraded run is invisible.
        _guard_report = str(getattr(_guard_result, "stderr", "") or "").strip()
        if _guard_report:
            _log.warning("sandbox_policy.gh_guard_install_reported: %s", _guard_report[:1000])
    if read_only:
        await _run_step(build_read_only_script(), user="root", enforce=True)


__all__ = [
    "DELIVERY_SENTINEL_SPEC_KEY",
    "apply_sandbox_policy",
    "build_egress_selected_script",
    "build_gh_pr_guard_script",
    "build_git_multi_host_script",
    "build_git_none_script",
    "build_git_scoped_script",
    "build_read_only_script",
    "gh_pr_guard_marker_path",
]
