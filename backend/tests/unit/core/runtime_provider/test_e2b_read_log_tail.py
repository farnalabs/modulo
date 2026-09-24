"""FAR-1050 R1: ``read_log_tail`` primitive + E2B implementation.

Exercises, without a live sandbox or network:

1. The ABC's ``read_log_tail`` default raises the typed
   ``ProviderCapabilityUnsupportedError`` (error honesty — never a raw
   ``NotImplementedError``) for a provider that does not override it.
2. ``E2BRuntimeProvider.read_log_tail`` parses/bounds the payload exactly
   like the legacy ``node_runner._fetch_sandbox_log_tail`` helper it was
   moved from: preferred-level reordering, ``[-max_bytes:]`` final bound,
   ``min(4000, max_bytes)`` raw fallback, ``b""`` on invalid ref or fetch
   failure (never raises).
3. **Content parity**: legacy-urllib-fixture vs the primitive over the
   same payload — both paths must produce identical tail content.
4. Key fallback: runtime bridge → legacy ``E2B_API_KEY`` → constructor key
   (the legacy probe's chain, preserved on the ABC path).
"""

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from modulo.core.pipeline_engine.node_runner import _fetch_sandbox_log_tail
from modulo.core.runtime_provider import (
    ProviderCapabilityUnsupportedError,
    RuntimeProvider,
    WorkspaceSpec,
)
from modulo.core.runtime_provider.e2b import E2BRuntimeProvider

_HOSTNAME = "api.e2b.app"

# Payloads pinned across legacy + primitive so parity is checked over the
# same bytes: preferred-level window, non-list payload (raw fallback),
# invalid JSON (raw fallback), and an over-bound payload ([-6000:] slice).
_PARITY_PAYLOADS: list[bytes] = [
    (
        b'{"logEntries": ['
        b'{"message": "error line", "level": "error"},'
        b'{"message": "plain line", "level": "debug"},'
        b'{"fields": "fields-only", "level": "warn"}'
        b"]}"
    ),
    b'{"other": 1}',
    b"not json",
    b'{"logEntries": [{"message": "' + (b"x" * 7000) + b'", "level": "info"}]}',
]


def _fake_urlopen(payload: bytes) -> Any:
    """urlopen stand-in returning *payload* (shape of ``http.client`` response)."""

    class _Resp:
        def __enter__(self) -> Any:
            return self

        def __exit__(self, *args: object) -> bool:
            return False

        def read(self) -> bytes:
            return payload

    return _Resp()


class _NoLogTailProvider(RuntimeProvider):
    """Concrete provider that does NOT override ``read_log_tail``."""

    async def create_workspace(self, spec: WorkspaceSpec) -> str:
        return "ws"

    async def exec_command(
        self,
        provider_ref: str,
        command: list[str],
        *,
        cmd_timeout: int | None = None,
    ) -> Any:
        raise NotImplementedError

    async def destroy_workspace(self, provider_ref: str) -> None:
        return None

    async def get_workspace_status(self, provider_ref: str) -> str:
        return "running"


# ---------------------------------------------------------------------------
# 1. ABC default — typed refusal (error honesty carve-out)
# ---------------------------------------------------------------------------


async def test_abc_default_read_log_tail_raises_typed_capability_unsupported() -> None:
    with pytest.raises(ProviderCapabilityUnsupportedError, match="read_log_tail") as exc_info:
        await _NoLogTailProvider().read_log_tail("sbx-ref", max_bytes=100)
    assert not isinstance(exc_info.value, NotImplementedError)


# ---------------------------------------------------------------------------
# 2. E2B implementation behaviours
# ---------------------------------------------------------------------------


async def test_e2b_read_log_tail_parses_and_bounds_like_legacy(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = (
        b'{"logEntries": ['
        b'{"message": "error line", "level": "error"},'
        b'{"message": "plain line", "level": "debug"},'
        b'{"fields": "fields-only", "level": "warn"}'
        b"]}"
    )
    monkeypatch.setenv("E2B_API_KEY", "test-key")
    monkeypatch.delenv("MODULO_E2B_API_KEY", raising=False)
    provider = E2BRuntimeProvider(api_key="test-key")
    with patch("urllib.request.urlopen", side_effect=lambda req, timeout: _fake_urlopen(payload)) as urlopen:
        tail = await provider.read_log_tail("sbx-1", max_bytes=6000)
    urlopen.assert_called_once()
    # Preferred levels sort ahead; the window keeps the last entries overall
    # (same expectation as the legacy helper's unit test).
    assert tail == b"error line\nfields-only\nplain line"


async def test_e2b_read_log_tail_invalid_ref_returns_empty_without_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("E2B_API_KEY", "test-key")
    provider = E2BRuntimeProvider(api_key="test-key")
    with patch("urllib.request.urlopen") as urlopen:
        assert not await provider.read_log_tail("", max_bytes=100)
        assert not await provider.read_log_tail(None, max_bytes=100)  # type: ignore[arg-type]
    urlopen.assert_not_called()


async def test_e2b_read_log_tail_network_failure_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fetch failure yields ``b""`` — never raises (T6 contract)."""
    monkeypatch.setenv("E2B_API_KEY", "test-key")
    provider = E2BRuntimeProvider(api_key="test-key")
    with patch("urllib.request.urlopen", side_effect=OSError("network down")):
        assert not await provider.read_log_tail("sbx-netfail", max_bytes=100)


async def test_e2b_read_log_tail_max_bytes_bounds_newest_content(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = b'{"logEntries": [{"message": "' + (b"y" * 500) + b'", "level": "info"}]}'
    monkeypatch.setenv("E2B_API_KEY", "test-key")
    provider = E2BRuntimeProvider(api_key="test-key")
    with patch("urllib.request.urlopen", lambda req, timeout: _fake_urlopen(payload)):
        tail = await provider.read_log_tail("sbx-1", max_bytes=50)
    assert len(tail) <= 50
    assert tail == b"y" * 50


async def test_e2b_read_log_tail_key_falls_back_to_legacy_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Key chain: bridge → legacy ``E2B_API_KEY`` → constructor key.

    With no bridge value and no env var the constructor-held key is used;
    the legacy env var wins over it (legacy helper's order).
    """
    monkeypatch.delenv("MODULO_E2B_API_KEY", raising=False)
    monkeypatch.setenv("E2B_API_KEY", "legacy-env-key")
    provider = E2BRuntimeProvider(api_key="ctor-key")
    captured: list[str] = []

    def _capture(req: Any, timeout: float) -> Any:
        # urllib normalizes header names via str.capitalize().
        captured.append(req.get_header("X-api-key"))
        return _fake_urlopen(b'{"logEntries": []}')

    with patch("urllib.request.urlopen", side_effect=_capture):
        await provider.read_log_tail("sbx-1", max_bytes=100)
    assert captured == ["legacy-env-key"]


# ---------------------------------------------------------------------------
# 3. Content parity: legacy urllib fixture vs the primitive
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload", _PARITY_PAYLOADS, ids=["preferred-levels", "non-list", "invalid-json", "over-bound"]
)
async def test_content_parity_legacy_vs_primitive(payload: bytes, monkeypatch: pytest.MonkeyPatch) -> None:
    """Same payload through both paths → byte-identical decoded tail."""
    monkeypatch.delenv("MODULO_E2B_API_KEY", raising=False)
    monkeypatch.setenv("E2B_API_KEY", "parity-key")

    with patch("urllib.request.urlopen", lambda req, timeout: _fake_urlopen(payload)):
        legacy = await _fetch_sandbox_log_tail("sbx-parity")

    provider = E2BRuntimeProvider(api_key="parity-key")
    with patch("urllib.request.urlopen", lambda req, timeout: _fake_urlopen(payload)):
        primitive = await provider.read_log_tail("sbx-parity", max_bytes=6000)

    assert primitive.decode("utf-8", errors="replace") == legacy


async def test_content_parity_network_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both paths fail open to an empty tail when the endpoint is down."""
    monkeypatch.setenv("E2B_API_KEY", "parity-key")
    with patch("urllib.request.urlopen", side_effect=OSError("down")):
        legacy = await _fetch_sandbox_log_tail("sbx-parity")
    provider = E2BRuntimeProvider(api_key="parity-key")
    with patch("urllib.request.urlopen", side_effect=OSError("down")):
        primitive = await provider.read_log_tail("sbx-parity", max_bytes=6000)
    assert not legacy
    assert not primitive


# ---------------------------------------------------------------------------
# 4. Hostname scanner precondition (plan §5): ``api.e2b.app`` confined to
#    the legacy helper island in node_runner — the flag-ON helper and the
#    dispatch body carry no hostname.
# ---------------------------------------------------------------------------


def test_hostname_confined_to_legacy_fetch_helper() -> None:
    import inspect

    from modulo.core.pipeline_engine import node_runner as nr

    node_runner_path = Path(nr.__file__)
    file_src = node_runner_path.read_text(encoding="utf-8")
    legacy_src = inspect.getsource(nr._fetch_sandbox_log_tail)
    flag_on_src = inspect.getsource(nr._read_log_tail_via_provider)
    dispatch_src = inspect.getsource(nr._sandbox_agent_impl)

    # The legacy island still holds the hostname (removed only at slice R6)...
    assert file_src.count(_HOSTNAME) > 0
    # ...and EVERY occurrence in the file lives inside that island, so the
    # flag-ON helper and the dispatch body are hostname-free.
    assert file_src.count(_HOSTNAME) == legacy_src.count(_HOSTNAME)
    assert _HOSTNAME not in flag_on_src
    assert _HOSTNAME not in dispatch_src
    # The moved primitive now owns the hostname on the ABC path.
    e2b_provider_src = (node_runner_path.parents[1] / "runtime_provider" / "e2b.py").read_text(encoding="utf-8")
    assert _HOSTNAME in e2b_provider_src
