"""Unit tests for FAR-1038: TLS enforcement on remote Docker endpoints."""

from __future__ import annotations

import pytest

from modulo.core.runtime_provider.docker import (
    _COMPOSE_INTERNAL_HOST,
    _is_local_endpoint,
    _is_tls_configured,
    _validate_docker_endpoint_tls,
)

# ---------------------------------------------------------------------------
# _is_local_endpoint
# ---------------------------------------------------------------------------


class TestIsLocalEndpoint:
    def test_none_is_local(self) -> None:
        assert _is_local_endpoint(None) is True

    def test_empty_string_is_local(self) -> None:
        assert _is_local_endpoint("") is True

    def test_unix_socket_is_local(self) -> None:
        assert _is_local_endpoint("unix:///var/run/docker.sock") is True

    def test_bare_unix_path_is_local(self) -> None:
        assert _is_local_endpoint("/var/run/docker.sock") is True

    def test_compose_internal_proxy_is_local(self) -> None:
        assert _is_local_endpoint(f"tcp://{_COMPOSE_INTERNAL_HOST}:2375") is True

    def test_compose_internal_proxy_case_insensitive(self) -> None:
        assert _is_local_endpoint(f"tcp://{_COMPOSE_INTERNAL_HOST.upper()}:2375") is True

    def test_remote_tcp_is_not_local(self) -> None:
        assert _is_local_endpoint("tcp://remote-host:2375") is False

    def test_remote_ip_is_not_local(self) -> None:
        assert _is_local_endpoint("tcp://192.168.1.100:2375") is False

    def test_localhost_tcp_is_not_local(self) -> None:
        """A bare tcp://localhost:2375 is NOT compose-internal — require TLS."""
        assert _is_local_endpoint("tcp://localhost:2375") is False

    def test_docker_host_header_stripped(self) -> None:
        """The docker:// or tcp:// prefix is handled by urlparse."""
        assert _is_local_endpoint("tcp://docker-socket-proxy:2375") is True


# ---------------------------------------------------------------------------
# _is_tls_configured
# ---------------------------------------------------------------------------


class TestIsTlsConfigured:
    def test_not_configured_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("DOCKER_TLS_VERIFY", raising=False)
        monkeypatch.delenv("DOCKER_CERT_PATH", raising=False)
        assert _is_tls_configured() is False

    def test_tls_verify_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DOCKER_TLS_VERIFY", "1")
        assert _is_tls_configured() is True

    def test_tls_verify_false_string(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DOCKER_TLS_VERIFY", "false")
        assert _is_tls_configured() is False

    def test_tls_verify_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DOCKER_TLS_VERIFY", "0")
        assert _is_tls_configured() is False

    def test_cert_path_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DOCKER_CERT_PATH", "/certs")
        assert _is_tls_configured() is True

    def test_both_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DOCKER_TLS_VERIFY", "1")
        monkeypatch.setenv("DOCKER_CERT_PATH", "/certs")
        assert _is_tls_configured() is True


# ---------------------------------------------------------------------------
# _validate_docker_endpoint_tls
# ---------------------------------------------------------------------------


class TestValidateDockerEndpointTls:
    def test_none_endpoint_passes(self) -> None:
        _validate_docker_endpoint_tls(None)  # should not raise

    def test_unix_socket_passes(self) -> None:
        _validate_docker_endpoint_tls("unix:///var/run/docker.sock")

    def test_compose_internal_passes(self) -> None:
        _validate_docker_endpoint_tls(f"tcp://{_COMPOSE_INTERNAL_HOST}:2375")

    def test_remote_no_tls_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("DOCKER_TLS_VERIFY", raising=False)
        monkeypatch.delenv("DOCKER_CERT_PATH", raising=False)
        with pytest.raises(ValueError, match="requires TLS"):
            _validate_docker_endpoint_tls("tcp://remote-host:2375")

    def test_remote_no_tls_error_message_is_actionable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("DOCKER_TLS_VERIFY", raising=False)
        monkeypatch.delenv("DOCKER_CERT_PATH", raising=False)
        with pytest.raises(ValueError, match="DOCKER_TLS_VERIFY") as exc_info:
            _validate_docker_endpoint_tls("tcp://10.0.0.5:2375")
        msg = str(exc_info.value)
        assert "10.0.0.5:2375" in msg
        assert "DOCKER_CERT_PATH" in msg
        assert "bundled-runner-operator-guide" in msg

    def test_remote_with_tls_verify_passes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DOCKER_TLS_VERIFY", "1")
        monkeypatch.setenv("DOCKER_CERT_PATH", "/certs")
        _validate_docker_endpoint_tls("tcp://remote-host:2375")

    def test_remote_with_only_cert_path_passes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("DOCKER_TLS_VERIFY", raising=False)
        monkeypatch.setenv("DOCKER_CERT_PATH", "/certs")
        _validate_docker_endpoint_tls("tcp://remote-host:2375")


# ---------------------------------------------------------------------------
# DockerRuntimeProvider constructor — FAR-1038 registration gate
# ---------------------------------------------------------------------------


class TestDockerProviderTlsValidation:
    def test_constructor_rejects_remote_no_tls(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("DOCKER_TLS_VERIFY", raising=False)
        monkeypatch.delenv("DOCKER_CERT_PATH", raising=False)
        from modulo.core.runtime_provider.docker import DockerRuntimeProvider

        with pytest.raises(ValueError, match="requires TLS"):
            DockerRuntimeProvider(docker_host="tcp://remote-host:2375")

    def test_constructor_accepts_unix_socket(self) -> None:
        from modulo.core.runtime_provider.docker import DockerRuntimeProvider

        p = DockerRuntimeProvider(docker_host="unix:///var/run/docker.sock")
        assert p._docker_host == "unix:///var/run/docker.sock"

    def test_constructor_accepts_compose_internal(self) -> None:
        from modulo.core.runtime_provider.docker import DockerRuntimeProvider

        p = DockerRuntimeProvider(docker_host=f"tcp://{_COMPOSE_INTERNAL_HOST}:2375")
        assert p._docker_host == f"tcp://{_COMPOSE_INTERNAL_HOST}:2375"

    def test_constructor_accepts_remote_with_tls(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DOCKER_TLS_VERIFY", "1")
        monkeypatch.setenv("DOCKER_CERT_PATH", "/certs")
        from modulo.core.runtime_provider.docker import DockerRuntimeProvider

        p = DockerRuntimeProvider(docker_host="tcp://remote-host:2375")
        assert p._docker_host == "tcp://remote-host:2375"

    def test_constructor_accepts_none_endpoint(self) -> None:
        from modulo.core.runtime_provider.docker import DockerRuntimeProvider

        p = DockerRuntimeProvider(docker_host=None)
        # Falls through to env vars or local socket
        assert p._docker_host is None or p._docker_host == ""
