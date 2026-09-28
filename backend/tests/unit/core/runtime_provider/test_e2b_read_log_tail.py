"""FAR-1050 R1: ``read_log_tail`` primitive + E2B implementation.

Exercises, without a live sandbox or network:

1. The ABC's ``read_log_tail`` default raises the typed
   ``ProviderCapabilityUnsupportedError`` (error honesty — never a raw
   ``NotImplementedError``) for a provider that does not override it.
2. ``E2BRuntimeProvider.read_log_tail`` owns the log probe end to end
   (FAR-1050 R6 deleted the legacy ``node_runner`` urllib helper):
   preferred-level reordering, ``[-max_bytes:]`` final bound,
   ``min(4000, max_bytes)`` raw fallback, ``b""`` on invalid ref or fetch
   failure (never raises).
3. **Pinned payloads**: the payloads the R1 parity suite pinned are
   asserted directly against the surviving primitive.
4. Key fallback: runtime bridge → legacy ``E2B_API_KEY`` → constructor key.
5. Hostname confinement: ``api.e2b.app`` appears only in the provider module.
"""

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

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


async def test_e2b_read_log_tail_skips_non_dict_and_empty_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    """Entry filtering drops non-dict entries and entries with no usable text.

    Pins the ``_combine_log_entries`` early-returns: a non-dict element, a
    dict whose message/fields are empty (both the empty-string and the
    missing-key arm of ``_log_entry_text``), leaving only the real line.
    """
    payload = (
        b'{"logEntries": ['
        b'"not-a-dict",'  # non-dict -> skip
        b'{"message": "", "level": "error"},'  # empty message -> skip
        b'{"fields": "", "level": "debug"},'  # empty fields -> skip
        b'{"level": "info"},'  # no message/fields -> skip
        b'{"message": "kept", "level": "info"}'  # survives
        b"]}"
    )
    monkeypatch.setenv("E2B_API_KEY", "test-key")
    monkeypatch.delenv("MODULO_E2B_API_KEY", raising=False)
    provider = E2BRuntimeProvider(api_key="test-key")
    with patch("urllib.request.urlopen", lambda req, timeout: _fake_urlopen(payload)):
        tail = await provider.read_log_tail("sbx-1", max_bytes=6000)
    assert tail == b"kept"


async def test_e2b_read_log_tail_no_key_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """Terminal key arm: bridge + legacy env absent and constructor key falsy.

    ``__init__`` guarantees a non-empty ``_api_key``, so the only way to reach
    the defensive ``if not api_key`` guard is to clear it — proving the guard
    returns empty without touching the network.
    """
    monkeypatch.delenv("MODULO_E2B_API_KEY", raising=False)
    monkeypatch.delenv("E2B_API_KEY", raising=False)
    provider = E2BRuntimeProvider(api_key="ctor-key")
    provider._api_key = None  # type: ignore[assignment]
    with patch("urllib.request.urlopen") as urlopen:
        assert not await provider.read_log_tail("sbx-1", max_bytes=100)
    urlopen.assert_not_called()


# ---------------------------------------------------------------------------
# 3. Pinned payload expectations over the primitive (R6: the legacy helper
#    this parity suite compared against is deleted, so these pin the SAME
#    payloads directly against the surviving primitive).
# ---------------------------------------------------------------------------


_PAYOUT_EXPECTATIONS: dict[bytes, bytes] = {
    (
        b'{"logEntries": ['
        b'{"message": "error line", "level": "error"},'
        b'{"message": "plain line", "level": "debug"},'
        b'{"fields": "fields-only", "level": "warn"}'
        b"]}"
    ): b"error line\nfields-only\nplain line",
    b'{"other": 1}': b'{"other": 1}',
    b"not json": b"not json",
}


@pytest.mark.parametrize(
    "payload", _PARITY_PAYLOADS, ids=["preferred-levels", "non-list", "invalid-json", "over-bound"]
)
async def test_primitive_handles_every_pinned_payload(payload: bytes, monkeypatch: pytest.MonkeyPatch) -> None:
    """Each pinned payload yields the expected tail through the primitive."""
    monkeypatch.delenv("MODULO_E2B_API_KEY", raising=False)
    monkeypatch.setenv("E2B_API_KEY", "parity-key")
    provider = E2BRuntimeProvider(api_key="parity-key")
    with patch("urllib.request.urlopen", lambda req, timeout: _fake_urlopen(payload)):
        tail = await provider.read_log_tail("sbx-parity", max_bytes=6000)

    if payload in _PAYOUT_EXPECTATIONS:
        assert tail == _PAYOUT_EXPECTATIONS[payload]
    else:
        # over-bound payload: the joined entry text is bounded to max_bytes.
        assert tail == b"x" * 6000


async def test_primitive_fails_open_on_network_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fetch failure yields an empty tail \u2014 never raises (T6 contract)."""
    monkeypatch.setenv("E2B_API_KEY", "parity-key")
    provider = E2BRuntimeProvider(api_key="parity-key")
    with patch("urllib.request.urlopen", side_effect=OSError("down")):
        assert not await provider.read_log_tail("sbx-parity", max_bytes=6000)


# ---------------------------------------------------------------------------
# 4. Hostname confinement (R6): ``api.e2b.app`` lives ONLY in the provider
#    module \u2014 node_runner's legacy log-probe island was deleted in R6.
# ---------------------------------------------------------------------------


def test_hostname_confined_to_the_provider_module() -> None:
    from modulo.core.pipeline_engine import node_runner as nr

    node_runner_path = Path(nr.__file__)
    file_src = node_runner_path.read_text(encoding="utf-8")
    assert _HOSTNAME not in file_src
    # The provider module owns the hostname.
    e2b_provider_src = (node_runner_path.parents[1] / "runtime_provider" / "e2b.py").read_text(encoding="utf-8")
    assert _HOSTNAME in e2b_provider_src
