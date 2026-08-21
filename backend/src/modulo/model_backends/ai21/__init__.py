from typing import Any

from modulo.model_backends.module import OpenAICompatibleBackend

AI21_BASE_URL = "https://api.ai21.com/studio/v1"


class Ai21Backend(OpenAICompatibleBackend):
    def __init__(self, api_key: str, model_id: str, **default_params: Any):
        super().__init__(
            api_key=api_key,
            model_id=model_id,
            base_url=AI21_BASE_URL,
            provider="ai21",
            **default_params,
        )
