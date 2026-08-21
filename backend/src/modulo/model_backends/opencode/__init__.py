"""OpenCode provider backend (OpenAI-compatible zen gateway).

The ``opencode`` provider talks to the external zen HTTP gateway at
``https://opencode.ai/zen/go/v1`` via the OpenAI-compatible protocol. The
hub currently builds the plain ``OpenAICompatibleBackend`` for this provider
(this ``OpenCodeBackend`` subclass is a drop-in replacement that pins the
base URL); error classification for 5xx/connection failures lives in the
base class (``ProviderUnavailableError``) so both routes surface the same
actionable failure. The reliable path for opencode work is a
``sandbox_agent`` node running the CLI (``opencode run ...``), not this HTTP
provider — see the repo-root AGENTS.md Lessons Learned.
"""

from typing import Any

from modulo.model_backends.module import OpenAICompatibleBackend, ProviderUnavailableError

__all__ = ["OpenCodeBackend", "ProviderUnavailableError"]


class OpenCodeBackend(OpenAICompatibleBackend):
    def __init__(self, api_key: str, model_id: str, **default_params: Any):
        super().__init__(
            api_key=api_key,
            model_id=model_id,
            base_url="https://opencode.ai/zen/go/v1",
            provider="opencode",
            **default_params,
        )
