"""Unit tests for the flag-gated serve_spa mount (FAR-671 slice 3).

Locks: OFF (default) mounts NOTHING (Docker parity); ON serves the SPA with
the index fallback registered LAST (never shadowing /api or the explicitly
registered API routes), no-cache HTML, immutable hashed assets, the
runtime-config.js route, and the upload-limit / host-origin guardrails.
"""

from pathlib import Path
from typing import Any

from fastapi import FastAPI
from starlette.routing import Mount
from starlette.testclient import TestClient

from modulo.api.main import _init_once_mount_spa

ASSET_NAME = "app-a1b2c3d4.js"


def _dist(tmp_path: Path) -> Path:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True, exist_ok=True)
    (dist / "index.html").write_text("<html><body>SPA-SHELLO</body></html>", encoding="utf-8")
    (dist / "assets" / ASSET_NAME).write_text("console.log('hashed')", encoding="utf-8")
    return dist


def _plain_api_app() -> FastAPI:
    app = FastAPI()

    @app.api_route("/api/v1/collide", methods=["GET", "POST"])
    def collide() -> dict[str, bool]:
        return {"api_won": True}

    return app


def _spa_app(dist: Path, extra: dict[str, str] | None = None) -> tuple[FastAPI, TestClient]:
    app = _plain_api_app()
    env: dict[str, str] = {"MODULO_SERVE_SPA": "1", "MODULO_FRONTEND_DIST": str(dist)}
    env.update(extra or {})
    _init_once_mount_spa(app, env)
    return app, TestClient(app, base_url="http://127.0.0.1:18000")


def test_serve_spa_disabled_mounts_nothing() -> None:
    app = _plain_api_app()
    _init_once_mount_spa(app, {})
    static_mounts = [r for r in app.router.routes if isinstance(r, Mount) and r.name in ("spa", "spa-assets")]
    assert not static_mounts
    middleware_names = {item.cls.__name__ for item in app.user_middleware}
    assert "HostOriginMiddleware" not in middleware_names


def test_serve_spa_disabled_by_default() -> None:
    app = _plain_api_app()
    assert _init_once_mount_spa(app, {}) is False
    assert _init_once_mount_spa(app, {"MODULO_SERVE_SPA": "false"}) is False


def test_truthy_flag_refuses_missing_dist(tmp_path: Path) -> None:
    app = _plain_api_app()
    assert (
        _init_once_mount_spa(app, {"MODULO_SERVE_SPA": "true", "MODULO_FRONTEND_DIST": str(tmp_path / "missing")})
        is False
    )


def test_serve_spa_on_serves_the_spa_shell(tmp_path: Path) -> None:
    dist = _dist(tmp_path)
    _, client = _spa_app(dist)
    response = client.get("/")
    assert response.status_code == 200
    assert "SPA-SHELLO" in response.text
    assert response.headers["cache-control"] == "no-cache, must-revalidate"


def test_spa_fallback_serves_index_for_deep_links(tmp_path: Path) -> None:
    dist = _dist(tmp_path)
    _, client = _spa_app(dist)
    response = client.get("/pipelines/unexisting-deep-link")
    assert response.status_code == 200
    assert "SPA-SHELLO" in response.text
    assert response.headers["cache-control"] == "no-cache, must-revalidate"


def test_hashed_assets_are_immutable(tmp_path: Path) -> None:
    dist = _dist(tmp_path)
    _, client = _spa_app(dist)
    response = client.get(f"/assets/{ASSET_NAME}")
    assert response.status_code == 200
    assert response.headers["cache-control"].startswith("public,")
    assert "max-age=31536000" in response.headers["cache-control"]
    assert "immutable" in response.headers["cache-control"]


def test_api_routes_are_never_shadowed_by_the_spa_fallback(tmp_path: Path) -> None:
    dist = _dist(tmp_path)
    app, client = _spa_app(dist)
    response = client.get("/api/v1/collide")
    assert response.status_code == 200
    assert response.json() == {"api_won": True}
    # The SPA fallback must be the LAST route: nothing after it can shadow it.
    last_route = app.router.routes[-1]
    assert isinstance(last_route, Mount)
    assert last_route.name == "spa"


def test_runtime_config_route_serves_allowlisted_keys_only(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("MODULO_MONITOR_CONFIG", '{"targets": ["db"]}')
    monkeypatch.setenv("MODULO_AUTO_LOGIN_USERNAME", "demo")
    monkeypatch.setenv("MODULO_AUTO_LOGIN_PASSWORD", "demo-pw")
    dist = _dist(tmp_path)
    _, client = _spa_app(dist)
    response = client.get("/runtime-config.js")
    assert response.status_code == 200
    body = response.text
    assert body.startswith("window.__MODULO_CONFIG__")
    assert '"monitor"' in body
    assert '"autoLogin"' in body
    assert response.headers["cache-control"].startswith("no-cache")


def test_runtime_config_route_excludes_ambient_env_values(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://modulo:secret@db/modulo")
    monkeypatch.delenv("MODULO_MONITOR_CONFIG", raising=False)
    dist = _dist(tmp_path)
    _, client = _spa_app(dist)
    body = client.get("/runtime-config.js").text
    assert "postgresql" not in body
    assert "secret" not in body


def test_upload_limit_middleware_returns_413(tmp_path: Path) -> None:
    dist = _dist(tmp_path)
    _, client = _spa_app(dist)
    response = client.post("/api/v1/collide", content=b"x", headers={"Content-Length": str(51 * 1024 * 1024)})
    assert response.status_code == 413
    assert client.post("/api/v1/collide", content=b"{}", headers={"Content-Length": "2"}).status_code == 200


def test_host_origin_middleware_is_wired_when_serving(tmp_path: Path) -> None:
    dist = _dist(tmp_path)
    app, client = _spa_app(dist)
    assert client.get("/", headers={"Host": "evil.example"}).status_code == 403
    assert client.get("/", headers={"Host": "127.0.0.1"}).status_code == 200
    middleware_names = {item.cls.__name__ for item in app.user_middleware}
    assert "HostOriginMiddleware" in middleware_names
    assert "Mount" not in middleware_names


def test_lan_origin_via_the_mount_helper(tmp_path: Path) -> None:
    dist = _dist(tmp_path)
    _, client = _spa_app(dist, {"MODULO_LAN_ORIGINS": "192.168.1.10"})
    assert client.get("/", headers={"Host": "192.168.1.10"}).status_code == 200
    assert client.get("/", headers={"Host": "evil.example"}).status_code == 403
