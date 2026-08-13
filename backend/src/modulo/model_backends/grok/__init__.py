from typing import Any

from modulo.model_backends.module import OpenAICompatibleBackend


class GrokBackend(OpenAICompatibleBackend):
    def __init__(self, api_key: str, model_id: str, **default_params: Any):
        super().__init__(
            api_key=api_key,
            model_id=model_id,
            base_url="https://api.x.ai/v1",
            provider="grok",
            **default_params,
        )
