"""OpenCode provider backend (OpenAI-compatible zen gateway).

The ``opencode`` provider talks to the external zen HTTP gateway at
``https://opencode.ai/zen/go/v1`` via the OpenAI-compatible protocol.
Error classification for 5xx/connection failures lives in the base class
(``ProviderUnavailableError``) so both routes surface the same actionable
failure. The reliable path for opencode work is a ``sandbox_agent`` node
running the CLI (``opencode run ...``), not this HTTP provider — see the
repo-root AGENTS.md Lessons Learned.

FAR-1139: the gateway requires ``x-opencode-session`` (a stable session ID
per conversation) and a custom ``User-Agent`` identifying the client. Both
are sent via ``ChatOpenAI(default_headers=...)`` which the OpenAI SDK
applies at request level, independent of the caller-supplied httpx client.
The session ID is a per-backend-instance UUID (stable across all model
calls within a pipeline run, which is the closest thing to a "conversation"
we have — the hub creates a fresh backend per run).
"""

import uuid
from typing import Any

from modulo.model_backends.base import ProviderUnavailableError
from modulo.model_backends.module import OpenAICompatibleBackend
from modulo.version import get_version

__all__ = ["OpenCodeBackend", "ProviderUnavailableError"]

_OPENCODE_BASE_URL = "https://opencode.ai/zen/go/v1"


class OpenCodeBackend(OpenAICompatibleBackend):
    def __init__(self, api_key: str, model_id: str, **default_params: Any):
        # FAR-1139: session ID stable across all model calls within this
        # backend instance (= one pipeline run).  The gateway uses this for
        # routing and prompt caching.
        self._session_id = str(uuid.uuid4())
        user_agent = f"modulo/{get_version()}"

        # default_headers are applied by the OpenAI SDK at request level
        # (via _build_headers → _custom_headers), not at the httpx transport
        # level — so they survive a caller-supplied http_async_client.
        opencode_headers: dict[str, str] = {
            "x-opencode-session": self._session_id,
            "User-Agent": user_agent,
        }

        # Merge with any headers the caller already passed through default_params.
        existing_headers: dict[str, str] = dict(default_params.pop("default_headers", {}))
        existing_headers.update(opencode_headers)

        super().__init__(
            api_key=api_key,
            model_id=model_id,
            base_url=_OPENCODE_BASE_URL,
            provider="opencode",
            default_headers=existing_headers,
            **default_params,
        )

    @property
    def session_id(self) -> str:
        """The stable session ID sent in ``x-opencode-session``."""
        return self._session_id
