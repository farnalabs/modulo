"""Unit tests for modulo.db.session — the one-engine-per-process async engine factory.

QA lens pass (correctness, bugs, edge cases) on the process-global engine
factory. Every DB consumer gets its engine from here — the API via
``api.dependencies.get_or_create_engine``, the SAQ worker (per-worker pool
budget), ``cron_helpers``, and the per-item fire jobs — so a pool
misconfiguration is a production incident (connection exhaustion → 503/504).
The factory bakes in the Fly/HAProxy compat knobs (``pool_pre_ping``,
asyncpg ``statement_cache_size=0``, ``ssl=False``), sizes the pool from
settings (or the caller's override — first caller fixes it for the process),
and wires the process-global ORM listeners (append-only guard + tenant filter)
exactly once.

``test_dependencies.py`` covers the API path end-to-end; this package locks the
factory's own contract so the overrides, per-backend knobs and singleton
semantics can't silently drift:

  * **Pool sizing** — postgres/mariadb get 20/10/3600/30 defaults; a caller's
    ``pool_size``/``max_overflow`` wins; sqlite skips every pool knob
    (aiosqlite has no real pool).
  * **Per-backend connect args** — ``timeout=10`` on every backend; postgres
    additionally gets ``ssl=False`` + ``statement_cache_size=0`` (asyncpg /
    HAProxy compat); mariadb/sqlite do NOT get the asyncpg-only knobs.
  * **RLS reset hook** — registered on the engine for postgres only.
  * **Global hooks** — ``register_append_only_guard`` + ``register_tenant_filter``
    are process-global (engine-agnostic) and must run exactly once even under
    concurrent first-build races.
  * **Shared engine** — ``get_shared_engine`` is a lazily-built process
    singleton: the same instance is returned on every call, the first caller
    fixes the pool size (later overrides are ignored), and concurrent first
    calls build a single engine.
  * **Session factory** — ``AsyncSessionLocal`` binds to the module engine with
    ``expire_on_commit=False`` / ``autoflush=False`` / ``autobegin=False``.

Mock/fake based — no real database, engine, or connection required. The module
is imported lazily (its top level builds the module engine from settings), so a
missing ``DATABASE_URL`` only fails these tests, never collection.
"""

from threading import Barrier, Thread
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

POSTGRES_URL = "postgresql+asyncpg://u:p@localhost/db"
SQLITE_URL = "sqlite+aiosqlite:///./test.db"
MARIADB_URL = "mysql+aiomysql://u:p@localhost:3306/db"


@pytest.fixture
def session_mod() -> Any:
    """The module under test, with its mutable process state restored after use.

    ``_shared_engine`` and ``_GLOBAL_HOOKS_REGISTERED`` are module globals that
    tests intentionally reset (to exercise lazy-build / once-registration), so
    they are snapshotted and restored here to avoid leaking fake engines or a
    deregistered-hooks state into the rest of the suite.
    """
    import modulo.db.session as m

    orig_shared = m._shared_engine
    orig_hooks = m._GLOBAL_HOOKS_REGISTERED
    yield m
    m._shared_engine = orig_shared
    m._GLOBAL_HOOKS_REGISTERED = orig_hooks


def _settings(modulo_db: str, database_url: str) -> MagicMock:
    settings = MagicMock()
    settings.modulo_db = modulo_db
    settings.database_url = database_url
    return settings


# ---------------------------------------------------------------------------
# _build_engine — per-backend pool sizing + Fly/HAProxy connect knobs
# ---------------------------------------------------------------------------


class TestBuildEngine:
    def test_postgres_default_pool_and_haproxy_knobs(self, session_mod: Any) -> None:
        """Postgres gets pool_pre_ping, 20/10/3600/30 pool config, and the asyncpg
        HAProxy connect knobs (ssl=False, statement_cache_size=0)."""
        with (
            patch("modulo.db.session.get_settings", return_value=_settings("postgres", POSTGRES_URL)),
            patch("modulo.db.session.create_async_engine") as mock_create,
            patch("modulo.db.session.register_rls_reset_hook"),
        ):
            engine = session_mod._build_engine()

        mock_create.assert_called_once()
        kw = mock_create.call_args[1]
        assert kw["url"] == POSTGRES_URL
        assert kw["pool_pre_ping"] is True
        assert kw["pool_size"] == 20
        assert kw["max_overflow"] == 10
        assert kw["pool_recycle"] == 3600
        assert kw["pool_timeout"] == 30
        assert kw["connect_args"] == {"timeout": 10, "ssl": False, "statement_cache_size": 0}
        assert mock_create.return_value is engine

    def test_postgres_pool_overrides_win(self, session_mod: Any) -> None:
        """A caller's pool budget (the SAQ worker's per-worker size) wins."""
        with (
            patch("modulo.db.session.get_settings", return_value=_settings("postgres", POSTGRES_URL)),
            patch("modulo.db.session.create_async_engine") as mock_create,
            patch("modulo.db.session.register_rls_reset_hook"),
        ):
            session_mod._build_engine(pool_size=5, max_overflow=2)

        kw = mock_create.call_args[1]
        assert kw["pool_size"] == 5
        assert kw["max_overflow"] == 2

    def test_partial_override_preserves_defaults(self, session_mod: Any) -> None:
        """pool_size=None keeps the 20 default while max_overflow override applies."""
        with (
            patch("modulo.db.session.get_settings", return_value=_settings("postgres", POSTGRES_URL)),
            patch("modulo.db.session.create_async_engine") as mock_create,
            patch("modulo.db.session.register_rls_reset_hook"),
        ):
            session_mod._build_engine(pool_size=None, max_overflow=3)

        kw = mock_create.call_args[1]
        assert kw["pool_size"] == 20
        assert kw["max_overflow"] == 3

    def test_sqlite_skips_every_pool_knob(self, session_mod: Any) -> None:
        """SQLite (aiosqlite) has no real pool — no pool_size/max_overflow/recycle/timeout."""
        with (
            patch("modulo.db.session.get_settings", return_value=_settings("sqlite", SQLITE_URL)),
            patch("modulo.db.session.create_async_engine") as mock_create,
            patch("modulo.db.session.register_rls_reset_hook"),
        ):
            session_mod._build_engine(pool_size=5, max_overflow=2)

        kw = mock_create.call_args[1]
        for knob in ("pool_size", "max_overflow", "pool_recycle", "pool_timeout"):
            assert knob not in kw, f"sqlite engine must not receive {knob}"
        assert kw["pool_pre_ping"] is True

    def test_sqlite_connect_args_are_timeout_only(self, session_mod: Any) -> None:
        """SQLite keeps the timeout connect arg but not the asyncpg-only knobs."""
        with (
            patch("modulo.db.session.get_settings", return_value=_settings("sqlite", SQLITE_URL)),
            patch("modulo.db.session.create_async_engine") as mock_create,
            patch("modulo.db.session.register_rls_reset_hook"),
        ):
            session_mod._build_engine()

        kw = mock_create.call_args[1]
        assert kw["connect_args"] == {"timeout": 10}

    def test_mariadb_pool_knobs_without_asyncpg_knobs(self, session_mod: Any) -> None:
        """MariaDB gets a real pool but must NOT get the asyncpg-only ssl/statement_cache args."""
        with (
            patch("modulo.db.session.get_settings", return_value=_settings("mariadb", MARIADB_URL)),
            patch("modulo.db.session.create_async_engine") as mock_create,
            patch("modulo.db.session.register_rls_reset_hook"),
        ):
            session_mod._build_engine()

        kw = mock_create.call_args[1]
        assert kw["pool_size"] == 20
        assert kw["max_overflow"] == 10
        assert kw["pool_recycle"] == 3600
        assert kw["pool_timeout"] == 30
        assert kw["connect_args"] == {"timeout": 10}

    def test_mysql_alias_matches_mariadb(self, session_mod: Any) -> None:
        """The mysql alias is treated exactly like mariadb."""
        with (
            patch("modulo.db.session.get_settings", return_value=_settings("mysql", MARIADB_URL)),
            patch("modulo.db.session.create_async_engine") as mock_create,
            patch("modulo.db.session.register_rls_reset_hook"),
        ):
            session_mod._build_engine()

        kw = mock_create.call_args[1]
        assert kw["connect_args"] == {"timeout": 10}
        assert "ssl" not in kw["connect_args"]
        assert "statement_cache_size" not in kw["connect_args"]

    def test_every_backend_gets_timeout_connect_arg(self, session_mod: Any) -> None:
        """timeout=10 is the one connect arg shared by all backends."""
        for modulo_db, url in (("postgres", POSTGRES_URL), ("sqlite", SQLITE_URL), ("mariadb", MARIADB_URL)):
            with (
                patch("modulo.db.session.get_settings", return_value=_settings(modulo_db, url)),
                patch("modulo.db.session.create_async_engine") as mock_create,
                patch("modulo.db.session.register_rls_reset_hook"),
            ):
                session_mod._build_engine()
            assert mock_create.call_args[1]["connect_args"]["timeout"] == 10

    def test_engine_built_from_settings(self, session_mod: Any) -> None:
        """The URL and backend come from the process settings, not hard-coded."""
        with (
            patch("modulo.db.session.get_settings", return_value=_settings("postgres", POSTGRES_URL)),
            patch("modulo.db.session.create_async_engine") as mock_create,
            patch("modulo.db.session.register_rls_reset_hook"),
        ):
            session_mod._build_engine()
        assert mock_create.call_args[1]["url"] == POSTGRES_URL


# ---------------------------------------------------------------------------
# RLS reset hook — engine-scoped, postgres only
# ---------------------------------------------------------------------------


class TestRlsResetHookRegistration:
    def test_registered_for_postgres(self, session_mod: Any) -> None:
        """The pool-checkout RLS reset hook is registered on the postgres engine."""
        with (
            patch("modulo.db.session.get_settings", return_value=_settings("postgres", POSTGRES_URL)),
            patch("modulo.db.session.create_async_engine", return_value=MagicMock()) as mock_create,
            patch("modulo.db.session.register_rls_reset_hook") as mock_hook,
        ):
            engine = session_mod._build_engine()

        mock_hook.assert_called_once_with(engine)
        assert mock_hook.call_args[0][0] is mock_create.return_value

    @pytest.mark.parametrize(
        ("modulo_db", "url"),
        [
            pytest.param("sqlite", SQLITE_URL, id="sqlite"),
            pytest.param("mariadb", MARIADB_URL, id="mariadb"),
            pytest.param("mysql", MARIADB_URL, id="mysql"),
        ],
    )
    def test_skipped_for_non_postgres(self, session_mod: Any, modulo_db: str, url: str) -> None:
        """The RLS reset hook is a Postgres pool concern — never registered elsewhere."""
        with (
            patch("modulo.db.session.get_settings", return_value=_settings(modulo_db, url)),
            patch("modulo.db.session.create_async_engine"),
            patch("modulo.db.session.register_rls_reset_hook") as mock_hook,
        ):
            session_mod._build_engine()

        mock_hook.assert_not_called()


# ---------------------------------------------------------------------------
# _register_global_hooks_once — process-global ORM listeners, exactly once
# ---------------------------------------------------------------------------


class TestRegisterGlobalHooksOnce:
    def test_registers_both_global_hooks_on_first_call(self, session_mod: Any) -> None:
        """append-only guard + tenant filter are registered together the first time."""
        session_mod._GLOBAL_HOOKS_REGISTERED = False
        with (
            patch("modulo.db.session.register_append_only_guard") as guard,
            patch("modulo.db.session.register_tenant_filter") as tenant,
        ):
            session_mod._register_global_hooks_once()

        guard.assert_called_once()
        tenant.assert_called_once()
        assert session_mod._GLOBAL_HOOKS_REGISTERED is True

    def test_second_call_is_a_noop(self, session_mod: Any) -> None:
        """Registering twice (e.g. a second engine build) must not re-fire the listeners."""
        session_mod._GLOBAL_HOOKS_REGISTERED = False
        with (
            patch("modulo.db.session.register_append_only_guard") as guard,
            patch("modulo.db.session.register_tenant_filter") as tenant,
        ):
            session_mod._register_global_hooks_once()
            session_mod._register_global_hooks_once()

        guard.assert_called_once()
        tenant.assert_called_once()

    def test_rearm_after_reset_registers_again(self, session_mod: Any) -> None:
        """Resetting the flag re-arms registration (used by tests/reloads)."""
        session_mod._GLOBAL_HOOKS_REGISTERED = False
        with (
            patch("modulo.db.session.register_append_only_guard") as guard,
            patch("modulo.db.session.register_tenant_filter") as tenant,
        ):
            session_mod._register_global_hooks_once()
            session_mod._GLOBAL_HOOKS_REGISTERED = False
            session_mod._register_global_hooks_once()

        assert guard.call_count == 2
        assert tenant.call_count == 2

    def test_concurrent_first_calls_register_once(self, session_mod: Any) -> None:
        """Concurrent first builds register each listener exactly once (lock-guarded)."""
        session_mod._GLOBAL_HOOKS_REGISTERED = False
        barrier = Barrier(2)
        guard = MagicMock()
        tenant = MagicMock()

        def _register() -> None:
            barrier.wait()
            session_mod._register_global_hooks_once()

        with (
            patch.object(session_mod, "register_append_only_guard", guard),
            patch.object(session_mod, "register_tenant_filter", tenant),
        ):
            threads = [Thread(target=_register) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)
                assert not thread.is_alive(), "worker thread failed to finish within 5s"

        assert guard.call_count == 1
        assert tenant.call_count == 1
        assert session_mod._GLOBAL_HOOKS_REGISTERED is True

    def test_build_engine_invokes_global_hook_registration(self, session_mod: Any) -> None:
        """Every engine build ensures the process-global hooks are registered."""
        with (
            patch("modulo.db.session.get_settings", return_value=_settings("sqlite", SQLITE_URL)),
            patch("modulo.db.session.create_async_engine"),
            patch("modulo.db.session.register_rls_reset_hook"),
            patch.object(session_mod, "_register_global_hooks_once") as mock_hooks,
        ):
            session_mod._build_engine()

        mock_hooks.assert_called_once()


# ---------------------------------------------------------------------------
# get_shared_engine — lazy process singleton, first caller fixes the pool
# ---------------------------------------------------------------------------


class TestGetSharedEngine:
    def test_returns_same_engine_for_every_call(self, session_mod: Any) -> None:
        """The shared engine is a process singleton — every caller gets the same instance."""
        session_mod._shared_engine = None
        with patch.object(session_mod, "_build_engine", return_value=MagicMock()) as mock_build:
            first = session_mod.get_shared_engine()
            second = session_mod.get_shared_engine()

        assert first is second
        mock_build.assert_called_once()

    def test_first_caller_fixes_pool_size(self, session_mod: Any) -> None:
        """The first caller's pool budget wins; a later override is silently ignored."""
        session_mod._shared_engine = None
        with patch.object(session_mod, "_build_engine", return_value=MagicMock()) as mock_build:
            first = session_mod.get_shared_engine(pool_size=5, max_overflow=2)
            second = session_mod.get_shared_engine(pool_size=100, max_overflow=50)

        assert first is second
        mock_build.assert_called_once_with(pool_size=5, max_overflow=2)

    def test_lazy_build_only_on_first_call(self, session_mod: Any) -> None:
        """No engine is built until the first call; the factory is not invoked for later calls."""
        session_mod._shared_engine = None
        with patch.object(session_mod, "_build_engine", return_value=MagicMock()) as mock_build:
            session_mod.get_shared_engine()
            session_mod.get_shared_engine()
            session_mod.get_shared_engine()

        assert mock_build.call_count == 1

    def test_reset_then_rebuild_creates_new_engine(self, session_mod: Any) -> None:
        """Resetting the singleton (tests, teardown) forces a fresh engine on next call."""
        session_mod._shared_engine = None
        first = MagicMock()
        second = MagicMock()
        with patch.object(session_mod, "_build_engine", side_effect=[first, second]) as mock_build:
            assert session_mod.get_shared_engine() is first
            session_mod._shared_engine = None
            assert session_mod.get_shared_engine() is second

        assert mock_build.call_count == 2

    def test_concurrent_first_calls_build_one_engine(self, session_mod: Any) -> None:
        """Two threads racing the first call both see the SAME engine, built once."""
        session_mod._shared_engine = None
        barrier = Barrier(2)
        results: list[MagicMock] = []

        def _call() -> None:
            barrier.wait()
            results.append(session_mod.get_shared_engine())

        fake = MagicMock()
        with patch.object(session_mod, "_build_engine", return_value=fake) as mock_build:
            threads = [Thread(target=_call) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)
                assert not thread.is_alive(), "worker thread failed to finish within 5s"

        assert mock_build.call_count == 1
        assert results[0] is fake
        assert results[1] is fake


# ---------------------------------------------------------------------------
# AsyncSessionLocal + module engine — the default session factory contract
# ---------------------------------------------------------------------------


class TestAsyncSessionLocal:
    def test_bound_to_module_engine(self, session_mod: Any) -> None:
        """AsyncSessionLocal sessions come from the module-level engine."""
        assert session_mod.AsyncSessionLocal.kw["bind"] is session_mod.engine

    def test_session_config_flags(self, session_mod: Any) -> None:
        """The default factory locks the safe-session contract: no autobegin/autocommit/autoflush,
        no expire-on-commit, AsyncSession class."""
        kw = session_mod.AsyncSessionLocal.kw
        assert session_mod.AsyncSessionLocal.class_ is AsyncSession
        assert kw["autobegin"] is False
        assert kw["autocommit"] is False
        assert kw["autoflush"] is False
        assert kw["expire_on_commit"] is False

    def test_module_engine_is_an_async_engine(self, session_mod: Any) -> None:
        """The import-time engine is a real SQLAlchemy AsyncEngine built by the factory."""
        from sqlalchemy.ext.asyncio import AsyncEngine

        assert isinstance(session_mod.engine, AsyncEngine)
