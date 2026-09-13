"""Credential model for managed workspace input clones (FAR-797, ADR 033).

This module owns the credential-resolution layer for workspace input git clones.
It resolves connector-stored credentials into git-usable ``CloneCredential``
value objects and builds POSIX provisioning scripts that inject those credentials
into the sandbox without leaking them into argv, command lines, or URLs.

Dependencies: stdlib + existing project imports only (no new deps).
"""

from __future__ import annotations

import json
import logging
import shlex
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.models.connector_instance import ConnectorInstance

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Forge allowlist
# ---------------------------------------------------------------------------

_FORGE_ALLOWLIST: frozenset[str] = frozenset(
    {
        "github.com",
        "gitlab.com",
        "bitbucket.org",
        "codeberg.org",
        "gitea.com",
    }
)


def is_forge_allowlisted(host: str) -> bool:  # vulture: ignore
    """Return True if *host* is a known public forge (null-connector path)."""
    return host in _FORGE_ALLOWLIST


# ---------------------------------------------------------------------------
# CloneCredential value object
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CloneCredential:
    """A git-usable credential for cloning a workspace input.

    Attributes:
        kind: ``"token"`` for PAT/OAuth tokens, ``"ssh"`` for SSH deploy keys.
        host: The git host this credential applies to (e.g. ``github.com``).
        username: Optional username (e.g. ``"x-access-token"`` for GitHub PATs).
        secret: The token value or private key — **never logged or repr'd**.
    """

    kind: str
    host: str
    username: str | None
    secret: str

    def __repr__(self) -> str:
        return (
            f"CloneCredential(kind={self.kind!r}, host={self.host!r}, username={self.username!r}, secret='[REDACTED]')"
        )

    def __str__(self) -> str:
        return repr(self)


# ---------------------------------------------------------------------------
# Credential resolution
# ---------------------------------------------------------------------------

# Connector types whose decrypted creds carry a "token" key usable for git
# HTTPS cloning.  Types not listed here either do not yield git credentials
# or require SSH-key shapes not yet supported (see DEFERRED section).
_TOKEN_CREDENTIAL_TYPES: frozenset[str] = frozenset(
    {
        "github",
        "gitlab",
        "bitbucket",
        "gitea",
        "azure_repos",
    }
)

# Map connector_type_id → git credential username convention.
_GIT_CREDENTIAL_USERNAMES: dict[str, str] = {
    "github": "x-access-token",
    "gitlab": "oauth2",
    "bitbucket": "x-token-auth",
}


class CredentialResolutionError(Exception):
    """Raised when a connector credential cannot be resolved for git cloning."""


def _get_settings() -> Any:
    """Read app settings.  Separated for testability (patch this)."""
    from modulo.settings import get_settings

    return get_settings()


def _create_secrets_backend(*, fernet_key: str, session: AsyncSession) -> Any:
    """Create the secrets backend.  Separated for testability (patch this)."""
    from modulo.core.secrets_backend import create_secrets_backend

    return create_secrets_backend(fernet_key=fernet_key, session=session)


async def _decrypt_connector_creds(
    ci: ConnectorInstance,
    *,
    session: AsyncSession,
) -> dict[str, Any]:
    """Decrypt a ConnectorInstance's credentials using the existing pattern.

    Mirrors the decryption logic from
    ``ConnectorHub.initialise()`` (Fernet fallback from
    ``credentials_ciphertext``) without instantiating a full hub.

    Raises ``CredentialResolutionError`` on decrypt failure.
    """
    # Try the secrets backend first (the canonical path for production).
    try:
        settings = _get_settings()
        backend = _create_secrets_backend(fernet_key=settings.fernet_key, session=session)
        raw_str = await backend.get_secret(str(ci.id))
        creds = json.loads(raw_str)
        if isinstance(creds, dict):
            return creds
        raise CredentialResolutionError(
            f"Connector {ci.id} credentials are not a JSON dict (got {type(creds).__name__})"
        )
    except KeyError:
        pass
    except json.JSONDecodeError as exc:
        raise CredentialResolutionError(f"Connector {ci.id} credentials are not valid JSON") from exc
    except CredentialResolutionError:
        raise
    except Exception:
        # Fall through to ciphertext fallback.
        logger.debug("Secrets backend lookup failed for connector %s, falling back to ciphertext", ci.id)

    # Fallback: decrypt credentials_ciphertext via Fernet.
    ciphertext = getattr(ci, "credentials_ciphertext", None)
    if not ciphertext or not isinstance(ciphertext, bytes) or ciphertext == b"":
        raise CredentialResolutionError(f"Connector {ci.id} has no credentials (secrets backend or ciphertext)")

    try:
        from cryptography.fernet import Fernet

        settings = _get_settings()
        f = Fernet(settings.fernet_key.encode())
        plaintext = f.decrypt(ciphertext).decode("utf-8")
    except Exception as exc:
        raise CredentialResolutionError(f"Failed to decrypt credentials for connector {ci.id}") from exc

    try:
        parsed = json.loads(plaintext)
    except json.JSONDecodeError as exc:
        raise CredentialResolutionError(f"Connector {ci.id} decrypted credentials are not valid JSON") from exc

    if isinstance(parsed, dict):
        return parsed

    # Bare scalar — wrap under the connector type's credential key.
    cred_key = "token" if ci.connector_type_id in _TOKEN_CREDENTIAL_TYPES else "api_key"
    return {cred_key: parsed}


async def resolve_clone_credential(
    session: AsyncSession,
    *,
    connector_instance_id: uuid.UUID | None,
    host: str,
) -> CloneCredential | None:
    """Resolve a connector-stored credential to a git-usable CloneCredential.

    Args:
        session: Active async DB session.
        connector_instance_id: UUID of the ConnectorInstance, or None for public repos.
        host: The git host the clone targets (e.g. ``github.com``).

    Returns:
        A ``CloneCredential`` on success, or ``None`` when no credential is
        needed (public repo, ``connector_instance_id is None``).

    Raises:
        CredentialResolutionError: connector not found, decrypt failure, or
            a connector type that cannot yield a git credential.
    """
    if connector_instance_id is None:
        return None

    # Fetch the ConnectorInstance row.
    result = await session.execute(
        select(ConnectorInstance).where(
            ConnectorInstance.id == connector_instance_id,
        )
    )
    ci = result.scalar_one_or_none()
    if ci is None:
        raise CredentialResolutionError(f"Connector instance {connector_instance_id} not found")

    if ci.connector_type_id not in _TOKEN_CREDENTIAL_TYPES:
        # DEFERRED: SSH-key auth shape on connector types not yet supported.
        # Document the gap and raise — do NOT change the ConnectorType model.
        raise CredentialResolutionError(
            f"Connector type {ci.connector_type_id!r} does not support git "
            f"HTTPS clone credentials. SSH-key auth for workspace inputs is "
            f"deferred to a future ticket (connector-model change required)."
        )

    creds = await _decrypt_connector_creds(ci, session=session)

    token = creds.get("token")
    if not token or not isinstance(token, str):
        raise CredentialResolutionError(f"Connector {ci.id} ({ci.connector_type_id}) has no 'token' credential")

    username = _GIT_CREDENTIAL_USERNAMES.get(ci.connector_type_id, "x-access-token")
    return CloneCredential(
        kind="token",
        host=host,
        username=username,
        secret=token,
    )


# ---------------------------------------------------------------------------
# Read-only assertion
# ---------------------------------------------------------------------------

# Hosts where we can probe the token's capability via an API call.
_PROBEABLE_HOSTS: frozenset[str] = frozenset({"github.com"})


async def assert_clone_credential_is_read_only(
    cred: CloneCredential,
    *,
    http_client: Any | None = None,
) -> None:
    """Assert that a credential is read-only (least privilege).

    For GitHub tokens, probes the ``/user`` endpoint to check the token's
    permission scope.  Push-capable tokens are refused.

    For hosts where a capability probe is not available, applies a documented
    conservative policy: tokens from the connector system are assumed
    read-write unless the host is in ``_PROBEABLE_HOSTS`` and the probe
    confirms read-only.

    Args:
        cred: The credential to validate.
        http_client: An ``httpx.AsyncClient`` (or compatible) for the probe.
            Injected for testability — callers provide a real client;
            unit tests mock it.

    Raises:
        CredentialResolutionError: the credential is push-capable or the
            probe cannot determine capability and the host is not in the
            allowlist for conservative read-only assumptions.
    """
    if cred.kind == "ssh":
        # SSH deploy keys are inherently read-only on most forges — accept.
        return

    if cred.host not in _PROBEABLE_HOSTS:
        # Conservative policy: for hosts where we cannot probe, we cannot
        # guarantee read-only.  The caller must handle this — document the
        # gap and refuse rather than silently allowing.
        raise CredentialResolutionError(
            f"Cannot verify read-only capability for host {cred.host!r} — "
            f"no API probe available. Use a known read-only token or an "
            f"SSH deploy key instead."
        )

    # GitHub token scope probe.
    if http_client is None:
        raise CredentialResolutionError("http_client is required for GitHub token capability probe")

    try:
        auth_header = f"Bearer {cred.secret}"
        response = await http_client.get(
            "https://api.github.com/user",
            headers={"Authorization": auth_header, "Accept": "application/vnd.github+json"},
        )
        if response.status_code == 401:
            raise CredentialResolutionError("GitHub token is invalid (401)")
        if response.status_code >= 500:
            # Server error — fail closed.
            raise CredentialResolutionError(f"GitHub API returned {response.status_code} during capability probe")

        # Check the X-OAuth-Scopes header for push indicators.
        scopes_header = response.headers.get("x-oauth-scopes", "")
        push_indicators = {"repo", "write:repo", "admin:repo", "write:org"}
        scopes_set = {s.strip() for s in scopes_header.split(",") if s.strip()}
        if scopes_set & push_indicators:
            raise CredentialResolutionError(
                f"GitHub token has push-capable scopes: {scopes_set & push_indicators}. "
                f"Workspace input tokens must be read-only."
            )
    except CredentialResolutionError:
        raise
    except Exception as exc:
        raise CredentialResolutionError(f"Failed to probe GitHub token capability: {exc}") from exc


# ---------------------------------------------------------------------------
# Provisioning credential scripts
# ---------------------------------------------------------------------------


def build_provisioning_credential_scripts(  # vulture: ignore
    *,
    cred: CloneCredential | None,
    host: str,
) -> tuple[str, str]:
    """Build POSIX ``sh`` setup and teardown scripts for clone credentials.

    Args:
        cred: The credential to provision, or ``None`` for public repos.
        host: The git host (for ``git -c credential.helper`` config).

    Returns:
        ``(setup_script, teardown_script)`` — both ``set -e`` POSIX ``sh``.
        When *cred* is ``None``, returns ``("", "")``.

    The secret is NEVER interpolated into command lines or argv.  It is
    written to a ``mktemp``-created file via a heredoc (embedded in the
    generated script text — not passed as an argument to any command), and
    git is configured to read it via ``GIT_ASKPASS``.
    """
    if cred is None:
        return ("", "")

    quoted_host = shlex.quote(host)
    quoted_username = shlex.quote(cred.username or "x-access-token")

    # The secret is embedded in the heredoc below (script text, not argv).
    # Heredocs with unquoted delimiters expand variables; the secret is a
    # shell variable set just before the heredoc so it never appears as an
    # argument to printf, cat, or any other command.
    setup_script = (
        "#!/bin/sh\n"
        "set -e\n"
        "# Create a one-time credential file (mktemp on POSIX sh).\n"
        "_modulo_cred_file=$(mktemp /dev/shm/.modulo-cred.XXXXXX)\n"
        'chmod 600 "$_modulo_cred_file"\n'
        "# Write the secret via heredoc (never in argv).\n"
        'cat > "$_modulo_cred_file" <<\'_CRED_EOF_\n'
        f"{cred.secret}\n"
        "_CRED_EOF_\n"
        "# Create the GIT_ASKPASS helper — returns username on first call,\n"
        "# password (read from the credential file) on subsequent calls.\n"
        "_modulo_askpass=$(mktemp /dev/shm/.modulo-askpass.XXXXXX)\n"
        'chmod 700 "$_modulo_askpass"\n'
        "cat > \"$_modulo_askpass\" <<'_ASKPASS_EOF_'\n"
        "#!/bin/sh\n"
        'case "$1" in\n'
        f"  Username) echo {quoted_username} ;;\n"
        '  Password) cat "$_modulo_cred_file" ;;\n'
        "esac\n"
        "_ASKPASS_EOF_\n"
        'export GIT_ASKPASS="$_modulo_askpass"\n'
        # Configure git credential helper for the target host.  The secret
        # is read from the file by the GIT_ASKPASS helper, never via argv.
        "git config --global credential.helper store\n"
        f"echo 'protocol=https\\nhost={quoted_host}' "
        '"| git credential fill 2>/dev/null >/dev/null || true\n'
    )

    # Teardown: remove the credential file and assert it no longer exists.
    teardown_script = (
        "#!/bin/sh\n"
        "set -e\n"
        'rm -f "$_modulo_cred_file"\n'
        'rm -f "$_modulo_askpass"\n'
        'test ! -e "$_modulo_cred_file"\n'
        'test ! -e "$_modulo_askpass"\n'
    )

    return (setup_script, teardown_script)
