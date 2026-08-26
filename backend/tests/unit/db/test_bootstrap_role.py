"""Unit tests for modulo.db.bootstrap_role — the break-glass role bootstrap.

Hermetic suite: no Postgres required. A fake asyncpg connection routes
responses by SQL shape — role-existence probes, to_regclass guards,
information_schema introspection, has_column_privilege, role-table grants,
superuser probe, and pg_auth_members — and records every executed statement so
the allow-list posture assertions (``_find_allow_list_violations``,
``_assert_role_posture``) can be exercised end-to-end in clean and violated
states.
"""

from __future__ import annotations

import re
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from modulo.db.bootstrap_role import (
    _MIGRATE_ROLE,
    _SYSTEM_ROLE,
    ACCOUNTS_WRITABLE_COLUMNS,
    REQUIRED_VARS,
    _apply_accounts_allow_list,
    _assert_role_posture,
    _asyncpg_admin_connect,
    _bootstrap,
    _create_or_update_role,
    _existing_columns,
    _find_allow_list_violations,
    _grant_break_glass,
    _grant_function_execute,
    _parse_password,
    _parse_role,
    _table_exists,
    bootstrap_roles,
    main,
)

_BREAK_GLASS_COLS = ("is_break_glass", "break_glass_expires_at", "break_glass_deactivated_at")


def _params(query: str) -> list[str]:
    """Extract the bound parameters from an asyncpg query (``$1`` etc.)."""
    return re.findall(r"\$([0-9]+)", query)


class _FakeConn:
    """asyncpg.Connection stand-in that routes responses by SQL content.

    Configuration:
      * ``roles``           — dict[str, bool]: rolname -> exists (pg_roles probe)
      * ``tables``          — set[str]: existing public tables (to_regclass)
      * ``columns``         — dict[str, set[str]]: table -> existing columns
      * ``table_update_grantees`` — list[str]: role_table_grants UPDATE grantees
      * ``column_updatable``      — set[str]: accounts columns modulo_app may UPDATE
      * ``is_superuser``          — bool
      * ``privileged_memberships``— list[str]: privileged roles app belongs to
      * ``function_exists``       — bool: deactivate_break_glass present
      * ``sql_hook``              — callable[str]: called for every executed statement
    """

    def __init__(self, **options: Any) -> None:
        self.roles: dict[str, bool] = options.get("roles", {})
        self.role_bypassrls: dict[str, bool] = options.get("role_bypassrls", {})
        self.tables: set[str] = set(options.get("tables", set()))
        self.columns: dict[str, set[str]] = options.get("columns", {})
        self.table_update_grantees: list[str] = options.get("table_update_grantees", [])
        self.column_updatable: set[str] = set(options.get("column_updatable", set()))
        self.is_superuser: bool = options.get("is_superuser", False)
        self.privileged_memberships: list[str] = options.get("privileged_memberships", [])
        self.function_exists: bool = options.get("function_exists", True)
        self.sql_hook: Any = options.get("sql_hook")
        self.executed: list[str] = []
        self.closed = False

    def _record(self, query: str) -> None:
        normalised = re.sub(r"\$[0-9]+", "?", query)
        self.executed.append(normalised)
        if self.sql_hook is not None:
            self.sql_hook(normalised)

    async def fetchval(self, query: str, *args: Any) -> Any:
        self._record(query)
        lowered = query.lower()
        if lowered.startswith("select 1 from pg_roles where rolname"):
            return self.roles.get(args[0] if args else "", False)
        if "to_regclass(" in lowered:
            table = (args[0] if args else "").replace("public.", "")
            return table in self.tables
        if "to_regprocedure" in lowered:
            return self.function_exists
        if lowered.startswith("select has_column_privilege"):
            col = args[1] if len(args) > 1 else ""
            return col in self.column_updatable
        if "select rolsuper from pg_roles" in lowered:
            return self.is_superuser
        if "rolbypassrls from pg_roles" in lowered:
            role_name = args[0] if args else ""
            return self.role_bypassrls.get(role_name, True)
        return None

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        self._record(query)
        lowered = query.lower()
        if "from information_schema.columns" in lowered:
            table = args[0] if args else ""
            return [{"column_name": c} for c in self.columns.get(table, set())]
        if "from information_schema.role_table_grants" in lowered:
            return [{"grantee": g} for g in self.table_update_grantees]
        if "from pg_auth_members" in lowered:
            return [{"rolname": r} for r in self.privileged_memberships]
        return []

    async def execute(self, query: str, *args: Any) -> str:
        self._record(query)
        return ""

    async def close(self) -> None:
        self.closed = True


def _full_accounts_columns() -> set[str]:
    cols = set(ACCOUNTS_WRITABLE_COLUMNS)
    cols.update({"id", *_BREAK_GLASS_COLS})
    return cols


def _clean_posture_conn() -> _FakeConn:
    """A connection that yields a perfectly clean allow-list posture."""
    return _FakeConn(
        roles={
            "modulo_app": True,
            _MIGRATE_ROLE: True,
            "modulo_breakglass": True,
            _SYSTEM_ROLE: True,
        },
        role_bypassrls={
            "modulo_app": False,
            _SYSTEM_ROLE: True,
        },
        tables={"accounts"},
        columns={"accounts": _full_accounts_columns()},
        column_updatable=set(ACCOUNTS_WRITABLE_COLUMNS),
        is_superuser=False,
        privileged_memberships=[],
    )


@pytest.fixture
def conn() -> _FakeConn:
    return _FakeConn()


# ---------------------------------------------------------------------------
# URL parsing helpers
# ---------------------------------------------------------------------------


class TestUrlParsing:
    def test_parse_role_extracts_username(self) -> None:
        assert _parse_role("postgres://modulo:pass@db:5432/modulo") == "modulo"

    def test_parse_role_empty_when_no_username(self) -> None:
        assert not _parse_role("postgres://db:5432/modulo")

    def test_parse_password_extracts_and_unescapes(self) -> None:
        assert _parse_password("postgres://modulo:p%40ss%23word@db:5432/modulo") == "p@ss#word"

    def test_parse_password_empty_when_none(self) -> None:
        assert not _parse_password("postgres://modulo@db:5432/modulo")


class TestAsyncpgAdminConnect:
    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            (
                "postgresql+asyncpg://admin:pw@db:5432/modulo",
                ("postgres://admin:pw@db:5432/modulo", False),
            ),
            (
                "postgres://admin:pw@db/modulo",
                ("postgres://admin:pw@db/modulo", False),
            ),
            (
                "postgres://u:p@h/db?sslmode=require",
                ("postgres://u:p@h/db", "require"),
            ),
            (
                "postgres://u:p@h/db?sslmode=verify-ca",
                ("postgres://u:p@h/db", "verify-ca"),
            ),
            (
                "postgres://u:p@h/db?sslmode=verify-full",
                ("postgres://u:p@h/db", "verify-full"),
            ),
            (
                "postgres://u:p@h/db?sslmode=disable",
                ("postgres://u:p@h/db", False),
            ),
            (
                "postgres://u:p@h/db?sslmode=prefer",
                ("postgres://u:p@h/db", False),
            ),
            (
                "postgres://u:p@h/db?sslmode=require&application_name=modulo",
                ("postgres://u:p@h/db", "require"),
            ),
            (
                "postgres://u:p@h:5433/db?sslmode=require",
                ("postgres://u:p@h:5433/db", "require"),
            ),
        ],
        ids=[
            "asyncpg-full",
            "postgres-default",
            "ssl-require",
            "ssl-verify-ca",
            "ssl-verify-full",
            "ssl-disable",
            "ssl-prefer",
            "ssl-require-extra-params",
            "ssl-require-custom-port",
        ],
    )
    def test_asyncpg_admin_connect(self, url: str, expected: tuple[str, bool | str]) -> None:
        assert _asyncpg_admin_connect(url) == expected


# ---------------------------------------------------------------------------
# _create_or_update_role
# ---------------------------------------------------------------------------


class TestCreateOrUpdateRole:
    async def test_creates_login_role_when_missing(self, conn: _FakeConn) -> None:
        await _create_or_update_role(conn, "modulo_app", login=True, password="pw")
        assert any('CREATE ROLE "modulo_app" LOGIN BYPASSRLS PASSWORD' in q for q in conn.executed)

    async def test_creates_nologin_role_when_missing(self, conn: _FakeConn) -> None:
        await _create_or_update_role(conn, _MIGRATE_ROLE, login=False, password=None)
        assert any('CREATE ROLE "modulo_migrate" NOSUPERUSER NOLOGIN BYPASSRLS' in q for q in conn.executed)

    async def test_updates_existing_login_role(self, conn: _FakeConn) -> None:
        conn.roles["modulo_app"] = True
        await _create_or_update_role(conn, "modulo_app", login=True, password="newpw")
        assert any('ALTER ROLE "modulo_app" WITH LOGIN BYPASSRLS PASSWORD' in q for q in conn.executed)
        assert not any(q.startswith("CREATE ROLE") for q in conn.executed)

    async def test_updates_existing_nologin_role(self, conn: _FakeConn) -> None:
        conn.roles[_MIGRATE_ROLE] = True
        await _create_or_update_role(conn, _MIGRATE_ROLE, login=False, password=None)
        assert any('ALTER ROLE "modulo_migrate" WITH NOSUPERUSER NOLOGIN BYPASSRLS' in q for q in conn.executed)

    async def test_escapes_single_quotes_in_password(self, conn: _FakeConn) -> None:
        await _create_or_update_role(conn, "modulo_app", login=True, password="p'word")
        escaped = [q for q in conn.executed if "CREATE ROLE" in q]
        assert escaped and "p''word" in escaped[0]


# ---------------------------------------------------------------------------
# _table_exists / _existing_columns
# ---------------------------------------------------------------------------


class TestIntrospection:
    async def test_table_exists_truthy(self, conn: _FakeConn) -> None:
        conn.tables = {"accounts"}
        assert await _table_exists(conn, "accounts") is True

    async def test_table_exists_falsy(self, conn: _FakeConn) -> None:
        assert await _table_exists(conn, "accounts") is False

    async def test_existing_columns_returns_names(self, conn: _FakeConn) -> None:
        conn.columns = {"accounts": {"id", "email", "password_hash"}}
        assert await _existing_columns(conn, "accounts") == {"id", "email", "password_hash"}

    async def test_existing_columns_empty_table_returns_empty_set(self, conn: _FakeConn) -> None:
        assert not await _existing_columns(conn, "accounts")


# ---------------------------------------------------------------------------
# _apply_accounts_allow_list
# ---------------------------------------------------------------------------


class TestApplyAccountsAllowList:
    async def test_skips_when_accounts_missing(self, conn: _FakeConn) -> None:
        await _apply_accounts_allow_list(conn, "modulo_app")
        assert not any("GRANT UPDATE" in q for q in conn.executed)
        assert not any("REVOKE UPDATE" in q for q in conn.executed)

    async def test_revokes_and_grants_allow_listed_columns(self, conn: _FakeConn) -> None:
        conn.tables = {"accounts"}
        conn.columns = {"accounts": _full_accounts_columns()}
        await _apply_accounts_allow_list(conn, "modulo_app")

        assert any('REVOKE UPDATE ON public.accounts FROM "modulo_app"' in q for q in conn.executed)
        assert any("REVOKE UPDATE ON public.accounts FROM PUBLIC" in q for q in conn.executed)
        grant = [q for q in conn.executed if "GRANT UPDATE" in q]
        assert grant
        for col in ACCOUNTS_WRITABLE_COLUMNS:
            assert col in grant[0]

    async def test_grants_only_columns_that_exist(self, conn: _FakeConn) -> None:
        conn.tables = {"accounts"}
        conn.columns = {"accounts": {"email"}}
        await _apply_accounts_allow_list(conn, "modulo_app")
        grant = [q for q in conn.executed if "GRANT UPDATE" in q]
        assert grant and "email" in grant[0] and "password_hash" not in grant[0]

    async def test_no_grant_statement_when_no_allow_listed_column_exists(self, conn: _FakeConn) -> None:
        conn.tables = {"accounts"}
        conn.columns = {"accounts": {"id"}}
        await _apply_accounts_allow_list(conn, "modulo_app")
        assert not any("GRANT UPDATE" in q for q in conn.executed)


# ---------------------------------------------------------------------------
# _grant_break_glass
# ---------------------------------------------------------------------------


class TestGrantBreakGlass:
    async def test_grants_select_insert_audit_and_sequence_usage(self, conn: _FakeConn) -> None:
        conn.tables = {
            "org_memberships",
            "token_families",
            "org_api_keys",
            "organisations",
            "accounts",
            "audit_events",
            "audit_chain_heads",
        }
        await _grant_break_glass(conn, "modulo_breakglass")

        assert any("GRANT SELECT, INSERT ON public.accounts" in q for q in conn.executed)
        assert any("GRANT SELECT, INSERT ON public.org_memberships" in q for q in conn.executed)
        assert any("GRANT SELECT, INSERT ON public.audit_events" in q for q in conn.executed)
        assert any("GRANT SELECT, INSERT, UPDATE ON public.audit_chain_heads" in q for q in conn.executed)
        assert any("GRANT USAGE ON ALL SEQUENCES IN SCHEMA public" in q for q in conn.executed)

    async def test_grants_select_on_read_surfaces(self, conn: _FakeConn) -> None:
        conn.tables = {"org_memberships", "token_families", "org_api_keys", "organisations"}
        await _grant_break_glass(conn, "modulo_breakglass")
        joined = " ".join(conn.executed)
        assert "public.org_memberships" in joined
        assert "public.token_families" in joined
        assert "public.org_api_keys" in joined
        assert "public.organisations" in joined

    async def test_skips_missing_tables(self, conn: _FakeConn) -> None:
        await _grant_break_glass(conn, "modulo_breakglass")
        assert not any("GRANT SELECT" in q and "public." in q for q in conn.executed)
        assert any("GRANT USAGE ON ALL SEQUENCES" in q for q in conn.executed)


# ---------------------------------------------------------------------------
# _grant_function_execute
# ---------------------------------------------------------------------------


class TestGrantFunctionExecute:
    async def test_grants_execute_when_function_exists(self, conn: _FakeConn) -> None:
        conn.function_exists = True
        await _grant_function_execute(conn, "modulo_app", "modulo_breakglass")
        assert any(
            "GRANT EXECUTE ON FUNCTION public.deactivate_break_glass(uuid, uuid, boolean)" in q
            and '"modulo_app", "modulo_breakglass"' in q
            for q in conn.executed
        )

    async def test_skips_when_function_missing(self, conn: _FakeConn) -> None:
        conn.function_exists = False
        await _grant_function_execute(conn, "modulo_app", "modulo_breakglass")
        assert not any("GRANT EXECUTE" in q for q in conn.executed)


# ---------------------------------------------------------------------------
# _find_allow_list_violations — fail-closed posture assertions
# ---------------------------------------------------------------------------


class TestFindAllowListViolations:
    async def test_skips_when_accounts_missing(self, conn: _FakeConn) -> None:
        assert not await _find_allow_list_violations(conn, "modulo_app")
        assert conn.executed == ["SELECT to_regclass(?) IS NOT NULL"]

    async def test_clean_posture_returns_no_violations(self, conn: _FakeConn) -> None:
        assert not await _find_allow_list_violations(_clean_posture_conn(), "modulo_app")

    async def test_detects_table_level_update_grant(self, conn: _FakeConn) -> None:
        conn.tables = {"accounts"}
        conn.columns = {"accounts": _full_accounts_columns()}
        conn.table_update_grantees = ["PUBLIC"]
        conn.column_updatable = set(ACCOUNTS_WRITABLE_COLUMNS)

        violations = await _find_allow_list_violations(conn, "modulo_app")
        assert any("table-level UPDATE grant" in v and "PUBLIC" in v for v in violations)

    async def test_detects_granted_column_drift(self, conn: _FakeConn) -> None:
        conn.tables = {"accounts"}
        conn.columns = {"accounts": {"email", "id"}}
        conn.column_updatable = {"email", "id"}  # drift: id granted but not allow-listed

        violations = await _find_allow_list_violations(conn, "modulo_app")
        assert any("grant drift" in v for v in violations)

    async def test_detects_break_glass_column_writable_by_app(self, conn: _FakeConn) -> None:
        conn.tables = {"accounts"}
        conn.columns = {"accounts": _full_accounts_columns()}
        conn.column_updatable = set(ACCOUNTS_WRITABLE_COLUMNS) | {"is_break_glass"}

        violations = await _find_allow_list_violations(conn, "modulo_app")
        assert any("break-glass column is_break_glass" in v for v in violations)

    async def test_detects_superuser_role(self, conn: _FakeConn) -> None:
        conn.tables = {"accounts"}
        conn.columns = {"accounts": _full_accounts_columns()}
        conn.column_updatable = set(ACCOUNTS_WRITABLE_COLUMNS)
        conn.is_superuser = True

        violations = await _find_allow_list_violations(conn, "modulo_app")
        assert any("is a superuser" in v for v in violations)

    async def test_detects_membership_in_privileged_roles(self, conn: _FakeConn) -> None:
        conn.tables = {"accounts"}
        conn.columns = {"accounts": _full_accounts_columns()}
        conn.column_updatable = set(ACCOUNTS_WRITABLE_COLUMNS)
        conn.privileged_memberships = [_MIGRATE_ROLE, "modulo_breakglass"]

        violations = await _find_allow_list_violations(conn, "modulo_app")
        assert any(_MIGRATE_ROLE in v for v in violations)
        assert any("modulo_breakglass" in v for v in violations)

    async def test_detects_missing_system_role_bypassrls(self, conn: _FakeConn) -> None:
        conn.tables = {"accounts"}
        conn.columns = {"accounts": _full_accounts_columns()}
        conn.column_updatable = set(ACCOUNTS_WRITABLE_COLUMNS)
        # modulo_system role exists but without BYPASSRLS
        conn.roles[_SYSTEM_ROLE] = True
        conn.role_bypassrls = {_SYSTEM_ROLE: False}

        violations = await _find_allow_list_violations(conn, "modulo_app")
        assert any("modulo_system" in v and "BYPASSRLS" in v for v in violations)

    async def test_clean_posture_includes_system_role(self) -> None:
        clean = _clean_posture_conn()
        violations = await _find_allow_list_violations(clean, "modulo_app")
        # modulo_system has BYPASSRLS in the clean conn — no violation
        assert not any("modulo_system" in v for v in violations)


# ---------------------------------------------------------------------------
# _assert_role_posture
# ---------------------------------------------------------------------------


class TestAssertRolePosture:
    async def test_clean_posture_is_noop(self, conn: _FakeConn) -> None:
        clean = _clean_posture_conn()
        assert not await _find_allow_list_violations(clean, "modulo_app")
        await _assert_role_posture(clean, "modulo_app")

    async def test_violation_raises_runtime_error(self, conn: _FakeConn) -> None:
        conn.tables = {"accounts"}
        conn.columns = {"accounts": _full_accounts_columns()}
        conn.table_update_grantees = ["PUBLIC"]
        conn.column_updatable = set(ACCOUNTS_WRITABLE_COLUMNS)

        with pytest.raises(RuntimeError, match="role posture assertion FAILED"):
            await _assert_role_posture(conn, "modulo_app")


# ---------------------------------------------------------------------------
# _bootstrap end-to-end
# ---------------------------------------------------------------------------


class TestBootstrap:
    async def test_full_bootstrap_applies_roles_grants_and_asserts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = _clean_posture_conn()
        fake.roles = {}  # every role starts missing -> CREATE paths
        fake.tables = {
            "accounts",
            "organisations",
            "org_memberships",
            "token_families",
            "org_api_keys",
            "audit_events",
            "audit_chain_heads",
        }

        monkeypatch.setenv("MODULO_BREAK_GLASS_DATABASE_URL", "")
        with patch("modulo.db.bootstrap_role.asyncpg.connect", new=AsyncMock(return_value=fake)):
            await bootstrap_roles(
                "postgresql+asyncpg://admin:secret@db:5432/modulo",
                "postgresql+asyncpg://modulo_app:apppw@db:5432/modulo",
            )

        joined = "\n".join(fake.executed)
        assert "CREATE ROLE \"modulo_app\" LOGIN PASSWORD 'apppw'" in joined
        assert "CREATE ROLE \"modulo_app\" LOGIN BYPASSRLS PASSWORD 'apppw'" not in joined
        assert 'CREATE ROLE "modulo_migrate" NOSUPERUSER NOLOGIN BYPASSRLS' in joined
        assert 'GRANT CREATE ON SCHEMA public TO "modulo_migrate"' in joined
        assert "GRANT REFERENCES ON TABLE public.organisations" in joined
        assert "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE" in joined
        assert "GRANT EXECUTE ON FUNCTION public.deactivate_break_glass" in joined
        assert fake.closed is True

    async def test_bootstrap_uses_configured_break_glass_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = _clean_posture_conn()
        fake.roles = {}
        fake.tables = {
            "accounts",
            "organisations",
            "org_memberships",
            "token_families",
            "org_api_keys",
            "audit_events",
            "audit_chain_heads",
        }

        monkeypatch.setenv(
            "MODULO_BREAK_GLASS_DATABASE_URL",
            "postgresql://my_bg:bgpw@db:5432/modulo",
        )
        with patch("modulo.db.bootstrap_role.asyncpg.connect", new=AsyncMock(return_value=fake)):
            await _bootstrap(
                "postgres://admin:secret@db:5432/modulo",
                "postgres://modulo_app:apppw@db:5432/modulo",
            )

        joined = "\n".join(fake.executed)
        assert "CREATE ROLE \"my_bg\" LOGIN BYPASSRLS PASSWORD 'bgpw'" in joined
        assert 'CREATE ROLE "modulo_breakglass"' not in joined

    async def test_bootstrap_closes_connection_even_on_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = _clean_posture_conn()
        fake.roles = {}
        fake.tables = {"accounts"}

        def _hook(q: str) -> None:
            if "ALTER DEFAULT PRIVILEGES" in q:
                raise RuntimeError("boom")

        fake.sql_hook = _hook
        monkeypatch.setenv("MODULO_BREAK_GLASS_DATABASE_URL", "")
        with (
            patch("modulo.db.bootstrap_role.asyncpg.connect", new=AsyncMock(return_value=fake)),
            pytest.raises(RuntimeError, match="boom"),
        ):
            await _bootstrap(
                "postgres://admin:secret@db:5432/modulo",
                "postgres://modulo_app:apppw@db:5432/modulo",
            )
        assert fake.closed is True


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------


class TestMain:
    def test_missing_env_vars_exits_one(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        for var in REQUIRED_VARS:
            monkeypatch.delenv(var, raising=False)

        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 1
        assert "Missing required env vars" in caplog.text

    def test_bootstrap_failure_exits_one(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setenv("DATABASE_ADMIN_URL", "postgres://admin:secret@db:5432/modulo")
        monkeypatch.setenv("DATABASE_URL", "postgres://modulo_app:apppw@db:5432/modulo")
        monkeypatch.setenv("MODULO_BREAK_GLASS_DATABASE_URL", "")

        async def _fail(admin_url: str, app_url: str) -> None:
            raise RuntimeError("boom")

        with patch("modulo.db.bootstrap_role._bootstrap", new=_fail), pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 1
        assert "Role bootstrap failed: boom" in caplog.text

    def test_success_logs_completion(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        caplog.set_level("INFO", logger="modulo.db.bootstrap_role")
        monkeypatch.setenv("DATABASE_ADMIN_URL", "postgres://admin:secret@db:5432/modulo")
        monkeypatch.setenv("DATABASE_URL", "postgres://modulo_app:apppw@db:5432/modulo")
        monkeypatch.setenv("MODULO_BREAK_GLASS_DATABASE_URL", "")

        async def _ok(admin_url: str, app_url: str) -> None:
            assert "modulo" in admin_url

        with patch("modulo.db.bootstrap_role._bootstrap", new=_ok):
            main()
        assert "Role bootstrap complete" in caplog.text
