"""``modulo doctor`` (FAR-671 core checks + FAR-676 full doctor).

Every check is a PURE function over an INJECTED probe interface
(:class:`DoctorProbes`) so every pass/fail path is unit-testable without
services. Probe exceptions are converted into failed checks with the detail
attached — the doctor never exits through an unhandled exception.

EXIT-CODE TABLE (documented contract; locked by the exhaustiveness test in
``tests/unit/launcher/test_doctor.py``; every code has a deterministic,
CI-automatable fault recipe):

===  ===========================  ===========================================
code  meaning                      recipe (fault injection, no live services)
===  ===========================  ===========================================
0    healthy — all checks pass    default probes back the happy path
1    unhealthy — a check failed   e.g. redis stopped (``probe_redis`` raises)
2    degraded — nothing failed    e.g. stale backup older than 7d or ambient
     but at least one WARNING     PG* env vars (``warning``-severity results)
3    uninitialized — the data     no secrets.json / state.json in the data dir
     dir is not initialized
===  ===========================  ===========================================

Real-machine-only recipes (not CI-reproducible) are annotated inline on the
corresponding check, never silently.

Core checks (FAR-671):

1. ``data-dir`` — writable + quantified free-disk floor
   (:data:`MIN_FREE_DISK_BYTES`).
2. ``ports`` — loopback bind assertions for the configured PG/Redis/API
   ports from state.json, actual listeners vs configured (only when the
   launcher is running; skipped honestly otherwise).
3. ``postgres`` — reachable + bootstrap-role posture.
4. ``redis`` — ping + auth on loopback.
5. ``migrations`` — current at the alembic head.
6. ``env-influence`` — a CWD ``.env`` must not be able to steer Settings.
7. ``privileges`` — root/sudo refusal + data-dir ownership mismatch.

Full-doctor checks (FAR-676):

8.  ``state-integrity`` — state.json HMAC verification with DISTINCT corrupt
    (unreadable/torn) vs HMAC-mismatch (tampered/foreign-key) reporting.
9.  ``secrets-permissions`` — the secrets file must be 0600 owner-only
    (TODO(P3): Windows ACL seam reports an honest skip).
10. ``ambient-pg-env`` — WARNING: ``PG*``/``DATABASE_URL``/``REDIS_URL``
    variables in the inherited environment could hijack bundled-binary
    invocations (the launcher scrubs them at boot; report so the operator
    knows where they came from).
11. ``settings-source`` — ``MODULO_DB`` must not point away from postgres,
    and an ambient DATABASE_URL that conflicts with state.json's port is a
    WARNING (see the exit-table docstring for the documented override).
12. ``cloud-sync-root`` — WARNING: the data dir sits under a cloud-sync
    vendor folder (Dropbox/OneDrive/Drive/...) — vendor component matching.
13. ``service`` — when service-installed: unit enabled + linger active
    (report "service not installed" honestly rather than fail; FAR-674's
    service.py may not exist yet).
14. ``memory`` — available memory vs the documented envelope
    (:data:`MIN_AVAILABLE_MEMORY_BYTES` hard floor,
    :data:`COMFORT_MEMORY_BYTES` warn band).
15. ``bundle-version`` — data-dir PG_VERSION vs the bundled binary
    (bundle-minor drift) and vs the last-run persisted bundle version
    (older-binary refusal / newer-binary upgrade hint; persisted via the
    supervisor's runtime manifest ``extra`` bookkeeping).
16. ``binaries`` — AV-block detection: a bundled binary that is missing,
    zero-byte, or not executable.
17. ``port-collisions`` — compose coexistence + system PG/Redis service
    detection with port-collision attribution.
18. ``install-shadows`` — second native install detection + PATH shadowing
    (first ``modulo`` on PATH vs the expected install root).
19. ``degraded`` — surfacing the supervisor's persisted degraded flag.
20. ``tls`` — TLS keypair near-expiry check when one exists in the data dir.
21. ``stale-backup`` — WARNING when the last recorded backup is older than
    :data:`STALE_BACKUP_SECONDS` (schema v1 records none: honest skip).

``--fix`` (via :func:`apply_fixes`): orphan cleanup — a stale
``postmaster.pid`` / stale per-boot Redis confs / initdb temp debris (the
supervisor's boot-time reconciliation, refusing a LIVE postgres) — plus
port re-assignment guidance printed after the sweep.

Every predicate reused from the boot path (“doctor and boot must never
disagree”): ``bootstrap_role._find_allow_list_violations``,
``health_checks.db_is_at_migration_head``, ``env_safety``, the supervisor
lock/holder/manifest readers. TODO(P3): Windows privilege/ownership/ACL
probes (uid/pwd/flock are POSIX) — reported as honest skips, never silent
passes.
"""

import asyncio
import logging
import os
import shutil
import socket
import struct
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from modulo.launcher.entry import PGDATA_DIRNAME

_log = logging.getLogger(__name__)

# Quantified disk floor for a healthy single-install data dir.
MIN_FREE_DISK_BYTES = 512 * 1024 * 1024

# Documented memory envelope for a healthy single install (bundled
# Postgres + Redis + API + SAQ workers): below 1 GiB available the bundled
# stack cannot run (hard failure), below 2 GiB is a warn band.
MIN_AVAILABLE_MEMORY_BYTES = 1 * 1024 * 1024 * 1024
COMFORT_MEMORY_BYTES = 2 * 1024 * 1024 * 1024

# A recorded backup older than this is a stale-backup WARNING (default 7d).
STALE_BACKUP_SECONDS = 7 * 86400

# TLS keypair near-expiry warn window.
TLS_NEAR_EXPIRY_SECONDS = 30 * 86400

# Cloud-sync vendors whose folders must never host the bundled data dir
# (case-insensitive substring match over the data dir and its ancestors —
# the same "walk up + vendor set component matching" shape the backup path
# uses for export-dir vetting).
CLOUD_SYNC_VENDOR_MARKERS: tuple[str, ...] = (
    "dropbox",
    "onedrive",
    "google drive",
    "icloud",
    "box sync",
    "google_drive",
)

# Documented exit codes for ``modulo doctor`` (see the module docstring).
EXIT_HEALTHY = 0
EXIT_UNHEALTHY = 1
EXIT_DEGRADED = 2
EXIT_UNINITIALIZED = 3

MB = 1024 * 1024
GB = 1024 * MB

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1"})

__all__ = [
    "CLOUD_SYNC_VENDOR_MARKERS",
    "COMFORT_MEMORY_BYTES",
    "EXIT_DEGRADED",
    "EXIT_HEALTHY",
    "EXIT_UNHEALTHY",
    "EXIT_UNINITIALIZED",
    "MIN_AVAILABLE_MEMORY_BYTES",
    "MIN_FREE_DISK_BYTES",
    "STALE_BACKUP_SECONDS",
    "TLS_NEAR_EXPIRY_SECONDS",
    "CheckResult",
    "DoctorProbes",
    "apply_fixes",
    "default_probes",
    "run_doctor",
]


@dataclass(frozen=True)
class CheckResult:
    """One check's outcome; ``detail`` carries the actionable text.

    Severity mapping: ``ok=True`` without ``warning`` is a pass; ``ok=True``
    with ``warning=True`` is a WARNING (feeds the degraded exit code);
    ``ok=False`` is always a failure.
    """

    name: str
    ok: bool
    detail: str = ""
    warning: bool = False


@dataclass
class DoctorProbes:
    """Injected probe interface — every check is pure over these callables.

    Contract per probe: raise (or return the failure form documented
    inline) to make its check FAIL; the doctor converts any probe exception
    into a failed check with the exception text as the detail.
    """

    # int free bytes on the filesystem holding the data dir.
    disk_free_bytes: Callable[[Path], int]
    # raises when the root is not writable (RuntimeError/OSError).
    assert_writable: Callable[[Path], None]
    # hosts currently bound to this port (LISTEN sockets); empty = unknown.
    listening_on: Callable[[int], list[str]]
    # DB admin probe: raises when the bundled Postgres is not reachable.
    probe_postgres: Callable[[], None]
    # role posture violations (reuse of bootstrap_role's audit predicate).
    role_violations: Callable[[], list[str]]
    # redis probe: raises when ping/auth fails on the configured port.
    probe_redis: Callable[[], None]
    # True when the DB's alembic_version equals the head; raises -> failed.
    migrations_at_head: Callable[[], bool]
    # POSIX uid or None on platforms without one (TODO(P3) Windows).
    effective_uid: Callable[[], int | None]
    # username for a uid, or None (unknown / platform seam).
    username_of_uid: Callable[[int], str | None]
    # owner username of *path*, or None when unknown.
    file_owner: Callable[[Path], str | None]
    # lowest permission bits of the secrets file (None = unknown platform /
    # absent file; TODO(P3) Windows ACL seam returns None).
    secrets_mode: Callable[[Path], int | None] = field(default=lambda _data_dir: None)
    # names of launcher-hostile variables present in the inherited env.
    ambient_env_names: Callable[[], list[str]] = field(default=list)
    # the raw value of one process-environment variable (or None).
    env_value: Callable[[str], str | None] = field(default=lambda _name: None)
    # True when the launcher is installed as an OS service (False = FAR-674
    # not landed yet — the service check reports "not installed").
    service_installed: Callable[[], bool] = field(default=lambda: False)
    service_enabled: Callable[[], bool] = field(default=lambda: False)
    service_linger: Callable[[], bool] = field(default=lambda: False)
    # bytes of available memory, or None when the platform cannot say.
    available_memory_bytes: Callable[[], int | None] = field(default=lambda: None)
    # the data-dir cluster's PG_VERSION string, or None when absent.
    data_dir_pg_version: Callable[[], str | None] = field(default=lambda: None)
    # the bundled postgres binary's version string, or None (unresolvable).
    bundle_pg_version: Callable[[], str | None] = field(default=lambda: None)
    # the persisted last-run cluster version (runtime manifest bookkeeping),
    # or None when never recorded.
    installed_bundle_pg_version: Callable[[], str | None] = field(default=lambda: None)
    # bundled-binary paths to AV-block-audit (empty = no bundle resolved).
    bundled_binaries: Callable[[], list[Path]] = field(default=list)
    # attribution ("compose owner", "system postgres", ...) of a NON-bundled
    # listener on *port*, or None.
    port_owner_description: Callable[[int], str | None] = field(default=lambda _port: None)
    # description of a second native install detected, or None.
    second_install_hint: Callable[[], str | None] = field(default=lambda: None)
    # the first `modulo` executable on PATH, or None.
    modulo_on_path: Callable[[], str | None] = field(default=lambda: None)
    # the install root directory (this launcher's bin parent), or None.
    install_root: Callable[[], str | None] = field(default=lambda: None)
    # the supervisor's persisted degraded reason, or None.
    degraded_reason: Callable[[], str | None] = field(default=lambda: None)
    # epoch seconds of the last recorded backup, or None when never backed up
    # / not recorded by schema v1.
    last_backup_at: Callable[[], float | None] = field(default=lambda: None)
    # epoch seconds of the data-dir TLS keypair's notAfter, or None when no
    # keypair exists.
    tls_expiry: Callable[[], float | None] = field(default=lambda: None)
    # vendor description when the data dir sits inside a cloud-sync folder.
    cloud_sync_hit: Callable[[Path], str | None] = field(default=lambda _root: None)
    # the CWD .env path when it exists, else None.
    cwd_env_file: Path | None = field(default=None)
    # True when Settings' env file is pinned (launcher-pinned config).
    env_file_pinned: Callable[[], bool] = field(default=lambda: False)
    # True when the per-data-dir launcher process is alive.
    launcher_running: Callable[[], bool] = field(default=lambda: False)


# ---------------------------------------------------------------------------
# Checks (pure functions over the probes)
# ---------------------------------------------------------------------------


def check_data_dir(data_dir: Path, _state: Any, probes: DoctorProbes) -> CheckResult:
    """Check 1 — writable data dir + quantified disk floor."""
    try:
        free = probes.disk_free_bytes(data_dir)
    except Exception as exc:
        return CheckResult("data-dir", False, f"disk probe failed: {exc}")
    if free < MIN_FREE_DISK_BYTES:
        return CheckResult(
            "data-dir",
            False,
            f"only {free / MB:.0f} MiB free below {data_dir!s} (floor {MIN_FREE_DISK_BYTES // MB} MiB) — "
            "free space before upgrading or rebuilding the bundled Postgres",
        )
    try:
        probes.assert_writable(data_dir)
    except Exception as exc:
        return CheckResult("data-dir", False, f"data dir not writable: {exc}")
    return CheckResult(
        "data-dir",
        True,
        f"writable, {free / GB:.1f} GiB free of the {MIN_FREE_DISK_BYTES // MB} MiB floor",
    )


def check_ports(data_dir: Path, state: Any, probes: DoctorProbes) -> CheckResult:
    """Check 2 — configured PG/Redis/API ports vs actual loopback listeners."""
    if state is None:
        return CheckResult("ports", False, "state.json unreadable — cannot compare configured vs actual ports")
    if not probes.launcher_running():
        return CheckResult("ports", True, "launcher not running — live port assertions skipped")
    listeners_postgres = probes.listening_on(state.postgres_port)
    listeners_redis = probes.listening_on(state.redis_port)
    listeners_api = probes.listening_on(state.api_port)
    problems: list[str] = []
    for name, port, listeners in (
        ("postgres", state.postgres_port, listeners_postgres),
        ("redis", state.redis_port, listeners_redis),
        ("api", state.api_port, listeners_api),
    ):
        if not listeners:
            problems.append(f"{name} is NOT listening on configured port {port}")
            continue
        foreign = sorted(h for h in listeners if h.lower() not in LOOPBACK_HOSTS)
        if foreign:
            problems.append(f"{name} port {port} is bound outside loopback on {', '.join(foreign)}")
    if problems:
        return CheckResult("ports", False, "; ".join(problems))
    return CheckResult(
        "ports",
        True,
        f"postgres/redis/api listening on loopback ports "
        f"{state.postgres_port}/{state.redis_port}/{state.api_port} as configured",
    )


def check_postgres(_data_dir: Path, state: Any, probes: DoctorProbes) -> CheckResult:
    """Check 3 — bundled Postgres reachable + role posture (boot predicates)."""
    if state is None:
        return CheckResult("postgres", False, "state.json unreadable — cannot verify postgres port/posture")
    if not probes.launcher_running():
        return CheckResult("postgres", True, "launcher not running — postgres posture check skipped")
    try:
        probes.probe_postgres()
    except Exception as exc:
        return CheckResult(
            "postgres",
            False,
            f"not reachable on 127.0.0.1:{state.postgres_port}: {exc} — is the launcher running?",
        )
    try:
        violations = probes.role_violations()
    except Exception as exc:
        return CheckResult("postgres", False, f"role-posture probe failed: {exc}")
    if violations:
        return CheckResult(
            "postgres",
            False,
            f"reachable on 127.0.0.1:{state.postgres_port} but role posture DRIFTED: " + "; ".join(violations),
        )
    return CheckResult(
        "postgres",
        True,
        f"reachable on 127.0.0.1:{state.postgres_port}; role posture holds "
        "(modulo_app NOBYPASSRLS/superuser=false, modulo_system BYPASSRLS)",
    )


def check_redis(_data_dir: Path, state: Any, probes: DoctorProbes) -> CheckResult:
    """Check 4 — redis ping + auth on the configured loopback port."""
    if state is None:
        return CheckResult("redis", False, "state.json unreadable — cannot verify the configured redis port")
    if not probes.launcher_running():
        return CheckResult("redis", True, "launcher not running — redis ping skipped")
    try:
        probes.probe_redis()
    except Exception as exc:
        return CheckResult(
            "redis",
            False,
            f"redis ping failed on 127.0.0.1:{state.redis_port} (service down, wrong port or bad auth): {exc}",
        )
    return CheckResult("redis", True, f"answers PING with auth on 127.0.0.1:{state.redis_port}")


def check_migrations(_data_dir: Path, state: Any, probes: DoctorProbes) -> CheckResult:
    """Check 5 — database at the alembic migration head (boot fast-path predicate)."""
    if state is None:
        return CheckResult("migrations", False, "state.json unreadable — cannot locate the bundled database")
    if not probes.launcher_running():
        return CheckResult("migrations", True, "launcher not running — migration check skipped")
    try:
        at_head = probes.migrations_at_head()
    except Exception as exc:
        return CheckResult("migrations", False, f"migration probe failed: {exc}")
    if not at_head:
        return CheckResult(
            "migrations",
            False,
            "database is NOT at the alembic head — restart `modulo start` so the lifespan migration path runs",
        )
    return CheckResult("migrations", True, "database at the alembic migration head")


def check_cwd_env_influence(_data_dir: Path, _state: Any, probes: DoctorProbes) -> CheckResult:
    """Check 6 — a CWD ``.env`` must not be able to steer Settings."""
    env_file = probes.cwd_env_file
    if env_file is None:
        return CheckResult("env-influence", True, "no .env in the launch directory")
    from modulo.launcher.env_safety import _ambient_urls_from_env_file

    ambient = sorted(_ambient_urls_from_env_file(env_file))
    if not ambient:
        return CheckResult("env-influence", True, f"{env_file} present but carries no ambient service URLs")
    if probes.env_file_pinned():
        return CheckResult(
            "env-influence",
            True,
            f"{env_file} carries {', '.join(ambient)} but the launcher pinned its config — "
            "the CWD .env cannot influence Settings",
        )
    return CheckResult(
        "env-influence",
        False,
        f"{', '.join(ambient)} in {env_file} would influence Settings (no pinned "
        "launcher config) — rename/remove the file, or start with `modulo start`",
    )


def check_privileges(data_dir: Path, _state: Any, probes: DoctorProbes) -> CheckResult:
    """Check 7 — root/sudo refusal + data-dir ownership mismatch."""
    uid = probes.effective_uid()
    if uid is None:
        return CheckResult("privileges", True, "uid unavailable on this platform (TODO(P3) Windows equivalents)")
    if uid == 0:
        return CheckResult(
            "privileges",
            False,
            "running as root/sudo — the bundled postgres refuses a root-owned data dir; "
            "start `modulo start` as an unprivileged user",
        )
    me = probes.username_of_uid(uid)
    owner = probes.file_owner(data_dir)
    if me is not None and owner is not None and owner != me:
        return CheckResult(
            "privileges",
            False,
            f"data dir {data_dir} is owned by '{owner}' but the launcher runs as '{me}' — "
            f"chown -R {me} {data_dir} (an ownership flip bricks the bundled Postgres on restart)",
        )
    who = f"'{me}'" if me is not None else f"uid {uid}"
    return CheckResult("privileges", True, f"running unprivileged as {who}; data-dir ownership consistent")


# ---------------------------------------------------------------------------
# Full-doctor checks (FAR-676)
# ---------------------------------------------------------------------------


def check_secrets_permissions(data_dir: Path, _state: Any, probes: DoctorProbes) -> CheckResult:
    """Check 9 — the secrets file is 0600 owner-only (POSIX)."""
    secrets_path = data_dir / "secrets.json"
    if not secrets_path.is_file():
        return CheckResult("secrets-permissions", True, "no secrets file (data dir not initialized)")
    try:
        mode = probes.secrets_mode(data_dir)
    except Exception as exc:
        return CheckResult("secrets-permissions", False, f"secrets-permission probe failed: {exc}")
    if mode is None:
        return CheckResult(
            "secrets-permissions",
            True,
            "permission bits unavailable on this platform (TODO(P3) Windows ACL equivalents)",
        )
    if mode != 0o600:
        return CheckResult(
            "secrets-permissions",
            False,
            f"secrets.json permissions are {oct(mode)} — the launcher credentials must be owner-only: "
            f"chmod 600 {secrets_path}",
        )
    return CheckResult("secrets-permissions", True, "secrets.json is 0600 owner-only")


def check_ambient_pg_env(_data_dir: Path, _state: Any, probes: DoctorProbes) -> CheckResult:
    """Check 10 — WARNING: ambient PG*/URL variables could hijack bundled invocations."""
    try:
        names = probes.ambient_env_names()
    except Exception as exc:
        return CheckResult("ambient-pg-env", False, f"env probe failed: {exc}")
    if not names:
        return CheckResult("ambient-pg-env", True, "no launcher-hostile service variables in the environment")
    return CheckResult(
        "ambient-pg-env",
        True,
        f"{', '.join(sorted(names))} present in the inherited environment — the launcher scrubs them at "
        "boot (they would otherwise hijack bundled-binary invocations); find and unset the sources",
        warning=True,
    )


def check_settings_source(_data_dir: Path, state: Any, probes: DoctorProbes) -> CheckResult:
    """Check 11 — MODULO_DB named check + env-vs-state.json conflict."""
    try:
        moduledb = probes.env_value("MODULO_DB")
        db_url = probes.env_value("DATABASE_URL")
    except Exception as exc:
        return CheckResult("settings-source", False, f"env probe failed: {exc}")
    problems: list[str] = []
    if moduledb is not None and moduledb.strip().lower() != "postgres":
        problems.append(
            f"MODULO_DB={moduledb!r} would route Settings away from the bundled postgres — "
            "unset it (or use the Docker Compose path for non-postgres databases)"
        )
    if db_url is not None and state is not None:
        host, port = _host_port_from_database_url(db_url)
        if port is not None and port != state.postgres_port:
            problems.append(
                f"ambient DATABASE_URL points at {host}:{port} but state.json pins postgres port "
                f"{state.postgres_port} — an explicit env URL overrides the pinned launcher config; "
                "unset DATABASE_URL unless you intend the documented override"
            )
    if problems:
        return CheckResult("settings-source", True, "; ".join(problems), warning=True)
    return CheckResult(
        "settings-source",
        True,
        "MODULO_DB is unset (postgres default) and no ambient DATABASE_URL conflicts with state.json",
    )


def _host_port_from_database_url(url: str) -> tuple[str, int | None]:
    """(host, port) of an ambient DATABASE_URL (('unknown', None) when unparseable)."""
    from urllib.parse import urlparse

    try:
        parsed = urlparse(url)
    except ValueError:
        return "unparseable", None
    host = parsed.hostname or "unknown"
    port = parsed.port if parsed.port is not None else (5432 if host not in LOOPBACK_HOSTS else None)
    return host, port


def check_cloud_sync_root(data_dir: Path, _state: Any, probes: DoctorProbes) -> CheckResult:
    """Check 12 — WARNING: the data dir must not live inside a cloud-sync folder."""
    try:
        hit = probes.cloud_sync_hit(data_dir)
    except Exception as exc:
        return CheckResult("cloud-sync-root", False, f"cloud-sync probe failed: {exc}")
    if hit is None:
        return CheckResult("cloud-sync-root", True, "data dir is not inside a cloud-sync vendor folder")
    return CheckResult(
        "cloud-sync-root",
        True,
        f"data dir sits under {hit} — cloud-syncing a live Postgres data dir corrupts the cluster; "
        "stop the launcher, move the data dir outside the synced tree, and restore from backup",
        warning=True,
    )


def check_service_identity(_data_dir: Path, _state: Any, probes: DoctorProbes) -> CheckResult:
    """Check 13 — service identity: unit enabled + linger active when installed."""
    try:
        if not probes.service_installed():
            return CheckResult(
                "service",
                True,
                "launcher not installed as a service (no unit/linger requirement) — "
                "service registration ships with FAR-674",
            )
        enabled = probes.service_enabled()
        linger = probes.service_linger()
    except Exception as exc:
        return CheckResult("service", False, f"service probe failed: {exc}")
    problems: list[str] = []
    if not enabled:
        problems.append("the service unit is NOT enabled (systemctl enable modulo.service)")
    if not linger:
        problems.append("the service account has NO lingering (loginctl enable-linger <user>)")
    if problems:
        return CheckResult("service", False, f"service installed but degraded posture: {'; '.join(problems)}")
    return CheckResult("service", True, "service unit enabled; lingering active")


def check_memory_headroom(_data_dir: Path, _state: Any, probes: DoctorProbes) -> CheckResult:
    """Check 14 — available memory vs the documented envelope."""
    try:
        available = probes.available_memory_bytes()
    except Exception as exc:
        return CheckResult("memory", False, f"memory probe failed: {exc}")
    if available is None:
        return CheckResult("memory", True, "available memory unknown on this platform — honest skip")
    mib = available / MB
    if available < MIN_AVAILABLE_MEMORY_BYTES:
        return CheckResult(
            "memory",
            False,
            f"only {mib:.0f} MiB available (floor {MIN_AVAILABLE_MEMORY_BYTES // MB} MiB) — "
            "the bundled Postgres + Redis + API + workers cannot run",
        )
    if available < COMFORT_MEMORY_BYTES:
        return CheckResult(
            "memory",
            True,
            f"{mib:.0f} MiB available — within the floor but below the "
            f"{COMFORT_MEMORY_BYTES // MB} MiB comfortable envelope; expect pressure under load",
            warning=True,
        )
    return CheckResult("memory", True, f"{mib / 1024:.1f} GiB available (comfortable envelope met)")


def _version_tuple(version: str) -> tuple[int, ...]:
    parts: list[int] = []
    for token in version.strip().split("."):
        digits = "".join(ch for ch in token if ch.isdigit())
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


def check_bundle_versions(_data_dir: Path, state: Any, probes: DoctorProbes) -> CheckResult:
    """Check 15 — data-dir PG_VERSION vs bundle (drift) + last-run persisted."""
    try:
        data_version = probes.data_dir_pg_version()
        bundle_version = probes.bundle_pg_version()
        installed_version = probes.installed_bundle_pg_version()
    except Exception as exc:
        return CheckResult("bundle-version", False, f"bundle-version probe failed: {exc}")
    if data_version is None:
        return CheckResult(
            "bundle-version",
            True,
            "no PG_VERSION cluster data (not initialized or launcher not running) — check skipped",
        )
    if bundle_version is None:
        return CheckResult(
            "bundle-version",
            True,
            "bundled postgres version unresolvable (no bundle resolved) — drift check skipped",
        )
    data_tuple = _version_tuple(data_version)
    bundle_tuple = _version_tuple(bundle_version)
    if data_tuple != bundle_tuple:
        return CheckResult(
            "bundle-version",
            False,
            f"bundled postgres is {bundle_version} but the data-dir cluster was inited at {data_version} "
            "(bundle-minor/major drift) — match the binaries before upgrading or rebuilding the data dir",
        )
    if installed_tuple := _version_tuple(installed_version or ""):
        if installed_tuple > bundle_tuple:
            return CheckResult(
                "bundle-version",
                False,
                f"the bundled binary is OLDER than the cluster's last-run version "
                f"({bundle_version} < {installed_version}) — a downgrade must be refused: restore the "
                "matching binaries",
            )
        if installed_tuple < bundle_tuple:
            return CheckResult(
                "bundle-version",
                True,
                f"available upgrade: data dir last ran {installed_version}, the bundle now ships "
                f"{bundle_version} — restart `modulo start` to upgrade",
                warning=True,
            )
    return CheckResult(
        "bundle-version",
        True,
        f"data-dir PG_VERSION {data_version} matches the bundled binary {bundle_version}",
    )


def check_bundled_binaries(_data_dir: Path, _state: Any, probes: DoctorProbes) -> CheckResult:
    """Check 16 — AV-block detection: missing / zero-byte / non-executable bundle."""
    try:
        binaries = probes.bundled_binaries()
    except Exception as exc:
        return CheckResult("binaries", False, f"bundle probe failed: {exc}")
    if not binaries:
        return CheckResult(
            "binaries",
            True,
            "no bundled binaries resolved — real-machine-only audit (AV quarantine markers) skipped",
        )
    problems: list[str] = []
    for binary in binaries:
        stat = binary.stat()
        if stat.st_size == 0:
            problems.append(f"{binary} is ZERO bytes (likely AV-quarantined) — reinstall the bundle")
        elif sys.platform != "win32" and not stat.st_mode & 0o111:
            # TODO(P3): Windows quarantine detection (MotW zone identifier).
            problems.append(f"{binary} is present but NOT executable — restore the exec bit")
    if problems:
        return CheckResult(
            "binaries",
            False,
            "bundled binaries are blocked/absent: " + "; ".join(problems),
        )
    return CheckResult("binaries", True, f"{len(binaries)} bundled binaries present and executable")


def check_port_collisions(_data_dir: Path, state: Any, probes: DoctorProbes) -> CheckResult:
    """Check 17 — compose coexistence + system PG/Redis with attribution."""
    try:
        if not probes.launcher_running():
            return CheckResult("port-collisions", True, "launcher not running — live collision attribution skipped")
    except Exception as exc:
        return CheckResult("port-collisions", False, f"launcher-state probe failed: {exc}")
    if state is None:
        return CheckResult("port-collisions", False, "state.json unreadable — no ports to audit for collisions")
    problems: list[str] = []
    for name, port in (("postgres", state.postgres_port), ("redis", state.redis_port)):
        try:
            description = probes.port_owner_description(port)
        except Exception as exc:
            return CheckResult("port-collisions", False, f"port-owner probe failed: {exc}")
        if description is not None:
            problems.append(
                f"{name}'s configured port {port} is ALSO used by {description} — port collision when the "
                "launcher boots: free the port or reassign state.json (documented state.json port edit)"
            )
    if problems:
        return CheckResult("port-collisions", False, "; ".join(problems))
    return CheckResult(
        "port-collisions",
        True,
        "no compose/system PG or Redis service owns the configured bundles' ports",
    )


def check_install_shadows(data_dir: Path, _state: Any, probes: DoctorProbes) -> CheckResult:
    """Check 18 — second native install + PATH shadowing detection."""
    try:
        on_path = probes.modulo_on_path()
        root = probes.install_root()
        hint = probes.second_install_hint()
    except Exception as exc:
        return CheckResult("install-shadows", False, f"install-shadow probe failed: {exc}")
    problems: list[str] = []
    if on_path is not None and root is not None and Path(on_path).resolve().parent != Path(root).resolve():
        problems.append(
            f"the first 'modulo' on PATH is {on_path} but this install lives in {root} — "
            "PATH shadowing can run a different install against this data dir"
        )
    if hint is not None:
        problems.append(hint)
    if problems:
        return CheckResult(
            "install-shadows",
            True,
            "; ".join(problems),
            warning=True,
        )
    return CheckResult(
        "install-shadows",
        True,
        f"the resolved modulo on PATH matches this install; only one native install found ({data_dir.name})",
    )


def check_degraded(_data_dir: Path, _state: Any, probes: DoctorProbes) -> CheckResult:
    """Check 19 — surface the supervisor's persisted degraded flag."""
    try:
        reason = probes.degraded_reason()
    except Exception as exc:
        return CheckResult("degraded", False, f"degraded-state probe failed: {exc}")
    if reason is None:
        return CheckResult("degraded", True, "supervisor is not in a degraded state")
    return CheckResult(
        "degraded",
        False,
        f"the supervisor tripped its terminal degraded state: {reason} — inspect `modulo status` and the "
        "app log (`modulo logs`), clear the underlying fault, then restart `modulo start`",
    )


def check_tls_expiry(_data_dir: Path, _state: Any, probes: DoctorProbes) -> CheckResult:
    """Check 20 — TLS keypair near-expiry (when one exists in the data dir)."""
    try:
        expiry = probes.tls_expiry()
    except Exception as exc:
        return CheckResult("tls", False, f"tls probe failed: {exc}")
    if expiry is None:
        return CheckResult("tls", True, "no TLS keypair in the data dir — expiry check skipped")
    import time

    now = time.time()
    remaining = expiry - now
    if remaining <= 0:
        return CheckResult(
            "tls",
            False,
            "the data-dir TLS keypair has EXPIRED — regenerate the keypair before clients reconnect",
        )
    if remaining < TLS_NEAR_EXPIRY_SECONDS:
        return CheckResult(
            "tls",
            True,
            f"TLS keypair expires in {remaining / 86400:.0f} days (< {TLS_NEAR_EXPIRY_SECONDS // 86400}d) "
            "— schedule regeneration",
            warning=True,
        )
    return CheckResult("tls", True, f"TLS keypair valid for {remaining / 86400:.0f} more days")


def check_stale_backup(_data_dir: Path, _state: Any, probes: DoctorProbes) -> CheckResult:
    """Check 21 — stale-backup warning (no backup newer than 7d)."""
    try:
        last_backup = probes.last_backup_at()
    except Exception as exc:
        return CheckResult("stale-backup", False, f"backup probe failed: {exc}")
    if last_backup is None:
        return CheckResult(
            "stale-backup",
            True,
            "no last-backup timestamp recorded (state.json schema v1 does not record one yet) — skipped",
        )
    import time

    age = time.time() - last_backup
    if age > STALE_BACKUP_SECONDS:
        return CheckResult(
            "stale-backup",
            True,
            f"the last recorded backup is {age / 86400:.0f} days old (> {STALE_BACKUP_SECONDS // 86400}d) — "
            "run `modulo backup` and point it at the fixed data dir to refresh",
            warning=True,
        )
    return CheckResult("stale-backup", True, f"last recorded backup is {age / 86400:.0f} days old")


_CHECKS: tuple[Callable[[Path, Any, DoctorProbes], CheckResult], ...] = (
    check_data_dir,
    check_ports,
    check_postgres,
    check_redis,
    check_migrations,
    check_cwd_env_influence,
    check_privileges,
    check_secrets_permissions,
    check_ambient_pg_env,
    check_settings_source,
    check_cloud_sync_root,
    check_service_identity,
    check_memory_headroom,
    check_bundle_versions,
    check_bundled_binaries,
    check_port_collisions,
    check_install_shadows,
    check_degraded,
    check_tls_expiry,
    check_stale_backup,
)

# crash-time check names must match the names the checks themselves emit.
_CRASH_NAMES: dict[str, str] = {
    "check_data_dir": "data-dir",
    "check_ports": "ports",
    "check_postgres": "postgres",
    "check_redis": "redis",
    "check_migrations": "migrations",
    "check_cwd_env_influence": "env-influence",
    "check_privileges": "privileges",
    "check_secrets_permissions": "secrets-permissions",
    "check_ambient_pg_env": "ambient-pg-env",
    "check_settings_source": "settings-source",
    "check_cloud_sync_root": "cloud-sync-root",
    "check_service_identity": "service",
    "check_memory_headroom": "memory",
    "check_bundle_versions": "bundle-version",
    "check_bundled_binaries": "binaries",
    "check_port_collisions": "port-collisions",
    "check_install_shadows": "install-shadows",
    "check_degraded": "degraded",
    "check_tls_expiry": "tls",
    "check_stale_backup": "stale-backup",
}

_KIND_CHECK_NAME = "state-integrity"


# ---------------------------------------------------------------------------
# Default probes (the real launcher runtime wiring)
# ---------------------------------------------------------------------------


def default_probes(data_dir: Path, state: Any) -> DoctorProbes:
    """Build the probe set used by ``run_doctor`` outside unit tests.

    READ-ONLY like ``collect_status``: the secrets file is parsed without the
    create-if-missing behaviour; a doctor run must never create or rewrite
    data-dir artefacts.
    """
    from modulo.launcher import secrets_file as secrets_file_module
    from modulo.launcher.config_source import compose_config
    from modulo.launcher.secrets_file import SecretsFileError

    composed: dict[str, str] = {}
    if state is not None:
        try:
            secrets = secrets_file_module._parse((data_dir / "secrets.json").read_bytes())
            composed = compose_config(state, secrets)
        except (SecretsFileError, OSError):
            composed = {}

    def _probe_writable(root: Path) -> None:
        probe_path = root / f".doctor-write-probe-{os.getpid()}"
        probe_path.write_text("ok", encoding="utf-8")
        probe_path.unlink()

    def _effective_uid() -> int | None:
        return os.getuid() if hasattr(os, "getuid") else None

    def _username_of_uid(uid: int) -> str | None:
        if os.name != "posix":
            return None  # TODO(P3): Windows SID → account mapping
        try:
            import pwd
        except ImportError:
            return None
        try:
            record: Any = pwd.getpwuid(uid)
            name = getattr(record, "pw_name", None)
            return str(name) if name else None
        except KeyError:
            return None

    def _file_owner(path: Path) -> str | None:
        if not path.exists():
            return None
        return _username_of_uid(path.stat().st_uid)

    def _listening_on(port: int) -> list[str]:
        if sys.platform != "linux":
            return []  # /proc absent; TODO(P3) Windows/macOS external-bind inspection
        return _parse_listeners_from_proc(port)

    def _probe_postgres() -> None:
        admin_url = composed.get("DATABASE_ADMIN_URL") or ""
        if not admin_url:
            raise RuntimeError("no composed DATABASE_ADMIN_URL (state/secrets unavailable)")
        import asyncpg  # asyncpg does not publish a py.typed marker

        from modulo.db.bootstrap_role import _asyncpg_admin_connect

        dsn, ssl_arg = _asyncpg_admin_connect(admin_url)

        async def _go() -> None:
            conn = await asyncpg.connect(dsn, ssl=ssl_arg, timeout=5)
            try:
                await conn.fetchval("SELECT 1")
            finally:
                await conn.close()

        asyncio.run(_go())

    def _probe_role_violations() -> list[str]:
        admin_url = composed.get("DATABASE_ADMIN_URL") or ""
        app_url = composed.get("DATABASE_URL") or ""
        if not admin_url or not app_url:
            raise RuntimeError("no composed database URLs (state/secrets unavailable)")
        import asyncpg

        from modulo.db.bootstrap_role import (
            _asyncpg_admin_connect,
            _find_allow_list_violations,
            _parse_role,
        )

        async def _go() -> list[str]:
            dsn, ssl_arg = _asyncpg_admin_connect(admin_url)
            conn = await asyncpg.connect(dsn, ssl=ssl_arg, timeout=5)
            try:
                return list(await _find_allow_list_violations(conn, _parse_role(app_url)))
            finally:
                await conn.close()

        return asyncio.run(_go())

    def _probe_redis() -> None:
        redis_url = composed.get("REDIS_URL") or ""
        if not redis_url:
            raise RuntimeError("no composed REDIS_URL (state/secrets unavailable)")
        import redis

        client = redis.Redis(
            host="127.0.0.1",
            port=state.redis_port,
            password=_password_from_url(redis_url),
            socket_connect_timeout=5,
            socket_timeout=5,
        )
        if not client.ping():
            raise RuntimeError("redis PING failed")

    def _probe_migrations_at_head() -> bool:
        database_url = composed.get("DATABASE_URL") or ""
        if not database_url:
            raise RuntimeError("no composed DATABASE_URL (state/secrets unavailable)")

        from modulo.db.health_checks import db_is_at_migration_head

        async def _go() -> bool:
            from sqlalchemy.ext.asyncio import create_async_engine

            engine = create_async_engine(database_url)
            try:
                return bool(await db_is_at_migration_head(engine))
            finally:
                await engine.dispose()

        return asyncio.run(_go())

    def _probe_launcher_running() -> bool:
        from modulo.launcher.supervisor import LOCK_SUFFIX, _pid_alive, _read_lock_holder

        # Mirrors collect_status: the lock sibling of the data dir names the holder.
        lock_path = data_dir.parent / (data_dir.name + LOCK_SUFFIX)
        holder = _read_lock_holder(lock_path)
        return holder is not None and _pid_alive(holder.pid)

    def _probe_env_file_pinned() -> bool:
        from modulo.settings import pinned_env_file

        return pinned_env_file() is not None

    env_snapshot = dict(os.environ)

    def __probe_env_value(name: str) -> str | None:
        return env_snapshot.get(name)

    def __probe_secrets_mode(root: Path) -> int | None:
        # TODO(P3): Windows ACL equivalence (icacls); the POSIX stat bits are
        # the P1a source of truth.
        if sys.platform == "win32":
            return None
        secrets_path = root / "secrets.json"
        if not secrets_path.is_file():
            return None
        return secrets_path.stat().st_mode & 0o777

    def __probe_ambient_env_names() -> list[str]:
        from modulo.launcher.env_safety import AMBIENT_SERVICE_URL_VARS, _is_scrubbed

        source = dict(os.environ)
        hostile = {name for name in source if _is_scrubbed(name)}
        hostile.update(name for name in AMBIENT_SERVICE_URL_VARS if source.get(name))
        return sorted(name for name in hostile if source.get(name))

    def __probe_available_memory_bytes() -> int | None:
        try:
            import psutil  # type: ignore[import-untyped]

            return int(psutil.virtual_memory().available)
        except Exception as exc:  # psutil unavailable: fall through to /proc (honest None)
            _log.warning("doctor.memory_psutil_unavailable reason=%r", exc)
        try:
            for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) * 1024
        except Exception:
            return None
        return None

    def __probe_data_dir_pg_version() -> str | None:
        version_path = data_dir / PGDATA_DIRNAME / "PG_VERSION"
        try:
            return version_path.read_text(encoding="ascii").strip() if version_path.is_file() else None
        except OSError:
            return None

    def __probe_bundle_pg_version() -> str | None:
        from modulo.launcher.entry import resolve_bin_dir

        binary = resolve_bin_dir() / ("postgres.exe" if sys.platform == "win32" else "postgres")
        if not binary.is_file():
            return None
        try:
            result = subprocess.run(  # noqa: S603 — argv fully pinned
                [str(binary), "--version"],
                check=False,
                capture_output=True,
                text=True,
                timeout=15,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        # "postgres (PostgreSQL) 16.4 (Ubuntu ...)" -> "16.4"
        for token in result.stdout.split():
            if token and token[0].isdigit() and "." in token:
                return token
        return None

    def __probe_installed_bundle_pg_version() -> str | None:
        from modulo.launcher.supervisor import RUNTIME_FILENAME, _read_manifest_fields

        extra = _read_manifest_fields(data_dir / RUNTIME_FILENAME).get("extra")
        if not isinstance(extra, dict):
            return None
        version = extra.get("installed_bundle_pg_version")
        return version if isinstance(version, str) else None

    def __probe_bundled_binaries() -> list[Path]:
        from modulo.launcher.entry import resolve_bin_dir

        bin_dir = resolve_bin_dir()
        names = ("initdb", "postgres", "pg_isready", "redis-server", "redis-cli")
        suffix = ".exe" if sys.platform == "win32" else ""
        return [bin_dir / f"{name}{suffix}" for name in names if (bin_dir / f"{name}{suffix}").is_file()]

    def __probe_cloud_sync_hit(root: Path) -> str | None:
        current = root
        for _depth in range(5):
            for marker in CLOUD_SYNC_VENDOR_MARKERS:
                if marker in current.name.lower():
                    return f"{current} (matched cloud-sync marker {marker!r})"
            if current.parent == current:
                break
            current = current.parent
        return None

    def __probe_modulo_on_path() -> str | None:
        return shutil.which("modulo")

    def __probe_install_root() -> str | None:
        return str(Path(sys.executable).parent)

    def __probe_second_install_hint() -> str | None:
        siblings = [
            sibling
            for sibling in data_dir.parent.iterdir()
            if sibling.is_dir()
            and sibling != data_dir
            and (sibling / "state.json").is_file()
            and (sibling / "secrets.json").is_file()
        ]
        if not siblings:
            return None
        return (
            f"a second native install lives at {siblings[0]} — two launcher data dirs can shadow each "
            "other's state; confirm which install you are operating"
        )

    def __probe_degraded_reason() -> str | None:
        from modulo.launcher.supervisor import RUNTIME_FILENAME, read_degraded_reason

        return read_degraded_reason(data_dir / RUNTIME_FILENAME)

    return DoctorProbes(
        disk_free_bytes=lambda root: shutil.disk_usage(str(root)).free,
        assert_writable=_probe_writable,
        listening_on=_listening_on,
        probe_postgres=_probe_postgres,
        role_violations=_probe_role_violations,
        probe_redis=_probe_redis,
        migrations_at_head=_probe_migrations_at_head,
        effective_uid=_effective_uid,
        username_of_uid=_username_of_uid,
        file_owner=_file_owner,
        secrets_mode=__probe_secrets_mode,
        ambient_env_names=__probe_ambient_env_names,
        env_value=__probe_env_value,
        service_installed=lambda: False,  # FAR-674's service registry is not landed yet
        available_memory_bytes=__probe_available_memory_bytes,
        data_dir_pg_version=__probe_data_dir_pg_version,
        bundle_pg_version=__probe_bundle_pg_version,
        installed_bundle_pg_version=__probe_installed_bundle_pg_version,
        bundled_binaries=__probe_bundled_binaries,
        cloud_sync_hit=__probe_cloud_sync_hit,
        modulo_on_path=__probe_modulo_on_path,
        install_root=__probe_install_root,
        second_install_hint=__probe_second_install_hint,
        degraded_reason=__probe_degraded_reason,
        cwd_env_file=_cwd_env_file(),
        env_file_pinned=_probe_env_file_pinned,
        launcher_running=_probe_launcher_running,
    )


def _password_from_url(redis_url: str) -> str:
    from urllib.parse import unquote, urlparse

    return unquote(urlparse(redis_url).password or "")


def _cwd_env_file() -> Path | None:
    candidate = Path.cwd() / ".env"
    return candidate if candidate.exists() else None


def _parse_listeners_from_proc(port: int) -> list[str]:
    """LISTEN sockets on *port* from /proc/net{,6}/tcp (Linux only)."""

    hosts: set[str] = set()
    for proc_path, family in ((Path("/proc/net/tcp"), socket.AF_INET), (Path("/proc/net/tcp6"), socket.AF_INET6)):
        try:
            lines = proc_path.read_text(encoding="ascii").splitlines()[1:]
        except (OSError, ValueError):
            continue
        for line in lines:
            fields = line.split()
            if len(fields) < 4 or fields[3] != "0A":  # 0A = LISTEN
                continue
            host_hex, port_hex = fields[1].split(":")
            if int(port_hex, 16) != port:
                continue
            try:
                hosts.add(_decode_proc_address(host_hex, family))
            except (ValueError, OSError):
                continue
    return sorted(hosts)


def _decode_proc_address(host_hex: str, family: int) -> str:
    if family == socket.AF_INET:
        raw = struct.pack("<I", int(host_hex, 16))
        return socket.inet_ntop(socket.AF_INET, raw)
    words = [int(host_hex[i : i + 8], 16) for i in range(0, 32, 8)]
    raw = struct.pack("<4I", *words)
    return socket.inet_ntop(socket.AF_INET6, raw)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _load_state_readonly(data_dir: Path) -> tuple[Any, str | None]:
    """(state, error) — read-only; a missing/unverifiable state is an error, not a crash."""
    from modulo.launcher.secrets_file import SecretsFileError, _parse
    from modulo.launcher.state import STATE_FILENAME, StateIntegrityError, StateVersionError, load_state

    secrets_path = data_dir / "secrets.json"
    if not secrets_path.exists():
        return None, "data dir is not initialized (no secrets file) — run `modulo start` first"
    try:
        secrets = _parse(secrets_path.read_bytes())
    except (SecretsFileError, OSError) as exc:
        return None, f"secrets file unreadable: {exc}"
    try:
        return load_state(data_dir / STATE_FILENAME, secrets.state_hmac_key), None
    except (StateIntegrityError, StateVersionError) as exc:
        return None, f"state.json unreadable: {exc}"
    except FileNotFoundError:
        return None, f"no {STATE_FILENAME} in {data_dir} — run `modulo start` first"


def _state_problem_kind(data_dir: Path, state_error: str | None) -> str | None:
    """DISTINCT classification for the state-integrity reporting.

    Kinds: ``None`` ok; ``missing`` (uninitialized — exit-code 3); ``corrupt``
    (state.json torn/unparseable/foreign envelope); ``hmac-mismatch``
    (tampered or keyed by a foreign secrets HMAC key); ``secrets-unreadable``
    (the 0600 secrets file itself is torn); ``schema-version`` (the
    lifecycle downgrade/upgrade boundary).
    """
    if state_error is None:
        return None
    if "secrets file unreadable" in state_error:
        return "secrets-unreadable"
    if not (data_dir / "secrets.json").exists():
        return "missing"
    if "HMAC verification" in state_error:
        return "hmac-mismatch"
    if "schema_version" in state_error:
        return "schema-version"
    return "corrupt"


# ---------------------------------------------------------------------------
# --fix (orphan cleanup + port re-assignment guidance)
# ---------------------------------------------------------------------------


def apply_fixes(data_dir: Path) -> list[str]:
    """Apply the doctor's FIX actions; return what was done as short lines.

    Orphan cleanup reuses the supervisor's boot-time reconciliation over the
    bundled pgdata (a stale ``postmaster.pid`` is removed ONLY when provably
    not a live postgres; stale per-boot Redis confs and initdb temp debris
    are swept). A LIVE postgres still holding its pidfile is a hard refusal
    surfaced as a RuntimeError — we never race another postmaster. Port
    re-assignment is printed as documented guidance, never auto-edited:
    state.json ports are launcher-owned.
    """
    from modulo.launcher.entry import PGDATA_DIRNAME
    from modulo.launcher.supervisor import LauncherError, reconcile_orphans

    actions: list[str] = []
    pgdata = data_dir / PGDATA_DIRNAME
    try:
        action = reconcile_orphans(pgdata)
    except LauncherError as exc:
        raise RuntimeError(
            f"refused: {exc}; stop the postgres that owns the pidfile (via its owner first), "
            "then re-run `modulo doctor --fix`"
        ) from exc
    if action is None:
        actions.append("no orphan debris found (nothing swept)")
    else:
        actions.append(f"orphan cleanup: {action}")
    if (data_dir / "state.json").exists():
        actions.append(
            "port re-assignment: colliding ports are launcher-owned state — free the colliding user, "
            "or hand-edit state.json's *_port fields (documented edit) while the launcher is stopped"
        )
    return actions


def run_doctor(
    data_dir: Path,
    *,
    as_json: bool = False,
    probes: DoctorProbes | None = None,
    fix: bool = False,
    sink: Callable[[str], Any] | None = None,
) -> int:
    """Run every check and emit the report (human table or --json).

    Returns the documented exit code (see the module docstring): 0 healthy,
    1 unhealthy, 2 degraded (warnings only), 3 uninitialized. ``fix=True``
    applies orphan cleanup first and retakes the checks; ``sink`` (default
    print) receives every output line — used by ``--report`` to capture the
    doctor output without a subprocess.
    """
    emit = sink if sink is not None else _print_line
    if fix:
        for action in apply_fixes(data_dir):
            emit(action)
    state, state_error = _load_state_readonly(data_dir)
    state_kind = _state_problem_kind(data_dir, state_error)
    built = probes if probes is not None else default_probes(data_dir, state)
    results: list[CheckResult] = []
    if state_error:
        info = f"state unavailable: {state_error}"
        results.append(CheckResult(_KIND_CHECK_NAME, False, _state_integrity_detail(state_error, state_kind)))
        results.extend(CheckResult(name, False, info) for name in ("ports", "postgres", "redis", "migrations"))
    for check in _CHECKS:
        try:
            results.append(check(data_dir, state, built))
        except Exception as exc:
            name = _CRASH_NAMES.get(check.__name__, check.__name__.removeprefix("check_"))
            results.append(CheckResult(name, False, f"check crashed: {exc}"))
    if not results:  # noqa: SIM108 — guard all() on empty iterable (semgrep all-empty-iterable)
        healthy = False
    else:
        healthy = all(result.ok for result in results)
    exit_code = _exit_code_for(healthy=healthy, results=results, state_kind=state_kind)
    if as_json:
        import json

        emit(json.dumps(_payload_json(data_dir, healthy, results, exit_code), indent=2, sort_keys=True))
    else:
        _print_report(data_dir, results, healthy, emit)
    return exit_code


def _state_integrity_detail(state_error: str, kind: str | None) -> str:
    """Distinct corrupt vs HMAC-mismatch language for the state-integrity check."""
    if kind == "hmac-mismatch":
        return (
            "state.json fails HMAC verification (tampered, truncated, or written by a different "
            f"install's secrets key) — restore the secrets file, or reset the data dir. {state_error}"
        )
    if kind == "corrupt":
        return (
            "state.json is CORRUPT (torn write or not an HMAC envelope) — restore from backup or "
            f"reset the data dir. {state_error}"
        )
    return state_error


def _exit_code_for(
    *,
    healthy: bool,
    results: list[CheckResult],
    state_kind: str | None,
) -> int:
    """Map health/warnings/uninitialized onto the documented doctor exit codes."""
    if state_kind == "missing":
        return EXIT_UNINITIALIZED
    if not healthy:
        return EXIT_UNHEALTHY
    if any(result.ok and result.warning for result in results):
        return EXIT_DEGRADED
    return EXIT_HEALTHY


def _print_line(text: str) -> None:
    print(text)  # noqa: T201 — CLI output


def _payload_json(data_dir: Path, healthy: bool, results: list[CheckResult], exit_code: int) -> dict[str, Any]:
    return {
        "data_dir": str(data_dir),
        "healthy": healthy,
        "exit_code": exit_code,
        "checks": [
            {"name": result.name, "ok": result.ok, "warning": result.warning, "detail": result.detail}
            for result in results
        ],
    }


def _print_report(data_dir: Path, results: list[CheckResult], healthy: bool, emit: Callable[[str], Any]) -> None:
    emit(f"modulo doctor — data dir: {data_dir}")
    width = max(len(result.name) for result in results)
    for result in results:
        status = "FAIL" if not result.ok else ("warn" if result.warning else "ok  ")
        emit(f"  [{status}] {result.name:<{width}}  {result.detail}")
    if healthy:
        warn_names = sorted({r.name for r in results if r.ok and r.warning})
        if warn_names:
            emit(f"degraded: {len(warn_names)} warning(s): {', '.join(warn_names)} — nothing is failing, yet")
        else:
            emit("healthy: all checks passed")
    else:
        failed = [result.name for result in results if not result.ok]
        emit(f"unhealthy: {len(failed)} failing check(s): {', '.join(failed)}")
