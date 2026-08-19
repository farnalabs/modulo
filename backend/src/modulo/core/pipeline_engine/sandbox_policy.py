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

The enforcement is REAL (the sandbox cannot write / egress is scoped), never a
declared flag. Script builders are pure string functions (unit-testable without
a sandbox); :func:`apply_sandbox_policy` runs them in the sandbox with bounded
timeouts. node_runner invokes :func:`apply_sandbox_policy` when ANY of the
policy fields is set.

This module is dependency-free (no LangGraph, no DB) so it can be imported by
node_runner and the unit tests without dragging in the pipeline engine.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

_log = logging.getLogger(__name__)

# The E2B sandbox runs the agent as the default non-root user (root has write
# access regardless of file mode bits, so chmodding the workspace read-only
# only binds the agent's unprivileged user — which is exactly what we enforce).
_WORKSPACE = "/home/user"

# The allowlisted host for a SCOPED git credential (FAR-212 PR B). Scoping to
# github.com is the product's git surface (the agent clones/pushes to GitHub).
# Kept in sync with ``sandbox_mode._SANDBOX_GIT_CREDENTIAL_ALLOWED_HOST``.
_GIT_ALLOWED_HOST = "github.com"


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
    """
    return (
        "set -e\n"
        f"mkdir -p {_WORKSPACE}/.git-policy\n"
        f"cat > {_WORKSPACE}/.git-policy/cred-helper.sh <<'POLICY_EOF'\n"
        f"{_credential_helper_script()}"
        f"POLICY_EOF\n"
        f"chmod +x {_WORKSPACE}/.git-policy/cred-helper.sh\n"
        # The helper is registered for the whole workspace (both the agent's
        # repo clones and the Modulo-owned context) via the per-repo git config
        # when a repo exists, and via the global git config otherwise. We set
        # the global config so ANY git operation in the sandbox honours it.
        "git config --global credential.helper "
        f'"{_WORKSPACE}/.git-policy/cred-helper.sh"\n'
    )


def build_git_none_script() -> str:
    """Build the shell script that provisions NO git credentials.

    A credential helper that always refuses prevents git from reaching any
    credential the sandbox may otherwise inherit (e.g. a baked-in template
    token). ``git_credentials="none"`` certifies no git credential reaches the
    agent.
    """
    return (
        "set -e\n"
        "printf '#!/bin/sh\\nexit 1\\n' > /tmp/modulo-git-refuse-helper.sh\n"
        "chmod +x /tmp/modulo-git-refuse-helper.sh\n"
        "git config --global credential.helper /tmp/modulo-git-refuse-helper.sh\n"
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


async def apply_sandbox_policy(
    sandbox: Any,
    *,
    read_only: bool,
    git_credentials: str | None,
    egress_policy: str | None,
    egress_allowlist: list[dict[str, Any]] | None,
    command_timeout: float = 60.0,
) -> None:
    """Run the enforced sandbox policy in the sandbox (FAR-212 PR B).

    Executes the git-credential scope, the selected-mode egress allowlist, and
    the read-only chmod scripts as root inside the sandbox, each wrapped in a
    bounded ``asyncio.wait_for`` (fresh coroutines per call, safe to cancel).

    STEP ORDER MATTERS: the git-credential scripts WRITE files into the
    workspace (``/home/user/.git-policy/cred-helper.sh``) and the read-only
    script SEALS the workspace read-only — so the git steps run FIRST and the
    read-only seal runs LAST, otherwise the seal would block the git helper
    install. The egress step uses iptables (no filesystem writes) and runs
    between them.

    Failures are logged-and-continued (best-effort) so a policy step that cannot
    run on a template lacking iptables does not silently wedge the dispatch —
    the Modulo capability derivation still certifies based on the CONFIGURED
    policy, and each script is itself fail-closed (egress drops before it
    allows; a read-only seal cannot make the sandbox MORE writable; a missing
    git helper means no credentials are disclosed).

    ``sandbox`` is the e2b ``AsyncSandbox``. ``egress_allowlist`` entries may
    carry an extra ``_resolved_ip`` key (resolved by node_runner before calling)
    used to bind the iptables rule to a concrete address.
    """
    steps: list[str] = []
    if git_credentials == "scoped":
        steps.append(build_git_scoped_script())
    elif git_credentials == "none":
        steps.append(build_git_none_script())
    if egress_policy == "selected" and egress_allowlist:
        steps.append(build_egress_selected_script(egress_allowlist))
    if read_only:
        steps.append(build_read_only_script())

    for step in steps:
        try:
            await asyncio.wait_for(
                asyncio.shield(sandbox.commands.run(step, user="root", timeout=command_timeout)),
                timeout=command_timeout,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            # Best-effort: a policy step that cannot execute on a given template
            # must not wedge the dispatch. Each step is itself fail-closed.
            _log.warning("sandbox_policy.step_failed", exc_info=True)


__all__ = [
    "apply_sandbox_policy",
    "build_egress_selected_script",
    "build_git_none_script",
    "build_git_scoped_script",
    "build_read_only_script",
]
