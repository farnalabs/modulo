"""Unit tests for the shared runtime-config allowlist (FAR-671 slice 3).

Locks: the allowlist keys are exactly monitor/autoLogin, the deploy
entrypoint and the native serve_routine generate from the SAME module
(no second generator exists), and no credential-shaped value from the
environment is ever copied into the rendered page.
"""

from pathlib import Path

import pytest

from modulo.launcher.runtime_config import (
    RUNTIME_CONFIG_FILENAME,
    RUNTIME_CONFIG_KEYS,
    build_runtime_config_payload,
    render_runtime_config_js,
    write_runtime_config_js,
)

_REPO_ROOT = Path(__file__).resolve().parents[4]
_ENTRYPOINT = _REPO_ROOT / "deploy" / "fly" / "entrypoint.sh"

_CREDENTIAL_ENV: dict[str, str] = {
    "DATABASE_URL": "postgresql+asyncpg://modulo:hunter2@127.0.0.1:15432/modulo",
    "DATABASE_ADMIN_URL": "postgresql+asyncpg://modulo:hunter2@127.0.0.1:15432/modulo",
    "MODULO_SYSTEM_DATABASE_URL": "postgresql+asyncpg://modulo:hunter2@127.0.0.1:15432/modulo",
    "REDIS_URL": "redis://:redis-pass@127.0.0.1:16379/0",
    "SECRET_KEY": "a" * 64,
    "FERNET_KEY": "b" * 44,
    "MODULO_ADMIN_PASSWORD": "admin-secret",
    "MODULO_BREAK_GLASS_SECRET": "breakglass-secret",
}


def test_allowlist_is_exactly_monitor_and_autologin() -> None:
    assert set(RUNTIME_CONFIG_KEYS) == {"monitor", "autoLogin"}


def test_monitor_config_is_parsed_into_the_allowlisted_key() -> None:
    payload = build_runtime_config_payload({"MODULO_MONITOR_CONFIG": '{"targets": ["db"]}'})
    assert set(payload) == {"monitor"}
    assert payload["monitor"] == {"targets": ["db"]}


def test_invalid_monitor_config_is_reported_and_dropped(capsys: pytest.CaptureFixture[str]) -> None:
    payload = build_runtime_config_payload({"MODULO_MONITOR_CONFIG": "not-json"})
    assert not payload
    assert "Ignoring invalid MODULO_MONITOR_CONFIG" in capsys.readouterr().out


def test_auto_login_pair_requires_both_values() -> None:
    assert not build_runtime_config_payload({"MODULO_AUTO_LOGIN_USERNAME": "u"})
    payload = build_runtime_config_payload({"MODULO_AUTO_LOGIN_USERNAME": "demo", "MODULO_AUTO_LOGIN_PASSWORD": "demo"})
    assert set(payload) == {"autoLogin"}


def test_payload_never_contains_credential_shaped_values() -> None:
    payload = build_runtime_config_payload(_CREDENTIAL_ENV)
    rendered = render_runtime_config_js(payload)
    for secret in _CREDENTIAL_ENV.values():
        assert secret not in rendered
    assert "autoLogin" not in payload
    assert "postgres" not in rendered
    assert "redis" not in rendered


def test_render_produces_the_config_line() -> None:
    rendered = render_runtime_config_js({"monitor": {"x": 1}})
    assert rendered.startswith("window.__MODULO_CONFIG__ = Object.assign(")
    assert rendered.endswith(");\n")


def test_write_matches_render(tmp_path: Path) -> None:
    destination = tmp_path / RUNTIME_CONFIG_FILENAME
    write_runtime_config_js({"monitor": {"x": 1}}, destination)
    assert destination.read_text(encoding="utf-8") == render_runtime_config_js({"monitor": {"x": 1}})


def test_deploy_entrypoint_generates_through_the_shared_module() -> None:
    """The deploy path imports the module instead of carrying its own generator."""
    script = _ENTRYPOINT.read_text(encoding="utf-8")
    assert "build_runtime_config_payload" in script
    assert "write_runtime_config_js" in script
    assert RUNTIME_CONFIG_FILENAME in script
    # The inline generator must be GONE — one allowlist, one renderer.
    assert "window.__MODULO_CONFIG__" not in script
    assert "MODULO_MONITOR_CONFIG" not in script
    assert "autoLogin" not in script
