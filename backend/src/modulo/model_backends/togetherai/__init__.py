from typing import Any

from langchain_openai import ChatOpenAI  # noqa: F401

from modulo.model_backends.module import OpenAICompatibleBackend

TOGETHERAI_BASE_URL = "https://api.together.xyz/v1"


class TogetherAIBackend(OpenAICompatibleBackend):
    def __init__(self, api_key: str, model_id: str, **default_params: Any):
        super().__init__(
            api_key=api_key,
            model_id=model_id,
            base_url=TOGETHERAI_BASE_URL,
            provider="togetherai",
            **default_params,
        )
