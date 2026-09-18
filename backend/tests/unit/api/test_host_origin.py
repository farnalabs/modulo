"""Unit tests for the loopback Host/Origin allowlist middleware (FAR-671).

Locks: foreign Host rejected 403; localhost/127.0.0.1 accepted (any port);
the configured LAN origins are honoured ONLY when configured; a present
foreign Origin is rejected too; missing Host is rejected.
"""

from fastapi import FastAPI
from starlette.testclient import TestClient

from modulo.api.middleware.host_origin import LAN_ORIGINS_ENV, HostOriginMiddleware, split_lan_origins

FOREIGN_HOST = "evil.example"


def _app() -> FastAPI:
    app = FastAPI()

    @app.get("/ping")
    def ping() -> dict[str, str]:
        return {"ok": "yes"}

    return app


def _client(**middleware_params: object) -> TestClient:
    app = _app()
    app.add_middleware(HostOriginMiddleware, **middleware_params)  # type: ignore[arg-type]
    return TestClient(app, base_url="http://127.0.0.1:18000")


def test_foreign_host_is_rejected_403() -> None:
    response = _client().get("/ping", headers={"Host": FOREIGN_HOST})
    assert response.status_code == 403
    assert "Forbidden" in response.json()["detail"]


def test_localhost_and_loopback_hosts_are_accepted() -> None:
    client = _client()
    assert client.get("/ping", headers={"Host": "localhost"}).status_code == 200
    assert client.get("/ping", headers={"Host": "127.0.0.1"}).status_code == 200
    assert client.get("/ping", headers={"Host": "127.0.0.1:18000"}).status_code == 200


def test_missing_host_is_rejected_403() -> None:
    response = _client().get("/ping", headers={"Host": ""})
    assert response.status_code == 403
    assert "Forbidden" in response.json()["detail"]


def test_lan_origins_must_be_configured() -> None:
    assert _client().get("/ping", headers={"Host": "192.168.1.10"}).status_code == 403
    lan_client = _client(lan_origins=("192.168.1.10", "operator-laptop.local"))
    assert lan_client.get("/ping", headers={"Host": "192.168.1.10"}).status_code == 200
    assert lan_client.get("/ping", headers={"Host": "192.168.1.10:18000"}).status_code == 200
    assert lan_client.get("/ping", headers={"Host": "operator-laptop.local"}).status_code == 200
    # Loopback stays allowed; LAN extends the allowlist, never narrows it.
    assert lan_client.get("/ping", headers={"Host": "127.0.0.1"}).status_code == 200


def test_origin_header_is_checked_against_the_allowlist() -> None:
    client = _client()
    assert client.get("/ping", headers={"Origin": "http://localhost:18000"}).status_code == 200
    assert client.get("/ping", headers={"Origin": "http://127.0.0.1:18000"}).status_code == 200
    assert client.get("/ping", headers={"Origin": f"http://{FOREIGN_HOST}"}).status_code == 403
    lan_client = _client(lan_origins=("192.168.1.10",))
    assert lan_client.get("/ping", headers={"Origin": "http://192.168.1.10:18000"}).status_code == 200


def test_ipv6_loopback_host_is_accepted() -> None:
    client = _client()
    assert client.get("/ping", headers={"Host": "[::1]:18000"}).status_code == 200


def test_split_lan_origins_parses_comma_lists() -> None:
    assert not split_lan_origins(None)
    assert not split_lan_origins("")
    assert split_lan_origins("a.local, b.local") == ("a.local", "b.local")


def test_lan_env_name_is_the_configuration_entry_point() -> None:
    assert LAN_ORIGINS_ENV == "MODULO_LAN_ORIGINS"
