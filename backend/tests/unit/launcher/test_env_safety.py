"""Unit tests for launcher env safety (FAR-671 / ADR 031 Decisions 2/8).

Locks the exact scrub list, the copy (non-mutating) semantics of
``scrub_environment``, the in-place behaviour of ``scrub_os_environment``, the
first-boot ambient-URL refusal (with and without a bootstrapped state.json),
and the dotenv-source coverage (a URL injected through the env file Settings
reads must refuse just like an os.environ one).
"""

import os
from pathlib import Path

import pytest

from modulo.launcher.env_safety import (
    AMBIENT_SERVICE_URL_VARS,
    SCRUB_EXACT_NAMES,
    SCRUB_PREFIXES,
    AmbientEnvironmentError,
    assert_no_ambient_service_urls,
    make_first_boot_guard,
    scrub_environment,
    scrub_os_environment,
)


def _poisoned_env() -> dict[str, str]:
    return {
        "PATH": "/usr/bin",
        "HOME": "/home/operator",
        "MODULO_SECRET_KEY": "x" * 32,
        # Exact-name scrub list:
        "REDISCLI_AUTH": "leaked",
        "PYTHONPATH": "/foreign/tree",
        "PYTHONHOME": "/foreign/home",
        "PYTHONUSERBASE": "/foreign/site-packages",
        "OPENSSL_CONF": "/evil/openssl.cnf",
        "SSL_CERT_FILE": "/evil/cert.pem",
        "SSL_CERT_DIR": "/evil/certs",
        "REQUESTS_CA_BUNDLE": "/evil/bundle.pem",
        "CURL_CA_BUNDLE": "/evil/curl-bundle.pem",
        "MODULO_TEST_PAUSE_AT": "initdb_pre_rename",
        # LD_* prefix coverage (the whole ELF dynamic-loader family):
        "LD_PRELOAD": "/evil.so",
        "LD_LIBRARY_PATH": "/evil",
        "LD_AUDIT": "/evil-audit.so",
        "LD_BIND_NOW": "1",
        # PG* prefix coverage (libpq client vars):
        "PGDATA": "/foreign/pgdata",
        "PGHOST": "foreign-host",
        "PGPORT": "6543",
        "PGPASSWORD": "leaked-pg",
        "PGUSER": "foreign-user",
        "PGSSLMODE": "require",
        # DYLD_* prefix coverage:
        "DYLD_INSERT_LIBRARIES": "/evil.dylib",
        "DYLD_LIBRARY_PATH": "/evil",
    }


def test_scrub_removes_every_listed_variable() -> None:
    scrubbed = scrub_environment(_poisoned_env())
    assert "PATH" in scrubbed
    assert "HOME" in scrubbed
    assert "MODULO_SECRET_KEY" in scrubbed
    for name in SCRUB_EXACT_NAMES:
        assert name not in scrubbed
    for name in ("PGDATA", "PGHOST", "PGPORT", "PGPASSWORD", "PGUSER", "PGSSLMODE"):
        assert name not in scrubbed
    for name in ("DYLD_INSERT_LIBRARIES", "DYLD_LIBRARY_PATH"):
        assert name not in scrubbed
    # The LD_* prefix family (not just the individually listed names):
    for name in ("LD_PRELOAD", "LD_LIBRARY_PATH", "LD_AUDIT", "LD_BIND_NOW"):
        assert name not in scrubbed


def test_scrub_covers_the_adr_list_exactly() -> None:
    # ADR 031 Decision 2 names, verbatim (the LD_ prefix family makes the
    # individually listed LD_* names redundant, but they stay as
    # self-documentation; MODULO_TEST_PAUSE_AT is the initdb test seam).
    expected_exact = {
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
    assert expected_exact == SCRUB_EXACT_NAMES
    assert SCRUB_PREFIXES == ("PG", "DYLD_", "LD_")


def test_scrub_is_case_insensitive() -> None:
    scrubbed = scrub_environment({"pgpassword": "leaked", "dyld_insert_libraries": "/evil.dylib", "PATH": "/bin"})
    assert "pgpassword" not in scrubbed
    assert "dyld_insert_libraries" not in scrubbed
    assert "PATH" in scrubbed
    scrubbed_ld = scrub_environment({"ld_audit": "/evil-audit.so", "PATH": "/bin"})
    assert "ld_audit" not in scrubbed_ld


def test_scrub_returns_a_copy_and_does_not_mutate_input() -> None:
    env = _poisoned_env()
    scrubbed = scrub_environment(env)
    assert "PGHOST" in env  # caller's dict untouched
    assert "PGHOST" not in scrubbed


def test_scrub_os_environment_scrubs_in_place(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PGHOST", "foreign-host")
    monkeypatch.setenv("PGPASSWORD", "leaked-pg")
    monkeypatch.setenv("DYLD_LIBRARY_PATH", "/evil")
    monkeypatch.setenv("PYTHONPATH", "/foreign")
    scrub_os_environment()
    for name in ("PGHOST", "PGPASSWORD", "DYLD_LIBRARY_PATH", "PYTHONPATH"):
        assert name not in os.environ


def test_first_boot_refuses_ambient_database_url_without_state(tmp_path: Path) -> None:
    env = {"DATABASE_URL": "postgres://foreign/db"}
    with pytest.raises(AmbientEnvironmentError, match="DATABASE_URL"):
        assert_no_ambient_service_urls(tmp_path / "data", env=env)


def test_first_boot_refusal_message_points_at_compose_path(tmp_path: Path) -> None:
    env = {"REDIS_URL": "redis://foreign:6379/0"}
    with pytest.raises(AmbientEnvironmentError) as excinfo:
        assert_no_ambient_service_urls(tmp_path / "data", env=env)
    message = str(excinfo.value)
    assert "docker compose" in message
    assert "REDIS_URL" in message


def test_first_boot_allows_clean_environment(tmp_path: Path) -> None:
    assert_no_ambient_service_urls(tmp_path / "data", env={"PATH": "/usr/bin"})


def test_first_boot_allows_ambient_urls_once_state_exists(tmp_path: Path) -> None:
    state_dir = tmp_path / "data"
    state_dir.mkdir()
    (state_dir / "state.json").write_text("{}")
    env = {"DATABASE_URL": "postgres://foreign/db", "REDIS_URL": "redis://foreign:6379/0"}
    assert_no_ambient_service_urls(state_dir, env=env)


def test_first_boot_checks_both_ambient_vars() -> None:
    assert AMBIENT_SERVICE_URL_VARS == ("DATABASE_URL", "REDIS_URL")


def test_make_first_boot_guard_refuses_poisoned_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgres://foreign/db")
    guard = make_first_boot_guard(tmp_path / "data")
    with pytest.raises(AmbientEnvironmentError, match="DATABASE_URL"):
        guard()


def test_make_first_boot_guard_passes_once_state_exists(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state_dir = tmp_path / "data"
    state_dir.mkdir()
    (state_dir / "state.json").write_text("{}")
    monkeypatch.setenv("DATABASE_URL", "postgres://foreign/db")
    monkeypatch.setenv("REDIS_URL", "redis://foreign:6379/0")
    result = make_first_boot_guard(state_dir)()
    assert result is None  # the guard returned without refusing


def test_first_boot_refuses_ambient_url_from_env_file(tmp_path: Path) -> None:
    # A DATABASE_URL injected through the dotenv file Settings reads never
    # appears in os.environ — the guard must refuse it anyway.
    env_file = tmp_path / "config.env"
    env_file.write_text("DATABASE_URL=postgres://foreign-from-dotenv/db\n")
    with pytest.raises(AmbientEnvironmentError, match="DATABASE_URL"):
        assert_no_ambient_service_urls(tmp_path / "data", env={"PATH": "/usr/bin"}, env_file=env_file)


def test_first_boot_env_file_refusal_names_the_file(tmp_path: Path) -> None:
    env_file = tmp_path / "config.env"
    env_file.write_text("REDIS_URL=redis://foreign:6379/0\n")
    with pytest.raises(AmbientEnvironmentError) as excinfo:
        assert_no_ambient_service_urls(tmp_path / "data", env={}, env_file=env_file)
    message = str(excinfo.value)
    assert str(env_file) in message
    assert "docker compose" in message


def test_first_boot_env_file_with_clean_keys_passes(tmp_path: Path) -> None:
    env_file = tmp_path / "config.env"
    env_file.write_text(
        "# operator config\n\n"
        "SECRET_KEY=abcdefgh\n"
        "REDIS_PASSWORD=not-an-endpoint\n"
        "DATABASE_URL=  \n"  # empty value is not an endpoint
    )
    assert_no_ambient_service_urls(tmp_path / "data", env={"PATH": "/usr/bin"}, env_file=env_file)


def test_first_boot_env_file_urls_allowed_once_state_exists(tmp_path: Path) -> None:
    state_dir = tmp_path / "data"
    state_dir.mkdir()
    (state_dir / "state.json").write_text("{}")
    env_file = tmp_path / "config.env"
    env_file.write_text("DATABASE_URL=postgres://foreign/db\n")
    assert_no_ambient_service_urls(state_dir, env={"PATH": "/usr/bin"}, env_file=env_file)


def test_first_boot_refuses_when_both_sources_poisoned(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgres://foreign/db")
    env_file = tmp_path / "config.env"
    env_file.write_text("REDIS_URL=redis://foreign:6379/0\n")
    with pytest.raises(AmbientEnvironmentError) as excinfo:
        assert_no_ambient_service_urls(tmp_path / "data", env_file=env_file)
    message = str(excinfo.value)
    assert "process environment" in message
    assert "env file" in message


def test_make_first_boot_guard_threads_env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("REDIS_URL", raising=False)
    env_file = tmp_path / "config.env"
    env_file.write_text("DATABASE_URL=postgres://foreign-from-dotenv/db\n")
    guard = make_first_boot_guard(tmp_path / "data", env_file=env_file)
    with pytest.raises(AmbientEnvironmentError, match="DATABASE_URL"):
        guard()


def test_make_first_boot_guard_without_env_file_ignores_dotenv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No env_file passed → no dotenv coverage (the launcher always pins one);
    # a stray file on disk must not trip the guard.
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("REDIS_URL", raising=False)
    (tmp_path / "config.env").write_text("DATABASE_URL=postgres://foreign/db\n")
    result = make_first_boot_guard(tmp_path / "data")()
    assert result is None  # the guard returned without refusing
