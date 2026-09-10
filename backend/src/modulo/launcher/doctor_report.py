"""``modulo doctor --report``: a REDACTED diagnostic zip (FAR-676).

Builds a self-contained support bundle: versions, OS info, the doctor
output, and the tails of the data-dir logs (app + bundled children) — with
EVERY generated credential scrubbed before anything is written.

The redaction map is SEEDED with the ACTUAL generated credential values
(the bundled postgres/redis passwords and the state.json HMAC key read from
the 0600 secrets file). The map's values are never embedded in the archive:
only the raw secret values are used as scrub patterns, replaced by
``<redacted>``. Structural redaction runs on top: ``KEY=value`` lines are
masked through the canonical sensitive-field classifier — the same
classifier ``modulo env`` uses — so credential-like fields whose values
never enter the seed map (operator-set webhook tokens and the like) are
also masked.

Credential-free by construction: secrets.json itself is never archived,
and every member is the REDACTED text plus an inventory manifest.
"""

from __future__ import annotations

import json
import os
import platform
import sys
import zipfile
from pathlib import Path
from typing import Any

from modulo.launcher.secrets_file import LauncherSecrets, SecretsFileError, _parse

REDACTED = "<redacted>"
DEFAULT_MAX_LOG_BYTES = 256 * 1024
MAX_LOG_MEMBERS = 50

SENSITIVE_FIELD_TOKENS = ("password", "secret", "token", "api_key", "private_key", "webhook", "users", "oidc")
SENSITIVE_FIELD_NAMES = frozenset(
    {
        "secret_key",
        "fernet_key",
        "fernet_key_old",
        # Wholesale redaction (values embed credentials the tokens above can
        # not see) — mirrors modulo cli.main's wholesale set.
        "modulo_oidc_providers",
        "modulo_users",
        "alert_webhook_url",
        "alert_teams_webhook_url",
    }
)

__all__ = [
    "DEFAULT_MAX_LOG_BYTES",
    "REDACTED",
    "build_report",
    "redact_text",
    "redaction_map_from_data_dir",
]


def _package_version() -> str:
    try:
        from importlib.metadata import version

        return version("farnalabs-modulo")
    except Exception:
        return "unknown"


def redaction_map_from_data_dir(data_dir: Path) -> dict[str, str]:
    """Secret VALUE -> ``<redacted>`` map (the values are the patterns).

    Seeded from the generated credentials in secrets.json. Nothing here ever
    enters the archive: the map keys (the raw values) are only ever used as
    scrub patterns. Unreadable secrets = empty map (the structural
    redaction still applies).
    """
    secrets_path = data_dir / "secrets.json"
    if not secrets_path.is_file():
        return {}
    try:
        secrets: LauncherSecrets = _parse(secrets_path.read_bytes())
    except (SecretsFileError, OSError):
        return {}
    return dict.fromkeys((secrets.postgres_password, secrets.redis_password, secrets.state_hmac_key_hex), REDACTED)


def redact_text(text: str, secret_values: dict[str, str]) -> str:
    """Scrub *text*: seeded credential values via exact replacement, then the
    ``KEY=value`` structural pass (canonical classifier; the same rules
    ``modulo env`` applies to Settings dumps). The seeded map holds FULL
    generated values, never fragments.
    """
    output = text
    for secret, replacement in secret_values.items():
        if secret:
            output = output.replace(secret, replacement)
    return _redact_kv_lines(output)


def _is_sensitive_key(key: str) -> bool:
    """Canonical sensitive-field classification — the SAME classifier and
    wholesale-redaction set ``modulo env`` uses (single source of truth)."""
    if key.lower() in SENSITIVE_FIELD_NAMES:
        return True
    try:
        from modulo.api.middleware.sensitive_mask import is_sensitive_env_key

        return is_sensitive_env_key(key.upper())
    except Exception:
        lowered = key.lower()
        return any(token in lowered for token in SENSITIVE_FIELD_TOKENS)


def _redact_kv_lines(text: str) -> str:
    lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if "=" in stripped and not stripped.startswith("#"):
            key = stripped.partition("=")[0].strip()
            if _is_sensitive_key(key):
                indent = line[: len(line) - len(line.lstrip())]
                lines.append(f"{indent}{key}={REDACTED}")
                continue
        lines.append(line)
    return "\n".join(lines)


def build_report(
    data_dir: Path,
    out_path: Path,
    *,
    doctor_output: str,
    max_log_bytes: int = DEFAULT_MAX_LOG_BYTES,
) -> Path:
    """Write the redacted diagnostic zip to *out_path*; return its path.

    Members (all redacted before writing): ``report.json`` inventory (member
    names + byte sizes, NO contents), ``version.txt`` (package version + OS
    + Python), ``doctor-output.txt``, and one ``logs/<component>.log.tail``
    member per data-dir log source.
    """
    from modulo.launcher.supervisor import CHILD_LOG_NAMES, log_paths, read_log_tail

    secret_values = redaction_map_from_data_dir(data_dir)
    version_text = "\n".join(
        [
            f"modulo version: {_package_version()}",
            f"python: {sys.version}",
            f"os: {platform.platform()}",
            f"arch: {platform.machine()}",
            f"data dir: {data_dir}",
        ]
    )
    members: dict[str, str] = {
        "version.txt": redact_text(version_text, secret_values),
        "doctor-output.txt": redact_text(doctor_output, secret_values),
    }
    log_members: list[str] = []
    for name in sorted(log_paths(data_dir)):
        member = f"logs/{name}.log.tail"
        members[member] = redact_text(
            read_log_tail(log_paths(data_dir)[name], max_bytes=max_log_bytes),
            secret_values,
        )
        log_members.append(member)
    manifest: dict[str, Any] = {
        "generated_by": "modulo doctor --report",
        "redacted_marker": REDACTED,
        "members": [
            {"name": member, "bytes": len(members[member].encode("utf-8"))}
            for member in sorted(members)[:MAX_LOG_MEMBERS]
        ],
        "log_members": log_members,
        "child_log_names": list(CHILD_LOG_NAMES),
        "os_family": os.name,
    }
    members["report.json"] = json.dumps(manifest, indent=2, sort_keys=True)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for member in sorted(members):
            archive.writestr(member, members[member].encode("utf-8"))
    return out_path
