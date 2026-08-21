from typing import Any

from modulo.model_backends.module import OpenAICompatibleBackend

FIREWORKS_BASE_URL = "https://api.fireworks.ai/inference/v1"


class FireworksBackend(OpenAICompatibleBackend):
    def __init__(self, api_key: str, model_id: str, **default_params: Any):
        super().__init__(
            api_key=api_key,
            model_id=model_id,
            base_url=FIREWORKS_BASE_URL,
            provider="fireworks",
            **default_params,
        )
