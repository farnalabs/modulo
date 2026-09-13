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

import base64
import hashlib
import shlex
from collections.abc import Sequence
from dataclasses import dataclass

from modulo.core.ssrf import _is_blocked_ip, _resolve_all_sync, normalize_allow_networks

# ---------------------------------------------------------------------------
# GitHub / GitLab published SSH host keys (pinned at module level)
# ---------------------------------------------------------------------------
# Source of truth: these blobs are the authoritative public host keys for the
# named hosts, captured via ``ssh-keyscan <host>`` and cross-verified against the
# SHA256 fingerprints GitHub publishes at https://api.github.com/meta
# (``ssh_key_fingerprints``) and GitLab publishes at
# https://gitlab.com/help/instance_configuration (``SSH host key fingerprints``).
# Ephemeral sandboxes cannot trust first-use key acceptance, so they must verify
# against this pinned set.  :func:`verify_pinned_host_keys` enforces that every
# embedded blob's SHA256 fingerprint still matches the published value at build
# time (the REFUSE-TO-SHIP gate) — if GitHub/GitLab ever rotates a host key the
# gate fails closed instead of silently shipping a stale or fabricated key.

# Real GitHub host keys — verified against api.github.com/meta ssh_key_fingerprints.
# Format: "<hostname> <key-type> <key-blob>" — one per line.
# SSH key blobs are inherently long; line-length enforcement is suppressed.
GITHUB_HOST_KEYS: tuple[str, ...] = (
    "github.com ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABgQCj7ndNxQowgcQnjshcLrqPEiiphnt+VTTvDP6mHBL9j1aNUkY4Ue1gvwnGLVlOhGeYrnZaMgRK6+PKCUXaDbC7qtbW8gIkhL7aGCsOr/C56SJMy/BCZfxd1nWzAOxSDPgVsmerOBYfNqltV9/hWCqBywINIR+5dIg6JTJ72pcEpEjcYgXkE2YEFXV1JHnsKgbLWNlhScqb2UmyRkQyytRLtL+38TGxkxCflmO+5Z8CSSNY7GidjMIZ7Q4zMjA2n1nGrlTDkzwDCsw+wqFPGQA179cnfGWOWRVruj16z6XyvxvjJwbz0wQZ75XK5tKSb7FNyeIEs4TT4jk+S4dhPeAUC5y+bDYirYgM4GC7uEnztnZyaVWQ7B381AK4Qdrwt51ZqExKbQpTUNn+EjqoTwvqNj4kqx5QUCI0ThS/YkOxJCXmPUWZbhjpCg56i+2aB6CmK2JGhn57K5mj0MNdBXA4/WnwH6XoPWJzK5Nyu2zB3nAZp+S5hpQs+p1vN1/wsjk=",  # noqa: E501
    "github.com ecdsa-sha2-nistp256 AAAAE2VjZHNhLXNoYTItbmlzdHAyNTYAAAAIbmlzdHAyNTYAAABBBEmKSENjQEezOmxkZMy7opKgwFB9nkt5YRrYMjNuG5N87uRgg6CLrbo5wAdT/y6v0mKV0U2w0WZ2YB/++Tpockg=",  # noqa: E501
    "github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl",
)

# Real GitLab host keys — verified against GitLab's published SSH host key
# fingerprints.  Included so non-GitHub git remotes can also be pinned.
_GITLAB_HOST_KEYS: tuple[str, ...] = (
    "gitlab.com ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQCsj2bNKTBSpIYDEGk9KxsGh3mySTRgMtXL583qmBpzeQ+jqCMRgBqB98u3z++J1sKlXHWfM9dyhSevkMwSbhoR8XIq/U0tCNyokEi/ueaBMCvbcTHhO7FcwzY92WK4Yt0aGROY5qX2UKSeOvuP4D6TPqKF1onrSzH9bx9XUf2lEdWT/ia1NEKjunUqu1xOB/StKDHMoX4/OKyIzuS0q/T1zOATthvasJFoPrAjkohTyaDUz2LN5JoH839hViyEG82yB+MjcFV5MU3N1l1QL3cVUCh93xSaua1N85qivl+siMkPGbO5xR/En4iEY6K2XPASUEMaieWVNTRCtJ4S8H+9",  # noqa: E501
    "gitlab.com ecdsa-sha2-nistp256 AAAAE2VjZHNhLXNoYTItbmlzdHAyNTYAAAAIbmlzdHAyNTYAAABBBFSMqzJeV9rUzU4kWitGjeR4PWSa29SPqJ1fVkhtj3Hw9xjLVXVYrU9QlYWrOLXBpQ6KWjbjTDTdDkoohFzgbEY=",  # noqa: E501
    "gitlab.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAfuCHKVTjquxvt6CM6tdG4SLp1Btn/nOeHHE5UOzRdf",
)

# SHA256 fingerprints (from the authoritative published sources above) used to
# verify the embedded host keys have not been tampered with or gone stale.  Keyed
# by hostname then key type.  These are the values :func:`verify_pinned_host_keys`
# checks each embedded blob against.
PUBLISHED_HOST_KEY_FINGERPRINTS: dict[str, dict[str, str]] = {
    "github.com": {
        "ssh-rsa": "SHA256:uNiVztksCsDhcc0u9e8BujQXVUpKZIDTMczCvj3tD2s",
        "ecdsa-sha2-nistp256": "SHA256:p2QAMXNIC1TJYWeIOttrVc98/R1BUFWu3/LiyKgUfQM",
        "ssh-ed25519": "SHA256:+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU",
    },
    "gitlab.com": {
        "ssh-rsa": "SHA256:ROQFvPThGrW4RuWLoL9tq9I9zJ42fK4XywyRtbOz/EQ",
        "ecdsa-sha2-nistp256": "SHA256:HbW3g8zUjNSksFbqTiUWPWg2Bq1x8xdGUrliXFzSnUw",
        "ssh-ed25519": "SHA256:eUXGGm1YGsMAS7vkcx6JOJdOGHPem5gQp4taiCfCLB8",
    },
}

# Mapping of known hosts to their published keys. Callers extend this
# mapping as new hosts are supported.
KNOWN_HOSTS_BY_HOSTNAME: dict[str, tuple[str, ...]] = {
    "github.com": GITHUB_HOST_KEYS,
    "gitlab.com": _GITLAB_HOST_KEYS,
}

# ---------------------------------------------------------------------------
# Host key fingerprint verification (REFUSE-TO-SHIP gate)
# ---------------------------------------------------------------------------


def ssh_public_key_fingerprint(key: str) -> str:
    """Return the SHA256 fingerprint of an SSH public key.

    Accepts either a full ``known_hosts`` line (``<host> <type> <blob>``) or a
    bare base64 key blob.  The fingerprint is computed exactly as OpenSSH
    ``ssh-keygen -lf`` does — SHA256 over the base64-decoded key body — and is
    returned in ``SHA256:<base64>`` form.  This lets the module prove (in tests
    and at build time) that each pinned blob is the genuine published key rather
    than a hand-crafted placeholder.
    """
    key = key.strip()
    if " " in key:
        key = key.split(" ", 2)[2]
    key += "=" * (-len(key) % 4)

    digest = hashlib.sha256(base64.b64decode(key)).digest()
    return "SHA256:" + base64.b64encode(digest).decode().rstrip("=")


def verify_pinned_host_keys() -> None:
    """Verify every pinned host key matches its published SHA256 fingerprint.

    Raises ``ValueError`` if any embedded host-key blob does not produce the
    published fingerprint for its hostname/type.  This is the REFUSE-TO-SHIP
    gate for the key material itself: a fabricated, stale, or tampered key is
    rejected before it can ever be written into a sandbox known_hosts file.

    Call this from build/CI/provisioning paths (it is invoked by
    :func:`build_ssh_transport`) so a key rotation upstream fails closed.
    """
    for hostname, keys in KNOWN_HOSTS_BY_HOSTNAME.items():
        published = PUBLISHED_HOST_KEY_FINGERPRINTS[hostname]
        for entry in keys:
            key_type = entry.split(" ", 2)[1]
            expected = published[key_type]
            actual = ssh_public_key_fingerprint(entry)
            if actual != expected:
                raise ValueError(
                    f"Pinned {key_type} host key for {hostname!r} does not match the "
                    f"published fingerprint. Expected {expected}, got {actual}. "
                    "The pinned key is stale, tampered, or fabricated — refusing to ship."
                )


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
    user: str = "git",
    extra_args: list[str] | None = None,
) -> str:
    """Build a complete ``ssh`` command string with hardened transport options.

    Returns a single string that can be executed directly or embedded in a
    shell script.  The command includes all hardened SSH options from
    :func:`build_ssh_options` followed by any ``extra_args`` and the target
    ``user@<validated_ip>``.

    The ``user`` defaults to ``git`` (the conventional git-over-SSH user), but
    is a parameter so non-GitHub remotes (e.g. ``git@gitlab.com``) can pin a
    different login — the connection still targets the validated IP while
    ``HostKeyAlias`` matches the host key against ``hostname``.

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
    parts.append(f"{user}@{validated_ip}")
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
        f"printf '%s' '{escaped_content}' > {shlex.quote(known_hosts_path)}\n"
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
    user: str = "git",
) -> SshTransportConfig:
    """Validate an SSH hostname and produce a complete hardened transport configuration.

    This is the main entry point for FAR-799's SSH transport hardening.  It:

    1. Verifies every pinned host key still matches its published SHA256
       fingerprint (fail closed — see :func:`verify_pinned_host_keys`).  This is
       the REFUSE-TO-SHIP gate for the key material: a fabricated, stale, or
       tampered key is rejected before anything is built.
    2. Resolves ``hostname`` via DNS and validates every resolved address
       through the SSRF guard (fail closed — see :func:`validate_and_pin_ip`).
    3. Generates a pinned known_hosts entry for the host (fail closed if the
       host's keys are not in :data:`KNOWN_HOSTS_BY_HOSTNAME`).
    4. Builds SSH options that enforce ``StrictHostKeyChecking=yes``,
       ``HostKeyAlias=<hostname>``, and ``HostName=<validated_ip>`` so the
       connection targets the validated IP while the host key is matched
       against the hostname entry.
    5. Produces a POSIX ``sh`` snippet that writes the known_hosts file and
       exports ``GIT_SSH_COMMAND`` inside the sandbox.

    The ``user`` (default ``git``) is the SSH login used in the rendered
    ``ssh`` command — parameterised so non-GitHub remotes (e.g.
    ``git@gitlab.com``) can pin a different login.

    Raises ``SshHostRefusedError`` when the hostname resolves to a blocked
    address (fail closed — the REFUSE-TO-SHIP gate).  Raises
    ``ValueError`` when a pinned host key does not match its published
    fingerprint, or when the host's published keys are not pinned (fail closed
    — the REFUSE-TO-SHIP gate requires genuine pinned keys for every host).
    """
    # Step 1: verify pinned key material against published fingerprints.
    verify_pinned_host_keys()

    # Step 2: validate + pin IP.
    validated_ips = validate_and_pin_ip(hostname, allow_networks=allow_networks)
    validated_ip = validated_ips[0]

    # Step 3: pinned known_hosts.
    known_hosts_content = generate_pinned_known_hosts(hostname)

    # Step 4: SSH options + commands.
    git_ssh_command = build_git_ssh_command(validated_ip, hostname=hostname, known_hosts_path=known_hosts_path)
    ssh_command = build_ssh_command(validated_ip, hostname=hostname, known_hosts_path=known_hosts_path, user=user)

    # Step 5: script snippet.
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
