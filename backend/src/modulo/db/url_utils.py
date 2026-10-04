"""Zero-dependency database-URL helpers shared by every boot path (FAR-671).

This is a LEAF module: stdlib only, no SQLAlchemy, no ``modulo`` imports, no
logging side effects. It is importable from the container entrypoint
(``deploy/fly/bootstrap_db.py``), the native launcher (ADR 031), and
``modulo.settings`` — the single implementation of the boot URL contract.

Unified semantics (FAR-1440):

* ``deploy/fly/bootstrap_db.py`` historically rewrote ``postgres://`` to the
  asyncpg scheme and stripped **every** ``sslmode`` parameter (asyncpg 0.24
  did not understand ``sslmode`` in URLs; on Fly's private networks the
  driver's SSL default caused ``ConnectionResetError``).
* ``modulo.settings`` rewrote the same scheme PLUS the legacy
  ``mysql+asyncmy`` driver prefix, but stripped only ``sslmode=disable``.

The current posture (FAR-1440): an operator's explicit ``sslmode`` on a
Postgres-family URL is HONOURED, not silently stripped — the docs
(``docs/deployment-security.md`` §3.2) recommend ``sslmode=require``, so the
code must accept it. ``fix_database_url`` therefore only strips ``sslmode``
from MySQL-family URLs (the aiomysql driver rejects ``sslmode``); Postgres
URLs keep the parameter and consumers translate it themselves:

* the SQLAlchemy engine factory (:mod:`modulo.db.session` and the system-role
  engines) extracts ``sslmode`` via :func:`split_postgres_sslmode` and passes
  the translated value through asyncpg's ``ssl`` connect arg;
* ``modulo.api.dependencies.pg_connection_string`` copies the parameter into
  the psycopg-compatible URL the checkpointer/migration path uses (psycopg
  honours ``sslmode`` natively).

Absent ``sslmode`` still means explicit plaintext (``ssl=False``) — never the
driver default, which on Fly's private networks caused
``ConnectionResetError``, and never a silent-downgrade mode: asyncpg's
``sslmode=prefer``/``allow`` fail OPEN (CERT_NONE when the server refuses
TLS), so :func:`split_postgres_sslmode` REJECTS them at boot instead.

One deliberate narrowing: the historical deploy variant rewrote
``postgres://`` wherever it first appeared in the string (``str.replace``),
while this implementation only rewrites a ``postgres://`` PREFIX (matching the
historical ``modulo.settings`` variant, which used ``startswith``). A URL with
``postgres://`` embedded after the scheme (e.g. inside a password) is not a
real Postgres URL; prefix-only rewriting is strictly safer and matches the
Settings contract.
"""

import re
from urllib.parse import parse_qsl, urlsplit, urlunsplit

# SSL modes asyncpg recognises (SSLMode enum). ``require``/``verify-ca``/
# ``verify-full`` fail closed in asyncpg (the connection is refused unless the
# server negotiates TLS); ``allow``/``prefer`` downgrade silently to plaintext
# (CERT_NONE) and are never accepted by :func:`split_postgres_sslmode`.
_REQUIRE_TLS_SSLMODES = frozenset({"require", "verify-ca", "verify-full"})
_PLAINTEXT_SSLMODES = frozenset({"disable"})

# Strip any sslmode parameter from MySQL-family URLs only (aiomysql rejects
# sslmode); Postgres URLs keep theirs (FAR-1440 — honoured upstream).
SSL_MODE_PARAM_RE = re.compile(r"[?&]sslmode=[^&]*")

_POSTGRES_PREFIX = "postgres://"
_POSTGRES_ASYNC_PREFIX = "postgresql+asyncpg://"
_MYSQL_ASYNCMY_PREFIX = "mysql+asyncmy://"
_MYSQL_AIOMYSQL_PREFIX = "mysql+aiomysql://"


def fix_database_url(url: str) -> str:
    """Rewrite legacy driver prefixes; strip ``sslmode`` only where it is alien.

    * ``postgres://`` → ``postgresql+asyncpg://`` (prefix only)
    * ``mysql+asyncmy://`` → ``mysql+aiomysql://`` (prefix only)
    * ``sslmode`` is PRESERVED on Postgres-family URLs (FAR-1440: an
      operator's explicit TLS setting is honoured, translated to asyncpg's
      ``ssl`` connect arg by the engine factory) and stripped from
      MySQL-family URLs (aiomysql does not accept ``sslmode``). When the strip
      leaves other params behind, the remaining param keeps a leading ``&``
      when ``sslmode`` was FIRST (a wart carried from the historical deploy
      variant, kept only on the MySQL path now).
    """
    fixed = url
    if fixed.startswith(_POSTGRES_PREFIX):
        fixed = _POSTGRES_ASYNC_PREFIX + fixed[len(_POSTGRES_PREFIX) :]
    elif fixed.startswith(_MYSQL_ASYNCMY_PREFIX):
        fixed = _MYSQL_AIOMYSQL_PREFIX + fixed[len(_MYSQL_ASYNCMY_PREFIX) :]
    if fixed.startswith(_MYSQL_AIOMYSQL_PREFIX):
        return SSL_MODE_PARAM_RE.sub("", fixed).rstrip("?")
    return fixed


def split_postgres_sslmode(url: str) -> tuple[str, str | bool]:
    """Extract ``sslmode`` from a Postgres URL into an asyncpg ``ssl`` value.

    Returns ``(url_without_sslmode, ssl)`` where ``ssl`` is the value to pass
    to asyncpg as the ``ssl`` connect arg:

    * absent / ``sslmode=disable`` → ``False`` (explicit plaintext — never the
      driver default, which breaks non-TLS listeners such as Fly's internal
      Postgres with ``ConnectionResetError``);
    * ``require`` / ``verify-ca`` / ``verify-full`` → the same string (asyncpg
      builds an SSLContext that FAILS CLOSED: the connection is refused unless
      the server negotiates TLS);
    * ``prefer`` / ``allow`` → ``ValueError``. asyncpg translates these to an
      SSLContext with ``CERT_NONE`` that silently downgrades to plaintext when
      the server refuses TLS — a security posture we do not permit; set
      ``require`` (or ``verify-*``) explicitly instead;
    * any other value → ``ValueError`` rather than a driver-side guess.

    ``url_without_sslmode`` is rebuilt cleanly (a mid-param ``sslmode`` is
    removed without leaving a stray ``&``). Only Postgres/asyncpg schemes are
    accepted — callers gate on the backend, and any other scheme is refused
    loudly rather than silently passing a bogus ``ssl`` arg.
    """
    parts = urlsplit(url)
    scheme_ok = parts.scheme in {"postgres", "postgresql", "postgresql+asyncpg", "postgresql+psycopg"}
    if not scheme_ok:
        raise ValueError(f"split_postgres_sslmode expects a Postgres URL, got scheme {parts.scheme!r}")
    ssl: str | bool = False
    kept_raw: list[str] = []
    for raw_item in parts.query.split("&") if parts.query else []:
        parsed_item = parse_qsl(raw_item, keep_blank_values=True)
        key = parsed_item[0][0] if parsed_item else raw_item
        if key == "sslmode":
            raw_value = parsed_item[0][1] if len(parsed_item[0]) == 2 else ""
            mode = raw_value.strip().lower().replace("_", "-")
            if mode in _PLAINTEXT_SSLMODES:
                ssl = False
            elif mode in _REQUIRE_TLS_SSLMODES:
                ssl = mode
            else:
                raise ValueError(
                    f"Unsupported sslmode={raw_value!r}: use disable, require, "
                    "verify-ca or verify-full (prefer/allow silently downgrade "
                    "to plaintext and are not permitted)"
                )
        else:
            kept_raw.append(raw_item)
    clean = parts._replace(query="&".join(kept_raw)).geturl()
    return clean, ssl


def split_engine_sslmode(url: str) -> tuple[str, str | bool | None]:
    """Translate ``sslmode`` for a SQLAlchemy async engine, or leave it alone.

    The ONE gate every asyncpg engine factory routes through (FAR-1440): a
    Postgres-family URL is split by :func:`split_postgres_sslmode`; any other
    scheme is returned unchanged with ``None``, signalling "not an asyncpg
    engine URL — pass no ``ssl``/``statement_cache_size`` connect args" so
    SQLite/MySQL boot paths keep their driver defaults.

    Callers MUST pass ``ssl`` only when it is not ``None``::

        engine_url, ssl = split_engine_sslmode(raw_url)
        if ssl is not None:
            connect_args["ssl"] = ssl
            connect_args["statement_cache_size"] = 0

    Routing every factory through this one function is what keeps the TLS
    posture uniform: a factory that does its own scheme check can silently
    drift (the FAR-1440 defect, where several orphan factories kept passing a
    preserved ``sslmode`` straight to asyncpg and raised ``TypeError`` at
    first connect).
    """
    if not urlsplit(url).scheme.startswith("postgres"):
        return url, None
    return split_postgres_sslmode(url)


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
