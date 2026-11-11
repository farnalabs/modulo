"""Support bundle format v1 (FAR-2305).

One artefact a self-hoster can safely send to Farnalabs: a zip with a FIXED,
versioned member allowlist and a five-layer, fail-closed redaction pipeline.
See :mod:`modulo.support.redaction` for the layers.

The member set is closed: adding one requires updating :data:`GOLDEN_MEMBERS`,
which the golden-member test pins. Readers tolerate unknown members so a newer
bundle can still be inspected by an older reader (forward compatibility).

When a member still trips the layer-5 gate, the build withholds only what
tripped (FAR-2333): the offending error group, run row or log line for a
collection member, the whole member for a small invariant. Every withholding
is recorded in ``MANIFEST.txt`` and ``report.json``. Only a trip that cannot
be attributed to an item refuses the whole build.

Runs inside the app container with no launcher and no data dir:

``python -m modulo.support.bundle --out <path> [--json]``
``python -m modulo.support.bundle --redact-stdin``

Every bundle carries a PER-RUN reference (``MSB-<format_version>-<UTC
yyyymmddThhmmssZ>-<8 lowercase hex>``) stamped in ``report.json`` and
``MANIFEST.txt`` and printed as one pastable line, so a bundle file can be
matched to the run that produced it. The Compose collector emits the same shape.

``--redact-stdin`` is the text-only mode the collector uses so it never has to
reimplement redaction in bash: it reads UTF-8 text on stdin and writes the
redacted text on stdout. It reuses the SAME layers 2-4 scrubbers and the
layer-5 gate as a bundle build, and it fails closed: an unclassified
high-entropy token exits non-zero with nothing on stdout.

Database-backed members (``db.txt`` / ``runs.json`` / ``errors.json``) degrade
honestly: if the DB is unreachable the rest of the bundle is still emitted and
the missing members are recorded with a reason in ``MANIFEST.txt`` and
``report.json``.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import platform
import secrets
import sys
import time
import zipfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from farna.support.redaction import (
    LAYER_EXACT,
    LAYER_PATTERNS,
    LAYER_STRUCTURAL,
    REDACTED,
    RedactionGateError,
    Redactor,
    collect_secret_values,
)

__all__ = [
    "BUNDLE_FORMAT_VERSION",
    "GOLDEN_MEMBERS",
    "BundleResult",
    "BundleSources",
    "BundleTooLargeError",
    "RedactionGateError",
    "build_bundle",
    "build_bundle_async",
    "bundle_reference",
    "collapse_repeated_lines",
    "generate_ref",
    "inspect_bundle",
    "is_allowed_member",
    "main",
    "read_bundle",
    "ref_line",
    "summary_lines",
]

#: Integer format version, stamped in ``report.json`` and the printed ref line.
BUNDLE_FORMAT_VERSION = 1

#: Compressed archive size cap. Over budget, log members are truncated and the
#: truncation is recorded in the manifest.
MAX_BUNDLE_BYTES = 5 * 1024 * 1024

#: Per-log-member source cap (before collapsing/truncation).
DEFAULT_MAX_LOG_BYTES = 256 * 1024

#: Hard line bound per log member after collapsing repeated lines.
MAX_LOG_LINES = 2000

MAX_ERROR_GROUPS = 25
MAX_FAILED_RUNS = 20

MANIFEST_MEMBER = "MANIFEST.txt"
REPORT_MEMBER = "report.json"
VERSION_MEMBER = "version.txt"
DOCTOR_MEMBER = "doctor.txt"
HEALTH_MEMBER = "health.json"
SETTINGS_MEMBER = "settings.txt"
DEPLOYMENT_MEMBER = "deployment.txt"
DB_MEMBER = "db.txt"
ERRORS_MEMBER = "errors.json"
RUNS_MEMBER = "runs.json"
LICENCE_MEMBER = "licence.txt"
DEGRADED_MEMBER = "degraded.json"

LOG_MEMBER_SUFFIX = ".log.tail"
LOG_MEMBER_PREFIX = "logs/"

#: Small invariant members. When one trips the layer-5 gate it is withheld WHOLE
#: (there is no meaningful item to keep), and the rest of the bundle still
#: builds (FAR-2333).
_MEMBER_LEVEL_WITHHOLD_MEMBERS: frozenset[str] = frozenset(
    {
        VERSION_MEMBER,
        DOCTOR_MEMBER,
        HEALTH_MEMBER,
        SETTINGS_MEMBER,
        DEPLOYMENT_MEMBER,
        DB_MEMBER,
        LICENCE_MEMBER,
        DEGRADED_MEMBER,
    }
)

#: Members that decompose into per-item records, keyed by the JSON list field
#: that holds them. One tripping record is withheld, the rest are kept.
_JSON_ITEM_MEMBER_KEYS: dict[str, str] = {
    ERRORS_MEMBER: "error_groups",
    RUNS_MEMBER: "runs",
}

#: The fixed member allowlist (layer 1). A member not in this set (and not a
#: well-formed ``logs/<component>.log.tail``) is refused at assembly time.
GOLDEN_MEMBERS: tuple[str, ...] = (
    MANIFEST_MEMBER,
    REPORT_MEMBER,
    VERSION_MEMBER,
    DOCTOR_MEMBER,
    HEALTH_MEMBER,
    SETTINGS_MEMBER,
    DEPLOYMENT_MEMBER,
    DB_MEMBER,
    ERRORS_MEMBER,
    RUNS_MEMBER,
    LICENCE_MEMBER,
    DEGRADED_MEMBER,
)

_MEMBER_PURPOSE: dict[str, str] = {
    MANIFEST_MEMBER: "human-readable inventory: format version, members + byte counts, purposes, missing list",
    REPORT_MEMBER: "machine inventory: format version, members + bytes, redaction substitution counts per layer",
    VERSION_MEMBER: "modulo version, build commit, Python, OS, arch, UTC offset + clock drift",
    DOCTOR_MEMBER: "existing doctor output",
    HEALTH_MEMBER: "health check names, statuses and error codes only",
    SETTINGS_MEMBER: "FULL key inventory; safe-list values only, everything else key + set/unset",
    DEPLOYMENT_MEMBER: "install shape, services, image tags/digests, restart counts, proxy NAMES, CA path",
    DB_MEMBER: "migration head, expected head, PG version, row counts for a fixed table list (no rows)",
    ERRORS_MEMBER: "top error groups: identity, level, source, counts, times, environment, version, message",
    RUNS_MEMBER: "last failed runs: ids, status, error code, timings, cost (no inputs/outputs/stdout)",
    LICENCE_MEMBER: "tier, state, expiry, opaque id, feature list (never the key)",
    DEGRADED_MEMBER: "native only: terminal-degrade reason + last crash backtraces (redacted)",
}

#: Settings whose VALUES are reviewed safe to publish verbatim. Everything
#: else is emitted as a key plus ``set``/``unset`` only.
SAFE_SETTINGS_VALUE_FIELDS: frozenset[str] = frozenset(
    {
        "modulo_db",
        "modulo_public_url",
        "modulo_frontend_url",
        "saq_worker_concurrency",
        "saq_worker_lost_window",
        "saq_worker_db_pool_size",
    }
)

_PROXY_ENV_NAMES: tuple[str, ...] = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
    "ALL_PROXY",
    "all_proxy",
)
_CA_BUNDLE_ENV_NAMES: tuple[str, ...] = ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE")
_IMAGE_ENV_NAMES: tuple[str, ...] = ("IMAGE_REF", "APP_IMAGE", "FLY_IMAGE_REF", "MODULO_IMAGE")
_BUILD_SHA_ENV_NAMES: tuple[str, ...] = ("GIT_SHA", "BUILD_SHA", "MODULO_BUILD_COMMIT")
_BUILD_TIMESTAMP_ENV_NAMES: tuple[str, ...] = ("BUILD_TIMESTAMP", "GIT_COMMIT_TIMESTAMP")

_TABLE_ROW_COUNT_QUERIES: dict[str, str] = {
    "accounts": "SELECT count(*) FROM accounts",
    "error_events": "SELECT count(*) FROM error_events",
    "error_groups": "SELECT count(*) FROM error_groups",
    "node_observations": "SELECT count(*) FROM node_observations",
    "organisations": "SELECT count(*) FROM organisations",
    "pipeline_snapshots": "SELECT count(*) FROM pipeline_snapshots",
    "pipelines": "SELECT count(*) FROM pipelines",
    "runs": "SELECT count(*) FROM runs",
}

_FAILED_RUN_STATUSES: tuple[str, ...] = (
    "failed",
    "cancelled",
    "eval_failed",
    "stalled",
    "budget_exceeded",
    "router_no_match",
    "cost_ceiling_exceeded",
    "compensation_failed",
    "rejected",
)

_PROCESS_START_WALL = time.time()
_PROCESS_START_MONO = time.monotonic()


class BundleTooLargeError(RuntimeError):
    """The compressed bundle still exceeds the cap after every log was truncated."""


@dataclass(frozen=True)
class BundleResult:
    """Outcome of a successful build (also the shape ``--json`` prints)."""

    bundle_path: Path
    members: tuple[tuple[str, int], ...]
    total_bytes: int
    redaction_counts: dict[str, int]
    missing: tuple[dict[str, str], ...]
    truncated_log_members: tuple[str, ...]
    bundle_format_version: int = BUNDLE_FORMAT_VERSION
    #: Per-run reference (``MSB-...``); empty only for a bundle that predates
    #: the ref or a reader that could not recover it.
    ref: str = ""
    #: Gate-triggered withholdings: ``{member, items, reason, scope}`` entries.
    #: Empty when every member passed the layer-5 gate intact (FAR-2333).
    withheld: tuple[dict[str, Any], ...] = ()

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "bundle_format_version": self.bundle_format_version,
            # ``reference`` stays the FORMAT identifier; the per-run token is
            # ``ref``. They are deliberately separate fields.
            "reference": bundle_reference(self.bundle_format_version),
            "ref": self.ref,
            "ref_line": ref_line(self.ref, self.bundle_format_version) if self.ref else "",
            "bundle_path": str(self.bundle_path),
            "members": [{"name": name, "bytes": size} for name, size in self.members],
            "bytes": self.total_bytes,
            "redaction": dict(self.redaction_counts),
            "missing": list(self.missing),
            "truncated_log_members": list(self.truncated_log_members),
            "withheld": list(self.withheld),
        }


@dataclass
class BundleSources:
    """Injectable inputs — the seam tests use; production leaves them ``None``.

    ``session_factory`` is a zero-arg callable returning an async session
    context manager (``AsyncSessionLocal``'s shape).
    """

    data_dir: Path | None = None
    doctor_output: str | None = None
    settings: Any = None
    environ: Mapping[str, str] | None = None
    session_factory: Callable[[], Any] | None = None
    extra_secrets: Mapping[str, str] | None = None
    max_log_bytes: int = DEFAULT_MAX_LOG_BYTES
    max_bundle_bytes: int = MAX_BUNDLE_BYTES
    enable_db: bool = True

    def resolved_environ(self) -> Mapping[str, str]:
        return self.environ if self.environ is not None else os.environ


@dataclass
class _DbMembers:
    db_text: str | None = None
    errors_json: str | None = None
    runs_json: str | None = None
    missing: list[dict[str, str]] = field(default_factory=list)


def bundle_reference(version: int = BUNDLE_FORMAT_VERSION) -> str:
    """The FORMAT identifier (stable across every bundle of that format).

    Not to be confused with :func:`generate_ref`, the per-run token that is
    also usable to match the produced file to its run.
    """
    return f"modulo-support-bundle-v{version}"


def generate_ref(version: int = BUNDLE_FORMAT_VERSION) -> str:
    """One per-run reference: ``MSB-<version>-<UTC stamp>-<8 lowercase hex>``.

    The Compose collector emits this exact shape, so both producers agree and a
    file can be matched to its reference by name or by content.
    """
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"MSB-{version}-{stamp}-{secrets.token_hex(4)}"


def ref_line(ref: str, version: int = BUNDLE_FORMAT_VERSION) -> str:
    """The single pastable line a user quotes when sending a bundle."""
    return f"modulo support bundle {version} ref={ref}"


def summary_lines(result: BundleResult) -> list[str]:
    """Human lines every CLI entry point prints for a built bundle.

    Ref line, bundle path + byte size, and the one instruction that matters:
    the bundle is redacted but the user should still open it before sending.
    """
    lines = [
        ref_line(result.ref, result.bundle_format_version),
        f"bundle: {result.bundle_path} ({result.total_bytes} bytes)",
        "Open the bundle and check it before sending it to Farnalabs support.",
    ]
    lines.extend(f"  {_withheld_sentence(entry)}" for entry in result.withheld)
    lines.extend(f"  missing {entry.get('member')}: {entry.get('reason')}" for entry in result.missing)
    return lines


def _withheld_sentence(entry: Mapping[str, Any]) -> str:
    """The one-line human record for a gate-triggered withholding."""
    member = entry.get("member")
    reason = entry.get("reason")
    if entry.get("scope") == "member":
        return f"withheld: {member} — member removed ({reason})"
    return f"withheld: {member} — {entry.get('items')} item(s) removed ({reason})"


def is_allowed_member(name: str) -> bool:
    """Layer 1: is *name* on the fixed allowlist (or a log-tail member)?"""
    if name in GOLDEN_MEMBERS:
        return True
    if not name.startswith(LOG_MEMBER_PREFIX) or not name.endswith(LOG_MEMBER_SUFFIX):
        return False
    component = name[len(LOG_MEMBER_PREFIX) : -len(LOG_MEMBER_SUFFIX)]
    return bool(component) and "/" not in component and ".." not in component


def _package_version() -> str:
    try:
        from importlib.metadata import version

        return version("farna-control")
    except Exception:  # pragma: no cover - uninstalled dev checkout
        return "unknown"


def _env_first(environ: Mapping[str, str], names: Sequence[str]) -> str:
    for name in names:
        value = environ.get(name, "").strip()
        if value:
            return value
    return ""


def build_version_text(environ: Mapping[str, str]) -> str:
    """``version.txt``: version, build commit, Python, OS, arch, clock drift."""
    drift = (time.time() - _PROCESS_START_WALL) - (time.monotonic() - _PROCESS_START_MONO)
    uptime = time.monotonic() - _PROCESS_START_MONO
    return "\n".join(
        [
            f"modulo version: {_package_version()}",
            f"build commit: {_env_first(environ, _BUILD_SHA_ENV_NAMES) or 'unknown'}",
            f"build timestamp: {_env_first(environ, _BUILD_TIMESTAMP_ENV_NAMES) or 'unknown'}",
            f"python: {sys.version.split()[0]} ({platform.python_implementation()})",
            f"os: {platform.platform()}",
            f"arch: {platform.machine()}",
            f"utc offset: {_utc_offset_text()}",
            f"clock drift: {drift:.3f}s (wall clock vs monotonic over process uptime {uptime:.0f}s)",
        ]
    )


def _utc_offset_text() -> str:
    offset = datetime.now(UTC).astimezone().utcoffset()
    if offset is None:  # pragma: no cover - astimezone always has an offset
        return "unknown"
    total = int(offset.total_seconds())
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    return f"{sign}{total // 3600:02d}:{(total % 3600) // 60:02d}"


def build_settings_text(settings: object | None, licence: dict[str, Any]) -> str:
    """``settings.txt``: full key inventory with only reviewed-safe values."""
    lines = ["# full settings key inventory; values only for the reviewed safe-list"]
    if settings is None:
        lines.append("settings unavailable: no Settings object in this process")
        return "\n".join(lines)
    fields = getattr(type(settings), "model_fields", {})
    for name in fields:
        if name in SAFE_SETTINGS_VALUE_FIELDS or name.endswith("_port"):
            try:
                value = getattr(settings, name)
            except Exception:  # pragma: no cover - defensive
                value = None
            lines.append(f"{name} = {value}")
            continue
        try:
            value = getattr(settings, name)
        except Exception:  # pragma: no cover - defensive
            value = None
        lines.append(f"{name}: {'set' if _is_set(value) else 'unset'}")
    lines.append(f"plan tier = {licence.get('tier', 'community')}")
    lines.append(f"feature flags = {', '.join(licence.get('features', [])) or '(none)'}")
    return "\n".join(lines)


def _is_set(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value)
    if isinstance(value, (list, tuple, dict, set, frozenset)):
        return bool(value)
    return True


def build_deployment_text(
    *,
    data_dir: Path | None,
    environ: Mapping[str, str],
    settings: object | None,
) -> str:
    """``deployment.txt``: shape, services, image, restarts, proxy NAMES, CA path."""
    install_shape = _determine_install_shape(data_dir)
    services = _collect_service_names(data_dir)
    image = _env_first(environ, _IMAGE_ENV_NAMES) or "unknown"
    image_digest = _env_first(environ, ("IMAGE_DIGEST", "IMAGE_ID")) or "unknown"
    restart_counts = _restart_counts(data_dir)
    proxy_names = _collect_proxy_names(environ)
    ca_bundle = _resolve_ca_bundle(environ, settings)
    lines = [
        f"install shape: {install_shape}",
        f"service names: {', '.join(services)}",
        f"image: {image}",
        f"image digest: {image_digest}",
        f"restart counts: {restart_counts}",
        f"proxy variable names: {', '.join(proxy_names) or '(none configured)'}",
        f"ca bundle path: {ca_bundle}",
    ]
    return "\n".join(lines)


def _determine_install_shape(data_dir: Path | None) -> str:
    """Determine the installation shape from the runtime environment."""
    in_container = Path("/.dockerenv").exists() or Path("/run/.containerenv").exists()
    if in_container:
        return "container"
    if data_dir is not None:
        return "native"
    return "unknown"


def _collect_service_names(data_dir: Path | None) -> list[str]:
    """Collect all service names for the deployment."""
    services = ["modulo-api", "modulo-worker"]
    if data_dir is not None:
        services.extend(_launcher_child_names(data_dir))
    return services


def _collect_proxy_names(environ: Mapping[str, str]) -> list[str]:
    """Collect proxy-related environment variable names."""
    return sorted(name for name in environ if _is_proxy_name(name))


def _resolve_ca_bundle(environ: Mapping[str, str], settings: object | None) -> str:
    """Resolve the CA bundle path from environment or settings."""
    env_value = _env_first(environ, _CA_BUNDLE_ENV_NAMES)
    if env_value:
        return env_value
    return str(getattr(settings, "ssl_cert_file", "") or "system default")


def _is_proxy_name(name: str) -> bool:
    lowered = name.lower()
    return name in _PROXY_ENV_NAMES or lowered.endswith("_proxy")


def _launcher_child_names(data_dir: Path) -> list[str]:
    try:
        from farna.launcher.supervisor import RUNTIME_FILENAME, read_runtime_manifest

        return sorted(read_runtime_manifest(data_dir / RUNTIME_FILENAME))
    except Exception:  # pragma: no cover - launcher package always present
        return []


def _restart_counts(data_dir: Path | None) -> str:
    if data_dir is None:
        return "unknown (no launcher data dir)"
    return "unknown (restart counters are not persisted by this installer)"


def build_degraded_json(data_dir: Path | None) -> str | None:
    """``degraded.json``: native-only terminal-degrade record (redacted later)."""
    if data_dir is None:
        return None
    try:
        from farna.launcher.supervisor import DEGRADED_FILENAME, read_degraded_record

        record = read_degraded_record(data_dir / DEGRADED_FILENAME)
    except Exception:  # pragma: no cover - launcher package always present
        record = None
    if record is None:
        return json.dumps({"degraded": False}, indent=2, sort_keys=True)
    return json.dumps(
        {
            "degraded": True,
            "reason": record.get("reason"),
            "backtraces": record.get("backtraces", []),
        },
        indent=2,
        sort_keys=True,
    )


def build_licence_text(settings: object | None) -> str:
    """``licence.txt``: tier/state/expiry/opaque id/features; never the key."""
    licence = resolve_licence(settings)
    return "\n".join(
        [
            f"tier: {licence['tier']}",
            f"state: {licence['state']}",
            f"expires_at: {licence['expires_at'] or '(perpetual)'}",
            f"days_until_expiry: {licence['days_until_expiry'] if licence['days_until_expiry'] is not None else 'n/a'}",
            f"licence_id: {licence['licence_id'] or '(none)'}",
            f"features: {', '.join(licence['features']) or '(none)'}",
            "license key: NEVER INCLUDED",
        ]
    )


def resolve_licence(settings: object | None) -> dict[str, Any]:
    """Resolve the effective licence claims WITHOUT ever returning the key."""
    payload: dict[str, Any] = {
        "tier": "community",
        "state": "none",
        "expires_at": None,
        "days_until_expiry": None,
        "licence_id": None,
        "features": [],
    }
    data: Any = None
    try:
        from farna.core.license import get_license, verify_license_key

        data = get_license()
        if data is None and settings is not None:
            raw_key = getattr(settings, "modulo_license_key", "") or ""
            if raw_key:
                data = verify_license_key(raw_key)
    except Exception:
        return payload
    if data is None:
        return payload
    payload["tier"] = getattr(data, "tier", "community")
    payload["features"] = list(getattr(data, "features", []) or [])
    payload["expires_at"] = getattr(data, "expires_at", None)
    payload["licence_id"] = getattr(data, "licence_id", None)
    try:
        from farna.api.routes.admin_license import derive_licence_state

        days, state = derive_licence_state(payload["expires_at"])
        payload["days_until_expiry"] = days
        payload["state"] = state
    except Exception:  # pragma: no cover - API import always available
        payload["state"] = "active"
    return payload


async def build_health_json() -> str:
    """``health.json``: check names, statuses and error codes ONLY (no detail)."""
    try:
        from farna.api.routes.health import evaluate_readiness

        result = await asyncio.wait_for(evaluate_readiness(), timeout=20)
    except Exception as exc:
        return json.dumps(
            {
                "bundle_format_version": BUNDLE_FORMAT_VERSION,
                "status": "unknown",
                "error_code": type(exc).__name__,
                "checks": [],
            },
            indent=2,
            sort_keys=True,
        )
    checks = []
    for name, check in result.checks.items():
        checks.append(
            {
                "name": name,
                "status": check.status,
                "error_code": None if check.status == "ok" else f"health.{check.status}",
            }
        )
    return json.dumps(
        {
            "bundle_format_version": BUNDLE_FORMAT_VERSION,
            "status": result.status,
            "checks": checks,
        },
        indent=2,
        sort_keys=True,
    )


async def _read_db_members(session_factory: Callable[[], Any] | None) -> _DbMembers:
    """Read ``db.txt`` / ``errors.json`` / ``runs.json``; degrade per member."""
    result = _DbMembers()
    if session_factory is None:
        for member in (DB_MEMBER, ERRORS_MEMBER, RUNS_MEMBER):
            result.missing.append({"member": member, "reason": "no database session available"})
        return result

    try:
        async with session_factory() as session:
            org_id = await _resolve_org_id(session)
            if org_id is None:
                for member in (DB_MEMBER, ERRORS_MEMBER, RUNS_MEMBER):
                    result.missing.append({"member": member, "reason": "no organisation row found"})
                return result
            result.db_text = await _read_one(session, org_id, DB_MEMBER, _build_db_text, result.missing)
            result.errors_json = await _read_one(session, org_id, ERRORS_MEMBER, _build_errors_json, result.missing)
            result.runs_json = await _read_one(session, org_id, RUNS_MEMBER, _build_runs_json, result.missing)
    except Exception as exc:
        reason = f"database unreachable: {type(exc).__name__}"
        for member in (DB_MEMBER, ERRORS_MEMBER, RUNS_MEMBER):
            result.missing.append({"member": member, "reason": reason})
        return _DbMembers(missing=result.missing)
    return result


async def _read_one(
    session: Any,
    org_id: Any,
    member: str,
    reader: Callable[[Any, Any], Any],
    missing: list[dict[str, str]],
) -> str | None:
    try:
        return await reader(session, org_id)  # type: ignore[no-any-return]
    except Exception as exc:
        missing.append({"member": member, "reason": f"read failed: {type(exc).__name__}"})
        return None


async def _resolve_org_id(session: Any) -> Any:
    from sqlalchemy import text

    async with session.begin():
        row = await session.execute(
            text(
                "SELECT id FROM organisations "
                "WHERE deleted_at IS NULL "
                "AND id NOT IN (:system_id, :registry_id) "
                "ORDER BY created_at ASC LIMIT 1"
            ),
            {
                "system_id": "00000000-0000-0000-0000-000000000000",
                "registry_id": "00000000-0000-0000-0000-000000000001",
            },
        )
        return row.scalar_one_or_none()


def _begin_org_context(session: Any, org_id: Any) -> Any:
    """Open a transaction bound to *org_id* (RLS-aware). Returns the context."""

    class _Context:
        def __init__(self, session: Any, org_id: Any) -> None:
            self._session = session
            self._org_id = org_id
            self._cm: Any = None

        async def __aenter__(self) -> Any:
            self._cm = self._session.begin()
            await self._cm.__aenter__()
            from farna.db.rls import set_rls_org

            await set_rls_org(self._session, self._org_id)
            return self._session

        async def __aexit__(self, *exc: object) -> None:
            await self._cm.__aexit__(*exc)

    return _Context(session, org_id)


async def _build_db_text(session: Any, org_id: Any) -> str:
    from sqlalchemy import text

    async with _begin_org_context(session, org_id):
        head_row = await session.execute(text("SELECT version_num FROM alembic_version"))
        current_head = ", ".join(sorted(str(row[0]) for row in head_row.fetchall())) or "unknown"
        pg_row = await session.execute(text("SELECT current_setting('server_version')"))
        pg_version = str(pg_row.scalar_one_or_none() or "unknown")
        counts: list[str] = []
        for table, statement in sorted(_TABLE_ROW_COUNT_QUERIES.items()):
            row = await session.execute(text(statement))
            counts.append(f"  {table}: {int(row.scalar_one())}")
    return "\n".join(
        [
            f"migration head: {current_head}",
            f"expected migration head: {_expected_migration_head()}",
            f"pg version: {pg_version}",
            "row counts:",
            *counts,
        ]
    )


def _expected_migration_head() -> str:
    try:
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        from farna.db.health_checks import resolve_alembic_ini, resolve_migration_script_location

        ini = resolve_alembic_ini()
        config = Config(str(ini))
        config.set_main_option("script_location", str(resolve_migration_script_location(ini)))
        head = ScriptDirectory.from_config(config).get_current_head()
    except Exception:
        return "unknown"
    return str(head) if head else "unknown"


async def _build_errors_json(session: Any, org_id: Any) -> str:
    from sqlalchemy import text

    async with _begin_org_context(session, org_id):
        rows = await session.execute(
            text(
                "SELECT g.id, g.fingerprint, g.level_peak, g.count, g.first_seen, g.last_seen, "
                "e.level, e.source, e.environment, e.version, e.message "
                "FROM error_groups g "
                "LEFT JOIN error_events e ON e.id = g.sample_event_id "
                "WHERE g.organisation_id = :oid "
                "ORDER BY g.count DESC, g.last_seen DESC LIMIT :limit"
            ),
            {"oid": str(org_id), "limit": MAX_ERROR_GROUPS},
        )
        payload = [_error_group_row(dict(row._mapping)) for row in rows]
    return json.dumps(
        {"bundle_format_version": BUNDLE_FORMAT_VERSION, "error_groups": payload},
        indent=2,
        sort_keys=True,
        default=str,
    )


def _error_group_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row.get("id"),
        "fingerprint": row.get("fingerprint"),
        "level": row.get("level") or row.get("level_peak"),
        "source": row.get("source"),
        "count": row.get("count"),
        "first_seen": row.get("first_seen"),
        "last_seen": row.get("last_seen"),
        "environment": row.get("environment"),
        "version": row.get("version"),
        "message": _sanitize_message(row.get("message")),
    }


def _sanitize_message(message: Any) -> str:
    """Sanitize the human message with the shared value patterns (layer 4)."""
    from farna.core.secret_patterns import mask_secret_values_in_text

    if not isinstance(message, str):
        return ""
    return mask_secret_values_in_text(message[:2000])


async def _build_runs_json(session: Any, org_id: Any) -> str:
    from sqlalchemy import bindparam, text

    statement = text(
        "SELECT r.id, r.run_number, r.pipeline_id, p.name AS pipeline_name, r.status, "
        "r.error_code, r.started_at, r.completed_at, r.created_at, r.total_cost_usd "
        "FROM runs r LEFT JOIN pipelines p ON p.id = r.pipeline_id "
        "WHERE r.organisation_id = :oid AND r.status IN :statuses "
        "ORDER BY r.created_at DESC LIMIT :limit"
    ).bindparams(bindparam("statuses", expanding=True))
    async with _begin_org_context(session, org_id):
        rows = await session.execute(
            statement,
            {
                "oid": str(org_id),
                "statuses": list(_FAILED_RUN_STATUSES),
                "limit": MAX_FAILED_RUNS,
            },
        )
        payload = [
            {
                "run_id": row.id,
                "run_number": row.run_number,
                "pipeline_id": row.pipeline_id,
                "pipeline_name": row.pipeline_name,
                "status": row.status,
                "error_code": row.error_code,
                "started_at": row.started_at,
                "completed_at": row.completed_at,
                "created_at": row.created_at,
                "total_cost_usd": row.total_cost_usd,
            }
            for row in rows
        ]
    return json.dumps(
        {"bundle_format_version": BUNDLE_FORMAT_VERSION, "runs": payload},
        indent=2,
        sort_keys=True,
        default=str,
    )


def collapse_repeated_lines(text: str, *, max_lines: int = MAX_LOG_LINES) -> tuple[str, int]:
    """Collapse consecutive identical lines and bound the total line count.

    A crash loop writes the same line thousands of times; one copy plus a
    repetition marker carries the same diagnostic signal in bounded space.
    Returns ``(collapsed_text, removed_line_count)``.
    """
    collapsed: list[str] = []
    previous: str | None = None
    run = 0
    removed = 0

    def flush() -> None:
        nonlocal removed
        if run > 1:
            removed += run - 1
            collapsed.append(f"... [previous line repeated {run - 1} more time(s)]")

    for line in text.splitlines():
        if line == previous:
            run += 1
            continue
        flush()
        collapsed.append(line)
        previous = line
        run = 1
    flush()
    if len(collapsed) > max_lines:
        dropped = len(collapsed) - max_lines
        removed += dropped
        collapsed = [f"... [truncated {dropped} lines]", *collapsed[-max_lines:]]
    return "\n".join(collapsed), removed


def _read_log_members(data_dir: Path | None, max_log_bytes: int) -> dict[str, str]:
    if data_dir is None:
        return {}
    try:
        from farna.launcher.supervisor import log_paths, read_log_tail

        paths = log_paths(data_dir)
    except Exception:  # pragma: no cover - launcher package always present
        return {}
    members: dict[str, str] = {}
    for component in sorted(paths):
        raw = read_log_tail(paths[component], max_bytes=max_log_bytes)
        collapsed, _ = collapse_repeated_lines(raw)
        members[f"{LOG_MEMBER_PREFIX}{component}{LOG_MEMBER_SUFFIX}"] = collapsed
    return members


def _render_report(
    *,
    ref: str,
    sizes: Mapping[str, int],
    redaction_counts: Mapping[str, int],
    missing: Sequence[Mapping[str, str]],
    truncated: Sequence[str],
    report_size: int,
    manifest_size: int,
    withheld: Sequence[Mapping[str, Any]] = (),
) -> str:
    members = [
        {"name": name, "bytes": size}
        for name, size in sorted(sizes.items())
        if name not in {MANIFEST_MEMBER, REPORT_MEMBER}
    ]
    members.append({"name": MANIFEST_MEMBER, "bytes": manifest_size})
    members.append({"name": REPORT_MEMBER, "bytes": report_size})
    members.sort(key=lambda entry: str(entry["name"]))
    payload = {
        "bundle_format_version": BUNDLE_FORMAT_VERSION,
        "reference": bundle_reference(),
        "ref": ref,
        "generated_at": datetime.now(UTC).isoformat(),
        "generated_by": "modulo support bundle",
        "redacted_marker": REDACTED,
        "members": members,
        "redaction": dict(redaction_counts),
        "missing": list(missing),
        "truncated_log_members": list(truncated),
        "withheld": list(withheld),
        "self_referential_bytes": [MANIFEST_MEMBER, REPORT_MEMBER],
    }
    return json.dumps(payload, indent=2, sort_keys=True)


def _render_manifest(
    *,
    ref: str,
    sizes: Mapping[str, int],
    redaction_counts: Mapping[str, int],
    missing: Sequence[Mapping[str, str]],
    truncated: Sequence[str],
    report_size: int,
    manifest_size: int,
    withheld: Sequence[Mapping[str, Any]] = (),
) -> str:
    lines = [
        "modulo support bundle",
        f"bundle_format_version: {BUNDLE_FORMAT_VERSION}",
        f"reference: {bundle_reference()}",
        f"ref: {ref}",
        f"generated_at: {datetime.now(UTC).isoformat()}",
        f"redacted_marker: {REDACTED}",
        "",
        "members (name | bytes | carries):",
    ]
    all_members = set(sizes) | {MANIFEST_MEMBER, REPORT_MEMBER}
    for name in sorted(all_members):
        size = {MANIFEST_MEMBER: manifest_size, REPORT_MEMBER: report_size}.get(name, sizes.get(name, 0))
        purpose = _MEMBER_PURPOSE.get(name, "log tail (repeated lines collapsed, capped)")
        lines.append(f"  {name} | {size} | {purpose}")
    lines.extend(
        [
            "",
            "redaction substitution counts per layer:",
            f"  {LAYER_EXACT}: {redaction_counts.get(LAYER_EXACT, 0)}",
            f"  {LAYER_STRUCTURAL}: {redaction_counts.get(LAYER_STRUCTURAL, 0)}",
            f"  {LAYER_PATTERNS}: {redaction_counts.get(LAYER_PATTERNS, 0)}",
            "",
            "truncated_log_members: " + (", ".join(truncated) if truncated else "(none)"),
            "",
            "withheld:",
        ]
    )
    if withheld:
        lines.extend(f"  {_withheld_sentence(entry)}" for entry in withheld)
    else:
        lines.append("  (none)")
    lines.extend(["", "missing:"])
    if missing:
        lines.extend(f"  {entry.get('member')}: {entry.get('reason')}" for entry in missing)
    else:
        lines.append("  (none)")
    return "\n".join(lines)


def _render_meta(
    *,
    ref: str,
    sizes: Mapping[str, int],
    redaction_counts: Mapping[str, int],
    missing: Sequence[Mapping[str, str]],
    truncated: Sequence[str],
    withheld: Sequence[Mapping[str, Any]] = (),
) -> tuple[str, str]:
    """Stabilise the self-referential byte counts for MANIFEST/report."""
    report_size = 0
    manifest_size = 0
    for _ in range(8):
        report_text = _render_report(
            ref=ref,
            sizes=sizes,
            redaction_counts=redaction_counts,
            missing=missing,
            truncated=truncated,
            report_size=report_size,
            manifest_size=manifest_size,
            withheld=withheld,
        )
        new_report = len(report_text.encode("utf-8"))
        manifest_text = _render_manifest(
            ref=ref,
            sizes=sizes,
            redaction_counts=redaction_counts,
            missing=missing,
            truncated=truncated,
            report_size=new_report,
            manifest_size=manifest_size,
            withheld=withheld,
        )
        new_manifest = len(manifest_text.encode("utf-8"))
        if new_report == report_size and new_manifest == manifest_size:
            break
        report_size, manifest_size = new_report, new_manifest
    return report_text, manifest_text


def _zip_bytes(members: Mapping[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(members):
            archive.writestr(name, members[name].encode("utf-8"))
    return buffer.getvalue()


def _assemble(
    members: Mapping[str, str],
    *,
    ref: str,
    redaction_counts: Mapping[str, int],
    missing: Sequence[Mapping[str, str]],
    truncated: Sequence[str],
    withheld: Sequence[Mapping[str, Any]] = (),
) -> tuple[dict[str, str], bytes]:
    meta = {MANIFEST_MEMBER, REPORT_MEMBER}
    sizes = {name: len(text.encode("utf-8")) for name, text in members.items() if name not in meta}
    report_text, manifest_text = _render_meta(
        ref=ref,
        sizes=sizes,
        redaction_counts=redaction_counts,
        missing=missing,
        truncated=truncated,
        withheld=withheld,
    )
    final = dict(members)
    final[REPORT_MEMBER] = report_text
    final[MANIFEST_MEMBER] = manifest_text
    return final, _zip_bytes(final)


def build_bundle(
    out_path: Path,
    *,
    sources: BundleSources | None = None,
    ref: str | None = None,
) -> BundleResult:
    """Build the support bundle synchronously (CLI + ``doctor --report``).

    *ref* lets a caller (``modulo support``) mint the per-run reference FIRST so
    the default output filename can carry it; omit it and one is generated.
    """
    return asyncio.run(build_bundle_async(out_path, sources=sources, ref=ref))


async def build_bundle_async(
    out_path: Path,
    *,
    sources: BundleSources | None = None,
    ref: str | None = None,
) -> BundleResult:
    """Build the support bundle, or raise :class:`RedactionGateError`.

    The zip is written ONLY after the residual scan runs, so a refused build
    leaves no partial artefact behind. A member that trips the layer-5 gate
    loses only the offending item (or, for a small invariant member, the member
    itself) and the rest of the bundle is still written (FAR-2333); every
    withholding is recorded in ``MANIFEST.txt`` and ``report.json``.
    """
    bundle_ref = ref or generate_ref()
    resolved = sources if sources is not None else BundleSources()
    environ = resolved.resolved_environ()
    settings = _resolve_settings(resolved)

    missing: list[dict[str, str]] = []

    licence = resolve_licence(settings)
    raw_members = await _collect_raw_members(resolved, environ, settings, licence, missing)

    _assert_allowlist(raw_members)

    seeds = collect_secret_values(
        data_dir=resolved.data_dir,
        settings=settings,
        extra=resolved.extra_secrets,
    )
    redactor = Redactor(seeds)

    redacted, counts = _redact_all_members(raw_members, redactor)

    # The ref is a token THIS build minted, so the entropy heuristic must not
    # refuse our own report/MANIFEST text. Only that exact token is exempt.
    allowed_tokens = frozenset({bundle_ref})
    redacted, withheld = _withhold_tripping_items(redacted, redactor, allowed_tokens)
    final, archive, truncated = _assemble_and_gate(
        redacted,
        bundle_ref,
        counts,
        missing,
        withheld,
        resolved.max_bundle_bytes,
        redactor,
        allowed_tokens,
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    _write_atomically(out_path, archive)

    sizes = {name: len(final[name].encode("utf-8")) for name in final}
    ordered = tuple(sorted(sizes.items()))
    return BundleResult(
        bundle_path=out_path,
        members=ordered,
        total_bytes=len(archive),
        redaction_counts=dict(counts),
        missing=tuple(missing),
        truncated_log_members=tuple(truncated),
        ref=bundle_ref,
        withheld=tuple(withheld),
    )


def _resolve_settings(resolved: BundleSources) -> object | None:
    """Resolve the Settings object from sources or the global getter."""
    settings: object | None = resolved.settings
    if settings is None:
        try:
            from farna.settings import get_settings

            settings = get_settings()
        except Exception:
            settings = None
    return settings


async def _collect_raw_members(
    resolved: BundleSources,
    environ: Mapping[str, str],
    settings: object | None,
    licence: dict[str, Any],
    missing: list[dict[str, str]],
) -> dict[str, str]:
    """Collect all raw (pre-redaction) bundle members."""
    raw_members: dict[str, str] = {
        VERSION_MEMBER: build_version_text(environ),
        DOCTOR_MEMBER: _doctor_member_text(resolved),
        HEALTH_MEMBER: await build_health_json(),
        SETTINGS_MEMBER: build_settings_text(settings, licence),
        DEPLOYMENT_MEMBER: build_deployment_text(data_dir=resolved.data_dir, environ=environ, settings=settings),
        LICENCE_MEMBER: build_licence_text(settings),
    }
    _maybe_add_doctor_missing(resolved, missing)

    degraded = build_degraded_json(resolved.data_dir)
    _handle_degraded_member(degraded, raw_members, missing)

    await _add_db_members(resolved, raw_members, missing)

    log_members = _read_log_members(resolved.data_dir, resolved.max_log_bytes)
    _handle_log_members(log_members, raw_members, missing)

    return raw_members


def _doctor_member_text(resolved: BundleSources) -> str:
    """Return the doctor member text or a placeholder."""
    if resolved.doctor_output is not None:
        return resolved.doctor_output
    return "doctor output unavailable: no launcher data dir (container install)"


def _maybe_add_doctor_missing(resolved: BundleSources, missing: list[dict[str, str]]) -> None:
    """Add doctor member to missing list if no output was captured."""
    if resolved.doctor_output is None:
        reason = (
            "no launcher data dir; doctor checks are native-install only"
            if resolved.data_dir is None
            else "doctor output was not captured by this entry point"
        )
        missing.append({"member": DOCTOR_MEMBER, "reason": reason})


def _handle_degraded_member(
    degraded: str | None,
    raw_members: dict[str, str],
    missing: list[dict[str, str]],
) -> None:
    """Add degraded member to raw_members or missing list."""
    if degraded is None:
        missing.append({"member": DEGRADED_MEMBER, "reason": "native launcher only; not applicable in a container"})
    else:
        raw_members[DEGRADED_MEMBER] = degraded


async def _add_db_members(
    resolved: BundleSources,
    raw_members: dict[str, str],
    missing: list[dict[str, str]],
) -> None:
    """Add database-backed members to raw_members and missing list."""
    db_members = await _read_db_members(_session_factory(resolved))
    missing.extend(db_members.missing)
    if db_members.db_text is not None:
        raw_members[DB_MEMBER] = db_members.db_text
    if db_members.errors_json is not None:
        raw_members[ERRORS_MEMBER] = db_members.errors_json
    if db_members.runs_json is not None:
        raw_members[RUNS_MEMBER] = db_members.runs_json


def _handle_log_members(
    log_members: dict[str, str],
    raw_members: dict[str, str],
    missing: list[dict[str, str]],
) -> None:
    """Add log members to raw_members or record as missing."""
    if not log_members:
        missing.append(
            {"member": f"{LOG_MEMBER_PREFIX}*{LOG_MEMBER_SUFFIX}", "reason": "no launcher data dir (container install)"}
        )
    raw_members.update(log_members)


def _redact_all_members(raw_members: dict[str, str], redactor: Redactor) -> tuple[dict[str, str], dict[str, int]]:
    """Redact all members and aggregate per-layer counts."""
    redacted: dict[str, str] = {}
    counts: dict[str, int] = {LAYER_EXACT: 0, LAYER_STRUCTURAL: 0, LAYER_PATTERNS: 0}
    for name in sorted(raw_members):
        outcome = redactor.redact(raw_members[name])
        redacted[name] = outcome.text
        for layer, count in outcome.counts.items():
            counts[layer] = counts.get(layer, 0) + count
    return redacted, counts


def _withhold_tripping_items(
    members: Mapping[str, str],
    redactor: Redactor,
    allowed_tokens: frozenset[str],
) -> tuple[dict[str, str], list[dict[str, Any]]]:
    """Drop only what the layer-5 gate objects to (FAR-2333).

    A collection member loses the offending error group / run row / log line;
    a small invariant member is withheld whole. Content is removed BEFORE
    assembly, so the offending text can never reach the written zip.

    A trip that cannot be attributed to an item — an unexpected member shape —
    is still a hard refusal naming the member: withholding must never become a
    silent loss of evidence.
    """
    kept: dict[str, str] = {}
    withheld: list[dict[str, Any]] = []
    for name in sorted(members):
        text = members[name]
        reasons = redactor.gate_reasons(text, allowed_tokens=allowed_tokens)
        if not reasons:
            kept[name] = text
            continue
        reason = "; ".join(reasons)
        if name in _MEMBER_LEVEL_WITHHOLD_MEMBERS:
            withheld.append({"member": name, "items": 1, "reason": reason, "scope": "member"})
            continue
        reduced = _without_tripping_items(name, text, redactor, allowed_tokens)
        if reduced is None:
            raise RedactionGateError(name, reasons)
        survivors, dropped = reduced
        withheld.append({"member": name, "items": dropped, "reason": reason, "scope": "item"})
        if survivors:
            kept[name] = survivors
    return kept, withheld


def _without_tripping_items(
    name: str,
    text: str,
    redactor: Redactor,
    allowed_tokens: frozenset[str],
) -> tuple[str, int] | None:
    """Return ``(surviving text, items withheld)``, or ``None`` when unattributable.

    ``None`` means the member's shape is not one this build knows how to
    decompose (or the survivors still trip), so the caller refuses the build.
    """
    if name in _JSON_ITEM_MEMBER_KEYS:
        return _without_tripping_json_items(name, text, redactor, allowed_tokens)
    if _is_log_member(name):
        return _without_tripping_log_lines(text, redactor, allowed_tokens)
    return None


def _without_tripping_json_items(
    name: str,
    text: str,
    redactor: Redactor,
    allowed_tokens: frozenset[str],
) -> tuple[str, int] | None:
    key = _JSON_ITEM_MEMBER_KEYS[name]
    try:
        payload = json.loads(text)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    items = payload.get(key)
    if not isinstance(items, list):
        return None
    survivors = [item for item in items if not _item_trips(item, redactor, allowed_tokens)]
    dropped = len(items) - len(survivors)
    if not dropped:
        return None
    if not survivors:
        return "", dropped
    payload[key] = survivors
    rebuilt = json.dumps(payload, indent=2, sort_keys=True, default=str)
    if redactor.gate_reasons(rebuilt, allowed_tokens=allowed_tokens):
        return None
    return rebuilt, dropped


def _without_tripping_log_lines(
    text: str,
    redactor: Redactor,
    allowed_tokens: frozenset[str],
) -> tuple[str, int] | None:
    lines = text.splitlines()
    survivors = [line for line in lines if not redactor.gate_reasons(line, allowed_tokens=allowed_tokens)]
    dropped = len(lines) - len(survivors)
    if not dropped:
        return None
    rebuilt = "\n".join(survivors)
    if survivors and redactor.gate_reasons(rebuilt, allowed_tokens=allowed_tokens):
        return None
    return rebuilt, dropped


def _item_trips(item: Any, redactor: Redactor, allowed_tokens: frozenset[str]) -> bool:
    item_text = json.dumps(item, indent=2, sort_keys=True, default=str)
    return bool(redactor.gate_reasons(item_text, allowed_tokens=allowed_tokens))


def _is_log_member(name: str) -> bool:
    return name.startswith(LOG_MEMBER_PREFIX) and name.endswith(LOG_MEMBER_SUFFIX)


def _assemble_and_gate(
    redacted: dict[str, str],
    bundle_ref: str,
    counts: dict[str, int],
    missing: list[dict[str, str]],
    withheld: list[dict[str, Any]],
    max_bundle_bytes: int,
    redactor: Redactor,
    allowed_tokens: frozenset[str],
) -> tuple[dict[str, str], bytes, list[str]]:
    """Assemble the bundle, run the gate, and shrink logs until under budget."""
    truncated: list[str] = []
    final, archive = _assemble(
        redacted,
        ref=bundle_ref,
        redaction_counts=counts,
        missing=missing,
        truncated=(),
        withheld=withheld,
    )

    for name in final:
        redactor.gate(name, final[name], allowed_tokens=allowed_tokens)

    while len(archive) > max_bundle_bytes and _has_shrinkable_log(redacted):
        redacted = _shrink_logs(redacted)
        truncated = [name for name in redacted if name.startswith(LOG_MEMBER_PREFIX)]
        final, archive = _assemble(
            redacted,
            ref=bundle_ref,
            redaction_counts=counts,
            missing=missing,
            truncated=truncated,
            withheld=withheld,
        )
        for name in final:
            redactor.gate(name, final[name], allowed_tokens=allowed_tokens)

    if len(archive) > max_bundle_bytes:
        raise BundleTooLargeError(
            f"bundle is {len(archive)} bytes after truncating every log member (cap {max_bundle_bytes})"
        )

    return final, archive, truncated


def _session_factory(sources: BundleSources) -> Callable[[], Any] | None:
    if not sources.enable_db:
        return None
    if sources.session_factory is not None:
        return sources.session_factory
    try:
        from farna.db.session import AsyncSessionLocal

        return AsyncSessionLocal
    except Exception:
        return None


def _assert_allowlist(members: Mapping[str, str]) -> None:
    for name in members:
        if not is_allowed_member(name):
            raise RedactionGateError(name, ["member is not on the fixed allowlist (layer 1)"])


def _has_shrinkable_log(members: Mapping[str, str]) -> bool:
    return any(name.startswith(LOG_MEMBER_PREFIX) and len(text) > 8 for name, text in members.items())


def _shrink_logs(members: Mapping[str, str]) -> dict[str, str]:
    shrunk = dict(members)
    for name, text in members.items():
        if not name.startswith(LOG_MEMBER_PREFIX):
            continue
        if len(text) <= 16:
            shrunk[name] = ""
            continue
        shrunk[name] = text[: max(1, len(text) // 2)]
    return shrunk


def _write_atomically(out_path: Path, data: bytes) -> None:
    # ``out_path`` is the operator-chosen destination of a LOCAL command:
    # ``modulo support --out PATH``, ``python -m modulo.support.bundle --out
    # PATH`` or ``modulo doctor --report PATH``. It is never supplied over HTTP
    # or by a model, and the process runs as the same operator that chose the
    # path, so writing there crosses no privilege boundary and the S8707 taint
    # flow from argparse argv is a false positive (same reasoning as the
    # trusted ``--data-dir`` path in modulo.launcher.upgrade).
    tmp_path = out_path.with_name(f"{out_path.name}.tmp-{os.getpid()}")
    with tmp_path.open("wb") as handle:
        handle.write(data)
    tmp_path.replace(out_path)  # NOSONAR pythonsecurity:S8707 - local operator-chosen --out path, not HTTP/model input


def read_bundle(path: Path) -> dict[str, bytes]:
    """Read every member of an existing bundle.

    Deliberately tolerant of UNKNOWN members: forward compatibility means an
    older reader can still open a newer bundle. The reader never validates the
    member set, it just returns what the archive holds.
    """
    with zipfile.ZipFile(path) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def inspect_bundle(path: Path) -> BundleResult:
    """Read a bundle's inventory without re-running the redaction gate."""
    members = read_bundle(path)
    counts: dict[str, int] = {}
    missing: list[dict[str, str]] = []
    truncated: list[str] = []
    withheld: list[dict[str, Any]] = []
    ref = ""
    report_raw = members.get(REPORT_MEMBER)
    if report_raw is not None:
        try:
            report = json.loads(report_raw)
            counts = dict(report.get("redaction", {}))
            missing = list(report.get("missing", []))
            truncated = list(report.get("truncated_log_members", []))
            withheld = list(report.get("withheld", []))
            ref = str(report.get("ref") or "")
        except ValueError:
            pass
    return BundleResult(
        bundle_path=path,
        members=tuple(sorted((name, len(data)) for name, data in members.items())),
        total_bytes=path.stat().st_size,
        redaction_counts=counts,
        missing=tuple(missing),
        truncated_log_members=tuple(truncated),
        ref=ref,
        withheld=tuple(withheld),
    )


def _bundle_ref_tokens(members: Mapping[str, bytes]) -> frozenset[str]:
    """The bundle's own ref, so ``--show`` accepts the text that carries it."""
    report_raw = members.get(REPORT_MEMBER)
    if report_raw is None:
        return frozenset()
    try:
        ref = json.loads(report_raw).get("ref")
    except ValueError:
        return frozenset()
    return frozenset({ref}) if isinstance(ref, str) and ref else frozenset()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m modulo.support.bundle",
        description="Build, inspect, or text-redact a Modulo support bundle.",
        epilog=(
            "--redact-stdin reads UTF-8 text on stdin and writes the redacted text on stdout, "
            "using the same redaction layers and fail-closed gate as a bundle build. It exits "
            "non-zero, writing nothing on stdout, when the gate finds an unclassified "
            "high-entropy token."
        ),
    )
    parser.add_argument("--out", type=Path, default=None, metavar="PATH", help="Bundle zip path.")
    parser.add_argument("--json", action="store_true", help="Print one JSON object (ref + path).")
    parser.add_argument("--list", action="store_true", help="List members and sizes of the bundle.")
    parser.add_argument("--show", metavar="MEMBER", help="Print ONE redacted member.")
    parser.add_argument("--data-dir", type=Path, default=None, help="Native launcher data dir (optional).")
    parser.add_argument(
        "--redact-stdin",
        action="store_true",
        help="Read text on stdin, write redacted text on stdout (no bundle written).",
    )
    return parser


def _emit(text: str, *, stream: Any = None) -> None:
    """Write one line without ``print`` (the lint rule bans it in src)."""
    target = stream if stream is not None else sys.stdout
    target.write(text + "\n")


def _redact_stdin() -> int:
    """Text-only mode: layers 2-4 plus the layer-5 gate, reading stdin.

    Reuses the SAME scrubbers and gate as a bundle build, so the Compose
    collector never reimplements redaction. Nothing is written on stdout when
    the gate refuses: fail closed, exit non-zero.
    """
    text = sys.stdin.buffer.read().decode("utf-8", errors="replace")
    settings: object | None
    try:
        from farna.settings import get_settings

        settings = get_settings()
    except Exception:  # pragma: no cover - settings are optional for text mode
        settings = None
    redactor = Redactor(collect_secret_values(data_dir=None, settings=settings))
    redacted_text = redactor.redact(text).text
    # The structural layer rebuilds the text line-by-line and therefore drops a
    # trailing newline; restore it so a clean stream round-trips byte-for-byte
    # (a collector may concatenate several of these).
    if text.endswith("\n") and not redacted_text.endswith("\n"):
        redacted_text += "\n"
    try:
        redactor.gate("<stdin>", redacted_text)
    except RedactionGateError as exc:
        _emit(f"refused: redaction gate refused stdin: {'; '.join(exc.reasons)}", stream=sys.stderr)
        _emit("nothing written", stream=sys.stderr)
        return 2
    sys.stdout.write(redacted_text)
    sys.stdout.flush()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.redact_stdin:
        validation_error = _validate_redact_stdin_args(args)
        if validation_error is not None:
            _emit(validation_error, stream=sys.stderr)
            return 2
        return _redact_stdin()

    validation_error = _validate_build_args(args)
    if validation_error is not None:
        _emit(validation_error, stream=sys.stderr)
        return 2

    build_result = _run_build_or_inspect(args)
    if isinstance(build_result, int):
        return build_result
    result = build_result

    if args.list:
        return _print_member_list(result)

    if args.show:
        return _show_member(args.out, args.show)

    if args.json:
        _emit(json.dumps(result.to_json_dict(), sort_keys=True))
    else:
        for line in summary_lines(result):
            _emit(line)
    return 0


def _validate_redact_stdin_args(args: Any) -> str | None:
    if args.out is not None or args.list or args.show or args.json or args.data_dir is not None:
        return "error: --redact-stdin cannot be combined with --out/--list/--show/--json/--data-dir"
    return None


def _validate_build_args(args: Any) -> str | None:
    if args.out is None:
        return "error: --out is required unless --redact-stdin is given"
    if args.list and args.show:
        return "error: --list and --show are mutually exclusive"
    return None


def _run_build_or_inspect(args: Any) -> BundleResult | int:
    read_mode = (args.list or args.show) and args.out.is_file()
    if read_mode:
        return inspect_bundle(args.out)
    try:
        return asyncio.run(
            build_bundle_async(
                args.out,
                sources=BundleSources(data_dir=args.data_dir),
            )
        )
    except RedactionGateError as exc:
        _emit(
            f"refused: redaction gate refused member {exc.member!r}: {'; '.join(exc.reasons)}",
            stream=sys.stderr,
        )
        _emit("no bundle written", stream=sys.stderr)
        return 2
    except (BundleTooLargeError, OSError) as exc:
        _emit(f"error: could not write bundle: {exc}", stream=sys.stderr)
        return 1


def _print_member_list(result: BundleResult) -> int:
    _emit(f"bundle_format_version: {result.bundle_format_version}")
    for name, size in result.members:
        _emit(f"{name}\t{size} bytes")
    return 0


def _show_member(bundle_path: Path, member: str) -> int:
    try:
        members = read_bundle(bundle_path)
    except (OSError, zipfile.BadZipFile) as exc:
        _emit(f"error: could not read bundle: {exc}", stream=sys.stderr)
        return 1
    if member not in members:
        _emit(f"error: bundle has no member {member!r}", stream=sys.stderr)
        return 1
    text = members[member].decode("utf-8", errors="replace")
    # Defence in depth: never print a member that would fail the gate, even
    # though a written bundle already passed it. The bundle's OWN ref is
    # exempt, exactly as it was at assembly time.
    try:
        Redactor({}).gate(member, text, allowed_tokens=_bundle_ref_tokens(members))
    except RedactionGateError as exc:
        _emit(f"refused: {exc}", stream=sys.stderr)
        return 2
    _emit(text)
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
