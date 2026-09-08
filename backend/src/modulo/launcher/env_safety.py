"""Environment scrubbing + first-boot ambient-URL refusal (ADR 031 Decisions 2/8).

The native entry calls :func:`scrub_os_environment` at the very top — BEFORE
any project import — so a launcher-hostile variable inherited from the calling
shell (``PGHOST`` pointing at a foreign Postgres, ``PYTHONPATH`` importing a
foreign tree, ``LD_PRELOAD``/``DYLD_*`` injecting code, a poisoned trust
store) cannot influence the bundled runtime.

Scrub list (ADR 031 Decision 2, verbatim):

* ``PG*`` — every Postgres client libpq variable (PGDATA, PGHOST, PGPORT,
  PGPASSWORD, PGUSER, PGSSLMODE, ...). Covered by the ``PG`` prefix.
* ``REDISCLI_AUTH``, ``PYTHONPATH``, ``PYTHONHOME``, ``LD_PRELOAD``,
  ``LD_LIBRARY_PATH``, ``OPENSSL_CONF``, ``SSL_CERT_FILE``,
  ``SSL_CERT_DIR``, ``REQUESTS_CA_BUNDLE`` — exact names.
* ``DYLD_*`` — every macOS dynamic-loader variable (covered by prefix; the
  P1a Linux runtime never sets them, but the scrub is platform-independent
  by design; TODO(P3): the Windows DLL search path has no DYLD equivalent
  and is out of scope).

The first-boot guard (:func:`assert_no_ambient_service_urls`) refuses to boot
when ambient ``DATABASE_URL``/``REDIS_URL`` are present and the data dir has
no ``state.json`` yet — an inherited URL would silently redirect the bundled
services to a foreign endpoint (ADR 031 Decision 2: unconditional in P1a).
Once a ``state.json`` exists (first boot completed), explicit URLs are an
operator override path and are no longer refused by this guard.
"""

import os
from collections.abc import Callable
from pathlib import Path

# Exact-name scrub list (ADR 031 Decision 2).
SCRUB_EXACT_NAMES: frozenset[str] = frozenset(
    {
        "REDISCLI_AUTH",
        "PYTHONPATH",
        "PYTHONHOME",
        "LD_PRELOAD",
        "LD_LIBRARY_PATH",
        "OPENSSL_CONF",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
    }
)

# Prefix scrub list: PG* (libpq client vars) and DYLD_* (macOS loader).
SCRUB_PREFIXES: tuple[str, ...] = ("PG", "DYLD_")

# Ambient service URLs refused on an un-bootstrapped data dir.
AMBIENT_SERVICE_URL_VARS: tuple[str, ...] = ("DATABASE_URL", "REDIS_URL")

# The launcher-owned public surface (consumed by the slice-2 native entry and
# modulo.settings integration; vulture's dead-code gate special-cases __all__).
__all__ = [
    "AMBIENT_SERVICE_URL_VARS",
    "SCRUB_EXACT_NAMES",
    "SCRUB_PREFIXES",
    "AmbientEnvironmentError",
    "assert_no_ambient_service_urls",
    "make_first_boot_guard",
    "scrub_environment",
    "scrub_os_environment",
]


class AmbientEnvironmentError(RuntimeError):
    """Raised when a first boot inherits ambient DATABASE_URL/REDIS_URL."""


def _is_scrubbed(name: str) -> bool:
    upper = name.upper()
    if upper in SCRUB_EXACT_NAMES:
        return True
    return any(upper.startswith(prefix) for prefix in SCRUB_PREFIXES)


def scrub_environment(env: dict[str, str] | None = None) -> dict[str, str]:
    """Return a scrubbed COPY of *env* (default: a snapshot of ``os.environ``).

    Pure function — the caller's dict (and ``os.environ``) are not mutated;
    use :func:`scrub_os_environment` for the in-place entrypoint behaviour.
    """
    source = dict(os.environ if env is None else env)
    return {name: value for name, value in source.items() if not _is_scrubbed(name)}


def scrub_os_environment() -> None:
    """Scrub ``os.environ`` in place (the native entrypoint's first action).

    Called at the top of the native entry BEFORE any project import
    (ADR 031 Decision 2). Idempotent.
    """
    scrubbed = scrub_environment()
    for name in [name for name in os.environ if _is_scrubbed(name)]:
        del os.environ[name]
    for name, value in scrubbed.items():
        if name not in os.environ:
            os.environ[name] = value


def assert_no_ambient_service_urls(state_dir: Path, env: dict[str, str] | None = None) -> None:
    """Refuse a first boot that inherits ambient service URLs.

    Unconditional in P1a (no override flag): when *state_dir* has no
    ``state.json`` yet, any ambient ``DATABASE_URL``/``REDIS_URL`` in the
    process environment is a hard error whose message points the operator at
    the Docker Compose path (the supported way to run with explicit URLs).
    """
    source = dict(os.environ if env is None else env)
    ambient = [name for name in AMBIENT_SERVICE_URL_VARS if source.get(name)]
    if not ambient:
        return
    if (state_dir / "state.json").exists():
        return
    raise AmbientEnvironmentError(
        "MODULO first boot refused: ambient "
        + ", ".join(ambient)
        + " found in the environment. A native single-install first boot must "
        "not inherit service endpoints from the surrounding shell — they would "
        "silently redirect the bundled services to a foreign endpoint. Unset "
        "the variable(s) and start again, or use the Docker Compose path for "
        "explicit service URLs (docker compose -f docker-compose.yml up)."
    )


def make_first_boot_guard(state_dir: Path) -> Callable[[], None]:
    """Build the hook installed into ``modulo.settings.set_first_boot_guard``.

    The hook runs at Settings validation time so ANY process constructing
    Settings through the launcher's pinned configuration fails fast on an
    un-bootstrapped data dir with ambient service URLs.
    """

    def _guard() -> None:
        assert_no_ambient_service_urls(state_dir)

    return _guard
