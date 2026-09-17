"""Real protocol-path validation for local OpenAI-compatible model backends.

Instead of mocking the SDK, these tests start a real in-process HTTP server
that implements the OpenAI-compatible ``/v1/models`` and
``/v1/chat/completions`` endpoints.  Every local provider backend is
instantiated with its ``base_url`` pointed at this server, then:

* ``health_check()`` is called and must report healthy,
* a real completion is performed through the backend and the returned content
  matches what the server sent,
* the server asserts it received the expected method, path, and body fields.

This proves the wire contract — not just that the constructor was called
with the right arguments, but that the backend can actually talk to a
server and get a completion back.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler
from typing import Any

import pytest
from langchain_core.messages import HumanMessage

from modulo.model_backends.jan import DEFAULT_JAN_BASE_URL, JanBackend
from modulo.model_backends.llamacpp import DEFAULT_LLAMACPP_BASE_URL, LLamaCppBackend
from modulo.model_backends.lm_studio import DEFAULT_LM_STUDIO_BASE_URL, LmStudioBackend
from modulo.model_backends.localai import DEFAULT_LOCALAI_BASE_URL, LocalAIBackend
from modulo.model_backends.ollama import DEFAULT_OLLAMA_BASE_URL, OllamaBackend
from modulo.model_backends.tgi import DEFAULT_TGI_BASE_URL, TgiBackend
from modulo.model_backends.vllm import DEFAULT_VLLM_BASE_URL, VllmBackend

# httpx connection-pool sockets are GC'd after backend.aclose() but the
# finaliser order is not deterministic — suppress the harmless warning.
pytestmark = pytest.mark.filterwarnings("ignore::ResourceWarning")

_MODEL_ID = "test-model"
_EXPECTED_CONTENT = "Hello from the test server!"
_REQUEST_LOG: list[dict[str, Any]] = []


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _OpenAIHandler(BaseHTTPRequestHandler):
    """Minimal OpenAI-compatible server for testing."""

    def log_message(self, fmt: str, *args: Any) -> None:
        pass  # suppress request logging during tests

    def do_GET(self) -> None:
        if self.path.rstrip("/") == "/v1/models":
            self._send_json(
                200,
                {
                    "object": "list",
                    "data": [{"id": _MODEL_ID, "object": "model", "owned_by": "test"}],
                },
            )
        else:
            self._send_json(404, {"error": {"message": f"Not found: {self.path}"}})

    def do_POST(self) -> None:
        if self.path.rstrip("/") == "/v1/chat/completions":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}
            _REQUEST_LOG.append(
                {
                    "method": "POST",
                    "path": self.path,
                    "model": body.get("model"),
                    "messages": body.get("messages"),
                }
            )
            self._send_json(
                200,
                {
                    "id": "chatcmpl-test",
                    "object": "chat.completion",
                    "model": body.get("model", _MODEL_ID),
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": _EXPECTED_CONTENT},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 5, "completion_tokens": 4, "total_tokens": 9},
                },
            )
        else:
            self._send_json(404, {"error": {"message": f"Not found: {self.path}"}})

    def _send_json(self, status: int, data: dict[str, Any]) -> None:
        payload = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class _ThreadedHTTPServer(threading.Thread):
    """Daemon thread running a simple HTTP server on a random free port."""

    def __init__(self) -> None:
        super().__init__(daemon=True)
        self.port = _find_free_port()
        self._started_event = threading.Event()
        self._server: Any = None

    def run(self) -> None:
        from http.server import HTTPServer

        self._server = HTTPServer(("127.0.0.1", self.port), _OpenAIHandler)
        self._started_event.set()
        self._server.serve_forever()

    def wait_until_ready(self, timeout: float = 5.0) -> None:
        if not self._started_event.wait(timeout):
            msg = "Test server did not start in time"
            raise RuntimeError(msg)
        # Also verify the port actually accepts connections
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.5):
                    return
            except OSError:
                time.sleep(0.05)
        msg = f"Test server port {self.port} never accepted connections"
        raise RuntimeError(msg)

    def shutdown(self) -> None:
        if self._server is not None:
            self._server.shutdown()


@pytest.fixture
def _ssrf_allow_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Allow loopback addresses in the SSRF protection for test server access."""
    monkeypatch.setenv("SSRF_ALLOW_PRIVATE_RANGES", "127.0.0.0/8,::1/128")


@pytest.fixture
def openai_server(_ssrf_allow_loopback: None) -> _ThreadedHTTPServer:  # type: ignore[misc]
    """Start and yield a real OpenAI-compatible test server, then shut it down."""
    _REQUEST_LOG.clear()
    server = _ThreadedHTTPServer()
    server.start()
    server.wait_until_ready()
    yield server
    server.shutdown()


# ---------------------------------------------------------------------------
# Provider matrix
# ---------------------------------------------------------------------------

_LOCAL_BACKENDS = [
    pytest.param(OllamaBackend, DEFAULT_OLLAMA_BASE_URL, "ollama", id="ollama"),
    pytest.param(LLamaCppBackend, DEFAULT_LLAMACPP_BASE_URL, "llamacpp", id="llamacpp"),
    pytest.param(LocalAIBackend, DEFAULT_LOCALAI_BASE_URL, "localai", id="localai"),
    pytest.param(VllmBackend, DEFAULT_VLLM_BASE_URL, "vllm", id="vllm"),
    pytest.param(TgiBackend, DEFAULT_TGI_BASE_URL, "tgi", id="tgi"),
    pytest.param(JanBackend, DEFAULT_JAN_BASE_URL, "jan", id="jan"),
    pytest.param(LmStudioBackend, DEFAULT_LM_STUDIO_BASE_URL, "lm_studio", id="lm_studio"),
]


# ---------------------------------------------------------------------------
# Default base_url constant assertions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("backend_cls", "expected_url"),
    [
        (OllamaBackend, "http://localhost:11434/v1"),
        (LLamaCppBackend, "http://localhost:8080/v1"),
        (LocalAIBackend, "http://localhost:8080/v1"),
        (VllmBackend, "http://localhost:8000/v1"),
        (TgiBackend, "http://localhost:8080/v1"),
        (JanBackend, "http://localhost:1337/v1"),
        (LmStudioBackend, "http://localhost:1234/v1"),
    ],
    ids=["ollama", "llamacpp", "localai", "vllm", "tgi", "jan", "lm_studio"],
)
def test_default_base_url_constant(backend_cls: type, expected_url: str) -> None:
    """Each provider's DEFAULT_*_BASE_URL must match the documented value."""
    defaults = {
        OllamaBackend: DEFAULT_OLLAMA_BASE_URL,
        LLamaCppBackend: DEFAULT_LLAMACPP_BASE_URL,
        LocalAIBackend: DEFAULT_LOCALAI_BASE_URL,
        VllmBackend: DEFAULT_VLLM_BASE_URL,
        TgiBackend: DEFAULT_TGI_BASE_URL,
        JanBackend: DEFAULT_JAN_BASE_URL,
        LmStudioBackend: DEFAULT_LM_STUDIO_BASE_URL,
    }
    assert defaults[backend_cls] == expected_url


# ---------------------------------------------------------------------------
# Real protocol-path tests — parametrised over all seven providers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("backend_cls", "default_url", "provider"),
    _LOCAL_BACKENDS,
)
class TestRealProtocolLocalOpenAIBackends:
    """Validate the full wire path against a real HTTP server."""

    def _make_base_url(self, server: _ThreadedHTTPServer) -> str:
        return f"http://127.0.0.1:{server.port}/v1"

    async def test_health_check(
        self,
        backend_cls: type,
        default_url: str,
        provider: str,
        openai_server: _ThreadedHTTPServer,
    ) -> None:
        base_url = self._make_base_url(openai_server)
        backend = backend_cls(api_key=None, model_id=_MODEL_ID, base_url=base_url)
        try:
            result = await backend.health_check()
            assert result.ok is True, f"health_check failed: {result.detail}"
        finally:
            await backend.aclose()

    async def test_invoke_returns_expected_content(
        self,
        backend_cls: type,
        default_url: str,
        provider: str,
        openai_server: _ThreadedHTTPServer,
    ) -> None:
        base_url = self._make_base_url(openai_server)
        backend = backend_cls(api_key=None, model_id=_MODEL_ID, base_url=base_url)
        try:
            response = await backend.invoke([HumanMessage(content="Hi there")])
            assert _EXPECTED_CONTENT in response.content
        finally:
            await backend.aclose()

    async def test_server_received_correct_request(
        self,
        backend_cls: type,
        default_url: str,
        provider: str,
        openai_server: _ThreadedHTTPServer,
    ) -> None:
        base_url = self._make_base_url(openai_server)
        backend = backend_cls(api_key=None, model_id=_MODEL_ID, base_url=base_url)
        try:
            _REQUEST_LOG.clear()
            await backend.invoke([HumanMessage(content="Test message")])
            assert len(_REQUEST_LOG) >= 1, "Server received no POST requests"
            last = _REQUEST_LOG[-1]
            assert last["method"] == "POST"
            assert "/chat/completions" in last["path"]
            assert last["model"] == _MODEL_ID
            assert any(m.get("content") == "Test message" for m in (last["messages"] or []))
        finally:
            await backend.aclose()

    async def test_backend_base_url_points_to_server(
        self,
        backend_cls: type,
        default_url: str,
        provider: str,
        openai_server: _ThreadedHTTPServer,
    ) -> None:
        """Constructing with base_url must store the test server URL, not the default."""
        base_url = self._make_base_url(openai_server)
        backend = backend_cls(api_key=None, model_id=_MODEL_ID, base_url=base_url)
        try:
            assert backend.base_url == base_url.rstrip("/")
        finally:
            await backend.aclose()

    async def test_default_base_url_when_no_override(
        self,
        backend_cls: type,
        default_url: str,
        provider: str,
        openai_server: _ThreadedHTTPServer,
    ) -> None:
        """Without an explicit base_url, the backend uses its provider default."""
        backend = backend_cls(api_key=None, model_id=_MODEL_ID)
        try:
            assert backend.base_url == default_url
        finally:
            await backend.aclose()
