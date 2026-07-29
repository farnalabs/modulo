from typing import Any

from modulo.model_backends.module import OpenAICompatibleBackend


class OpenCodeBackend(OpenAICompatibleBackend):
    def __init__(self, api_key: str, model_id: str, **default_params: Any):
        super().__init__(
            api_key=api_key,
            model_id=model_id,
            base_url="https://opencode.ai/zen/go/v1",
            provider="opencode",
            **default_params,
        )
