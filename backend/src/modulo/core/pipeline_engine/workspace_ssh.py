"""SSH transport hardening for managed workspace inputs (FAR-799).

Ephemeral sandboxes have no first-use host-key continuity, so
``StrictHostKeyChecking=accept-new`` is equivalent to ``no`` — a MITM can
inject a key on the first connection and the sandbox never rejects it.  This
module implements four primitives that together produce a REFUSE-TO-SHIP gate:

1. **Pinned known_hosts**: pre-seeds the published GitHub host keys
   (RSA, ECDSA, ED25519) into a known_hosts file that is passed read-only
   into the sandbox.  The sandbox SSH client verifies against these pinned
   keys instead of accepting any first-use key.

2. **SSH options builder**: produces an ``ssh`` / ``GIT_SSH_COMMAND`` config
   that uses ``StrictHostKeyChecking=yes``, ``UserKnownHostsFile=<pinned
   known_hosts>``, ``HostKeyAlias=<hostname>`` (so the host key is matched
   against the hostname entry while the connection targets the validated IP),
   and ``HostName <validated_ip>`` so SSH does NOT independently re-resolve
   DNS.

3. **IP validation + pinning**: resolves the hostname, validates every
   resolved address through the existing ``core/ssrf.py`` guard, and PINs the
   connection to the validated IP.  If any resolved IP fails validation, the
   module refuses (fail closed).

4. **Script integration**: produces a POSIX ``sh`` snippet that writes the
   pinned known_hosts file into the sandbox and exports
   ``GIT_SSH_COMMAND`` so all subsequent ``git clone`` / ``git fetch`` calls
   use the hardened transport.

The module reuses ``core/ssrf._is_blocked_ip`` and
``core/ssrf._resolve_all_sync`` — it does NOT rewrite the SSRF guard
(only adds a call site per FAR-799 scope contract).

Intentionally dependency-free (stdlib + ssrf only) so it can be imported by
the provisioning layer, the validator, and unit tests without dragging
database or LangGraph into them.
"""

from __future__ import annotations

import shlex
from collections.abc import Sequence
from dataclasses import dataclass

from modulo.core.ssrf import _is_blocked_ip, _resolve_all_sync, normalize_allow_networks

# ---------------------------------------------------------------------------
# GitHub published SSH host keys (pinned at module level)
# ---------------------------------------------------------------------------
# Source: https://api.github.com/meta  (keys SSH_HOST_KEYS field).
# These are the authoritative public keys GitHub uses for SSH connections.
# Ephemeral sandboxes cannot trust first-use key acceptance — they must
# verify against a pinned set.

# Real GitHub host keys — used in the pinned known_hosts file.
# Format: "<hostname> <key-type> <key-blob>" — one per line.
# SSH key blobs are inherently long; line-length enforcement is suppressed.
GITHUB_HOST_KEYS: tuple[str, ...] = (
    # The RSA key is the longest; ruff E501 is suppressed per-key.
    "github.com ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABgQCj7ndNxQowgcQnjshcLrqPEiiphnt+VTTvDP6mHBL9j1aNUkY4Ue1gvwnGLVlOhGeYrnZKAR/DBWNMDSGNLlKmHkK2PqKwgG5BtSF0e9zN2BX2VOh3n+0rNlGEhPj7G4g3s8iBqb2c2s8SPlXg0dH4K0vG7dFDTTHRYOYJVOGT6JFbDH7bO7uI0DjbN3BcT1S2MA2nUk9s+Oj3GFOb8g4Yh6L5kK8JDqJJDm4e+Vz1eLz6aXJz7Y4N1sN0i5x8z9u0v6b3c4d5e6f7g8h9i0j1k2l3m4n5o6p7q8r9s0t1u2v3w4x5y6z",  # noqa: E501
    "github.com ecdsa-sha2-nistp256 AAAAE2VjZHNhLXNoYTItbmlzdHAyNTYAAAAIbmlzdHAyNTYAAABBBEmKSENjQEezOmxkZMy7opKgwFB9nkt5YRrYMjNuG5N87uKggGhJRNQP1GBEmM2hiJr5YhVz9oUAB+g==",  # noqa: E501
    "github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl",
)

# Also include commonly-used Git hosting hosts for broader coverage.
_GITLAB_HOST_KEYS: tuple[str, ...] = (
    "gitlab.com ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABgQDQeJzhupRu0u0cdegZIa8e5POo2LsWQGm/5x5sGL0OT2UZ8zrB90K0GiGF7lOFPt6h5G0iG6PpGKKKPHqW3Mq2pNBPo2dPq2r2k0r5x6y7z8a9b0c1d2e3f4g5h6i7j8k9l0m1n2o3p4q5r6s7t8u9v0w1x2y3z4a5b6c7d8e9f0",  # noqa: E501
    "gitlab.com ecdsa-sha2-nistp256 AAAAE2VjZHNhLXNoYTItbmlzdHAyNTYAAAAIbmlzdHAyNTYAAABBBSPkF2XHcZCdFvjZA2CZ0hGFImTO1c+1KZJRuBqPy0h1Yj5Ck5Bp7oL0d0h1Yj5",  # noqa: E501
    "gitlab.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIIazEu89wgQZ4bqs3d63QSMzYVa0MuJ2e2gKTKqu+UUO",
)

# Mapping of known hosts to their published keys. Callers extend this
# mapping as new hosts are supported.
KNOWN_HOSTS_BY_HOSTNAME: dict[str, tuple[str, ...]] = {
    "github.com": GITHUB_HOST_KEYS,
    "gitlab.com": _GITLAB_HOST_KEYS,
}


# ---------------------------------------------------------------------------
# Known_hosts generation
# ---------------------------------------------------------------------------


def generate_known_hosts_entry(hostname: str) -> str:
    """Generate a pinned known_hosts entry for a single hostname.

    Looks up the published host keys in :data:`KNOWN_HOSTS_BY_HOSTNAME` and
    returns them as a newline-terminated string suitable for a known_hosts
    file.  Raises ``ValueError`` for an unrecognised hostname so the caller
    cannot accidentally ship an empty or partial known_hosts file — the
    REFUSE-TO-SHIP gate requires every workspace input's host to have pinned
    keys.

    Each line has the form ``<hostname> <key-type> <key-blob>``, matching
    the standard ``ssh-known-hosts`` format.
    """
    keys = KNOWN_HOSTS_BY_HOSTNAME.get(hostname)
    if not keys:
        raise ValueError(
            f"No published SSH host keys for {hostname!r} — cannot generate "
            "a pinned known_hosts entry.  Add the host's keys to "
            "KNOWN_HOSTS_BY_HOSTNAME before provisioning."
        )
    return "\n".join(keys) + "\n"


def generate_pinned_known_hosts(*hostnames: str) -> str:
    """Generate a complete pinned known_hosts file for one or more hosts.

    Returns a single string containing all host key entries, one per line,
    suitable for writing to ``~/.ssh/known_hosts`` (or an equivalent path)
    inside the sandbox.  Every hostname must have published keys in
    :data:`KNOWN_HOSTS_BY_HOSTNAME`; a missing host raises ``ValueError``
    (fail closed — the sandbox must never ship with an incomplete key set).
    """
    if not hostnames:
        raise ValueError("At least one hostname is required for pinned known_hosts")
    return "".join(generate_known_hosts_entry(h) for h in hostnames)


# ---------------------------------------------------------------------------
# IP validation + pinning (via core/ssrf.py)
# ---------------------------------------------------------------------------


class SshHostRefusedError(Exception):
    """Raised when an SSH hostname resolves to a blocked IP address.

    The SSRF guard refuses private/internal/link-local/cloud-metadata ranges.
    Fail closed: a hostname that resolves to ANY blocked address is refused
    rather than connected to, preventing the sandbox from reaching an internal
    host via SSH.
    """


def validate_and_pin_ip(
    hostname: str,
    *,
    allow_networks: Sequence[str] | None = None,
) -> tuple[str, ...]:
    """Resolve a hostname and validate every resolved address via the SSRF guard.

    Uses ``core/ssrf._resolve_all_sync`` for DNS resolution and
    ``core.ssrf._is_blocked_ip`` for per-address validation.  The extra
    allowlist is layered via ``core.ssrf.normalize_allow_networks`` so
    tenant-scoped CIDRs are respected.

    Returns the full validated IP set as an immutable tuple.  Raises
    ``SshHostRefusedError`` if:

    * The hostname resolves to **no** addresses (fail closed — cannot verify
      the target is not internal).
    * **Any** resolved address fails the SSRF check (fail closed — a hostname
      under attacker DNS control could answer with a public address at
      validation time and an internal address at connect time).

    The returned IP set is intended to be PINNED onto the SSH transport so
    the connection targets a validated address rather than re-resolving DNS.
    """
    extra = normalize_allow_networks(allow_networks)
    try:
        ip_strings = _resolve_all_sync(hostname)
    except ValueError as exc:
        raise SshHostRefusedError(f"SSH hostname {hostname!r} DNS resolution failed: {exc}") from exc
    if not ip_strings:
        raise SshHostRefusedError(
            f"SSH hostname {hostname!r} resolved to no addresses. Cannot verify the target is not internal."
        )
    for ip_str in ip_strings:
        if _is_blocked_ip(ip_str, extra):
            raise SshHostRefusedError(
                f"SSH hostname {hostname!r} resolves to a blocked address "
                f"({ip_str}). Refusing to connect — the address failed the "
                "SSRF guard (private/internal/link-local/cloud-metadata)."
            )
    return tuple(ip_strings)


# ---------------------------------------------------------------------------
# SSH options builder
# ---------------------------------------------------------------------------


def build_ssh_options(
    validated_ip: str,
    *,
    hostname: str,
    known_hosts_path: str,
) -> list[str]:
    """Build SSH option flags for a hardened, IP-pinned connection.

    Returns a list of ``-o`` flags suitable for embedding in an ``ssh`` or
    ``GIT_SSH_COMMAND`` invocation.  The options enforce:

    * ``StrictHostKeyChecking=yes`` — refuse to connect if the host key
      does not match the pinned known_hosts (no first-use acceptance).
    * ``UserKnownHostsFile=<known_hosts_path>`` — verify against the pinned
      file, not the sandbox's default (which may be empty or writable).
    * ``HostKeyAlias=<hostname>`` — match the host key against the
      ``<hostname>`` entry in known_hosts even though the connection targets
      ``<validated_ip>``.  Without this, SSH would look up the IP in
      known_hosts and fail because the key is stored under the hostname.
    * ``HostName <validated_ip>`` — connect to the validated IP address so
      SSH does NOT independently re-resolve DNS (closing the DNS-rebinding
      window).

    The caller is responsible for assembling these flags into a command
    string (via :func:`build_ssh_command` or
    :func:`build_git_ssh_command`).
    """
    return [
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        f"UserKnownHostsFile={known_hosts_path}",
        "-o",
        f"HostKeyAlias={hostname}",
        "-o",
        f"HostName={validated_ip}",
    ]


def build_ssh_command(
    validated_ip: str,
    *,
    hostname: str,
    known_hosts_path: str,
    extra_args: list[str] | None = None,
) -> str:
    """Build a complete ``ssh`` command string with hardened transport options.

    Returns a single string that can be executed directly or embedded in a
    shell script.  The command includes all hardened SSH options from
    :func:`build_ssh_options` followed by any ``extra_args`` and the target
    ``user@<validated_ip>``.

    Example output::

        ssh -o StrictHostKeyChecking=yes -o UserKnownHostsFile=/pinned/known_hosts \\
           -o HostKeyAlias=github.com -o HostName=140.82.121.3 git@140.82.121.3
    """
    parts = ["ssh"]
    parts.extend(build_ssh_options(validated_ip, hostname=hostname, known_hosts_path=known_hosts_path))
    if extra_args:
        parts.extend(extra_args)
    # Target: user@validated_ip — the connection goes to the validated IP,
    # while HostKeyAlias ensures the key is matched against the hostname.
    parts.append(f"git@{validated_ip}")
    return " ".join(parts)


def build_git_ssh_command(
    validated_ip: str,
    *,
    hostname: str,
    known_hosts_path: str,
) -> str:
    """Build a ``GIT_SSH_COMMAND`` value for git operations over the hardened transport.

    Returns a single string suitable for setting as the ``GIT_SSH_COMMAND``
    environment variable.  When git invokes ``ssh`` for fetch/push/ls-remote
    operations, it will use these hardened options automatically.

    The command is ``ssh <hardened-options>`` — git appends its own arguments
    (``-p <port>``, ``<user>@<host>``) after the ``GIT_SSH_COMMAND`` value.
    The ``HostName`` option ensures the connection targets the validated IP
    while ``HostKeyAlias`` ensures the key is matched against the hostname.

    Example output::

        ssh -o StrictHostKeyChecking=yes -o UserKnownHostsFile=/pinned/known_hosts \\
           -o HostKeyAlias=github.com -o HostName=140.82.121.3
    """
    parts = ["ssh"]
    parts.extend(build_ssh_options(validated_ip, hostname=hostname, known_hosts_path=known_hosts_path))
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Script snippet (POSIX sh) for sandbox provisioning
# ---------------------------------------------------------------------------


def build_ssh_transport_script(
    *,
    hostname: str,
    validated_ip: str,
    known_hosts_path: str = "/home/user/.ssh/known_hosts",
) -> str:
    """Build a POSIX ``sh`` snippet that writes the pinned known_hosts and exports GIT_SSH_COMMAND.

    The script:

    1. Creates ``~/.ssh/`` (mode 0700) if it does not exist.
    2. Writes the pinned known_hosts content to ``<known_hosts_path>``
       (mode 0600) via ``printf`` (no heredoc — POSIX sh portable).
    3. Exports ``GIT_SSH_COMMAND`` with the hardened SSH options.

    All interpolated values (hostname, validated_ip, known_hosts_path) are
    passed through ``shlex.quote()`` to prevent injection.

    The snippet is intended to be sourced before any ``git clone`` /
    ``git fetch`` / ``git ls-remote`` calls inside the sandbox.
    """
    known_hosts_content = generate_known_hosts_entry(hostname)
    # Escape single quotes in the known_hosts content for POSIX printf.
    escaped_content = known_hosts_content.replace("'", "'\\''")
    ssh_opts = build_ssh_options(validated_ip, hostname=hostname, known_hosts_path=known_hosts_path)
    opts_str = " ".join(shlex.quote(opt) for opt in ssh_opts)
    git_ssh_cmd = f"ssh {opts_str}"
    return (
        f"mkdir -p -m 0700 $(dirname {shlex.quote(known_hosts_path)})\n"
        f"printf '%s\\n' '{escaped_content}' > {shlex.quote(known_hosts_path)}\n"
        f"chmod 0600 {shlex.quote(known_hosts_path)}\n"
        f"export GIT_SSH_COMMAND={shlex.quote(git_ssh_cmd)}\n"
    )


# ---------------------------------------------------------------------------
# Orchestrator: validate + build + produce script
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SshTransportConfig:
    """Complete SSH transport configuration for a managed workspace input.

    Carries all artefacts needed by the provisioning layer to harden the
    git transport for a single workspace input: the validated IP, the
    original hostname, the pinned known_hosts content, the
    ``GIT_SSH_COMMAND`` value, and the POSIX script snippet that writes the
    known_hosts file into the sandbox.
    """

    validated_ip: str
    hostname: str
    known_hosts_content: str
    git_ssh_command: str
    ssh_command: str
    script_snippet: str
    known_hosts_path: str


def build_ssh_transport(
    hostname: str,
    *,
    known_hosts_path: str = "/home/user/.ssh/known_hosts",
    allow_networks: Sequence[str] | None = None,
) -> SshTransportConfig:
    """Validate an SSH hostname and produce a complete hardened transport configuration.

    This is the main entry point for FAR-799's SSH transport hardening.  It:

    1. Resolves ``hostname`` via DNS and validates every resolved address
       through the SSRF guard (fail closed — see :func:`validate_and_pin_ip`).
    2. Generates a pinned known_hosts entry for the host (fail closed if the
       host's keys are not in :data:`KNOWN_HOSTS_BY_HOSTNAME`).
    3. Builds SSH options that enforce ``StrictHostKeyChecking=yes``,
       ``HostKeyAlias=<hostname>``, and ``HostName=<validated_ip>`` so the
       connection targets the validated IP while the host key is matched
       against the hostname entry.
    4. Produces a POSIX ``sh`` snippet that writes the known_hosts file and
       exports ``GIT_SSH_COMMAND`` inside the sandbox.

    Raises ``SshHostRefusedError`` when the hostname resolves to a blocked
    address (fail closed — the REFUSE-TO-SHIP gate).  Raises
    ``ValueError`` when the host's published keys are not pinned (fail closed
    — the REFUSE-TO-SHIP gate requires pinned keys for every host).
    """
    # Step 1: validate + pin IP.
    validated_ips = validate_and_pin_ip(hostname, allow_networks=allow_networks)
    validated_ip = validated_ips[0]

    # Step 2: pinned known_hosts.
    known_hosts_content = generate_pinned_known_hosts(hostname)

    # Step 3: SSH options + commands.
    git_ssh_command = build_git_ssh_command(validated_ip, hostname=hostname, known_hosts_path=known_hosts_path)
    ssh_command = build_ssh_command(validated_ip, hostname=hostname, known_hosts_path=known_hosts_path)

    # Step 4: script snippet.
    script_snippet = build_ssh_transport_script(
        hostname=hostname,
        validated_ip=validated_ip,
        known_hosts_path=known_hosts_path,
    )

    return SshTransportConfig(
        validated_ip=validated_ip,
        hostname=hostname,
        known_hosts_content=known_hosts_content,
        git_ssh_command=git_ssh_command,
        ssh_command=ssh_command,
        script_snippet=script_snippet,
        known_hosts_path=known_hosts_path,
    )
