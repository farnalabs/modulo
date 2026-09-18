"""Environment scrubbing + first-boot ambient-URL refusal (ADR 031 Decisions 2/8).

The native entry calls :func:`scrub_os_environment` at the very top — BEFORE
any project import — so a launcher-hostile variable inherited from the calling
shell (``PGHOST`` pointing at a foreign Postgres, ``PYTHONPATH`` importing a
foreign tree, ``LD_*`` injecting code, a poisoned trust
store) cannot influence the bundled runtime.

Scrub list (ADR 031 Decision 2, verbatim):

* ``PG*`` — every Postgres client libpq variable (PGDATA, PGHOST, PGPORT,
  PGPASSWORD, PGUSER, PGSSLMODE, ...). Covered by the ``PG`` prefix.
* ``REDISCLI_AUTH``, ``PYTHONPATH``, ``PYTHONHOME``, ``PYTHONUSERBASE``,
  ``OPENSSL_CONF``, ``SSL_CERT_FILE``, ``SSL_CERT_DIR``,
  ``REQUESTS_CA_BUNDLE``, ``CURL_CA_BUNDLE`` — exact names.
* ``LD_*`` — the whole ELF dynamic-loader family (LD_PRELOAD, LD_LIBRARY_PATH,
  LD_AUDIT, LD_BIND_NOW, ...), covered by the prefix; the individual
  ``LD_PRELOAD``/``LD_LIBRARY_PATH``/``LD_AUDIT`` names stay in the exact set
  as self-documentation.
* ``MODULO_TEST_PAUSE_AT`` — the initdb deterministic-pause test seam must
  never leak from a developer shell into a production launcher boot (a stale
  value would silently block mid-bootstrap).
* ``DYLD_*`` — every macOS dynamic-loader variable (covered by prefix; the
  P1a Linux runtime never sets them, but the scrub is platform-independent
  by design; TODO(P3): the Windows DLL search path has no DYLD equivalent
  and is out of scope).

The first-boot guard (:func:`assert_no_ambient_service_urls`) refuses to boot
when ambient ``DATABASE_URL``/``REDIS_URL`` are present — in the process
environment OR in the env file Settings will read (a dotenv-sourced URL
reaches Settings without appearing in ``os.environ``) — and the data dir has
no ``state.json`` yet: an inherited URL would silently redirect the bundled
services to a foreign endpoint (ADR 031 Decision 2: unconditional in P1a).
Once a ``state.json`` exists (first boot completed), explicit URLs are an
operator override path and are no longer refused by this guard.
"""

import os
from collections.abc import Callable
from pathlib import Path

from modulo.launcher.state import STATE_FILENAME

# Exact-name scrub list (ADR 031 Decision 2).
SCRUB_EXACT_NAMES: frozenset[str] = frozenset(
    {
        "REDISCLI_AUTH",
        "PYTHONPATH",
        "PYTHONHOME",
        "PYTHONUSERBASE",
        "LD_PRELOAD",
        "LD_LIBRARY_PATH",
        "LD_AUDIT",
        "OPENSSL_CONF",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
        "MODULO_TEST_PAUSE_AT",
    }
)

# Prefix scrub list: PG* (libpq client vars), DYLD_* (macOS loader) and LD_*
# (the whole ELF dynamic-loader family).
SCRUB_PREFIXES: tuple[str, ...] = ("PG", "DYLD_", "LD_")

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
    for name in [name for name in os.environ if _is_scrubbed(name)]:
        del os.environ[name]


def _ambient_urls_from_env_file(env_file: Path | str | None) -> dict[str, str]:
    """Parse the dotenv file Settings will read for ambient service URLs.

    Settings sources values from ``os.environ`` AND its env_file (the pinned
    launcher config, or the CWD ``.env`` when unpinned) — a URL injected only
    through the file would bypass an os.environ-only check. Blank lines and
    ``#`` comments are ignored; only non-empty values count (an empty value
    is not an endpoint).
    """
    if env_file is None:
        return {}
    try:
        text = Path(env_file).read_text(encoding="utf-8")
    except (OSError, ValueError):
        return {}
    found: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("'\"")
        if key in AMBIENT_SERVICE_URL_VARS and value:
            found[key] = value
    return found


def assert_no_ambient_service_urls(
    state_dir: Path,
    env: dict[str, str] | None = None,
    env_file: Path | str | None = None,
) -> None:
    """Refuse a first boot that inherits ambient service URLs.

    Unconditional in P1a (no override flag): when *state_dir* has no
    ``state.json`` yet, any ambient ``DATABASE_URL``/``REDIS_URL`` — in the
    process environment or in the *env_file* Settings will read — is a hard
    error whose message points the operator at the Docker Compose path (the
    supported way to run with explicit URLs).
    """
    source = dict(os.environ if env is None else env)
    ambient_env = [name for name in AMBIENT_SERVICE_URL_VARS if source.get(name)]
    dotenv_urls = _ambient_urls_from_env_file(env_file)
    ambient_file = [name for name in AMBIENT_SERVICE_URL_VARS if name in dotenv_urls]
    if not ambient_env and not ambient_file:
        return
    if (state_dir / STATE_FILENAME).exists():
        return
    origins = []
    if ambient_env:
        origins.append(", ".join(ambient_env) + " in the process environment")
    if ambient_file:
        origins.append(", ".join(ambient_file) + f" in the env file {env_file}")
    raise AmbientEnvironmentError(
        "MODULO first boot refused: ambient " + "; ".join(origins) + ". A native single-install first boot must "
        "not inherit service endpoints from the surrounding shell — they would "
        "silently redirect the bundled services to a foreign endpoint. Unset "
        "the variable(s) (or remove them from the env file) and start again, "
        "or use the Docker Compose path for explicit service URLs "
        "(docker compose -f docker-compose.yml up)."
    )


def make_first_boot_guard(state_dir: Path, env_file: Path | str | None = None) -> Callable[[], None]:
    """Build the hook installed into ``modulo.settings.set_first_boot_guard``.

    The hook runs at Settings validation time so ANY process constructing
    Settings through the launcher's pinned configuration fails fast on an
    un-bootstrapped data dir with ambient service URLs. *env_file* must be
    the same env_file the Settings construction will read (the pinned config
    path) — the dotenv source is otherwise invisible to the guard.
    """

    def _guard() -> None:
        assert_no_ambient_service_urls(state_dir, env_file=env_file)

    return _guard
