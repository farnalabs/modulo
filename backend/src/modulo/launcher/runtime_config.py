"""Single shared runtime-config allowlist (FAR-671 slice 3 / ADR 031).

``window.__MODULO_CONFIG__`` is the browser-side runtime configuration the SPA
reads before anything else. Two delivery paths produce it:

* the container path — ``deploy/fly/entrypoint.sh`` renders
  ``runtime-config.js`` into the nginx docroot;
* the native single-port path — the API serves ``GET /runtime-config.js``
  when ``serve_spa`` is ON (``modulo.api.main._init_once_mount_spa``).

ONE allowlist backs both: its top-level keys are exactly
:data:`RUNTIME_CONFIG_KEYS`, and both paths generate through
:func:`build_runtime_config_payload` — a malformed ``MODULO_MONITOR_CONFIG``
is reported (stdout) and dropped, never partially served. The allowlist is
deliberately tiny: ``monitor`` (parsed monitor config) and ``autoLogin`` (the
demo auto-login pair, which is BY DESIGN a credential delivered to the
browser, exactly as the existing deploy path already ships it). Nothing else
in the environment is ever copied into the page — no ``DATABASE_URL``, no
``REDIS_URL``, no fernet/secret values (locked by
``tests/unit/launcher/test_runtime_config.py``).

Import-light by design: only the stdlib, so the Fly entrypoint's heredoc can
import it before heavy dependencies are ready.
"""

import json
import os
from collections.abc import Mapping
from pathlib import Path

RUNTIME_CONFIG_KEYS: frozenset[str] = frozenset({"monitor", "autoLogin"})
RUNTIME_CONFIG_FILENAME = "runtime-config.js"

_MONITOR_ENV = "MODULO_MONITOR_CONFIG"
_AUTO_LOGIN_USERNAME_ENV = "MODULO_AUTO_LOGIN_USERNAME"
_AUTO_LOGIN_PASSWORD_ENV = "MODULO_AUTO_LOGIN_PASSWORD"

# The launcher-owned public surface (consumed by the deploy entrypoint, the
# serve_spa mount, and the tests; vulture's dead-code gate special-cases
# __all__).
__all__ = [
    "RUNTIME_CONFIG_FILENAME",
    "RUNTIME_CONFIG_KEYS",
    "build_runtime_config_payload",
    "render_runtime_config_js",
    "write_runtime_config_js",
]


def build_runtime_config_payload(env: Mapping[str, str] | None = None) -> dict[str, object]:
    """Restrict the runtime config to the allowlisted keys and their ONLY sources.

    Behaviour-identical to the historical inline heredoc in
    ``deploy/fly/entrypoint.sh`` (including the stdout wording for an invalid
    ``MODULO_MONITOR_CONFIG``).
    """
    source = dict(os.environ if env is None else env)
    payload: dict[str, object] = {}
    monitor_config = source.get(_MONITOR_ENV)
    if monitor_config:
        try:
            payload["monitor"] = json.loads(monitor_config)
        except json.JSONDecodeError as exc:
            print(f"Ignoring invalid {_MONITOR_ENV}: {exc}")  # noqa: T201 — deploy-path parity
    username = source.get(_AUTO_LOGIN_USERNAME_ENV)
    password = source.get(_AUTO_LOGIN_PASSWORD_ENV)
    if username and password:
        payload["autoLogin"] = {"username": username, "password": password}
    return payload


def render_runtime_config_js(payload: Mapping[str, object]) -> str:
    """Render the ``window.__MODULO_CONFIG__`` assignment line."""
    return (
        "window.__MODULO_CONFIG__ = Object.assign(window.__MODULO_CONFIG__ || {}, "
        + json.dumps(payload, separators=(",", ":"), ensure_ascii=True)
        + ");\n"
    )


def write_runtime_config_js(payload: Mapping[str, object], destination: str | os.PathLike[str]) -> None:
    """Write the rendered script (same tf-8 text write as the deploy path)."""
    Path(destination).write_text(render_runtime_config_js(payload), encoding="utf-8")
