"""Zero-dependency database-URL helpers shared by every boot path (FAR-671).

This is a LEAF module: stdlib only, no SQLAlchemy, no ``modulo`` imports, no
logging side effects. It is importable from the container entrypoint
(``deploy/fly/bootstrap_db.py``), the native launcher (ADR 031), and
``modulo.settings`` — the single implementation of the boot URL contract.

Unified semantics (reconciles the two historical variants):

* ``deploy/fly/bootstrap_db.py`` rewrote ``postgres://`` to the asyncpg scheme
  and stripped **every** ``sslmode`` parameter (asyncpg does not understand
  ``sslmode`` in URLs; on Fly's private networks the driver's SSL default
  caused ``ConnectionResetError``).
* ``modulo.settings`` rewrote the same scheme PLUS the legacy
  ``mysql+asyncmy`` driver prefix, but stripped only ``sslmode=disable``.

The unified behaviour is the safer SUPERSET of both: rewrite both legacy
prefixes and strip ALL ``sslmode`` parameters regardless of value. Stripping
is always safe because the SQLAlchemy engine sets the connection SSL posture
explicitly through ``connect_args`` (``modulo.api.dependencies
.get_or_create_engine`` MUST keep setting ``ssl=False`` for Postgres to match
this module's assumption), so an ``sslmode=require`` that reaches the driver
is never honoured — it can only break URL parsing. Both historical call sites
behave identically on their locked fixture inputs; the only visible change is
that a Settings URL carrying a non-``disable`` ``sslmode`` is now cleaned
instead of being passed through to the driver.

One deliberate narrowing: the historical deploy variant rewrote
``postgres://`` wherever it first appeared in the string (``str.replace``),
while this implementation only rewrites a ``postgres://`` PREFIX (matching the
historical ``modulo.settings`` variant, which used ``startswith``). A URL with
``postgres://`` embedded after the scheme (e.g. inside a password) is not a
real Postgres URL; prefix-only rewriting is strictly safer and matches the
Settings contract.
"""

import re
from urllib.parse import urlsplit, urlunsplit

# Strip any sslmode parameter (disable, require, prefer, verify-full, ...) —
# asyncpg rejects sslmode in URLs and the engine sets SSL via connect_args.
SSL_MODE_PARAM_RE = re.compile(r"[?&]sslmode=[^&]*")

_POSTGRES_PREFIX = "postgres://"
_POSTGRES_ASYNC_PREFIX = "postgresql+asyncpg://"
_MYSQL_ASYNCMY_PREFIX = "mysql+asyncmy://"
_MYSQL_AIOMYSQL_PREFIX = "mysql+aiomysql://"


def fix_database_url(url: str) -> str:
    """Rewrite legacy driver prefixes and strip every sslmode parameter.

    * ``postgres://`` → ``postgresql+asyncpg://`` (prefix only)
    * ``mysql+asyncmy://`` → ``mysql+aiomysql://`` (prefix only)
    * every ``sslmode`` query parameter is removed; a trailing ``?`` left by
      the removal is trimmed. Known wart carried from the historical deploy
      variant (locked by characterization tests): when ``sslmode`` is the
      FIRST query param and others follow, the ``?`` is removed but the
      remaining params keep their leading ``&`` (e.g.
      ``.../db?sslmode=require&connect_timeout=10`` →
      ``.../db&connect_timeout=10``).
    """
    fixed = url
    if fixed.startswith(_POSTGRES_PREFIX):
        fixed = _POSTGRES_ASYNC_PREFIX + fixed[len(_POSTGRES_PREFIX) :]
    elif fixed.startswith(_MYSQL_ASYNCMY_PREFIX):
        fixed = _MYSQL_AIOMYSQL_PREFIX + fixed[len(_MYSQL_ASYNCMY_PREFIX) :]
    return SSL_MODE_PARAM_RE.sub("", fixed).rstrip("?")


def derive_system_database_url(runtime_url: str) -> str:
    """Derive the modulo_system URL from the runtime DATABASE_URL.

    Swaps the username to ``modulo_system``, preserving the password (so
    ``modulo.db.bootstrap_role`` can create the role with the same
    credential), the host/port, the scheme, and any query params. Returns an
    empty string when the netloc has no ``@`` separator (no userinfo), when
    the userinfo has no password, or when the password is explicitly empty
    (``user:@host``) — in all three cases the caller falls back to modulo_app
    rather than wiring a modulo_system URL whose credential could never match
    the role bootstrap would create (it seeds ``secrets.token_urlsafe`` when
    the URL carries no password).
    """
    parts = urlsplit(runtime_url)
    userinfo, sep, hostport = parts.netloc.rpartition("@")
    if sep:
        _, _, password = userinfo.partition(":")
        if password:
            new_userinfo = f"modulo_system:{password}"
            parts = parts._replace(netloc=f"{new_userinfo}@{hostport}")
            return urlunsplit(parts)
    return ""
